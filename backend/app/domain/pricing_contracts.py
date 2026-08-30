from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.customer_facts import CUSTOMER_FACT_SOURCES
from app.domain.models import ServiceRequirement

PRICING_CONTRACT_VERSION = "2026-08-30.3"

_EBS_STORAGE_TYPES = {"gp2", "gp3", "io1", "io2", "st1", "sc1", "standard"}
_DISK_FIELDS = {
    "system_disk_gib",
    "total_system_disk_gib",
    "additional_ebs_volumes",
    "volume_type",
    "ebs_iops",
    "ebs_throughput_mbps",
}


@dataclass(frozen=True, slots=True)
class PricingContractIssue:
    field: str
    message: str
    evidence: str = ""


def _service_key(requirement: ServiceRequirement) -> str:
    key = requirement.service.strip().casefold().replace("-", "_")
    return {
        "amazon_ec2": "ec2",
        "redis": "elasticache",
        "amazon_elasticache": "elasticache",
        "aurora": "rds",
        "alb": "elb",
        "elbv2": "elb",
        "dynamo_db": "dynamodb",
        "amazon_dynamodb": "dynamodb",
        "amazon_s3": "s3",
        "aws_backup": "backup",
        "amazon_backup": "backup",
    }.get(key, key)


def _path(field: str) -> str:
    return f"requirements.{field}"


def _customer_owned(requirement: ServiceRequirement, field: str) -> bool:
    path = _path(field)
    return (
        requirement.field_sources.get(path) in CUSTOMER_FACT_SOURCES
        or path in requirement.locked_fields
    )


def _remove_field(requirement: ServiceRequirement, field: str) -> None:
    path = _path(field)
    requirement.requirements.pop(field, None)
    requirement.field_sources.pop(path, None)
    requirement.field_evidence.pop(path, None)
    requirement.field_match_policies.pop(field, None)
    requirement.field_scopes.pop(field, None)
    requirement.locked_fields = [item for item in requirement.locked_fields if item != path]


def _move_field(
    requirement: ServiceRequirement,
    source_field: str,
    target_field: str,
) -> None:
    source_path = _path(source_field)
    target_path = _path(target_field)
    requirement.requirements[target_field] = requirement.requirements[source_field]
    if source_path in requirement.field_sources:
        requirement.field_sources[target_path] = requirement.field_sources[source_path]
    if source_path in requirement.field_evidence:
        requirement.field_evidence[target_path] = requirement.field_evidence[source_path]
    if source_field in requirement.field_match_policies:
        requirement.field_match_policies[target_field] = requirement.field_match_policies[
            source_field
        ]
    if source_field in requirement.field_scopes:
        requirement.field_scopes[target_field] = requirement.field_scopes[source_field]
    if source_path in requirement.locked_fields:
        requirement.locked_fields = sorted(
            set(requirement.locked_fields) | {target_path}
        )
    _remove_field(requirement, source_field)


def _same_customer_fact(
    requirement: ServiceRequirement,
    left_field: str,
    right_field: str,
) -> bool:
    """Return whether two fields came from one customer statement.

    AI cleaning can emit both a product-specific field and its generic alias,
    such as ``log_ingestion_gib`` plus ``data_in_gib``. They are one billable
    fact when both value and evidence agree. Equal numbers from different
    phrases deliberately remain independent.
    """

    left = requirement.requirements.get(left_field)
    right = requirement.requirements.get(right_field)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if abs(float(left) - float(right)) > 1e-9:
            return False
    elif left != right:
        return False

    def normalized_evidence(field: str) -> str:
        evidence = requirement.field_evidence.get(_path(field), "")
        return "".join(
            character.casefold()
            for character in evidence
            if character.isalnum()
        ).removeprefix("每月")

    left_evidence = normalized_evidence(left_field)
    right_evidence = normalized_evidence(right_field)
    return bool(
        left_evidence
        and right_evidence
        and (
            left_evidence == right_evidence
            or left_evidence in right_evidence
            or right_evidence in left_evidence
        )
    )


def _remove_duplicate_alias(
    requirement: ServiceRequirement,
    alias_field: str,
    canonical_field: str,
) -> None:
    if (
        alias_field in requirement.requirements
        and canonical_field in requirement.requirements
        and _same_customer_fact(requirement, alias_field, canonical_field)
    ):
        _remove_field(requirement, alias_field)


