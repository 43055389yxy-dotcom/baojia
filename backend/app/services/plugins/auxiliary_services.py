from __future__ import annotations

from typing import Any

from app.core.errors import ManualConfirmationRequired
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


class EbsPlugin(_NoConfirmationPlugin):
    kind = ServiceKind.EBS
    display_name = "Amazon EBS"

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        requested_region = requirement.region or default_region
        is_global = requested_region.casefold() in {"global", "aws-global", "全球"}
        region = default_region if is_global else requested_region
        requested = canonicalize_requirement_fields(requirement.requirements, service="ebs")
        volume_type = str(requested.get("volume_type") or "gp3").casefold()
        storage_gib = required_float(requested, "storage_gib")
        volume_count = max(int(requirement.quantity or 1), 1)
        total_storage_gib = storage_gib * volume_count if storage_gib is not None else None
        products = self.catalog.products(
            "AmazonEC2",
            {
                "regionCode": region,
                "productFamily": "Storage",
                "volumeApiName": volume_type,
            },
            max_pages=3,
        )
        product = PricingCatalog.require_unique(
            products, context=f"EBS {volume_type} 存储 ({region})"
        )
        lines = (
            [_usage(product, "ebs", total_storage_gib, "ebs")]
            if total_storage_gib is not None
            else []
        )
        references = (
            [_reference(product, f"EBS {volume_type} 存储单价")]
            if storage_gib is None
            else []
        )
        notice = None
        if is_global:
            notice = f"EBS 必须属于具体区域；客户未指定归属，本次按 {region} 的最低基础存储项估算。"
        elif storage_gib is None:
            notice = "客户未提供 EBS 容量；仅展示 1 GiB 对应的官方单位价，不计入月费合计。"
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model=volume_type,
            architecture=(
                f"{volume_count} 块 × {storage_gib:g} GiB {volume_type} 云盘"
                if storage_gib is not None
                else f"{volume_type} 官方单位参考价"
            ),
            specifications={
                "volumeType": volume_type,
                **({"storageGiB": storage_gib} if storage_gib is not None else {}),
                **({"volumeCount": volume_count} if storage_gib is not None else {}),
                **(
                    {"totalStorageGiB": total_storage_gib}
                    if total_storage_gib is not None
                    else {}
                ),
            },
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale="使用 Amazon EBS 官方 GB-Month 计费维度。",
            substitution_notice=notice,
            usage_lines=lines,
            reference_rates=references,
        )


class DataTransferPlugin(_NoConfirmationPlugin):
    kind = ServiceKind.DATA_TRANSFER
    display_name = "AWS Data Transfer"

    _REGION_MARKERS = {
        "新加坡": "ap-southeast-1",
        "singapore": "ap-southeast-1",
        "悉尼": "ap-southeast-2",
        "sydney": "ap-southeast-2",
        "香港": "ap-east-1",
        "hong kong": "ap-east-1",
        "东京": "ap-northeast-1",
        "tokyo": "ap-northeast-1",
        "首尔": "ap-northeast-2",
        "seoul": "ap-northeast-2",
    }

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        requested = requirement.requirements
        amount = required_float(requested, "data_transfer_out_gib")
        regions = self._source_regions(requirement, default_region)
        candidates: list[tuple[float, str, dict[str, Any]]] = []
        for region in regions:
            products = self.catalog.products(
                "AWSDataTransfer",
                {
                    "fromLocation": self.catalog.location(region),
                    "toLocation": "External",
                    "transferType": "AWS Outbound",
                },
                max_pages=3,
            )
            for product in products:
                rate = PricingCatalog.on_demand_unit_rate(product)
                if rate is not None:
                    candidates.append((rate[0], region, product))
        if not candidates:
            raise ManualConfirmationRequired(
                "AWS 官方目录暂时没有返回公网出站流量计费项",
                code="data_transfer_billing_dimension_not_found",
            )
        _, region, product = min(candidates, key=lambda item: (item[0], item[1]))
        lines = [_usage(product, "dto", amount, "data-transfer")] if amount is not None else []
        references = (
            [_reference(product, f"{region} 公网出站流量单价")]
            if amount is None
            else []
        )
        notice = None
        if len(regions) > 1:
            notice = (
                "客户给出多区域合计流量但未分配到各区域；本次按所列区域中的最低官方单价"
                f"（{region}）估算，取得分区流量后应更新报价。"
            )
        elif amount is None:
            notice = "客户未提供公网出站量；仅展示 1 GiB 对应的官方单位价，不计入月费合计。"
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=region,
            model="Internet Data Transfer Out",
            architecture=(f"{amount:g} GiB/月公网出站" if amount is not None else "官方单位参考价"),
            specifications={
                "sourceRegions": regions,
                **({"dataTransferOutGiB": amount} if amount is not None else {}),
            },
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale="使用 AWS Data Transfer 公网出站官方计费维度。",
            substitution_notice=notice,
            usage_lines=lines,
            reference_rates=references,
        )

    @classmethod
    def _source_regions(
        cls, requirement: ServiceRequirement, default_region: str
    ) -> list[str]:
        raw = requirement.requirements.get("source_regions")
        regions = [str(item) for item in raw] if isinstance(raw, list) else []
        source = (requirement.source_text or "").casefold()
        for marker, region in cls._REGION_MARKERS.items():
            if marker in source and region not in regions:
                regions.append(region)
        if requirement.region and requirement.region.casefold() not in {"global", "全球"}:
            regions.append(requirement.region)
        return list(dict.fromkeys(regions)) or [default_region]


