from __future__ import annotations

import re

from app.core.errors import ManualConfirmationRequired
from app.domain.models import (
    CandidateOption,
    PreviewSelection,
    ReferenceRate,
    SelectedResource,
    ServiceRequirement,
    UsageLine,
)
from app.integrations.aws import AwsClients, PricingCatalog
from app.integrations.auto_service_discovery import AutoServiceDiscovery
from app.integrations.service_templates import SERVICE_TEMPLATE_FIELDS


_SERVICE_CODE_ALIASES = {
    "eks": "AmazonEKS",
    "ecr": "AmazonECR",
    "backup": "AWSBackup",
    "secretsmanager": "AWSSecretsManager",
    "lambda": "AWSLambda",
    "ecs": "AmazonECS",
    "fargate": "AmazonECS",
    "dynamodb": "AmazonDynamoDB",
    "efs": "AmazonEFS",
    "sns": "AmazonSNS",
    "kinesis": "AmazonKinesis",
    "emr": "ElasticMapReduce",
    "redshift": "AmazonRedshift",
    "athena": "AmazonAthena",
    "glue": "AWSGlue",
    "sagemaker": "AmazonSageMaker",
    "cognito": "AmazonCognito",
    "mq": "AmazonMQ",
    "stepfunctions": "AWSStepFunctions",
    "bedrock": "AmazonBedrock",
    "cloudmap": "AWSCloudMap",
    "appconfig": "AWSAppConfig",
    "documentdb": "AmazonDocDB",
    "docdb": "AmazonDocDB",
    "mongodb": "AmazonDocDB",
    "vpc": "AmazonVPC",
    "dms": "AWSDatabaseMigrationSvc",
    "kms": "awskms",
    "xray": "AWSXRay",
    "grafana": "AmazonGrafana",
    "managedgrafana": "AmazonGrafana",
    "amp": "AmazonPrometheus",
    "prometheus": "AmazonPrometheus",
    "managedprometheus": "AmazonPrometheus",
    "quicksight": "AmazonQuickSight",
}