def _normalize_deployment_count_alias(
    requirement: ServiceRequirement,
    count_field: str,
) -> None:
    """Give one independently deployed component count one fact owner.

    Some product templates expose a descriptive count field even though the
    shared component contract already owns the same count as ``quantity``.
    Keeping both customer-owned rows makes the fact ledger demand that one
    phrase be billed twice.  Move a sole count into ``quantity`` and collapse
    a same-evidence duplicate; preserve genuinely distinct statements.
    """

    count_path = _path(count_field)
    count = requirement.requirements.get(count_field)
    if not isinstance(count, (int, float)) or isinstance(count, bool) or count <= 0:
        return

    quantity_source = requirement.field_sources.get("quantity")
    quantity_is_customer_owned = quantity_source in CUSTOMER_FACT_SOURCES
    count_source = requirement.field_sources.get(count_path)
    count_evidence = requirement.field_evidence.get(count_path, "")

    if not quantity_is_customer_owned:
        requirement.quantity = int(count) if float(count).is_integer() else float(count)
        if count_source:
            requirement.field_sources["quantity"] = count_source
        if count_evidence:
            requirement.field_evidence["quantity"] = count_evidence
        if count_path in requirement.locked_fields:
            requirement.locked_fields = sorted(
                set(requirement.locked_fields) | {"quantity"}
            )
        _remove_field(requirement, count_field)
        return

    quantity_evidence = requirement.field_evidence.get("quantity", "")
    same_value = abs(float(requirement.quantity) - float(count)) <= 1e-9
    normalized_quantity_evidence = "".join(
        character.casefold()
        for character in quantity_evidence
        if character.isalnum()
    )
    normalized_count_evidence = "".join(
        character.casefold()
        for character in count_evidence
        if character.isalnum()
    )
    same_evidence = bool(
        normalized_quantity_evidence
        and normalized_count_evidence
        and (
            normalized_quantity_evidence == normalized_count_evidence
            or normalized_quantity_evidence in normalized_count_evidence
            or normalized_count_evidence in normalized_quantity_evidence
        )
    )
    if same_value and same_evidence:
        _remove_field(requirement, count_field)


