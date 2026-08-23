from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.config import Settings
from app.core.errors import QuoteError
from app.integrations.azure_cache import PersistentAzureCache

RETAIL_ENDPOINT = "https://prices.azure.com/api/retail/prices"


class AzureOfficialCatalog:
    """Read-only Microsoft catalog access with bounded in-memory caching."""

    def __init__(
        self,
        settings: Settings,
        persistent_cache: PersistentAzureCache | None = None,
    ):
        self._settings = settings
        self._persistent = persistent_cache or PersistentAzureCache()
        self._retail_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._sku_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._token: tuple[float, str] | None = None

    async def retail_items(
        self,
        *,
        service_name: str,
        region: str | None,
        arm_sku_name: str | None = None,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        filters = [f"serviceName eq '{self._escape(service_name)}'"]
        if region and region != "global":
            filters.append(f"armRegionName eq '{self._escape(region)}'")
        if arm_sku_name:
            filters.append(f"armSkuName eq '{self._escape(arm_sku_name)}'")
        filter_text = " and ".join(filters)
        cache_key = filter_text.casefold()
        cached = self._retail_cache.get(cache_key)
        if not force_refresh and cached and cached[0] > time.monotonic():
            return [dict(item) for item in cached[1]]
        persistent_key = self._persistent.key("retail-v1", filter_text)
        persistent = await asyncio.to_thread(self._persistent.get, persistent_key)
        if not force_refresh and isinstance(persistent, list):
            rows = [dict(item) for item in persistent if isinstance(item, dict)]
            self._retail_cache[cache_key] = (time.monotonic() + 900, rows)
            return rows

        url: str | None = RETAIL_ENDPOINT
        params: dict[str, str] | None = {
            "api-version": "2023-01-01-preview",
            "$filter": filter_text,
            "currencyCode": "USD",
        }
        rows: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
                for _ in range(50):
                    if not url:
                        break
                    response = await client.get(url, params=params)
                    response.raise_for_status()
                    payload = response.json()
                    items = payload.get("Items")
                    if isinstance(items, list):
                        rows.extend(dict(item) for item in items if isinstance(item, dict))
                    url = payload.get("NextPageLink") or payload.get("nextPageLink")
                    params = None
                else:
                    raise QuoteError(
                        "azure_catalog_pagination_limit",
                        "Azure 官方价格目录分页数量异常，本次停止报价。",
                    )
        except QuoteError:
            raise
        except Exception as exc:
            raise QuoteError(
                "azure_retail_api_failed",
                "Azure Retail Prices API 暂时未返回价格，请重试。",
                {"type": type(exc).__name__},
                503,
            ) from exc
        self._retail_cache[cache_key] = (time.monotonic() + 3600, rows)
        await asyncio.to_thread(
            self._persistent.set,
            persistent_key,
            rows,
            ttl_seconds=6 * 60 * 60,
        )
        return [dict(item) for item in rows]

    async def compute_skus(self, region: str) -> list[dict[str, Any]]:
        """Return subscription-valid VM SKUs when Azure credentials are configured."""

        if not self._settings.azure_subscription_id or not self._settings.azure_account_configured:
            return []
        cached = self._sku_cache.get(region)
        if cached and cached[0] > time.monotonic():
            return [dict(item) for item in cached[1]]
        persistent_key = self._persistent.key(
            "compute-skus-v1",
            {"subscription": self._settings.azure_subscription_id, "region": region},
        )
        persistent = await asyncio.to_thread(self._persistent.get, persistent_key)
        if isinstance(persistent, list):
            rows = [dict(item) for item in persistent if isinstance(item, dict)]
            self._sku_cache[region] = (time.monotonic() + 3600, rows)
            return rows
        token = await self._access_token()
        url = (
            "https://management.azure.com/subscriptions/"
            f"{self._settings.azure_subscription_id}/providers/Microsoft.Compute/skus"
        )
        params: dict[str, str] | None = {
            "api-version": "2021-07-01",
            "$filter": f"location eq '{region}'",
        }
        rows: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=35, trust_env=False) as client:
                for _ in range(30):
                    response = await client.get(
                        url,
                        params=params,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    response.raise_for_status()
                    payload = response.json()
                    values = payload.get("value")
                    if isinstance(values, list):
                        rows.extend(dict(item) for item in values if isinstance(item, dict))
                    url = payload.get("nextLink")
                    params = None
                    if not url:
                        break
        except Exception as exc:
            raise QuoteError(
                "azure_resource_skus_failed",
                "Azure 账号未能返回订阅可用 SKU，本次停止自动选型。",
                {"type": type(exc).__name__},
                503,
            ) from exc
        self._sku_cache[region] = (time.monotonic() + 3600, rows)
        await asyncio.to_thread(
            self._persistent.set,
            persistent_key,
            rows,
            ttl_seconds=24 * 60 * 60,
        )
        return [dict(item) for item in rows]

    async def sync_service_profile(
        self,
        *,
        service_name: str,
        region: str,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Refresh read-only Azure SKU/Meter field metadata for safe reuse."""

        key = self._persistent.key(
            "service-field-profile-v1",
            {"service": service_name, "region": region},
        )
        if not force_refresh:
            cached = await asyncio.to_thread(self._persistent.get, key)
            if isinstance(cached, dict):
                return dict(cached)
        rows = await self.retail_items(
            service_name=service_name,
            region=region,
            force_refresh=force_refresh,
        )
        profile = self._profile_from_rows(service_name, region, rows)
        await asyncio.to_thread(
            self._persistent.set,
            key,
            profile,
            ttl_seconds=24 * 60 * 60,
        )
        return profile

    async def available_regions(self) -> list[dict[str, str]]:
        """Return current public Azure regions with retail VM availability."""

        key = self._persistent.key("available-regions-v1", "commercial-cloud")
        cached = await asyncio.to_thread(self._persistent.get, key)
        if isinstance(cached, list):
            return [dict(item) for item in cached if isinstance(item, dict)]
        rows = await self.retail_items(service_name="Virtual Machines", region=None)
        regions: dict[str, str] = {}
        for row in rows:
            code = str(row.get("armRegionName") or "").strip()
            if not code or code.casefold() == "global":
                continue
            label = str(row.get("location") or code).strip()
            regions.setdefault(code, label)
        result = [
            {"code": code, "label": label}
            for code, label in sorted(regions.items(), key=lambda item: (item[1], item[0]))
        ]
        await asyncio.to_thread(
            self._persistent.set,
            key,
            result,
            ttl_seconds=24 * 60 * 60,
        )
        return result

    @staticmethod
    def _profile_from_rows(
        service_name: str,
        region: str,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        def values(field: str, limit: int = 200) -> list[str]:
            return sorted(
                {
                    str(row.get(field)).strip()
                    for row in rows
                    if str(row.get(field) or "").strip()
                }
            )[:limit]

        return {
            "profile_schema_version": 1,
            "provider": "azure",
            "service_name": service_name,
            "region": region,
            "official_fields": sorted({key for row in rows for key in row}),
            "arm_sku_names": values("armSkuName"),
            "sku_names": values("skuName"),
            "meter_names": values("meterName"),
            "units": values("unitOfMeasure", 50),
            "price_types": values("type", 20),
            "reservation_terms": values("reservationTerm", 20),
            "row_count": len(rows),
            "updated_at": datetime.now(UTC).isoformat(),
        }

    async def _access_token(self) -> str:
        if self._token and self._token[0] > time.monotonic():
            return self._token[1]
        url = (
            f"https://login.microsoftonline.com/{self._settings.azure_tenant_id}/oauth2/v2.0/token"
        )
        data = {
            "client_id": self._settings.azure_client_id,
            "client_secret": self._settings.azure_client_secret,
            "grant_type": "client_credentials",
            "scope": "https://management.azure.com/.default",
        }
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(url, data=data)
            response.raise_for_status()
            payload = response.json()
        token = str(payload["access_token"])
        expires = max(60, int(payload.get("expires_in") or 3600) - 300)
        self._token = (time.monotonic() + expires, token)
        await asyncio.sleep(0)
        return token

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("'", "''")
