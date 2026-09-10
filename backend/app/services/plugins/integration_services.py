from __future__ import annotations

import math
from typing import Any

from app.core.errors import ManualConfirmationRequired
from app.domain.models import (
    CandidateOption,
    PreviewSelection,
    ReferenceRate,
    SelectedResource,
    ServiceKind,
    ServiceRequirement,
    UsageLine,
)
from app.integrations.aws import PricingCatalog
from app.services.aws_query_executor import ReadOnlyAwsQueryExecutor
from app.services.plugins.base import ServicePlugin, required_float


def _usage(
    product: dict[str, Any],
    key: str,
    amount: float,
    group: str,
    *,
    source_fields: tuple[str, ...] = (),
) -> UsageLine:
    service_code, usage_type, operation = PricingCatalog.billing_identity(product)
    return UsageLine(
        key=key,
        service_code=service_code,
        usage_type=usage_type,
        operation=operation,
        amount=amount,
        group=group,
        source_fields=list(source_fields),
    )


def _reference(product: dict[str, Any], description: str) -> ReferenceRate:
    service_code, usage_type, operation = PricingCatalog.billing_identity(product)
    priced = PricingCatalog.on_demand_unit_rate(product)
    if priced is None:
        raise ManualConfirmationRequired(
            "AWS 官方目录暂时没有返回该项目的单位价格",
            code="reference_unit_rate_not_found",
            service_code=service_code,
            usage_type=usage_type,
        )
    price, unit = priced
    return ReferenceRate(
        description=description,
        unit=unit,
        unit_price=price,
        service_code=service_code,
        usage_type=usage_type,
        operation=operation,
    )


class _NoConfirmationPlugin(ServicePlugin):
    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        preview = super().preview(requirement, default_region)
        return preview.model_copy(
            update={"requires_confirmation": False, "confirmation_reason": None}
        )