def apply_pricing_contract(
    requirement: ServiceRequirement,
) -> list[PricingContractIssue]:
    """Normalize safe defaults and reject cross-product field semantics.

    Templates describe what the cleaning AI may extract.  This contract is a
    separate, deterministic boundary describing what the selected AWS product
    is allowed to bill.  It prevents EC2/EBS defaults from leaking into Aurora,
    ElastiCache, S3 or other managed services.
    """

    service = _service_key(requirement)
    fields = requirement.requirements
    issues: list[PricingContractIssue] = []

    # ``hours_per_month`` has exactly one owner: the shared top-level runtime
    # column.  Dynamically discovered hourly AWS rows used to add the same
    # field inside ``requirements`` as well.  Canonicalize current and saved
    # drafts here, before the fact ledger is sealed, so every adapter consumes
    # one stable path and one piece of customer evidence.
    nested_hours = fields.get("hours_per_month")
    if isinstance(nested_hours, (int, float)) and not isinstance(nested_hours, bool):
        nested_path = _path("hours_per_month")
        nested_source = requirement.field_sources.get(nested_path)
        top_source = requirement.field_sources.get("hours_per_month")
        nested_evidence = requirement.field_evidence.get(nested_path, "")
        top_evidence = requirement.field_evidence.get("hours_per_month", "")
        top_is_customer = top_source in CUSTOMER_FACT_SOURCES
        same_runtime = abs(float(requirement.hours_per_month) - float(nested_hours)) <= 1e-9
        if not top_is_customer:
            requirement.hours_per_month = float(nested_hours)
            if nested_source:
                requirement.field_sources["hours_per_month"] = nested_source
            if nested_evidence:
                requirement.field_evidence["hours_per_month"] = nested_evidence
            if nested_path in requirement.locked_fields:
                requirement.locked_fields = sorted(
                    set(requirement.locked_fields) | {"hours_per_month"}
                )
            _remove_field(requirement, "hours_per_month")
        elif same_runtime and (
            not nested_evidence
            or not top_evidence
            or re.sub(r"\s+", "", nested_evidence).casefold()
            == re.sub(r"\s+", "", top_evidence).casefold()
        ):
            _remove_field(requirement, "hours_per_month")
        else:
            issues.append(
                PricingContractIssue(
                    field="hours_per_month",
                    message=(
                        "同一组件出现两个不同的每月运行时长；必须保留一个明确的组件运行时长"
                    ),
                    evidence="；".join(
                        evidence for evidence in (top_evidence, nested_evidence) if evidence
                    ),
                )
            )

    # A deployable service count is represented once at the shared component
    # boundary. Product-specific names remain valid cleaning inputs, but they
    # are aliases rather than second billable facts. Internal topology counts
    # (Broker/data nodes/replicas) are intentionally not listed here.
    deployment_count_aliases = {
        "eks": "cluster_count",
        "nat_gateway": "gateway_count",
    }
    if count_alias := deployment_count_aliases.get(service):
        _normalize_deployment_count_alias(requirement, count_alias)

    # Product-specific fields are authoritative over generic AI aliases when
    # both point to the same customer evidence. Collapse these before sealing
    # the fact ledger so dedicated and generic adapters cannot charge the same
    # statement twice. New services extend this declarative contract rather
    # than adding natural-language keyword rules.
    semantic_aliases: dict[str, dict[str, str]] = {
        "cloudwatch": {
            "data_in_gib": "log_ingestion_gib",
            "storage_gib": "log_storage_gib",
        },
        "cloudfront": {
            "requests": "https_requests",
        },
    }
    for alias_field, canonical_field in semantic_aliases.get(service, {}).items():
        _remove_duplicate_alias(requirement, alias_field, canonical_field)

    if service == "s3" and "requests" in fields:
        # S3 has distinct request tiers. A generic request value repeating an
        # operation-specific statement is an alias, not another request class.
        for specific_field in (
            "put_copy_post_list_requests",
            "get_select_requests",
        ):
            if _same_customer_fact(requirement, "requests", specific_field):
                _remove_field(requirement, "requests")
                break

    if service == "ec2":
        has_disk = any(
            fields.get(field) not in (None, "", [], {})
            for field in ("system_disk_gib", "additional_ebs_volumes")
        )
        if has_disk and not fields.get("volume_type"):
            fields["volume_type"] = "gp3"
            requirement.field_sources[_path("volume_type")] = "system_default"
            requirement.field_evidence[_path("volume_type")] = (
                "客户未指定磁盘类型；按报价策略使用 EBS gp3"
            )

    engine = str(fields.get("engine") or "").casefold()
    if service == "rds" and engine.startswith("aurora"):
        invalid_fields = set(_DISK_FIELDS)
        if str(fields.get("storage_type") or "").casefold() in _EBS_STORAGE_TYPES:
            invalid_fields.add("storage_type")
        for field in sorted(invalid_fields):
            if field not in fields:
                continue
            if _customer_owned(requirement, field):
                issues.append(
                    PricingContractIssue(
                        field=field,
                        message=(
                            "Aurora 不按普通 RDS/EC2 的 EBS gp3、io2 或系统盘方式配置；"
                            "请改用 Aurora Standard 或 Aurora I/O-Optimized 的集群存储口径"
                        ),
                        evidence=requirement.field_evidence.get(_path(field), ""),
                    )
                )
            else:
                _remove_field(requirement, field)

    if service in {"elasticache", "memorydb"}:
        # Older drafts stored a customer's source-system disk as ordinary
        # ``storage_gib``. Preserve it, but move it to an explicitly
        # non-billable migration fact rather than pricing it as EBS.
        if "storage_gib" in fields and _customer_owned(requirement, "storage_gib"):
            if "source_storage_gib_per_node" not in fields:
                _move_field(requirement, "storage_gib", "source_storage_gib_per_node")
            else:
                _remove_field(requirement, "storage_gib")
        for field in sorted(_DISK_FIELDS):
            if field not in fields:
                continue
            if _customer_owned(requirement, field):
                issues.append(
                    PricingContractIssue(
                        field=field,
                        message=(
                            "ElastiCache/MemoryDB 节点不能按 EC2 的 EBS 数据盘计费；"
                            "该容量只能保留为迁移参考或重新确认托管服务容量含义"
                        ),
                        evidence=requirement.field_evidence.get(_path(field), ""),
                    )
                )
            else:
                _remove_field(requirement, field)

    if service == "backup" and "storage_gib" in fields:
        # Inside an AWS Backup component, an unqualified customer capacity is
        # backup storage, not a second generic storage product.  The cleaning
        # pass can legitimately materialize both names from the same phrase;
        # collapse that duplicate before the immutable fact ledger is sealed.
        if "backup_storage_gib" not in fields:
            _move_field(requirement, "storage_gib", "backup_storage_gib")
        elif fields["storage_gib"] == fields["backup_storage_gib"]:
            _remove_field(requirement, "storage_gib")
        elif _customer_owned(requirement, "storage_gib"):
            issues.append(
                PricingContractIssue(
                    field="storage_gib",
                    message=(
                        "AWS Backup 同时出现两个不同的存储容量，无法判断一个是受保护数据量"
                        "还是实际备份存储量；必须确认后才能避免重复计费"
                    ),
                    evidence=requirement.field_evidence.get(
                        _path("storage_gib"), ""
                    ),
                )
            )
        else:
            _remove_field(requirement, "storage_gib")

    if service in {"s3", "elb", "dynamodb"}:
        forbidden = {
            "requested_model",
            "vcpu",
            "memory_gib",
            "system_disk_gib",
            "additional_ebs_volumes",
            "volume_type",
        }
        for field in sorted(forbidden):
            if field not in fields:
                continue
            if _customer_owned(requirement, field):
                issues.append(
                    PricingContractIssue(
                        field=field,
                        message=(
                            f"{service.upper()} 的官方计费契约不接受 "
                            "EC2 型号、CPU、内存或 EBS 磁盘字段"
                        ),
                        evidence=requirement.field_evidence.get(_path(field), ""),
                    )
                )
            else:
                _remove_field(requirement, field)

    if service == "dynamodb" and any(
        fields.get(field) is not None for field in ("read_request_units", "write_request_units")
    ):
        for legacy in ("requests", "request_count"):
            if legacy in fields and not _customer_owned(requirement, legacy):
                _remove_field(requirement, legacy)

    return issues


def apply_pricing_contracts(
    requirements: list[ServiceRequirement],
) -> dict[str, list[PricingContractIssue]]:
    return {
        requirement.component_key or str(index): issues
        for index, requirement in enumerate(requirements)
        if (issues := apply_pricing_contract(requirement))
    }
