from __future__ import annotations

import math
import re

from app.core.errors import ManualConfirmationRequired
from app.domain.customer_facts import customer_field_is_explicit, scoped_amount
from app.domain.pricing_contracts import declared_shared_offer_usages
from app.domain.models import (
    CandidateOption,
    PreviewSelection,
    ReferenceRate,
    SelectedResource,
    ServiceRequirement,
    UsageLine,
)
from app.domain.service_billing_policies import (
    SERVICE_BILLING_POLICY_VERSION,
    no_additional_charge_decision,
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
    "events": "AWSEvents",
    "config": "AWSConfig",
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
    "kinesisfirehose": "AmazonKinesisFirehose",
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
    "guardduty": "AmazonGuardDuty",
    "textract": "AmazonTextract",
    "comprehend": "comprehend",
    "rekognition": "AmazonRekognition",
    "transcribe": "transcribe",
    "translate": "translate",
    "polly": "AmazonPolly",
    "transitgateway": "AmazonVPC",
    "directconnect": "AWSDirectConnect",
    "sitetositevpn": "AmazonVPC",
    "vpcendpoint": "AmazonVPC",
    "s3glacierdeeparchive": "AmazonS3GlacierDeepArchive",
    "storagegateway": "AWSStorageGateway",
    "datasync": "AWSDataSync",
    "transfer": "AWSTransfer",
    "transferfamily": "AWSTransfer",
    "appstream": "AmazonAppStream",
    "workmail": "AmazonWorkMail",
    "codebuild": "CodeBuild",
    "codepipeline": "AWSCodePipeline",
    "codeartifact": "AWSCodeArtifact",
    "codedeploy": "AWSCodeDeploy",
    "cloudformation": "AWSCloudFormation",
    "inspectorv2": "AmazonInspectorV2",
    "macie": "AmazonMacie",
    "securityhub": "AWSSecurityHub",
    "auditmanager": "auditmanager",
    "iot": "AWSIoT",
    "iotdevicemanagement": "IoTDeviceManagement",
    "iotdevicedefender": "IoTDeviceDefender",
    "kinesisvideo": "AmazonKinesisVideo",
    "ivs": "AmazonIVS",
    "elementalmediaconvert": "AWSElementalMediaConvert",
    "elementalmedialive": "AWSElementalMediaLive",
    "elementalmediapackage": "AWSElementalMediaPackage",
    "mediaconnect": "AWSMediaConnect",
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

# Some customer facts belong to the workload component, but AWS publishes the
# corresponding price in a shared offer rather than in that component's offer.
# For example, API Gateway request charges live in ``AmazonApiGateway`` while
# ordinary public egress is billed from ``AWSDataTransfer``.  Treat these as
# cross-offer dimensions so every dedicated and auto-discovered adapter gets
# the same safe fallback instead of teaching each product about every shared
# AWS charge separately.
_SHARED_OFFER_DIMENSIONS: dict[str, str] = {
    "data_transfer_out_gib": "AWSDataTransfer",
}

# These values select or document an AWS configuration but are not a second
# billable amount once the component already supplies the metered usage.  Keep
# this contract beside the generic adapter so the fact ledger can distinguish
# "not charged separately" from "forgotten" without asking AI every time.
_CONFIGURATION_CONTEXT_FIELDS: dict[str, frozenset[str]] = {
    "backup": frozenset(
        {
            "backup_frequency",
            "backup_retention_days",
            "protected_service",
        }
    ),
    # DMS charges for provisioned replication capacity. Multiple migration
    # tasks may share those instances, so the task count describes scheduling
    # and topology but must not multiply or create another AWS meter.
    "dms": frozenset({"task_count"}),
    # X-Ray's official TracesStored meter charges traces recorded by the
    # service. A separately supplied retained/stored subset is sampling and
    # retention context, not a second copy of the same AWS meter.
    "xray": frozenset({"traces_stored"}),
    # Standard Site-to-Site VPN exposes a connection-hour meter. Traffic is
    # still important capacity evidence, but AWS bills any applicable transfer
    # by direction/destination through shared transfer dimensions rather than
    # a fictional VPN processing-GB row.
    "sitetositevpn": frozenset({"data_processed_gib", "vpn_tier"}),
    # File Gateway cache is customer-managed local capacity. AWS charges the
    # bytes written through the gateway, not the cache disk itself.
    "storagegateway": frozenset({"cache_storage_gib", "gateway_type"}),
    "datasync": frozenset({"task_mode"}),
    "transfer": frozenset(
        {"protocol", "storage_backend", "transfer_direction"}
    ),
    "scheduler": frozenset({"schedules"}),
    # Targets describe the fleet receiving configurations. The chargeable
    # amount is the total configurations actually received, which is already
    # represented by configuration_retrievals.
    "appconfig": frozenset({"targets_receiving_configuration"}),
    "securityhub": frozenset({"resource_count"}),
    "auditmanager": frozenset({"evidence_items"}),
    "iot": frozenset({"device_count"}),
}


def _backup_dimension_is_compatible(
    requirement: ServiceRequirement,
    field: str,
    values: object,
) -> bool:
    """Keep an AWS Backup meter inside the selected official child identity.

    AWS publishes every protected resource and warm/cold/partial variant under
    one offer.  The Calculator child selected by the customer is the product
    identity boundary; a lower-priced EBS or cold-storage row must never be
    substituted for EFS merely because both are measured in GB.
    """

    if _stem(requirement.service) != "backup":
        return True
    identity = _canonical(str(values))
    protected = _canonical(
        str(requirement.requirements.get("protected_service") or "")
    )
    for prefix in ("amazon", "aws"):
        if protected.startswith(prefix):
            protected = protected[len(prefix) :]
    if protected.endswith("backup"):
        protected = protected[: -len("backup")]
    if protected and protected not in identity:
        return False
    if "lagv" in identity or "logicallyairgapped" in identity:
        return False
    if "earlydelete" in identity:
        return False
    if field == "backup_storage_gib":
        return (
            "storage" in identity
            and "warm" in identity
            and "restore" not in identity
            and "partial" not in identity
        )
    if field == "warm_storage_gib":
        return "warm" in identity and "storage" in identity and "restore" not in identity
    if field == "cold_storage_gib":
        return "cold" in identity and "storage" in identity and "restore" not in identity
    if field == "restore_gib":
        wants_cold = bool(requirement.requirements.get("cold_storage_gib"))
        return (
            "restore" in identity
            and "partial" not in identity
            and (("cold" in identity) if wants_cold else ("warm" in identity))
        )
    return True

# These products have closed, provider-reviewed selectors that bind customer
# fields to stable UsageType/Operation semantics. A generic profile may still
# enrich labels and editable fields, but it must never inject its cheapest raw
# dimension as a billing variant or fall back to an unrelated catalog row.
_STRICT_SEMANTIC_SERVICES = frozenset(
    {
        "efs",
        "fsx",
        "emr",
        "redshift",
        "athena",
        "directconnect",
        "sitetositevpn",
        "vpcendpoint",
        "s3glacierdeeparchive",
        "storagegateway",
        "datasync",
        "transfer",
        "transferfamily",
        "appstream",
        "workmail",
        "cloudmap",
        "sns",
        "scheduler",
        "appconfig",
        "eventbridge",
        "stepfunctions",
        "codebuild",
        "codepipeline",
        "codeartifact",
        "cloudformation",
        "inspectorv2",
        "macie",
        "securityhub",
        "auditmanager",
        "iot",
        "iotdevicemanagement",
        "iotdevicedefender",
        "kinesisvideo",
        "ivs",
        "elementalmediaconvert",
        "elementalmedialive",
        "elementalmediapackage",
        "mediaconnect",
    }
)


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
    return {
        "events": "eventbridge",
        "awsevents": "eventbridge",
    }.get(result, result)


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

    @staticmethod
    def _is_managed_flink(requirement: ServiceRequirement) -> bool:
        """Identify Flink from structured product identity only.

        The cleaning boundary owns natural-language interpretation.  Catalog
        adapters may inspect normalized identity fields, but must never reopen
        the customer's prose to choose a billed AWS dimension.
        """

        identity = " ".join(
            str(value or "")
            for value in (
                requirement.service,
                requirement.product_identity,
                requirement.calculator_service_name,
                requirement.workload_name,
            )
        ).casefold()
        return "flink" in identity

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

        if _stem(requirement.service) in _STRICT_SEMANTIC_SERVICES:
            return
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

        reader_billing_mode = str(
            requirement.requirements.get("_billing_variant_reader_billing_mode") or ""
        ).strip()
        by_field: dict[str, list[dict[str, object]]] = {}
        for binding in raw_bindings:
            if isinstance(binding, dict) and binding.get("field"):
                by_field.setdefault(str(binding["field"]), []).append(binding)

        for field, bindings in by_field.items():
            bindings = [
                binding
                for binding in bindings
                if _backup_dimension_is_compatible(
                    requirement,
                    field,
                    " ".join(
                        str(binding.get(key) or "")
                        for key in (
                            "usage_type",
                            "operation",
                            "description",
                            "product_family",
                        )
                    ),
                )
            ]
            if not bindings:
                continue
            if (
                reader_billing_mode == "per_user"
                and field == "session_capacity"
            ) or (
                reader_billing_mode == "capacity"
                and field == "reader_users"
            ):
                continue
            if field == "hours_per_month":
                has_value = customer_field_is_explicit(
                    requirement, "hours_per_month"
                )
            else:
                value = requirement.requirements.get(field)
                has_value = (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and value > 0
                )
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
                includes_amazon_q = bool(
                    requirement.requirements.get("includes_amazon_q")
                    or requirement.requirements.get("amazon_q_enabled")
                )
                matching_roles: dict[
                    tuple[str, str, str], dict[str, object]
                ] = {}
                for identity, binding in variants.items():
                    usage_folded = identity[0].casefold()
                    if edition in {"enterprise", "standard"} and edition not in usage_folded:
                        continue
                    if usage_folded.endswith("-q") and not includes_amazon_q:
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
                includes_amazon_q = bool(
                    requirement.requirements.get("includes_amazon_q")
                    or requirement.requirements.get("amazon_q_enabled")
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
                    if usage_folded.endswith("-q") and not includes_amazon_q:
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
                item: tuple[str, tuple[tuple[str, str, str], dict[str, object]]],
                *,
                markers: tuple[str, ...] = addon_markers,
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
                    any(marker in padded for marker in markers)
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
        service_stem = _stem(requirement.service)
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
        # Service billing semantics are stronger than catalog presence. Some
        # no-additional-charge control planes publish paid optional features
        # in the same AWS offer (ECS Managed Instances and CloudFormation
        # third-party handlers are examples).  Resolve the reviewed base-mode
        # contract before any generic rate lookup so an optional feature can
        # never become a fake service price.
        free_decision = no_additional_charge_decision(requirement)
        if free_decision is not None:
            specifications = dict(requirement.requirements)
            if service_stem == "ecs":
                cluster_count = int(
                    requirement.requirements.get("cluster_count")
                    or requirement.quantity
                    or 1
                )
                specifications.update(
                    {
                        "cluster_count": max(cluster_count, 1),
                        "launch_type": "EC2",
                    }
                )
                quantity = max(cluster_count, 1)
            else:
                quantity = max(requirement.quantity, 1)
            return SelectedResource(
                service=requirement.service,
                display_name=(
                    requirement.calculator_service_name or requirement.service
                ),
                region=region,
                model=free_decision.model,
                quantity=quantity,
                architecture=free_decision.architecture,
                specifications=specifications,
                official_product={
                    "source": free_decision.source_url,
                    "pricingMode": free_decision.pricing_mode,
                    "policyVersion": SERVICE_BILLING_POLICY_VERSION,
                },
                rationale=free_decision.rationale,
                substitution_notice=free_decision.notice,
                pricing_status="free",
                usage_lines=[],
                reference_rates=[],
                applied_requirement_fields=list(free_decision.applied_fields),
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
        rates = self._profile_rates(profile) if is_unknown_service else []
        if not rates:
            rates = self._catalog_rates(service_code, region)
        rates = self._enrich_missing_instance_shapes(requirement, rates, region)
        self._require_cross_field_billing_mode(requirement)
        self._require_billing_variant_choice(requirement, profile)
        semantic_rates = self._semantic_rates(requirement, rates)
        confirmed_billing_variants = any(
            key.startswith("_billing_variant_")
            and bool(value)
            and requirement.field_sources.get(f"requirements.{key}")
            in {
                "customer_text",
                "customer_confirmation",
                "customer_correction",
                "sales_confirmation",
            }
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

        def exclusive_capacity_role(item: tuple) -> str | None:
            """Classify mutually exclusive variants of one billable capacity.

            AWS publishes product variants as separate official rows.  A
            purpose-built selector may choose the customer's ONTAP storage or
            RabbitMQ topology while the generic profile independently chooses
            a cheaper OpenZFS or ActiveMQ row for the same input.  Different
            UsageTypes and instance models do not make those charges additive.
            Merge by billing role, while leaving genuinely additive dimensions
            such as backup storage distinct.
            """

            _label, _amount, rate = item
            attrs = PricingCatalog.attributes(rate[4])
            unit = str(rate[1]).casefold()
            identity_text = " ".join(
                str(value)
                for value in (
                    rate[2],
                    rate[3],
                    attrs.get("productFamily"),
                    attrs.get("storageTier"),
                    attrs.get("storageType"),
                    rate[4].get("officialDimensionDescription"),
                )
                if value
            ).casefold()
            if any(token in unit for token in ("hrs", "hour")) and (
                attrs.get("instanceType")
                or any(
                    token in _canonical(identity_text)
                    for token in (
                        "instanceusage",
                        "instanceusg",
                        "createdmsinstance",
                        "singleazusage",
                        "multiazusage",
                    )
                )
            ):
                return "instance_hours"
            if any(
                token in unit
                for token in ("mibps-mo", "mbps-mo", "mb/s-month")
            ):
                return "throughput_capacity"
            if any(
                token in unit for token in ("gb-mo", "gb-month", "gib-month")
            ) and not any(
                token in identity_text for token in ("backup", "snapshot")
            ):
                return "primary_storage_capacity"
            return None

        occupied_capacity_roles = {
            role for item in selected_rates if (role := exclusive_capacity_role(item))
        }
        for item in secondary_rates:
            role = exclusive_capacity_role(item)
            if role is not None and role in occupied_capacity_roles:
                continue
            identity = (item[2][1], item[2][2], item[2][3])
            position = identity_positions.get(identity)
            if position is None:
                identity_positions[identity] = len(selected_rates)
                selected_rates.append(item)
                if role is not None:
                    occupied_capacity_roles.add(role)
                continue
            # A field-bound customer amount is stronger than a reference-only
            # semantic row for the exact same AWS dimension.
            if selected_rates[position][1] is None and item[1] is not None:
                selected_rates[position] = item
        auto_discovered = bool(profile_bound_rates) and is_unknown_service
        strict_semantic_services = _STRICT_SEMANTIC_SERVICES
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
                        "客户填写的型号与处理器或内存规格不一致，"
                        "请从当前区域的 AWS 官方可售配置中选择",
                        code="generic_official_specification_not_found",
                        service_code=service_code,
                        region=region,
                        requested_model=requirement.requirements.get("requested_model"),
                        requested_vcpu=requested_vcpu,
                        requested_memory_gib=requested_memory,
                    )
                raise ManualConfirmationRequired(
                    "AWS 官方目录返回的计费项没有可核验的处理器和内存规格，"
                    "系统不会用无关计费项猜价",
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
        reserved_rate_identities: set[tuple[str, str, str]] = set()
        reserved_applied_fields: set[str] = set()
        reserved_capacity_units: dict[str, int] = {}
        monthly_commitment_cost = 0.0
        upfront_commitment_cost = 0.0
        if requirement.requirements.get("purchase_option") == "reserved":
            # Do not keep a service allow-list here.  A managed service supports
            # this exact Reserved offer only when its selected official product
            # exposes a matching Reserved term in the AWS Price List.  This
            # makes new services work automatically and prevents an on-demand
            # rate from being presented as a 1/3-year commitment price.
            if service_stem == "dynamodb" and str(
                requirement.requirements.get("capacity_mode") or ""
            ).casefold() in {"provisioned", "预置", "预置容量"}:
                # DynamoDB does not use No/Partial/All Upfront instance terms.
                # Its official Reserved Provisioned Capacity is published as
                # Heavy Utilization and must be bought in blocks of 100 RCU or
                # WCU. Price exactly those official terms and leave storage or
                # any excess on-demand dimensions as normal usage lines.
                for _, amount, candidate in selected_rates:
                    if amount is None:
                        continue
                    attrs = PricingCatalog.attributes(candidate[4])
                    group = str(attrs.get("group") or "")
                    field = {
                        "DDB-ReadUnits": "read_request_units",
                        "DDB-WriteUnits": "write_request_units",
                    }.get(group)
                    if field is None or "capacityunit-hrs" not in str(
                        candidate[2]
                    ).casefold():
                        continue
                    requested_units = float(
                        requirement.requirements.get(field) or 0
                    )
                    if requested_units <= 0:
                        continue
                    reserved_units = math.ceil(requested_units / 100) * 100
                    reserved_capacity_units[field] = reserved_units
                    try:
                        reserved = PricingCatalog.reserved_price(
                            candidate[4],
                            years=int(
                                requirement.requirements.get("reserved_term_years")
                                or 1
                            ),
                            payment_option="heavy_utilization",
                            hours_per_month=requirement.hours_per_month,
                        )
                    except ManualConfirmationRequired as exc:
                        if exc.code in {
                            "reserved_term_not_found",
                            "reserved_price_dimensions_missing",
                        }:
                            continue
                        raise
                    reserved_rate_identities.add(candidate[1:4])
                    reserved_applied_fields.update(
                        {
                            field,
                            "capacity_mode",
                            "reserved_term_years",
                            "purchase_option",
                        }
                    )
                    monthly_commitment_cost += (
                        reserved.monthly_amortized * reserved_units
                    )
                    upfront_commitment_cost += reserved.upfront * reserved_units
            else:
                for _, amount, candidate in selected_rates:
                    if amount is None:
                        continue
                    if not PricingCatalog.attributes(candidate[4]).get("instanceType"):
                        continue
                    try:
                        reserved = PricingCatalog.reserved_price(
                            candidate[4],
                            years=int(
                                requirement.requirements.get("reserved_term_years")
                                or 1
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
                    reserved_nodes = float(
                        requirement.requirements.get("node_count")
                        or requirement.requirements.get("nodes")
                        or requirement.requirements.get("instance_count")
                        or requirement.requirements.get("broker_count")
                        or requirement.requirements.get("replication_instances")
                        or 1
                    )
                    reserved_rate_identities.add(candidate[1:4])
                    monthly_commitment_cost = (
                        reserved.monthly_amortized
                        * requirement.quantity
                        * reserved_nodes
                    )
                    upfront_commitment_cost = (
                        reserved.upfront * requirement.quantity * reserved_nodes
                    )
                    break

        def source_fields_for_rate(
            candidate: tuple[float, str, str, str, dict[str, object]],
        ) -> list[str]:
            """Trace one selected official row back to customer fields."""

            _price, unit, usage_type, operation, product = candidate
            fields: set[str] = set()
            # Semantic adapters calculate an AWS usage amount from typed
            # customer fields before a catalog row is selected.  Keep that
            # lineage on the selected row so the universal fact ledger can
            # prove the value was used without re-reading customer prose.
            declared_fields = product.get("_astra_source_fields")
            has_declared_fields = isinstance(
                declared_fields, (list, tuple, set)
            ) and bool(declared_fields)
            if has_declared_fields:
                fields.update(
                    str(field)
                    for field in declared_fields
                    if isinstance(field, str) and field
                )
            raw_bindings = (profile or {}).get("field_bindings")
            if isinstance(raw_bindings, list) and not has_declared_fields:
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
                            "storage_gib",
                        },
                        "kpu_hours": {"kpu_count", "kpu_hours", "hours_per_month"},
                        "data_in_gib": {"data_in_gib", "write_records"},
                    }.get(field)
                    if derived_sources:
                        fields.update(derived_sources)
                    else:
                        fields.add(field)
            attrs = PricingCatalog.attributes(product)
            # Some official dimensions are calculated from several customer
            # inputs before they reach the AWS catalog.  The catalog row only
            # names the derived unit (for example Lambda GB-seconds), so keep
            # the complete input lineage here.  Otherwise the final fact
            # ledger sees the correct calculated amount but incorrectly
            # reports the original memory/duration fields as missing.
            if service_stem == "lambda":
                lambda_group = str(attrs.get("group") or "")
                if lambda_group == "AWS-Lambda-Requests":
                    fields.add("requests")
                elif lambda_group in {
                    "AWS-Lambda-Duration",
                    "AWS-Lambda-Duration-ARM",
                }:
                    fields.update({"requests", "memory_mb", "duration_ms"})
                    if requirement.requirements.get("architecture") not in (None, ""):
                        fields.add("architecture")
            elif service_stem == "eks" and "amazoneks-hours:percluster" in str(
                usage_type
            ).casefold():
                fields.update({"quantity", "cluster_count", "hours_per_month"})
            if service_stem == "dynamodb":
                fields.update(
                    {
                        "DDB-ReadUnits": {"read_request_units", "capacity_mode"},
                        "DDB-WriteUnits": {"write_request_units", "capacity_mode"},
                    }.get(str(attrs.get("group") or ""), set())
                )
            if attrs.get("instanceType") and not has_declared_fields:
                fields.update(
                    {
                        "requested_model",
                        "vcpu",
                        "memory_gib",
                        "instance_count",
                        "node_count",
                        "nodes",
                        "broker_count",
                        "replication_instances",
                        "hours_per_month",
                        "quantity",
                    }
                )
            return sorted(fields)

        applied_fields: set[str] = {
            field
            for field in _CONFIGURATION_CONTEXT_FIELDS.get(
                service_stem,
                frozenset(),
            )
            if requirement.requirements.get(field) not in (None, "", [], {})
        }
        if service_stem == "kinesis":
            capacity_mode = _canonical(
                str(requirement.requirements.get("capacity_mode") or "provisioned")
            )
            if requirement.requirements.get("capacity_mode") not in (None, ""):
                applied_fields.add("capacity_mode")
            if (
                capacity_mode
                not in {"ondemand", "ondemandstandard", "ondemandadvantage"}
                and requirement.requirements.get("data_out_gib")
                not in (None, "")
            ):
                # Standard provisioned consumers read within the throughput
                # supplied by the purchased shards; monthly read bytes are not
                # a second AWS usage dimension. Retain the value as capacity
                # context without manufacturing another billable line.
                applied_fields.add("data_out_gib")
        if service_stem == "ecr" and _canonical(
            str(requirement.requirements.get("transfer_scope") or "")
        ) in {"sameregion", "inregion"}:
            # ECR image transfer to AWS services in the same Region is free.
            # Keep the customer's volume and destination scope as explicit
            # non-billable context; never reinterpret it as internet egress.
            applied_fields.update({"data_transfer_out_gib", "transfer_scope"})
        for index, (description, amount, rate) in enumerate(selected_rates, start=1):
            price, unit, usage_type, operation, _ = rate
            if rate[1:4] in reserved_rate_identities:
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
        if reserved_rate_identities:
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
        if service_stem == "dynamodb" and reserved_capacity_units:
            substitution_notices.append(
                "DynamoDB 预留容量只能按每 100 个 RCU/WCU 一组购买；"
                "本次已分别向上取整，并使用官方 Heavy Utilization 条款核价。"
            )
        if (
            service_stem == "sitetositevpn"
            and requirement.requirements.get("data_processed_gib") not in (None, "")
        ):
            substitution_notices.append(
                "Site-to-Site VPN 的官方直接计费项为连接小时；客户提供的流量已作为容量上下文保留，"
                "任何数据传输费用必须按实际方向和目的地通过共享 Data Transfer 计费项另行核算。"
            )
        if (
            service_stem in {"transfer", "transferfamily"}
            and requirement.requirements.get("data_processed_gib") not in (None, "")
            and not customer_field_is_explicit(requirement, "transfer_direction")
        ):
            substitution_notices.append(
                "客户未区分上传和下载；两者当前官方单位价相同，本次按上传计费身份核价。"
            )
        if (
            service_stem == "s3glacierdeeparchive"
            and requirement.requirements.get("data_retrieval_gib") not in (None, "")
            and not customer_field_is_explicit(requirement, "retrieval_tier")
        ):
            substitution_notices.append(
                "客户未指定恢复档位；本次采用 Standard Retrieval，不以更慢的 Bulk 档位压低价格。"
            )
        if (
            service_stem == "opensearch"
            and requirement.requirements.get("master_nodes")
            and not requirement.requirements.get("master_requested_model")
            and selected_instance_model
        ):
            substitution_notices.append(
                "专用主节点未单独指定型号；本次按客户已指定的数据节点型号 "
                f"{selected_instance_model} 核价，避免遗漏主节点费用。"
            )
        if not has_billable_cost or reference_rates:
            substitution_notices.append(
                "未提供完整用量的部分仅展示对应官方单位价，不计入月费合计。"
            )
        specifications = dict(requirement.requirements)
        if service_stem == "lambda":
            # The customer may give an aggregate invocation count for several
            # functions. Function count is then deployment metadata, not a
            # price multiplier. Preserve it in the selected configuration so
            # the number remains auditable without multiplying total requests.
            specifications["function_count"] = requirement.quantity
            applied_fields.add("quantity")
        if reserved_capacity_units:
            specifications["reservedCapacityBilling"] = (
                "DynamoDB Reserved Provisioned Capacity (Heavy Utilization)"
            )
            if "read_request_units" in reserved_capacity_units:
                specifications["reservedReadCapacityUnits"] = (
                    reserved_capacity_units["read_request_units"]
                )
            if "write_request_units" in reserved_capacity_units:
                specifications["reservedWriteCapacityUnits"] = (
                    reserved_capacity_units["write_request_units"]
                )
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
                | reserved_applied_fields
                | (
                    {
                        "purchase_option",
                        "reserved_term_years",
                        *(
                            []
                            if service_stem == "dynamodb"
                            else ["payment_option"]
                        ),
                        "requested_model",
                        "vcpu",
                        "memory_gib",
                        "instance_count",
                        "node_count",
                        "nodes",
                        "broker_count",
                        "replication_instances",
                        "hours_per_month",
                        "quantity",
                    }
                    if reserved_rate_identities
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
        learned_aliases = {
            key.removeprefix("_fact_purpose_alias."): value
            for key, value in requirement.field_sources.items()
            if key.startswith("_fact_purpose_alias.")
            and isinstance(value, str)
            and re.fullmatch(r"[a-z][a-z0-9_]{1,62}", value)
        }
        for field in sorted(target_fields):
            alias = learned_aliases.get(field) or _SUPPLEMENT_FIELD_ALIASES.get(field)
            if not alias or field not in supplemental_requirement.requirements:
                continue
            if alias == field:
                reverse_aliases[alias] = field
                continue
            # Never overwrite another explicit customer value. A disagreement
            # between two real fields must stay visible to the ledger.
            if alias in supplemental_requirement.requirements:
                if (
                    supplemental_requirement.requirements[alias]
                    == supplemental_requirement.requirements[field]
                ):
                    reverse_aliases[alias] = field
                continue
            reverse_aliases[alias] = field
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

        # The component's own official profile cannot expose dimensions that
        # AWS publishes in another offer.  Resolve those only after the normal
        # supplement pass and only for facts that are still unconsumed.  The
        # returned rows retain the original fact path, so the global ledger
        # still prevents both omission and double billing.
        consumed = {
            field.split(".", 1)[1]
            if field.startswith("requirements.")
            else field
            for line in merged_lines
            for field in line.source_fields
        }
        shared_targets = normalized_targets - consumed
        shared_lines = self._shared_offer_usage_lines(
            requirement,
            shared_targets,
            default_region,
        )
        for index, line in enumerate(shared_lines, start=1):
            applied.update(
                field.split(".", 1)[1]
                if field.startswith("requirements.")
                else field
                for field in line.source_fields
            )
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
                    set(existing.source_fields) | set(line.source_fields)
                )
                continue
            merged_lines.append(
                line.model_copy(update={"key": f"shr{index}{line.key}"[:10]})
            )

        return selection.model_copy(
            update={
                "usage_lines": merged_lines,
                "applied_requirement_fields": sorted(applied),
            }
        )

    def _shared_offer_usage_lines(
        self,
        requirement: ServiceRequirement,
        target_fields: set[str],
        default_region: str,
    ) -> list[UsageLine]:
        """Resolve globally shared AWS charges without changing product identity."""

        lines: list[UsageLine] = []
        region = requirement.region or default_region
        if "data_transfer_out_gib" in target_fields:
            raw_amount = requirement.requirements.get("data_transfer_out_gib")
            if not isinstance(raw_amount, bool):
                try:
                    numeric_amount = float(raw_amount)
                except (TypeError, ValueError):
                    numeric_amount = 0.0
                if numeric_amount > 0:
                    amount = scoped_amount(
                        requirement,
                        "data_transfer_out_gib",
                        numeric_amount,
                        resource_count=float(
                            requirement.requirements.get("instance_count")
                            or requirement.requirements.get("node_count")
                            or requirement.requirements.get("nodes")
                            or requirement.quantity
                            or 1
                        ),
                    )
                    service_code = _SHARED_OFFER_DIMENSIONS[
                        "data_transfer_out_gib"
                    ]
                    filters = {
                        "fromLocation": self.catalog.location(region),
                        "toLocation": "External",
                        "transferType": "AWS Outbound",
                    }
                    products = self.catalog.products(
                        service_code, filters, max_pages=3
                    )
                    if not products:
                        products = self.catalog.products(
                            service_code,
                            filters,
                            max_pages=3,
                            refresh=True,
                        )
                    priced = [
                        (rate[0], product)
                        for product in products
                        if (
                            rate := PricingCatalog.on_demand_unit_rate(product)
                        )
                        is not None
                    ]
                    if priced:
                        _, product = min(priced, key=lambda item: item[0])
                        resolved_service, usage_type, operation = (
                            PricingCatalog.billing_identity(product)
                        )
                        lines.append(
                            UsageLine(
                                key="dto",
                                service_code=resolved_service,
                                usage_type=usage_type,
                                operation=operation,
                                amount=float(amount),
                                group="data-transfer",
                                source_fields=["data_transfer_out_gib"],
                            )
                        )

        for index, usage in enumerate(
            declared_shared_offer_usages(requirement, target_fields), start=1
        ):
            if usage.billing_kind != "ebs_volume_storage":
                continue
            volume_type = usage.variant or "gp3"

            def is_volume_storage(attributes: dict[str, str]) -> bool:
                usage_type = str(attributes.get("usagetype") or "").casefold()
                return usage_type.endswith(
                    f"ebs:volumeusage.{volume_type}".casefold()
                )

            products = self.catalog.matching_products(
                usage.service_code,
                {
                    "regionCode": region,
                    "productFamily": "Storage",
                    "volumeApiName": volume_type,
                },
                is_volume_storage,
                max_pages=4,
                fallback_filters={
                    "regionCode": region,
                    "volumeApiName": volume_type,
                },
                fallback_predicate=is_volume_storage,
            )
            product = PricingCatalog.require_unique(
                products,
                context=f"共享 EBS {volume_type} 存储 ({region})",
            )
            resolved_service, usage_type, operation = (
                PricingCatalog.billing_identity(product)
            )
            lines.append(
                UsageLine(
                    key=f"xbs{index}",
                    service_code=resolved_service,
                    usage_type=usage_type,
                    operation=operation,
                    amount=usage.amount,
                    group="ec2-storage",
                    source_fields=list(usage.source_fields),
                )
            )
        return lines

    def official_field_candidates(
        self,
        requirement: ServiceRequirement,
        default_region: str,
    ) -> list[dict[str, str]]:
        """Return human-readable, officially discovered field destinations.

        The AI resolver is allowed to choose only one of these field names. It
        never sees a writable price, UsageType or formula, and the selected
        field is still re-run through ``select`` before it can affect a quote.
        """

        if self.auto_discovery is None:
            return []
        profile = self.auto_discovery.ensure_profile(
            service_key=requirement.service,
            display_name=(
                requirement.calculator_service_name or requirement.service
            ),
            region=requirement.region or default_region,
        )
        if not profile or profile.get("status") != "verified":
            return []
        raw_bindings = profile.get("field_bindings")
        if not isinstance(raw_bindings, list):
            return []
        candidates: dict[tuple[str, str, str], dict[str, str]] = {}
        for binding in raw_bindings:
            if not isinstance(binding, dict):
                continue
            field = str(binding.get("field") or "").strip()
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,62}", field):
                continue
            label = str(binding.get("label") or field).strip()
            unit = str(binding.get("unit") or "unit").strip()
            description = str(binding.get("description") or "").strip()
            identity = (field, label, unit)
            candidates.setdefault(
                identity,
                {
                    "field": field,
                    "label": label,
                    "unit": unit,
                    "description": description,
                },
            )
        return sorted(
            candidates.values(),
            key=lambda item: (item["field"], item["label"], item["unit"]),
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
            usage_type_suffix: str | None = None,
            exact_operation: str | None = None,
        ) -> tuple[float, str, str, str, dict[str, object]] | None:
            candidates = []
            for item in rates:
                if exact_usage_type is not None and str(item[2]) != exact_usage_type:
                    continue
                if usage_type_suffix is not None:
                    usage_type = str(item[2]).casefold()
                    expected_suffix = usage_type_suffix.casefold()
                    if not (
                        usage_type == expected_suffix
                        or usage_type.endswith(f"-{expected_suffix}")
                    ):
                        continue
                if exact_operation is not None and str(
                    item[3]
                ).casefold() != exact_operation.casefold():
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

        def bind_source_fields(
            rate: tuple[float, str, str, str, dict[str, object]],
            source_fields: tuple[str, ...],
        ) -> tuple[float, str, str, str, dict[str, object]]:
            if not source_fields:
                return rate
            product = dict(rate[4])
            product["_astra_source_fields"] = list(source_fields)
            return (rate[0], rate[1], rate[2], rate[3], product)

        def add(
            result: list,
            description: str,
            amount: float | None,
            source_fields: tuple[str, ...] = (),
            **filters,
        ) -> None:
            rate = matching(**filters)
            if rate is not None:
                rate = bind_source_fields(rate, source_fields)
                result.append((description, amount, rate))

        result: list[
            tuple[str, float | None, tuple[float, str, str, str, dict[str, object]]]
        ] = []
        if service == "appstream":
            # AppStream publishes fleet, image-builder, app-block-builder and
            # multi-session rows for the same instanceType. A customer asking
            # for streamed-user runtime owns a Fleet workload; choosing the
            # cheapest same-model builder row would be a different product.
            model = str(requested.get("requested_model") or "").strip()
            users = requested.get("user_count")
            monthly_hours = requested.get("hours_per_user_per_month")
            daily_hours = requested.get("hours_per_user_per_day")
            effective_monthly_hours = (
                float(monthly_hours)
                if isinstance(monthly_hours, (int, float))
                and not isinstance(monthly_hours, bool)
                else (
                    float(daily_hours) * 30
                    if isinstance(daily_hours, (int, float))
                    and not isinstance(daily_hours, bool)
                    else None
                )
            )
            amount = (
                float(users) * effective_monthly_hours
                if isinstance(users, (int, float))
                and not isinstance(users, bool)
                and effective_monthly_hours is not None
                else None
            )
            source_fields = tuple(
                field
                for field in (
                    "requested_model",
                    "user_count",
                    "hours_per_user_per_month",
                    "hours_per_user_per_day",
                )
                if requested.get(field) not in (None, "")
            )
            add(
                result,
                "AppStream Fleet 实例小时价",
                amount,
                source_fields=source_fields,
                include=("fleet",),
                exclude=(
                    "imagebuilder",
                    "image builder",
                    "appblockbuilder",
                    "app block builder",
                    "elasticfleet",
                    "elastic fleet",
                    "multisession",
                    "multi-session",
                ),
                model=model or None,
                unit_contains=("hour", "hrs"),
            )
        elif service == "workmail":
            users = requested.get("user_count")
            add(
                result,
                "WorkMail 标准邮箱用户月费",
                (
                    float(users)
                    if isinstance(users, (int, float))
                    and not isinstance(users, bool)
                    else None
                ),
                source_fields=("user_count",) if users not in (None, "") else (),
                usage_type_suffix="WorkMail-NormalTier-UserHrs",
                unit_contains=("user-mo", "user-month"),
            )
        elif service == "lambda":
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
                    bind_source_fields(
                        selected_storage,
                        ("storage_gib", "storage_class", "deployment_type"),
                    ),
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
                            bind_source_fields(selected, (field,)),
                        )
                    )
            elif throughput_mode == "provisioned":
                provisioned = requested.get("provisioned_throughput_mibps")
                if provisioned not in (None, ""):
                    add(
                        result,
                        "EFS 预置吞吐量单价",
                        float(provisioned),
                        source_fields=(
                            "provisioned_throughput_mibps",
                            "throughput_mode",
                        ),
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
                        bind_source_fields(
                            selected_storage,
                            tuple(
                                field
                                for field in (
                                    "storage_gib",
                                    "file_system_type",
                                    "storage_type",
                                    "deployment_type",
                                )
                                if requested.get(field) not in (None, "")
                            ),
                        ),
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
                            bind_source_fields(
                                selected_throughput,
                                ("throughput_mbps", "file_system_type"),
                            ),
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
                            bind_source_fields(
                                selected_backup,
                                ("backup_storage_gib", "file_system_type"),
                            ),
                        )
                    )
                return result

            requested_deployment = _canonical(
                str(
                    requested.get("deployment_type")
                    or requested.get("deployment_option")
                    or ""
                )
            )
            requested_storage_type = _canonical(
                str(requested.get("storage_type") or "")
            )
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
                official_deployment = _canonical(
                    str(attrs.get("deploymentOption") or "")
                )
                if (
                    requested_deployment
                    and official_deployment != requested_deployment
                ):
                    continue
                official_storage_type = _canonical(
                    str(attrs.get("storageType") or "")
                )
                if (
                    requested_storage_type
                    and official_storage_type != requested_storage_type
                ):
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
                    "FSx 官方存储单价",
                    (
                        scoped_amount(requirement, "storage_gib", float(storage))
                        if storage
                        else None
                    ),
                    bind_source_fields(
                        selected,
                        tuple(
                            field
                            for field in (
                                "storage_gib",
                                "file_system_type",
                                "storage_type",
                                "throughput_mbps_per_tib",
                                "deployment_type",
                            )
                            if requested.get(field) not in (None, "")
                        ),
                    ),
                )
            )
            throughput = requested.get("throughput_mbps")
            if throughput not in (None, ""):
                throughput_candidates = []
                for rate in rates:
                    attrs = PricingCatalog.attributes(rate[4])
                    unit = str(rate[1]).casefold()
                    if not any(
                        token in unit
                        for token in ("mibps-mo", "mbps-mo", "mb/s-month")
                    ):
                        continue
                    official_type = str(
                        attrs.get("fileSystemType") or ""
                    ).casefold()
                    if file_system_type and official_type != file_system_type:
                        continue
                    official_deployment = _canonical(
                        str(attrs.get("deploymentOption") or "")
                    )
                    if (
                        requested_deployment
                        and official_deployment != requested_deployment
                    ):
                        continue
                    throughput_candidates.append(rate)
                if not throughput_candidates:
                    return []
                selected_throughput = min(
                    [rate for rate in throughput_candidates if rate[0] > 0]
                    or throughput_candidates,
                    key=lambda rate: (rate[0], rate[2], rate[3]),
                )
                result.append(
                    (
                        "FSx 官方吞吐量单价",
                        scoped_amount(
                            requirement,
                            "throughput_mbps",
                            float(throughput),
                        ),
                        bind_source_fields(
                            selected_throughput,
                            ("throughput_mbps", "file_system_type"),
                        ),
                    )
                )
        elif service == "s3glacierdeeparchive":
            storage = requested.get("storage_gib")
            if storage not in (None, ""):
                add(
                    result,
                    "S3 Glacier Deep Archive 存储单价",
                    scoped_amount(requirement, "storage_gib", float(storage)),
                    source_fields=("storage_gib",),
                    usage_type_suffix="TimedStorage-GDA-ByteHrs",
                    unit_contains=("gb-mo", "gb-month"),
                )
            retrieval = requested.get("data_retrieval_gib")
            if retrieval not in (None, ""):
                retrieval_tier = _canonical(
                    str(requested.get("retrieval_tier") or "standard")
                )
                suffix = (
                    "Bulk-Retrieval-Bytes"
                    if retrieval_tier == "bulk"
                    else "Standard-Retrieval-Bytes"
                )
                add(
                    result,
                    "S3 Glacier Deep Archive 数据恢复单价",
                    scoped_amount(
                        requirement, "data_retrieval_gib", float(retrieval)
                    ),
                    source_fields=("data_retrieval_gib", "retrieval_tier"),
                    usage_type_suffix=suffix,
                    exact_operation="DeepArchiveRestoreObject",
                    unit_contains=("gb", "gigabyte"),
                )
            return result
        elif service == "storagegateway":
            uploaded = requested.get("data_processed_gib")
            if uploaded not in (None, ""):
                add(
                    result,
                    "Storage Gateway 写入 AWS 数据单价",
                    scoped_amount(
                        requirement, "data_processed_gib", float(uploaded)
                    ),
                    source_fields=("data_processed_gib", "gateway_type"),
                    usage_type_suffix="Uploaded-Bytes",
                    unit_contains=("gb", "gigabyte"),
                )
            return result
        elif service == "datasync":
            transferred = requested.get("data_processed_gib")
            if transferred not in (None, ""):
                task_mode = _canonical(str(requested.get("task_mode") or "basic"))
                suffix = (
                    "Transferred-Bytes-Enhanced"
                    if task_mode == "enhanced"
                    else "Transferred-Bytes"
                )
                add(
                    result,
                    "AWS DataSync 数据复制单价",
                    scoped_amount(
                        requirement, "data_processed_gib", float(transferred)
                    ),
                    source_fields=("data_processed_gib", "task_mode"),
                    usage_type_suffix=suffix,
                    unit_contains=("gb", "gigabyte"),
                )
            return result
        elif service in {"transfer", "transferfamily"}:
            protocol = str(requested.get("protocol") or "sftp").strip().upper()
            backend = str(requested.get("storage_backend") or "s3").strip().upper()
            operation = f"{protocol}:{backend}"
            endpoint_count = requested.get("endpoint_count")
            if endpoint_count not in (None, ""):
                add(
                    result,
                    "AWS Transfer Family 协议端点小时单价",
                    scoped_amount(
                        requirement,
                        "endpoint_count",
                        float(endpoint_count) * float(requirement.hours_per_month),
                    ),
                    source_fields=(
                        "endpoint_count",
                        "protocol",
                        "storage_backend",
                        "hours_per_month",
                    ),
                    usage_type_suffix="ProtocolHours",
                    exact_operation=operation,
                    unit_contains=("hour", "hrs"),
                )
            transferred = requested.get("data_processed_gib")
            if transferred not in (None, ""):
                direction = _canonical(
                    str(requested.get("transfer_direction") or "upload")
                )
                suffix = (
                    "DownloadBytes" if direction == "download" else "UploadBytes"
                )
                add(
                    result,
                    "AWS Transfer Family 数据传输单价",
                    scoped_amount(
                        requirement, "data_processed_gib", float(transferred)
                    ),
                    source_fields=(
                        "data_processed_gib",
                        "transfer_direction",
                        "protocol",
                        "storage_backend",
                    ),
                    usage_type_suffix=suffix,
                    exact_operation=operation,
                    unit_contains=("gb", "gigabyte"),
                )
            return result
        elif service == "kinesis":
            # A provisioned Kinesis stream is billed by shard-hour.  Treat an
            # explicit shard count as workload evidence instead of falling
            # back to a one-unit reference price (which previously produced a
            # zero-dollar quote row).
            capacity_mode = _canonical(
                str(requested.get("capacity_mode") or "provisioned")
            )
            shards = requested.get("shards")
            shard_source_field = "shards"
            if shards in (None, ""):
                shards = requested.get("shard_count")
                shard_source_field = "shard_count"
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
                    source_fields=(shard_source_field, "capacity_mode"),
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
                requests = requested.get("requests")
                put_source_field = "requests"
                if requests in (None, ""):
                    requests = requested.get("request_count")
                    put_source_field = "request_count"
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
                    source_fields=(put_source_field, "capacity_mode"),
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
                        source_fields=("data_in_gib", "capacity_mode"),
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
                        source_fields=("data_out_gib", "capacity_mode"),
                        include=("ondemand",),
                        include_any=("outgoingbytes", "retrieval"),
                        exclude=("advantagecommitment", "extended", "enhanced"),
                        unit_contains=("gb", "gib"),
                    )
        elif service == "sns":
            topic_type = _canonical(
                str(requested.get("topic_type") or "standard")
            )
            requests = requested.get("requests")
            deliveries = requested.get("deliveries")
            transfer = requested.get("data_transfer_out_gib")
            if topic_type == "fifo":
                if requests not in (None, ""):
                    add(
                        result,
                        "Amazon SNS FIFO 发布请求单价",
                        float(requests),
                        source_fields=("requests", "topic_type"),
                        usage_type_suffix="F-Request-Tier1",
                        unit_contains=("request",),
                    )
                if deliveries not in (None, ""):
                    add(
                        result,
                        "Amazon SNS FIFO 订阅消息单价",
                        float(deliveries),
                        source_fields=("deliveries", "topic_type"),
                        usage_type_suffix="F-DA-SQS",
                        unit_contains=("message",),
                    )
                if transfer not in (None, ""):
                    add(
                        result,
                        "Amazon SNS FIFO 订阅消息数据量单价",
                        float(transfer),
                        source_fields=("data_transfer_out_gib", "topic_type"),
                        usage_type_suffix="F-Egress-SQS",
                        unit_contains=("gb", "gib"),
                    )
            elif topic_type in {"standard", "standardtopic"}:
                if requests not in (None, ""):
                    add(
                        result,
                        "Amazon SNS Standard API 请求单价",
                        float(requests),
                        source_fields=("requests", "topic_type"),
                        usage_type_suffix="Requests-Tier1",
                        exclude=("f-request",),
                        unit_contains=("request",),
                    )
                delivery_type = _canonical(
                    str(requested.get("delivery_type") or "")
                )
                delivery_suffix = {
                    "http": "DeliveryAttempts-HTTP",
                    "https": "DeliveryAttempts-HTTP",
                    "email": "DeliveryAttempts-SMTP",
                    "smtp": "DeliveryAttempts-SMTP",
                    "sqs": "DeliveryAttempts-SQS",
                    "lambda": "DeliveryAttempts-LAMBDA",
                    "firehose": "DeliveryAttempts-FIREHOSE",
                    "kinesisfirehose": "DeliveryAttempts-FIREHOSE",
                    "sms": "DeliveryAttempts-SMS",
                }.get(delivery_type)
                if deliveries not in (None, "") and delivery_suffix:
                    add(
                        result,
                        "Amazon SNS Standard 通知投递单价",
                        float(deliveries),
                        source_fields=("deliveries", "delivery_type", "topic_type"),
                        usage_type_suffix=delivery_suffix,
                        unit_contains=("notification", "message"),
                    )
            else:
                return []
        elif service == "scheduler":
            invocations = requested.get("scheduled_invocations")
            if invocations not in (None, ""):
                add(
                    result,
                    "Amazon EventBridge Scheduler 调用单价",
                    float(invocations),
                    source_fields=("scheduled_invocations",),
                    usage_type_suffix="ScheduledInvocation",
                    exact_operation="Invocation",
                    unit_contains=("invocation",),
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
                    source_fields=("state_transitions", "workflow_type"),
                    include_any=("statetransition", "state-transition"),
                    unit_contains=("transition",),
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
                    source_fields=("requests", "workflow_type"),
                    include=("express",),
                    include_any=("request",),
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
                    source_fields=("duration_gb_seconds", "workflow_type"),
                    include=("express",),
                    include_any=("duration", "gb-second"),
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
                source_fields=("configuration_requests",),
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
                source_fields=("configuration_retrievals",),
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
                source_fields=("experiment_hours",),
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
                source_fields=("events",),
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
                source_fields=("schema_discovery_events",),
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
                source_fields=("pipes_requests",),
                include=("piperequest",),
            )
        elif service == "config":
            recorded = requested.get("configuration_items_recorded")
            recorded_source_fields = ("configuration_items_recorded",)
            if recorded is None:
                recorded = requested.get("resource_count")
                recorded_source_fields = ("resource_count",)
            if recorded is not None:
                add(
                    result,
                    "AWS Config 配置项记录单价",
                    float(recorded),
                    source_fields=recorded_source_fields,
                    include=("configurationitemrecorded",),
                    exclude=("custom", "daily"),
                )
            evaluations = requested.get("rule_evaluations")
            if evaluations is not None:
                add(
                    result,
                    "AWS Config 规则评估单价",
                    float(evaluations),
                    source_fields=("rule_evaluations",),
                    include=("configruleevaluations",),
                    exclude=("internal", "proactive", "conformance"),
                )
        elif service == "transitgateway":
            attachment_type = _canonical(
                str(requested.get("attachment_type") or "vpc")
            )
            operation = {
                "vpc": "transitgatewayvpc",
                "directconnect": "transitgatewaydirectconnect",
                "vpn": "transitgatewayvpn",
                "peering": "transitgatewaypeering",
                "connect": "transitgatewayconnect",
            }.get(attachment_type)
            if operation:
                attachments = requested.get("attachments")
                if attachments is not None:
                    add(
                        result,
                        "AWS Transit Gateway Attachment 小时单价",
                        float(attachments) * requirement.hours_per_month,
                        source_fields=(
                            "attachments",
                            "hours_per_month",
                            "attachment_type",
                        ),
                        include=("transitgateway-hours", operation),
                        unit_contains=("hour",),
                    )
                processed = requested.get("data_processed_gib")
                if processed is not None:
                    add(
                        result,
                        "AWS Transit Gateway 数据处理 GB 单价",
                        float(processed),
                        source_fields=("data_processed_gib", "attachment_type"),
                        include=("transitgateway-bytes", operation),
                        unit_contains=("byte",),
                    )
        elif service == "directconnect":
            connection_count = requested.get("connection_count")
            count_source = "connection_count"
            if connection_count in (None, ""):
                connection_count = requirement.quantity
                count_source = "quantity"
            speed = requested.get("port_speed_gbps")
            if speed in (None, ""):
                return []
            target_speed = f"{float(speed):g}g".casefold()
            port_candidates = []
            for rate in rates:
                attrs = PricingCatalog.attributes(rate[4])
                official_speed = str(attrs.get("portSpeed") or "").strip().casefold()
                usage = str(rate[2] or "").casefold()
                operation = str(rate[3] or "").casefold()
                if official_speed != target_speed:
                    continue
                if "portusage:" not in usage or "hcportusage:" in usage:
                    continue
                if operation not in {"", "createdirectconnectport"}:
                    continue
                if not any(token in str(rate[1]).casefold() for token in ("hrs", "hour")):
                    continue
                port_candidates.append(rate)
            if not port_candidates:
                return []
            port_rate = min(
                [rate for rate in port_candidates if rate[0] > 0] or port_candidates,
                key=lambda rate: (rate[0], rate[2], rate[3]),
            )
            port_product = dict(port_rate[4])
            port_product["_astra_source_fields"] = [
                count_source,
                "port_speed_gbps",
                "hours_per_month",
            ]
            port_rate = (*port_rate[:4], port_product)
            result.append(
                (
                    "AWS Direct Connect Dedicated Port 小时单价",
                    float(connection_count) * requirement.hours_per_month,
                    port_rate,
                )
            )

            transfer = requested.get("data_transfer_out_gib")
            if transfer not in (None, ""):
                billing_prefix = str(port_rate[2]).split("-", 1)[0].casefold()
                transfer_candidates = []
                for rate in rates:
                    attrs = PricingCatalog.attributes(rate[4])
                    usage = str(rate[2] or "").casefold()
                    transfer_type = str(attrs.get("transferType") or "").casefold()
                    if not usage.startswith(f"{billing_prefix}-"):
                        continue
                    if "dataxfer-out" not in usage:
                        continue
                    if transfer_type and "outbound" not in transfer_type:
                        continue
                    if not any(token in str(rate[1]).casefold() for token in ("gb", "gib")):
                        continue
                    transfer_candidates.append(rate)
                if not transfer_candidates:
                    return []
                transfer_rate = min(
                    [rate for rate in transfer_candidates if rate[0] > 0]
                    or transfer_candidates,
                    key=lambda rate: (rate[0], rate[2], rate[3]),
                )
                transfer_product = dict(transfer_rate[4])
                transfer_product["_astra_source_fields"] = [
                    "data_transfer_out_gib"
                ]
                transfer_rate = (*transfer_rate[:4], transfer_product)
                result.append(
                    (
                        "AWS Direct Connect 出站数据传输单价",
                        float(transfer),
                        transfer_rate,
                    )
                )
        elif service == "sitetositevpn":
            tier = _canonical(str(requested.get("vpn_tier") or "standard"))
            if tier not in {"standard", "standardvpn", "ipsec"}:
                return []
            connection_count = requested.get("connection_count")
            count_source = "connection_count"
            if connection_count in (None, ""):
                connection_count = requirement.quantity
                count_source = "quantity"
            candidates = [
                rate
                for rate in rates
                if re.fullmatch(
                    r"(?:[a-z0-9]+-)?vpn-usage-hours:ipsec\.1",
                    str(rate[2] or ""),
                    re.I,
                )
                and str(rate[3] or "").casefold() == "createvpnconnection"
                and any(
                    token in str(rate[1]).casefold()
                    for token in ("hrs", "hour")
                )
            ]
            if not candidates:
                return []
            selected = min(
                [rate for rate in candidates if rate[0] > 0] or candidates,
                key=lambda rate: (rate[0], rate[2], rate[3]),
            )
            product = dict(selected[4])
            product["_astra_source_fields"] = [
                count_source,
                "hours_per_month",
            ]
            selected = (*selected[:4], product)
            result.append(
                (
                    "AWS Site-to-Site VPN 连接小时单价",
                    float(connection_count) * requirement.hours_per_month,
                    selected,
                )
            )
        elif service == "vpcendpoint":
            endpoint_type = _canonical(
                str(requested.get("endpoint_type") or "interface")
            )
            if endpoint_type not in {"interface", "interfaceendpoint", "privatelink"}:
                return []
            endpoints = requested.get("endpoint_count")
            count_source = "endpoint_count"
            if endpoints in (None, ""):
                endpoints = requirement.quantity
                count_source = "quantity"

            def exact_endpoint_rate(kind: str):
                pattern = re.compile(
                    rf"(?:[a-z0-9]+-)?vpcendpoint-{kind}", re.I
                )
                candidates = [
                    rate
                    for rate in rates
                    if pattern.fullmatch(str(rate[2] or ""))
                    and str(rate[3] or "").casefold() == "vpcendpoint"
                ]
                return (
                    min(
                        [rate for rate in candidates if rate[0] > 0]
                        or candidates,
                        key=lambda rate: (rate[0], rate[2], rate[3]),
                    )
                    if candidates
                    else None
                )

            hourly = exact_endpoint_rate("hours")
            if hourly is None:
                return []
            hourly_product = dict(hourly[4])
            hourly_product["_astra_source_fields"] = [
                count_source,
                "hours_per_month",
                "endpoint_type",
            ]
            hourly = (*hourly[:4], hourly_product)
            result.append(
                (
                    "Interface VPC Endpoint 小时单价",
                    float(endpoints) * requirement.hours_per_month,
                    hourly,
                )
            )
            processed = requested.get("data_processed_gib")
            if processed not in (None, ""):
                byte_rate = exact_endpoint_rate("bytes")
                if byte_rate is None:
                    return []
                byte_product = dict(byte_rate[4])
                byte_product["_astra_source_fields"] = [
                    "data_processed_gib",
                    "endpoint_type",
                ]
                byte_rate = (*byte_rate[:4], byte_product)
                result.append(
                    (
                        "Interface VPC Endpoint 数据处理单价",
                        float(processed),
                        byte_rate,
                    )
                )
        elif service == "dynamodb":
            capacity_mode = str(requested.get("capacity_mode") or "").casefold()
            read_units = requested.get("read_request_units")
            write_units = requested.get("write_request_units")
            provisioned = capacity_mode in {"provisioned", "预置", "预置容量"}
            if read_units:
                add(
                    result,
                    (
                        "DynamoDB 预置读取容量单位小时价"
                        if provisioned
                        else "DynamoDB 按请求读取单位价"
                    ),
                    (
                        float(read_units) * requirement.hours_per_month
                        if provisioned
                        else float(read_units)
                    ),
                    exact_group="DDB-ReadUnits",
                    include=(
                        (
                            "readcapacityunit-hrs"
                            if provisioned
                            else "readrequestunits"
                        ),
                    ),
                    exclude=("ia-",),
                )
            if write_units:
                add(
                    result,
                    (
                        "DynamoDB 预置写入容量单位小时价"
                        if provisioned
                        else "DynamoDB 按请求写入单位价"
                    ),
                    (
                        float(write_units) * requirement.hours_per_month
                        if provisioned
                        else float(write_units)
                    ),
                    exact_group="DDB-WriteUnits",
                    include=(
                        (
                            "writecapacityunit-hrs"
                            if provisioned
                            else "writerequestunits"
                        ),
                    ),
                    exclude=("ia-",),
                )
            storage = requested.get("storage_gib")
            add(
                result,
                "DynamoDB 标准表存储单价",
                float(storage) if storage else None,
                include=("timedstorage-bytehrs",),
                exclude=("ia-", "backup", "restore", "change", "capture"),
            )
        elif service == "eks":
            cluster_count = float(
                requested.get("cluster_count") or requirement.quantity
            )
            add(
                result,
                "EKS 标准控制面小时价",
                cluster_count * requirement.hours_per_month,
                include=("amazoneks-hours:percluster",),
                exclude=("local", "extended", "provisioned"),
            )
        elif service in {"ecs", "fargate"} and (
            service == "fargate"
            or _canonical(str(requested.get("launch_type") or "")) == "fargate"
        ):
            tasks = float(requested.get("tasks") or requirement.quantity)
            task_hours = requested.get("task_hours")
            runtime_field = "task_hours"
            if task_hours in (None, ""):
                task_hours = requirement.hours_per_month
                runtime_field = "hours_per_month"
            vcpu = requested.get("task_vcpu")
            memory = requested.get("task_memory_gib")
            compute_hours = (
                float(task_hours) * tasks if task_hours not in (None, "") else None
            )
            architecture = _canonical(
                str(requested.get("architecture") or "x86_64")
            )
            operating_system = _canonical(
                str(requested.get("operating_system") or "linux")
            )
            is_arm = architecture in {"arm", "arm64", "aarch64", "graviton"}
            is_windows = operating_system.startswith("windows")
            variant_tokens = (
                ("windows",)
                if is_windows
                else (("arm",) if is_arm else ())
            )
            variant_exclusions = (
                ("arm",)
                if is_windows
                else (("windows",) if is_arm else ("arm", "windows"))
            )
            common_source_fields = {
                "launch_type",
                "tasks",
                runtime_field,
                "quantity",
            }
            if requested.get("architecture") not in (None, ""):
                common_source_fields.add("architecture")
            if requested.get("operating_system") not in (None, ""):
                common_source_fields.add("operating_system")
            add(
                result,
                "Fargate vCPU 小时单价",
                compute_hours * float(vcpu) if compute_hours and vcpu else None,
                source_fields=tuple(
                    sorted(common_source_fields | {"task_vcpu"})
                ),
                include=("fargate", "vcpu", *variant_tokens),
                exclude=("spot", *variant_exclusions),
            )
            add(
                result,
                "Fargate 内存 GiB 小时单价",
                compute_hours * float(memory) if compute_hours and memory else None,
                source_fields=tuple(
                    sorted(common_source_fields | {"task_memory_gib"})
                ),
                include=("fargate", "gb-hours", *variant_tokens),
                exclude=("spot", "ephemeral", *variant_exclusions),
            )
        elif service == "ecr":
            storage = requested.get("storage_gib")
            add(
                result,
                "Amazon ECR 标准镜像存储单价",
                float(storage) if storage else None,
                source_fields=("storage_gib",),
                include=("timedstorage-bytehrs",),
                exclude=("archive", "retrieval"),
            )
        elif service == "cloudmap":
            service_instances = requested.get("service_instances")
            if service_instances not in (None, ""):
                add(
                    result,
                    "AWS Cloud Map 服务注册资源月单价",
                    float(service_instances),
                    source_fields=("service_instances",),
                    include=("cloud-map-resources",),
                    unit_contains=("cloudmapresource",),
                )
            api_calls = requested.get("api_calls")
            if api_calls not in (None, ""):
                add(
                    result,
                    "AWS Cloud Map 服务发现 API 调用单价",
                    float(api_calls),
                    source_fields=("api_calls",),
                    include=("cloud-map-api-calls",),
                    exclude=("cloud-map-dir-api-calls",),
                    unit_contains=("cloudmapapicall",),
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
                role_source_fields = [
                    f"{field_prefix}_nodes",
                    "hours_per_month",
                    "quantity",
                ]
                for field in (
                    f"{field_prefix}_requested_model",
                    f"{field_prefix}_vcpu",
                    f"{field_prefix}_memory_gib",
                ):
                    if requested.get(field) not in (None, ""):
                        role_source_fields.append(field)
                if (
                    not requested.get(f"{field_prefix}_requested_model")
                    and requested.get("requested_model") not in (None, "")
                ):
                    role_source_fields.append("requested_model")
                add(
                    result,
                    f"Amazon EMR {label}实例小时价",
                    (
                        requirement.quantity
                        * float(count)
                        * requirement.hours_per_month
                    ),
                    source_fields=tuple(sorted(set(role_source_fields))),
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
                source_fields=("data_scanned_gib",),
                include=("datascannedintb",),
                exclude=("dpu", "capacity"),
            )
            capacity = requested.get("provisioned_dpu_hours")
            if capacity:
                add(
                    result,
                    "Athena 预置容量 DPU 小时单价",
                    float(capacity),
                    source_fields=("provisioned_dpu_hours",),
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
            explicit_instance_hours = requested.get("instance_hours")
            instance_count = requested.get("instance_count")
            if explicit_instance_hours is not None:
                count = float(
                    instance_count
                    if instance_count is not None
                    else requirement.quantity
                )
                amount = count * float(explicit_instance_hours)
                source_fields = (
                    (
                        "instance_count"
                        if instance_count is not None
                        else "quantity"
                    ),
                    "instance_hours",
                    "requested_model",
                    "endpoint_type",
                )
            else:
                count = float(
                    instance_count
                    if instance_count is not None
                    else requirement.quantity
                )
                amount = count * requirement.hours_per_month
                runtime_field = "hours_per_month"
                source_fields = (
                    (
                        "instance_count"
                        if instance_count is not None
                        else "quantity"
                    ),
                    runtime_field,
                    "requested_model",
                    "endpoint_type",
                )
            add(
                result,
                f"SageMaker {model or '实例'} 小时单价",
                amount,
                source_fields=source_fields,
                include=("hrs", "runinstance"),
                exclude=("reserved", "spot"),
                model=model or None,
            )
        elif service == "textract":
            pages = requested.get("document_pages")
            analysis_type = _canonical(
                str(requested.get("analysis_type") or "document_text")
            )
            processing_mode = _canonical(
                str(requested.get("processing_mode") or "async")
            )
            analysis_suffixes = {
                "documenttext": "textpagesprocessed",
                "text": "textpagesprocessed",
                "forms": "formspagesprocessed",
                "tables": "tablespagesprocessed",
                "expense": "expensepagesprocessed",
                "id": "idpagesprocessed",
            }
            suffix = analysis_suffixes.get(analysis_type)
            mode = "sync" if processing_mode == "sync" else "async"
            if pages is not None and suffix:
                add(
                    result,
                    "Amazon Textract 文档页处理单价",
                    float(pages),
                    source_fields=("document_pages",),
                    include=(f"{mode}{suffix}",),
                    unit_contains=("page",),
                )
        elif service == "comprehend":
            characters = requested.get("characters")
            analysis_type = _canonical(
                str(requested.get("analysis_type") or "sentiment")
            )
            operations = {
                "sentiment": "detectsentiment",
                "entities": "detectentities",
                "keyphrases": "detectkeyphrases",
                "language": "detectdominantlanguage",
                "syntax": "detectsyntax",
                "pii": "detectpiientities",
            }
            operation = operations.get(analysis_type)
            if characters is not None and operation:
                add(
                    result,
                    "Amazon Comprehend 标准文本分析单价（每 100 字符一单位）",
                    math.ceil(float(characters) / 100),
                    source_fields=("characters",),
                    include=(operation,),
                    exclude=("custom", "endpoint", "storage"),
                    unit_contains=("unit",),
                )
        elif service == "rekognition":
            images = requested.get("images")
            if images is not None:
                add(
                    result,
                    "Amazon Rekognition 普通图片分析单价",
                    float(images),
                    source_fields=("images",),
                    include=("imagesprocessed",),
                    exclude=(
                        "group1-",
                        "group2-",
                        "async",
                        "custom",
                        "properties",
                    ),
                    unit_contains=("image",),
                )
        elif service == "transcribe":
            minutes = requested.get("audio_minutes")
            transcription_type = _canonical(
                str(requested.get("transcription_type") or "standard")
            )
            operations = {
                "standard": "transcribeaudio",
                "medical": "medicaltranscribeaudio",
                "callanalytics": "callanalyticstranscribeaudio",
            }
            operation = operations.get(transcription_type)
            if minutes is not None and operation:
                exclusions = (
                    ("medical", "callanalytics", "redaction", "toxicity", "clm")
                    if transcription_type == "standard"
                    else ()
                )
                add(
                    result,
                    "Amazon Transcribe 音频转写秒单价",
                    float(minutes) * 60,
                    source_fields=("audio_minutes",),
                    include=(operation,),
                    exclude=exclusions,
                    unit_contains=("second",),
                )
        elif service == "translate":
            characters = requested.get("characters")
            translation_type = _canonical(
                str(requested.get("translation_type") or "text")
            )
            operations = {
                "text": "translatetext",
                "document": "translatedocument",
                "custom": "activecustomtranslationjob",
            }
            operation = operations.get(translation_type)
            if characters is not None and operation:
                add(
                    result,
                    "Amazon Translate 字符翻译单价",
                    float(characters),
                    source_fields=("characters",),
                    include=(operation,),
                    exclude=("office",) if translation_type == "document" else (),
                    unit_contains=("character",),
                )
        elif service == "polly":
            characters = requested.get("characters")
            voice_engine = _canonical(
                str(requested.get("voice_engine") or "standard")
            )
            usage_markers = {
                "standard": "synthesizespeech-characters",
                "neural": "synthesizespeechneural-characters",
                "generative": "synthesizespeechgenerative-characters",
            }
            marker = usage_markers.get(voice_engine)
            if characters is not None and marker:
                add(
                    result,
                    "Amazon Polly 语音合成字符单价",
                    float(characters),
                    source_fields=("characters",),
                    include=(marker,),
                    exclude=("neural", "generative") if voice_engine == "standard" else (),
                    unit_contains=("character",),
                )
        elif service == "cognito":
            users = requested.get("monthly_active_users")
            user_field = "monthly_active_users"
            if users in (None, ""):
                users = requested.get("user_count")
                user_field = "user_count"
            add(
                result,
                "Cognito User Pools MAU 单价",
                (
                    scoped_amount(requirement, user_field, float(users))
                    if users not in (None, "")
                    else None
                ),
                source_fields=(user_field,),
                include_any=("cognitouserpoolsmau", "cognitouserpoolsoperation"),
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
                source_fields=(
                    "quantity",
                    "hours_per_month",
                    "broker_count",
                    "vcpu",
                    "memory_gib",
                ),
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
                    source_fields=(
                        "quantity",
                        "broker_count",
                        "storage_gib_per_broker",
                        "storage_gib",
                        "total_storage_gib",
                    ),
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
                unit_contains=("hrs", "hour"),
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
                    scoped_amount(
                        requirement,
                        "storage_gib",
                        float(storage),
                        resource_count=replication_instances,
                    ),
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
            requested_key_count = requested.get("key_count")
            key_count = float(
                requested_key_count
                if requested_key_count is not None
                else requirement.quantity
            )
            add(
                result,
                "AWS KMS 客户托管密钥月费",
                key_count,
                source_fields=(
                    ("key_count",)
                    if requested_key_count is not None
                    else ("quantity",)
                ),
                include=("kms-keys",),
                exclude=("request",),
            )
            requests = requested.get("requests")
            request_source_fields = ("requests",)
            if requests is None:
                requests = requested.get("request_count")
                request_source_fields = ("request_count",)
            add(
                result,
                "AWS KMS API 请求单价",
                float(requests) if requests is not None else None,
                source_fields=request_source_fields,
                include=("kms-requests",),
            )
        elif service == "xray":
            traces = requested.get("traces_recorded")
            trace_source_fields = ("traces_recorded",)
            if traces is None:
                traces = requested.get("traces_stored")
                trace_source_fields = ("traces_stored",)
            if traces is None:
                traces = requested.get("trace_count")
                trace_source_fields = ("trace_count",)
            add(
                result,
                "AWS X-Ray 记录 Trace 单价",
                float(traces) if traces is not None else None,
                source_fields=trace_source_fields,
                include=("xray-tracesstored",),
            )
            retrieved = requested.get("traces_retrieved")
            if retrieved is not None:
                add(
                    result,
                    "AWS X-Ray 检索 Trace 单价",
                    float(retrieved),
                    source_fields=("traces_retrieved",),
                    include=("xray-tracesaccessed", "xray-traces-retrieved"),
                )
        elif service == "codebuild":
            build_minutes = requested.get("build_minutes")
            if build_minutes is not None:
                architecture = _canonical(
                    str(requested.get("architecture") or "x86_64")
                )
                operating_system = _canonical(
                    str(requested.get("operating_system") or "linux")
                )
                compute_type = _canonical(
                    str(requested.get("compute_type") or "g1.medium")
                )
                compute_type = {
                    "general1small": "g1.small",
                    "general1medium": "g1.medium",
                    "general1large": "g1.large",
                    "general1xlarge": "g1.xlarge",
                    "general12xlarge": "g1.2xlarge",
                    "arm1small": "g1.small",
                    "arm1medium": "g1.medium",
                    "arm1large": "g1.large",
                    "arm1xlarge": "g1.xlarge",
                    "arm12xlarge": "g1.2xlarge",
                    "g1small": "g1.small",
                    "g1medium": "g1.medium",
                    "g1large": "g1.large",
                    "g1xlarge": "g1.xlarge",
                    "g12xlarge": "g1.2xlarge",
                }.get(compute_type, str(requested.get("compute_type") or "g1.medium"))
                platform = (
                    "ARM"
                    if architecture in {"arm", "arm64", "aarch64", "graviton"}
                    else "Windows"
                    if operating_system.startswith("windows")
                    else "Linux"
                )
                source_fields = tuple(
                    field
                    for field in (
                        "build_minutes",
                        "compute_type",
                        "operating_system",
                        "architecture",
                    )
                    if requested.get(field) not in (None, "")
                )
                add(
                    result,
                    "AWS CodeBuild 构建分钟单价",
                    float(build_minutes),
                    source_fields=source_fields,
                    usage_type_suffix=f"Build-Min:{platform}:{compute_type}",
                    unit_contains=("minute", "min"),
                )
        elif service == "codepipeline":
            action_minutes = requested.get("action_execution_minutes")
            if action_minutes is not None:
                add(
                    result,
                    "AWS CodePipeline V2 Action 执行分钟单价",
                    float(action_minutes),
                    source_fields=("action_execution_minutes", "pipeline_type"),
                    usage_type_suffix="actionExecutionMinute",
                    unit_contains=("minute", "min"),
                )
            active_pipelines = requested.get("active_pipelines")
            if active_pipelines is not None:
                add(
                    result,
                    "AWS CodePipeline 活跃流水线月费",
                    float(active_pipelines),
                    source_fields=("active_pipelines", "pipeline_type"),
                    usage_type_suffix="activePipeline",
                    unit_contains=("pipeline",),
                )
        elif service == "codeartifact":
            requests = requested.get("requests")
            storage = requested.get("storage_gib")
            if requests is not None:
                add(
                    result,
                    "AWS CodeArtifact 请求单价",
                    float(requests),
                    source_fields=("requests",),
                    usage_type_suffix="Requests",
                    unit_contains=("request",),
                )
            if storage is not None:
                add(
                    result,
                    "AWS CodeArtifact 制品存储单价",
                    float(storage),
                    source_fields=("storage_gib",),
                    usage_type_suffix="TimedStorage-ByteHrs",
                    unit_contains=("gb-mo", "gb-month"),
                )
        elif service == "cloudformation":
            operations = requested.get("resource_handler_operations")
            if operations is not None:
                add(
                    result,
                    "AWS CloudFormation 第三方资源处理操作单价",
                    float(operations),
                    source_fields=("resource_handler_operations",),
                    usage_type_suffix="Resource-Invocation-Count",
                    exact_operation="ProcessResourceHandlers",
                    unit_contains=("operation",),
                )
        elif service == "inspectorv2":
            monthly_hours = float(requirement.hours_per_month or 730)
            ec2_instances = requested.get("ec2_instances")
            ecr_images = requested.get("ecr_images")
            lambda_functions = requested.get("lambda_functions")
            if ec2_instances is not None:
                add(
                    result,
                    "Amazon Inspector EC2 持续扫描单价",
                    float(ec2_instances) * monthly_hours,
                    source_fields=("ec2_instances",),
                    usage_type_suffix="EC2-Scanning",
                    exclude=("free", "agentless"),
                    unit_contains=("instance-hr", "instance-hour"),
                )
            if ecr_images is not None:
                add(
                    result,
                    "Amazon Inspector ECR 镜像首次扫描单价",
                    float(ecr_images),
                    source_fields=("ecr_images",),
                    usage_type_suffix="container-image-initial-scan",
                    exclude=("free",),
                    unit_contains=("assessment",),
                )
            if lambda_functions is not None:
                add(
                    result,
                    "Amazon Inspector Lambda 标准扫描单价",
                    float(lambda_functions) * monthly_hours,
                    source_fields=("lambda_functions",),
                    usage_type_suffix="Lambda-Standard-Scanning",
                    exclude=("free",),
                    unit_contains=("hour",),
                )
        elif service == "securityhub":
            security_checks = requested.get("security_checks")
            if security_checks is not None:
                add(
                    result,
                    "AWS Security Hub CSPM 安全检查单价",
                    float(security_checks),
                    source_fields=("security_checks",),
                    usage_type_suffix="PaidComplianceCheck",
                    exclude=("azure", "free"),
                    unit_contains=("security check",),
                )
        elif service == "auditmanager":
            assessments = requested.get("resource_assessments")
            if assessments is not None:
                add(
                    result,
                    "AWS Audit Manager 资源评估单价",
                    float(assessments),
                    source_fields=("resource_assessments",),
                    usage_type_suffix="Resource-Assessment-Collected",
                    unit_contains=("assessment",),
                )
        elif service == "iot":
            connection_minutes = requested.get("connection_minutes")
            if connection_minutes is not None:
                add(
                    result,
                    "AWS IoT Core 设备连接分钟单价",
                    float(connection_minutes),
                    source_fields=("connection_minutes", "device_count"),
                    usage_type_suffix="ConnectionMinutes",
                    unit_contains=("minute",),
                )
            messages = requested.get("messages")
            if messages is not None:
                size_kib = float(requested.get("message_size_kib") or 5)
                billed_messages = float(messages) * max(1, math.ceil(size_kib / 5))
                add(
                    result,
                    "AWS IoT Core MQTT 5 KiB 消息单价",
                    billed_messages,
                    source_fields=("messages", "message_size_kib"),
                    usage_type_suffix="Messages",
                    exclude=("lorawan", "direct", "free"),
                    unit_contains=("message",),
                )
        elif service == "iotdevicemanagement":
            things = requested.get("things_registered")
            actions = requested.get("remote_actions")
            if things is not None:
                add(
                    result,
                    "AWS IoT Device Management Thing 注册单价",
                    float(things),
                    source_fields=("things_registered",),
                    usage_type_suffix="ThingRegistration",
                    unit_contains=("thing",),
                )
            if actions is not None:
                add(
                    result,
                    "AWS IoT Device Management 远程操作单价",
                    float(actions),
                    source_fields=("remote_actions",),
                    usage_type_suffix="JobExecutions",
                    unit_contains=("remote action",),
                )
        elif service == "iotdevicedefender":
            devices = requested.get("device_count")
            datapoints = requested.get("metric_datapoints")
            if devices is not None:
                add(
                    result,
                    "AWS IoT Device Defender 设备审计单价",
                    float(devices),
                    source_fields=("device_count",),
                    usage_type_suffix="Audit",
                    unit_contains=("device",),
                )
            if datapoints is not None:
                add(
                    result,
                    "AWS IoT Device Defender 规则检测指标数据点单价",
                    float(datapoints),
                    source_fields=("metric_datapoints",),
                    usage_type_suffix="Detect",
                    exclude=("ml",),
                    unit_contains=("metric datapoint",),
                )
        elif service == "kinesisvideo":
            incoming = requested.get("data_in_gib")
            outgoing = requested.get("data_out_gib")
            storage = requested.get("storage_gib")
            if incoming is not None:
                add(
                    result,
                    "Kinesis Video Streams 摄取数据单价",
                    float(incoming),
                    source_fields=("data_in_gib",),
                    usage_type_suffix="BytesIn",
                    exact_operation="PutMedia",
                    unit_contains=("gb",),
                )
            if outgoing is not None:
                add(
                    result,
                    "Kinesis Video Streams 消费读取单价",
                    float(outgoing),
                    source_fields=("data_out_gib",),
                    usage_type_suffix="BytesOut",
                    exact_operation="GetMedia",
                    unit_contains=("gb",),
                )
            if storage is not None:
                add(
                    result,
                    "Kinesis Video Streams 视频存储单价",
                    float(storage),
                    source_fields=("storage_gib",),
                    usage_type_suffix="BytesHr",
                    exact_operation="PutMedia",
                    unit_contains=("gb-month", "gb-mo"),
                )
        elif service == "elementalmediaconvert":
            minutes = requested.get("transcode_minutes")
            tier = str(
                requested.get("transcoding_tier")
                or requested.get("_billing_variant_transcoding_tier")
                or ""
            ).casefold()
            if minutes is not None and tier not in {"basic", "professional"}:
                raise ManualConfirmationRequired(
                    "MediaConvert 的标准化转码分钟需要确认 Basic 或 Professional 层级。",
                    code="mediaconvert_tier_required",
                    field="transcoding_tier",
                    nearby_candidates=[
                        {
                            "model": "Basic 标准化转码分钟",
                            "family": "billing_variant",
                            "specifications": {
                                "decision": "billing_variant:transcoding_tier:basic",
                                "field": "transcoding_tier",
                            },
                            "rationale": "使用 AWS 官方 Basic Normalized Transcode Minute 维度。",
                        },
                        {
                            "model": "Professional 标准化转码分钟",
                            "family": "billing_variant",
                            "specifications": {
                                "decision": "billing_variant:transcoding_tier:professional",
                                "field": "transcoding_tier",
                            },
                            "rationale": "使用 AWS 官方 Professional Normalized Transcode Minute 维度。",
                        },
                    ],
                )
            if minutes is not None:
                add(
                    result,
                    f"MediaConvert {tier.title()} 标准化转码分钟单价",
                    float(minutes),
                    source_fields=("transcode_minutes", "resolution", "transcoding_tier"),
                    usage_type_suffix=f"Normalized-Transcode-Minute-{tier.title()}",
                    unit_contains=("minute",),
                )
        elif service == "elementalmediapackage":
            incoming = requested.get("data_in_gib")
            outgoing = requested.get("data_out_gib")
            if incoming is not None:
                add(
                    result,
                    "MediaPackage 摄取数据单价",
                    float(incoming),
                    source_fields=("data_in_gib",),
                    usage_type_suffix="EMP-ingest-bytes",
                    unit_contains=("gb",),
                )
            if outgoing is not None:
                add(
                    result,
                    "MediaPackage Origin 打包输出单价",
                    float(outgoing),
                    source_fields=("data_out_gib",),
                    usage_type_suffix="EMP-origin-packaging-bytes",
                    unit_contains=("gb",),
                )
        elif service == "ivs":
            if not requested.get("_billing_variant_stream_profile"):
                raise ManualConfirmationRequired(
                    "IVS Low-Latency 的输入价格取决于频道类型，输出价格还取决于清晰度和观众计费地区。",
                    code="ivs_stream_profile_required",
                    field="stream_profile",
                    nearby_candidates=[
                        {
                            "model": "Standard · HD · 亚太观众",
                            "family": "billing_variant",
                            "specifications": {
                                "decision": "billing_variant:stream_profile:standard_hd_asia_pacific",
                                "field": "stream_profile",
                            },
                            "rationale": "标准频道、HD 输出，观众主要位于亚太。",
                        },
                        {
                            "model": "Basic · HD · 亚太观众",
                            "family": "billing_variant",
                            "specifications": {
                                "decision": "billing_variant:stream_profile:basic_hd_asia_pacific",
                                "field": "stream_profile",
                            },
                            "rationale": "基础频道、HD 输出，观众主要位于亚太。",
                        },
                    ],
                )
        elif service == "elementalmedialive":
            if not requested.get("_billing_variant_media_live_io_profile"):
                raise ManualConfirmationRequired(
                    "MediaLive 运行费由输入编码/分辨率/码率及输出编码/分辨率/帧率共同决定。",
                    code="medialive_io_profile_required",
                    field="media_live_io_profile",
                    nearby_candidates=[
                        {
                            "model": "Standard · AVC HD 输入 · AVC HD 30fps 输出",
                            "family": "billing_variant",
                            "specifications": {
                                "decision": "billing_variant:media_live_io_profile:standard_avc_hd_10mbps_avc_hd_30fps",
                                "field": "media_live_io_profile",
                            },
                            "rationale": "标准双管线、AVC HD 常用输入输出档位。",
                        },
                        {
                            "model": "Standard · HEVC HD 输入 · AVC HD 30fps 输出",
                            "family": "billing_variant",
                            "specifications": {
                                "decision": "billing_variant:media_live_io_profile:standard_hevc_hd_20mbps_avc_hd_30fps",
                                "field": "media_live_io_profile",
                            },
                            "rationale": "标准双管线、HEVC HD 输入与 AVC HD 输出。",
                        },
                    ],
                )
        elif service == "mediaconnect":
            if not requested.get("_billing_variant_media_connect_output_profile"):
                raise ManualConfirmationRequired(
                    "MediaConnect Output 小时价取决于 20/50/100 Mbps 档位，传输费还取决于目的地。",
                    code="mediaconnect_output_profile_required",
                    field="media_connect_output_profile",
                    nearby_candidates=[
                        {
                            "model": "20 Mbps Output · 互联网传出",
                            "family": "billing_variant",
                            "specifications": {
                                "decision": "billing_variant:media_connect_output_profile:20mbps_internet",
                                "field": "media_connect_output_profile",
                            },
                            "rationale": "按 20 Mbps 运行 Output，并按互联网传出计费。",
                        },
                        {
                            "model": "50 Mbps Output · 互联网传出",
                            "family": "billing_variant",
                            "specifications": {
                                "decision": "billing_variant:media_connect_output_profile:50mbps_internet",
                                "field": "media_connect_output_profile",
                            },
                            "rationale": "按 50 Mbps 运行 Output，并按互联网传出计费。",
                        },
                        {
                            "model": "100 Mbps Output · 互联网传出",
                            "family": "billing_variant",
                            "specifications": {
                                "decision": "billing_variant:media_connect_output_profile:100mbps_internet",
                                "field": "media_connect_output_profile",
                            },
                            "rationale": "按 100 Mbps 运行 Output，并按互联网传出计费。",
                        },
                    ],
                )
        elif service == "macie":
            bucket_count = requested.get("bucket_count")
            scanned = requested.get("data_scanned_gib")
            if bucket_count is not None:
                add(
                    result,
                    "Amazon Macie S3 Bucket 日常盘点单价",
                    float(bucket_count) * 30,
                    source_fields=("bucket_count",),
                    usage_type_suffix="PaidDataInventoryEvaluation",
                    exclude=("free",),
                    unit_contains=("bucket-day",),
                )
            if scanned is not None:
                add(
                    result,
                    "Amazon Macie 敏感数据发现扫描单价",
                    float(scanned),
                    source_fields=("data_scanned_gib",),
                    usage_type_suffix="SensitiveDataDiscovery",
                    exclude=("free",),
                    unit_contains=("gb",),
                )
        elif service == "guardduty":
            # GuardDuty publishes free-tier, S3/Lambda/RDS-specific and generic
            # data-event dimensions in one offer.  Bind the two customer facts
            # to the stable paid UsageType identities before comparing prices.
            processed = requested.get("data_processed_gib")
            if processed is not None:
                add(
                    result,
                    "Amazon GuardDuty 数据事件分析 GB 单价",
                    float(processed),
                    source_fields=("data_processed_gib",),
                    include=("paideventsanalyzed-bytes",),
                    exclude=("free", "s3", "lambda", "rds", "malware"),
                    unit_contains=("gb",),
                )
            events = requested.get("events")
            if events is not None:
                add(
                    result,
                    "Amazon GuardDuty CloudTrail 事件分析单价",
                    float(events),
                    source_fields=("events",),
                    include=("paideventsanalyzed",),
                    exclude=("bytes", "free", "s3", "lambda", "rds", "malware"),
                    unit_contains=("event",),
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
            if _stem(requirement.service) == "backup":
                requested_backup_fields = (
                    field
                    for field in (
                        "backup_storage_gib",
                        "warm_storage_gib",
                        "cold_storage_gib",
                        "restore_gib",
                    )
                    if requested.get(field) not in (None, "")
                )
                if not any(
                    _backup_dimension_is_compatible(requirement, field, text)
                    for field in requested_backup_fields
                ):
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
            if _stem(requirement.service) in {"ecs", "fargate"} and (
                _stem(requirement.service) == "fargate"
                or _canonical(str(requested.get("launch_type") or ""))
                == "fargate"
            ):
                # ECS Managed Instances expose ordinary EC2 instance types in
                # the same offer. They are a different launch mode and can
                # never supplement an explicitly selected Fargate task.
                return False
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
                or requested.get("nodes")
                or requested.get("data_nodes")
                or requested.get("broker_count")
                or requested.get("replication_instances")
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

            # One managed component can contain several independently billed
            # instance roles. OpenSearch data nodes and dedicated-master nodes
            # share the same official instance catalog. Keep the arithmetic
            # here, after structured cleaning, instead of reopening prose.
            if _stem(requirement.service) == "opensearch" and isinstance(
                requested.get("master_nodes"), (int, float)
            ) and not isinstance(requested.get("master_nodes"), bool):
                master_count = float(requested["master_nodes"])
                primary_index = next(
                    (
                        index
                        for index, (_label, _amount, rate) in enumerate(result)
                        if PricingCatalog.attributes(rate[4]).get("instanceType")
                    ),
                    None,
                )
                if master_count > 0 and primary_index is not None:
                    primary_rate = result[primary_index][2]
                    master_model = str(
                        requested.get("master_requested_model") or model
                    ).strip().casefold()
                    master_vcpu = requested.get("master_vcpu")
                    master_memory = requested.get("master_memory_gib")

                    def master_instance(rate) -> bool:
                        attrs, text = details(rate)
                        unit = str(rate[1]).casefold()
                        instance = str(attrs.get("instanceType") or "").casefold()
                        if not instance or not any(
                            token in unit for token in ("hrs", "hour")
                        ):
                            return False
                        if master_model and master_model not in {
                            instance,
                            f"db.{instance}",
                            f"cache.{instance}",
                        }:
                            return False
                        try:
                            if master_vcpu is not None and float(
                                attrs.get("vcpu") or 0
                            ) < float(master_vcpu):
                                return False
                        except (TypeError, ValueError):
                            return False
                        if master_memory is not None:
                            memory = str(
                                attrs.get("memory") or attrs.get("memoryGib") or ""
                            )
                            match = re.search(r"\d+(?:\.\d+)?", memory)
                            if not match or float(match.group()) < float(master_memory):
                                return False
                        return not any(
                            token in text
                            for token in ("reserved", "spot", "serverless")
                        )

                    master_candidates = [
                        rate for rate in safe_rates if master_instance(rate)
                    ]
                    positive_master = [
                        rate for rate in master_candidates if rate[0] > 0
                    ] or master_candidates
                    master_rate = (
                        min(
                            positive_master,
                            key=lambda rate: (rate[0], rate[2], rate[3]),
                        )
                        if positive_master
                        else None
                    )
                    if master_rate is not None:
                        master_amount = (
                            requirement.quantity
                            * master_count
                            * requirement.hours_per_month
                        )
                        master_sources = {
                            "master_nodes",
                            "dedicated_master",
                            *(
                                {"master_requested_model"}
                                if requested.get("master_requested_model")
                                else {"requested_model"}
                            ),
                            *({"master_vcpu"} if master_vcpu is not None else set()),
                            *(
                                {"master_memory_gib"}
                                if master_memory is not None
                                else set()
                            ),
                        }

                        def with_sources(rate, sources: set[str]):
                            product = dict(rate[4])
                            declared = product.get("_astra_source_fields")
                            declared_values = (
                                declared
                                if isinstance(declared, (list, tuple, set))
                                else ()
                            )
                            product["_astra_source_fields"] = sorted(
                                {
                                    *(
                                        str(value)
                                        for value in declared_values
                                        if isinstance(value, str) and value
                                    ),
                                    *sources,
                                }
                            )
                            return (*rate[:4], product)

                        if master_rate[1:4] == primary_rate[1:4]:
                            label, primary_amount, _ = result[primary_index]
                            result[primary_index] = (
                                f"{label}（数据节点及专用主节点）",
                                float(primary_amount or 0) + master_amount,
                                with_sources(primary_rate, master_sources),
                            )
                        else:
                            result.append(
                                (
                                    "AWS 官方专用主节点实例小时价",
                                    master_amount,
                                    with_sources(master_rate, master_sources),
                                )
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
                bindings = [
                    binding
                    for binding in bindings
                    if _backup_dimension_is_compatible(
                        requirement,
                        field,
                        " ".join(
                            str(binding.get(key) or "")
                            for key in (
                                "usage_type",
                                "operation",
                                "description",
                                "product_family",
                            )
                        ),
                    )
                ]
                if not bindings:
                    continue
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
                    # A customer can provide either current/expected stored
                    # capacity directly or an ingest volume plus retention.
                    # Both describe the same official GB-month dimension.  A
                    # direct capacity is more authoritative and avoids asking
                    # the customer to invent retention settings merely to
                    # quote an already known 2 TiB magnetic-store footprint.
                    value = requested.get("storage_gib")
                    if value in (None, ""):
                        value = (
                            max(
                                100.0,
                                float(monthly_ingest_gib)
                                * float(retention_days)
                                / 30.0,
                            )
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
                    storage_source_fields: list[str] = []
                    value = None
                    per_node_storage = requested.get("storage_gib_per_node")
                    topology_field = next(
                        (
                            key
                            for key in (
                                "instance_count",
                                "node_count",
                                "nodes",
                                "data_nodes",
                                "broker_count",
                                "replication_instances",
                            )
                            if isinstance(requested.get(key), (int, float))
                            and not isinstance(requested.get(key), bool)
                            and float(requested[key]) > 0
                        ),
                        "",
                    )
                    if (
                        isinstance(per_node_storage, (int, float))
                        and not isinstance(per_node_storage, bool)
                        and float(per_node_storage) > 0
                        and topology_field
                    ):
                        # Explicit scope outranks a generic duplicate produced
                        # during normalization.  AWS managed products commonly
                        # expose storage as aggregate GB-month while customers
                        # describe it per node.
                        value = (
                            float(per_node_storage)
                            * float(requested[topology_field])
                            * float(requirement.quantity)
                        )
                        storage_source_fields.extend(
                            ("storage_gib_per_node", topology_field)
                        )
                    if value in (None, ""):
                        value = requested.get("total_storage_gib")
                        if value not in (None, ""):
                            storage_source_fields.append("total_storage_gib")
                    if value in (None, ""):
                        value = requested.get("storage_gib")
                        if value not in (None, ""):
                            storage_source_fields.append("storage_gib")
                    kpu_count = requested.get("kpu_count")
                    is_flink = GenericOfficialPlugin._is_managed_flink(requirement)
                    is_flink_storage = any(
                        "runningapplicationstorage"
                        in str(binding.get("usage_type") or "").casefold()
                        and "interactive"
                        not in str(binding.get("usage_type") or "").casefold()
                        for binding in bindings
                    ) and is_flink
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
                    hours_are_explicit = customer_field_is_explicit(
                        requirement, "hours_per_month"
                    )
                    value = requirement.hours_per_month if hours_are_explicit else None
                else:
                    value = requested.get(field)
                if (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or value <= 0
                ):
                    continue
                selected_usage_type = str(
                    requested.get(f"_billing_variant_{field}") or ""
                ).strip()
                if (
                    not selected_usage_type
                    and GenericOfficialPlugin._is_managed_flink(requirement)
                ):
                    selected_usage_type = next(
                        (
                            str(binding.get("usage_type") or "")
                            for binding in bindings
                            if (
                                field == "kpu_hours"
                                and "kpu-hour-java"
                                in str(binding.get("usage_type") or "").casefold()
                            )
                            or (
                                field == "storage_gib"
                                and "runningapplicationstorage"
                                in str(binding.get("usage_type") or "").casefold()
                                and "interactive"
                                not in str(binding.get("usage_type") or "").casefold()
                            )
                        ),
                        "",
                    )

                def bound_rate(
                    rate,
                    *,
                    candidates=bindings,
                    required_usage_type=selected_usage_type,
                ) -> bool:
                    if required_usage_type and str(rate[2]) != required_usage_type:
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
                if field == "processing_hours" and any(
                    "minute" in str(binding.get("unit") or "").casefold()
                    for binding in bindings
                ):
                    amount *= 60.0
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
                    if field == "storage_gib" and storage_source_fields:
                        description, selected_amount, selected_rate = result[-1]
                        selected_product = dict(selected_rate[4])
                        declared = selected_product.get("_astra_source_fields")
                        declared_values = (
                            declared
                            if isinstance(declared, (list, tuple, set))
                            else ()
                        )
                        selected_product["_astra_source_fields"] = sorted(
                            {
                                *(
                                    str(item)
                                    for item in declared_values
                                    if isinstance(item, str) and item
                                ),
                                *storage_source_fields,
                            }
                        )
                        result[-1] = (
                            description,
                            selected_amount,
                            (*selected_rate[:4], selected_product),
                        )
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
                    for token in ("backup", "warm storage", "warmstorage")
                )
                and _backup_dimension_is_compatible(
                    requirement,
                    "backup_storage_gib",
                    details(rate)[1],
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
                previous_count = len(result)
                choose(description, float(value), predicate)
                if len(result) > previous_count:
                    selected_description, selected_amount, selected_rate = result[-1]
                    selected_product = dict(selected_rate[4])
                    selected_product["_astra_source_fields"] = [field]
                    result[-1] = (
                        selected_description,
                        selected_amount,
                        (*selected_rate[:4], selected_product),
                    )

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
