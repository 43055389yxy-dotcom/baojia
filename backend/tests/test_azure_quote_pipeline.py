from __future__ import annotations

import pytest

from app.core.errors import ManualConfirmationRequired
from app.domain.models import ParsedIntent, PreviewSelection, QuoteRequest, ServiceRequirement
from app.integrations.azure_auto_service_discovery import AzureAutoServiceDiscovery
from app.integrations.azure_cache import PersistentAzureCache
from app.integrations.azure_catalog import AzureOfficialCatalog
from app.integrations.azure_intent import (
    AzureIntentParser,
    azure_numbered_component_identity,
    split_numbered_components,
)
from app.integrations.component_result_cache import ValidatedComponentResultCache
from app.services.azure_plugins import (
    AzureGenericRetailPlugin,
    AzurePluginRegistry,
    AzureRetailPlugin,
    _rate_model,
)
from app.services.azure_quote_service import AzureQuoteService
from app.services.confirmation_sessions import (
    CONFIGURATION_COMPONENT_FEEDBACK_PREFIX,
    ConfirmationSessionStore,
)


def test_azure_sales_numbering_is_a_hard_component_boundary() -> None:
    text = """区域：新加坡
1、Azure Virtual Machines
数量：4台
配置：8核32G
2、Azure Database for PostgreSQL
数量：1套
存储：500GB
3、Azure Cache for Redis
节点：2个
每节点内存：8GB"""

    components = split_numbered_components(text)

    assert len(components) == 3
    assert "数量：4台" in components[0]
    assert "PostgreSQL" not in components[0]
    assert "存储：500GB" in components[1]
    assert "每节点内存：8GB" in components[2]
    assert all("区域：新加坡" in component for component in components)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Azure 虚拟机 Standard_D4s_v5", "azure_vm"),
        ("Azure 托管磁盘 Premium SSD P20", "managed_disks"),
        ("Azure Database for PostgreSQL", "azure_postgresql"),
        ("Azure Cache for Redis Standard C1", "azure_cache"),
        ("公网出站流量 2TB", "bandwidth"),
    ],
)
def test_azure_numbered_blocks_are_classified_before_ai(
    text: str, expected: str
) -> None:
    assert azure_numbered_component_identity(text)[0] == expected


@pytest.mark.parametrize(
    ("resource_type", "expected"),
    [
        ("Microsoft.Compute/virtualMachines", "azure_vm"),
        ("Microsoft.Compute/disks", "managed_disks"),
        ("Azure Redis Cache", "azure_cache"),
        ("Microsoft.Cache/redis", "azure_cache"),
    ],
)
def test_azure_resource_provider_names_resolve_to_quote_plugins(
    resource_type: str, expected: str
) -> None:
    registry = AzurePluginRegistry(FakeCatalog([]))  # type: ignore[arg-type]

    assert registry.get(resource_type).service == expected


class ComponentGateway:
    def __init__(self) -> None:
        self.contents: list[str] = []

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        content = str(kwargs.get("user_content") or "")
        self.contents.append(content)
        if "PostgreSQL" in content:
            service = "azure_postgresql"
            name = "Azure Database for PostgreSQL"
            requirements = {"storage_gib": 500, "requested_sku": "Standard_D2ds_v5"}
        else:
            service = "azure_vm"
            name = "Azure Virtual Machines"
            requirements = {
                "requested_sku": "Standard_D4s_v5",
                "vcpu": 4,
                "memory_gib": 16,
            }
        return {
            "component": {
                "service": service,
                "calculator_service_name": name,
                "product_identity": service,
                "region": "southeastasia",
                "quantity": 1,
                "hours_per_month": 730,
                "requirements": requirements,
                "source_text": content,
                "query_action": None,
            }
        }


@pytest.mark.asyncio
async def test_numbered_azure_components_each_get_an_isolated_ai_call() -> None:
    gateway = ComponentGateway()
    parser = AzureIntentParser(gateway)  # type: ignore[arg-type]

    intent = await parser.parse(
        """区域：新加坡
1、Azure VM Standard_D4s_v5，4核16G
2、Azure Database for PostgreSQL，存储500G"""
    )

    assert len(gateway.contents) == 2
    assert "PostgreSQL" not in gateway.contents[0]
    assert "Standard_D4s_v5" not in gateway.contents[1]
    assert [item.service for item in intent.services] == [
        "azure_vm",
        "azure_postgresql",
    ]


class NullableOperationalFieldsGateway:
    async def complete_json(self, **_: object) -> dict[str, object]:
        return {
            "component": {
                "service": "managed_disks",
                "calculator_service_name": "Azure Managed Disks",
                "product_identity": "managed_disks",
                "region": "southeastasia",
                "quantity": None,
                "hours_per_month": None,
                "requirements": {
                    "requested_sku": "P20",
                    "storage_gib": 512,
                },
                "source_text": "",
                "query_action": None,
            }
        }


