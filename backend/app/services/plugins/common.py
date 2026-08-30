from __future__ import annotations

import re
from typing import Any

from app.core.errors import ManualConfirmationRequired
from app.domain.customer_facts import scoped_amount
from app.domain.models import (
    PreviewSelection,
    ReferenceRate,
    SelectedResource,
    ServiceKind,
    ServiceRequirement,
    UsageLine,
)
from app.domain.requirement_fields import canonicalize_requirement_fields
from app.integrations.aws import PricingCatalog
from app.services.plugins.base import ServicePlugin, required_float


def _one_product(
    catalog: PricingCatalog, service_code: str, filters: dict[str, str], context: str
) -> dict[str, Any]:
    products = catalog.products(service_code, filters, max_pages=3)
    if not products:
        products = catalog.products(
            service_code,
            filters,
            max_pages=3,
            refresh=True,
        )
    return PricingCatalog.require_unique(products, context=context)


def _line(
    product: dict[str, Any],
    *,
    key: str,
    amount: float,
    group: str,
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


def _reference(product: dict[str, Any], *, description: str) -> ReferenceRate:
    service_code, usage_type, operation = PricingCatalog.billing_identity(product)
    priced = PricingCatalog.on_demand_unit_rate(product)
    if priced is None:
        raise ManualConfirmationRequired(
            "AWS 官方目录暂时没有返回该项目的单位价格",
            code="reference_unit_rate_not_found",
            service_code=service_code,
            usage_type=usage_type,
        )
    unit_price, unit = priced
    return ReferenceRate(
        description=description,
        unit=unit,
        unit_price=unit_price,
        service_code=service_code,
        usage_type=usage_type,
        operation=operation,
    )


def _s3_request_product(
    catalog: PricingCatalog,
    *,
    region: str,
    group: str,
    tier: int,
    context: str,
) -> dict[str, Any]:
    """Resolve an ordinary object API meter inside an official S3 group.

    AWS now publishes S3 Metadata ``Annotation Requests`` in the same Tier 1
    and Tier 2 groups as ordinary PUT/GET requests.  The group is therefore a
    discovery boundary, not a unique billing identity.  Match the customer's
    action to the official usage type/description first, then retain the
    fail-closed uniqueness check for genuinely ambiguous results.
    """

    expected_usage = re.compile(
        rf"requests(?:-(?:sia|zia))?-tier{tier}$",
        re.IGNORECASE,
    )

    def matches(product: dict[str, Any]) -> bool:
        attributes = PricingCatalog.attributes(product)
        usage_type = str(
            attributes.get("usagetype") or attributes.get("usageType") or ""
        ).strip()
        description = str(attributes.get("groupDescription") or "").strip()
        searchable = f"{usage_type} {description}".casefold()
        if "annotation" in searchable or not expected_usage.search(usage_type):
            return False
        # Official descriptions add the storage tier suffix for IA classes,
        # but the billed action at the beginning remains stable.
        if description:
            lowered = description.casefold()
            if tier == 1 and not ("put" in lowered and "list" in lowered):
                return False
            if tier == 2 and "get" not in lowered:
                return False
        return True

    products = catalog.products(
        "AmazonS3", {"regionCode": region, "group": group}, max_pages=3
    )
    candidates = [product for product in products if matches(product)]
    if not candidates:
        products = catalog.products(
            "AmazonS3",
            {"regionCode": region, "group": group},
            max_pages=3,
            refresh=True,
        )
        candidates = [product for product in products if matches(product)]
    return PricingCatalog.require_unique(candidates, context=context)


def _normalize_s3_storage_class(value: object) -> str:
    """Normalize equivalent customer/AI labels without changing the S3 tier."""
    normalized = str(value or "standard").strip().lower()
    normalized = "_".join(normalized.replace("-", " ").split())
    if normalized in {
        "standard",
        "s3_standard",
        "amazon_s3_standard",
        "general_purpose",
        "generalpurpose",
        "标准",
        "标准存储",
    }:
        return "standard"
    if normalized in {
        "standard_ia",
        "s3_standard_ia",
        "standard_infrequent_access",
        "标准_ia",
        "标准低频访问",
        "低频访问",
    }:
        return "standard_ia"
    if normalized in {
        "one_zone_ia",
        "s3_one_zone_ia",
        "one_zone_infrequent_access",
        "单区_ia",
        "单区低频访问",
    }:
        return "one_zone_ia"
    return normalized


class S3Plugin(ServicePlugin):
    kind = ServiceKind.S3
    display_name = "Amazon S3"

    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        preview = super().preview(requirement, default_region)
        # A missing storage quantity is handled by QuoteService's approved
        # 1 GiB minimum.  It is a disclosed pricing assumption, not a customer
        # decision, so it must never create a confirmation question.
        return preview.model_copy(
            update={"requires_confirmation": False, "confirmation_reason": None}
        )

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        requested = canonicalize_requirement_fields(requirement.requirements, service="s3")
        storage_gib = required_float(requested, "storage_gib")
        raw_storage_class = requested.get("storage_class") or "standard"
        storage_class = _normalize_s3_storage_class(raw_storage_class)
        storage_dimensions = {
            "standard": ("General Purpose", "Standard", "S3 Standard"),
            "standard_ia": (
                "Infrequent Access",
                "Standard - Infrequent Access",
                "S3 Standard-IA",
            ),
            "one_zone_ia": (
                "Infrequent Access",
                "One Zone - Infrequent Access",
                "S3 One Zone-IA",
            ),
        }
        if storage_class not in storage_dimensions:
            raise ManualConfirmationRequired(
                f"S3 存储类型 {raw_storage_class} 尚未接入 API 适配器",
                code="unsupported_s3_storage_class",
            )
        product_family, volume_type, display_tier = storage_dimensions[storage_class]
        product = _one_product(
            self.catalog,
            "AmazonS3",
            {
                "regionCode": region,
                "productFamily": "Storage",
                "storageClass": product_family,
                "volumeType": volume_type,
            },
            f"{display_tier} 存储 ({region})",
        )
        attrs = PricingCatalog.attributes(product)
        storage_line = (
            _line(
                product,
                key="s3",
                amount=storage_gib,
                group="s3",
                source_fields=("storage_gib",),
            )
            if storage_gib is not None
            else None
        )
        reference_rates = (
            [_reference(product, description=f"{display_tier} 存储单价")]
            if storage_gib is None
            else []
        )
        lines = [storage_line] if storage_line else []
        request_dimensions = {
            "standard": ("S3-API-Tier1", "S3-API-Tier2"),
            "standard_ia": ("S3-API-SIA-Tier1", "S3-API-SIA-Tier2"),
            "one_zone_ia": ("S3-API-ZIA-Tier1", "S3-API-ZIA-Tier2"),
        }
        for field, key, group, tier in (
            (
                "put_copy_post_list_requests",
                "s3put",
                request_dimensions[storage_class][0],
                1,
            ),
            (
                "get_select_requests",
                "s3get",
                request_dimensions[storage_class][1],
                2,
            ),
        ):
            amount = required_float(requested, field)
            if amount is None:
                continue
            request_product = _s3_request_product(
                self.catalog,
                region=region,
                group=group,
                tier=tier,
                context=f"{display_tier} {field} ({region})",
            )
            lines.append(
                _line(
                    request_product,
                    key=key,
                    amount=scoped_amount(requirement, field, amount),
                    group="s3",
                    source_fields=(field,),
                )
            )
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model=display_tier,
            architecture=(
                f"{storage_gib:g} GiB {display_tier} 对象存储"
                if storage_gib is not None
                else "未提供容量，仅展示官方单位参考价"
            ),
            specifications={
                "storageClass": display_tier,
                **({"storageGiB": storage_gib} if storage_gib is not None else {}),
                **(
                    {"putCopyPostListRequests": requested["put_copy_post_list_requests"]}
                    if requested.get("put_copy_post_list_requests") is not None
                    else {}
                ),
                **(
                    {"getSelectRequests": requested["get_select_requests"]}
                    if requested.get("get_select_requests") is not None
                    else {}
                ),
            },
            official_product={
                "sku": product["product"]["sku"],
                "usageType": (
                    storage_line.usage_type if storage_line else reference_rates[0].usage_type
                ),
                "operation": (
                    storage_line.operation if storage_line else reference_rates[0].operation
                ),
                "regionCode": attrs.get("regionCode"),
            },
            rationale=f"使用 AWS 官方 {display_tier} GB-Month 计费维度。",
            substitution_notice=(
                "客户未提供 S3 容量；本次仅展示 AWS 官方单位价，不计入月费合计。"
                if storage_gib is None
                else None
            ),
            usage_lines=lines,
            reference_rates=reference_rates,
        )


