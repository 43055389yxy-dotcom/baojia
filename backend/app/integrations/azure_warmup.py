from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.integrations.azure_catalog import AzureOfficialCatalog
from app.integrations.azure_product_registry import AzureProductRegistry
from app.integrations.azure_service_templates import azure_requirement_fields

AZURE_WARM_REGIONS = (
    "southeastasia",
    "japaneast",
    "eastasia",
    "eastus",
    "westeurope",
)

AZURE_WARM_SERVICES = (
    "Virtual Machines",
    "Storage",
    "Azure Database for PostgreSQL",
    "Redis Cache",
    "Bandwidth",
)


@dataclass(slots=True)
class AzureWarmupStatus:
    state: str = "idle"
    completed: int = 0
    failed: int = 0
    profiles: int = 0
    regions: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "completed": self.completed,
            "failed": self.failed,
            "profiles": self.profiles,
            "regions": self.regions,
            "errors": self.errors[-10:],
        }


class AzureCatalogWarmer:
    def __init__(
        self,
        catalog: AzureOfficialCatalog,
        product_registry: AzureProductRegistry | None = None,
        service_names: dict[str, str] | None = None,
    ):
        self._catalog = catalog
        self._product_registry = product_registry
        self._service_names = dict(service_names or {})
        self.status = AzureWarmupStatus()

    async def warm(self, *, refresh_profiles: bool = False) -> None:
        self.status = AzureWarmupStatus(state="running")
        semaphore = asyncio.Semaphore(3)

        async def warm_one(
            service: str,
            region: str,
            service_key: str | None = None,
        ) -> None:
            async with semaphore:
                try:
                    await self._catalog.retail_items(
                        service_name=service,
                        region=region,
                    )
                    profile = await self._catalog.sync_service_profile(
                        service_name=service,
                        region=region,
                        force_refresh=refresh_profiles,
                    )
                    if (
                        self._product_registry is not None
                        and service_key is not None
                        and int(profile.get("row_count") or 0) > 0
                    ):
                        profile.update(
                            {
                                "service_key": service_key,
                                "display_name": service_key.replace("_", " ").title(),
                                "fields": list(azure_requirement_fields(service_key)),
                            }
                        )
                        self._product_registry.update_profile(profile)
                    self.status.completed += 1
                    self.status.profiles += 1
                except Exception as exc:
                    self.status.failed += 1
                    self.status.errors.append(f"{service}/{region}: {type(exc).__name__}")

        async def warm_regions() -> None:
            try:
                regions = await self._catalog.available_regions()
                self.status.regions = len(regions)
            except Exception as exc:
                self.status.errors.append(f"regions: {type(exc).__name__}")

        primary_targets = [
            warm_one(service_name, "southeastasia", service_key)
            for service_key, service_name in self._service_names.items()
        ]
        hot_targets = [
            warm_one(service, region)
            for region in AZURE_WARM_REGIONS
            for service in AZURE_WARM_SERVICES
            if region != "southeastasia"
        ]
        await asyncio.gather(warm_regions(), *primary_targets, *hot_targets)
        self.status.state = "ready" if self.status.failed == 0 else "ready_with_warnings"