@pytest.mark.asyncio
async def test_azure_absent_operational_fields_use_safe_defaults() -> None:
    parser = AzureIntentParser(NullableOperationalFieldsGateway())  # type: ignore[arg-type]

    intent = await parser.parse("1、Azure 托管磁盘 Premium SSD P20，512GB")

    assert intent.services[0].quantity == 1
    assert intent.services[0].hours_per_month == 730


class OneMalformedComponentGateway:
    def __init__(self) -> None:
        self.disk_attempts = 0

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        content = str(kwargs.get("user_content") or "")
        if "托管磁盘" in content:
            self.disk_attempts += 1
            return {
                "component": {
                    "service": "managed_disks",
                    "region": "southeastasia",
                    "quantity": 0,
                    "hours_per_month": 730,
                    "requirements": {"requested_sku": "P20"},
                }
            }
        return {
            "component": {
                "service": "azure_vm",
                "region": "southeastasia",
                "quantity": 1,
                "hours_per_month": 730,
                "requirements": {"requested_sku": "Standard_D4s_v5"},
            }
        }


@pytest.mark.asyncio
async def test_one_bad_azure_component_does_not_discard_valid_components() -> None:
    gateway = OneMalformedComponentGateway()
    parser = AzureIntentParser(gateway)  # type: ignore[arg-type]

    intent = await parser.parse(
        "1、Azure VM Standard_D4s_v5\n2、Azure 托管磁盘 P20"
    )

    assert len(intent.services) == 2
    assert intent.services[0].service == "azure_vm"
    assert intent.services[1].service == "managed_disks"
    assert intent.services[1].requirements["requested_sku"] == "P20"
    assert gateway.disk_attempts == 3


@pytest.mark.parametrize(
    ("service", "source", "expected"),
    [
        ("azure_vm", "Linux Standard_D4s_v5", "Standard_D4s_v5"),
        ("managed_disks", "Premium SSD，P20，512GB", "P20"),
        ("azure_cache", "Standard C1，容量1GB", "Standard C1"),
    ],
)
def test_azure_literal_sku_overrides_ai_field_misplacement(
    service: str, source: str, expected: str
) -> None:
    component = ServiceRequirement(
        service=service,
        source_text=source,
        requirements={"requested_sku": "wrong-family-label"},
    )

    AzureIntentParser._reconcile_explicit_sku(component)

    assert component.requirements["requested_sku"] == expected


@pytest.mark.asyncio
async def test_azure_component_ai_results_are_persistently_cached(tmp_path) -> None:
    gateway = ComponentGateway()
    cache = ValidatedComponentResultCache(tmp_path / "azure-components.sqlite3")
    parser = AzureIntentParser(
        gateway,  # type: ignore[arg-type]
        cache,
        "test-model",
    )
    text = "1、Azure VM Standard_D4s_v5，4核16G"

    first = await parser.parse(text)
    second = await parser.parse(text)

    assert len(gateway.contents) == 1
    assert first.services[0].requirements == second.services[0].requirements


def test_azure_official_catalog_cache_is_provider_isolated(tmp_path) -> None:
    cache = PersistentAzureCache(tmp_path / "azure-catalog.sqlite3")
    key = cache.key("retail-v1", {"service": "Virtual Machines"})

    cache.set(key, [{"meterId": "official-meter"}], ttl_seconds=60)

    assert cache.get(key) == [{"meterId": "official-meter"}]
    assert cache.status() == {"total": 1, "fresh": 1, "expired": 0}


def test_azure_official_field_profile_tracks_current_catalog_shape() -> None:
    profile = AzureOfficialCatalog._profile_from_rows(
        "Virtual Machines",
        "southeastasia",
        [
            {
                "armSkuName": "Standard_D4s_v5",
                "skuName": "D4s v5",
                "meterName": "D4s v5",
                "unitOfMeasure": "1 Hour",
                "type": "Consumption",
                "unitPrice": 0.1,
            }
        ],
    )

    assert profile["arm_sku_names"] == ["Standard_D4s_v5"]
    assert profile["units"] == ["1 Hour"]
    assert "unitPrice" in profile["official_fields"]


class FakeCatalog:
    def __init__(self, rows: list[dict[str, object]]):
        self.rows = rows

    async def retail_items(self, **_: object) -> list[dict[str, object]]:
        return self.rows

    async def compute_skus(self, _: str) -> list[dict[str, object]]:
        return []


class AutoDiscoveryCatalog(FakeCatalog):
    def __init__(self, rows: list[dict[str, object]]):
        super().__init__(rows)
        self.profile_requests: list[tuple[str, str, bool]] = []

    async def sync_service_profile(
        self,
        *,
        service_name: str,
        region: str,
        force_refresh: bool = False,
    ) -> dict[str, object]:
        self.profile_requests.append((service_name, region, force_refresh))
        if service_name != "Functions":
            return {"row_count": 0}
        return {
            "row_count": len(self.rows),
            "arm_sku_names": ["Y1", "EP1"],
            "meter_names": ["Execution Time", "Premium Plan"],
            "units": ["1 GB Second"],
        }


