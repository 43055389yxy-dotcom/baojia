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
from app.integrations.aws import PricingCatalog
from app.services.aws_query_executor import ReadOnlyAwsQueryExecutor
from app.services.plugins.base import ServicePlugin, required_float


class Ec2Plugin(ServicePlugin):
    kind = ServiceKind.EC2
    display_name = "Amazon EC2"
    _candidate_cache: dict[
        tuple[str, str | None, float | None, float | None], list[dict[str, Any]]
    ] = {}

    def specified_model_compatibility_notice(
        self, requirement: ServiceRequirement, default_region: str
    ) -> str | None:
        """Return a customer-facing conflict found from official EC2 metadata.

        A named instance can exist in a region while still being incompatible
        with the requested operating system.  Detect that before any Price List
        lookup so an unsupported combination never leaks out as a catalog error.
        """

        requested = canonicalize_requirement_fields(requirement.requirements, service="ec2")
        requested_model = _optional_string(requested.get("requested_model"))
        operating_system = _pricing_operating_system(
            _optional_string(requested.get("operating_system"))
        )
        requested_architecture = (_optional_string(requested.get("architecture")) or "").casefold()
        if operating_system == "Windows" and requested_architecture in {
            "arm",
            "arm64",
            "aarch64",
        }:
            return (
                "Windows Server 不能运行在 ARM64 EC2 上。"
                "请把处理器架构改为 x86_64，或保留 ARM64 并改用 Linux。"
            )
        if not requested_model or operating_system != "Windows":
            return None

        region = requirement.region or default_region
        exact = self._official_candidates(region, requested_model)
        selected = next((item for item in exact if item["model"] == requested_model), None)
        if selected is None:
            return None
        architectures = {str(value).casefold() for value in selected.get("architectures", [])}
        if "x86_64" in architectures:
            return None

        compatible_model = self.compatible_x86_model(requirement, default_region)
        alternative_text = f"（例如 {compatible_model}）" if compatible_model else ""
        return (
            f"您指定 {requested_model} 并要求 Windows Server；该型号是 ARM 架构，"
            f"不支持 Windows。请确认：改用 Linux 保留 {requested_model}，还是保留 Windows "
            f"并改选同规格 x86 型号{alternative_text}？"
        )

    def compatible_x86_model(
        self, requirement: ServiceRequirement, default_region: str
    ) -> str | None:
        requested = canonicalize_requirement_fields(requirement.requirements, service="ec2")
        requested_model = _optional_string(requested.get("requested_model"))
        if not requested_model:
            return None
        region = requirement.region or default_region
        exact = self._official_candidates(region, requested_model)
        selected = next((item for item in exact if item["model"] == requested_model), None)
        if selected is None:
            return None
        alternatives = self._official_candidates(
            region,
            None,
            float(selected["vcpu"]),
            float(selected["memory_gib"]),
        )
        compatible = [
            item
            for item in alternatives
            if "x86_64" in {str(value).casefold() for value in item.get("architectures", [])}
        ]
        if not compatible:
            return None

        def generation(model: str) -> int:
            match = re.match(r"^[a-z]+(\d+)", model)
            return int(match.group(1)) if match else 0

        compatible.sort(
            key=lambda item: (
                item.get("family") != selected.get("family"),
                not bool(item.get("current_generation")),
                -generation(str(item["model"])),
                str(item["model"]),
            )
        )
        return str(compatible[0]["model"])

    def nearest_shape_options(
        self, requirement: ServiceRequirement, default_region: str
    ) -> list[dict[str, object]]:
        requested = canonicalize_requirement_fields(requirement.requirements, service="ec2")
        if _optional_string(requested.get("requested_model")):
            return []
        requested_vcpu = required_float(requested, "vcpu")
        requested_memory = required_float(requested, "memory_gib")
        if requested_vcpu is None or requested_memory is None:
            return []

        candidates = self._official_candidates(
            requirement.region or default_region,
            None,
            requested_vcpu,
            requested_memory,
        )
        suitable = [
            item
            for item in candidates
            if item["family"] in {"general_purpose", "compute_optimized", "memory_optimized"}
        ]
        current = [item for item in suitable if item["current_generation"]]
        pool = current or suitable or candidates
        if any(
            item["vcpu"] == requested_vcpu and item["memory_gib"] == requested_memory
            for item in pool
        ):
            return []

        def distance(item: dict[str, Any]) -> float:
            return abs(item["vcpu"] - requested_vcpu) / max(requested_vcpu, 1) + abs(
                item["memory_gib"] - requested_memory
            ) / max(requested_memory, 1)

        unique: dict[str, dict[str, Any]] = {}
        for item in sorted(pool, key=lambda candidate: (distance(candidate), candidate["model"])):
            unique.setdefault(str(item["model"]).casefold(), item)
        return [
            {
                "label": "当前区域可用配置",
                "vcpu": item["vcpu"],
                "memory_gib": item["memory_gib"],
                "example_model": item["model"],
            }
            for item in unique.values()
        ]

    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        region = requirement.region or default_region
        requested = canonicalize_requirement_fields(requirement.requirements, service="ec2")
        requested_model = _optional_string(requested.get("requested_model"))
        min_vcpu = required_float(requested, "vcpu")
        min_memory = required_float(requested, "memory_gib")
        official = self._official_candidates(region, requested_model, min_vcpu, min_memory)
        eligible = [item for item in official if _fits(item, min_vcpu, min_memory)]
        if not eligible and requested_model:
            eligible = official
        if not eligible:
            raise ManualConfirmationRequired(
                "AWS 官方规格中没有满足 EC2 需求的候选型号",
                code="ec2_specification_not_found",
            )
        eligible = _rank_reasonable_ec2(
            eligible,
            business_type=_optional_string(requested.get("business_type")) or "general_purpose",
            architecture=_optional_string(requested.get("architecture")),
            min_vcpu=min_vcpu,
            min_memory=min_memory,
        )
        # If the requested CPU/memory shape does not exist, this page is an
        # explicit replacement picker, not an automatic minimum-size match.
        # Keep smaller regional models visible as well so the customer can
        # deliberately revise the original requirement (for example from
        # 6 vCPU down to 2 or 4 vCPU). Normal automatic selection below still
        # uses only ``eligible`` models and never under-provisions silently.
        exact_shape_exists = bool(
            min_vcpu is not None
            and min_memory is not None
            and any(
                item["vcpu"] == min_vcpu and item["memory_gib"] == min_memory for item in official
            )
        )
        candidate_items = eligible
        if requested_model is None and not exact_shape_exists:
            candidate_items = _rank_reasonable_ec2(
                official,
                business_type=_optional_string(requested.get("business_type")) or "general_purpose",
                architecture=_optional_string(requested.get("architecture")),
                min_vcpu=min_vcpu,
                min_memory=min_memory,
            )
        operating_system = _pricing_operating_system(
            _optional_string(requested.get("operating_system"))
        )
        tenancy = _pricing_tenancy(_optional_string(requested.get("tenancy")))
        options: list[CandidateOption] = []
        price_list_enabled = True
        price_checked_models: set[str] = set()
        billable_models: set[str] = set()
        for item in candidate_items:
            product = None
            # Smaller replacement choices do not need a speculative price at
            # this stage. Once selected, the normal quote pass prices that
            # exact official model.
            if price_list_enabled and _fits(item, min_vcpu, min_memory):
                try:
                    price_checked_models.add(str(item["model"]))
                    product = self._compute_product(
                        region, item["model"], operating_system, tenancy
                    )
                    if product is not None:
                        billable_models.add(str(item["model"]))
                except ManualConfirmationRequired:
                    # EC2 Price List is optional in the Calculator pipeline. Once it
                    # fails, finish candidate selection from EC2 API specifications.
                    price_list_enabled = False
            hourly = PricingCatalog.on_demand_rate(product) if product else None
            monthly = (
                hourly * requirement.hours_per_month * requirement.quantity
                if hourly is not None
                else None
            )
            attrs = PricingCatalog.attributes(product) if product else {}
            billing = _optional_billing_identity(product)
            options.append(
                CandidateOption(
                    model=item["model"],
                    family=item["family"],
                    specifications={
                        "vCPU": item["vcpu"],
                        "memoryGiB": item["memory_gib"],
                        "operatingSystem": operating_system,
                        "tenancy": tenancy,
                    },
                    monthly_catalog_cost=monthly,
                    rationale=_candidate_rationale(
                        item, business_type=_optional_string(requested.get("business_type"))
                    ),
                    official_product={
                        "source": "EC2 DescribeInstanceTypes",
                        "sku": product.get("product", {}).get("sku") if product else None,
                        "usageType": billing[1] if billing else None,
                        "operation": billing[2] if billing else None,
                        "regionCode": attrs.get("regionCode"),
                    },
                )
            )
        # A model can be present in DescribeInstanceTypes yet have no billable
        # product for the edited OS/tenancy combination (Mac + Windows is a
        # common example).  Never persist such a model as "ready" and discover
        # the problem only after the customer approves the entire quote.
        if (
            price_checked_models
            and not billable_models
            and (requested_model is not None or exact_shape_exists)
        ):
            raise ManualConfirmationRequired(
                "当前 EC2 规格与所选操作系统、处理器架构或租用方式没有官方计费项",
                code="ec2_configuration_not_billable",
                region=region,
                requested_model=requested_model,
                operating_system=operating_system,
                tenancy=tenancy,
            )
        if billable_models:
            options = [
                option
                for option in options
                if option.model not in price_checked_models or option.model in billable_models
            ]
        exact = next((option for option in options if option.model == requested_model), None)
        if requested_model and exact is None:
            options[
                0
            ].rationale = (
                f"客户指定型号 {requested_model} 不可用，以下为满足规格且区域可用的替代候选。"
            )
        elif requested_model:
            options = [exact]
        else:
            options.sort(
                key=lambda option: (
                    option.monthly_catalog_cost is None,
                    option.monthly_catalog_cost or float("inf"),
                    option.model,
                )
            )
        priced_defaults = [option for option in options if option.monthly_catalog_cost is not None]
        default_model = (
            min(
                priced_defaults,
                key=lambda option: (option.monthly_catalog_cost or float("inf"), option.model),
            ).model
            if priced_defaults
            else eligible[0]["model"]
        )
        default = next(option for option in options if option.model == default_model)
        default.is_default = True
        # Multiple official models can share the exact requested CPU/memory
        # shape. That means AWS *does* support the configuration; select the
        # cheapest ranked exact model automatically instead of incorrectly
        # telling the customer that the shape does not exist. Ask for a
        # replacement only when the requested shape itself is unavailable.
        customer_must_select = bool(requirement.field_sources.get("_customer_select_configuration"))
        # Catalog sizing is deterministic: choose the cheapest official model
        # that does not under-provision the customer's CPU/memory. The only
        # remaining picker is an explicit architecture workflow (for example
        # a newly derived self-hosted child whose machine size was never given).
        requires_confirmation = customer_must_select
        confirmation_reason = None
        if customer_must_select:
            confirmation_reason = (
                f"请选择自建服务的机器台数和每台 EC2 配置（当前 {requirement.quantity} 台）。"
            )
        elif requires_confirmation:
            confirmation_reason = (
                f"客户要求至少 {min_vcpu:g} vCPU、{min_memory:g} GiB，AWS 合适系列中"
                f"最接近的规格是 {default.specifications.get('vCPU'):g} vCPU、"
                f"{default.specifications.get('memoryGiB'):g} GiB。"
                if min_vcpu is not None and min_memory is not None
                else "推荐规格与客户原始要求不是完全匹配，请您确认。"
            )
        return PreviewSelection(
            component_id="component",
            service=self.kind,
            display_name=self.display_name,
            region=region,
            requested_model=requested_model,
            selected_model=None if requires_confirmation else default.model,
            selection_reason=(
                "客户指定型号已确认可用，直接采用。"
                if requested_model and exact
                else (
                    f"客户指定型号 {requested_model} 在当前区域不可用；已在相同或不低于原配置、"
                    "且可取得官方价格的型号中自动选择最低价。"
                    if requested_model
                    else "先按业务类型与官方规格筛选；能取得目录价时仅用它辅助排序。"
                )
            ),
            candidates=options,
            requires_confirmation=requires_confirmation,
            confirmation_reason=confirmation_reason,
        )

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        requested = canonicalize_requirement_fields(requirement.requirements, service="ec2")
        requested_model = _optional_string(requested.get("requested_model"))
        min_vcpu = required_float(requested, "vcpu")
        min_memory = required_float(requested, "memory_gib")

        operating_system = _pricing_operating_system(
            _optional_string(requested.get("operating_system"))
        )
        tenancy = _pricing_tenancy(_optional_string(requested.get("tenancy")))
        purchase_option = _optional_string(requested.get("purchase_option")) or "on_demand"
        candidates = self._official_candidates(region, requested_model, min_vcpu, min_memory)
        # A model explicitly chosen on the confirmation page is authoritative.
        # Later edits such as changing quantity may restore descriptive CPU or
        # memory from the original request; those old values must not reject or
        # silently replace the customer's exact official model.
        confirmed = next(
            (item for item in candidates if item["model"] == requested_model),
            None,
        )
        eligible = [item for item in candidates if _fits(item, min_vcpu, min_memory)]
        substitution = False
        if requested_model and confirmed:
            selected = confirmed
        else:
            if requested_model:
                substitution = True
            eligible = _rank_reasonable_ec2(
                eligible,
                business_type=_optional_string(requested.get("business_type")) or "general_purpose",
                architecture=_optional_string(requested.get("architecture")),
                min_vcpu=min_vcpu,
                min_memory=min_memory,
            )
            priced: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
            for candidate in eligible[:40]:
                product = self._compute_product(
                    region, candidate["model"], operating_system, tenancy
                )
                rate = PricingCatalog.on_demand_rate(product) if product else None
                if product is not None and rate is not None:
                    priced.append((rate, candidate, product))
            if not priced:
                raise ManualConfirmationRequired(
                    "AWS 官方目录没有返回可提交 BCM 的 EC2 计费项",
                    code="ec2_billing_product_not_found",
                )
            _, selected, compute_product = min(priced, key=lambda item: (item[0], item[1]["model"]))

        reserved_years = (
            int(requested.get("reserved_term_years") or 1)
            if purchase_option in {"standard_reserved", "convertible_reserved"}
            else None
        )
        reserved_payment = (
            _optional_string(requested.get("payment_option")) or "no_upfront"
            if reserved_years is not None
            else None
        )
        reserved_offering_class = (
            "convertible"
            if purchase_option == "convertible_reserved"
            else "standard"
            if purchase_option == "standard_reserved"
            else None
        )
        compute_product = self._compute_product(
            region,
            selected["model"],
            operating_system,
            tenancy,
            reserved_years=reserved_years,
            payment_option=reserved_payment,
            offering_class=reserved_offering_class,
            hours_per_month=requirement.hours_per_month,
        )
        if compute_product is None:
            raise ManualConfirmationRequired(
                "AWS 官方目录没有返回所选 EC2 型号的计费项",
                code="ec2_billing_product_not_found",
            )
        service_code, usage_type, operation = PricingCatalog.billing_identity(compute_product)
        monthly_commitment_cost = 0.0
        upfront_commitment_cost = 0.0
        usage_lines: list[UsageLine] = []
        if purchase_option == "on_demand":
            utilization_percent = required_float(requested, "utilization_percent") or 100.0
            if utilization_percent > 100:
                raise ManualConfirmationRequired(
                    "EC2 使用率不能超过 100%",
                    code="invalid_requirement",
                    field="utilization_percent",
                )
            usage_lines.append(
                UsageLine(
                    key="ec2",
                    service_code=service_code,
                    usage_type=usage_type,
                    operation=operation,
                    amount=(
                        requirement.quantity
                        * requirement.hours_per_month
                        * utilization_percent
                        / 100.0
                    ),
                    group="ec2",
                    source_fields=[
                        "quantity",
                        "hours_per_month",
                        "requested_model",
                        "vcpu",
                        "memory_gib",
                        "operating_system",
                        "tenancy",
                        "purchase_option",
                        "utilization_percent",
                    ],
                )
            )
        elif purchase_option in {"standard_reserved", "convertible_reserved"}:
            reserved = PricingCatalog.reserved_price(
                compute_product,
                years=int(requested.get("reserved_term_years") or 1),
                payment_option=_optional_string(requested.get("payment_option")) or "no_upfront",
                offering_class=(
                    "convertible" if purchase_option == "convertible_reserved" else "standard"
                ),
                hours_per_month=requirement.hours_per_month,
            )
            monthly_commitment_cost = reserved.monthly_amortized * requirement.quantity
            upfront_commitment_cost = reserved.upfront * requirement.quantity
        else:
            raise ManualConfirmationRequired(
                f"EC2 购买方式 {purchase_option!r} 尚不支持官方核价",
                code="unsupported_purchase_option",
            )
        disk_gib = required_float(requested, "system_disk_gib")
        if disk_gib is not None:
            volume_api_name = _optional_string(requested.get("volume_type")) or "gp3"
            usage_lines.append(
                self._ebs_storage_usage(
                    region=region,
                    volume_type=volume_api_name,
                    amount=requirement.quantity * disk_gib,
                    key="ebs",
                    source_fields=(
                        "quantity",
                        "system_disk_gib",
                        "total_system_disk_gib",
                        "volume_type",
                    ),
                )
            )
        additional_volumes = requested.get("additional_ebs_volumes")
        if isinstance(additional_volumes, list):
            for index, volume in enumerate(additional_volumes, start=1):
                if not isinstance(volume, dict):
                    continue
                size_gib = required_float(volume, "size_gib")
                if size_gib is None:
                    continue
                count = required_float(volume, "count_per_instance") or 1
                volume_type = _optional_string(volume.get("volume_type")) or "gp3"
                usage_lines.append(
                    self._ebs_storage_usage(
                        region=region,
                        volume_type=volume_type,
                        amount=requirement.quantity * count * size_gib,
                        key=f"ebs{index + 1}",
                        source_fields=("quantity", "additional_ebs_volumes"),
                    )
                )
        transfer_out_gib = required_float(requested, "data_transfer_out_gib")
        if transfer_out_gib is not None:
            transfer_products = self.catalog.products(
                "AWSDataTransfer",
                {
                    "fromLocation": self.catalog.location(region),
                    "toLocation": "External",
                    "transferType": "AWS Outbound",
                },
                max_pages=3,
            )
            transfer_product = PricingCatalog.require_unique(
                transfer_products, context=f"公网出站流量 ({region})"
            )
            transfer_service, transfer_usage, transfer_operation = PricingCatalog.billing_identity(
                transfer_product
            )
            usage_lines.append(
                UsageLine(
                    key="ec2out",
                    service_code=transfer_service,
                    usage_type=transfer_usage,
                    operation=transfer_operation,
                    amount=transfer_out_gib,
                    group="ec2-transfer",
                    source_fields=[
                        "data_transfer_out_gib",
                        "data_transfer_out_gib_per_instance",
                        "quantity",
                    ],
                )
            )

        notice = None
        if substitution:
            notice = (
                f"客户指定的 {requested_model} 在当前区域不可用或与配置冲突；系统已在"
                f"相同或不低于原配置且可报价的型号中，自动替换为最低价的 {selected['model']}。"
            )
        elif min_memory is not None and selected["memory_gib"] > min_memory:
            notice = (
                f"AWS 没有恰好 {min_memory:g} GiB 的匹配实例，已选择最接近且不低于需求的 "
                f"{selected['memory_gib']:g} GiB。"
            )

        storage_description = _optional_string(requested.get("ebs_storage_breakdown"))
        if storage_description is None and isinstance(additional_volumes, list):
            descriptions = []
            for volume in additional_volumes:
                if not isinstance(volume, dict):
                    continue
                size = required_float(volume, "size_gib")
                count = required_float(volume, "count_per_instance") or 1
                if size is not None:
                    descriptions.append(
                        f"{count:g} 块 × {size:g} GiB "
                        f"{_optional_string(volume.get('volume_type')) or 'gp3'}"
                    )
            if descriptions:
                storage_description = "每台额外数据盘：" + "；".join(descriptions)
        if not storage_description and disk_gib is not None:
            storage_description = f"每台 {disk_gib:g} GiB 系统盘"

        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model=selected["model"],
            architecture=(
                f"{requirement.quantity} 台 {purchase_option} 实例"
                + (f"；{storage_description}" if storage_description else "")
            ),
            specifications={
                "vCPU": selected["vcpu"],
                "memoryGiB": selected["memory_gib"],
                "operatingSystem": operating_system,
                "tenancy": tenancy,
            },
            official_product={
                "source": "EC2 DescribeInstanceTypes + AWS Price List",
                "regionCode": region,
                "sku": compute_product.get("product", {}).get("sku"),
                "usageType": usage_type,
                "operation": operation,
            },
            rationale=(
                "客户指定型号已通过官方目录确认。"
                if requested_model and not substitution
                else "先排除不适合业务的系列，再在满足规格的官方按需产品中选择最低价型号。"
            ),
            substitution_notice=notice,
            usage_lines=usage_lines,
            applied_requirement_fields=(
                [
                    "quantity",
                    "hours_per_month",
                    "requested_model",
                    "vcpu",
                    "memory_gib",
                    "operating_system",
                    "tenancy",
                    "purchase_option",
                    "reserved_term_years",
                    "payment_option",
                ]
                if purchase_option in {"standard_reserved", "convertible_reserved"}
                else []
            ),
            monthly_commitment_cost=monthly_commitment_cost,
            upfront_commitment_cost=upfront_commitment_cost,
        )

    def _ebs_storage_usage(
        self,
        *,
        region: str,
        volume_type: str,
        amount: float,
        key: str,
        source_fields: tuple[str, ...],
    ) -> UsageLine:
        storage_products = self.catalog.products(
            "AmazonEC2",
            {
                "regionCode": region,
                "productFamily": "Storage",
                "volumeApiName": volume_type,
            },
            max_pages=3,
        )
        storage_product = PricingCatalog.require_unique(
            storage_products, context=f"EBS {volume_type} 存储 ({region})"
        )
        service_code, usage_type, operation = PricingCatalog.billing_identity(storage_product)
        return UsageLine(
            key=key,
            service_code=service_code,
            usage_type=usage_type,
            operation=operation,
            amount=amount,
            group="ec2-storage",
            source_fields=list(source_fields),
        )

    def _compute_product(
        self,
        region: str,
        model: str,
        operating_system: str,
        tenancy: str,
        *,
        reserved_years: int | None = None,
        payment_option: str | None = None,
        offering_class: str | None = None,
        hours_per_month: float = 730,
    ) -> dict[str, Any] | None:
        products = self.catalog.products(
            "AmazonEC2",
            {
                "regionCode": region,
                "productFamily": "Compute Instance",
                "instanceType": model,
                "operatingSystem": operating_system,
                "tenancy": tenancy,
                "preInstalledSw": "NA",
                "capacitystatus": "Used",
            },
        )
        if not products:
            return None

        # Price List can return several records for the same EC2 model, OS and
        # tenancy.  Some are on-demand-only operation variants while another
        # record owns the Reserved terms.  Selecting by SKU used to pick an
        # arbitrary on-demand-only record and falsely report that a valid
        # 1-year/3-year Reserved offer did not exist.  For a Reserved quote,
        # bind the product record to the exact commercial terms first.
        if reserved_years is not None and payment_option is not None:
            reserved_products: list[tuple[float, str, dict[str, Any]]] = []
            for product in products:
                try:
                    price = PricingCatalog.reserved_price(
                        product,
                        years=reserved_years,
                        payment_option=payment_option,
                        offering_class=offering_class,
                        hours_per_month=hours_per_month,
                    )
                except ManualConfirmationRequired as exc:
                    if exc.code in {
                        "reserved_term_not_found",
                        "reserved_price_dimensions_missing",
                    }:
                        continue
                    raise
                reserved_products.append(
                    (
                        price.monthly_amortized,
                        str(product.get("product", {}).get("sku") or ""),
                        product,
                    )
                )
            if reserved_products:
                return min(reserved_products, key=lambda item: (item[0], item[1]))[2]

        identities = {
            identity: product
            for product in products
            if (identity := _optional_billing_identity(product)) is not None
        }
        if identities:
            return sorted(
                identities.values(), key=lambda item: item.get("product", {}).get("sku", "")
            )[0]
        run_instances = [
            product
            for product in products
            if PricingCatalog.attributes(product).get("operation") == "RunInstances"
        ]
        if len(run_instances) == 1:
            return run_instances[0]
        return sorted(products, key=lambda item: item.get("product", {}).get("sku", ""))[0]

    def _official_candidates(
        self,
        region: str,
        requested_model: str | None,
        requested_vcpu: float | None = None,
        requested_memory: float | None = None,
    ) -> list[dict[str, Any]]:
        cache_key = (region, requested_model, requested_vcpu, requested_memory)
        cached = self._candidate_cache.get(cache_key)
        if cached is not None:
            return [dict(item) for item in cached]
        executor = ReadOnlyAwsQueryExecutor(self.clients)

        def remember(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            self._candidate_cache[cache_key] = [dict(item) for item in items]
            return items

        def parse(payload: dict[str, Any]) -> list[dict[str, Any]]:
            candidates: list[dict[str, Any]] = []
            for page in payload.get("pages", [payload]):
                for item in page.get("InstanceTypes", []):
                    candidates.append(
                        {
                            "model": item["InstanceType"],
                            "vcpu": float(item["VCpuInfo"]["DefaultVCpus"]),
                            "memory_gib": item["MemoryInfo"]["SizeInMiB"] / 1024,
                            "current_generation": item.get("CurrentGeneration", False),
                            "family": _instance_family(item["InstanceType"]),
                            "architectures": item.get("ProcessorInfo", {}).get(
                                "SupportedArchitectures", []
                            ),
                        }
                    )
            return candidates

        discovery_errors: list[Exception] = []
        successful_query = False

        def query(
            *, parameters: dict[str, Any], max_items: int | None = None, paginate: bool = True
        ) -> dict[str, Any] | None:
            nonlocal successful_query
            try:
                response = executor.execute(
                    service="ec2",
                    operation="describe_instance_types",
                    region=region,
                    parameters=parameters,
                    max_items=max_items,
                    paginate=paginate,
                )
                successful_query = True
                return response
            except ManualConfirmationRequired as exc:
                if exc.code in {"aws_credentials_invalid", "aws_region_not_enabled"}:
                    raise
                discovery_errors.append(exc)
                return None

        try:
            if requested_model:
                response = query(parameters={"InstanceTypes": [requested_model]}, paginate=False)
                exact_model = parse(response) if response is not None else []
                if exact_model:
                    return remember(exact_model)

            if requested_vcpu is not None and requested_memory is not None:
                exact_filters = [
                    {
                        "Name": "vcpu-info.default-vcpus",
                        "Values": [str(int(requested_vcpu))],
                    },
                    {
                        "Name": "memory-info.size-in-mib",
                        "Values": [str(int(requested_memory * 1024))],
                    },
                ]
                response = query(
                    parameters={"Filters": exact_filters},
                    max_items=100,
                )
                exact_shapes = parse(response) if response is not None else []
                if exact_shapes:
                    return remember(exact_shapes)

            # Only unusual, non-existent shapes need a broader current-generation
            # scan to produce the lower and upper confirmation choices.
            response = query(
                parameters={"Filters": [{"Name": "current-generation", "Values": ["true"]}]},
                max_items=1000,
            )
            candidates = parse(response) if response is not None else []
        except (ManualConfirmationRequired, KeyError) as exc:
            if isinstance(exc, ManualConfirmationRequired) and exc.code in {
                "aws_credentials_invalid",
                "aws_region_not_enabled",
            }:
                raise
            raise ManualConfirmationRequired(
                f"EC2 官方 API 无法确认 {region} 的实例规格或区域支持",
                code="ec2_discovery_failed",
                region=region,
            ) from exc
        if not successful_query and discovery_errors:
            raise ManualConfirmationRequired(
                f"EC2 官方 API 无法确认 {region} 的实例规格或区域支持",
                code="ec2_discovery_failed",
                region=region,
            ) from discovery_errors[-1]
        return remember(candidates)


def _select_instance(
    candidates: list[dict[str, Any]],
    *,
    requested_model: str | None,
    min_vcpu: float | None,
    min_memory: float | None,
) -> tuple[dict[str, Any], bool]:
    exact = next((item for item in candidates if item["model"] == requested_model), None)
    exact_fits = exact and _fits(exact, min_vcpu, min_memory)
    if exact_fits:
        return exact, False

    if requested_model and exact is None and min_vcpu is None and min_memory is None:
        raise ManualConfirmationRequired(
            "客户指定的 EC2 型号不存在或区域不可用，且没有足够规格信息选择替代型号",
            code="invalid_ec2_model_without_replacement_basis",
        )

    eligible = [item for item in candidates if _fits(item, min_vcpu, min_memory)]
    if not eligible:
        raise ManualConfirmationRequired(
            "AWS 官方规格中没有满足 EC2 需求的实例",
            code="ec2_specification_not_found",
        )
    eligible.sort(
        key=lambda item: (
            0 if item["current_generation"] else 1,
            item["memory_gib"],
            item["vcpu"],
            item["model"],
        )
    )
    return eligible[0], requested_model is not None


def _fits(item: dict[str, Any], min_vcpu: float | None, min_memory: float | None) -> bool:
    return (min_vcpu is None or item["vcpu"] >= min_vcpu) and (
        min_memory is None or item["memory_gib"] >= min_memory
    )


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_billing_identity(product: dict[str, Any] | None) -> tuple[str, str, str] | None:
    """Return Price List dimensions only as optional candidate metadata."""

    if product is None:
        return None
    attrs = PricingCatalog.attributes(product)
    service_code = product.get("serviceCode") or attrs.get("servicecode")
    usage_type = attrs.get("usagetype") or attrs.get("usageType")
    operation = attrs.get("operation")
    if not service_code or not usage_type or operation is None:
        return None
    return service_code, usage_type, operation


def _pricing_operating_system(value: str | None) -> str:
    """Map user-facing OS names to the official Price List product family value."""

    if not value:
        return "Linux"
    normalized = value.casefold()
    if any(token in normalized for token in ("ubuntu", "amazon linux", "al2023", "linux")):
        return "Linux"
    if "windows" in normalized:
        return "Windows"
    if "red hat" in normalized or normalized.startswith("rhel"):
        return "RHEL"
    if "suse" in normalized:
        return "SUSE"
    return value


def _pricing_tenancy(value: str | None) -> str:
    """Map parser/customer tenancy wording to AWS Price List values."""

    if not value:
        return "Shared"
    normalized = value.casefold().replace("_", " ").strip()
    if normalized in {"default", "shared", "共享", "默认"}:
        return "Shared"
    if normalized in {"dedicated", "dedicated instance", "专用实例"}:
        return "Dedicated"
    if normalized in {"host", "dedicated host", "专用宿主机"}:
        return "Host"
    return value


def _instance_family(model: str) -> str:
    prefix = model.split(".", 1)[0].lower()
    if prefix.startswith(("c",)):
        return "compute_optimized"
    if prefix.startswith(("r", "x", "u", "z")):
        return "memory_optimized"
    if prefix.startswith(("i", "d", "h")):
        return "storage_optimized"
    if prefix.startswith(("g", "p", "inf", "trn", "f")):
        return "accelerated"
    return "general_purpose"


def _rank_reasonable_ec2(
    candidates: list[dict[str, Any]],
    *,
    business_type: str,
    architecture: str | None,
    min_vcpu: float | None,
    min_memory: float | None,
) -> list[dict[str, Any]]:
    requested_family = (
        business_type
        if business_type
        in {
            "general_purpose",
            "compute_optimized",
            "memory_optimized",
            "storage_optimized",
            "database",
            "cache",
            "gpu",
        }
        else "general_purpose"
    )
    family = "memory_optimized" if requested_family in {"database", "cache"} else requested_family
    compatible = candidates
    if architecture:
        requested_architecture = architecture.lower()
        architecture_pool = [
            item
            for item in candidates
            if requested_architecture in [value.lower() for value in item.get("architectures", [])]
        ]
        compatible = architecture_pool or candidates

    # A 1:2 CPU-to-memory request is a natural compute-optimized shape. Compute
    # families are valid for ordinary application servers; only storage/GPU
    # families are excluded unless the customer asks for them.
    if requested_family == "general_purpose" and min_vcpu and min_memory:
        ratio = min_memory / min_vcpu
        allowed_families = (
            {"compute_optimized", "general_purpose"} if ratio <= 2.5 else {"general_purpose"}
        )
    else:
        allowed_families = {family}
    preferred = [item for item in compatible if item["family"] in allowed_families]
    general = [item for item in compatible if item["family"] == "general_purpose"]
    pool = preferred or general or compatible
    return sorted(
        pool,
        key=lambda item: (
            0
            if (min_memory is None or item["memory_gib"] == min_memory)
            and (min_vcpu is None or item["vcpu"] == min_vcpu)
            else 1,
            item["memory_gib"] - (min_memory or 0),
            item["vcpu"] - (min_vcpu or 0),
            0 if item["current_generation"] else 1,
            item["model"],
        ),
    )


def _candidate_rationale(item: dict[str, Any], business_type: str | None) -> str:
    if business_type:
        return f"符合 {business_type} 业务类型，且满足客户最低规格。"
    return "默认按通用应用服务器类型筛选，满足客户最低规格。"
