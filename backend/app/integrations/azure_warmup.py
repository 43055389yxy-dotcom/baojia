from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.integrations.azure_catalog import AzureOfficialCatalog

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
    def __init__(self, catalog: AzureOfficialCatalog):
        self._catalog = catalog
        self.status = AzureWarmupStatus()

    async def warm(self, *, refresh_profiles: bool = False) -> None:
        self.status = AzureWarmupStatus(state="running")
        semaphore = asyncio.Semaphore(3)

        async def warm_one(service: str, region: str) -> None:
            async with semaphore:
                try:
                    await self._catalog.retail_items(
                        service_name=service,
                        region=region,
                    )
                    await self._catalog.sync_service_profile(
                        service_name=service,
                        region=region,
                        force_refresh=refresh_profiles,
                    )
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

        await asyncio.gather(
            warm_regions(),
            *(
                warm_one(service, region)
                for region in AZURE_WARM_REGIONS
                for service in AZURE_WARM_SERVICES
            )
        )
        self.status.state = "ready" if self.status.failed == 0 else "ready_with_warnings"
