from __future__ import annotations

import re
from typing import Any

from app.core.errors import ManualConfirmationRequired
from app.domain.models import (
    CandidateOption,
    PreviewSelection,
    SelectedResource,
    ServiceKind,
    ServiceRequirement,
    UsageLine,
)
from app.domain.requirement_fields import canonicalize_requirement_fields
from app.integrations.aws import PricingCatalog, parse_number
from app.services.aws_query_executor import ReadOnlyAwsQueryExecutor
from app.services.plugins.base import ServicePlugin, required_float


class RdsPlugin(ServicePlugin):
    kind = ServiceKind.RDS
    display_name = "Amazon RDS"

    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        region = requirement.region or default_region
        requested = canonicalize_requirement_fields(requirement.requirements, service="rds")
        engine = _text(requested.get("engine")) or "mysql"
        requested_model = _text(requested.get("requested_model"))
        min_vcpu = required_float(requested, "vcpu")
        min_memory = required_float(requested, "memory_gib")
        deployment = _billing_deployment(engine, requested)
        priced_instance_count = _priced_instance_count(requirement, engine, requested)
        orderable = self._orderable_classes(
            region,
            engine,
            requested.get("engine_version"),
            requested_model=requested_model,
        )
        product_filters = {
            "regionCode": region,
            "productFamily": "Database Instance",
            "databaseEngine": _pricing_engine(engine),
            "deploymentOption": _pricing_deployment(deployment),
        }
        if edition := _pricing_edition(engine):
            product_filters["databaseEdition"] = edition
        # Exact customer shapes are by far the common case. Ask Price List for
        # that shape directly instead of downloading every RDS instance in the
        # region. Fall back to the wider catalog only when AWS has no exact fit.
        if requested_model:
            product_filters["instanceType"] = requested_model
        elif min_vcpu is not None and min_memory is not None:
            product_filters["vcpu"] = f"{min_vcpu:g}"
            product_filters["memory"] = f"{min_memory:g} GiB"
        products = self.catalog.products("AmazonRDS", product_filters, max_pages=3)
        candidates = _rds_candidates(products, engine, deployment, orderable)
        has_exact_shape = min_vcpu is not None and min_memory is not None
        if not candidates and (requested_model or has_exact_shape):
            broad_filters = {
                "regionCode": region,
                "productFamily": "Database Instance",
                "databaseEngine": _pricing_engine(engine),
                "deploymentOption": _pricing_deployment(deployment),
            }
            if edition := _pricing_edition(engine):
                broad_filters["databaseEdition"] = edition
            products = self.catalog.products("AmazonRDS", broad_filters, max_pages=20)
            candidates = _rds_candidates(products, engine, deployment, orderable)
        eligible = [item for item in candidates if _fits(item, min_vcpu, min_memory)]
        eligible = _reasonable_rds_candidates(
            eligible,
            deployment=deployment,
            min_vcpu=min_vcpu,
            min_memory=min_memory,
        )
        if requested_model:
            exact = next((item for item in eligible if item["model"] == requested_model), None)
            if exact:
                eligible = [exact]
        if not eligible:
            raise ManualConfirmationRequired(
                "AWS 官方 RDS 目录中没有满足需求的候选实例", code="rds_specification_not_found"
            )
        option_candidates = list(eligible)
        # Confirmation must show both sides of a non-existent shape. The
        # selected/default quote still comes only from the non-underprovisioned
        # eligible set; the lower tier is exposed solely as a customer choice.
        if not requested_model and min_vcpu is not None and min_memory is not None:
            lower = [
                item
                for item in candidates
                if item["vcpu"] <= min_vcpu
                and item["memory_gib"] <= min_memory
                and (item["vcpu"], item["memory_gib"]) != (min_vcpu, min_memory)
            ]
            if lower:
                nearest_lower = min(
                    lower,
                    key=lambda item: (
                        (min_vcpu - item["vcpu"]) / max(min_vcpu, 1)
                        + (min_memory - item["memory_gib"]) / max(min_memory, 1),
                        -item["memory_gib"],
                        item["model"],
                    ),
                )
                if all(item["model"] != nearest_lower["model"] for item in option_candidates):
                    option_candidates.append(nearest_lower)
        options: list[CandidateOption] = []
        for item in option_candidates:
            try:
                product = PricingCatalog.require_unique(
                    item["products"], context=f"RDS {item['model']} ({engine}, {deployment})"
                )
                _, usage_type, operation = PricingCatalog.billing_identity(product)
            except ManualConfirmationRequired:
                continue
            attrs = PricingCatalog.attributes(product)
            hourly = PricingCatalog.on_demand_rate(product)
            monthly = (
                hourly * requirement.hours_per_month * priced_instance_count
                if hourly is not None
                else None
            )
            options.append(
                CandidateOption(
                    model=item["model"],
                    family=item["model"].split(".")[0],
                    specifications={
                        "vCPU": item["vcpu"],
                        "memoryGiB": item["memory_gib"],
                        "engine": engine,
                        "deployment": deployment,
                    },
                    monthly_catalog_cost=monthly,
                    rationale="满足 RDS 引擎、区域、部署模式与最低规格后，按官方按需月费排序。",
                    official_product={
                        "sku": product["product"]["sku"],
                        "usageType": usage_type,
                        "operation": operation,
                        "regionCode": attrs.get("regionCode"),
                    },
                )
            )
        if not options:
            raise ManualConfirmationRequired(
                "AWS Price List 没有返回可排序的 RDS 官方产品",
                code="rds_pricing_candidates_not_found",
            )
        options.sort(
            key=lambda option: (
                option.monthly_catalog_cost is None,
                option.monthly_catalog_cost or float("inf"),
                option.model,
            )
        )
        eligible_models = {item["model"] for item in eligible}
        default = next(
            (option for option in options if option.model in eligible_models),
            options[0],
        )
        default.is_default = True
        options = [default, *(option for option in options if option.model != default.model)][:12]
        # CPU/memory without a model means "choose for me". The adapter already
        # ranks non-underprovisioned official products by monthly price, so a
        # non-exact shape must not be sent back to the customer.
        requires_confirmation = bool(
            requested_model and default.model != requested_model
        )
        return PreviewSelection(
            component_id="component",
            service=self.kind,
            display_name=_display_name(engine),
            region=region,
            requested_model=requested_model,
            selected_model=default.model,
            selection_reason=(
                "客户指定型号已确认可用，直接采用。"
                if requested_model and options[0].model == requested_model
                else "先满足 RDS 引擎、部署与规格要求，再按官方按需月费排序。"
            ),
            candidates=options,
            requires_confirmation=requires_confirmation,
            confirmation_reason=(
                "AWS 可订购的数据库规格与客户要求不是完全匹配，请确认推荐配置。"
                if requires_confirmation
                else None
            ),
        )

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        requested = canonicalize_requirement_fields(requirement.requirements, service="rds")
        engine = _text(requested.get("engine")) or "mysql"

        requested_model = _text(requested.get("requested_model"))
        purchase_option = _text(requested.get("purchase_option")) or "on_demand"
        min_vcpu = required_float(requested, "vcpu")
        min_memory = required_float(requested, "memory_gib")
        deployment = _billing_deployment(engine, requested)
        if deployment not in {"single_az", "multi_az", "multi_az_cluster"}:
            raise ManualConfirmationRequired(
                f"RDS 部署模式 {deployment!r} 无法识别",
                code="invalid_rds_deployment",
            )

        orderable = self._orderable_classes(
            region,
            engine,
            requested.get("engine_version"),
            requested_model=requested_model,
        )
        product_filters = {
            "regionCode": region,
            "productFamily": "Database Instance",
            "databaseEngine": _pricing_engine(engine),
            "deploymentOption": _pricing_deployment(deployment),
        }
        if edition := _pricing_edition(engine):
            product_filters["databaseEdition"] = edition
        products = self.catalog.products("AmazonRDS", product_filters)
        candidates = _rds_candidates(products, engine, deployment, orderable)
        selected, substitution = _select_rds(
            candidates,
            requested_model=requested_model,
            min_vcpu=min_vcpu,
            min_memory=min_memory,
        )
        product = PricingCatalog.require_unique(
            selected["products"], context=f"RDS {selected['model']} ({engine}, {deployment})"
        )
        service_code, usage_type, operation = PricingCatalog.billing_identity(product)
        attrs = PricingCatalog.attributes(product)
        priced_instance_count = _priced_instance_count(requirement, engine, requested)
        cluster_members = _cluster_members(requirement, engine, requested)
        amount = priced_instance_count * requirement.hours_per_month
        monthly_commitment_cost = 0.0
        upfront_commitment_cost = 0.0
        usage_lines: list[UsageLine] = []
        if purchase_option == "on_demand":
            usage_lines.append(
                UsageLine(
                    key="rds",
                    service_code=service_code,
                    usage_type=usage_type,
                    operation=operation,
                    amount=amount,
                    group="rds",
                )
            )
        elif purchase_option == "reserved":
            reserved = PricingCatalog.reserved_price(
                product,
                years=int(requested.get("reserved_term_years") or 1),
                payment_option=_text(requested.get("payment_option")) or "no_upfront",
                hours_per_month=requirement.hours_per_month,
            )
            monthly_commitment_cost = reserved.monthly_amortized * priced_instance_count
            upfront_commitment_cost = reserved.upfront * priced_instance_count
        else:
            raise ManualConfirmationRequired(
                f"RDS 购买方式 {purchase_option!r} 尚不支持官方核价",
                code="unsupported_purchase_option",
            )
        storage_gib = required_float(requested, "storage_gib")
        storage_type_used: str | None = None
        if storage_gib is not None:
            storage_line, storage_type_used = self._storage_usage(
                region, requested, requirement.quantity, storage_gib
            )
            usage_lines.append(storage_line)

        notice_parts: list[str] = []
        if substitution:
            notice_parts.append(
                f"客户指定的 {requested_model} 在目标区域不可订购或与规格/部署模式冲突，"
                f"已替换为最接近的 {selected['model']}。"
            )
        elif min_memory is not None and selected["memory_gib"] > min_memory:
            notice_parts.append(
                f"AWS 没有恰好 {min_memory:g} GiB 的匹配 RDS 规格，已选择 "
                f"{selected['memory_gib']:g} GiB。"
            )
        requested_storage_type = _text(requested.get("storage_type"))
        if storage_type_used and _is_generic_ssd(requested_storage_type):
            notice_parts.append(
                "客户仅指定 SSD、未指定具体 RDS 存储类型；本次按通用型 SSD gp3 报价。"
            )

        architecture = {
            "single_az": "RDS Single-AZ；每个数据库实例按一个计费实例处理",
            "multi_az": "RDS Multi-AZ DB instance；使用 AWS 对应 Multi-AZ 计费维度",
            "multi_az_cluster": "RDS Multi-AZ DB cluster；使用 AWS 对应集群计费维度",
        }[deployment]
        if _is_aurora_engine(engine):
            customer_deployment = _text(requested.get("deployment")) or "single_az"
            availability = (
                "高可用"
                if customer_deployment in {"multi_az", "multi_az_cluster"}
                else "单可用区"
            )
            architecture = (
                f"Aurora {availability}集群；{requirement.quantity} 套集群，"
                f"每套 {cluster_members} 个数据库实例；"
                "实例成员按 Aurora 官方实例计费维度处理"
            )
        return SelectedResource(
            service=self.kind,
            display_name=_display_name(engine),
            region=region,
            model=selected["model"],
            architecture=(
                architecture
                if _is_aurora_engine(engine)
                else f"{requirement.quantity} × {architecture}"
            ),
            specifications={
                "engine": attrs.get("databaseEngine"),
                "deploymentOption": attrs.get("deploymentOption"),
                "vCPU": selected["vcpu"],
                "memoryGiB": selected["memory_gib"],
                "storageType": storage_type_used,
                **(
                    {
                        "customerDeployment": _text(requested.get("deployment")) or "single_az",
                        "clusterMembers": cluster_members,
                    }
                    if _is_aurora_engine(engine)
                    else {}
                ),
            },
            official_product={
                "sku": product["product"]["sku"],
                "usageType": usage_type,
                "operation": operation,
                "regionCode": attrs.get("regionCode"),
            },
            rationale="先用 RDS Orderable API 核验区域/引擎可订购性，再从官方目录选最小满足规格。",
            substitution_notice=" ".join(notice_parts) or None,
            usage_lines=usage_lines,
            monthly_commitment_cost=monthly_commitment_cost,
            upfront_commitment_cost=upfront_commitment_cost,
        )

    def _storage_usage(
        self, region: str, requested: dict[str, Any], quantity: int, storage_gib: float
    ) -> tuple[UsageLine, str]:
        requested_storage_type = _text(requested.get("storage_type"))
        engine = _text(requested.get("engine"))
        deployment = _billing_deployment(engine, requested)
        if not engine:
            raise ManualConfirmationRequired(
                "RDS 存储计费缺少数据库引擎", code="missing_rds_engine"
            )
        if _is_aurora_engine(engine):
            # Aurora cluster storage is a shared cluster-level billing
            # dimension. It does not use the regular RDS GP3 volume product,
            # and some regions publish the standard Aurora storage row with
            # databaseEngine=Any rather than Aurora MySQL.
            storage_type = (
                "IO Optimized-Aurora"
                if "io" in _normalize(requested_storage_type or "")
                and "optimized" in _normalize(requested_storage_type or "")
                else "General Purpose-Aurora"
            )
            product_filters = {
                "regionCode": region,
                "productFamily": "Database Storage",
                "databaseEngine": _pricing_engine(engine),
                "deploymentOption": "Single-AZ",
                "volumeType": storage_type,
            }
            products = self.catalog.products("AmazonRDS", product_filters, max_pages=5)
            if not products:
                product_filters["databaseEngine"] = "Any"
                products = self.catalog.products("AmazonRDS", product_filters, max_pages=5)
            storage_amount = storage_gib
        else:
            storage_values = self.catalog.attribute_values("AmazonRDS", "volumeType")
            storage_type = _resolve_volume_type(requested_storage_type, storage_values)
            product_filters = {
                "regionCode": region,
                "productFamily": "Database Storage",
                "databaseEngine": _pricing_engine(engine),
                "deploymentOption": _pricing_deployment(deployment),
                "volumeType": storage_type,
            }
            if edition := _pricing_edition(engine):
                product_filters["databaseEdition"] = edition
            products = self.catalog.products("AmazonRDS", product_filters, max_pages=5)
            storage_amount = quantity * storage_gib
        product = PricingCatalog.require_unique(
            products, context=f"RDS {storage_type} 存储 ({region})"
        )
        service_code, usage_type, operation = PricingCatalog.billing_identity(product)
        return (
            UsageLine(
                key="rdsstg",
                service_code=service_code,
                usage_type=usage_type,
                operation=operation,
                amount=storage_amount,
                group="rds-storage",
            ),
            storage_type,
        )

    def _orderable_classes(
        self,
        region: str,
        engine: str,
        engine_version: object,
        *,
        requested_model: str | None = None,
    ) -> set[str]:
        api_engine = _rds_api_engine(engine)
        kwargs: dict[str, Any] = {"Engine": api_engine}
        if requested_model:
            # A named class must be checked directly. Scanning a truncated broad
            # orderable catalog can falsely report a real customer model missing.
            kwargs["DBInstanceClass"] = requested_model
        if version := _text(engine_version):
            # Customers normally provide a major family such as MySQL 8.0 or
            # PostgreSQL 16. The RDS orderable-options API expects a full engine
            # build for EngineVersion, so major-family text must not be sent as-is.
            if re.fullmatch(r"\d+(?:\.\d+){2,}(?:[-.][A-Za-z0-9]+)*", version):
                kwargs["EngineVersion"] = version
        try:
            classes: set[str] = set()
            response = ReadOnlyAwsQueryExecutor(self.clients).execute(
                service="rds",
                operation="describe_orderable_db_instance_options",
                region=region,
                parameters=kwargs,
                max_items=1000,
            )
            for page in response.get("pages", [response]):
                classes.update(
                    item["DBInstanceClass"] for item in page["OrderableDBInstanceOptions"]
                )
        except (ManualConfirmationRequired, KeyError) as exc:
            if isinstance(exc, ManualConfirmationRequired) and exc.code in {
                "aws_credentials_invalid",
                "aws_region_not_enabled",
            }:
                raise
            raise ManualConfirmationRequired(
                f"RDS 官方 API 无法确认 {engine} 在 {region} 的可订购规格",
                code="rds_discovery_failed",
                engine=engine,
                region=region,
            ) from exc
        if not classes:
            raise ManualConfirmationRequired(
                f"RDS 引擎 {engine} 或指定版本在 {region} 不受支持",
                code="unsupported_rds_engine_or_region",
            )
        return classes


