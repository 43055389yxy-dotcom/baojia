from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.customer_facts import CUSTOMER_FACT_SOURCES
from app.domain.models import ServiceRequirement

PRICING_CONTRACT_VERSION = "2026-09-01.2"

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


@dataclass(frozen=True, slots=True)
class ScaledTotalContract:
    """Declarative arithmetic relation between one unit and its total."""

    per_resource_field: str
    total_field: str
    topology_fields: tuple[str, ...] = ()
    include_deployment_quantity: bool = True


@dataclass(frozen=True, slots=True)
class SharedOfferUsageContract:
    """Declarative cross-offer usage derived from formal requirement fields."""

    amount_field: str
    service_code: str
    billing_kind: str
    topology_fields: tuple[str, ...] = ()
    include_deployment_quantity: bool = True
    default_variant: str = ""


@dataclass(frozen=True, slots=True)
class DeclaredSharedOfferUsage:
    amount_field: str
    service_code: str
    billing_kind: str
    amount: float
    source_fields: tuple[str, ...]
    variant: str = ""


_SCALED_TOTAL_CONTRACTS: dict[str, tuple[ScaledTotalContract, ...]] = {
    "ec2": (
        ScaledTotalContract("system_disk_gib", "total_system_disk_gib"),
        ScaledTotalContract(
            "data_transfer_out_gib_per_instance",
            "data_transfer_out_gib",
        ),
    ),
    "ebs": (ScaledTotalContract("storage_gib", "total_storage_gib"),),
    "msk": (
        ScaledTotalContract(
            "storage_gib_per_broker",
            "total_storage_gib",
            topology_fields=("broker_count",),
        ),
    ),
    "mq": (
        ScaledTotalContract(
            "storage_gib_per_broker",
            "total_storage_gib",
            topology_fields=("broker_count",),
        ),
    ),
    "opensearch": (
        ScaledTotalContract(
            "storage_gib_per_node",
            "total_storage_gib",
            topology_fields=("data_nodes", "nodes"),
            include_deployment_quantity=False,
        ),
    ),
}


# Role-scoped capacity belongs to the workload component while AWS may publish
# its price in another offer.  Keep topology arithmetic here so adapters never
# invent their own ``capacity × nodes × deployments`` interpretation.
_SHARED_OFFER_USAGE_CONTRACTS: dict[
    str, tuple[SharedOfferUsageContract, ...]
] = {
    "emr": tuple(
        SharedOfferUsageContract(
            amount_field=f"{role}_storage_gib_per_node",
            service_code="AmazonEC2",
            billing_kind="ebs_volume_storage",
            topology_fields=(f"{role}_nodes",),
            default_variant="gp3",
        )
        for role in ("master", "core", "task")
    ),
}


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


