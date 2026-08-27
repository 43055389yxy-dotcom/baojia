from __future__ import annotations

import re
from typing import Any

from app.core.errors import ManualConfirmationRequired
from app.integrations.azure_catalog import AzureOfficialCatalog
from app.integrations.azure_product_registry import AzureProductRegistry


def _service_candidates(service_key: str, display_name: str) -> list[str]:
    values = [display_name, service_key.replace("_", " ")]
    result: list[str] = []
    for value in values:
        cleaned = re.sub(r"^(?:microsoft\s+azure|microsoft|azure)\s+", "", value, flags=re.I)
        for candidate in (value.strip(), cleaned.strip()):
            if candidate and candidate.casefold() not in {item.casefold() for item in result}:
                result.append(candidate)
    return result


def _profile_fields(profile: dict[str, Any]) -> list[str]:
    fields = {"requested_sku", "monthly_quantity", "usage_unit"}
    units = " ".join(str(item) for item in profile.get("units", [])).casefold()
    meters = " ".join(str(item) for item in profile.get("meter_names", [])).casefold()
    if "hour" in units:
        fields.add("hours_per_month")
    if any(marker in units for marker in ("gb", "gib", "tb")):
        if any(marker in meters for marker in ("transfer", "egress", "outbound")):
            fields.add("data_transfer_out_gib")
        elif any(marker in meters for marker in ("storage", "capacity", "stored")):
            fields.add("storage_gib")
    if any(marker in units for marker in ("request", "operation", "transaction")):
        fields.add("requests")
    return sorted(fields)


class AzureAutoServiceDiscovery:
    """Build non-executable Azure component profiles from public retail metadata."""

    def __init__(
        self,
        catalog: AzureOfficialCatalog,
        product_registry: AzureProductRegistry | None = None,
    ):
        self._catalog = catalog
        self._product_registry = product_registry
        self._used_profiles: set[tuple[str, str, str]] = set()

    async def ensure_profile(
        self,
        *,
        service_key: str,
        display_name: str,
        region: str | None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        target_region = region or "southeastasia"
        candidates: list[str] = []
        if self._product_registry is not None:
            registered = self._product_registry.resolve_product(
                service_key,
                display_name,
            )
            if registered is not None:
                official_name = str(registered.get("service_name") or "").strip()
                if official_name:
                    candidates.append(official_name)
        for candidate in _service_candidates(service_key, display_name):
            if candidate.casefold() not in {item.casefold() for item in candidates}:
                candidates.append(candidate)
        for service_name in candidates:
            profile = await self._catalog.sync_service_profile(
                service_name=service_name,
                region=target_region,
                force_refresh=force_refresh,
            )
            if int(profile.get("row_count") or 0) <= 0:
                continue
            result = {
                **profile,
                "status": "verified",
                "service_key": service_key,
                "display_name": display_name,
                "service_name": service_name,
                "region": target_region,
                "fields": _profile_fields(profile),
            }
            result["prompt_text"] = self._profile_prompt(result)
            self._used_profiles.add((service_key, display_name, target_region))
            if self._product_registry is not None:
                self._product_registry.update_profile(result)
            return result
        region_mismatches: list[tuple[str, list[str]]] = []
        for service_name in candidates:
            supported_regions = await self._catalog.service_regions(
                service_name,
                force_refresh=force_refresh,
            )
            if supported_regions and target_region not in supported_regions:
                region_mismatches.append((service_name, supported_regions))
        if region_mismatches:
            service_name, supported_regions = min(
                region_mismatches,
                key=lambda item: len(item[1]),
            )
            if self._product_registry is not None:
                self._product_registry.register_identity(
                    service_key=service_key,
                    display_name=display_name,
                    service_name=service_name,
                    regions=supported_regions,
                )
            raise ManualConfirmationRequired(
                f"{display_name} 在当前 Azure 区域不可用，请选择 Microsoft 官方支持区域。",
                code="azure_service_region_not_supported",
                service=service_key,
                service_name=service_name,
                requested_region=target_region,
                supported_regions=supported_regions,
            )
        raise ManualConfirmationRequired(
            "Microsoft 官方零售目录暂时无法唯一匹配这个 Azure 新组件",
            code="azure_auto_discovery_service_not_found",
            service=service_key,
        )

    async def refresh_used_profiles(self) -> dict[str, int]:
        """Refresh every dynamically discovered profile used by this process."""

        refreshed = 0
        failed = 0
        for service_key, display_name, region in tuple(self._used_profiles):
            try:
                await self.ensure_profile(
                    service_key=service_key,
                    display_name=display_name,
                    region=region,
                    force_refresh=True,
                )
                refreshed += 1
            except Exception:
                failed += 1
        return {"refreshed": refreshed, "failed": failed}

    async def refresh_registered_profiles(self) -> dict[str, int]:
        """Refresh persisted dynamic profiles after process restarts."""

        if self._product_registry is None:
            return await self.refresh_used_profiles()
        for product in self._product_registry.list_products():
            template = product.get("field_template") or {}
            region = str(template.get("region") or "").strip()
            if not region:
                continue
            self._used_profiles.add(
                (
                    str(product["service_key"]),
                    str(product["display_name"]),
                    region,
                )
            )
        return await self.refresh_used_profiles()

    @staticmethod
    def _profile_prompt(profile: dict[str, Any]) -> str:
        return (
            f"【Azure 官方自动发现：{profile.get('display_name')}】\n"
            f"Retail serviceName：{profile.get('service_name')}。\n"
            f"固定字段：{', '.join(profile.get('fields', []))}。\n"
            f"官方 SKU 示例：{', '.join(profile.get('arm_sku_names', [])[:20]) or '无'}。\n"
            f"官方 Meter 示例：{', '.join(profile.get('meter_names', [])[:20]) or '无'}。\n"
            f"官方计费单位：{', '.join(profile.get('units', [])[:20]) or '无'}。\n"
            "只填写客户明确提供的值，空缺保持 null；不得根据价格反推或编造用量。"
        )
