from __future__ import annotations

import pytest

from app.integrations.azure_adaptation_audit import AzureAdaptationAudit
from app.integrations.azure_product_registry import AzureProductRegistry
from app.integrations.azure_warmup import AzureCatalogWarmer
from app.services.azure_plugins import AZURE_RETAIL_SERVICE_NAMES


def test_azure_registry_keeps_components_with_shared_service_name_isolated(
    tmp_path,
) -> None:
    registry = AzureProductRegistry(tmp_path / "azure-only.sqlite3")

    registry.register_identity(
        service_key="managed_disks",
        display_name="Azure Managed Disks",
        service_name="Storage",
    )
    registry.register_identity(
        service_key="blob_storage",
        display_name="Azure Blob Storage",
        service_name="Storage",
    )

    products = {item["service_key"]: item for item in registry.list_products()}
    assert set(products) == {"managed_disks", "blob_storage"}
    assert products["managed_disks"]["policy"]["aws_data_access"] == "forbidden"
    assert products["blob_storage"]["policy"]["aws_data_access"] == "forbidden"
    assert registry.resolve_product("Storage") is None


def test_azure_dynamic_profile_survives_process_restart(tmp_path) -> None:
    database = tmp_path / "azure-only.sqlite3"
    first = AzureProductRegistry(database)
    first.update_profile(
        {
            "service_key": "azure_functions",
            "display_name": "Azure Functions",
            "service_name": "Functions",
            "region": "southeastasia",
            "fields": ["monthly_quantity", "usage_unit"],
            "arm_sku_names": ["Y1", "EP1"],
            "meter_names": ["Execution Time"],
            "units": ["1 GB Second"],
        }
    )

    restarted = AzureProductRegistry(database)
    product = restarted.resolve_product("azure_functions")

    assert product is not None
    assert product["profile_status"] == "profile_ready"
    assert product["regions"] == ["southeastasia"]
    assert product["field_template"]["arm_sku_names"] == ["Y1", "EP1"]


def test_azure_twelve_layer_audit_reports_full_provider_boundary(tmp_path) -> None:
    registry = AzureProductRegistry(tmp_path / "azure-only.sqlite3")
    registry.bootstrap(
        AZURE_RETAIL_SERVICE_NAMES,
        {key: key for key in AZURE_RETAIL_SERVICE_NAMES},
    )

    report = AzureAdaptationAudit(registry).report()

    assert len(report["stages"]) == 12
    assert report["summary"]["full_isolation_coverage"] is True
    assert report["summary"]["registered_products"] == len(AZURE_RETAIL_SERVICE_NAMES)
    assert registry.database_path.name == "azure-only.sqlite3"


class WarmupCatalog:
    async def retail_items(self, **_: object) -> list[dict[str, object]]:
        return [{"meterId": "meter"}]

    async def sync_service_profile(self, **kwargs: object) -> dict[str, object]:
        return {
            "service_name": kwargs["service_name"],
            "region": kwargs["region"],
            "row_count": 1,
            "arm_sku_names": ["OfficialSku"],
            "meter_names": ["Official Meter"],
            "units": ["1 Hour"],
        }

    async def available_regions(self) -> list[dict[str, str]]:
        return [{"code": "southeastasia", "label": "Southeast Asia"}]


@pytest.mark.asyncio
async def test_azure_warmup_materializes_each_component_profile_independently(
    tmp_path,
) -> None:
    registry = AzureProductRegistry(tmp_path / "azure-only.sqlite3")
    service_names = {
        "managed_disks": "Storage",
        "blob_storage": "Storage",
    }
    registry.bootstrap(service_names, {})
    warmer = AzureCatalogWarmer(
        WarmupCatalog(),  # type: ignore[arg-type]
        registry,
        service_names,
    )

    await warmer.warm()

    products = {item["service_key"]: item for item in registry.list_products()}
    assert products["managed_disks"]["profile_status"] == "profile_ready"
    assert products["blob_storage"]["profile_status"] == "profile_ready"
    assert (
        products["managed_disks"]["field_template"]["fields"]
        != products["blob_storage"]["field_template"]["fields"]
    )