def _rds_candidates(
    products: list[dict[str, Any]], engine: str, deployment: str, orderable: set[str]
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, float, float], list[dict[str, Any]]] = {}
    for product in products:
        attrs = PricingCatalog.attributes(product)
        model = attrs.get("instanceType")
        if not model or model not in orderable:
            continue
        if not _engine_matches(engine, attrs.get("databaseEngine", "")):
            continue
        if not _deployment_matches(deployment, attrs.get("deploymentOption", "")):
            continue
        try:
            vcpu = parse_number(attrs.get("vcpu"), field="vcpu")
            memory = parse_number(attrs.get("memory"), field="memory")
        except ManualConfirmationRequired:
            continue
        grouped.setdefault((model, vcpu, memory), []).append(product)
    return [
        {
            "model": model,
            "vcpu": vcpu,
            "memory_gib": memory,
            "products": _preferred_rds_products(matches, engine),
        }
        for (model, vcpu, memory), matches in grouped.items()
    ]


def _preferred_rds_products(
    products: list[dict[str, Any]], engine: str
) -> list[dict[str, Any]]:
    """Keep the lowest/default billing variant for one RDS instance shape.

    Aurora publishes both Standard and I/O-Optimized instance usage products
    for the same model and deployment.  If the customer did not explicitly
    request I/O-Optimized (the normal case here), the standard usage record is
    the deterministic minimum-cost choice.  Returning both made
    ``require_unique`` discard an otherwise valid Aurora model.
    """

    if not _is_aurora_engine(engine) or len(products) < 2:
        return products
    standard = [
        product
        for product in products
        if "iooptimized"
        not in _normalize(PricingCatalog.attributes(product).get("usagetype", ""))
    ]
    return standard or products


