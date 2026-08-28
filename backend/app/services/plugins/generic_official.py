from __future__ import annotations

import math
import re

from app.core.errors import ManualConfirmationRequired
from app.domain.customer_facts import scoped_amount
from app.domain.models import (
    CandidateOption,
    PreviewSelection,
    ReferenceRate,
    SelectedResource,
    ServiceRequirement,
    UsageLine,
)
from app.integrations.auto_service_discovery import AutoServiceDiscovery
from app.integrations.aws import AwsClients, PricingCatalog
from app.integrations.aws_supported_services import (
    CURATED_ENDPOINT_SERVICE_IDS,
    CURATED_SERVICE_OFFER_CODES,
    RETIRED_AWS_SERVICE_PROFILES,
)
from app.integrations.service_templates import SERVICE_TEMPLATE_FIELDS
from app.services.aws_query_executor import ReadOnlyAwsQueryExecutor

_SERVICE_CODE_ALIASES = {
    # Several AWS resources are billed inside a parent offer rather than an
    # offer named after the customer-facing product.  Keep those identities
    # explicit and validate every target against the live official registry.
    "ebs": "AmazonEC2",
    "natgateway": "AmazonEC2",
    "opensearch": "AmazonES",
    "sqs": "AWSQueueService",
    "scheduler": "AWSEvents",
    "eventbridge": "AWSEvents",
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
    # The public product is called "AWS Step Functions", but its official
    # Price List ServiceCode is the historical ``AmazonStates``.  Never derive
    # a ServiceCode from the marketing name.
    "stepfunctions": "AmazonStates",
    "bedrock": "AmazonBedrock",
    "cloudmap": "AWSCloudMap",
    # AppConfig dimensions are published inside the Systems Manager offer.
    "appconfig": "AWSSystemsManager",
    "documentdb": "AmazonDocDB",
    "docdb": "AmazonDocDB",
    "mongodb": "AmazonDocDB",
    "memorydb": "AmazonMemoryDB",
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
    "pinpoint": "AmazonPinpoint",
}