def functions_row(*, sku: str, price: float) -> dict[str, object]:
    return {
        "serviceName": "Functions",
        "productName": "Azure Functions",
        "armSkuName": sku,
        "skuName": sku,
        "meterName": f"{sku} Execution Time",
        "type": "Consumption",
        "unitPrice": price,
        "unitOfMeasure": "1 GB Second",
        "productId": "functions-product",
        "skuId": f"functions-{sku}",
        "meterId": f"functions-meter-{sku}",
    }


def generic_azure_row(
    *,
    service: str,
    product: str,
    arm_sku: str,
    sku: str,
    meter: str,
    unit: str,
    price: float,
) -> dict[str, object]:
    return {
        "serviceName": service,
        "productName": product,
        "armSkuName": arm_sku,
        "skuName": sku,
        "meterName": meter,
        "type": "Consumption",
        "unitPrice": price,
        "unitOfMeasure": unit,
        "productId": "product",
        "skuId": "sku",
        "meterId": f"{service}-{arm_sku}-{sku}-{meter}",
    }


@pytest.mark.asyncio
async def test_postgresql_vcore_requirement_cannot_select_one_vcore_meter() -> None:
    plugin = AzureRetailPlugin(
        "azure_postgresql",
        FakeCatalog(
            [
                generic_azure_row(
                    service="Azure Database for PostgreSQL",
                    product="Flexible Server General Purpose Compute",
                    arm_sku="Generic",
                    sku="1 vCore",
                    meter="vCore",
                    unit="1 Hour",
                    price=0.12,
                ),
                generic_azure_row(
                    service="Azure Database for PostgreSQL",
                    product="Flexible Server General Purpose Compute",
                    arm_sku="Standard_D4ds_v5",
                    sku="4 vCore",
                    meter="vCore",
                    unit="1 Hour",
                    price=0.48,
                ),
            ]
        ),  # type: ignore[arg-type]
    )
    requirement = ServiceRequirement(
        service="azure_postgresql",
        region="southeastasia",
        requirements={"service_tier": "General Purpose", "vcore": 4},
    )

    preview = await plugin.preview(
        requirement,
        QuoteRequest(cloud_provider="azure", customer_request="PostgreSQL 4 vCore"),
        "0",
    )

    assert preview.selected_model == "Standard_D4ds_v5"
    assert all(candidate.model != "Generic" for candidate in preview.candidates)


@pytest.mark.asyncio
async def test_monitor_log_ingestion_does_not_select_unrelated_low_price_meter() -> None:
    plugin = AzureRetailPlugin(
        "monitor",
        FakeCatalog(
            [
                generic_azure_row(
                    service="Azure Monitor",
                    product="Azure Monitor",
                    arm_sku="Standard Web Test",
                    sku="Standard Web Test",
                    meter="Standard Web Test Execution",
                    unit="1",
                    price=0.00065,
                ),
                generic_azure_row(
                    service="Azure Monitor",
                    product="Azure Monitor",
                    arm_sku="Basic Logs",
                    sku="Basic Logs",
                    meter="Basic Logs Data Ingestion",
                    unit="1 GB",
                    price=0.65,
                ),
                generic_azure_row(
                    service="Azure Monitor",
                    product="Azure Monitor",
                    arm_sku="Auxiliary Logs",
                    sku="Auxiliary Logs",
                    meter="Auxiliary Logs Data Ingestion",
                    unit="1 GB",
                    price=0.07,
                ),
            ]
        ),  # type: ignore[arg-type]
    )
    requirement = ServiceRequirement(
        service="monitor",
        region="southeastasia",
        requirements={"log_ingestion_gib": 100},
    )

    preview = await plugin.preview(
        requirement,
        QuoteRequest(cloud_provider="azure", customer_request="Monitor logs 100 GiB"),
        "0",
    )

    assert preview.requires_confirmation is True
    assert {candidate.model for candidate in preview.candidates} == {
        "Basic Logs",
        "Auxiliary Logs",
    }


def test_application_gateway_waf_v2_keeps_customer_facing_model_name() -> None:
    row = generic_azure_row(
        service="Application Gateway",
        product="Application Gateway WAF v2 - Discounted",
        arm_sku="Standard",
        sku="Standard",
        meter="Capacity Unit",
        unit="1 Hour",
        price=0.008,
    )

    assert _rate_model(row) == "WAF_v2"


@pytest.mark.asyncio
async def test_unknown_azure_component_builds_profile_from_official_catalog() -> None:
    catalog = AutoDiscoveryCatalog([functions_row(sku="Y1", price=0.000016)])
    discovery = AzureAutoServiceDiscovery(catalog)  # type: ignore[arg-type]

    profile = await discovery.ensure_profile(
        service_key="azure_functions",
        display_name="Azure Functions",
        region="southeastasia",
    )

    assert profile["service_name"] == "Functions"
    assert profile["fields"] == ["monthly_quantity", "requested_sku", "usage_unit"]
    assert "Retail serviceName：Functions" in str(profile["prompt_text"])
    assert catalog.profile_requests[:2] == [
        ("Azure Functions", "southeastasia", False),
        ("Functions", "southeastasia", False),
    ]