def _select_rds(
    candidates: list[dict[str, Any]],
    *,
    requested_model: str | None,
    min_vcpu: float | None,
    min_memory: float | None,
) -> tuple[dict[str, Any], bool]:
    exact = next((item for item in candidates if item["model"] == requested_model), None)
    if exact and _fits(exact, min_vcpu, min_memory):
        return exact, False
    if requested_model and exact is None and min_vcpu is None and min_memory is None:
        raise ManualConfirmationRequired(
            "客户指定的 RDS 型号不可用，且没有足够规格信息选择替代型号",
            code="invalid_rds_model_without_replacement_basis",
        )
    eligible = [item for item in candidates if _fits(item, min_vcpu, min_memory)]
    if not eligible:
        raise ManualConfirmationRequired(
            "AWS 官方 RDS 目录中没有满足需求且可订购的实例",
            code="rds_specification_not_found",
        )
    eligible.sort(key=lambda item: (item["memory_gib"], item["vcpu"], item["model"]))
    return eligible[0], requested_model is not None


def _reasonable_rds_candidates(
    candidates: list[dict[str, Any]],
    *,
    deployment: str,
    min_vcpu: float | None,
    min_memory: float | None,
) -> list[dict[str, Any]]:
    production_sized = deployment != "single_az" or (min_vcpu or 0) >= 4 or (min_memory or 0) >= 16
    if not production_sized:
        return candidates
    non_burstable = [item for item in candidates if not item["model"].startswith("db.t")]
    return non_burstable or candidates