def _normalize_evidence(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _collapse_duplicate_ec2_disk_fact(requirement: ServiceRequirement) -> None:
    """Ensure one customer storage statement owns one EC2 disk field.

    The cleaning model may interpret an unqualified phrase such as
    ``每节点存储2T`` as an additional EBS volume while the deterministic
    literal overlay interprets the same phrase as the instance system disk.
    Those are two representations of one fact, not two disks.  Explicitly
    separate system/data disk statements remain independent even when their
    capacities happen to match.
    """

    system_size = requirement.requirements.get("system_disk_gib")
    volumes = requirement.requirements.get("additional_ebs_volumes")
    if (
        not isinstance(system_size, (int, float))
        or isinstance(system_size, bool)
        or not isinstance(volumes, list)
        or len(volumes) != 1
        or not isinstance(volumes[0], dict)
    ):
        return

    volume = volumes[0]
    data_size = volume.get("size_gib")
    count = volume.get("count_per_instance", 1)
    if (
        not isinstance(data_size, (int, float))
        or isinstance(data_size, bool)
        or not isinstance(count, (int, float))
        or isinstance(count, bool)
        or abs(float(count) - 1.0) > 1e-9
        or abs(float(system_size) - float(data_size)) > 1e-9
    ):
        return

    system_evidence = requirement.field_evidence.get(
        _path("system_disk_gib"), ""
    )
    data_evidence = requirement.field_evidence.get(
        _path("additional_ebs_volumes"), ""
    )
    normalized_system = _normalize_evidence(system_evidence)
    normalized_data = _normalize_evidence(data_evidence)
    same_statement = bool(
        normalized_system
        and normalized_data
        and (
            normalized_system == normalized_data
            or normalized_system in normalized_data
            or normalized_data in normalized_system
        )
    )
    if not same_statement:
        return

    combined_evidence = f"{system_evidence}；{data_evidence}".casefold()
    has_explicit_system_role = bool(
        re.search(
            r"系统盘|启动盘|根卷|root\s*(?:disk|volume)|boot\s*(?:disk|volume)",
            combined_evidence,
            flags=re.IGNORECASE,
        )
    )
    has_explicit_data_role = bool(
        re.search(
            r"数据盘|附加盘|数据卷|附加卷|data\s*(?:disk|volume)",
            combined_evidence,
            flags=re.IGNORECASE,
        )
    )

    if has_explicit_system_role and has_explicit_data_role:
        return
    if has_explicit_data_role:
        _remove_field(requirement, "system_disk_gib")
        return

    # A sole explicit system role, or an unqualified storage phrase, uses the
    # established EC2 canonical owner.  The important invariant is that the
    # one customer fact is consumed exactly once.
    _remove_field(requirement, "additional_ebs_volumes")


def _reconcile_per_resource_total(
    requirement: ServiceRequirement,
    *,
    per_resource_field: str,
    total_field: str,
    issues: list[PricingContractIssue],
    multiplier: float | None = None,
) -> None:
    """Reconcile a per-resource value with its arithmetic total.

    An arithmetic total generated by the system is never a second customer
    fact.  Two explicit customer values may coexist as a useful cross-check,
    but a contradiction must stop the quote instead of allowing an adapter to
    pick whichever field it happens to read first.
    """

    per_resource = requirement.requirements.get(per_resource_field)
    total = requirement.requirements.get(total_field)
    if (
        not isinstance(per_resource, (int, float))
        or isinstance(per_resource, bool)
        or not isinstance(total, (int, float))
        or isinstance(total, bool)
    ):
        return

    per_path = _path(per_resource_field)
    total_path = _path(total_field)
    per_source = requirement.field_sources.get(per_path, "")
    total_source = requirement.field_sources.get(total_path, "")

    # A generated total is never an additional customer fact and must not
    # survive into usage provenance. The canonical per-resource value remains
    # the owner when the customer supplied the per-resource value.
    if total_source == "system_derived":
        _remove_field(requirement, total_field)
        return

    scale = float(requirement.quantity) if multiplier is None else float(multiplier)
    if scale <= 0:
        return

    per_is_customer = _customer_owned(requirement, per_resource_field)
    total_is_customer = _customer_owned(requirement, total_field)
    if total_is_customer and not per_is_customer:
        # Some parsers receive only an explicit total. Keep that customer fact
        # and materialize the per-unit helper required by pricing adapters.
        # Recalculate stale defaults/derived values rather than making the
        # customer repair arithmetic the system can prove itself.
        requirement.requirements[per_resource_field] = float(total) / scale
        requirement.field_sources[per_path] = "system_derived"
        requirement.field_evidence[per_path] = "system_derived"
        requirement.locked_fields = [
            item for item in requirement.locked_fields if item != per_path
        ]
        return
    if per_is_customer and not total_is_customer:
        _remove_field(requirement, total_field)
        return

    expected_total = float(per_resource) * scale
    same_total = abs(expected_total - float(total)) <= max(
        1e-9, abs(expected_total) * 1e-9
    )
    if same_total:
        # Both values were explicitly supplied. Keep them as independent
        # validation facts; downstream usage declares both as its provenance.
        return
    if per_is_customer and total_is_customer:
        issues.append(
            PricingContractIssue(
                field=total_field,
                message=(
                    f"{per_resource_field} × 数量 与 {total_field} 的总量不一致；"
                    "必须先确认口径，不能选择其中一个继续报价"
                ),
                evidence="；".join(
                    evidence
                    for evidence in (
                        requirement.field_evidence.get(
                            _path(per_resource_field), ""
                        ),
                        requirement.field_evidence.get(_path(total_field), ""),
                    )
                    if evidence
                ),
            )
        )
    else:
        # Neither side is a customer fact. Keep the canonical per-unit helper
        # used by adapters and discard a redundant or stale total.
        _remove_field(requirement, total_field)


def _scaled_total_multiplier(
    requirement: ServiceRequirement,
    contract: ScaledTotalContract,
) -> float:
    topology_count: float | None = None
    for field in contract.topology_fields:
        value = requirement.requirements.get(field)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and value > 0
        ):
            topology_count = float(value)
            break

    if contract.include_deployment_quantity:
        return float(requirement.quantity) * (topology_count or 1.0)
    # OpenSearch and similar schemas use the node count itself as the billed
    # topology. When it is absent the adapter falls back to shared quantity.
    return topology_count or float(requirement.quantity)