class GlobalAcceleratorPlugin(_NoConfirmationPlugin):
    kind = ServiceKind.GLOBAL_ACCELERATOR
    display_name = "AWS Global Accelerator"

    _GEOGRAPHY_LABELS = {
        "ap": "AP",
        "asia pacific": "AP",
        "亚太": "AP",
        "na": "NA",
        "north america": "NA",
        "美国": "NA",
        "加拿大": "NA",
        "eu": "EU",
        "europe": "EU",
        "欧洲": "EU",
        "kr": "KR",
        "south korea": "KR",
        "韩国": "KR",
        "in": "IN",
        "india": "IN",
        "印度": "IN",
        "au": "AU",
        "australia": "AU",
        "澳大利亚": "AU",
        "me": "ME",
        "middle east": "ME",
        "中东": "ME",
        "sa": "SA",
        "south america": "SA",
        "南美": "SA",
        "za": "ZA",
        "south africa": "ZA",
        "南非": "ZA",
    }

    @classmethod
    def _region_geography(cls, region: str) -> str:
        normalized = region.strip().casefold()
        if normalized.startswith(("us-", "ca-")):
            return "NA"
        if normalized.startswith(("eu-", "il-")):
            return "EU"
        if normalized.startswith("sa-"):
            return "SA"
        if normalized.startswith(("me-", "mx-")):
            return "ME"
        if normalized.startswith("af-"):
            return "ZA"
        if normalized.startswith("ap-northeast-2"):
            return "KR"
        if normalized.startswith("ap-south-"):
            return "IN"
        if normalized.startswith("ap-southeast-2") or normalized.startswith(
            "ap-southeast-4"
        ):
            return "AU"
        return "AP"

    @classmethod
    def _destination_geography(cls, value: object, default: str) -> str:
        normalized = str(value or "").strip().casefold()
        if not normalized:
            return default
        if normalized.upper() in set(cls._GEOGRAPHY_LABELS.values()):
            return normalized.upper()
        for marker, code in cls._GEOGRAPHY_LABELS.items():
            if marker in normalized:
                return code
        return default

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        # Use the same canonical transfer contract as the rest of the pricing
        # pipeline. Older confirmation sessions may still carry
        # ``data_transfer_gib``; canonicalization preserves compatibility by
        # promoting that alias to ``data_transfer_out_gib``.
        requested = canonicalize_requirement_fields(
            requirement.requirements, service="global_accelerator"
        )
        accelerators = required_float(requested, "accelerators") or float(requirement.quantity)
        transfer = required_float(requested, "data_transfer_out_gib")
        fixed = PricingCatalog.require_unique(
            self.catalog.products(
                "AWSGlobalAccelerator",
                {"usagetype": "Global-Accelerator-fixed-fee"},
                max_pages=1,
            ),
            context="Global Accelerator 小时费",
        )
        lines = [
            _usage(
                fixed,
                "gah",
                accelerators * requirement.hours_per_month,
                "global-accelerator",
            )
        ]
        references: list[ReferenceRate] = []
        notice = None
        if transfer is not None:
            transfer_products = [
                product
                for product in self.catalog.products(
                    "AWSGlobalAccelerator", {}, max_pages=20
                )
                if PricingCatalog.attributes(product).get("operation") == "Dominant"
                and PricingCatalog.attributes(product).get("usagetype", "").endswith(
                    "OUT-Bytes-Internet"
                )
            ]
            rated = [
                (rate[0], product)
                for product in transfer_products
                if (rate := PricingCatalog.on_demand_unit_rate(product)) is not None
            ]
            if not rated:
                raise ManualConfirmationRequired(
                    "AWS 官方目录暂时没有返回 Global Accelerator 流量计费项",
                    code="global_accelerator_transfer_dimension_not_found",
                )
            raw_sources = requested.get("source_regions")
            source_regions = (
                [str(item) for item in raw_sources]
                if isinstance(raw_sources, list)
                else [str(raw_sources)]
                if raw_sources
                else []
            )
            if (
                requirement.region
                and requirement.region.casefold() not in {"global", "全球"}
                and requirement.region not in source_regions
            ):
                source_regions.append(requirement.region)
            if not source_regions:
                source_regions = [default_region]
            source_geographies = list(
                dict.fromkeys(self._region_geography(region) for region in source_regions)
            )
            destination = self._destination_geography(
                requested.get("destination_geography"),
                source_geographies[0],
            )
            path_rated = [
                (rate, product)
                for rate, product in rated
                if PricingCatalog.attributes(product).get("fromLocation")
                in source_geographies
                and PricingCatalog.attributes(product).get("toLocation") == destination
            ]
            if not path_rated:
                raise ManualConfirmationRequired(
                    "AWS 官方目录暂时没有返回对应来源与目标地域的 Global Accelerator 流量计费项",
                    code="global_accelerator_transfer_dimension_not_found",
                    source_geographies=source_geographies,
                    destination_geography=destination,
                )
            _, transfer_product = min(path_rated, key=lambda item: item[0])
            lines.append(_usage(transfer_product, "gadt", transfer, "global-accelerator"))
            if requested.get("destination_geography") in (None, ""):
                notice = (
                    "客户未指定访问者地域；加速流量暂按源站所在地域估算，"
                    "取得用户地域分布后可更新报价。"
                )
        else:
            notice = "客户未提供 Global Accelerator 加速流量；月费仅包含加速器小时费。"
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region="Global",
            model="Standard Accelerator",
            architecture=f"{accelerators:g} 个加速器",
            specifications={
                "accelerators": accelerators,
                **({"dataTransferOutGiB": transfer} if transfer is not None else {}),
            },
            official_product={"source": "AWS Price List", "regionCode": "Global"},
            rationale="按 Global Accelerator 固定小时费及已知加速流量提交 BCM。",
            substitution_notice=notice,
            usage_lines=lines,
            reference_rates=references,
        )