def _fits(item: dict[str, Any], min_vcpu: float | None, min_memory: float | None) -> bool:
    return (min_vcpu is None or item["vcpu"] >= min_vcpu) and (
        min_memory is None or item["memory_gib"] >= min_memory
    )


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _is_aurora_engine(engine: str) -> bool:
    return _normalize(engine) in {"auroramysql", "aurorapostgres", "aurorapostgresql"}


def _display_name(engine: str) -> str:
    normalized = _normalize(engine)
    if normalized == "auroramysql":
        return "Amazon Aurora MySQL"
    if normalized in {"aurorapostgres", "aurorapostgresql"}:
        return "Amazon Aurora PostgreSQL"
    return "Amazon RDS"


def _priced_instance_count(
    requirement: ServiceRequirement,
    engine: str,
    requested: dict[str, Any],
) -> int:
    if not _is_aurora_engine(engine):
        return requirement.quantity
    return requirement.quantity * _cluster_members(requirement, engine, requested)


def _cluster_members(
    requirement: ServiceRequirement,
    engine: str,
    requested: dict[str, Any],
) -> int:
    if not _is_aurora_engine(engine):
        return 1
    value = requested.get("cluster_members")
    try:
        return max(int(value), 1) if value is not None else 1
    except (TypeError, ValueError) as exc:
        raise ManualConfirmationRequired(
            "Aurora 集群数据库实例数必须是正整数",
            code="invalid_requirement",
            field="cluster_members",
        ) from exc


