from __future__ import annotations

from typing import Any

from app.core.errors import ManualConfirmationRequired
from app.domain.requirement_fields import canonicalize_requirement_fields
from app.domain.models import (
    PreviewSelection,
    ReferenceRate,
    SelectedResource,
    ServiceKind,
    ServiceRequirement,
    UsageLine,
)
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


def _line(product: dict[str, Any], *, key: str, amount: float, group: str) -> UsageLine:
    service_code, usage_type, operation = PricingCatalog.billing_identity(product)
    return UsageLine(
        key=key,
        service_code=service_code,
        usage_type=usage_type,
        operation=operation,
        amount=amount,
        group=group,
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
        line = (
            _line(product, key="s3", amount=storage_gib, group="s3")
            if storage_gib is not None
            else None
        )
        reference_rates = (
            [_reference(product, description=f"{display_tier} 存储单价")]
            if storage_gib is None
            else []
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
            specifications={"storageClass": display_tier, **({"storageGiB": storage_gib} if storage_gib is not None else {})},
            official_product={
                "sku": product["product"]["sku"],
                "usageType": (line.usage_type if line else reference_rates[0].usage_type),
                "operation": (line.operation if line else reference_rates[0].operation),
                "regionCode": attrs.get("regionCode"),
            },
            rationale=f"使用 AWS 官方 {display_tier} GB-Month 计费维度。",
            substitution_notice=(
                "客户未提供 S3 容量；本次仅展示 AWS 官方单位价，不计入月费合计。"
                if storage_gib is None
                else None
            ),
            usage_lines=([line] if line else []),
            reference_rates=reference_rates,
        )


class AlbPlugin(ServicePlugin):
    kind = ServiceKind.ELB
    display_name = "Application Load Balancer"

    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        preview = super().preview(requirement, default_region)
        # Missing LCU traffic is an explicitly approved minimum assumption,
        # disclosed in the final quote rather than blocking customer confirmation.
        return preview.model_copy(
            update={"requires_confirmation": False, "confirmation_reason": None}
        )

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        region = requirement.region or default_region
        hourly = _one_product(
            self.catalog,
            "AWSELB",
            {
                "regionCode": region,
                "usagetype": self._usage_type(region, "LoadBalancerUsage"),
                "operation": "LoadBalancing:Application",
            },
            f"Application Load Balancer 小时费 ({region})",
        )
        lcu = _one_product(
            self.catalog,
            "AWSELB",
            {
                "regionCode": region,
                "usagetype": self._usage_type(region, "LCUUsage"),
                "operation": "LoadBalancing:Application",
            },
            f"Application Load Balancer LCU ({region})",
        )
        requested = canonicalize_requirement_fields(requirement.requirements, service="elb")
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
                "客户未提供 ALB LCU 业务量；月费仅包含 ALB 实例小时费，"
                "LCU 仅展示 AWS 官方单位参考价，不计入合计。"
            )
        lines = [
            _line(
                hourly,
                key="albh",
                amount=requirement.quantity * requirement.hours_per_month,
                group="alb",
            ),
        ]
        if processed is not None:
            lines.append(_line(lcu, key="alblcu", amount=processed, group="alb"))
        reference_rates = (
            [_reference(lcu, description="Application LCU 单价")]
            if processed is None
            else []
        )
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model="Application Load Balancer",
            architecture=f"{requirement.quantity} 个 ALB",
            specifications={"quantity": requirement.quantity, **({"processedBytesGiB": processed} if processed is not None else {})},
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale="ALB 小时费与 Application LCU 使用量分别提交 BCM。",
            substitution_notice=assumption,
            usage_lines=lines,
            reference_rates=reference_rates,
        )

    def _usage_type(self, region: str, suffix: str) -> str:
        products = self.catalog.products(
            "AWSELB",
            {
                "regionCode": region,
                "operation": "LoadBalancing:Application",
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
                f"AWS 官方目录无法唯一确认 ALB {suffix} 计费项",
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
        geography, prefix = self._geography(requirement.region or default_region)
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
            [_line(transfer, key="cfout", amount=transfer_gib, group="cloudfront")]
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
            lines.append(_line(requests, key="cfreq", amount=request_count, group="cloudfront"))
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
    def _geography(region: str) -> tuple[str, str]:
        normalized = region.lower()
        if normalized.startswith("eu-"):
            return "Europe", "EU"
        if normalized.startswith("us-"):
            return "United States", "US"
        if normalized.startswith("ca-"):
            return "Canada", "CA"
        if normalized.startswith("ap-northeast-1"):
            return "Japan", "JP"
        if normalized.startswith("ap-southeast-2"):
            return "Australia", "AU"
        return "Asia Pacific", "AP"