class AlbPlugin(ServicePlugin):
    kind = ServiceKind.ELB
    display_name = "Elastic Load Balancing"

    _TYPE_PROFILES = {
        "application": (
            "Application Load Balancer",
            "ALB",
            "LoadBalancing:Application",
            "Application LCU",
        ),
        "network": (
            "Network Load Balancer",
            "NLB",
            "LoadBalancing:Network",
            "Network LCU",
        ),
        "gateway": (
            "Gateway Load Balancer",
            "GWLB",
            "LoadBalancing:Gateway",
            "Gateway LCU",
        ),
    }

    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        preview = super().preview(requirement, default_region)
        # Missing LCU traffic is an explicitly approved minimum assumption,
        # disclosed in the final quote rather than blocking customer confirmation.
        return preview.model_copy(
            update={"requires_confirmation": False, "confirmation_reason": None}
        )

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        requested = canonicalize_requirement_fields(requirement.requirements, service="elb")
        requested_type = str(
            requested.get("load_balancer_type") or "application"
        ).strip().casefold()
        aliases = {
            "alb": "application",
            "application_load_balancer": "application",
            "nlb": "network",
            "network_load_balancer": "network",
            "gwlb": "gateway",
            "gateway_load_balancer": "gateway",
        }
        load_balancer_type = aliases.get(requested_type, requested_type)
        if load_balancer_type not in self._TYPE_PROFILES:
            raise ManualConfirmationRequired(
                "负载均衡器类型必须是 ALB、NLB 或 GWLB",
                code="invalid_load_balancer_type",
                field="load_balancer_type",
            )
        display_name, acronym, operation, capacity_label = self._TYPE_PROFILES[
            load_balancer_type
        ]
        hourly = _one_product(
            self.catalog,
            "AWSELB",
            {
                "regionCode": region,
                "usagetype": self._usage_type(
                    region, operation, "LoadBalancerUsage", acronym
                ),
                "operation": operation,
            },
            f"{display_name} 小时费 ({region})",
        )
        lcu = _one_product(
            self.catalog,
            "AWSELB",
            {
                "regionCode": region,
                "usagetype": self._usage_type(
                    region, operation, "LCUUsage", acronym
                ),
                "operation": operation,
            },
            f"{display_name} 容量单位 ({region})",
        )
        processed = required_float(requested, "processed_bytes_gib")
        if processed is None:
            processed = required_float(requested, "data_processed_gib")
        assumption = (
            str(requested["system_default_assumption"])
            if requested.get("system_default_assumption")
            else None
        )
        if processed is None:
            assumption = assumption or (
                f"客户未提供 {acronym} 容量单位业务量；月费仅包含 {acronym} 实例小时费，"
                "容量单位仅展示 AWS 官方参考价，不计入合计。"
            )
        lines = [
            _line(
                hourly,
                key=f"{acronym.casefold()}h",
                amount=requirement.quantity * requirement.hours_per_month,
                group=acronym.casefold(),
                source_fields=("quantity", "hours_per_month", "load_balancer_type"),
            ),
        ]
        if processed is not None:
            lines.append(
                _line(
                    lcu,
                    key=f"{acronym.casefold()}lcu",
                    amount=processed,
                    group=acronym.casefold(),
                    source_fields=("processed_bytes_gib", "data_processed_gib"),
                )
            )
        reference_rates = (
            [_reference(lcu, description=f"{capacity_label} 单价")]
            if processed is None
            else []
        )
        return SelectedResource(
            service=self.kind,
            display_name=display_name,
            region=region,
            model=display_name,
            quantity=requirement.quantity,
            architecture=f"{requirement.quantity} 个 {acronym}",
            specifications={
                "quantity": requirement.quantity,
                "load_balancer_type": load_balancer_type,
                **({"processedBytesGiB": processed} if processed is not None else {}),
            },
            official_product={
                "source": "AWS Price List",
                "regionCode": region,
                "operation": operation,
            },
            rationale=f"{acronym} 小时费与 {capacity_label} 使用量分别提交 BCM。",
            substitution_notice=assumption,
            usage_lines=lines,
            reference_rates=reference_rates,
        )

    def _usage_type(
        self, region: str, operation: str, suffix: str, acronym: str
    ) -> str:
        products = self.catalog.products(
            "AWSELB",
            {
                "regionCode": region,
                "operation": operation,
            },
            max_pages=2,
        )
        matches = [
            PricingCatalog.attributes(product).get("usagetype", "")
            for product in products
            if PricingCatalog.attributes(product).get("usagetype", "").endswith(suffix)
            and "Reserved" not in PricingCatalog.attributes(product).get("usagetype", "")
            and "Outposts" not in PricingCatalog.attributes(product).get("usagetype", "")
            and "TS-" not in PricingCatalog.attributes(product).get("usagetype", "")
        ]
        if len(set(matches)) != 1:
            raise ManualConfirmationRequired(
                f"AWS 官方目录无法唯一确认 {acronym} {suffix} 计费项",
                code="alb_billing_dimension_not_found",
            )
        return matches[0]