def _billing_deployment(engine: str, requested: dict[str, Any]) -> str:
    """Return the AWS Price List deployment dimension used for this engine.

    Aurora availability is expressed by multiple cluster members, not by the
    regular RDS ``Multi-AZ DB instance`` product dimension.  Each Aurora member
    therefore uses the Aurora instance/Single-AZ catalog record and quantity
    carries the explicit cluster member count.
    """

    if _is_aurora_engine(engine):
        return "single_az"
    return _text(requested.get("deployment")) or "single_az"


def _engine_matches(requested: str, official: str) -> bool:
    left, right = _normalize(requested), _normalize(official)
    if left == right:
        return True
    aliases = {
        "postgres": "postgresql",
        "aurorapostgres": "aurorapostgresql",
        "sqlserverstandard": "sqlserver",
        "sqlserverweb": "sqlserver",
        "sqlserverenterprise": "sqlserver",
        "sqlserverexpress": "sqlserver",
    }
    return aliases.get(left, left) == right


def _rds_api_engine(engine: str) -> str:
    """Map customer-facing engine names to RDS API identifiers."""

    normalized = _normalize(engine)
    aliases = {
        "postgres": "postgres",
        "postgresql": "postgres",
        "mysql": "mysql",
        "mariadb": "mariadb",
        "oracle": "oracle-ee",
        "sqlserver": "sqlserver-se",
        "sqlserverstandard": "sqlserver-se",
        "sqlserverweb": "sqlserver-web",
        "sqlserverenterprise": "sqlserver-ee",
        "sqlserverexpress": "sqlserver-ex",
        "auroramysql": "aurora-mysql",
        "aurorapostgres": "aurora-postgresql",
        "aurorapostgresql": "aurora-postgresql",
    }
    return aliases.get(normalized, engine.lower())


