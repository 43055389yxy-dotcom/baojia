from __future__ import annotations

import re
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
            update={
                "requires_confirmation": False,
                "confirmation_reason": None,
                "next_action": "none",
            }
        )


class EbsPlugin(_NoConfirmationPlugin):
    kind = ServiceKind.EBS
    display_name = "Amazon EBS"

    _SNAPSHOT_VARIANTS = frozenset({"snapshot", "snapshot_archive"})
    _VARIANT_ALIASES = {
        "volume": "volume",
        "ebs_volume": "volume",
        "snapshot": "snapshot",
        "standard_snapshot": "snapshot",
        "snapshot_standard": "snapshot",
        "ebs_snapshot": "snapshot",
        "snapshot_archive": "snapshot_archive",
        "archive_snapshot": "snapshot_archive",
        "ebs_snapshot_archive": "snapshot_archive",
    }

    @classmethod
    def _product_variant(cls, requested: dict[str, Any]) -> str:
        """Resolve the closed EBS product schema without reading prose.

        Component cleaning owns language interpretation.  The adapter accepts
        its closed variant enum and may infer ``snapshot`` only from already
        structured snapshot fields, which also protects older cleaned drafts
        that predate the explicit variant field.
        """

        raw = str(requested.get("product_variant") or "").strip().casefold()
        normalized = re.sub(r"[\s-]+", "_", raw)
        if normalized:
            variant = cls._VARIANT_ALIASES.get(normalized)
            if variant is None:
                raise ManualConfirmationRequired(
                    "EBS 产品类型必须是云盘、标准快照或归档快照",
                    code="invalid_ebs_product_variant",
                    product_variant=raw,
                )
            return variant
        if any(
            requested.get(field) not in (None, "", [], {})
            for field in (
                "backup_storage_gib",
                "snapshot_changed_gib",
                "snapshot_frequency",
                "snapshot_retention_days",
            )
        ):
            return "snapshot"
        return "volume"

    def _snapshot_product(
        self,
        *,
        region: str,
        archive: bool,
    ) -> dict[str, Any]:
        """Return one exact official EBS snapshot GB-month product.

        ``productFamily`` is an optional catalog label, so it is only the
        narrow first query.  The semantic UsageType predicate remains the
        authority on the region-only fallback, following the shared catalog
        compatibility rule.
        """

        expected = (
            r"(?:^|-)EBS:SnapshotArchiveStorage$"
            if archive
            else r"(?:^|-)EBS:SnapshotUsage$"
        )

        def is_requested_snapshot(attributes: dict[str, str]) -> bool:
            usage_type = str(
                attributes.get("usagetype") or attributes.get("usageType") or ""
            )
            return bool(re.search(expected, usage_type, re.IGNORECASE))

        products = self.catalog.matching_products(
            "AmazonEC2",
            {"regionCode": region, "productFamily": "Storage Snapshot"},
            is_requested_snapshot,
            max_pages=10,
            fallback_filters={"regionCode": region},
            fallback_predicate=is_requested_snapshot,
        )
        label = "EBS 归档快照" if archive else "EBS 标准快照"
        return PricingCatalog.require_unique(products, context=f"{label}存储 ({region})")

    def _select_snapshot(
        self,
        requirement: ServiceRequirement,
        requested: dict[str, Any],
        *,
        region: str,
        variant: str,
        is_global: bool,
    ) -> SelectedResource:
        conflicting_volume_fields = [
            field
            for field in (
                "storage_gib",
                "total_storage_gib",
                "volume_type",
                "iops",
                "throughput_mbps",
            )
            if requested.get(field) not in (None, "", [], {})
        ]
        if conflicting_volume_fields:
            raise ManualConfirmationRequired(
                "EBS 快照组件同时包含云盘字段，系统已停止混合两种计费口径",
                code="ebs_product_variant_field_conflict",
                product_variant=variant,
                fields=conflicting_volume_fields,
            )

        storage_gib = required_float(requested, "backup_storage_gib")
        product = self._snapshot_product(
            region=region,
            archive=variant == "snapshot_archive",
        )
        label = "EBS 归档快照" if variant == "snapshot_archive" else "EBS 标准快照"
        lines = (
            [
                _usage(
                    product,
                    "ebssnap",
                    storage_gib,
                    "ebs_snapshot",
                    source_fields=("backup_storage_gib",),
                )
            ]
            if storage_gib is not None
            else []
        )
        references = (
            []
            if storage_gib is not None
            else [_reference(product, f"{label}存储单价")]
        )
        frequency = requested.get("snapshot_frequency")
        retention_days = requested.get("snapshot_retention_days")
        changed_gib = requested.get("snapshot_changed_gib")
        applied_fields = [
            field
            for field in (
                "product_variant",
                "backup_storage_gib",
                "snapshot_frequency",
                "snapshot_retention_days",
                "snapshot_changed_gib",
                "quantity",
            )
            if field == "quantity" or requested.get(field) not in (None, "", [], {})
        ]
        notices: list[str] = []
        if is_global:
            notices.append(f"EBS 快照必须属于具体区域；本次按 {region} 查询官方计费项。")
        if storage_gib is None:
            notices.append(
                "客户已提供快照频率或保留策略，但未提供可计费的快照存储量；"
                "本项仅展示 1 GiB-month 官方单位价，不根据频率、保留天数或源云盘容量猜测月费。"
            )
        return SelectedResource(
            service=self.kind,
            display_name="Amazon EBS Snapshot",
            region=region,
            model=label,
            architecture=(
                f"{label} · {storage_gib:g} GiB-month"
                if storage_gib is not None
                else f"{label} · 官方单位参考价"
            ),
            specifications={
                "productVariant": variant,
                **({"backupStorageGiB": storage_gib} if storage_gib is not None else {}),
                **(
                    {"snapshotChangedGiB": changed_gib}
                    if changed_gib is not None
                    else {}
                ),
                **(
                    {"snapshotFrequency": frequency}
                    if frequency not in (None, "")
                    else {}
                ),
                **(
                    {"snapshotRetentionDays": retention_days}
                    if retention_days is not None
                    else {}
                ),
            },
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale=f"使用 {label} 官方 GB-Month 计费维度。",
            substitution_notice=" ".join(notices) or None,
            pricing_status="priced" if storage_gib is not None else "reference_only",
            pricing_issue_code=(
                None if storage_gib is not None else "snapshot_storage_usage_missing"
            ),
            pricing_notice=" ".join(notices) or None,
            usage_lines=lines,
            reference_rates=references,
            applied_requirement_fields=applied_fields,
        )

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        requested_region = requirement.region or default_region
        is_global = requested_region.casefold() in {"global", "aws-global", "全球"}
        region = default_region if is_global else requested_region
        requested = canonicalize_requirement_fields(requirement.requirements, service="ebs")
        variant = self._product_variant(requested)
        if variant in self._SNAPSHOT_VARIANTS:
            return self._select_snapshot(
                requirement,
                requested,
                region=region,
                variant=variant,
                is_global=is_global,
            )

        conflicting_snapshot_fields = [
            field
            for field in (
                "backup_storage_gib",
                "snapshot_changed_gib",
                "snapshot_frequency",
                "snapshot_retention_days",
            )
            if requested.get(field) not in (None, "", [], {})
        ]
        if conflicting_snapshot_fields:
            raise ManualConfirmationRequired(
                "EBS 云盘组件同时包含快照字段，系统已停止混合两种计费口径",
                code="ebs_product_variant_field_conflict",
                product_variant=variant,
                fields=conflicting_snapshot_fields,
            )
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
            [
                _usage(
                    product,
                    "ebs",
                    total_storage_gib,
                    "ebs",
                    source_fields=(
                        "storage_gib",
                        "total_storage_gib",
                        "quantity",
                        "volume_type",
                    ),
                )
            ]
            if total_storage_gib is not None
            else []
        )
        references = (
            [_reference(product, f"EBS {volume_type} 存储单价")]
            if storage_gib is None
            else []
        )
        provisioned_iops = required_float(requested, "iops")
        provisioned_throughput = required_float(requested, "throughput_mbps")
        applied_fields: list[str] = []

        def provisioned_product(metric: str) -> dict[str, Any]:
            usage_suffix = f"ebs:volumep-{metric}.{volume_type}".casefold()
            matches = self.catalog.matching_products(
                "AmazonEC2",
                {
                    "regionCode": region,
                    "volumeApiName": volume_type,
                },
                lambda attrs: str(attrs.get("usagetype") or "")
                .casefold()
                .endswith(usage_suffix),
                max_pages=20,
            )
            return PricingCatalog.require_unique(
                matches,
                context=f"EBS {volume_type} {metric} ({region})",
            )

        additional_iops: float | None = None
        additional_throughput: float | None = None
        if volume_type == "gp3" and provisioned_iops is not None:
            additional_iops = max(provisioned_iops - 3_000, 0) * volume_count
            if additional_iops > 0:
                lines.append(
                    _usage(
                        provisioned_product("iops"),
                        "ebsiops",
                        additional_iops,
                        "ebs",
                        source_fields=("iops", "quantity", "volume_type"),
                    )
                )
            else:
                applied_fields.append("iops")
        if volume_type == "gp3" and provisioned_throughput is not None:
            additional_throughput = (
                max(provisioned_throughput - 125, 0) * volume_count
            )
            if additional_throughput > 0:
                lines.append(
                    _usage(
                        provisioned_product("throughput"),
                        "ebsthru",
                        additional_throughput,
                        "ebs",
                        source_fields=(
                            "throughput_mbps",
                            "quantity",
                            "volume_type",
                        ),
                    )
                )
            else:
                applied_fields.append("throughput_mbps")
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
                **(
                    {
                        "provisionedIops": provisioned_iops,
                        "billedAdditionalIops": additional_iops,
                    }
                    if provisioned_iops is not None
                    else {}
                ),
                **(
                    {
                        "provisionedThroughputMbps": provisioned_throughput,
                        "billedAdditionalThroughputMbps": additional_throughput,
                    }
                    if provisioned_throughput is not None
                    else {}
                ),
            },
            official_product={"source": "AWS Price List", "regionCode": region},
            rationale="使用 Amazon EBS 官方 GB-Month 计费维度。",
            substitution_notice=notice,
            pricing_status="priced" if storage_gib is not None else "reference_only",
            pricing_issue_code=(
                None if storage_gib is not None else "ebs_storage_usage_missing"
            ),
            pricing_notice=notice,
            usage_lines=lines,
            reference_rates=references,
            applied_requirement_fields=applied_fields,
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
        lines = [
            _usage(
                product,
                "dto",
                amount,
                "data-transfer",
                source_fields=("data_transfer_out_gib",),
            )
        ] if amount is not None else []
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
                source_fields=("accelerators", "quantity", "hours_per_month"),
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
            lines.append(
                _usage(
                    transfer_product,
                    "gadt",
                    transfer,
                    "global-accelerator",
                    source_fields=(
                        "data_transfer_out_gib",
                        "source_regions",
                        "destination_geography",
                    ),
                )
            )
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