@pytest.mark.asyncio
async def test_unknown_azure_component_uses_generic_official_plugin_and_customer_choice() -> None:
    catalog = AutoDiscoveryCatalog(
        [
            functions_row(sku="Y1", price=0.000016),
            functions_row(sku="EP1", price=0.20),
        ]
    )
    discovery = AzureAutoServiceDiscovery(catalog)  # type: ignore[arg-type]
    registry = AzurePluginRegistry(catalog, discovery)  # type: ignore[arg-type]
    plugin = registry.get("azure_functions")
    requirement = ServiceRequirement(
        service="azure_functions",
        calculator_service_name="Azure Functions",
        region="southeastasia",
        requirements={"monthly_quantity": 1_000_000},
    )
    request = QuoteRequest(cloud_provider="azure", customer_request="Azure Functions")

    assert isinstance(plugin, AzureGenericRetailPlugin)
    preview = await plugin.preview(requirement, request, "0")
    assert preview.requires_confirmation is True
    assert {candidate.model for candidate in preview.candidates} == {"Y1", "EP1"}
    assert "尚未安装" not in str(preview.issue_message or preview.confirmation_reason)

    requirement.requirements["requested_sku"] = "Y1"
    resolved = await plugin.preview(requirement, request, "0")
    quote = await plugin.quote(requirement, request, 0)
    assert resolved.status == "ready"
    assert resolved.selected_model == "Y1"
    assert quote.selection.official_product["source"] == "Microsoft Azure Retail Prices API"
    assert quote.priced_lines[0].amount == 1_000_000


@pytest.mark.asyncio
async def test_dynamic_azure_profiles_are_refreshed_periodically() -> None:
    catalog = AutoDiscoveryCatalog([functions_row(sku="Y1", price=0.000016)])
    discovery = AzureAutoServiceDiscovery(catalog)  # type: ignore[arg-type]
    await discovery.ensure_profile(
        service_key="azure_functions",
        display_name="Azure Functions",
        region="southeastasia",
    )

    result = await discovery.refresh_used_profiles()

    assert result == {"refreshed": 1, "failed": 0}
    assert catalog.profile_requests[-1] == ("Functions", "southeastasia", True)


class DynamicAzureComponentGateway:
    def __init__(self) -> None:
        self.system_prompts: list[str] = []

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.system_prompts.append(str(kwargs.get("system_prompt") or ""))
        return {
            "component": {
                "service": "azure_functions",
                "calculator_service_name": "Azure Functions",
                "product_identity": "azure_functions",
                "region": "southeastasia",
                "quantity": 1,
                "hours_per_month": 730,
                "requirements": {"monthly_quantity": 1_000_000},
                "source_text": "Azure Functions",
                "query_action": None,
            }
        }


@pytest.mark.asyncio
async def test_unknown_component_gets_a_second_ai_pass_with_generated_azure_prompt() -> None:
    gateway = DynamicAzureComponentGateway()
    catalog = AutoDiscoveryCatalog([functions_row(sku="Y1", price=0.000016)])
    parser = AzureIntentParser(
        gateway,  # type: ignore[arg-type]
        auto_discovery=AzureAutoServiceDiscovery(catalog),  # type: ignore[arg-type]
    )

    intent = await parser.parse("1、Azure Functions，每月 100 万 GB-s，新加坡")

    assert len(gateway.system_prompts) == 2
    assert "其他 Azure 组件通用规则" in gateway.system_prompts[0]
    assert "Azure 官方自动发现：Azure Functions" in gateway.system_prompts[1]
    assert "Retail serviceName：Functions" in gateway.system_prompts[1]
    assert intent.services[0].service == "azure_functions"


class TimedOutAzureGateway:
    async def complete_json(self, **_: object) -> dict[str, object]:
        raise TimeoutError("AI unavailable")


@pytest.mark.asyncio
async def test_numbered_unknown_component_survives_ai_timeout() -> None:
    catalog = AutoDiscoveryCatalog([functions_row(sku="Y1", price=0.000016)])
    parser = AzureIntentParser(
        TimedOutAzureGateway(),  # type: ignore[arg-type]
        auto_discovery=AzureAutoServiceDiscovery(catalog),  # type: ignore[arg-type]
    )

    intent = await parser.parse(
        "区域：新加坡\n1、Azure Functions\n每月执行用量：1000000 GB-s"
    )

    assert len(intent.services) == 1
    assert intent.services[0].service == "azure_functions"
    assert intent.services[0].calculator_service_name == "Azure Functions"
    assert intent.services[0].region == "southeastasia"


def vm_row(
    *,
    price: float,
    meter: str = "D4s v5",
    sku: str = "Standard_D4s_v5",
    price_type: str = "Consumption",
    term: str | None = None,
) -> dict[str, object]:
    return {
        "serviceName": "Virtual Machines",
        "productName": "Virtual Machines Dsv5 Series",
        "armSkuName": sku,
        "skuName": meter,
        "meterName": meter,
        "type": price_type,
        "reservationTerm": term,
        "unitPrice": price,
        "unitOfMeasure": "1 Hour",
        "productId": "product",
        "skuId": "sku",
        "meterId": meter,
    }