# Fixed business templates sometimes use a service-qualified name while the
# official dimension layer intentionally uses one cross-service semantic name.
# This bridge is used only when a dedicated adapter left an explicit customer
# field unconsumed.  It does not choose a price or service; the exact official
# profile still decides which UsageType/Operation/Unit is valid.
_SUPPLEMENT_FIELD_ALIASES = {
    "ebs_iops": "iops",
    "storage_iops": "iops",
    "ebs_throughput_mbps": "throughput_mbps",
    "storage_throughput_mbps": "throughput_mbps",
    "log_storage_gib": "storage_gib",
    "snapshot_changed_gib": "backup_storage_gib",
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
        self._unavailable_region_cache: set[tuple[str, str]] = set()

    def _service_identity_stems(self, requirement: ServiceRequirement) -> list[str]:
        return list(
            dict.fromkeys(
                stem
                for stem in (
                    _stem(requirement.service),
                    _stem(requirement.calculator_service_name or ""),
                )
                if stem
            )
        )

    def supported_regions(self, requirement: ServiceRequirement) -> list[str]:
        """Return regions from the locally bundled official endpoint metadata."""

        session = getattr(self.clients, "session", None)
        if session is None:
            return []
        endpoint_ids: tuple[str, ...] = ()
        for stem in self._service_identity_stems(requirement):
            endpoint_ids = CURATED_ENDPOINT_SERVICE_IDS.get(stem, ())
            if endpoint_ids:
                break
        if not endpoint_ids:
            available_services = set(session.get_available_services())
            for stem in self._service_identity_stems(requirement):
                direct = next(
                    (
                        service_id
                        for service_id in available_services
                        if _stem(service_id) == stem
                    ),
                    None,
                )
                if direct:
                    endpoint_ids = (direct,)
                    break
        if not endpoint_ids:
            return []
        region_sets = [
            set(session.get_available_regions(service_id)) for service_id in endpoint_ids
        ]
        if not region_sets:
            return []
        regions = set.intersection(*region_sets)
        return sorted(region for region in regions if region and not region.startswith("cn-"))

    def _region_candidates(
        self, requirement: ServiceRequirement, current_region: str
    ) -> list[dict[str, object]]:
        return [
            {
                "model": region,
                "family": "aws_region",
                "specifications": {"region": region, "label": region},
                "rationale": "AWS 官方端点目录中当前可用的部署区域。",
            }
            for region in self.supported_regions(requirement)
            if region != current_region
        ]

    def _retired_profile(self, requirement: ServiceRequirement) -> dict[str, object] | None:
        for stem in self._service_identity_stems(requirement):
            profile = RETIRED_AWS_SERVICE_PROFILES.get(stem)
            if profile is not None:
                return profile
        return None

    def refresh_component(self, requirement: ServiceRequirement) -> None:
        """Refresh only one component's discovery data before an isolated retry."""

        try:
            service_code = self._service_code(requirement)
        except ManualConfirmationRequired:
            service_code = ""
        if service_code:
            self._unavailable_region_cache.discard(
                (service_code, requirement.region or "ap-southeast-1")
            )
        self._refresh_official_profile(requirement)

    def _service_code(self, requirement: ServiceRequirement) -> str:
        labels = [requirement.service, requirement.calculator_service_name or ""]
        official_codes = self.catalog.service_codes()
        codes_by_identity = {
            _canonical(code): code for code in official_codes if _canonical(code)
        }
        curated_key = requirement.service.strip().casefold().replace("-", "_")
        if configured := CURATED_SERVICE_OFFER_CODES.get(curated_key):
            if resolved := codes_by_identity.get(_canonical(configured)):
                return resolved
        for label in labels:
            canonical = _canonical(label)
            stem = _stem(label)
            if canonical in _SERVICE_CODE_ALIASES:
                configured = _SERVICE_CODE_ALIASES[canonical]
                if resolved := codes_by_identity.get(_canonical(configured)):
                    return resolved
            if stem in _SERVICE_CODE_ALIASES:
                configured = _SERVICE_CODE_ALIASES[stem]
                if resolved := codes_by_identity.get(_canonical(configured)):
                    return resolved
        stems: dict[str, list[str]] = {}
        for code in official_codes:
            stems.setdefault(_stem(code), []).append(code)
        for label in labels:
            matches = stems.get(_stem(label), [])
            if len(matches) == 1:
                return matches[0]
        if self.auto_discovery is not None:
            try:
                return self.auto_discovery.resolve_service_code(
                    requirement.service,
                    requirement.calculator_service_name or requirement.service,
                )
            except ManualConfirmationRequired:
                pass
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

    def _enrich_missing_instance_shapes(
        self,
        requirement: ServiceRequirement,
        rates: list[tuple[float, str, str, str, dict[str, object]]],
        region: str,
    ) -> list[tuple[float, str, str, str, dict[str, object]]]:
        """Join managed-service instance prices to the official EC2 shape API.

        Several AWS Price List offers publish an EC2-compatible instanceType
        but omit vCPU and memory (Amazon EMR is a common example). Repeatedly
        downloading that same offer can never add the missing shape. Query the
        provider's read-only EC2 specification endpoint for only the customer
        requested shapes and attach those official attributes to matching
        price rows in memory.
        """

        requested = requirement.requirements
        requested_shapes: set[tuple[int, int]] = set()
        for prefix in ("", "master_", "core_", "task_"):
            raw_vcpu = requested.get(f"{prefix}vcpu")
            raw_memory = requested.get(f"{prefix}memory_gib")
            if raw_vcpu in (None, "") or raw_memory in (None, ""):
                continue
            try:
                requested_shapes.add((int(float(raw_vcpu)), int(float(raw_memory))))
            except (TypeError, ValueError):
                continue
        requested_models = {
            str(requested.get(field) or "").strip()
            for field in (
                "requested_model",
                "master_requested_model",
                "core_requested_model",
                "task_requested_model",
            )
            if str(requested.get(field) or "").strip()
        }
        missing_models = {
            str(PricingCatalog.attributes(rate[4]).get("instanceType") or "").strip()
            for rate in rates
            if PricingCatalog.attributes(rate[4]).get("instanceType")
            and any(value is None for value in self._official_instance_shape(rate[4])[1:])
        }
        if not missing_models or (not requested_shapes and not requested_models):
            return rates

        shapes_by_model: dict[str, tuple[float, float]] = {}
        executor = ReadOnlyAwsQueryExecutor(self.clients)

        def remember(payload: dict[str, object]) -> None:
            for page in payload.get("pages", [payload]):
                if not isinstance(page, dict):
                    continue
                for item in page.get("InstanceTypes", []):
                    if not isinstance(item, dict):
                        continue
                    try:
                        shapes_by_model[str(item["InstanceType"]).casefold()] = (
                            float(item["VCpuInfo"]["DefaultVCpus"]),
                            float(item["MemoryInfo"]["SizeInMiB"]) / 1024,
                        )
                    except (KeyError, TypeError, ValueError):
                        continue

        try:
            exact_models = sorted(requested_models & missing_models)
            if exact_models:
                remember(
                    executor.execute(
                        service="ec2",
                        operation="describe_instance_types",
                        region=region,
                        parameters={"InstanceTypes": exact_models},
                        paginate=False,
                    )
                )
            for vcpu, memory_gib in sorted(requested_shapes):
                remember(
                    executor.execute(
                        service="ec2",
                        operation="describe_instance_types",
                        region=region,
                        parameters={
                            "Filters": [
                                {
                                    "Name": "vcpu-info.default-vcpus",
                                    "Values": [str(vcpu)],
                                },
                                {
                                    "Name": "memory-info.size-in-mib",
                                    "Values": [str(memory_gib * 1024)],
                                },
                            ]
                        },
                        max_items=100,
                    )
                )
        except ManualConfirmationRequired:
            # The normal catalog path remains authoritative. If the read-only
            # shape endpoint is unavailable, return the untouched rows and let
            # the caller expose one bounded technical issue instead of looping.
            return rates

        enriched = []
        for price, unit, usage_type, operation, product in rates:
            attrs = PricingCatalog.attributes(product)
            model = str(attrs.get("instanceType") or "").casefold()
            shape = shapes_by_model.get(model)
            if shape is None:
                enriched.append((price, unit, usage_type, operation, product))
                continue
            vcpu, memory_gib = shape
            copied_product = {
                **product,
                "product": {
                    **dict(product.get("product", {})),
                    "attributes": {
                        **attrs,
                        "vcpu": str(vcpu),
                        "memory": f"{memory_gib:g} GiB",
                    },
                },
            }
            enriched.append((price, unit, usage_type, operation, copied_product))
        return enriched

    @staticmethod
    def _official_instance_shape(
        product: dict[str, object],
    ) -> tuple[str, float | None, float | None]:
        """Read a purchasable model shape only from AWS product attributes."""

        attrs = PricingCatalog.attributes(product)
        model = str(attrs.get("instanceType") or "").strip()

        def number(value: object) -> float | None:
            if isinstance(value, bool):
                return None
            match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
            if not match:
                return None
            parsed = float(match.group())
            return parsed if parsed > 0 else None

        return (
            model,
            number(attrs.get("vcpu") or attrs.get("vCPU")),
            number(attrs.get("memoryGib") or attrs.get("memoryGiB") or attrs.get("memory")),
        )

    @classmethod
    def _candidate_specifications(
        cls,
        product: dict[str, object],
    ) -> dict[str, object]:
        model, vcpu, memory = cls._official_instance_shape(product)
        return {
            **({"instanceType": model} if model else {}),
            **({"vCPU": vcpu} if vcpu is not None else {}),
            **({"memoryGiB": memory} if memory is not None else {}),
        }

    @staticmethod
    def _instance_rate_matches_requirement(
        requirement: ServiceRequirement,
        rate: tuple[float, str, str, str, dict[str, object]],
    ) -> bool:
        """Keep only models belonging to the requested managed product mode."""

        _price, unit, usage_type, operation, product = rate
        attrs = PricingCatalog.attributes(product)
        model = str(attrs.get("instanceType") or "").strip()
        if not model or not any(token in str(unit).casefold() for token in ("hour", "hrs")):
            return False
        text = " ".join(
            str(value)
            for value in (
                unit,
                usage_type,
                operation,
                attrs.get("productFamily"),
                attrs.get("engine"),
                attrs.get("databaseEngine"),
                attrs.get("deploymentOption"),
                *attrs.values(),
            )
            if value
        ).casefold()
        if any(
            token in text
            for token in (
                "reserved",
                "spot",
                "serverless",
                "snapshot",
                "iooptimized",
                "io-optimized",
            )
        ):
            return False

        service = _stem(requirement.service)
        requested = requirement.requirements
        engine = str(requested.get("engine_type") or requested.get("engine") or "").casefold()
        if service == "memorydb":
            if engine == "redis" and "valkey" in text:
                return False
            if engine == "valkey" and "valkey" not in text:
                return False
        elif service in {"documentdb", "docdb", "mongodb"}:
            # The live AWS Query API currently omits productFamily for many
            # DocumentDB rows, while UsageType remains authoritative.
            if not any(marker in text for marker in ("database instance", "instanceusage")):
                return False
        elif service == "mq":
            broker_count = int(requested.get("broker_count") or 1)
            if engine == "rabbitmq" and "rabbitmq" not in text:
                return False
            if engine == "activemq" and "rabbitmq" in text:
                return False
            if engine == "rabbitmq":
                if broker_count >= 3 and "3-instance" not in text:
                    return False
                if broker_count < 3 and "3-instance" in text:
                    return False
            if engine == "activemq":
                multi_az = any(marker in text for marker in ("multi-az", "multi az"))
                if (broker_count >= 2) != multi_az:
                    return False
        return True

    def configuration_candidates(
        self,
        requirement: ServiceRequirement,
        default_region: str,
    ) -> list[CandidateOption]:
        """Return all regional official instance choices for generic services."""

        region = requirement.region or default_region
        service_code = self._service_code(requirement)
        rates = self._catalog_rates(service_code, region)
        rates = self._enrich_missing_instance_shapes(requirement, rates, region)
        by_model: dict[
            str,
            tuple[float, tuple[float, str, str, str, dict[str, object]]],
        ] = {}
        for rate in rates:
            if not self._instance_rate_matches_requirement(requirement, rate):
                continue
            model, vcpu, memory = self._official_instance_shape(rate[4])
            if not model or (vcpu is None and memory is None):
                continue
            current = by_model.get(model.casefold())
            if current is None or rate[0] < current[0]:
                by_model[model.casefold()] = (rate[0], rate)

        result = [
            CandidateOption(
                model=self._official_instance_shape(rate[4])[0],
                family=requirement.service,
                specifications=self._candidate_specifications(rate[4]),
                monthly_catalog_cost=price * requirement.hours_per_month,
                rationale="AWS 当前区域可购买的官方实例规格。",
                official_product=rate[4],
            )
            for price, rate in by_model.values()
        ]
        return sorted(
            result,
            key=lambda candidate: (
                candidate.monthly_catalog_cost is None,
                candidate.monthly_catalog_cost or 0,
                candidate.model,
            ),
        )

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

    @staticmethod
    def _billing_variant_label(binding: dict[str, object]) -> str:
        """Turn an official dimension variant into a short customer choice."""

        raw_text = " ".join(
            str(binding.get(key) or "")
            for key in ("usage_type", "operation", "description")
        )
        # Official UsageTypes mix CamelCase, dashes and colons.  Normalize all
        # three before classification so SingleAuthorizationRequest cannot be
        # mistaken for the broader AuthorizationRequest token.
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", raw_text)
        text = re.sub(r"[^a-zA-Z0-9]+", " ", text).strip().casefold()
        usage_text = re.sub(
            r"[^a-zA-Z0-9]+",
            " ",
            re.sub(
                r"(?<=[a-z0-9])(?=[A-Z])",
                " ",
                str(binding.get("usage_type") or ""),
            ),
        ).strip().casefold()
        quicksight_plans = (
            (r"author pro enterprise month q$", "企业版 Author Pro + Amazon Q（月付）"),
            (r"author pro enterprise month$", "企业版 Author Pro（月付）"),
            (r"qs user enterprise annual$", "企业版作者（年付）"),
            (r"qs user enterprise month$", "企业版作者（月付）"),
            (r"reader pro enterprise month q$", "企业版 Reader Pro + Amazon Q（月付）"),
            (r"reader pro enterprise month$", "企业版 Reader Pro（月付）"),
            (r"reader enterprise month$", "企业版读者（月付）"),
        )
        for pattern, label in quicksight_plans:
            if re.search(pattern, usage_text):
                return label
        capacity = re.search(r"reader capacity (\d+) k usage$", usage_text)
        if capacity:
            sessions = int(capacity.group(1)) * 1_000
            amount = f"{sessions // 10_000} 万"
            return f"年度 {amount}次读者会话套餐"
        if re.search(r"reader usage paid session q$", usage_text):
            return "按实际读者会话付费（含 Amazon Q）"
        if re.search(r"reader usage paid session$", usage_text):
            return "按实际读者会话付费"
        choices = (
            (("secondary endpoint",), "辅助端点"),
            (("advanced inspection endpoint",), "高级检测端点"),
            (("endpoint hour", "firewallendpoint"), "普通防火墙端点"),
            (("advanced threat",), "高级威胁防护流量"),
            (("advanced inspection",), "高级检测流量"),
            (("transit gateway", "transitgateway"), "通过 Transit Gateway 的流量"),
            (("privatelink", "private link"), "通过 PrivateLink 处理"),
            (("dest ext", "outside aws", "external"), "发送到 AWS 外部"),
            (("dest aws", "destination aws"), "发送到 AWS 服务"),
            (("traffic gb processed", "data processing"), "普通防火墙处理流量"),
            (("event api connection",), "Event API 连接"),
            (("connection duration",), "GraphQL 实时连接"),
            (("io optimized storage", "io optimizedstorage"), "I/O 优化存储"),
            (("storage usage",), "标准存储"),
            (("graph snapshot",), "图数据库快照存储"),
            (("backup usage",), "数据库备份存储"),
            (("enterprise spice", "qs enterprise"), "QuickSight 企业版 SPICE"),
            (("provisioned spice", "qs provisioned"), "QuickSight 预置容量 SPICE"),
            (("reader usage paid session",), "按实际读者会话付费"),
            (("reader usage cap session",), "读者会话封顶计费"),
            (("single authorization",), "单次授权请求"),
            (("batch authorization",), "批量授权请求"),
            (("create policy",), "创建策略请求"),
            (("get policy",), "读取策略请求"),
            (("list policies",), "查询策略列表请求"),
            (("update policy",), "更新策略请求"),
        )
        for markers, label in choices:
            if any(marker in text for marker in markers):
                return label
        description = str(binding.get("description") or "").strip()
        description = re.sub(
            r"^(?:usd\s*)?\$?\d+(?:\.\d+)?\s+per\s+",
            "",
            description,
            flags=re.I,
        )
        return description[:80] or str(binding.get("usage_type") or "这种收费方式")

    @staticmethod
    def _billing_variant_source_markers(label: str) -> tuple[str, ...]:
        """Phrases that prove the customer already selected one variant."""

        return {
            "单次授权请求": ("单次授权", "单个授权", "single authorization"),
            "批量授权请求": ("批量授权", "batch authorization"),
            "创建策略请求": ("创建策略", "create policy"),
            "读取策略请求": ("读取策略", "get policy"),
            "查询策略列表请求": ("策略列表", "list policies"),
            "更新策略请求": ("更新策略", "update policy"),
            "高级威胁防护流量": ("高级威胁防护", "advanced threat"),
            "高级检测流量": ("高级检测", "advanced inspection"),
            "通过 Transit Gateway 的流量": ("transit gateway", "中转网关"),
            "通过 PrivateLink 处理": ("privatelink", "私网连接"),
            "发送到 AWS 外部": ("发送到 aws 外部", "传到 aws 外部", "外部目的地"),
            "发送到 AWS 服务": ("发送到 aws 服务", "传到 aws 服务", "aws 内部目的地"),
            "辅助端点": ("辅助端点", "secondary endpoint"),
            "高级检测端点": ("高级检测端点", "advanced inspection endpoint"),
            "普通防火墙端点": ("普通防火墙端点", "标准防火墙端点"),
            "普通防火墙处理流量": ("普通防火墙流量", "标准防火墙流量"),
            "Event API 连接": ("event api",),
            "GraphQL 实时连接": ("graphql", "graphql 实时"),
            "I/O 优化存储": ("i/o 优化", "io 优化", "io-optimized"),
            "标准存储": ("标准存储", "standard storage"),
            "图数据库快照存储": ("快照", "snapshot"),
            "数据库备份存储": ("备份存储", "数据库备份", "backup storage"),
            "QuickSight 企业版 SPICE": ("企业版", "enterprise"),
            "QuickSight 预置容量 SPICE": ("预置容量", "provisioned spice"),
        }.get(label, ())

    @classmethod
    def _require_billing_variant_choice(
        cls,
        requirement: ServiceRequirement,
        profile: dict[str, object] | None,
    ) -> None:
        """Resolve detailed official dimensions without burdening customers.

        Customer text still wins when it explicitly names a billing variant.
        Otherwise choose the lowest-priced compatible base dimension and lock
        that identity for the rest of the quote.  Architecture, unsupported
        regions/services, conflicting specifications, and mutually exclusive
        billing models are handled by their dedicated confirmation rules; raw
        AWS UsageType details are not useful customer questions.
        """

        raw_bindings = (profile or {}).get("field_bindings")
        dimensions = (profile or {}).get("dimensions")
        if not isinstance(raw_bindings, list) or not isinstance(dimensions, list):
            return
        prices: dict[tuple[str, str, str], float] = {}
        for dimension in dimensions:
            if not isinstance(dimension, dict):
                continue
            identity = (
                str(dimension.get("usage_type") or ""),
                str(dimension.get("operation") or ""),
                str(dimension.get("unit") or ""),
            )
            try:
                prices[identity] = float(dimension.get("price") or 0)
            except (TypeError, ValueError):
                continue

        source = requirement.source_text or ""
        reader_billing_mode = str(
            requirement.requirements.get("_billing_variant_reader_billing_mode") or ""
        ).strip()
        by_field: dict[str, list[dict[str, object]]] = {}
        for binding in raw_bindings:
            if isinstance(binding, dict) and binding.get("field"):
                by_field.setdefault(str(binding["field"]), []).append(binding)

        for field, bindings in by_field.items():
            if (
                reader_billing_mode == "per_user"
                and field == "session_capacity"
            ) or (
                reader_billing_mode == "capacity"
                and field == "reader_users"
            ):
                continue
            if field == "hours_per_month":
                has_value = bool(
                    requirement.field_evidence.get("hours_per_month")
                    or re.search(r"\d+(?:\.\d+)?\s*(?:小时|hours?|hrs?)", source, re.I)
                )
            else:
                value = requirement.requirements.get(field)
                has_value = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
            if not has_value or requirement.requirements.get(f"_billing_variant_{field}"):
                continue
            unique: dict[tuple[str, str, str], dict[str, object]] = {}
            for binding in bindings:
                identity = (
                    str(binding.get("usage_type") or ""),
                    str(binding.get("operation") or ""),
                    str(binding.get("unit") or ""),
                )
                unique.setdefault(identity, binding)
            positive = {
                identity: binding
                for identity, binding in unique.items()
                if prices.get(identity, 0) > 0
            }
            variants = positive or unique
            if field in {"author_users", "reader_users"}:
                edition = str(requirement.requirements.get("edition") or "").casefold()
                source_mentions_q = bool(
                    re.search(
                        r"amazon\s+q(?:\b|[^a-z])|quicksight\s+q(?:\b|[^a-z])|含\s*q(?:\b|[^a-z])",
                        source,
                        re.I,
                    )
                )
                matching_roles: dict[
                    tuple[str, str, str], dict[str, object]
                ] = {}
                for identity, binding in variants.items():
                    usage_folded = identity[0].casefold()
                    if edition in {"enterprise", "standard"} and edition not in usage_folded:
                        continue
                    if usage_folded.endswith("-q") and not source_mentions_q:
                        continue
                    matching_roles[identity] = binding
                if matching_roles:
                    variants = matching_roles
            if field == "session_capacity":
                # Capacity pricing publishes the included tier and its overage
                # row as separate UsageTypes. They are two parts of one plan,
                # not two customer choices. Offer only complete base plans and
                # hide tiers that cannot cover the stated monthly volume.
                monthly_sessions = float(
                    requirement.requirements.get("session_capacity") or 0
                )
                annual_sessions = monthly_sessions * 12
                source_mentions_q = bool(
                    re.search(
                        r"amazon\s+q(?:\b|[^a-z])|quicksight\s+q(?:\b|[^a-z])|含\s*q(?:\b|[^a-z])",
                        source,
                        re.I,
                    )
                )
                usable_session_plans: dict[
                    tuple[str, str, str], dict[str, object]
                ] = {}
                for identity, binding in variants.items():
                    usage_folded = identity[0].casefold()
                    if any(
                        marker in usage_folded
                        for marker in ("-extra", "bonus", "report", "-cap-")
                    ):
                        continue
                    if usage_folded.endswith("-q") and not source_mentions_q:
                        continue
                    capacity_match = re.search(
                        r"reader-capacity-(\d+)k-usage$", usage_folded
                    )
                    if capacity_match:
                        capacity = int(capacity_match.group(1)) * 1_000
                        if annual_sessions > capacity:
                            continue
                    if "reader-capacity" in usage_folded or "reader-usage-paid" in usage_folded:
                        usable_session_plans[identity] = binding
                if usable_session_plans:
                    def session_plan_rank(item):
                        usage = item[0][0].casefold()
                        match = re.search(r"reader-capacity-(\d+)k-usage$", usage)
                        return (0, int(match.group(1))) if match else (1, usage)

                    variants = dict(
                        sorted(usable_session_plans.items(), key=session_plan_rank)
                    )
            # One official meaning may appear twice (for example a regional
            # and a Global UsageType).  That is not a customer choice.  Group
            # by the customer-facing meaning and prefer the regional identity
            # for regional components so duplicate catalog rows never produce
            # duplicate buttons or block literal-source resolution.
            semantic_variants: dict[
                str, tuple[tuple[str, str, str], dict[str, object]]
            ] = {}
            for identity, binding in variants.items():
                label = cls._billing_variant_label(binding)
                existing = semantic_variants.get(label)
                current_is_global = identity[0].casefold().startswith("global-")
                if existing is None or (
                    requirement.region
                    and existing[0][0].casefold().startswith("global-")
                    and not current_is_global
                ):
                    semantic_variants[label] = (identity, binding)
            if len(semantic_variants) <= 1:
                if len(variants) > 1 and semantic_variants:
                    identity, _ = next(iter(semantic_variants.values()))
                    requirement.requirements[f"_billing_variant_{field}"] = identity[0]
                continue
            source_folded = source.casefold()
            source_matches = [
                identity
                for label, (identity, _binding) in semantic_variants.items()
                if any(
                    marker in source_folded
                    for marker in cls._billing_variant_source_markers(label)
                )
            ]
            if len(source_matches) == 1:
                key = f"_billing_variant_{field}"
                requirement.requirements[key] = source_matches[0][0]
                requirement.field_sources[f"requirements.{key}"] = "customer_text"
                requirement.field_evidence[f"requirements.{key}"] = (
                    "客户原话已明确收费方式"
                )
                continue
            # Prefer the ordinary/base product over optional add-ons even when
            # an add-on publishes a deceptively low unit rate.  Among equally
            # compatible base products, use the actual lowest positive rate.
            # The final UsageType is persisted so preview and final pricing can
            # never drift to different catalog rows.
            addon_markers = (
                "advanced",
                "transit gateway",
                "transitgateway",
                "privatelink",
                "private link",
                "lambda edge",
                "origin shield",
                "originshield",
                "keyvaluestore",
                "kvs",
                "io optimized",
                "overage",
                " extra",
                " pro ",
                "amazon q",
                "free trial",
                "free tier",
                "promotion",
            )

            def default_rank(
                item: tuple[str, tuple[tuple[str, str, str], dict[str, object]]]
            ) -> tuple[int, int, float, str, str, str]:
                label, (identity, binding) = item
                searchable = " ".join(
                    (
                        label,
                        identity[0],
                        identity[1],
                        str(binding.get("description") or ""),
                    )
                )
                searchable = re.sub(
                    r"[^a-z0-9]+", " ", searchable.casefold()
                )
                padded = f" {searchable} "
                addon_penalty = int(
                    any(marker in padded for marker in addon_markers)
                )
                price = prices.get(identity, 0)
                missing_price = int(price <= 0)
                return (
                    addon_penalty,
                    missing_price,
                    price if price > 0 else float("inf"),
                    identity[0],
                    identity[1],
                    identity[2],
                )

            _label, (identity, _binding) = min(
                semantic_variants.items(), key=default_rank
            )
            key = f"_billing_variant_{field}"
            requirement.requirements[key] = identity[0]
            requirement.field_sources[f"requirements.{key}"] = (
                "system_lowest_compatible"
            )
            requirement.field_evidence[f"requirements.{key}"] = (
                "客户未指定细分收费项，系统使用符合原需求的最低价基础计费项"
            )
            requirement.locked_fields = sorted(
                set(requirement.locked_fields) | {f"requirements.{key}"}
            )

    @staticmethod
    def _require_cross_field_billing_mode(
        requirement: ServiceRequirement,
    ) -> None:
        """Ask once when two explicit quantities describe alternative plans."""

        if _stem(requirement.service) != "quicksight":
            return
        requested = requirement.requirements
        readers = requested.get("reader_users")
        sessions = requested.get("session_capacity")
        if not (
            isinstance(readers, (int, float))
            and not isinstance(readers, bool)
            and readers > 0
            and isinstance(sessions, (int, float))
            and not isinstance(sessions, bool)
            and sessions > 0
        ):
            return
        if requested.get("_billing_variant_reader_billing_mode"):
            return
        display_name = requirement.calculator_service_name or requirement.service
        raise ManualConfirmationRequired(
            f"{display_name} 的读者可以按人数付费，也可以按会话容量付费，不能两种一起算。"
            "这次要用哪一种？",
            code="billing_variant_required",
            field="reader_billing_mode",
            nearby_candidates=[
                {
                    "model": f"按读者人数付费（{float(readers):g} 名）",
                    "family": "billing_variant",
                    "specifications": {
                        "decision": "billing_variant:reader_billing_mode:per_user",
                        "field": "reader_billing_mode",
                    },
                    "rationale": "按已填写的读者人数计算。",
                },
                {
                    "model": f"按读者会话容量付费（每月 {float(sessions):g} 次）",
                    "family": "billing_variant",
                    "specifications": {
                        "decision": "billing_variant:reader_billing_mode:capacity",
                        "field": "reader_billing_mode",
                    },
                    "rationale": "按已填写的会话容量计算。",
                },
            ],
        )

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        retired_profile = self._retired_profile(requirement)
        if retired_profile is not None:
            replacements = retired_profile.get("replacements")
            nearby_candidates = [
                {
                    "model": str(item.get("label") or "").strip(),
                    "family": "service_replacement",
                    "specifications": {
                        "decision": str(item.get("decision") or "").strip()
                    },
                    "rationale": "由客户决定是否采用仍受支持的服务。",
                }
                for item in replacements
                if isinstance(item, dict) and item.get("label") and item.get("decision")
            ] if isinstance(replacements, (list, tuple)) else []
            raise ManualConfirmationRequired(
                f"{retired_profile.get('display_name') or requirement.service} 已停止服务，"
                "请选择仍受支持的替代方案或移出本次报价",
                code="service_retired",
                retired_on=retired_profile.get("retired_on"),
                nearby_candidates=nearby_candidates,
            )
        # Region availability is an endpoint capability question, not a price
        # search. Botocore ships AWS's signed endpoint catalogue locally, so a
        # service that is not offered in the selected region can be rejected
        # immediately with valid alternatives. Previously these components
        # downloaded up to 40 Price List pages, refreshed discovery, and then
        # repeated the same doomed query on every retry. This guard applies to
        # every service identity that resolves to an AWS endpoint id.
        supported_regions = self.supported_regions(requirement)
        if region != "global" and supported_regions and region not in supported_regions:
            raise ManualConfirmationRequired(
                f"{requirement.calculator_service_name or requirement.service} "
                f"当前不支持区域 {region}，请选择该服务实际可用的 AWS 区域",
                code="service_region_not_supported",
                region=region,
                nearby_candidates=self._region_candidates(requirement, region),
            )
        # CodeDeploy does not add a service charge for deployments to EC2.
        # Treat that as a valid zero-cost official result instead of forcing a
        # pricing-catalog lookup that has no positive dimension to return.
        if _stem(requirement.service) == "codedeploy":
            source = (requirement.source_text or "").casefold()
            is_on_premises = any(
                marker in source
                for marker in ("on-prem", "on premises", "本地实例", "本地服务器")
            )
            if not is_on_premises:
                return SelectedResource(
                    service=requirement.service,
                    display_name=requirement.calculator_service_name or "AWS CodeDeploy",
                    region=region,
                    model="EC2 部署（无额外服务费）",
                    architecture="使用 AWS CodeDeploy 部署到 Amazon EC2",
                    specifications=dict(requirement.requirements),
                    official_product={
                        "source": "AWS CodeDeploy Pricing",
                        "pricingMode": "no-additional-charge-for-ec2",
                    },
                    rationale="部署到 Amazon EC2 的 CodeDeploy 服务不收取额外服务费。",
                    substitution_notice=None,
                    usage_lines=[],
                    reference_rates=[],
                    applied_requirement_fields=list(requirement.requirements),
                )
        is_unknown_service = requirement.service not in SERVICE_TEMPLATE_FIELDS
        profile = None
        # A stable service-code alias is enough to query the official catalog,
        # but it is not a complete field contract.  Build/reuse the official
        # dimension profile for every service routed through this generic
        # adapter, including curated names.  Previously only unfamiliar names
        # received field bindings, so a well-known service could preserve fewer
        # customer fields than a newly discovered one.  Profile discovery is
        # still an enrichment rather than a hard dependency when the service
        # code itself is already known.
        try:
            service_code = self._service_code(requirement)
        except ManualConfirmationRequired:
            service_code = ""
        if self.auto_discovery is not None:
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
        unavailable_key = (service_code, region)
        if unavailable_key in self._unavailable_region_cache:
            raise ManualConfirmationRequired(
                (
                    f"{requirement.calculator_service_name or requirement.service} 在 {region} "
                    "没有官方区域计费目录，请调整该组件区域"
                    if region != "global"
                    else "AWS 官方目录在当前区域没有返回该产品的计费项"
                ),
                code=(
                    "service_region_not_supported"
                    if region != "global"
                    else "generic_semantic_rate_not_found"
                ),
                service_code=service_code,
                region=region,
                nearby_candidates=self._region_candidates(requirement, region),
            )
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
                applied_requirement_fields=list(requirement.requirements),
            )
        rates = self._profile_rates(profile) if is_unknown_service else []
        if not rates:
            rates = self._catalog_rates(service_code, region)
        rates = self._enrich_missing_instance_shapes(requirement, rates, region)
        self._require_cross_field_billing_mode(requirement)
        self._require_billing_variant_choice(requirement, profile)
        semantic_rates = self._semantic_rates(requirement, rates)
        confirmed_billing_variants = any(
            key.startswith("_billing_variant_") and bool(value)
            for key, value in requirement.requirements.items()
        )
        profile_bound_rates = (
            self._auto_semantic_rates(requirement, rates, profile=profile)
            if profile
            else []
        )
        if profile and confirmed_billing_variants:
            # Customer-confirmed catalog identities are authoritative. Build
            # those exact profile-bound lines first, then merge any dedicated
            # semantic lines (for example QuickSight SPICE) without allowing
            # the latter to replace a confirmed choice with a cheaper row.
            selected_rates = profile_bound_rates
            secondary_rates = semantic_rates
        else:
            # Dedicated service semantics remain the preferred interpretation,
            # but they are no longer allowed to hide another explicit customer
            # field that the official profile can bind.  The previous all-or-
            # nothing choice made (for example) a Cognito MAU reference line
            # prevent machine-token usage from ever reaching pricing.
            selected_rates = semantic_rates
            secondary_rates = [
                item for item in profile_bound_rates if item[1] is not None
            ]
        identity_positions = {
            (rate[1], rate[2], rate[3]): index
            for index, (_label, _amount, rate) in enumerate(selected_rates)
        }
        for item in secondary_rates:
            identity = (item[2][1], item[2][2], item[2][3])
            position = identity_positions.get(identity)
            if position is None:
                identity_positions[identity] = len(selected_rates)
                selected_rates.append(item)
                continue
            # A field-bound customer amount is stronger than a reference-only
            # semantic row for the exact same AWS dimension.
            if selected_rates[position][1] is None and item[1] is not None:
                selected_rates[position] = item
        auto_discovered = bool(profile_bound_rates) and is_unknown_service
        strict_semantic_services = {"emr", "redshift", "athena"}
        service_stem = _stem(requirement.service)

        # Known generic products already have complete official product rows in
        # the persistent local catalog. Try those rows before forcing a network
        # refresh. The previous order refreshed MemoryDB on every pricing
        # scenario even though the node catalog was already cached locally.
        if not selected_rates and service_stem not in strict_semantic_services:
            selected_rates = self._auto_semantic_rates(
                requirement,
                rates,
                profile=profile,
            )
            auto_discovered = bool(selected_rates)

        def has_instance_rate() -> bool:
            return any(
                bool(PricingCatalog.attributes(rate[4]).get("instanceType"))
                for _, _, rate in selected_rates
            )

        # An explicit MemoryDB node can never degrade into a snapshot-storage
        # reference row. If the cached regional catalog cannot find an exact or
        # same-capacity replacement, force one bounded refresh and then return a
        # precise component error rather than a plausible-looking wrong price.
        memorydb_requires_node = bool(
            service_stem == "memorydb"
            and requirement.requirements.get("requested_model")
        )
        if memorydb_requires_node and selected_rates and not has_instance_rate():
            selected_rates = []

        # A stale or incomplete catalog page must not stop the quote. Refresh
        # only this component and run the same deterministic field mapping
        # again; all other selected resources remain untouched.
        if not selected_rates:
            refreshed_rates = self._catalog_rates(
                service_code, region, refresh=True, max_pages=40
            )
            if refreshed_rates:
                rates = self._enrich_missing_instance_shapes(
                    requirement, refreshed_rates, region
                )
                selected_rates = self._semantic_rates(requirement, rates)
                if not selected_rates and service_stem not in strict_semantic_services:
                    selected_rates = self._auto_semantic_rates(
                        requirement,
                        rates,
                        profile=profile,
                    )
                    auto_discovered = bool(selected_rates)
                if memorydb_requires_node and selected_rates and not has_instance_rate():
                    selected_rates = []

        # Known products such as MemoryDB may use the generic adapter but
        # still have full regional product records (including Reserved terms)
        # in the live catalog. Derive from those records before consulting the
        # lightweight discovery profile, whose cached dimensions intentionally
        # omit commercial term payloads.
        if not selected_rates and service_stem not in strict_semantic_services:
            selected_rates = self._auto_semantic_rates(
                requirement,
                rates,
                profile=profile,
            )
            auto_discovered = bool(selected_rates)

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

        if not selected_rates and service_stem not in strict_semantic_services:
            selected_rates = self._auto_semantic_rates(
                requirement, rates, profile=profile
            )
            auto_discovered = bool(selected_rates)
        if not selected_rates:
            if memorydb_requires_node:
                raise ManualConfirmationRequired(
                    "当前区域没有客户指定的 MemoryDB 节点，也没有足够规格信息选择同配置替代节点",
                    code="memorydb_specification_not_found",
                    requested_model=requirement.requirements.get("requested_model"),
                    region=region,
                )
            if not rates:
                self._unavailable_region_cache.add(unavailable_key)
                if region != "global":
                    raise ManualConfirmationRequired(
                        f"{requirement.calculator_service_name or requirement.service} 在 {region} "
                        "没有官方区域计费目录，请调整该组件区域",
                        code="service_region_not_supported",
                        service_code=service_code,
                        region=region,
                        nearby_candidates=self._region_candidates(requirement, region),
                    )
            raise ManualConfirmationRequired(
                "AWS 官方目录没有返回可安全展示的新组件计费项",
                code="generic_semantic_rate_not_found",
                service_code=service_code,
            )

        # A customer-specified CPU/memory shape may only be priced from an
        # official product row that exposes the same comparable attributes.
        # Some regional catalogs contain unrelated service-fee rows under the
        # same ServiceCode (for example a managed-instances administration
        # fee).  Selecting one of those merely because it is the only hourly
        # row creates a plausible but false quote.
        requested_vcpu = requirement.requirements.get("vcpu")
        requested_memory = requirement.requirements.get("memory_gib")
        flink_capacity_was_converted = (
            service_code.casefold() == "amazonkinesisanalytics"
            and isinstance(requirement.requirements.get("kpu_count"), (int, float))
        )
        if (
            requested_vcpu is not None or requested_memory is not None
        ) and not flink_capacity_was_converted:
            def exposes_requested_shape(rate) -> bool:
                attrs = PricingCatalog.attributes(rate[2][4])
                if requested_vcpu is not None:
                    try:
                        if float(attrs.get("vcpu")) < float(requested_vcpu):
                            return False
                    except (TypeError, ValueError):
                        return False
                if requested_memory is not None:
                    memory_text = str(attrs.get("memory") or attrs.get("memoryGib") or "")
                    memory_match = re.search(r"\d+(?:\.\d+)?", memory_text)
                    if not memory_match or float(memory_match.group()) < float(requested_memory):
                        return False
                return True

            if not any(exposes_requested_shape(rate) for rate in selected_rates):
                # If the regional catalog contains real instance shapes, this
                # is a customer-resolvable conflict (for example a named
                # Neptune model plus different CPU/RAM), not a catalog outage.
                # The quote service will call ``configuration_candidates`` on
                # this same live/cached catalog and render those rows as a
                # dropdown.  Keep the technical error only for products whose
                # catalog truly exposes no comparable configuration matrix.
                has_regional_shape_catalog = any(
                    self._instance_rate_matches_requirement(requirement, rate)
                    and any(
                        value is not None
                        for value in self._official_instance_shape(rate[4])[1:]
                    )
                    for rate in rates
                )
                if has_regional_shape_catalog:
                    raise ManualConfirmationRequired(
                        "客户填写的型号与处理器或内存规格不一致，请从当前区域的 AWS 官方可售配置中选择",
                        code="generic_official_specification_not_found",
                        service_code=service_code,
                        region=region,
                        requested_model=requirement.requirements.get("requested_model"),
                        requested_vcpu=requested_vcpu,
                        requested_memory_gib=requested_memory,
                    )
                raise ManualConfirmationRequired(
                    "AWS 官方目录返回的计费项没有可核验的处理器和内存规格，系统不会用无关计费项猜价",
                    code="generic_official_shape_not_exposed",
                    service_code=service_code,
                    region=region,
                    requested_vcpu=requested_vcpu,
                    requested_memory_gib=requested_memory,
                )

        # When the official field is known but the customer did not provide a
        # usage amount, keep exactly one representative official dimension as
        # a reference rate. A unit price is not a monthly workload: submitting
        # one invented unit to BCM made Athena's "$5/TB" instruction become a
        # fake $5 monthly charge.
        if selected_rates and all(amount is None for _, amount, _ in selected_rates):
            positive = [item for item in selected_rates if item[2][0] > 0]
            pool = positive or selected_rates
            description, _, rate = min(
                pool,
                key=lambda item: (item[2][0], item[2][1], item[2][2], item[2][3]),
            )
            selected_rates = [(description, None, rate)]

        display_name = requirement.calculator_service_name or requirement.service
        usage_lines: list[UsageLine] = []
        reference_rates: list[ReferenceRate] = []
        reserved_compute_rate = None
        reserved_price = None
        if requirement.requirements.get("purchase_option") == "reserved":
            # Do not keep a service allow-list here.  A managed service supports
            # this exact Reserved offer only when its selected official product
            # exposes a matching Reserved term in the AWS Price List.  This
            # makes new services work automatically and prevents an on-demand
            # rate from being presented as a 1/3-year commitment price.
            for _, amount, candidate in selected_rates:
                if amount is None:
                    continue
                if not PricingCatalog.attributes(candidate[4]).get("instanceType"):
                    continue
                try:
                    candidate_reserved = PricingCatalog.reserved_price(
                        candidate[4],
                        years=int(
                            requirement.requirements.get("reserved_term_years") or 1
                        ),
                        payment_option=str(
                            requirement.requirements.get("payment_option")
                            or "no_upfront"
                        ),
                    )
                except ManualConfirmationRequired as exc:
                    if exc.code in {
                        "reserved_term_not_found",
                        "reserved_price_dimensions_missing",
                    }:
                        continue
                    raise
                reserved_compute_rate = candidate
                reserved_price = candidate_reserved
                break
        monthly_commitment_cost = 0.0
        upfront_commitment_cost = 0.0
        if reserved_compute_rate is not None and reserved_price is not None:
            reserved_nodes = float(
                requirement.requirements.get("node_count")
                or requirement.requirements.get("instance_count")
                or requirement.requirements.get("broker_count")
                or requirement.requirements.get("replication_instances")
                or 1
            )
            monthly_commitment_cost = (
                reserved_price.monthly_amortized
                * requirement.quantity
                * reserved_nodes
            )
            upfront_commitment_cost = (
                reserved_price.upfront * requirement.quantity * reserved_nodes
            )

        def source_fields_for_rate(
            candidate: tuple[float, str, str, str, dict[str, object]],
        ) -> list[str]:
            """Trace one selected official row back to customer fields."""

            _price, unit, usage_type, operation, product = candidate
            fields: set[str] = set()
            raw_bindings = (profile or {}).get("field_bindings")
            if isinstance(raw_bindings, list):
                for binding in raw_bindings:
                    if not isinstance(binding, dict):
                        continue
                    if (
                        str(binding.get("usage_type") or "") != usage_type
                        or str(binding.get("operation") or "") != operation
                        or str(binding.get("unit") or "") != unit
                    ):
                        continue
                    field = str(binding.get("field") or "")
                    if not field:
                        continue
                    derived_sources = {
                        "endpoint_hours": {"endpoint_count", "hours_per_month"},
                        "memory_store_gib_hours": {
                            "data_in_gib",
                            "write_records",
                            "memory_retention_hours",
                        },
                        "magnetic_store_gib_months": {
                            "data_in_gib",
                            "write_records",
                            "magnetic_retention_days",
                        },
                        "kpu_hours": {"kpu_count", "kpu_hours", "hours_per_month"},
                    }.get(field)
                    if derived_sources:
                        fields.update(derived_sources)
                    else:
                        fields.add(field)
            attrs = PricingCatalog.attributes(product)
            if attrs.get("instanceType"):
                fields.update(
                    {
                        "requested_model",
                        "vcpu",
                        "memory_gib",
                        "instance_count",
                        "node_count",
                        "broker_count",
                        "replication_instances",
                        "hours_per_month",
                        "quantity",
                    }
                )
            return sorted(fields)

        applied_fields: set[str] = set()
        for index, (description, amount, rate) in enumerate(selected_rates, start=1):
            price, unit, usage_type, operation, _ = rate
            if rate is reserved_compute_rate:
                continue
            if amount is not None and amount > 0:
                source_fields = source_fields_for_rate(rate)
                applied_fields.update(source_fields)
                usage_lines.append(
                    UsageLine(
                        key=f"gen{index}",
                        service_code=service_code,
                        usage_type=usage_type,
                        operation=operation,
                        amount=amount,
                        group=requirement.service,
                        source_fields=source_fields,
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
        has_billable_cost = has_usage or monthly_commitment_cost > 0 or upfront_commitment_cost > 0
        requested_model = str(requirement.requirements.get("requested_model") or "").strip()
        selected_model = requested_model
        selected_instance_model = ""
        selected_instance_product: dict[str, object] | None = None
        for _, _, selected_rate in selected_rates:
            attrs = PricingCatalog.attributes(selected_rate[4])
            if attrs.get("instanceType"):
                selected_instance_model = str(attrs["instanceType"])
                selected_instance_product = selected_rate[4]
                break
        if selected_instance_model and (not selected_model or service_stem == "memorydb"):
            selected_model = selected_instance_model
        if service_stem == "athena":
            selected_model = "按查询数据扫描量计费"
        elif service_stem == "emr" and not selected_model:
            selected_model = "Amazon EMR 托管集群"
        elif service_stem == "redshift" and not selected_model:
            selected_model = "Amazon Redshift 数据仓库"
        elif service_stem == "fsx" and not selected_model:
            fsx_type = str(
                requirement.requirements.get("file_system_type") or "FSx"
            ).strip()
            fsx_tier = requirement.requirements.get("throughput_mbps_per_tib")
            selected_model = (
                f"FSx for {fsx_type.title()} · {float(fsx_tier):g} MB/s/TiB"
                if fsx_tier is not None
                else f"FSx for {fsx_type.title()}"
            )

        architecture = "按客户明确用量核价" if has_billable_cost else "官方单位参考价"
        if reserved_compute_rate is not None:
            architecture = "AWS 官方预留价格"
        if service_stem == "athena":
            architecture = "无服务器查询，按扫描数据量计费"
        elif service_stem == "emr":
            architecture = "按主节点、核心节点和任务节点分别核价"
        elif service_stem == "redshift":
            architecture = "按计算节点与数据仓库存储分别核价"
        elif service_stem == "fsx":
            storage = requirement.requirements.get("storage_gib")
            architecture = (
                f"{float(storage):g} GiB 文件系统"
                if storage is not None
                else "AWS 官方文件系统计费维度"
            )
        substitution_notices: list[str] = []
        if (
            service_stem == "memorydb"
            and requested_model
            and selected_instance_model
            and requested_model.casefold() != selected_instance_model.casefold()
        ):
            substitution_notices.append(
                f"客户指定的 {requested_model} 在当前区域没有官方计费项，已保持不低于客户确认的"
                f"同配置处理器和内存，并自动改用其中价格最低的 {selected_instance_model}。"
            )
        if not has_billable_cost or reference_rates:
            substitution_notices.append(
                "未提供完整用量的部分仅展示对应官方单位价，不计入月费合计。"
            )
        specifications = dict(requirement.requirements)
        reader_billing_mode = str(
            requirement.requirements.get("_billing_variant_reader_billing_mode") or ""
        ).strip()
        if reader_billing_mode in {"per_user", "capacity"}:
            specifications["readerBillingMode"] = (
                "按读者人数付费"
                if reader_billing_mode == "per_user"
                else "按读者会话容量付费"
            )
        if selected_instance_product is not None:
            # Customer-request fields remain lower-case. Official catalog
            # facts use separate canonical keys consumed by global validation.
            specifications.update(self._candidate_specifications(selected_instance_product))
        return SelectedResource(
            service=requirement.service,
            display_name=display_name,
            region=region,
            model=selected_model or "AWS 官方计费维度",
            architecture=architecture,
            specifications=specifications,
            official_product={"source": "AWS Price List", "serviceCode": service_code},
            rationale=(
                "新组件已根据 AWS 官方产品属性和计费单位自动建立只读报价档案。"
                if auto_discovered
                else "按服务语义匹配 AWS 官方计费维度，不使用无关的最低价目录项。"
            ),
            substitution_notice=" ".join(substitution_notices) or None,
            usage_lines=usage_lines,
            reference_rates=reference_rates,
            applied_requirement_fields=sorted(
                applied_fields
                | (
                    {
                        "purchase_option",
                        "reserved_term_years",
                        "payment_option",
                        "requested_model",
                        "vcpu",
                        "memory_gib",
                        "instance_count",
                        "node_count",
                        "broker_count",
                        "replication_instances",
                        "hours_per_month",
                        "quantity",
                    }
                    if reserved_compute_rate is not None
                    else set()
                )
            ),
            monthly_commitment_cost=monthly_commitment_cost,
            upfront_commitment_cost=upfront_commitment_cost,
        )

    def supplement_selection(
        self,
        requirement: ServiceRequirement,
        selection: SelectedResource,
        missing_paths: list[str],
        default_region: str,
    ) -> SelectedResource:
        """Attach official usage rows omitted by a dedicated adapter.

        Dedicated adapters remain authoritative for product/instance choice.
        The generic official profile is allowed to contribute only rows whose
        trace explicitly names one of the still-unconsumed customer fields.
        This prevents both silent loss and the opposite failure mode—billing
        the same compute or storage row twice.
        """

        target_fields = {
            path.split(".", 1)[1] if path.startswith("requirements.") else path
            for path in missing_paths
        }
        if not target_fields:
            return selection

        supplemental_requirement = requirement.model_copy(deep=True)
        reverse_aliases: dict[str, str] = {}
        for field in sorted(target_fields):
            alias = _SUPPLEMENT_FIELD_ALIASES.get(field)
            if not alias or field not in supplemental_requirement.requirements:
                continue
            # Never overwrite another explicit customer value. A disagreement
            # between two real fields must stay visible to the ledger.
            if alias in supplemental_requirement.requirements:
                continue
            supplemental_requirement.requirements[alias] = (
                supplemental_requirement.requirements[field]
            )
            source_path = f"requirements.{field}"
            alias_path = f"requirements.{alias}"
            if source_path in supplemental_requirement.field_sources:
                supplemental_requirement.field_sources[alias_path] = (
                    supplemental_requirement.field_sources[source_path]
                )
            if source_path in supplemental_requirement.field_evidence:
                supplemental_requirement.field_evidence[alias_path] = (
                    supplemental_requirement.field_evidence[source_path]
                )
            if source_path in supplemental_requirement.field_scopes:
                supplemental_requirement.field_scopes[alias_path] = (
                    supplemental_requirement.field_scopes[source_path]
                )
            reverse_aliases[alias] = field

        supplemental = self.select(supplemental_requirement, default_region)
        normalized_targets = set(target_fields)

        def remap_source(field: str) -> str:
            prefix = "requirements." if field.startswith("requirements.") else ""
            raw = field.split(".", 1)[1] if prefix else field
            mapped = reverse_aliases.get(raw, raw)
            return f"{prefix}{mapped}" if prefix else mapped

        merged_lines = [line.model_copy(deep=True) for line in selection.usage_lines]
        applied = set(selection.applied_requirement_fields)
        for index, line in enumerate(supplemental.usage_lines, start=1):
            remapped_sources = [remap_source(field) for field in line.source_fields]
            base_sources = {
                field.split(".", 1)[1]
                if field.startswith("requirements.")
                else field
                for field in remapped_sources
            }
            matched = base_sources & normalized_targets
            if not matched:
                continue
            applied.update(matched)
            identity = (
                line.service_code,
                line.usage_type,
                line.operation,
                float(line.amount),
            )
            existing = next(
                (
                    item
                    for item in merged_lines
                    if (
                        item.service_code,
                        item.usage_type,
                        item.operation,
                        float(item.amount),
                    )
                    == identity
                ),
                None,
            )
            if existing is not None:
                existing.source_fields = sorted(
                    set(existing.source_fields) | set(remapped_sources)
                )
                continue
            merged_lines.append(
                line.model_copy(
                    update={
                        "key": f"supplement-{index}-{line.key}",
                        "source_fields": sorted(set(remapped_sources)),
                    }
                )
            )

        return selection.model_copy(
            update={
                "usage_lines": merged_lines,
                "applied_requirement_fields": sorted(applied),
            }
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
            exact_group: str | None = None,
            exact_usage_type: str | None = None,
        ) -> tuple[float, str, str, str, dict[str, object]] | None:
            candidates = []
            for item in rates:
                if exact_usage_type is not None and str(item[2]) != exact_usage_type:
                    continue
                product = item[4]
                attrs = PricingCatalog.attributes(product)
                if exact_group is not None and str(
                    attrs.get("group") or ""
                ).casefold() != exact_group.casefold():
                    continue
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
            billed_requests = (
                scoped_amount(requirement, "requests", float(requests))
                if requests
                else None
            )
            add(
                result,
                "Lambda 请求单价",
                billed_requests,
                exact_group="AWS-Lambda-Requests",
            )
            memory_mb = requested.get("memory_mb")
            duration_ms = requested.get("duration_ms")
            compute_amount = None
            if billed_requests and memory_mb and duration_ms:
                compute_amount = (
                    billed_requests * float(memory_mb) / 1024 * float(duration_ms) / 1000
                )
            architecture = str(requested.get("architecture") or "x86_64").casefold()
            add(
                result,
                "Lambda 计算 GB-Second 单价",
                compute_amount,
                exact_group=(
                    "AWS-Lambda-Duration-ARM"
                    if architecture in {"arm", "arm64", "aarch64"}
                    else "AWS-Lambda-Duration"
                ),
            )
        elif service == "efs":
            # EFS publishes Standard storage, IA/Archive storage, small-file
            # overhead, early-deletion penalties and throughput usage under
            # the same offer.  A generic "lowest GB-Mo" choice therefore
            # selected ArchiveEarlyDelete-SmallFiles ($0.01) for a Standard
            # Regional file system.  Bind the exact customer-facing storage
            # class first; only then is price used as a tie-breaker.
            storage = requested.get("storage_gib")
            storage_class = _canonical(
                str(requested.get("storage_class") or "standard")
            )
            deployment_type = _canonical(
                str(requested.get("deployment_type") or "regional")
            )
            one_zone = "onezone" in deployment_type or "onezone" in storage_class
            wants_archive = "archive" in storage_class
            wants_ia = any(
                marker in storage_class
                for marker in ("infrequentaccess", "standardia", "efsia")
            ) or storage_class == "ia"

            storage_candidates = []
            for rate in rates:
                if not any(
                    token in str(rate[1]).casefold()
                    for token in ("gb-mo", "gb-month", "gib-month")
                ):
                    continue
                attrs = PricingCatalog.attributes(rate[4])
                official_class = _canonical(str(attrs.get("storageClass") or ""))
                usage = _canonical(str(rate[2]))
                if any(marker in usage for marker in ("smallfiles", "earlydelete")):
                    continue
                if wants_archive:
                    compatible = official_class == "archive"
                elif wants_ia:
                    compatible = official_class == (
                        "onezoneinfrequentaccess" if one_zone else "infrequentaccess"
                    )
                else:
                    compatible = official_class == (
                        "onezonegeneralpurpose" if one_zone else "generalpurpose"
                    )
                if compatible:
                    storage_candidates.append(rate)
            if not storage_candidates:
                return []
            selected_storage = min(
                [rate for rate in storage_candidates if rate[0] > 0]
                or storage_candidates,
                key=lambda rate: (rate[0], rate[2], rate[3]),
            )
            result.append(
                (
                    "EFS 存储单价",
                    (
                        scoped_amount(requirement, "storage_gib", float(storage))
                        if storage not in (None, "")
                        else None
                    ),
                    selected_storage,
                )
            )

            throughput_mode = _canonical(
                str(requested.get("throughput_mode") or "elastic")
            )
            if throughput_mode == "elastic":
                for field, operation, label in (
                    ("data_out_gib", "read", "EFS 弹性吞吐读取单价"),
                    ("data_in_gib", "write", "EFS 弹性吞吐写入单价"),
                ):
                    amount = requested.get(field)
                    if amount in (None, ""):
                        continue
                    candidates = [
                        rate
                        for rate in rates
                        if "gb" in str(rate[1]).casefold()
                        and "etdataaccessbytes" in _canonical(str(rate[2]))
                        and str(rate[3]).casefold() == operation
                    ]
                    if not candidates:
                        return []
                    selected = min(
                        [rate for rate in candidates if rate[0] > 0] or candidates,
                        key=lambda rate: (rate[0], rate[2], rate[3]),
                    )
                    result.append(
                        (
                            label,
                            scoped_amount(requirement, field, float(amount)),
                            selected,
                        )
                    )
            elif throughput_mode == "provisioned":
                provisioned = requested.get("provisioned_throughput_mibps")
                if provisioned not in (None, ""):
                    add(
                        result,
                        "EFS 预置吞吐量单价",
                        float(provisioned),
                        include=("provisionedtp",),
                        unit_contains=("mibps-mo", "mbps-mo"),
                    )
            return result
        elif service == "fsx":
            # FSx for Lustre publishes the selected MB/s/TiB tier on the
            # storage product itself (for example Storage.SSD.250). It is a
            # product-selection constraint, not a second arbitrary throughput
            # usage line. Preserve the customer tier and price the exact
            # official row instead of choosing the cheapest GB-Mo dimension.
            file_system_type = str(
                requested.get("file_system_type") or ""
            ).strip().casefold()
            storage = requested.get("storage_gib")
            throughput_tier = requested.get("throughput_mbps_per_tib")
            # OpenZFS meters provisioned SSD storage, provisioned throughput,
            # and backup storage independently. A plain "cheapest GB-Mo"
            # search can accidentally select the tiny Intelligent-Tiering
            # monitoring fee and produce a plausible-looking wrong total.
            if file_system_type == "openzfs":
                requested_deployment = _canonical(
                    str(
                        requested.get("deployment_type")
                        or requested.get("deployment_option")
                        or ""
                    )
                )

                def openzfs_rates(*, unit_tokens: tuple[str, ...]) -> list:
                    matched = []
                    for rate in rates:
                        attrs = PricingCatalog.attributes(rate[4])
                        if str(attrs.get("fileSystemType") or "").casefold() != "openzfs":
                            continue
                        operation = str(attrs.get("operation") or rate[3] or "").casefold()
                        if "openzfs" not in operation:
                            continue
                        unit = str(rate[1]).casefold()
                        if not any(token in unit for token in unit_tokens):
                            continue
                        deployment = _canonical(str(attrs.get("deploymentOption") or ""))
                        if requested_deployment and deployment != requested_deployment:
                            continue
                        matched.append(rate)
                    return matched

                storage_candidates = []
                for rate in openzfs_rates(unit_tokens=("gb-mo", "gb-month", "gib-month")):
                    attrs = PricingCatalog.attributes(rate[4])
                    text = " ".join(
                        str(value)
                        for value in (
                            rate[2], rate[3], attrs.get("storageTier"),
                            attrs.get("storageType"), attrs.get("cacheType"),
                        )
                        if value
                    ).casefold()
                    if any(
                        token in text
                        for token in (
                            "backup", "snapshot", "monitoring", "frequent access",
                            "infrequent access", "archive", "ssd cache", "int_",
                        )
                    ):
                        continue
                    if str(attrs.get("storageType") or "").casefold() != "ssd":
                        continue
                    storage_candidates.append(rate)
                if not storage_candidates:
                    return []
                selected_storage = min(
                    [rate for rate in storage_candidates if rate[0] > 0]
                    or storage_candidates,
                    key=lambda rate: (rate[0], rate[2], rate[3]),
                )
                selected_deployment = _canonical(
                    str(
                        PricingCatalog.attributes(selected_storage[4]).get(
                            "deploymentOption"
                        )
                        or ""
                    )
                )
                result.append(
                    (
                        "FSx for OpenZFS SSD 存储单价",
                        (
                            scoped_amount(requirement, "storage_gib", float(storage))
                            if storage
                            else None
                        ),
                        selected_storage,
                    )
                )

                throughput = requested.get("throughput_mbps")
                if throughput not in (None, ""):
                    throughput_candidates = [
                        rate
                        for rate in openzfs_rates(
                            unit_tokens=("mibps-mo", "mbps-mo", "mb/s-month")
                        )
                        if _canonical(
                            str(
                                PricingCatalog.attributes(rate[4]).get(
                                    "deploymentOption"
                                )
                                or ""
                            )
                        )
                        == selected_deployment
                    ]
                    if not throughput_candidates:
                        return []
                    selected_throughput = min(
                        [rate for rate in throughput_candidates if rate[0] > 0]
                        or throughput_candidates,
                        key=lambda rate: (rate[0], rate[2], rate[3]),
                    )
                    result.append(
                        (
                            "FSx for OpenZFS 预置吞吐量单价",
                            scoped_amount(
                                requirement, "throughput_mbps", float(throughput)
                            ),
                            selected_throughput,
                        )
                    )

                backup_storage = requested.get("backup_storage_gib")
                if backup_storage not in (None, ""):
                    backup_candidates = []
                    for rate in openzfs_rates(
                        unit_tokens=("gb-mo", "gb-month", "gib-month")
                    ):
                        attrs = PricingCatalog.attributes(rate[4])
                        identity = " ".join(
                            str(value)
                            for value in (rate[2], rate[3], attrs.get("storageTier"))
                            if value
                        ).casefold()
                        if "backup" not in identity:
                            continue
                        deployment = _canonical(str(attrs.get("deploymentOption") or ""))
                        if deployment not in {"", "na", selected_deployment}:
                            continue
                        backup_candidates.append(rate)
                    if not backup_candidates:
                        return []
                    selected_backup = min(
                        [rate for rate in backup_candidates if rate[0] > 0]
                        or backup_candidates,
                        key=lambda rate: (rate[0], rate[2], rate[3]),
                    )
                    result.append(
                        (
                            "FSx for OpenZFS 备份存储单价",
                            scoped_amount(
                                requirement,
                                "backup_storage_gib",
                                float(backup_storage),
                            ),
                            selected_backup,
                        )
                    )
                return result

            candidates = []
            for rate in rates:
                attrs = PricingCatalog.attributes(rate[4])
                text = " ".join(
                    str(value)
                    for value in (
                        rate[1], rate[2], rate[3], *attrs.values()
                    )
                    if value
                ).casefold()
                if not any(
                    token in str(rate[1]).casefold()
                    for token in ("gb-mo", "gb-month", "gib-month")
                ):
                    continue
                if any(token in text for token in ("backup", "snapshot")):
                    continue
                official_type = str(attrs.get("fileSystemType") or "").casefold()
                if file_system_type and official_type != file_system_type:
                    continue
                if throughput_tier is not None:
                    official_tier = str(attrs.get("throughputCapacity") or "")
                    tier_match = re.search(r"\d+(?:\.\d+)?", official_tier)
                    if not tier_match or abs(
                        float(tier_match.group()) - float(throughput_tier)
                    ) > 1e-9:
                        continue
                candidates.append(rate)
            if not candidates:
                return []
            positive = [rate for rate in candidates if rate[0] > 0] or candidates
            selected = min(positive, key=lambda rate: (rate[0], rate[2], rate[3]))
            result.append(
                (
                    "FSx 官方存储与吞吐档位单价",
                    (
                        scoped_amount(requirement, "storage_gib", float(storage))
                        if storage
                        else None
                    ),
                    selected,
                )
            )
        elif service == "kinesis":
            # A provisioned Kinesis stream is billed by shard-hour.  Treat an
            # explicit shard count as workload evidence instead of falling
            # back to a one-unit reference price (which previously produced a
            # zero-dollar quote row).
            capacity_mode = _canonical(
                str(requested.get("capacity_mode") or "provisioned")
            )
            shards = requested.get("shards") or requested.get("shard_count")
            if shards and capacity_mode not in {
                "ondemand",
                "ondemandstandard",
                "ondemandadvantage",
            }:
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

            # Provisioned streams also charge for PUT payload units.  The
            # customer commonly supplies a monthly data volume instead of a
            # low-level 25-KB unit count.  Under the product-wide lowest-cost
            # rule, convert that volume to the minimum possible number of
            # payload units (full 25-KB chunks) rather than dropping the
            # charge or asking a highly technical record-size question.
            put_payload_units = requested.get("put_payload_units")
            put_source_field = "put_payload_units"
            if put_payload_units in (None, ""):
                data_in_gib = requested.get("data_in_gib")
                if data_in_gib not in (None, ""):
                    billed_gib = scoped_amount(
                        requirement,
                        "data_in_gib",
                        float(data_in_gib),
                    )
                    put_payload_units = math.ceil(
                        billed_gib * 1024**3 / 25_000
                    )
                    put_source_field = "data_in_gib"
            if put_payload_units in (None, ""):
                requests = requested.get("requests") or requested.get("request_count")
                if requests not in (None, ""):
                    put_payload_units = scoped_amount(
                        requirement,
                        "requests",
                        float(requests),
                    )
                    put_source_field = "requests"

            if (
                put_payload_units not in (None, "")
                and capacity_mode not in {
                    "ondemand",
                    "ondemandstandard",
                    "ondemandadvantage",
                }
            ):
                add(
                    result,
                    (
                        "Kinesis 写入数据最低 PUT Payload Unit 费用"
                        if put_source_field == "data_in_gib"
                        else "Kinesis 写入负载单价"
                    ),
                    float(put_payload_units),
                    include_any=("putrequestpayloadunits", "putrequest"),
                    exclude=("enhanced",),
                    unit_contains=("putrequest", "request"),
                )

            if capacity_mode in {"ondemand", "ondemandstandard", "ondemandadvantage"}:
                data_in_gib = requested.get("data_in_gib")
                if data_in_gib not in (None, ""):
                    add(
                        result,
                        "Kinesis 按需写入数据单价",
                        scoped_amount(
                            requirement, "data_in_gib", float(data_in_gib)
                        ),
                        include=("ondemand",),
                        include_any=("incomingbytes", "ingest"),
                        exclude=("advantagecommitment", "extended", "enhanced"),
                        unit_contains=("gb", "gib"),
                    )
                data_out_gib = requested.get("data_out_gib")
                if data_out_gib not in (None, ""):
                    add(
                        result,
                        "Kinesis 按需读取数据单价",
                        scoped_amount(
                            requirement, "data_out_gib", float(data_out_gib)
                        ),
                        include=("ondemand",),
                        include_any=("outgoingbytes", "retrieval"),
                        exclude=("advantagecommitment", "extended", "enhanced"),
                        unit_contains=("gb", "gib"),
                    )
        elif service == "stepfunctions":
            # Step Functions exposes three unrelated dimensions under the
            # historical AmazonStates offer.  Bind the customer's workload
            # type and field to the exact AWS group so Standard transitions
            # can never be mistaken for Express requests or duration.
            workflow_type = _canonical(
                str(requested.get("workflow_type") or "standard")
            )
            if workflow_type in {"standard", "standardworkflow", "standardworkflows"}:
                transitions = requested.get("state_transitions")
                add(
                    result,
                    "Step Functions Standard 状态转换单价",
                    (
                        scoped_amount(
                            requirement,
                            "state_transitions",
                            float(transitions),
                        )
                        if transitions
                        else None
                    ),
                    exact_group="SFN-StateTransitions",
                )
            elif workflow_type in {"express", "expressworkflow", "expressworkflows"}:
                requests = requested.get("requests") or requested.get("request_count")
                duration = requested.get("duration_gb_seconds")
                add(
                    result,
                    "Step Functions Express 工作流请求单价",
                    (
                        scoped_amount(requirement, "requests", float(requests))
                        if requests
                        else None
                    ),
                    exact_group="SFN-ExpressWorkflows-Requests",
                )
                add(
                    result,
                    "Step Functions Express 执行时长单价",
                    (
                        scoped_amount(
                            requirement,
                            "duration_gb_seconds",
                            float(duration),
                        )
                        if duration
                        else None
                    ),
                    exact_group="SFN-ExpressWorkflows-Duration",
                )
            else:
                # Unknown workflow types are pricing-significant.  Returning
                # no semantic match keeps the component out of the total until
                # the existing confirmation flow obtains a real choice.
                return []
        elif service == "appconfig":
            # AppConfig is another product whose marketing name differs from
            # the owning Price List offer.  Restrict all matches to AppConfig
            # UsageTypes so no unrelated Systems Manager dimension can leak
            # into the quote.
            configuration_requests = requested.get("configuration_requests")
            configurations_received = requested.get("configuration_retrievals")
            experiment_hours = requested.get("experiment_hours")
            add(
                result,
                "AWS AppConfig 配置请求单价",
                (
                    scoped_amount(
                        requirement,
                        "configuration_requests",
                        float(configuration_requests),
                    )
                    if configuration_requests
                    else None
                ),
                include=("appconfig-requests",),
            )
            add(
                result,
                "AWS AppConfig 配置接收单价",
                (
                    scoped_amount(
                        requirement,
                        "configuration_retrievals",
                        float(configurations_received),
                    )
                    if configurations_received
                    else None
                ),
                include=("appconfig-deployments",),
            )
            add(
                result,
                "AWS AppConfig 功能标志实验小时价",
                (
                    scoped_amount(
                        requirement,
                        "experiment_hours",
                        float(experiment_hours),
                    )
                    if experiment_hours
                    else None
                ),
                include=("appconfig-experimenthours",),
            )
        elif service == "eventbridge":
            # EventBridge event buses, schema discovery and Pipes share the
            # AWSEvents offer but use different chunk sizes and operations.
            # Bind each customer field to its exact operation; a generic
            # "event" match could otherwise pick the global free schema row or
            # charge a Pipe request as a custom event.
            events = requested.get("events")
            schema_events = requested.get("schema_discovery_events")
            pipe_requests = requested.get("pipes_requests")
            add(
                result,
                "EventBridge 自定义事件单价",
                (
                    scoped_amount(requirement, "events", float(events))
                    if events
                    else None
                ),
                include=("putevents",),
            )
            add(
                result,
                "EventBridge Schema Discovery 事件单价",
                (
                    scoped_amount(
                        requirement,
                        "schema_discovery_events",
                        float(schema_events),
                    )
                    if schema_events
                    else None
                ),
                include=("discoveryevent",),
            )
            add(
                result,
                "EventBridge Pipes 请求单价",
                (
                    scoped_amount(
                        requirement,
                        "pipes_requests",
                        float(pipe_requests),
                    )
                    if pipe_requests
                    else None
                ),
                include=("piperequest",),
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
            instance_count = float(requested.get("instance_count") or 1)
            compute_amount = (
                requirement.quantity * instance_count * requirement.hours_per_month
            )
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
            if model and not re.fullmatch(
                r"(?:dms\.)?[a-z][a-z0-9-]*\."
                r"(?:micro|small|medium|large|xlarge|\d+xlarge)",
                model,
                re.I,
            ):
                model = ""
            if model.startswith("dms."):
                model = model[4:]
            replication_instances = float(
                requested.get("replication_instances") or requirement.quantity
            )
            add(
                result,
                f"AWS DMS {model or '复制实例'} 小时价",
                replication_instances * requirement.hours_per_month,
                include_any=("instanceusg", "createdmsinstance"),
                exclude=("multi-az", "serverless"),
                model=model or None,
                min_vcpu=float(requested["vcpu"]) if requested.get("vcpu") else None,
                min_memory_gib=(
                    float(requested["memory_gib"])
                    if requested.get("memory_gib")
                    else None
                ),
            )
            storage = requested.get("storage_gib")
            if storage not in (None, ""):
                add(
                    result,
                    "AWS DMS 额外日志存储单价",
                    scoped_amount(requirement, "storage_gib", float(storage)),
                    include=("storage",),
                    exclude=("snapshot", "backup", "s3"),
                    unit_contains=("gb",),
                )
        elif service == "quicksight":
            edition = str(requested.get("edition") or "enterprise").casefold()
            reader_billing_mode = str(
                requested.get("_billing_variant_reader_billing_mode") or ""
            ).strip()
            common_exclude = ("free-trial", "free trial", "pro", "-q", "annual")
            author_users = requested.get("author_users")
            reader_users = (
                requested.get("reader_users")
                if reader_billing_mode != "capacity"
                else None
            )
            users = requested.get("users")
            author_usage_type = str(
                requested.get("_billing_variant_author_users") or ""
            ).strip()
            reader_usage_type = str(
                requested.get("_billing_variant_reader_users") or ""
            ).strip()
            session_usage_type = str(
                requested.get("_billing_variant_session_capacity") or ""
            ).strip()
            if author_users:
                if author_usage_type:
                    add(
                        result,
                        "QuickSight 作者用户月费",
                        float(author_users),
                        exact_usage_type=author_usage_type,
                    )
                else:
                    add(
                        result,
                        "QuickSight 作者用户月费",
                        float(author_users),
                        include=("user subscription", edition, "month"),
                        exclude=common_exclude + ("reader",),
                        unit_contains=("user",),
                    )
            if reader_users:
                if reader_usage_type:
                    add(
                        result,
                        "QuickSight 读者用户月费",
                        float(reader_users),
                        exact_usage_type=reader_usage_type,
                    )
                else:
                    add(
                        result,
                        "QuickSight 读者用户月费",
                        float(reader_users),
                        include=("reader", edition),
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
            sessions = (
                requested.get("session_capacity")
                if reader_billing_mode != "per_user"
                else None
            )
            if sessions:
                if session_usage_type:
                    add(
                        result,
                        "QuickSight 读者会话用量",
                        float(sessions),
                        exact_usage_type=session_usage_type,
                    )
                else:
                    add(
                        result,
                        "QuickSight 读者会话用量",
                        float(sessions),
                        include=("reader", "session"),
                        exclude=("free", "bonus", "-q"),
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
    def _derive_flink_kpu_count(requirement: ServiceRequirement) -> None:
        """Translate customer node capacity into Managed Flink KPUs.

        Managed Service for Apache Flink is billed in KPUs rather than EC2-like
        node shapes. One KPU represents one vCPU and 4 GiB of memory, so an
        explicit node count plus CPU/RAM can be converted without guessing a
        product or dropping the customer's capacity.
        """

        identities = {
            _stem(requirement.service),
            _stem(requirement.product_identity or ""),
            _stem(requirement.calculator_service_name or ""),
        }
        if not identities.intersection(
            {"kinesisanalytics", "managedserviceforapacheflink"}
        ):
            return
        requested = requirement.requirements
        if isinstance(requested.get("kpu_count"), (int, float)):
            return
        vcpu = requested.get("vcpu")
        memory_gib = requested.get("memory_gib")
        if not isinstance(vcpu, (int, float)) and not isinstance(
            memory_gib, (int, float)
        ):
            return
        per_node_kpus = max(
            float(vcpu) if isinstance(vcpu, (int, float)) else 0.0,
            (
                float(memory_gib) / 4.0
                if isinstance(memory_gib, (int, float))
                else 0.0
            ),
        )
        node_count = requested.get("node_count") or requested.get("instance_count") or 1
        if not isinstance(node_count, (int, float)) or float(node_count) <= 0:
            node_count = 1
        requested["kpu_count"] = max(1, math.ceil(per_node_kpus * float(node_count)))
        requirement.field_sources["requirements.kpu_count"] = "system_derived"

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

        GenericOfficialPlugin._derive_flink_kpu_count(requirement)
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
                    rate[4].get("officialDimensionDescription"),
                )
                if value
            ).casefold()
            return attrs, text

        def safe(rate) -> bool:
            attrs, text = details(rate)
            product_variant = str(requested.get("product_variant") or "").casefold()
            if product_variant == "live_analytics" and (
                "influx" in text or attrs.get("instanceType")
            ):
                return False
            if product_variant == "influxdb" and "influx" not in text:
                return False
            return not any(
                token in text
                for token in (
                    "credit", "refund", "discount", "tax", "support",
                    "professional service",
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
        requested_engine = str(
            requested.get("engine") or requested.get("engine_type") or ""
        ).strip().casefold()

        def hourly_instance(rate, *, enforce_model: bool = True) -> bool:
            attrs, text = details(rate)
            unit = str(rate[1]).casefold()
            instance = str(attrs.get("instanceType") or "").casefold()
            if not instance or not any(token in unit for token in ("hrs", "hour")):
                return False
            if (
                enforce_model
                and model
                and model not in {instance, f"db.{instance}", f"cache.{instance}"}
            ):
                return False
            if _stem(requirement.service) == "memorydb" and requested_engine:
                if requested_engine == "redis" and "valkey" in text:
                    return False
                if requested_engine == "valkey" and "valkey" not in text:
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
            instance_count = float(
                requested.get("instance_count")
                or requested.get("node_count")
                or (
                    float(requested.get("shards") or 1)
                    * (1 + float(requested.get("replicas_per_shard") or 0))
                    if _stem(requirement.service) == "memorydb"
                    else 1
                )
            )
            choose(
                "AWS 官方最低匹配实例小时价",
                requirement.quantity * instance_count * requirement.hours_per_month,
                hourly_instance,
            )
        elif _stem(requirement.service) == "memorydb" and model and (
            min_vcpu is not None or min_memory is not None
        ):
            # The requested family may not be sold in this region. MemoryDB
            # node families are interchangeable for pricing purposes when the
            # replacement preserves the confirmed CPU and memory floors. Rank
            # every valid replacement by its real official hourly rate.
            choose(
                "AWS 官方同配置最低价实例小时价",
                requirement.quantity
                * float(
                    requested.get("node_count")
                    or float(requested.get("shards") or 1)
                    * (1 + float(requested.get("replicas_per_shard") or 0))
                )
                * requirement.hours_per_month,
                lambda rate: hourly_instance(rate, enforce_model=False),
            )

        # For a first-use service, prefer the persisted binding between the
        # customer field and AWS's exact UsageType / Operation / Unit.  This
        # prevents a value such as storage or traffic from being attached to a
        # different, cheaper dimension that merely happens to use GB.
        profile_bound_fields: set[str] = set()
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
                reader_billing_mode = str(
                    requested.get("_billing_variant_reader_billing_mode") or ""
                ).strip()
                if (
                    reader_billing_mode == "per_user"
                    and field == "session_capacity"
                ) or (
                    reader_billing_mode == "capacity"
                    and field == "reader_users"
                ):
                    continue
                if field == "endpoint_hours":
                    endpoint_count = requested.get("endpoint_count")
                    value = (
                        float(endpoint_count) * requirement.hours_per_month
                        if isinstance(endpoint_count, (int, float))
                        and not isinstance(endpoint_count, bool)
                        and endpoint_count > 0
                        else None
                    )
                elif field == "memory_store_gib_hours":
                    retention_hours = requested.get("memory_retention_hours")
                    monthly_ingest_gib = requested.get("data_in_gib")
                    if monthly_ingest_gib in (None, "") and requested.get("write_records"):
                        # Lowest official billable write size is 1 KiB.  The
                        # customer record count remains visible separately;
                        # this derived amount is only the lowest-price estimate
                        # requested when record size was omitted.
                        monthly_ingest_gib = float(requested["write_records"]) / 1_048_576
                    value = (
                        float(monthly_ingest_gib) * float(retention_hours)
                        if monthly_ingest_gib not in (None, "")
                        and retention_hours not in (None, "")
                        else None
                    )
                elif field == "magnetic_store_gib_months":
                    retention_days = requested.get("magnetic_retention_days")
                    monthly_ingest_gib = requested.get("data_in_gib")
                    if monthly_ingest_gib in (None, "") and requested.get("write_records"):
                        monthly_ingest_gib = float(requested["write_records"]) / 1_048_576
                    value = (
                        max(100.0, float(monthly_ingest_gib) * float(retention_days) / 30.0)
                        if monthly_ingest_gib not in (None, "")
                        and retention_days not in (None, "")
                        else None
                    )
                elif field == "data_in_gib" and requested.get("write_records"):
                    value = requested.get("data_in_gib")
                    if value in (None, ""):
                        value = float(requested["write_records"]) / 1_048_576
                elif field == "kpu_hours":
                    value = requested.get("kpu_hours")
                    kpu_count = requested.get("kpu_count")
                    if value in (None, "") and isinstance(kpu_count, (int, float)):
                        # Managed Service for Apache Flink bills the configured
                        # KPUs plus one application-management KPU per running
                        # application.  ``quantity`` is the application count.
                        value = (
                            float(kpu_count) + float(requirement.quantity)
                        ) * float(requirement.hours_per_month)
                elif field == "storage_gib":
                    value = requested.get("storage_gib")
                    kpu_count = requested.get("kpu_count")
                    is_flink_storage = any(
                        "runningapplicationstorage" in str(binding.get("usage_type") or "").casefold()
                        and "interactive" not in str(binding.get("usage_type") or "").casefold()
                        for binding in bindings
                    ) and "flink" in (
                        f"{requirement.calculator_service_name or ''} "
                        f"{requirement.source_text or ''}"
                    ).casefold()
                    if (
                        value in (None, "")
                        and is_flink_storage
                        and isinstance(kpu_count, (int, float))
                    ):
                        # AWS allocates 50 GiB of running application storage
                        # per configured KPU.  This is a billed dimension, not
                        # an invented customer disk request.
                        value = (
                            float(kpu_count)
                            * 50.0
                            * float(requirement.quantity)
                        )
                elif field == "hours_per_month":
                    hours_are_explicit = bool(
                        requirement.field_evidence.get("hours_per_month")
                        or re.search(
                            r"\d+(?:\.\d+)?\s*(?:小时|hours?|hrs?)",
                            requirement.source_text or "",
                            re.I,
                        )
                    )
                    value = requirement.hours_per_month if hours_are_explicit else None
                else:
                    value = requested.get(field)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                    continue
                selected_usage_type = str(
                    requested.get(f"_billing_variant_{field}") or ""
                ).strip()
                if not selected_usage_type and "flink" in (
                    f"{requirement.calculator_service_name or ''} "
                    f"{requirement.source_text or ''}"
                ).casefold():
                    selected_usage_type = next(
                        (
                            str(binding.get("usage_type") or "")
                            for binding in bindings
                            if (
                                field == "kpu_hours"
                                and "kpu-hour-java" in str(binding.get("usage_type") or "").casefold()
                            )
                            or (
                                field == "storage_gib"
                                and "runningapplicationstorage" in str(binding.get("usage_type") or "").casefold()
                                and "interactive" not in str(binding.get("usage_type") or "").casefold()
                            )
                        ),
                        "",
                    )

                def bound_rate(rate, *, candidates=bindings) -> bool:
                    if selected_usage_type and str(rate[2]) != selected_usage_type:
                        return False
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
                elif field in {"bucket_count", "object_count"} and any(
                    "day" in str(binding.get("unit") or "").casefold()
                    for binding in bindings
                ):
                    # AWS publishes these inventory dimensions per day while
                    # customers naturally provide a current bucket/object
                    # count. A monthly quote therefore uses the standard
                    # 30-day catalog month, just as hourly services use 730h.
                    amount *= 30
                label = next(
                    (
                        str(binding.get("label"))
                        for binding in bindings
                        if binding.get("label")
                    ),
                    field,
                )
                result_count = len(result)
                choose(f"AWS 官方{label}单价", amount, bound_rate)
                if len(result) > result_count:
                    # A profile binding is authoritative.  The broad fallback
                    # below must not bill the same customer quantity again
                    # against a second dimension that happens to share a unit.
                    profile_bound_fields.add(field)

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
                "backup_storage_gib",
                "AWS 官方备份存储单价",
                lambda rate: any(
                    token in str(rate[1]).casefold()
                    for token in ("gb-mo", "gb-month", "gib-month")
                )
                and any(
                    token in details(rate)[1]
                    for token in ("backup", "warm storage")
                ),
            ),
            (
                "provisioned_throughput_mibps",
                "AWS 官方预置吞吐量单价",
                lambda rate: any(
                    token in str(rate[1]).casefold()
                    for token in ("mibps", "mbps", "mb/s")
                )
                or all(
                    token in details(rate)[1]
                    for token in ("provisioned", "throughput")
                ),
            ),
            (
                "cross_region_copy_gib",
                "AWS 官方跨区域复制单价",
                lambda rate: str(rate[1]).casefold()
                in {"gb", "gbyte", "gigabyte", "gib"}
                and any(
                    token in details(rate)[1]
                    for token in ("cross-region", "cross region", "transfer", "copy")
                ),
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
                "outbound_messages",
                "AWS 官方出站消息单价",
                lambda rate: any(
                    token in str(rate[1]).casefold()
                    for token in ("message", "email", "request")
                )
                or any(
                    token in details(rate)[1]
                    for token in ("email", "message", "outbound")
                ),
            ),
            (
                "data_in_gib",
                "AWS 官方数据摄入单价",
                lambda rate: (
                    str(rate[1]).casefold() in {"gb", "gbyte", "gigabyte"}
                    or "byte" in str(rate[1]).casefold()
                )
                and any(token in details(rate)[1] for token in ("ingest", "incoming data")),
            ),
            (
                "data_processed_gib",
                "AWS 官方数据处理单价",
                lambda rate: (
                    str(rate[1]).casefold() in {"gb", "gbyte", "gigabyte"}
                    or "byte" in str(rate[1]).casefold()
                )
                and any(token in details(rate)[1] for token in ("process", "scan", "ingest")),
            ),
            (
                "data_scanned_gib",
                "AWS 官方数据扫描单价",
                lambda rate: (
                    str(rate[1]).casefold() in {"gb", "gbyte", "gigabyte"}
                    or "byte" in str(rate[1]).casefold()
                )
                and any(
                    token in details(rate)[1]
                    for token in ("scan", "discovery", "classif")
                ),
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
            if field in profile_bound_fields:
                continue
            value = requested.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                choose(description, float(value), predicate)

        if result:
            return result

        # With no customer usage, select one real, smallest official billing
        # dimension for display only. ``None`` keeps it out of the monthly
        # estimate while still exposing the exact AWS unit price.
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
            result.append((f"AWS 官方最小 {rate[1]} 计费单位", None, rate))
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