class MskPlugin(_NoConfirmationPlugin):
    kind = ServiceKind.MSK
    display_name = "Amazon MSK"

    @staticmethod
    def _is_provisioned_broker(attributes: dict[str, str]) -> bool:
        """Match the stable AWS billing identity, not optional catalog labels."""

        usage_type = str(attributes.get("usagetype") or "").casefold()
        operation = str(attributes.get("operation") or "").casefold()
        compute_family = str(attributes.get("computeFamily") or "").casefold()
        return (
            operation == "runbroker"
            and "-kafka." in usage_type
            and "-express." not in usage_type
            and not compute_family.startswith("express.")
        )

    @staticmethod
    def _is_gp2_broker_storage(attributes: dict[str, str]) -> bool:
        usage_type = str(attributes.get("usagetype") or "").casefold()
        operation = str(attributes.get("operation") or "").casefold()
        return operation == "runvolume" and usage_type.endswith("kafka.storage.gp2")

    def _broker_products(self, region: str) -> list[dict[str, Any]]:
        # ``group`` is an optional Price List label.  Some current regions
        # publish Broker on compute rows but omit it from storage rows; other
        # schema revisions may omit it from both.  Prefer the narrow query for
        # speed, then use the stable UsageType/Operation identity as the final
        # authority through the shared catalog fallback.
        return self.catalog.matching_products(
            "AmazonMSK",
            {"regionCode": region, "group": "Broker", "operation": "RunBroker"},
            self._is_provisioned_broker,
            max_pages=10,
            fallback_filters={"regionCode": region, "operation": "RunBroker"},
            fallback_predicate=self._is_provisioned_broker,
        )

    def _storage_products(self, region: str) -> list[dict[str, Any]]:
        return self.catalog.matching_products(
            "AmazonMSK",
            {"regionCode": region, "group": "Storage", "operation": "RunVolume"},
            self._is_gp2_broker_storage,
            max_pages=4,
            fallback_filters={"regionCode": region, "operation": "RunVolume"},
            fallback_predicate=self._is_gp2_broker_storage,
        )

    @staticmethod
    def _is_serverless_dimension(attributes: dict[str, str]) -> bool:
        usage_type = str(attributes.get("usagetype") or "").casefold()
        operation = str(attributes.get("operation") or "").casefold()
        return operation == "serverless" and "kafkaserverless-" in usage_type

    def _serverless_products(self, region: str) -> list[dict[str, Any]]:
        return self.catalog.matching_products(
            "AmazonMSK",
            {"regionCode": region, "operation": "Serverless"},
            self._is_serverless_dimension,
            max_pages=5,
        )

    @staticmethod
    def _serverless_dimension(
        products: list[dict[str, Any]], suffix: str, *, region: str
    ) -> dict[str, Any]:
        normalized_suffix = suffix.casefold()
        return PricingCatalog.require_unique(
            [
                product
                for product in products
                if str(PricingCatalog.attributes(product).get("usagetype") or "")
                .casefold()
                .endswith(normalized_suffix)
            ],
            context=f"Amazon MSK Serverless {suffix} ({region})",
        )

    def _select_serverless(
        self, requirement: ServiceRequirement, region: str
    ) -> SelectedResource:
        requested = requirement.requirements
        products = self._serverless_products(region)
        cluster = self._serverless_dimension(
            products, "KafkaServerless-ClusterHours", region=region
        )
        incoming = self._serverless_dimension(
            products, "KafkaServerless-In-Bytes", region=region
        )
        outgoing = self._serverless_dimension(
            products, "KafkaServerless-Out-Bytes", region=region
        )
        partitions = self._serverless_dimension(
            products, "KafkaServerless-PartitionHours", region=region
        )
        storage = self._serverless_dimension(
            products, "KafkaServerless-StorageHours", region=region
        )

        lines = [
            _usage(
                cluster,
                "mskclshr",
                requirement.quantity * requirement.hours_per_month,
                "msk",
                source_fields=("quantity", "hours_per_month", "cluster_type"),
            )
        ]
        references: list[ReferenceRate] = []

        incoming_field = (
            "data_in_gib"
            if requested.get("data_in_gib") is not None
            else "data_transfer_in_gib"
        )
        incoming_amount = required_float(requested, incoming_field)
        if incoming_amount is not None:
            lines.append(
                _usage(
                    incoming,
                    "mskin",
                    incoming_amount,
                    "msk",
                    source_fields=(incoming_field,),
                )
            )
        else:
            references.append(_reference(incoming, "MSK Serverless 写入数据单价"))

        outgoing_field = (
            "data_out_gib"
            if requested.get("data_out_gib") is not None
            else "data_transfer_out_gib"
        )
        outgoing_amount = required_float(requested, outgoing_field)
        if outgoing_amount is not None:
            lines.append(
                _usage(
                    outgoing,
                    "mskout",
                    outgoing_amount,
                    "msk",
                    source_fields=(outgoing_field,),
                )
            )
        else:
            references.append(_reference(outgoing, "MSK Serverless 读取数据单价"))

        partition_count = required_float(requested, "partition_count")
        if partition_count is not None:
            lines.append(
                _usage(
                    partitions,
                    "mskpart",
                    requirement.quantity
                    * partition_count
                    * requirement.hours_per_month,
                    "msk",
                    source_fields=(
                        "quantity",
                        "partition_count",
                        "hours_per_month",
                    ),
                )
            )
        else:
            references.append(_reference(partitions, "MSK Serverless 分区小时单价"))

        storage_field = next(
            (
                field
                for field in ("storage_gib", "total_storage_gib")
                if requested.get(field) is not None
            ),
            None,
        )
        if storage_field is not None:
            lines.append(
                _usage(
                    storage,
                    "mskstore",
                    float(requested[storage_field]),
                    "msk",
                    source_fields=(storage_field,),
                )
            )
        else:
            references.append(_reference(storage, "MSK Serverless 存储单价"))

        return SelectedResource(
            service=self.kind,
            display_name="Amazon MSK Serverless",
            region=region,
            model="serverless",
            quantity=requirement.quantity,
            architecture=f"{requirement.quantity} 套 Serverless 集群",
            requested_specifications={"clusterType": "serverless"},
            official_specifications={"clusterType": "serverless"},
            specifications={"clusterType": "serverless"},
            official_product=cluster,
            rationale=(
                "按 MSK Serverless 集群小时及客户提供的写入、读取、分区和存储"
                "官方计费维度提交 BCM。"
            ),
            usage_lines=lines,
            reference_rates=references,
            applied_requirement_fields=["cluster_type"],
        )

    def configuration_candidates(
        self, requirement: ServiceRequirement, default_region: str
    ) -> list[CandidateOption]:
        """Return every regional provisioned Broker shape for edit controls."""

        region = requirement.region or default_region
        if str(requirement.requirements.get("cluster_type") or "").casefold() == "serverless":
            return []
        products = self._broker_products(region)
        products_by_model: dict[str, tuple[float, dict[str, Any]]] = {}
        for product in products:
            model = _msk_model(product)
            if not model or model.startswith("express."):
                continue
            rate = PricingCatalog.on_demand_rate(product)
            if rate is None:
                continue
            current = products_by_model.get(model)
            if current is None or rate < current[0]:
                products_by_model[model] = (rate, product)
        if not products_by_model:
            return []

        official_specs: dict[str, tuple[float, float]] = {}
        try:
            payload = ReadOnlyAwsQueryExecutor(self.clients).execute(
                service="ec2",
                operation="describe_instance_types",
                region=region,
                parameters={"InstanceTypes": sorted(products_by_model)},
                paginate=False,
            )
            for page in payload.get("pages", [payload]):
                for item in page.get("InstanceTypes", []):
                    official_specs[str(item["InstanceType"]).lower()] = (
                        float(item["VCpuInfo"]["DefaultVCpus"]),
                        float(item["MemoryInfo"]["SizeInMiB"]) / 1024,
                    )
        except (
            ManualConfirmationRequired,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            # Catalog attributes are still usable when the read-only EC2
            # specification endpoint is temporarily unavailable.
            pass

        candidates: list[CandidateOption] = []
        for model, (rate, product) in products_by_model.items():
            attrs = PricingCatalog.attributes(product)
            try:
                catalog_vcpu = float(attrs.get("vcpu") or 0)
                catalog_memory = float(attrs.get("memoryGib") or 0)
            except (TypeError, ValueError):
                catalog_vcpu, catalog_memory = 0, 0
            vcpu, memory = official_specs.get(model, (catalog_vcpu, catalog_memory))
            specifications = {
                **({"vCPU": vcpu} if vcpu > 0 else {}),
                **({"memoryGiB": memory} if memory > 0 else {}),
            }
            candidates.append(
                CandidateOption(
                    model=model,
                    family="Amazon MSK Broker",
                    specifications=specifications,
                    monthly_catalog_cost=rate * 730,
                    rationale="AWS 当前区域可用的 MSK Broker 官方规格",
                    official_product=product,
                )
            )
        return sorted(
            candidates,
            key=lambda item: (
                item.monthly_catalog_cost is None,
                item.monthly_catalog_cost or 0,
                item.model,
            ),
        )

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        requested = requirement.requirements
        if str(requested.get("cluster_type") or "").casefold() == "serverless":
            return self._select_serverless(requirement, region)
        broker_count = int(required_float(requested, "broker_count") or 1)
        requested_model = str(requested.get("requested_model") or "").strip().lower()
        min_vcpu = required_float(requested, "vcpu")
        min_memory = required_float(requested, "memory_gib")

        broker_products = self._broker_products(region)
        product_models = {
            _msk_model(product)
            for product in broker_products
            if _msk_model(product) and not _msk_model(product).startswith("express.")
        }
        official_specs: dict[str, tuple[float, float]] = {}
        # AWS Price List identifies the MSK compute family but currently does
        # not consistently include vCPU/memory attributes.  Enrich those
        # families from the read-only EC2 DescribeInstanceTypes API before
        # applying the customer's shape constraints.
        # A selected Broker family and the CPU/memory shown beside it must come
        # from the same official source.  Previously this lookup ran only when
        # the customer still had an explicit shape constraint. After the
        # customer chose a replacement model, that constraint was correctly
        # removed, but this branch was skipped and presentation fell back to
        # stale Price List attributes. Always resolve the official EC2 shape
        # for every MSK Broker model used in pricing.
        if product_models:
            try:
                payload = ReadOnlyAwsQueryExecutor(self.clients).execute(
                    service="ec2",
                    operation="describe_instance_types",
                    region=region,
                    parameters={"InstanceTypes": sorted(product_models)},
                    paginate=False,
                )
                for page in payload.get("pages", [payload]):
                    for item in page.get("InstanceTypes", []):
                        official_specs[str(item["InstanceType"]).lower()] = (
                            float(item["VCpuInfo"]["DefaultVCpus"]),
                            float(item["MemoryInfo"]["SizeInMiB"]) / 1024,
                        )
            except (ManualConfirmationRequired, KeyError, TypeError, ValueError) as exc:
                if isinstance(exc, ManualConfirmationRequired) and exc.code in {
                    "aws_credentials_invalid",
                    "aws_region_not_enabled",
                }:
                    raise
                raise ManualConfirmationRequired(
                    "AWS 官方 API 暂时无法核验 MSK Broker 的 CPU 和内存规格",
                    code="msk_discovery_failed",
                    region=region,
                ) from exc
        candidates: list[tuple[float, str, dict[str, Any]]] = []
        for product in broker_products:
            attrs = PricingCatalog.attributes(product)
            model = _msk_model(product)
            if model.startswith("express."):
                continue
            try:
                catalog_vcpu = float(attrs.get("vcpu") or 0)
                catalog_memory = float(attrs.get("memoryGib") or 0)
            except (TypeError, ValueError):
                continue
            vcpu, memory = official_specs.get(model, (catalog_vcpu, catalog_memory))
            if min_vcpu is not None and vcpu < min_vcpu:
                continue
            if min_memory is not None and memory < min_memory:
                continue
            rate = PricingCatalog.on_demand_rate(product)
            if rate is not None:
                candidates.append((rate, model, product))

        if not candidates:
            raise ManualConfirmationRequired(
                "AWS 官方目录没有返回符合要求的 MSK Broker 规格",
                code="msk_specification_not_found",
                requested_model=requested_model or None,
                vcpu=min_vcpu,
                memory_gib=min_memory,
            )
        normalized_requested = requested_model.removeprefix("kafka.")
        exact = next(
            (item for item in candidates if item[1] == normalized_requested),
            None,
        )
        _, model, broker_product = exact or min(candidates, key=lambda item: (item[0], item[1]))
        attrs = PricingCatalog.attributes(broker_product)
        selected_vcpu, selected_memory = official_specs.get(
            model,
            (float(attrs.get("vcpu") or 0), float(attrs.get("memoryGib") or 0)),
        )
        storage_gib = required_float(requested, "storage_gib_per_broker")
        storage_products = self._storage_products(region)
        storage_product = PricingCatalog.require_unique(
            storage_products, context=f"Amazon MSK Broker 存储 ({region})"
        )

        cluster_count = requirement.quantity
        lines = [
            _usage(
                broker_product,
                "mskbroker",
                cluster_count * broker_count * requirement.hours_per_month,
                "msk",
                source_fields=(
                    "quantity",
                    "broker_count",
                    "hours_per_month",
                    "requested_model",
                    "vcpu",
                    "memory_gib",
                ),
            )
        ]
        references: list[ReferenceRate] = []
        if storage_gib is None:
            references.append(_reference(storage_product, "Amazon MSK Broker 存储单价"))
        else:
            lines.append(
                _usage(
                    storage_product,
                    "mskstore",
                    cluster_count * broker_count * storage_gib,
                    "msk",
                    source_fields=(
                        "quantity",
                        "broker_count",
                        "storage_gib_per_broker",
                        "total_storage_gib",
                    ),
                )
            )

        auto_selected = not requested_model
        substituted = bool(requested_model and exact is None)
        notice = None
        if storage_gib is None:
            notice = "客户未提供每个 Broker 的存储容量；存储仅展示 AWS 官方单位价，不计入月费合计。"
        if auto_selected:
            selected_notice = (
                f"客户未指定 Broker 型号；按满足已知规格的最低官方小时价选择 {model}。"
            )
            notice = f"{selected_notice}{notice or ''}"
        elif substituted:
            selected_notice = (
                f"客户指定的 {requested_model} 在当前区域不可报价；已在相同或不低于原配置且"
                f"可报价的 Broker 中，自动替换为最低价的 {model}。"
            )
            notice = f"{selected_notice}{notice or ''}"
        requested_shape: list[str] = []
        selected_shape: list[str] = []
        if min_vcpu is not None:
            requested_shape.append(f"{min_vcpu:g} vCPU")
            selected_shape.append(f"{selected_vcpu:g} vCPU")
        if min_memory is not None:
            requested_shape.append(f"{min_memory:g} GiB 内存")
            selected_shape.append(f"{selected_memory:g} GiB 内存")
        shape_was_raised = (min_vcpu is not None and selected_vcpu > min_vcpu) or (
            min_memory is not None and selected_memory > min_memory
        )
        if shape_was_raised:
            shape_notice = (
                f"客户要求每个 Broker 至少{'、'.join(requested_shape)}；"
                f"AWS MSK 可购规格中满足全部下限且小时价最低的是 {model}"
                f"（{'、'.join(selected_shape)}），Broker 数量仍为 {broker_count}。"
            )
            notice = f"{shape_notice}{notice or ''}"
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model=model,
            architecture=f"{cluster_count} 套集群 · 每套 {broker_count} 个 Broker",
            specifications={
                "brokerCount": broker_count,
                "vCPU": selected_vcpu,
                "memoryGiB": selected_memory,
                **({"storageGiBPerBroker": storage_gib} if storage_gib is not None else {}),
            },
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale="按 MSK Broker 小时和每 Broker 存储两个官方计费维度提交 BCM。",
            substitution_notice=notice,
            usage_lines=lines,
            reference_rates=references,
        )