@pytest.mark.asyncio
async def test_azure_vm_quote_uses_exact_payg_meter_and_excludes_spot() -> None:
    plugin = AzureRetailPlugin(
        "azure_vm",
        FakeCatalog(
            [
                vm_row(price=0.02, meter="D4s v5 Spot"),
                vm_row(price=0.10),
            ]
        ),  # type: ignore[arg-type]
    )
    requirement = ServiceRequirement(
        service="azure_vm",
        region="southeastasia",
        quantity=2,
        requirements={
            "requested_sku": "Standard_D4s_v5",
            "operating_system": "linux",
        },
    )

    result = await plugin.quote(
        requirement,
        QuoteRequest(
            cloud_provider="azure",
            customer_request="Azure VM Standard_D4s_v5",
        ),
        0,
    )

    assert len(result.priced_lines) == 1
    assert result.priced_lines[0].operation == "D4s v5"
    assert result.priced_lines[0].amount == 1460
    assert result.priced_lines[0].cost == pytest.approx(146.0)


@pytest.mark.asyncio
async def test_azure_reservation_converts_commitment_to_monthly_and_upfront() -> None:
    plugin = AzureRetailPlugin(
        "azure_vm",
        FakeCatalog(
            [
                vm_row(price=0.10),
                vm_row(price=1200, price_type="Reservation", term="1 Year"),
            ]
        ),  # type: ignore[arg-type]
    )
    requirement = ServiceRequirement(
        service="azure_vm",
        region="southeastasia",
        requirements={"requested_sku": "Standard_D4s_v5"},
    )

    result = await plugin.quote(
        requirement,
        QuoteRequest(
            cloud_provider="azure",
            customer_request="Azure VM Standard_D4s_v5",
            azure_pricing_mode="reservation",
            azure_term_years=1,
            azure_payment_option="upfront",
        ),
        0,
    )

    assert result.priced_lines[0].cost == pytest.approx(100.0)
    assert result.upfront_cost == pytest.approx(1200.0)


@pytest.mark.asyncio
async def test_public_vm_exact_shape_automatically_selects_lowest_official_sku() -> None:
    plugin = AzureRetailPlugin(
        "azure_vm",
        FakeCatalog([vm_row(price=0.10)]),  # type: ignore[arg-type]
    )
    requirement = ServiceRequirement(
        service="azure_vm",
        region="southeastasia",
        requirements={"vcpu": 4, "memory_gib": 16},
    )

    preview = await plugin.preview(
        requirement,
        QuoteRequest(
            cloud_provider="azure",
            customer_request="Azure VM 4核16G",
        ),
        "0",
    )

    assert preview.requires_confirmation is False
    assert preview.selected_model == "Standard_D4s_v5"
    assert [candidate.model for candidate in preview.candidates] == ["Standard_D4s_v5"]


class AzureVmShapeRevisionParser:
    async def parse(self, _: str, reporter=None) -> ParsedIntent:  # noqa: ANN001
        raise AssertionError("saved Azure draft must be reused")

    async def revise_component_from_feedback(
        self,
        _original: str,
        component: ServiceRequirement,
        _feedback: str,
        reporter=None,  # noqa: ANN001
    ) -> ServiceRequirement:
        revised = component.model_copy(deep=True)
        revised.requirements["vcpu"] = 4
        revised.requirements["memory_gib"] = 128
        return revised


def test_saved_legacy_azure_vm_shape_conflict_is_quarantined() -> None:
    intent = ParsedIntent(
        customer_summary="Azure VM",
        services=[
            ServiceRequirement(
                service="azure_vm",
                region="southeastasia",
                requirements={
                    "requested_sku": "Standard_D4s_v5",
                    "vcpu": 4,
                    "memory_gib": 128,
                    "_review_selected_model": "Standard_D4s_v5",
                },
                source_text=(
                    "客户最新修改：4核128G\n"
                    "客户原始配置：Standard_D4s_v5，4核16GB"
                ),
            )
        ],
    )

    AzureQuoteService._repair_legacy_vm_shape_conflicts(intent)

    requirements = intent.services[0].requirements
    assert "requested_sku" not in requirements
    assert "_review_selected_model" not in requirements
    assert requirements["_customer_select_official_sku"] is True


