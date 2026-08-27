from __future__ import annotations

import time

import pytest

from app.core.config import Settings
from app.integrations.azure_bulk_cache import AzureBulkRetailCache
from app.integrations.azure_cache import PersistentAzureCache
from app.integrations.azure_catalog import AzureOfficialCatalog


def retail_row(
    *,
    service: str,
    service_id: str,
    product_id: str,
    sku_id: str,
    meter_id: str,
    region: str,
    sku: str,
    price: float,
) -> dict[str, object]:
    return {
        "currencyCode": "USD",
        "tierMinimumUnits": 0,
        "retailPrice": price,
        "unitPrice": price,
        "armRegionName": region,
        "location": region,
        "effectiveStartDate": "2026-01-01T00:00:00Z",
        "meterId": meter_id,
        "meterName": f"{sku} Hour",
        "productId": product_id,
        "skuId": sku_id,
        "productName": service,
        "skuName": sku,
        "serviceName": service,
        "serviceId": service_id,
        "serviceFamily": "Compute",
        "unitOfMeasure": "1 Hour",
        "type": "Consumption",
        "armSkuName": sku,
        "isPrimaryMeterRegion": True,
    }


def test_incomplete_snapshot_never_replaces_last_complete_catalog(tmp_path) -> None:
    cache = AzureBulkRetailCache(tmp_path / "azure-bulk.sqlite3")
    old_id, _, _, _ = cache._new_snapshot()
    old_row = retail_row(
        service="Virtual Machines",
        service_id="service-vm",
        product_id="product-old",
        sku_id="sku-old",
        meter_id="meter-old",
        region="southeastasia",
        sku="Standard_D4s_v5",
        price=0.2,
    )
    cache._save_page(old_id, [old_row], None, 1, 1)
    cache._complete(old_id, 1, 1)

    new_id, _, _, _ = cache._new_snapshot()
    new_row = retail_row(
        service="Virtual Machines",
        service_id="service-vm",
        product_id="product-new",
        sku_id="sku-new",
        meter_id="meter-new",
        region="eastus",
        sku="Standard_D8s_v5",
        price=0.4,
    )
    cache._save_page(new_id, [new_row], "next-page", 1, 1)

    visible = cache.retail_items(
        service_name="Virtual Machines",
        region="southeastasia",
    )
    assert visible is not None
    assert [row["meterId"] for row in visible] == ["meter-old"]
    assert cache.retail_items(service_name="Virtual Machines", region="eastus") == []

    cache._complete(new_id, 1, 1)
    switched = cache.retail_items(
        service_name="Virtual Machines",
        region="eastus",
    )
    assert switched is not None
    assert [row["meterId"] for row in switched] == ["meter-new"]


def test_complete_snapshot_exposes_exact_counts_services_and_regions(tmp_path) -> None:
    cache = AzureBulkRetailCache(tmp_path / "azure-bulk.sqlite3")
    snapshot_id, _, _, _ = cache._new_snapshot()
    rows = [
        retail_row(
            service="Virtual Machines",
            service_id="service-vm",
            product_id="product-vm",
            sku_id="sku-vm",
            meter_id="meter-vm",
            region="southeastasia",
            sku="Standard_D4s_v5",
            price=0.2,
        ),
        retail_row(
            service="Functions",
            service_id="service-functions",
            product_id="product-functions",
            sku_id="sku-functions",
            meter_id="meter-functions",
            region="eastus",
            sku="Y1",
            price=0.000016,
        ),
    ]
    rows[0]["savingsPlan"] = [
        {"term": "1 Year", "retailPrice": 0.12},
        {"term": "3 Years", "retailPrice": 0.08},
    ]
    cache._save_page(snapshot_id, rows, None, 1, 2)

    status = cache._complete(snapshot_id, 1, 2)

    assert status["rows"] == 2
    assert status["services"] == 2
    assert status["products"] == 2
    assert status["skus"] == 2
    assert status["meters"] == 2
    assert cache.service_regions("Virtual Machines") == ["southeastasia"]
    assert cache.service_region_options("Virtual Machines") == [
        {"code": "southeastasia", "label": "southeastasia"}
    ]
    cached_vm = cache.retail_items(
        service_name="Virtual Machines",
        region="southeastasia",
    )
    assert cached_vm is not None
    assert cached_vm[0]["savingsPlan"] == rows[0]["savingsPlan"]
    assert all(row["serviceName"] == "Virtual Machines" for row in cached_vm)
    cached_functions = cache.retail_items(
        service_name="Functions",
        region="eastus",
    )
    assert cached_functions is not None
    assert all(row["serviceName"] == "Functions" for row in cached_functions)
    assert [item["service_name"] for item in cache.services()] == [
        "Functions",
        "Virtual Machines",
    ]


@pytest.mark.asyncio
async def test_completed_bulk_snapshot_replaces_stale_empty_memory_result(tmp_path) -> None:
    bulk = AzureBulkRetailCache(tmp_path / "azure-bulk.sqlite3")
    catalog = AzureOfficialCatalog(
        Settings(_env_file=None),
        PersistentAzureCache(tmp_path / "azure-catalog.sqlite3"),
        bulk,
    )
    filter_text = (
        "serviceName eq 'Virtual Machines' and "
        "armRegionName eq 'centralindia' and "
        "armSkuName eq 'Standard_D8s_v5'"
    )
    catalog._retail_cache[filter_text.casefold()] = (time.monotonic() + 3600, [])

    snapshot_id, _, _, _ = bulk._new_snapshot()
    row = retail_row(
        service="Virtual Machines",
        service_id="service-vm",
        product_id="product-vm",
        sku_id="sku-d8s-v5",
        meter_id="meter-d8s-v5",
        region="centralindia",
        sku="Standard_D8s_v5",
        price=0.404,
    )
    bulk._save_page(snapshot_id, [row], None, 1, 1)
    bulk._complete(snapshot_id, 1, 1)

    rows = await catalog.retail_items(
        service_name="Virtual Machines",
        region="centralindia",
        arm_sku_name="Standard_D8s_v5",
    )

    assert len(rows) == 1
    assert rows[0]["armSkuName"] == "Standard_D8s_v5"
    assert rows[0]["unitPrice"] == 0.404