class CloudFrontPlugin(ServicePlugin):
    kind = ServiceKind.CLOUDFRONT
    display_name = "Amazon CloudFront"

    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        preview = super().preview(requirement, default_region)
        # A missing transfer quantity is handled by QuoteService's approved
        # 1 GiB/month minimum, with request count left at zero.  Disclose this
        # on the final quote instead of asking the customer to approve it.
        return preview.model_copy(
            update={"requires_confirmation": False, "confirmation_reason": None}
        )

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        requested = canonicalize_requirement_fields(requirement.requirements, service="cloudfront")
        transfer_gib = required_float(requested, "data_transfer_out_gib")
        raw_geography = str(requested.get("traffic_geography") or "").strip()
        if not raw_geography:
            raise ManualConfirmationRequired(
                "CloudFront 的公网下行和 HTTPS 请求价格取决于访问者流量地区，客户尚未指定。请从下方选择主要访问者流量地区。",
                code="cloudfront_traffic_geography_required",
                field="traffic_geography",
            )
        geography, prefix = self._geography(raw_geography)
        transfer = _one_product(
            self.catalog,
            "AmazonCloudFront",
            {
                "transferType": "CloudFront Outbound",
                "fromLocation": geography,
                "toLocation": "External",
            },
            f"CloudFront {geography} 公网下行",
        )
        lines = (
            [
                _line(
                    transfer,
                    key="cfout",
                    amount=transfer_gib,
                    group="cloudfront",
                    source_fields=("data_transfer_out_gib", "traffic_geography"),
                )
            ]
            if transfer_gib is not None
            else []
        )
        reference_rates = (
            [_reference(transfer, description=f"CloudFront {geography} 公网下行单价")]
            if transfer_gib is None
            else []
        )
        request_count = self._request_count(requested)
        if request_count is not None:
            requests = _one_product(
                self.catalog,
                "AmazonCloudFront",
                {
                    "productFamily": "Request",
                    "requestType": "CloudFront-Request-HTTPS-Proxy",
                    "usagetype": f"{prefix}-Requests-HTTPS-Proxy",
                },
                f"CloudFront {geography} HTTPS 请求",
            )
            lines.append(
                _line(
                    requests,
                    key="cfreq",
                    amount=request_count,
                    group="cloudfront",
                    source_fields=("https_requests", "requests", "traffic_geography"),
                )
            )
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=f"Global · {geography}",
            model="CloudFront Pay-as-you-go",
            architecture=(
                f"{transfer_gib:g} GiB/月公网下行"
                if transfer_gib is not None
                else "未提供下行量，仅展示官方单位参考价"
            ),
            specifications={
                **({"dataTransferOutGiB": transfer_gib} if transfer_gib is not None else {}),
                "httpsRequests": request_count,
                "priceClassGeography": geography,
            },
            official_product={"source": "AWS Price List", "trafficRegion": geography},
            rationale="使用 AWS 官方 CloudFront 地理区域流量与 HTTPS 请求计费维度。",
            substitution_notice=(
                "客户未提供 CloudFront 下行量；本次仅展示 AWS 官方单位价，不计入月费合计。"
                if transfer_gib is None
                else None
            ),
            usage_lines=lines,
            reference_rates=reference_rates,
        )

    @staticmethod
    def _request_count(requirements: dict[str, Any]) -> float | None:
        for key in ("https_requests", "https_requests_per_month", "request_count"):
            value = required_float(requirements, key)
            if value is not None:
                return value
        return None

    @staticmethod
    def _geography(value: str) -> tuple[str, str]:
        normalized = value.strip().casefold()
        if normalized in {"europe", "欧洲"}:
            return "Europe", "EU"
        if normalized in {"united states", "us", "美国"}:
            return "United States", "US"
        if normalized in {"canada", "加拿大"}:
            return "Canada", "CA"
        if normalized in {"japan", "日本"}:
            return "Japan", "JP"
        if normalized in {"australia", "澳大利亚"}:
            return "Australia", "AU"
        if normalized in {"asia pacific", "ap", "亚太", "亚太地区"}:
            return "Asia Pacific", "AP"
        raise ManualConfirmationRequired(
            "CloudFront 流量地区不在当前官方选项中，请重新选择。",
            code="cloudfront_traffic_geography_invalid",
            field="traffic_geography",
            supplied=value,
        )