@pytest.mark.asyncio
async def test_invalid_azure_vm_shape_edit_requires_official_sku_selection(
    tmp_path,
) -> None:
    store = ConfirmationSessionStore(tmp_path / "vm-shape-edit.sqlite3", "azure")
    service = AzureQuoteService(
        AzureVmShapeRevisionParser(),  # type: ignore[arg-type]
        AzurePluginRegistry(
            FakeCatalog(
                [
                    vm_row(price=0.10),
                    vm_row(
                        price=0.80,
                        meter="E16s v5",
                        sku="Standard_E16s_v5",
                    ),
                    vm_row(
                        price=0.82,
                        meter="E16-4s v5",
                        sku="Standard_E16-4s_v5",
                    ),
                ]
            )  # type: ignore[arg-type]
        ),
        store,
    )
    draft_id = "azshape00001"
    request_text = "Azure VM"
    service._drafts[draft_id] = (
        request_text,
        ParsedIntent(
            customer_summary="Azure VM",
            services=[
                ServiceRequirement(
                    service="azure_vm",
                    calculator_service_name="Azure Virtual Machines",
                    region="southeastasia",
                    quantity=3,
                    requirements={
                        "requested_sku": "Standard_D4s_v5",
                        "vcpu": 4,
                        "memory_gib": 16,
                        "_review_selected_model": "Standard_D4s_v5",
                    },
                )
            ],
        ),
    )

    preview = await service.preview(
        QuoteRequest(
            cloud_provider="azure",
            customer_request=request_text,
            draft_id=draft_id,
            confirmation_responses={
                f"{CONFIGURATION_COMPONENT_FEEDBACK_PREFIX}0": "改成4核128G"
            },
        )
    )

    assert preview.configuration_review_required is False
    assert len(preview.confirmation_items) == 1
    item = preview.confirmation_items[0]
    assert item.component_id == "0"
    assert item.selection_mode == "catalog"
    assert "4 核 128 GiB" in item.question
    assert "匹配或最接近" in item.question
    assert [option.model for option in item.options] == ["Standard_E16-4s_v5"]
    assert item.options[0].specifications == {"vCPU": 4.0, "memoryGiB": 128.0}
    pending = service._drafts[draft_id][1].services[0]
    assert "requested_sku" not in pending.requirements
    assert pending.requirements["memory_gib"] == 128

    submitted = store.submit(
        preview.confirmation_token or "",
        {item.question: "选择 Standard_E16-4s_v5"},
    )
    assert submitted is not None
    resolved = await service.preview(
        QuoteRequest(
            cloud_provider="azure",
            customer_request=request_text,
            draft_id=draft_id,
            confirmation_responses=submitted.answers,
        )
    )

    assert resolved.confirmation_items == []
    assert resolved.configuration_review_required is True
    session = store.get(preview.confirmation_token or "")
    assert session is not None
    assert session.status == "configuration_review"
    assert session.configuration_items[0].selected_model == "Standard_E16-4s_v5"
    assert "memory_gib" not in session.configuration_items[0].requirements


@pytest.mark.asyncio
async def test_initial_exact_vm_shape_is_automatically_selected() -> None:
    plugin = AzureRetailPlugin(
        "azure_vm",
        FakeCatalog(
            [
                vm_row(price=0.31, sku="Standard_D8pls_v6", meter="D8pls v6"),
                vm_row(price=0.39, sku="Standard_D8pls_v5", meter="D8pls v5"),
            ]
        ),  # type: ignore[arg-type]
    )
    requirement = ServiceRequirement(
        service="azure_vm",
        region="southeastasia",
        requirements={"vcpu": 8, "memory_gib": 32, "operating_system": "linux"},
        source_text="Azure VM，8核32GB，Linux",
    )

    preview = await plugin.preview(
        requirement,
        QuoteRequest(cloud_provider="azure", customer_request=requirement.source_text),
        "0",
    )

    assert preview.requires_confirmation is False
    assert preview.selected_model == "Standard_D8pls_v6"


@pytest.mark.asyncio
async def test_event_hubs_kafka_requirement_automatically_selects_standard() -> None:
    rows = [
        generic_azure_row(
            service="Event Hubs",
            product="Event Hubs",
            arm_sku=sku,
            sku=sku,
            meter=f"{sku} Throughput Unit",
            unit="1 Hour",
            price=price,
        )
        for sku, price in (("Standard", 0.09), ("Premium", 0.13), ("Dedicated", 0.13))
    ]
    catalog = FakeCatalog(rows)

    class EventHubsDiscovery:
        async def ensure_profile(self, **_: object) -> dict[str, object]:
            return {"service_name": "Event Hubs"}

    plugin = AzureGenericRetailPlugin(
        "azure_event_hubs",
        catalog,  # type: ignore[arg-type]
        EventHubsDiscovery(),  # type: ignore[arg-type]
    )
    requirement = ServiceRequirement(
        service="azure_event_hubs",
        calculator_service_name="Azure Event Hubs",
        region="southeastasia",
        source_text="Azure Event Hubs，用于实时数据流，启用 Kafka 兼容接口",
    )

    preview = await plugin.preview(
        requirement,
        QuoteRequest(cloud_provider="azure", customer_request=requirement.source_text),
        "0",
    )

    assert preview.requires_confirmation is False
    assert preview.selected_model == "Standard"
    assert "Kafka" in str(preview.selection_reason)


@pytest.mark.asyncio
async def test_azure_region_fallback_contains_full_searchable_catalog() -> None:
    registry = AzurePluginRegistry(FakeCatalog([]))  # type: ignore[arg-type]

    options = await registry.region_options()

    assert len(options) > 40
    assert ("southeastasia", "东南亚（新加坡）") in options
    assert ("eastus", "美国东部") in options


