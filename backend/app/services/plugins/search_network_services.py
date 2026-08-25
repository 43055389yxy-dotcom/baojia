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
from app.domain.requirement_fields import canonicalize_requirement_fields
from app.integrations.aws import PricingCatalog, parse_number
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


class OpenSearchPlugin(_NoConfirmationPlugin):
    kind = ServiceKind.OPENSEARCH
    display_name = "Amazon OpenSearch Service"

    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        """Require a regional catalog choice when the customer omitted a model."""

        requested = canonicalize_requirement_fields(requirement.requirements, service="opensearch")
        requested_model = str(requested.get("requested_model") or "").strip().lower()
        min_vcpu = required_float(requested, "vcpu")
        min_memory = required_float(requested, "memory_gib")
        if requested_model:
            return super().preview(requirement, default_region)

        region = requirement.region or default_region
        products = self.catalog.products(
            "AmazonES",
            {
                "regionCode": region,
                "productFamily": "Amazon OpenSearch Service Instance",
            },
            max_pages=20,
        )
        shapes: list[tuple[float, str, float, float, dict[str, Any]]] = []
        for product in products:
            attrs = PricingCatalog.attributes(product)
            model = str(attrs.get("instanceType") or "").lower()
            if not model or attrs.get("vcpu") in (None, "") or attrs.get("memoryGib") in (None, ""):
                continue
            try:
                vcpu = parse_number(attrs["vcpu"], field="vcpu")
                memory = parse_number(attrs["memoryGib"], field="memoryGib")
            except ManualConfirmationRequired:
                continue
            rate = PricingCatalog.on_demand_rate(product)
            if rate is not None:
                shapes.append((rate, model, vcpu, memory, product))

        if not shapes:
            return super().preview(requirement, default_region)

        def distance(item: tuple[float, str, float, float, dict[str, Any]]) -> float:
            _, _, vcpu, memory, _ = item
            return (abs(vcpu - min_vcpu) / max(min_vcpu, 1) if min_vcpu is not None else 0) + (
                abs(memory - min_memory) / max(min_memory, 1) if min_memory is not None else 0
            )

        available = sorted(
            shapes,
            key=(
                (
                    lambda item: (
                        not (
                            (min_vcpu is None or item[2] >= min_vcpu)
                            and (min_memory is None or item[3] >= min_memory)
                        ),
                        distance(item),
                        item[0],
                        item[1],
                    )
                )
                if min_vcpu is not None or min_memory is not None
                else (lambda item: (item[0], item[1]))
            ),
        )
        options: list[CandidateOption] = []
        seen_models: set[str] = set()
        data_nodes = int(
            required_float(requested, "data_nodes")
            or required_float(requested, "nodes")
            or requirement.quantity
        )
        for item in available:
            if item[1] in seen_models:
                continue
            rate, model, vcpu, memory, product = item
            seen_models.add(model)
            options.append(
                CandidateOption(
                    model=model,
                    family="opensearch",
                    specifications={"vCPU": vcpu, "memoryGiB": memory},
                    monthly_catalog_cost=(rate * requirement.hours_per_month * data_nodes),
                    rationale="AWS 官方目录中的可用 OpenSearch 节点规格。",
                    official_product={
                        "source": "AWS Price List",
                        "sku": product.get("product", {}).get("sku"),
                        "regionCode": region,
                    },
                    is_default=False,
                )
            )
        return PreviewSelection(
            component_id="component",
            service=self.kind,
            display_name=self.display_name,
            region=region,
            requested_model=None,
            selected_model=None,
            selection_reason="客户未指定节点型号，等待从部署区域的官方可用型号中选择。",
            candidates=options,
            requires_confirmation=True,
            confirmation_reason=(
                "请选择 OpenSearch 节点型号；列表仅展示当前部署区域可用的官方型号。"
            ),
        )

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        requested = canonicalize_requirement_fields(requirement.requirements, service="opensearch")
        requested_model = str(requested.get("requested_model") or "").strip().lower()
        min_vcpu = required_float(requested, "vcpu")
        min_memory = required_float(requested, "memory_gib")
        data_nodes = int(
            required_float(requested, "data_nodes")
            or required_float(requested, "nodes")
            or requirement.quantity
        )
        storage_gib = required_float(requested, "storage_gib_per_node") or required_float(
            requested, "storage_gib"
        )
        products = self.catalog.products(
            "AmazonES",
            {"regionCode": region, "productFamily": "Amazon OpenSearch Service Instance"},
            max_pages=20,
        )
        candidates: list[tuple[float, str, float, float, dict[str, Any]]] = []
        catalog_shapes: list[tuple[float, str, float, float, dict[str, Any]]] = []
        normalized_requested = (
            requested_model
            if not requested_model or requested_model.endswith(".search")
            else f"{requested_model}.search"
        )
        for product in products:
            attrs = PricingCatalog.attributes(product)
            model = str(attrs.get("instanceType") or "").lower()
            raw_vcpu = attrs.get("vcpu")
            raw_memory = attrs.get("memoryGib")
            # The OpenSearch "Instance" product family also contains a few
            # non-instance charges (for example DirectQuery OCU and Extended
            # Support).  They have no instanceType/vCPU/memory and must not
            # abort discovery of the real node SKUs that follow them.
            if not model or raw_vcpu in (None, "") or raw_memory in (None, ""):
                continue
            try:
                vcpu = parse_number(raw_vcpu, field="vcpu")
                memory = parse_number(raw_memory, field="memoryGib")
            except ManualConfirmationRequired:
                # One malformed catalog record is not evidence that the whole
                # AWS catalog or the customer's requirement is invalid.
                continue
            rate = PricingCatalog.on_demand_rate(product)
            if rate is None:
                continue
            catalog_shapes.append((rate, model, vcpu, memory, product))
            if normalized_requested and model != normalized_requested:
                continue
            if min_vcpu is not None and vcpu < min_vcpu:
                continue
            if min_memory is not None and memory < min_memory:
                continue
            candidates.append((rate, model, vcpu, memory, product))
        substituted = False
        if not candidates and normalized_requested:
            candidates = [
                item
                for item in catalog_shapes
                if (min_vcpu is None or item[2] >= min_vcpu)
                and (min_memory is None or item[3] >= min_memory)
            ]
            substituted = bool(candidates)
        if not candidates:

            def distance(
                item: tuple[float, str, float, float, dict[str, Any]],
            ) -> tuple[bool, float, float, str]:
                rate, model, vcpu, memory, _ = item
                underprovisioned = not (
                    (min_vcpu is None or vcpu >= min_vcpu)
                    and (min_memory is None or memory >= min_memory)
                )
                shape_distance = (
                    abs(vcpu - min_vcpu) / max(min_vcpu, 1) if min_vcpu is not None else 0
                ) + (abs(memory - min_memory) / max(min_memory, 1) if min_memory is not None else 0)
                return (underprovisioned, shape_distance, rate, model)

            nearby_candidates: list[dict[str, Any]] = []
            seen_models: set[str] = set()
            for rate, model, vcpu, memory, product in sorted(catalog_shapes, key=distance):
                if model in seen_models:
                    continue
                seen_models.add(model)
                nearby_candidates.append(
                    {
                        "model": model,
                        "family": "opensearch",
                        "vcpu": vcpu,
                        "memory_gib": memory,
                        "monthly_catalog_cost": (rate * requirement.hours_per_month * data_nodes),
                        "rationale": "当前区域官方目录中的相近 OpenSearch 节点规格。",
                        "official_product": {
                            "source": "AWS Price List",
                            "sku": product.get("product", {}).get("sku"),
                            "regionCode": region,
                        },
                    }
                )
                if len(nearby_candidates) >= 20:
                    break
            raise ManualConfirmationRequired(
                "AWS 官方目录没有返回符合要求的 OpenSearch 节点规格",
                code="opensearch_specification_not_found",
                nearby_candidates=nearby_candidates,
            )
        _, model, vcpu, memory, instance_product = min(
            candidates, key=lambda item: (item[0], item[1])
        )
        lines = [
            _usage(
                instance_product,
                "osnode",
                data_nodes * requirement.hours_per_month,
                "opensearch",
            )
        ]
        references: list[ReferenceRate] = []
        storage_product: dict[str, Any] | None = None
        if storage_gib is not None:
            storage_products = self.catalog.products(
                "AmazonES",
                {"regionCode": region, "productFamily": "Amazon OpenSearch Service Volume"},
                max_pages=5,
            )
            desired_media = str(requested.get("volume_type") or "gp3").upper()
            storage_product = next(
                (
                    product
                    for product in storage_products
                    if str(PricingCatalog.attributes(product).get("storageMedia") or "").upper()
                    == desired_media
                ),
                None,
            )
            if storage_product is None:
                raise ManualConfirmationRequired(
                    "AWS 官方目录没有返回 OpenSearch EBS 存储计费项",
                    code="opensearch_storage_not_found",
                )
            lines.append(_usage(storage_product, "osstore", data_nodes * storage_gib, "opensearch"))
        notice = None
        if substituted:
            notice = (
                f"客户指定的 {requested_model} 在当前区域不可报价；已在相同或不低于原配置且"
                f"可报价的节点中，自动替换为最低价的 {model}。"
            )
        elif not requested_model:
            requested_shape: list[str] = []
            if min_vcpu is not None:
                requested_shape.append(f"{min_vcpu:g} vCPU")
            if min_memory is not None:
                requested_shape.append(f"{min_memory:g} GiB 内存")
            if storage_gib is not None:
                requested_shape.append(f"{storage_gib:g} GiB 存储/节点")
            shape_text = "、".join(requested_shape) or "客户已提供的节点规格"
            notice = (
                "客户未指定 Amazon OpenSearch Service 数据节点型号；"
                f"已按每节点 {shape_text} 的要求，从 AWS 官方目录选择满足条件且"
                f"小时价最低的 {model}。"
            )
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model=model,
            architecture=f"{data_nodes} 个数据节点",
            specifications={
                "dataNodes": data_nodes,
                "vCPU": vcpu,
                "memoryGiB": memory,
                **({"storageGiBPerNode": storage_gib} if storage_gib is not None else {}),
            },
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale="按 OpenSearch 节点小时费和每节点 EBS 存储费提交 BCM。",
            substitution_notice=notice,
            usage_lines=lines,
            reference_rates=references,
        )