def _pricing_engine(engine: str) -> str:
    values = {
        "postgres": "PostgreSQL",
        "postgresql": "PostgreSQL",
        "mysql": "MySQL",
        "mariadb": "MariaDB",
        "sqlserver": "SQL Server",
        "sqlserverstandard": "SQL Server",
        "sqlserverweb": "SQL Server",
        "sqlserverenterprise": "SQL Server",
        "sqlserverexpress": "SQL Server",
        "auroramysql": "Aurora MySQL",
        "aurorapostgres": "Aurora PostgreSQL",
        "aurorapostgresql": "Aurora PostgreSQL",
    }
    return values.get(_normalize(engine), engine)


def _pricing_edition(engine: str) -> str | None:
    return {
        "sqlserverstandard": "Standard",
        "sqlserverweb": "Web",
        "sqlserverenterprise": "Enterprise",
        "sqlserverexpress": "Express",
    }.get(_normalize(engine))


def _pricing_deployment(deployment: str) -> str:
    return {
        "single_az": "Single-AZ",
        "multi_az": "Multi-AZ",
        "multi_az_cluster": "Multi-AZ DB Cluster",
    }[deployment]


def _deployment_matches(requested: str, official: str) -> bool:
    value = _normalize(official)
    if requested == "single_az":
        return "singleaz" in value
    if requested == "multi_az_cluster":
        return "multiaz" in value and "cluster" in value
    return "multiaz" in value and "cluster" not in value


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _resolve_volume_type(requested: str | None, official_values: list[str]) -> str:
    if not official_values:
        raise ManualConfirmationRequired(
            "AWS 官方目录没有返回 RDS 存储类型", code="rds_storage_type_unavailable"
        )
    if _is_generic_ssd(requested):
        matches = [
            value for value in official_values if _normalize(value) == "generalpurposegp3"
        ]
    elif requested:
        normalized = _normalize(requested)
        matches = [value for value in official_values if normalized in _normalize(value)]
    else:
        matches = [value for value in official_values if _normalize(value) == "generalpurposegp3"]
    if len(matches) != 1:
        raise ManualConfirmationRequired(
            "RDS 存储类型无法在 AWS 官方目录中唯一确定",
            code="ambiguous_rds_storage_type",
            candidates=matches[:10],
        )
    return matches[0]


def _is_generic_ssd(requested: str | None) -> bool:
    if not requested:
        return False
    normalized = _normalize(requested)
    return normalized in {
        "ssd",
        "solidstatedrive",
        "generalpurposessd",
        "generalpurposessdstorage",
        "通用ssd",
        "通用型ssd",
        "固态硬盘",
    }