class MissingAzureRegionParser:
    async def parse(self, _: str, reporter=None) -> ParsedIntent:  # noqa: ANN001
        return ParsedIntent(
            customer_summary="一项待选区域的 Azure 配置",
            services=[
                ServiceRequirement(
                    service="azure_vm",
                    calculator_service_name="Azure Virtual Machines",
                    requirements={"requested_sku": "Standard_D4s_v5"},
                )
            ],
            ambiguities=["请确认该组件部署在哪个 Azure 区域。"],
        )


@pytest.mark.asyncio
async def test_missing_azure_region_uses_searchable_choices_not_text_input(
    tmp_path,
) -> None:
    service = AzureQuoteService(
        MissingAzureRegionParser(),  # type: ignore[arg-type]
        AzurePluginRegistry(FakeCatalog([])),  # type: ignore[arg-type]
        ConfirmationSessionStore(tmp_path / "regions.sqlite3", "azure"),
    )

    preview = await service.preview(
        QuoteRequest(cloud_provider="azure", customer_request="Azure VM quote")
    )

    assert len(preview.confirmation_items) == 1
    item = preview.confirmation_items[0]
    assert item.selection_mode == "catalog"
    assert len(item.options) > 40


class PartiallyMissingAzureRegionParser:
    async def parse(self, _: str, reporter=None) -> ParsedIntent:  # noqa: ANN001
        return ParsedIntent(
            customer_summary="一项已有区域、一项待选区域的 Azure 配置",
            services=[
                ServiceRequirement(
                    service="azure_vm",
                    calculator_service_name="Azure Virtual Machines",
                    region="southeastasia",
                    requirements={"requested_sku": "Standard_D4s_v5"},
                ),
                ServiceRequirement(
                    service="bandwidth",
                    calculator_service_name="Azure Bandwidth",
                    requirements={
                        "data_transfer_out_gib": 2048,
                        "source_region": "southeastasia",
                    },
                ),
            ],
            ambiguities=[],
        )


class PartiallyMissingAzureRegionPlugin:
    def __init__(self, service: str) -> None:
        self.service = service
        self.display_name = (
            "Azure Virtual Machines" if service == "azure_vm" else "Azure Bandwidth"
        )

    async def preview(self, requirement, request, component_id):  # noqa: ANN001
        if requirement.region:
            return PreviewSelection(
                component_id=component_id,
                service=self.service,
                display_name=self.display_name,
                region=str(requirement.region),
                quantity=requirement.quantity,
                requirements=requirement.requirements,
                source_text=requirement.source_text,
                selected_model="Standard_D4s_v5",
                selection_reason="Microsoft 官方目录精确匹配",
                status="ready",
            )
        return PreviewSelection(
            component_id=component_id,
            service=self.service,
            display_name=self.display_name,
            region="未指定区域",
            quantity=requirement.quantity,
            requirements=requirement.requirements,
            source_text=requirement.source_text,
            requires_confirmation=True,
            confirmation_reason="请确认该组件部署在哪个 Azure 区域。",
            status="customer_issue",
        )


class PartiallyMissingAzureRegionRegistry:
    def get(self, service: str) -> PartiallyMissingAzureRegionPlugin:
        return PartiallyMissingAzureRegionPlugin(service)

    async def region_options(self) -> list[tuple[str, str]]:
        return [
            ("southeastasia", "东南亚（新加坡）"),
            ("eastasia", "东亚（香港）"),
            ("eastus", "美国东部"),
            ("westus", "美国西部"),
            ("northeurope", "北欧"),
            ("westeurope", "西欧"),
            ("japaneast", "日本东部"),
        ]


@pytest.mark.asyncio
async def test_only_missing_component_region_still_uses_dropdown(tmp_path) -> None:
    store = ConfirmationSessionStore(tmp_path / "partial-regions.sqlite3", "azure")
    service = AzureQuoteService(
        PartiallyMissingAzureRegionParser(),  # type: ignore[arg-type]
        PartiallyMissingAzureRegionRegistry(),  # type: ignore[arg-type]
        store,
    )

    preview = await service.preview(
        QuoteRequest(cloud_provider="azure", customer_request="Azure partial region quote")
    )

    assert len(preview.confirmation_items) == 1
    item = preview.confirmation_items[0]
    assert item.component_id == "1"
    assert item.service == "bandwidth"
    assert item.selection_mode == "catalog"
    assert len(item.options) == 7
    assert item.options[0].value == "southeastasia"