def declared_shared_offer_usages(
    requirement: ServiceRequirement,
    target_fields: set[str],
) -> tuple[DeclaredSharedOfferUsage, ...]:
    """Resolve registered cross-offer arithmetic without reading customer text.

    A role-scoped per-node value requires an explicit topology field.  Missing
    topology deliberately yields no usage so the compiler keeps the customer
    fact unconsumed and blocks publication instead of silently assuming one.
    """

    usages: list[DeclaredSharedOfferUsage] = []
    for contract in _SHARED_OFFER_USAGE_CONTRACTS.get(_service_key(requirement), ()):
        if contract.amount_field not in target_fields:
            continue
        raw_amount = requirement.requirements.get(contract.amount_field)
        if (
            not isinstance(raw_amount, (int, float))
            or isinstance(raw_amount, bool)
            or raw_amount <= 0
        ):
            continue

        topology_field = ""
        topology_count = 1.0
        if contract.topology_fields:
            for candidate in contract.topology_fields:
                raw_count = requirement.requirements.get(candidate)
                if (
                    isinstance(raw_count, (int, float))
                    and not isinstance(raw_count, bool)
                    and raw_count > 0
                ):
                    topology_field = candidate
                    topology_count = float(raw_count)
                    break
            if not topology_field:
                continue

        deployment_count = (
            float(requirement.quantity or 1)
            if contract.include_deployment_quantity
            else 1.0
        )
        source_fields = [contract.amount_field]
        if topology_field:
            source_fields.append(topology_field)
        if contract.include_deployment_quantity:
            source_fields.append("quantity")
        usages.append(
            DeclaredSharedOfferUsage(
                amount_field=contract.amount_field,
                service_code=contract.service_code,
                billing_kind=contract.billing_kind,
                amount=float(raw_amount) * topology_count * deployment_count,
                source_fields=tuple(sorted(set(source_fields))),
                variant=contract.default_variant,
            )
        )
    return tuple(usages)


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


def _prefer_product_role_count_owner(
    requirement: ServiceRequirement,
    count_field: str,
) -> None:
    """Collapse a generic quantity duplicated by a product-role count.

    Products such as WAF and Route 53 price a named resource count directly.
    When cleaning assigns the same customer phrase to both that field and the
    compatibility ``quantity`` scalar, the product field is the sole formal
    owner.  The scalar value remains available for display but is explicitly
    marked derived so it cannot become a second customer pricing fact.
    """

    count = requirement.requirements.get(count_field)
    if (
        not isinstance(count, (int, float))
        or isinstance(count, bool)
        or requirement.field_sources.get("quantity") not in CUSTOMER_FACT_SOURCES
        or abs(float(requirement.quantity) - float(count)) > 1e-9
    ):
        return
    quantity_evidence = _normalize_evidence(
        requirement.field_evidence.get("quantity", "")
    )
    count_evidence = _normalize_evidence(
        requirement.field_evidence.get(_path(count_field), "")
    )
    if not (
        quantity_evidence
        and count_evidence
        and (
            quantity_evidence == count_evidence
            or quantity_evidence in count_evidence
            or count_evidence in quantity_evidence
        )
    ):
        return
    requirement.field_sources["quantity"] = "system_derived"
    requirement.field_evidence["quantity"] = "system_derived"
    requirement.locked_fields = [
        item for item in requirement.locked_fields if item != "quantity"
    ]


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

    product_role_count_owners = {
        "waf": "web_acls",
        "route53": "hosted_zones",
    }
    if product_count_field := product_role_count_owners.get(service):
        _prefer_product_role_count_owner(requirement, product_count_field)

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
        "msk": {
            "storage_gib": "storage_gib_per_broker",
        },
        "mq": {
            "storage_gib": "storage_gib_per_broker",
        },
        "opensearch": {
            "storage_gib": "storage_gib_per_node",
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

    # Per-unit values and totals are reconciled by a declarative formula table,
    # not by adapter read order. New services register their topology fields
    # here and automatically inherit duplicate/contradiction protection.
    for scaled_contract in _SCALED_TOTAL_CONTRACTS.get(service, ()):
        _reconcile_per_resource_total(
            requirement,
            per_resource_field=scaled_contract.per_resource_field,
            total_field=scaled_contract.total_field,
            issues=issues,
            multiplier=_scaled_total_multiplier(requirement, scaled_contract),
        )

    if service == "ec2":
        _collapse_duplicate_ec2_disk_fact(requirement)
        has_disk = any(
            fields.get(field) not in (None, "", [], {})
            for field in (
                "system_disk_gib",
                "total_system_disk_gib",
                "additional_ebs_volumes",
            )
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