def _msk_model(product: dict[str, Any]) -> str:
    attrs = PricingCatalog.attributes(product)
    model = str(attrs.get("computeFamily") or "").strip().lower()
    if model:
        return model
    usage = str(attrs.get("usagetype") or "")
    marker = "-Kafka."
    return usage.rsplit(marker, 1)[-1].lower() if marker in usage else ""


class ApiGatewayPlugin(_NoConfirmationPlugin):
    kind = ServiceKind.API_GATEWAY
    display_name = "Amazon API Gateway"

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        requested = requirement.requirements
        api_type = str(requested.get("api_type") or "http").strip().casefold()
        is_websocket = api_type in {"websocket", "web_socket", "websocket_api"}
        if is_websocket:
            products = self.catalog.products(
                "AmazonApiGateway",
                {"regionCode": region, "operation": "ApiGatewayWebSocket"},
                max_pages=3,
            )
            message_product = PricingCatalog.require_unique(
                [
                    product
                    for product in products
                    if "message" in str(
                        PricingCatalog.on_demand_unit_rate(product)[1]
                        if PricingCatalog.on_demand_unit_rate(product)
                        else ""
                    ).casefold()
                ],
                context=f"API Gateway WebSocket 消息 ({region})",
            )
            minute_product = PricingCatalog.require_unique(
                [
                    product
                    for product in products
                    if "minute" in str(
                        PricingCatalog.on_demand_unit_rate(product)[1]
                        if PricingCatalog.on_demand_unit_rate(product)
                        else ""
                    ).casefold()
                ],
                context=f"API Gateway WebSocket 连接分钟 ({region})",
            )
            messages = required_float(requested, "messages")
            connection_minutes = required_float(requested, "connection_minutes")
            lines = []
            references = []
            if messages is not None:
                lines.append(
                    _usage(
                        message_product,
                        "apigwmsg",
                        messages,
                        "api-gateway",
                        source_fields=("messages", "api_type"),
                    )
                )
            else:
                references.append(_reference(message_product, "WebSocket 消息单价"))
            if connection_minutes is not None:
                lines.append(
                    _usage(
                        minute_product,
                        "apigwmin",
                        connection_minutes,
                        "api-gateway",
                        source_fields=("connection_minutes", "api_type"),
                    )
                )
            else:
                references.append(_reference(minute_product, "WebSocket 连接分钟单价"))
            missing = []
            if messages is None:
                missing.append("消息数")
            if connection_minutes is None:
                missing.append("连接分钟")
            return SelectedResource(
                service=self.kind,
                display_name=self.display_name,
                region=region,
                model="WebSocket API",
                architecture=(
                    f"每月 {messages:g} 条消息 · {connection_minutes:g} 连接分钟"
                    if messages is not None and connection_minutes is not None
                    else "WebSocket 官方计费维度"
                ),
                specifications={
                    "apiType": "WebSocket",
                    **({"messages": messages} if messages is not None else {}),
                    **(
                        {"connectionMinutes": connection_minutes}
                        if connection_minutes is not None
                        else {}
                    ),
                },
                official_product={"source": "AWS Price List", "regionCode": region},
                rationale="使用 API Gateway WebSocket 官方消息与连接分钟两个独立计费维度。",
                substitution_notice=(
                    f"客户未提供{'、'.join(missing)}；缺少部分仅展示官方单位价，不计入月费合计。"
                    if missing
                    else None
                ),
                usage_lines=lines,
                reference_rates=references,
            )
        is_rest = api_type in {"rest", "rest_api", "restapi"}
        operation = "ApiGatewayRequest" if is_rest else "ApiGatewayHttpApi"
        products = self.catalog.products(
            "AmazonApiGateway",
            {"regionCode": region, "operation": operation},
            max_pages=3,
        )
        product = PricingCatalog.require_unique(
            products,
            context=f"API Gateway {'REST' if is_rest else 'HTTP'} API 请求 ({region})",
        )
        requests = None
        for key in ("requests", "request_count", "monthly_requests"):
            requests = required_float(requested, key)
            if requests is not None:
                break
        request_size_mb = required_float(requested, "request_size_mb")
        # HTTP API requests are metered in complete 512-KB increments.  The
        # structured payload size therefore changes the billed request units;
        # it is not merely display metadata.  REST request pricing remains per
        # call, but an explicit size is still retained as configuration
        # context rather than being reported as lost.
        payload_units = (
            max(1, math.ceil(request_size_mb / 0.5))
            if request_size_mb is not None and not is_rest
            else 1
        )
        billed_requests = requests * payload_units if requests is not None else None
        request_sources = (
            "requests",
            "request_count",
            "monthly_requests",
            "api_type",
            *(("request_size_mb",) if request_size_mb is not None and not is_rest else ()),
        )
        lines = [
            _usage(
                product,
                "apigw",
                billed_requests,
                "api-gateway",
                source_fields=request_sources,
            )
        ] if requests else []
        references = [] if requests else [_reference(product, "API Gateway 请求单价")]
        notice = None
        if requests is None:
            notice = "客户未提供 API 请求次数；仅展示 AWS 官方单位价，不计入月费合计。"
        if not requested.get("api_type"):
            default_note = "客户未指定 API 类型；单位参考价按成本较低的 HTTP API 展示。"
            notice = f"{default_note}{notice or ''}"
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model="REST API" if is_rest else "HTTP API",
            architecture=(f"每月 {requests:g} 次请求" if requests else "官方请求单位参考价"),
            specifications={
                "apiType": "REST" if is_rest else "HTTP",
                **({"requests": requests} if requests else {}),
                **(
                    {
                        "requestSizeMB": request_size_mb,
                        "billedUnitsPerRequest": payload_units,
                    }
                    if request_size_mb is not None
                    else {}
                ),
            },
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale="使用 API Gateway 官方请求计费维度。",
            substitution_notice=notice,
            usage_lines=lines,
            reference_rates=references,
            applied_requirement_fields=(
                ["request_size_mb"]
                if request_size_mb is not None and is_rest
                else []
            ),
        )