@pytest.mark.asyncio
async def test_component_region_selection_advances_to_configuration_review(
    tmp_path,
) -> None:
    store = ConfirmationSessionStore(tmp_path / "region-submit.sqlite3", "azure")
    service = AzureQuoteService(
        PartiallyMissingAzureRegionParser(),  # type: ignore[arg-type]
        PartiallyMissingAzureRegionRegistry(),  # type: ignore[arg-type]
        store,
    )
    request_text = "Azure partial region quote"
    first = await service.preview(
        QuoteRequest(cloud_provider="azure", customer_request=request_text)
    )
    question = first.confirmation_items[0].question
    submitted = store.submit(first.confirmation_token or "", {question: "francecentral"})

    assert submitted is not None
    second = await service.preview(
        QuoteRequest(
            cloud_provider="azure",
            customer_request=request_text,
            draft_id=first.draft_id,
            confirmation_responses=submitted.answers,
        )
    )

    assert second.confirmation_items == []
    assert second.configuration_review_required is True
    refreshed = store.get(first.confirmation_token or "")
    assert refreshed is not None
    assert refreshed.status == "configuration_review"
    assert refreshed.configuration_items[1].region == "francecentral"
    restored = store.restore_draft(first.draft_id)
    assert restored is not None
    assert restored[1].services[1].requirements["source_region"] == "francecentral"


class ReadyAzureParser:
    async def parse(self, _: str, reporter=None) -> ParsedIntent:  # noqa: ANN001
        return ParsedIntent(
            customer_summary="一项 Azure 配置",
            services=[
                ServiceRequirement(
                    service="azure_vm",
                    calculator_service_name="Azure Virtual Machines",
                    region="southeastasia",
                    requirements={"requested_sku": "Standard_D4s_v5"},
                )
            ],
        )


class ReadyAzurePlugin:
    display_name = "Azure Virtual Machines"

    async def preview(self, requirement, request, component_id):  # noqa: ANN001
        return PreviewSelection(
            component_id=component_id,
            service="azure_vm",
            display_name=self.display_name,
            region=str(requirement.region),
            quantity=requirement.quantity,
            requirements=requirement.requirements,
            source_text=requirement.source_text,
            selected_model="Standard_D4s_v5",
            selection_reason="Microsoft 官方目录精确匹配",
            status="ready",
        )


class ReadyAzureRegistry:
    def get(self, _: str) -> ReadyAzurePlugin:
        return ReadyAzurePlugin()


def test_azure_service_rejects_aws_confirmation_storage(tmp_path) -> None:
    with pytest.raises(ValueError, match="Azure 专用确认存储"):
        AzureQuoteService(
            ReadyAzureParser(),  # type: ignore[arg-type]
            ReadyAzureRegistry(),  # type: ignore[arg-type]
            ConfirmationSessionStore(tmp_path / "aws.sqlite3", "aws"),
        )


@pytest.mark.asyncio
async def test_ready_azure_quote_still_requires_customer_configuration_link(
    tmp_path,
) -> None:
    store = ConfirmationSessionStore(tmp_path / "confirmations.sqlite3", "azure")
    service = AzureQuoteService(
        ReadyAzureParser(),  # type: ignore[arg-type]
        ReadyAzureRegistry(),  # type: ignore[arg-type]
        store,
    )

    preview = await service.preview(
        QuoteRequest(cloud_provider="azure", customer_request="Azure VM quote")
    )

    assert preview.confirmation_token
    assert preview.confirmation_token.startswith("azure_")
    assert preview.draft_id.startswith("az")
    assert preview.configuration_review_required is True
    session = store.get(preview.confirmation_token)
    assert session is not None
    assert session.status == "configuration_review"


@pytest.mark.asyncio
async def test_azure_quote_cannot_start_before_customer_final_approval(tmp_path) -> None:
    store = ConfirmationSessionStore(tmp_path / "approval.sqlite3", "azure")
    service = AzureQuoteService(
        ReadyAzureParser(),  # type: ignore[arg-type]
        ReadyAzureRegistry(),  # type: ignore[arg-type]
        store,
    )
    request = QuoteRequest(cloud_provider="azure", customer_request="Azure VM quote")
    preview = await service.preview(request)

    with pytest.raises(ManualConfirmationRequired) as blocked:
        await service.create_quote(request.model_copy(update={"draft_id": preview.draft_id}))

    assert blocked.value.code == "configuration_review_required"


@pytest.mark.asyncio
async def test_azure_customer_can_add_component_before_final_approval(tmp_path) -> None:
    store = ConfirmationSessionStore(tmp_path / "addition.sqlite3", "azure")
    service = AzureQuoteService(
        ReadyAzureParser(),  # type: ignore[arg-type]
        ReadyAzureRegistry(),  # type: ignore[arg-type]
        store,
    )
    request = QuoteRequest(cloud_provider="azure", customer_request="Azure VM quote")
    first = await service.preview(request)
    assert first.confirmation_token
    submitted = store.submit_configuration_feedback(
        first.confirmation_token,
        feedback="请新增 Azure VM Standard_D4s_v5",
    )
    assert submitted is not None

    revised = await service.preview(
        request.model_copy(
            update={
                "draft_id": first.draft_id,
                "confirmation_responses": submitted.answers,
            }
        )
    )

    assert len(revised.selections) == 2
    session = store.get(revised.confirmation_token or "")
    assert session is not None
    assert len(session.configuration_items) == 2
    assert session.status == "configuration_review"