def _canonical(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _stem(value: str) -> str:
    result = _canonical(value)
    for prefix in ("amazon", "aws"):
        if result.startswith(prefix):
            result = result[len(prefix) :]
    for suffix in ("service", "services"):
        if result.endswith(suffix):
            result = result[: -len(suffix)]
    return result


class GenericOfficialPlugin:
    """Safe fallback for AWS services without a workload-specific adapter.

    It never invents usage. For well-known metered services it selects the
    billing dimension that matches the customer's field; it must never choose
    the globally cheapest, unrelated dimension from a large service catalog.
    """

    def __init__(
        self,
        clients: AwsClients,
        catalog: PricingCatalog,
        auto_discovery: AutoServiceDiscovery | None = None,
    ):
        self.clients = clients
        self.catalog = catalog
        self.auto_discovery = auto_discovery

    def _service_code(self, requirement: ServiceRequirement) -> str:
        labels = [requirement.service, requirement.calculator_service_name or ""]
        for label in labels:
            canonical = _canonical(label)
            stem = _stem(label)
            if canonical in _SERVICE_CODE_ALIASES:
                return _SERVICE_CODE_ALIASES[canonical]
            if stem in _SERVICE_CODE_ALIASES:
                return _SERVICE_CODE_ALIASES[stem]
        stems: dict[str, list[str]] = {}
        for code in self.catalog.service_codes():
            stems.setdefault(_stem(code), []).append(code)
        for label in labels:
            matches = stems.get(_stem(label), [])
            if len(matches) == 1:
                return matches[0]
        raise ManualConfirmationRequired(
            "AWS 官方服务目录无法唯一匹配该服务",
            code="generic_service_code_not_found",
            service=requirement.service,
        )

    def _catalog_rates(
        self,
        service_code: str,
        region: str,
        *,
        refresh: bool = False,
        max_pages: int = 20,
    ) -> list[tuple[float, str, str, str, dict[str, object]]]:
        """Load one component's official prices, optionally bypassing cache."""

        filters = {"regionCode": region} if region != "global" else {}

        def products(query_filters: dict[str, str]) -> list[dict[str, object]]:
            try:
                return self.catalog.products(
                    service_code,
                    query_filters,
                    max_pages=max_pages,
                    refresh=refresh,
                )
            except TypeError:
                # Lightweight test and third-party catalog implementations may
                # predate the refresh keyword. Their normal query remains safe.
                return self.catalog.products(
                    service_code, query_filters, max_pages=max_pages
                )

        raw_products = products(filters)
        if region != "global":
            # Some AWS products publish a mixture of regional dimensions and
            # global subscriptions under the same ServiceCode (QuickSight is a
            # common example).  A successful regional query therefore does not
            # mean the catalog is complete.  Merge only truly global products
            # from the unfiltered catalog; never borrow a price from another
            # AWS region.
            all_products = products({})
            global_products = [
                product
                for product in all_products
                if self._is_global_catalog_product(product)
            ]
            seen_skus = {
                str(product.get("product", {}).get("sku") or product.get("sku") or "")
                for product in raw_products
            }
            raw_products.extend(
                product
                for product in global_products
                if str(
                    product.get("product", {}).get("sku")
                    or product.get("sku")
                    or ""
                )
                not in seen_skus
            )
        rates: list[tuple[float, str, str, str, dict[str, object]]] = []
        for product in raw_products:
            try:
                identity = PricingCatalog.billing_identity(product)
                priced = PricingCatalog.on_demand_unit_rate(product)
            except ManualConfirmationRequired:
                continue
            if priced is None:
                continue
            price, unit = priced
            rates.append((price, unit, identity[1], identity[2], product))
        return rates

    @staticmethod
    def _is_global_catalog_product(product: dict[str, object]) -> bool:
        attrs = PricingCatalog.attributes(product)
        region = str(
            attrs.get("regionCode")
            or attrs.get("regioncode")
            or attrs.get("region")
            or ""
        ).strip().casefold()
        location = str(attrs.get("location") or "").strip().casefold()
        return region in {"", "global"} and location in {
            "",
            "any",
            "global",
        }

    def _refresh_official_profile(
        self, requirement: ServiceRequirement
    ) -> dict[str, object] | None:
        if self.auto_discovery is None:
            return None
        arguments = {
            "service_key": requirement.service,
            "display_name": requirement.calculator_service_name
            or requirement.service,
            "region": requirement.region,
        }
        try:
            return self.auto_discovery.ensure_profile(
                **arguments, force_refresh=True
            )
        except TypeError:
            return self.auto_discovery.ensure_profile(**arguments)

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        is_unknown_service = requirement.service not in SERVICE_TEMPLATE_FIELDS
        profile = None
        # A stable service-code alias is enough to query the official catalog.
        # Do not make first-use profile discovery a hard dependency for services
        # whose AWS service code is already known (for example Managed Grafana).
        # Discovery may still enrich field bindings, but a temporary discovery
        # failure must not turn a valid official catalog into a customer-facing
        # "interface unavailable" error.
        try:
            service_code = self._service_code(requirement)
        except ManualConfirmationRequired:
            service_code = ""
        if is_unknown_service and self.auto_discovery is not None:
            # ensure_profile owns the 10-day validity check. Calling get_profile
            # directly here previously allowed stale field mappings to live forever.
            try:
                profile = self.auto_discovery.ensure_profile(
                    service_key=requirement.service,
                    display_name=requirement.calculator_service_name or requirement.service,
                    region=requirement.region,
                )
            except ManualConfirmationRequired:
                if not service_code:
                    raise
        service_code = str((profile or {}).get("service_code") or service_code)
        if not service_code:
            service_code = self._service_code(requirement)
        if _stem(requirement.service) == "vpc":
            return SelectedResource(
                service=requirement.service,
                display_name=requirement.calculator_service_name or "Amazon VPC",
                region=region,
                model="VPC + Subnets",
                architecture=f"{requirement.quantity} 套 VPC 基础网络",
                specifications=dict(requirement.requirements),
                official_product={"source": "AWS Price List", "serviceCode": service_code},
                rationale="Amazon VPC 与子网本身没有基础小时费。",
                substitution_notice=(
                    "VPC 与子网本身不收取基础费用；NAT Gateway、公网 IPv4、流量等"
                    "独立资源按实际配置另行计费。"
                ),
                usage_lines=[],
                reference_rates=[],
            )
        rates = self._profile_rates(profile) if is_unknown_service else []
        if not rates:
            rates = self._catalog_rates(service_code, region)
        selected_rates = self._semantic_rates(requirement, rates)
        auto_discovered = False
        strict_semantic_services = {"emr", "redshift", "athena"}

        # A stale or incomplete catalog page must not stop the quote. Refresh
        # only this component and run the same deterministic field mapping
        # again; all other selected resources remain untouched.
        if not selected_rates:
            refreshed_rates = self._catalog_rates(
                service_code, region, refresh=True, max_pages=40
            )
            if refreshed_rates:
                rates = refreshed_rates
                selected_rates = self._semantic_rates(requirement, rates)

        # If the service has never been quoted, build/refresh its official
        # field profile from AWS and retry this component once more.
        if not selected_rates:
            refreshed_profile = self._refresh_official_profile(requirement)
            profile_rates = self._profile_rates(refreshed_profile)
            if profile_rates:
                profile = refreshed_profile
                service_code = str(profile.get("service_code") or service_code)
                rates = profile_rates
                selected_rates = self._semantic_rates(requirement, rates)
                if not selected_rates:
                    selected_rates = self._auto_semantic_rates(
                        requirement, rates, profile=profile
                    )
                auto_discovered = bool(selected_rates)

        if not selected_rates and _stem(requirement.service) not in strict_semantic_services:
            selected_rates = self._auto_semantic_rates(
                requirement, rates, profile=profile if is_unknown_service else None
            )
            auto_discovered = bool(selected_rates)
        if not selected_rates:
            raise ManualConfirmationRequired(
                "AWS 官方目录没有返回可安全展示的新组件计费项",
                code="generic_semantic_rate_not_found",
                service_code=service_code,
            )

        # When the official field is known but the customer did not provide a
        # usage amount, apply the same minimum-unit rule consistently across
        # both established and first-use generic services. Keep only the
        # lowest positive matching dimension and charge exactly one official
        # unit; never manufacture a monthly volume.
        if selected_rates and all(amount is None for _, amount, _ in selected_rates):
            positive = [item for item in selected_rates if item[2][0] > 0]
            pool = positive or selected_rates
            description, _, rate = min(
                pool,
                key=lambda item: (item[2][0], item[2][1], item[2][2], item[2][3]),
            )
            selected_rates = [(description, 1.0, rate)]

        display_name = requirement.calculator_service_name or requirement.service
        usage_lines: list[UsageLine] = []
        reference_rates: list[ReferenceRate] = []
        for index, (description, amount, rate) in enumerate(selected_rates, start=1):
            price, unit, usage_type, operation, _ = rate
            if amount is not None and amount > 0:
                usage_lines.append(
                    UsageLine(
                        key=f"gen{index}",
                        service_code=service_code,
                        usage_type=usage_type,
                        operation=operation,
                        amount=amount,
                        group=requirement.service,
                    )
                )
            else:
                reference_rates.append(
                    ReferenceRate(
                        description=description,
                        unit=unit,
                        unit_price=price,
                        service_code=service_code,
                        usage_type=usage_type,
                        operation=operation,
                    )
                )
        has_usage = bool(usage_lines)
        selected_model = str(requirement.requirements.get("requested_model") or "").strip()
        if not selected_model:
            for _, _, selected_rate in selected_rates:
                attrs = PricingCatalog.attributes(selected_rate[4])
                if attrs.get("instanceType"):
                    selected_model = str(attrs["instanceType"])
                    break
        service_stem = _stem(requirement.service)
        if service_stem == "athena":
            selected_model = "按查询数据扫描量计费"
        elif service_stem == "emr" and not selected_model:
            selected_model = "Amazon EMR 托管集群"
        elif service_stem == "redshift" and not selected_model:
            selected_model = "Amazon Redshift 数据仓库"

        architecture = "按客户明确用量核价" if has_usage else "官方单位参考价"
        if service_stem == "athena":
            architecture = "无服务器查询，按扫描数据量计费"
        elif service_stem == "emr":
            architecture = "按主节点、核心节点和任务节点分别核价"
        elif service_stem == "redshift":
            architecture = "按计算节点与数据仓库存储分别核价"
        return SelectedResource(
            service=requirement.service,
            display_name=display_name,
            region=region,
            model=selected_model or "AWS 官方计费维度",
            architecture=architecture,
            specifications=dict(requirement.requirements),
            official_product={"source": "AWS Price List", "serviceCode": service_code},
            rationale=(
                "新组件已根据 AWS 官方产品属性和计费单位自动建立只读报价档案。"
                if auto_discovered
                else "按服务语义匹配 AWS 官方计费维度，不使用无关的最低价目录项。"
            ),
            substitution_notice=(
                None
                if has_usage and not reference_rates
                else "未提供完整用量的部分仅展示对应官方单位价，不计入月费合计。"
            ),
            usage_lines=usage_lines,
            reference_rates=reference_rates,
        )

    @staticmethod
    def _profile_rates(
        profile: dict[str, object] | None,
    ) -> list[tuple[float, str, str, str, dict[str, object]]]:
        """Rebuild rate candidates from the exact cached official dimensions.

        This is used only for automatically discovered services.  Existing
        workload-specific adapters and their candidate selection are untouched.
        """

        result: list[tuple[float, str, str, str, dict[str, object]]] = []
        if not profile or profile.get("status") != "verified":
            return result
        service_code = str(profile.get("service_code") or "")
        dimensions = profile.get("dimensions")
        if not service_code or not isinstance(dimensions, list):
            return result
        for dimension in dimensions:
            if not isinstance(dimension, dict):
                continue
            try:
                price = float(dimension.get("price") or 0)
            except (TypeError, ValueError):
                continue
            attrs = {
                "usagetype": str(dimension.get("usage_type") or ""),
                "operation": str(dimension.get("operation") or ""),
                "productFamily": str(dimension.get("product_family") or ""),
                "instanceType": str(dimension.get("instance_type") or ""),
                "vcpu": dimension.get("vcpu"),
                "memory": dimension.get("memory"),
            }
            product: dict[str, object] = {
                "serviceCode": service_code,
                "product": {"attributes": attrs},
                "officialDimensionDescription": str(
                    dimension.get("description") or ""
                ),
            }
            result.append(
                (
                    price,
                    str(dimension.get("unit") or "unit"),
                    str(dimension.get("usage_type") or ""),
                    str(dimension.get("operation") or ""),
                    product,
                )
            )
        return result

    @staticmethod
    def _semantic_rates(
        requirement: ServiceRequirement,
        rates: list[tuple[float, str, str, str, dict[str, object]]],
    ) -> list[
        tuple[str, float | None, tuple[float, str, str, str, dict[str, object]]]
    ]:
        """Choose billing dimensions by service meaning, never global price."""

        service = _stem(requirement.service)
        requested = requirement.requirements

        def matching(
            *,
            include: tuple[str, ...] = (),
            include_any: tuple[str, ...] = (),
            exclude: tuple[str, ...] = (),
            model: str | None = None,
            min_vcpu: float | None = None,
            min_memory_gib: float | None = None,
            unit_contains: tuple[str, ...] = (),
            current_generation: bool = False,
        ) -> tuple[float, str, str, str, dict[str, object]] | None:
            candidates = []
            for item in rates:
                product = item[4]
                attrs = PricingCatalog.attributes(product)
                text = " ".join(
                    str(value)
                    for value in (
                        item[1],
                        item[2],
                        item[3],
                        attrs.get("productFamily"),
                        attrs.get("instanceType"),
                        attrs.get("instanceTypeFamily"),
                        *attrs.values(),
                    )
                    if value
                ).casefold()
                if not all(token.casefold() in text for token in include):
                    continue
                if include_any and not any(
                    token.casefold() in text for token in include_any
                ):
                    continue
                if any(token.casefold() in text for token in exclude):
                    continue
                if unit_contains and not any(
                    token.casefold() in str(item[1]).casefold()
                    for token in unit_contains
                ):
                    continue
                if model and model.casefold() not in text:
                    continue
                if current_generation:
                    instance_type = str(attrs.get("instanceType") or "").casefold()
                    generation = re.match(r"^[a-z]+(\d+)", instance_type)
                    if not generation or int(generation.group(1)) < 5:
                        continue
                if min_vcpu is not None:
                    try:
                        if float(attrs.get("vcpu") or 0) < min_vcpu:
                            continue
                    except (TypeError, ValueError):
                        continue
                if min_memory_gib is not None:
                    memory_text = str(attrs.get("memory") or attrs.get("memoryGib") or "")
                    memory_match = re.search(r"\d+(?:\.\d+)?", memory_text)
                    if not memory_match or float(memory_match.group()) < min_memory_gib:
                        continue
                candidates.append(item)
            positive = [item for item in candidates if item[0] > 0] or candidates
            return min(positive, key=lambda item: (item[0], item[2], item[3])) if positive else None

        def add(
            result: list,
            description: str,
            amount: float | None,
            **filters,
        ) -> None:
            rate = matching(**filters)
            if rate is not None:
                result.append((description, amount, rate))

        result: list[
            tuple[str, float | None, tuple[float, str, str, str, dict[str, object]]]
        ] = []
        if service == "lambda":
            requests = requested.get("requests") or requested.get("request_count")
            add(
                result,
                "Lambda 请求单价",
                float(requests) if requests else None,
                include=("request",),
                exclude=("edge", "managed", "durable"),
            )
            memory_mb = requested.get("memory_mb")
            duration_ms = requested.get("duration_ms")
            compute_amount = None
            if requests and memory_mb and duration_ms:
                compute_amount = (
                    float(requests) * float(memory_mb) / 1024 * float(duration_ms) / 1000
                )
            add(
                result,
                "Lambda 计算 GB-Second 单价",
                compute_amount,
                include=("lambda-gb-second",),
                exclude=("arm",),
            )
        elif service == "kinesis":
            # A provisioned Kinesis stream is billed by shard-hour.  Treat an
            # explicit shard count as workload evidence instead of falling
            # back to a one-unit reference price (which previously produced a
            # zero-dollar quote row).
            shards = requested.get("shards") or requested.get("shard_count")
            if shards:
                add(
                    result,
                    "Kinesis 预置分片小时价",
                    (
                        requirement.quantity
                        * float(shards)
                        * requirement.hours_per_month
                    ),
                    include_any=("storage-shardhour", "shardhourstorage"),
                    exclude=("extended",),
                    unit_contains=("shardhour", "shard hour"),
                )
            requests = requested.get("requests") or requested.get("request_count")
            if requests:
                add(
                    result,
                    "Kinesis 写入负载单价",
                    float(requests),
                    include_any=("putrequestpayloadunits", "putrequest"),
                    exclude=("enhanced",),
                    unit_contains=("putrequest", "request"),
                )
        elif service == "dynamodb":
            storage = requested.get("storage_gib")
            add(
                result,
                "DynamoDB 标准表存储单价",
                float(storage) if storage else None,
                include=("timedstorage-bytehrs",),
                exclude=("ia-", "backup", "restore", "change", "capture"),
            )
        elif service == "eks":
            add(
                result,
                "EKS 标准控制面小时价",
                requirement.quantity * requirement.hours_per_month,
                include=("amazoneks-hours:percluster",),
                exclude=("local", "extended", "provisioned"),
            )
        elif service in {"ecs", "fargate"} and (
            service == "fargate" or requested.get("launch_type") == "fargate"
        ):
            tasks = float(requested.get("tasks") or requirement.quantity)
            task_hours = requested.get("task_hours")
            vcpu = requested.get("task_vcpu")
            memory = requested.get("task_memory_gib")
            compute_hours = float(task_hours) * tasks if task_hours else None
            add(
                result,
                "Fargate vCPU 小时单价",
                compute_hours * float(vcpu) if compute_hours and vcpu else None,
                include=("fargate-vcpu-hours",),
            )
            add(
                result,
                "Fargate 内存 GiB 小时单价",
                compute_hours * float(memory) if compute_hours and memory else None,
                include=("fargate-gb-hours",),
            )
        elif service == "emr":
            common_model = str(requested.get("requested_model") or "").strip()
            roles = (
                (
                    "主节点",
                    "master",
                    requested.get("master_nodes"),
                ),
                (
                    "核心节点",
                    "core",
                    requested.get("core_nodes"),
                ),
                (
                    "任务节点",
                    "task",
                    requested.get("task_nodes"),
                ),
            )
            emitted_role = False
            for label, field_prefix, count in roles:
                if not count:
                    continue
                emitted_role = True
                model = str(
                    requested.get(f"{field_prefix}_requested_model")
                    or common_model
                    or ""
                ).strip()
                add(
                    result,
                    f"Amazon EMR {label}实例小时价",
                    (
                        requirement.quantity
                        * float(count)
                        * requirement.hours_per_month
                    ),
                    # The current AWS Price List publishes EMR instance
                    # surcharges as *BoxUsage* (older fixtures and regions also
                    # use InstanceUsage/RunJobFlow). Support all official names.
                    include_any=(
                        "boxusage",
                        "runjobflow",
                        "instanceusage",
                        "instance usage",
                    ),
                    exclude=("serverless", "studio", "notebook", "reserved", "spot"),
                    unit_contains=("hrs", "hour"),
                    model=model or None,
                    min_vcpu=(
                        float(requested[f"{field_prefix}_vcpu"])
                        if requested.get(f"{field_prefix}_vcpu")
                        else None
                    ),
                    min_memory_gib=(
                        float(requested[f"{field_prefix}_memory_gib"])
                        if requested.get(f"{field_prefix}_memory_gib")
                        else None
                    ),
                    current_generation=not bool(model),
                )
            if not emitted_role:
                add(
                    result,
                    "Amazon EMR 实例小时参考价",
                    None,
                    include_any=(
                        "boxusage",
                        "runjobflow",
                        "instanceusage",
                        "instance usage",
                    ),
                    exclude=("serverless", "studio", "notebook", "reserved", "spot"),
                    unit_contains=("hrs", "hour"),
                    model=common_model or None,
                    current_generation=not bool(common_model),
                )
        elif service == "redshift":
            deployment = str(requested.get("deployment_type") or "").casefold()
            if deployment == "serverless":
                rpu = requested.get("rpu")
                hours = requested.get("hours_per_month")
                add(
                    result,
                    "Amazon Redshift Serverless RPU 小时价",
                    float(rpu) * float(hours) if rpu and hours else None,
                    include_any=("rpu", "serverless"),
                    exclude=("managedstorage", "snapshot"),
                    unit_contains=("rpu", "hour", "hrs"),
                )
            else:
                model = str(requested.get("requested_model") or "").strip()
                storage = requested.get("managed_storage_gib") or requested.get("storage_gib")
                # DC2 local storage cannot safely represent an arbitrary data-warehouse
                # capacity.  When the customer specifies capacity but no node family,
                # use an RA3 compute candidate and its separately metered managed storage.
                effective_model = model or ("ra3" if storage else "")
                nodes = float(requested.get("nodes") or 1)
                add(
                    result,
                    f"Amazon Redshift {effective_model or '计算节点'}小时价",
                    requirement.quantity * nodes * requirement.hours_per_month,
                    include_any=("node", "instance", "runinstances"),
                    exclude=("serverless", "reserved", "snapshot", "managedstorage"),
                    unit_contains=("hrs", "hour"),
                    model=effective_model or None,
                    min_vcpu=float(requested["vcpu"]) if requested.get("vcpu") else None,
                    min_memory_gib=(
                        float(requested["memory_gib"])
                        if requested.get("memory_gib")
                        else None
                    ),
                )
            storage = requested.get("managed_storage_gib") or requested.get("storage_gib")
            if storage:
                add(
                    result,
                    "Amazon Redshift 托管存储单价",
                    float(storage),
                    include_any=("managedstorage", "managed storage", "rms"),
                    exclude=("snapshot", "backup"),
                    unit_contains=("gb", "gib"),
                )
        elif service == "athena":
            scanned = requested.get("data_scanned_gib")
            add(
                result,
                "Athena 查询数据扫描单价",
                float(scanned) / 1024 if scanned else None,
                include=("datascannedintb",),
                exclude=("dpu", "capacity"),
            )
            capacity = requested.get("provisioned_dpu_hours")
            if capacity:
                add(
                    result,
                    "Athena 预置容量 DPU 小时单价",
                    float(capacity),
                    include_any=("dpu", "capacity"),
                    unit_contains=("dpu", "hour", "hrs"),
                )
        elif service == "glue":
            add(
                result,
                "Glue 标准 ETL DPU 小时单价",
                None,
                include=("etl-dpu-hour", "jobrun"),
                exclude=("flex", "memoptimized", "interactive"),
            )
        elif service == "sagemaker":
            model = str(requested.get("requested_model") or "").strip()
            add(
                result,
                f"SageMaker {model or '实例'} 小时单价",
                None,
                include=("hrs", "runinstance"),
                exclude=("reserved", "spot"),
                model=model or None,
            )
        elif service == "cognito":
            add(
                result,
                "Cognito User Pools MAU 单价",
                None,
                include=("cognitouserpoolsmau", "cognitouserpoolsoperation"),
                exclude=("plus", "enterprise", "essentials", "lite", "asf", "mrr"),
            )
        elif service in {"secretsmanager", "secrets_manager"}:
            secrets = requested.get("secret_count")
            add(
                result,
                "Secrets Manager 每个 Secret 月单价",
                float(secrets) if secrets else None,
                include=("secretsmanager-secrets",),
                exclude=("api",),
            )
            api_calls = requested.get("api_calls")
            if api_calls:
                add(
                    result,
                    "Secrets Manager API 请求单价",
                    float(api_calls),
                    include=("secretsmanagerapirequest",),
                )
        elif service == "mq":
            model = str(requested.get("requested_model") or "").removeprefix("mq.")
            broker_count = int(requested.get("broker_count") or 1)
            engine = str(requested.get("engine_type") or "").strip().casefold()
            # Amazon MQ publishes bundled deployment rates: a RabbitMQ
            # three-node product already contains all three Brokers, while a
            # single-instance product contains one.  ``quantity`` therefore
            # multiplies deployments, never the Brokers inside the bundle.
            if engine == "rabbitmq" and broker_count >= 3:
                compute_include = ("rabbitmq-3-instanceusage", "createbroker")
                compute_exclude: tuple[str, ...] = ()
            elif engine == "rabbitmq":
                compute_include = ("rabbitmq-single-instanceusage", "createbroker")
                compute_exclude = ("3-instance",)
            elif engine == "activemq" and broker_count >= 2:
                compute_include = ("multi-azusage", "createbroker")
                compute_exclude = ("rabbitmq",)
            else:
                compute_include = ("single-azusage", "createbroker")
                compute_exclude = ("rabbitmq",) if engine == "activemq" else ()
            add(
                result,
                f"Amazon MQ {model or 'Broker'} 小时价",
                requirement.quantity * requirement.hours_per_month,
                include=compute_include,
                exclude=compute_exclude,
                model=model or None,
                min_vcpu=float(requested["vcpu"]) if requested.get("vcpu") else None,
                min_memory_gib=(
                    float(requested["memory_gib"])
                    if requested.get("memory_gib")
                    else None
                ),
            )
            storage = requested.get("storage_gib_per_broker") or requested.get("storage_gib")
            if storage:
                add(
                    result,
                    "Amazon MQ Broker 存储单价",
                    requirement.quantity * broker_count * float(storage),
                    include=("storage",),
                    exclude=(
                        "backup", "snapshot",
                        *(('activemq',) if engine == "rabbitmq" else ()),
                        *(('rabbitmq',) if engine == "activemq" else ()),
                    ),
                )
        elif service in {"documentdb", "docdb", "mongodb"}:
            model = str(requested.get("requested_model") or "").strip()
            if model.startswith("db."):
                model = model[3:]
            compute_amount = requirement.quantity * requirement.hours_per_month
            add(
                result,
                "Amazon DocumentDB 实例小时价",
                compute_amount,
                include=("database instance",),
                exclude=("serverless", "io-optimized"),
                model=model or None,
                min_vcpu=float(requested["vcpu"]) if requested.get("vcpu") else None,
                min_memory_gib=(
                    float(requested["memory_gib"])
                    if requested.get("memory_gib")
                    else None
                ),
            )
            storage = requested.get("storage_gib")
            add(
                result,
                "Amazon DocumentDB 集群存储单价",
                float(storage) if storage else None,
                include=("database storage",),
                exclude=("backup", "snapshot", "io-optimized"),
            )
        elif service == "dms":
            model = str(requested.get("requested_model") or "").strip()
            add(
                result,
                f"AWS DMS {model or '复制实例'} 小时价",
                requirement.quantity * requirement.hours_per_month,
                include=("instanceusg", "createdmsinstance"),
                exclude=("multi-az", "serverless"),
                model=model or None,
            )
        elif service == "quicksight":
            edition = str(requested.get("edition") or "enterprise").casefold()
            common_exclude = ("free-trial", "free trial", "pro", "-q", "annual")
            author_users = requested.get("author_users")
            reader_users = requested.get("reader_users")
            users = requested.get("users")
            if author_users:
                add(
                    result,
                    "QuickSight 作者用户月费",
                    float(author_users),
                    include=("author subscription", edition),
                    exclude=common_exclude,
                    unit_contains=("user",),
                )
            if reader_users:
                add(
                    result,
                    "QuickSight 读者用户月费",
                    float(reader_users),
                    include=("reader subscription", edition),
                    exclude=common_exclude,
                    unit_contains=("user",),
                )
            if users and not author_users and not reader_users:
                # The generic "user" contract is QuickSight's normal monthly
                # user subscription.  Do not silently reinterpret it as a
                # cheaper Reader, Pro or Amazon Q entitlement.
                add(
                    result,
                    "QuickSight 用户月费",
                    float(users),
                    include=("user subscription", edition, "month"),
                    exclude=common_exclude,
                    unit_contains=("user",),
                )
            spice = requested.get("spice_gib")
            if spice:
                add(
                    result,
                    "QuickSight SPICE 容量月费",
                    float(spice),
                    include=("spice", edition),
                    unit_contains=("gb",),
                )
            sessions = requested.get("session_capacity")
            if sessions:
                add(
                    result,
                    "QuickSight 读者会话用量",
                    float(sessions),
                    include=("reader", "session"),
                    exclude=("free",),
                    unit_contains=("session",),
                )
        elif service == "kms":
            key_count = float(requested.get("key_count") or requirement.quantity)
            add(
                result,
                "AWS KMS 客户托管密钥月费",
                key_count,
                include=("kms-keys",),
                exclude=("request",),
            )
            add(
                result,
                "AWS KMS API 请求单价",
                None,
                include=("kms-requests",),
            )
        elif service == "xray":
            traces = requested.get("traces_stored") or requested.get("trace_count")
            add(
                result,
                "AWS X-Ray 存储 Trace 单价",
                float(traces) if traces else None,
                include=("xray-tracesstored",),
            )
        else:
            # Unknown services may expose many unrelated products. Returning no
            # match is safer than presenting an arbitrary dimension as a quote.
            return []
        return result

    @staticmethod
    def _auto_semantic_rates(
        requirement: ServiceRequirement,
        rates: list[tuple[float, str, str, str, dict[str, object]]],
        *,
        profile: dict[str, object] | None = None,
    ) -> list[
        tuple[str, float | None, tuple[float, str, str, str, dict[str, object]]]
    ]:
        """Safely derive a first-use profile without inventing customer usage.

        Explicit customer quantities may be totalled.  Any dimension selected
        without an explicit quantity is reference-only and therefore cannot
        inflate the quotation total.
        """

        requested = requirement.requirements

        def details(rate):
            attrs = PricingCatalog.attributes(rate[4])
            text = " ".join(
                str(value)
                for value in (
                    rate[1],
                    rate[2],
                    rate[3],
                    attrs.get("productFamily"),
                    attrs.get("instanceType"),
                )
                if value
            ).casefold()
            return attrs, text

        def safe(rate) -> bool:
            _, text = details(rate)
            return not any(
                token in text
                for token in (
                    "credit", "refund", "discount", "tax", "support",
                    "marketplace", "professional service",
                )
            )

        safe_rates = [rate for rate in rates if safe(rate)]
        result: list[
            tuple[str, float | None, tuple[float, str, str, str, dict[str, object]]]
        ] = []
        used: set[tuple[str, str, str]] = set()

        def choose(
            description: str,
            amount: float | None,
            predicate,
        ) -> None:
            candidates = [rate for rate in safe_rates if predicate(rate)]
            if not candidates:
                return
            positive = [rate for rate in candidates if rate[0] > 0] or candidates
            selected = min(positive, key=lambda rate: (rate[0], rate[2], rate[3]))
            identity = (selected[1], selected[2], selected[3])
            if identity not in used:
                used.add(identity)
                result.append((description, amount, selected))

        model = str(requested.get("requested_model") or "").strip().casefold()
        min_vcpu = requested.get("vcpu")
        min_memory = requested.get("memory_gib")

        def hourly_instance(rate) -> bool:
            attrs, text = details(rate)
            unit = str(rate[1]).casefold()
            instance = str(attrs.get("instanceType") or "").casefold()
            if not instance or not any(token in unit for token in ("hrs", "hour")):
                return False
            if model and model not in {instance, f"db.{instance}", f"cache.{instance}"}:
                return False
            try:
                if min_vcpu is not None and float(attrs.get("vcpu") or 0) < float(min_vcpu):
                    return False
            except (TypeError, ValueError):
                return False
            if min_memory is not None:
                memory = str(attrs.get("memory") or attrs.get("memoryGib") or "")
                match = re.search(r"\d+(?:\.\d+)?", memory)
                if not match or float(match.group()) < float(min_memory):
                    return False
            return not any(token in text for token in ("reserved", "spot", "serverless"))

        if any(hourly_instance(rate) for rate in safe_rates):
            choose(
                "AWS 官方最低匹配实例小时价",
                requirement.quantity * requirement.hours_per_month,
                hourly_instance,
            )

        # For a first-use service, prefer the persisted binding between the
        # customer field and AWS's exact UsageType / Operation / Unit.  This
        # prevents a value such as storage or traffic from being attached to a
        # different, cheaper dimension that merely happens to use GB.
        profile_bindings = profile.get("field_bindings") if profile else None
        if isinstance(profile_bindings, list):
            by_field: dict[str, list[dict[str, object]]] = {}
            for binding in profile_bindings:
                if not isinstance(binding, dict):
                    continue
                field = str(binding.get("field") or "")
                if field:
                    by_field.setdefault(field, []).append(binding)

            for field, bindings in by_field.items():
                value = requested.get(field)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                    continue

                def bound_rate(rate, *, candidates=bindings) -> bool:
                    for binding in candidates:
                        if str(rate[1]).casefold() != str(binding.get("unit") or "").casefold():
                            continue
                        if str(rate[2]) != str(binding.get("usage_type") or ""):
                            continue
                        if str(rate[3]) != str(binding.get("operation") or ""):
                            continue
                        bound_instance = str(binding.get("instance_type") or "").casefold()
                        if bound_instance:
                            attrs = PricingCatalog.attributes(rate[4])
                            if str(attrs.get("instanceType") or "").casefold() != bound_instance:
                                continue
                        return True
                    return False

                amount = float(value)
                if field == "hours_per_month":
                    amount *= requirement.quantity
                label = next(
                    (
                        str(binding.get("label"))
                        for binding in bindings
                        if binding.get("label")
                    ),
                    field,
                )
                choose(f"AWS 官方{label}单价", amount, bound_rate)

        explicit_dimensions = (
            (
                "storage_gib",
                "AWS 官方存储单价",
                lambda rate: any(
                    token in str(rate[1]).casefold()
                    for token in ("gb-mo", "gb-month", "gib-month")
                )
                and not any(token in details(rate)[1] for token in ("backup", "snapshot")),
            ),
            (
                "requests",
                "AWS 官方请求单价",
                lambda rate: any(
                    token in str(rate[1]).casefold()
                    for token in ("request", "api call", "message", "event")
                ),
            ),
            (
                "data_processed_gib",
                "AWS 官方数据处理单价",
                lambda rate: str(rate[1]).casefold() in {"gb", "gbyte", "gigabyte"}
                and any(token in details(rate)[1] for token in ("process", "scan", "ingest")),
            ),
            (
                "data_transfer_out_gib",
                "AWS 官方出站流量单价",
                lambda rate: str(rate[1]).casefold() in {"gb", "gbyte", "gigabyte"}
                and any(token in details(rate)[1] for token in ("transfer", "out", "egress")),
            ),
            (
                "input_tokens",
                "AWS 官方输入 Token 单价",
                lambda rate: "token" in str(rate[1]).casefold()
                and "input" in details(rate)[1],
            ),
            (
                "output_tokens",
                "AWS 官方输出 Token 单价",
                lambda rate: "token" in str(rate[1]).casefold()
                and "output" in details(rate)[1],
            ),
        )
        for field, description, predicate in explicit_dimensions:
            value = requested.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                choose(description, float(value), predicate)

        if result:
            return result

        # With no customer usage, select one real, smallest official billing
        # unit. This is the system-wide minimum-unit rule requested for
        # first-use services; it never invents a monthly workload quantity.
        preferred = [
            rate
            for rate in safe_rates
            if rate[0] > 0
            and any(
                token in str(rate[1]).casefold()
                for token in (
                    "hour", "hrs", "request", "gb", "token", "message",
                    "event", "quantity", "unit", "user", "workspace",
                )
            )
        ]
        for rate in sorted(preferred, key=lambda item: (item[0], item[1], item[2])):
            identity = (rate[1], rate[2], rate[3])
            if identity in used:
                continue
            used.add(identity)
            result.append((f"AWS 官方最小 {rate[1]} 计费单位", 1.0, rate))
            break
        return result

    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        selection = self.select(requirement, default_region)
        return PreviewSelection(
            component_id="component",
            service=requirement.service,
            display_name=selection.display_name,
            region=selection.region,
            selected_model=selection.model,
            selection_reason=selection.rationale,
            candidates=[
                CandidateOption(
                    model=selection.model,
                    family=requirement.service,
                    specifications=selection.specifications,
                    rationale=selection.rationale,
                    official_product=selection.official_product,
                    is_default=True,
                )
            ],
            requires_confirmation=False,
        )