class EventBridgeSchedulerPlugin(_NoConfirmationPlugin):
    kind = ServiceKind.SCHEDULER
    display_name = "Amazon EventBridge Scheduler"

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        requested = requirement.requirements
        products = [
            product
            for product in self.catalog.products(
                "AWSEvents",
                {"regionCode": region, "operation": "Invocation"},
                max_pages=3,
            )
            if str(PricingCatalog.attributes(product).get("usagetype") or "").endswith(
                "ScheduledInvocation"
            )
        ]
        product = PricingCatalog.require_unique(
            products, context=f"EventBridge Scheduler 调用 ({region})"
        )
        invocations = None
        invocation_field = None
        for key in ("scheduled_invocations", "invocations", "requests"):
            invocations = required_float(requested, key)
            if invocations is not None:
                invocation_field = key
                break
        lines = [
            _usage(
                product,
                "schedule",
                invocations,
                "scheduler",
                source_fields=(invocation_field,) if invocation_field else (),
            )
        ] if invocations else []
        references = [] if invocations else [_reference(product, "Scheduler 调用单价（含免费层）")]
        schedules = required_float(requested, "schedules")
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model="EventBridge Scheduler",
            architecture=(
                f"每月 {invocations:g} 次计划调用" if invocations else "官方调用单位参考价"
            ),
            specifications={
                **({"scheduledInvocations": invocations} if invocations else {}),
                **({"schedules": schedules} if schedules is not None else {}),
            },
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale="使用 EventBridge Scheduler ScheduledInvocation 官方计费维度。",
            substitution_notice=(
                "客户未提供计划调用次数；仅展示 AWS 官方单位价及免费层，不计入月费合计。"
                if invocations is None
                else None
            ),
            usage_lines=lines,
            reference_rates=references,
            # AWS bills scheduled invocations, not the number of configured
            # schedules. Preserve an explicit schedule count as topology
            # context so the fact ledger does not mistake it for a lost meter.
            applied_requirement_fields=(
                ["schedules"] if schedules is not None else []
            ),
        )