class NatGatewayPlugin(_NoConfirmationPlugin):
    kind = ServiceKind.NAT_GATEWAY
    display_name = "AWS NAT Gateway"

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        products = self.catalog.products(
            "AmazonEC2",
            {"regionCode": region, "productFamily": "NAT Gateway"},
            max_pages=3,
        )
        hourly = next(
            (
                product
                for product in products
                if str(PricingCatalog.attributes(product).get("usagetype") or "").endswith(
                    "-NatGateway-Hours"
                )
            ),
            None,
        )
        processed = next(
            (
                product
                for product in products
                if str(PricingCatalog.attributes(product).get("usagetype") or "").endswith(
                    "-NatGateway-Bytes"
                )
            ),
            None,
        )
        if hourly is None or processed is None:
            raise ManualConfirmationRequired(
                "AWS 官方目录没有返回 NAT Gateway 标准计费项",
                code="nat_gateway_pricing_not_found",
            )
        quantity = requirement.quantity
        data_gib = None
        for key in ("data_processed_gib", "processed_bytes_gib", "data_transfer_gib"):
            data_gib = required_float(requirement.requirements, key)
            if data_gib is not None:
                break
        lines = [_usage(hourly, "nathour", quantity * requirement.hours_per_month, "nat")]
        references: list[ReferenceRate] = []
        if data_gib is None:
            references.append(_reference(processed, "NAT Gateway 每 GB 数据处理单价"))
        else:
            lines.append(_usage(processed, "natbytes", data_gib, "nat"))
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model="NAT Gateway",
            architecture=f"{quantity} 个 NAT Gateway",
            specifications={
                "quantity": quantity,
                **({"processedGiB": data_gib} if data_gib is not None else {}),
            },
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale="按 NAT Gateway 小时费和数据处理费提交 BCM。",
            substitution_notice=(
                "客户未提供处理流量；已计入网关小时费，流量仅展示官方每 GB 单价。"
                if data_gib is None
                else None
            ),
            usage_lines=lines,
            reference_rates=references,
        )
