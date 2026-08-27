from __future__ import annotations

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


def _usage(product: dict[str, Any], key: str, amount: float, group: str) -> UsageLine:
    service_code, usage_type, operation = PricingCatalog.billing_identity(product)
    return UsageLine(
        key=key,
        service_code=service_code,
        usage_type=usage_type,
        operation=operation,
        amount=amount,
        group=group,
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

    def configuration_candidates(
        self, requirement: ServiceRequirement, default_region: str
    ) -> list[CandidateOption]:
        """Return every regional provisioned Broker shape for edit controls."""

        region = requirement.region or default_region
        products = self.catalog.products(
            "AmazonMSK",
            {"regionCode": region, "group": "Broker", "operation": "RunBroker"},
            max_pages=10,
        )
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
        broker_count = int(required_float(requested, "broker_count") or 1)
        requested_model = str(requested.get("requested_model") or "").strip().lower()
        min_vcpu = required_float(requested, "vcpu")
        min_memory = required_float(requested, "memory_gib")

        broker_products = self.catalog.products(
            "AmazonMSK",
            {"regionCode": region, "group": "Broker", "operation": "RunBroker"},
            max_pages=10,
        )
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
        if (min_vcpu is not None or min_memory is not None) and product_models:
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
        storage_products = [
            product
            for product in self.catalog.products(
                "AmazonMSK",
                {"regionCode": region, "group": "Storage", "operation": "RunVolume"},
                max_pages=4,
            )
            if str(PricingCatalog.attributes(product).get("usagetype") or "").endswith(
                "Kafka.Storage.GP2"
            )
        ]
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
                lines.append(_usage(message_product, "apigwmsg", messages, "api-gateway"))
            else:
                references.append(_reference(message_product, "WebSocket 消息单价"))
            if connection_minutes is not None:
                lines.append(
                    _usage(
                        minute_product,
                        "apigwmin",
                        connection_minutes,
                        "api-gateway",
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
        lines = [_usage(product, "apigw", requests, "api-gateway")] if requests else []
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
            },
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale="使用 API Gateway 官方请求计费维度。",
            substitution_notice=notice,
            usage_lines=lines,
            reference_rates=references,
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
        for key in ("scheduled_invocations", "invocations", "requests"):
            invocations = required_float(requested, key)
            if invocations is not None:
                break
        lines = [_usage(product, "schedule", invocations, "scheduler")] if invocations else []
        references = [] if invocations else [_reference(product, "Scheduler 调用单价（含免费层）")]
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model="EventBridge Scheduler",
            architecture=(
                f"每月 {invocations:g} 次计划调用" if invocations else "官方调用单位参考价"
            ),
            specifications=({"scheduledInvocations": invocations} if invocations else {}),
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale="使用 EventBridge Scheduler ScheduledInvocation 官方计费维度。",
            substitution_notice=(
                "客户未提供计划调用次数；仅展示 AWS 官方单位价及免费层，不计入月费合计。"
                if invocations is None
                else None
            ),
            usage_lines=lines,
            reference_rates=references,
        )
