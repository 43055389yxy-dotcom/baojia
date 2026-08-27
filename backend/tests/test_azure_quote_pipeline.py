from __future__ import annotations

import pytest

from app.core.errors import ManualConfirmationRequired, QuoteError
from app.domain.models import (
    CandidateOption,
    ParsedIntent,
    PreviewSelection,
    QuoteRequest,
    ServiceRequirement,
)
from app.integrations.azure_auto_service_discovery import AzureAutoServiceDiscovery
from app.integrations.azure_cache import PersistentAzureCache
from app.integrations.azure_catalog import AzureOfficialCatalog
from app.integrations.azure_intent import (
    AzureIntentParser,
    azure_numbered_component_identity,
    azure_pricing_relevant_source,
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


def test_azure_first_pass_removes_sales_noise_but_keeps_every_pricing_fact() -> None:
    cleaned = azure_pricing_relevant_source(
        """客户名称：示例公司
联系人：张经理
项目背景：集团业务上云
区域：新加坡
1、Azure Virtual Machines Standard_D8s_v5
需求说明：数量 3 台，单台 8 vCPU、32 GiB 内存、200 GiB 系统盘，Ubuntu 24.04
用途：内部测试平台
备注：请在月底前交付"""
    )

    assert "客户名称" not in cleaned
    assert "联系人" not in cleaned
    assert "项目背景" not in cleaned
    assert "内部测试平台" not in cleaned
    assert "月底前交付" not in cleaned
    assert "区域：新加坡" in cleaned
    assert "Standard_D8s_v5" in cleaned
    assert "数量 3 台" in cleaned
    assert "8 vCPU、32 GiB" in cleaned
    assert "200 GiB 系统盘" in cleaned
    assert "Ubuntu 24.04" in cleaned


@pytest.mark.asyncio
async def test_azure_component_ai_only_receives_cleaned_pricing_context() -> None:
    gateway = ComponentGateway()
    parser = AzureIntentParser(gateway)  # type: ignore[arg-type]

    intent = await parser.parse(
        """联系人：张经理
项目背景：集团业务上云
区域：新加坡
1、Azure VM Standard_D4s_v5，数量 2 台，4 核 16 GiB"""
    )

    assert len(gateway.contents) == 1
    assert "联系人：张经理" not in gateway.contents[0]
    assert "项目背景：集团业务上云" not in gateway.contents[0]
    assert "Standard_D4s_v5" in gateway.contents[0]
    assert intent.services[0].original_source_text is not None
    assert "联系人：张经理" in (intent.services[0].original_source_text or "")


@pytest.mark.parametrize(
    ("text", "expected", "declared"),
    [
        ("区域：新加坡\n1、Azure VM", "southeastasia", True),
        ("新加坡地区\n1、Azure VM", "southeastasia", True),
        ("southeastasia\n1、Azure VM", "southeastasia", True),
        ("Region: East US\n1、Azure VM", "eastus", True),
        ("区域：俄罗斯\n1、Azure VM", None, True),
        ("1、Azure VM，新加坡\n2、Azure SQL", None, False),
        ("1、Azure VM\n2、Azure SQL", None, False),
    ],
)
def test_azure_sales_region_is_resolved_only_from_global_context(
    text: str,
    expected: str | None,
    declared: bool,
) -> None:
    options = [
        ("southeastasia", "东南亚（新加坡）"),
        ("eastus", "美国东部"),
    ]

    assert AzureQuoteService._explicit_sales_region(text, options) == (expected, declared)


def test_azure_sales_region_is_the_only_shared_component_variable() -> None:
    intent = ParsedIntent(
        customer_summary="Azure global region",
        services=[
            ServiceRequirement(service="azure_vm"),
            ServiceRequirement(service="azure_postgresql", region="eastus"),
            ServiceRequirement(service="bandwidth"),
            ServiceRequirement(service="front_door", region="global"),
        ],
        ambiguities=["这些区域型服务部署在哪个 Azure 区域？"],
    )

    AzureQuoteService._apply_sales_region(intent, "southeastasia")

    assert [item.region for item in intent.services] == [
        "southeastasia",
        "eastus",
        "southeastasia",
        "global",
    ]
    assert intent.services[0].field_sources["region"] == "sales_confirmation"
    assert intent.services[2].requirements["source_region"] == "southeastasia"
    assert intent.ambiguities == []


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
def test_azure_numbered_blocks_are_classified_before_ai(text: str, expected: str) -> None:
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

    intent = await parser.parse("1、Azure VM Standard_D4s_v5\n2、Azure 托管磁盘 P20")

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

    async def service_regions(self, service_name: str, *, force_refresh: bool = False) -> list[str]:
        return ["southeastasia"]


class RegionGateGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_json(self, **_: object) -> dict[str, object]:
        self.calls += 1
        raise AssertionError("Azure component AI must not start before sales confirms region")


@pytest.mark.asyncio
async def test_missing_azure_sales_region_blocks_before_component_ai() -> None:
    gateway = RegionGateGateway()
    service = AzureQuoteService(
        AzureIntentParser(gateway),  # type: ignore[arg-type]
        AzurePluginRegistry(FakeCatalog([])),  # type: ignore[arg-type]
    )

    with pytest.raises(ManualConfirmationRequired) as captured:
        await service.preview(
            QuoteRequest(
                cloud_provider="azure",
                customer_request="1、Azure VM，4核16G",
            )
        )

    assert captured.value.code == "azure_sales_region_confirmation_required"
    assert gateway.calls == 0


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

    async def service_regions(self, service_name: str, *, force_refresh: bool = False) -> list[str]:
        return ["southeastasia"] if service_name == "Functions" else []


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

    intent = await parser.parse("区域：新加坡\n1、Azure Functions\n每月执行用量：1000000 GB-s")

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
                source_text=("客户最新修改：4核128G\n客户原始配置：Standard_D4s_v5，4核16GB"),
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
            confirmation_responses={f"{CONFIGURATION_COMPONENT_FEEDBACK_PREFIX}0": "改成4核128G"},
        )
    )

    assert preview.configuration_review_required is False
    assert len(preview.confirmation_items) == 1
    item = preview.confirmation_items[0]
    assert item.component_id == "0"
    assert item.selection_mode == "catalog"
    assert "4 vCPU、128 GiB 内存" in item.question
    assert "Azure 官方实例规格（SKU）" in item.question
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
async def test_azure_edit_options_are_generated_from_official_catalog_rows() -> None:
    catalog = FakeCatalog(
        [
            vm_row(price=0.2, sku="Standard_D4s_v5", meter="D4s v5"),
            vm_row(price=0.4, sku="Standard_E8s_v5", meter="E8s v5"),
        ]
    )
    plugin = AzureRetailPlugin("azure_vm", catalog)  # type: ignore[arg-type]

    result = await plugin.configuration_field_options(
        ServiceRequirement(
            service="azure_vm",
            region="southeastasia",
            requirements={"operating_system": "linux"},
        )
    )

    assert result["options"]["region"] == ["southeastasia"]
    assert result["options"]["requested_sku"] == [
        "Standard_D4s_v5",
        "Standard_E8s_v5",
    ]
    assert result["shapes"] == [
        {"model": "Standard_D4s_v5", "vcpu": 4.0, "memory_gib": 16.0},
        {"model": "Standard_E8s_v5", "vcpu": 8.0, "memory_gib": 64.0},
    ]


def test_azure_savings_plan_uses_nested_official_cached_rate() -> None:
    row = vm_row(price=0.2, sku="Standard_D4s_v5", meter="D4s v5")
    row["savingsPlan"] = [
        {"term": "1 Year", "retailPrice": 0.15, "unitPrice": 0.15},
        {"term": "3 Years", "retailPrice": 0.1, "unitPrice": 0.1},
    ]
    plugin = AzureRetailPlugin("azure_vm", FakeCatalog([]))  # type: ignore[arg-type]

    eligible = plugin._eligible_rows(
        ServiceRequirement(
            service="azure_vm",
            region="southeastasia",
            requirements={"requested_sku": "Standard_D4s_v5"},
        ),
        QuoteRequest(
            cloud_provider="azure",
            customer_request="Azure VM savings plan",
            azure_pricing_mode="savings_plan",
            azure_term_years=3,
        ),
        [row],
    )

    assert len(eligible) == 1
    assert eligible[0]["unitPrice"] == 0.1
    assert eligible[0]["_azurePriceType"] == "SavingsPlan"


@pytest.mark.asyncio
async def test_azure_region_fallback_contains_full_searchable_catalog() -> None:
    registry = AzurePluginRegistry(FakeCatalog([]))  # type: ignore[arg-type]

    options = await registry.region_options()

    assert len(options) > 40
    assert ("southeastasia", "东南亚（新加坡） / Southeast Asia") in options
    assert ("eastus", "美国东部 / East US") in options


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
        self.display_name = "Azure Virtual Machines" if service == "azure_vm" else "Azure Bandwidth"

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
async def test_one_confirmed_region_is_reused_by_all_unresolved_components(tmp_path) -> None:
    store = ConfirmationSessionStore(tmp_path / "partial-regions.sqlite3", "azure")
    service = AzureQuoteService(
        PartiallyMissingAzureRegionParser(),  # type: ignore[arg-type]
        PartiallyMissingAzureRegionRegistry(),  # type: ignore[arg-type]
        store,
    )

    preview = await service.preview(
        QuoteRequest(cloud_provider="azure", customer_request="Azure partial region quote")
    )

    assert preview.confirmation_items == []
    assert preview.configuration_review_required is True
    restored = store.restore_draft(preview.draft_id)
    assert restored is not None
    assert restored[1].services[1].region == "southeastasia"
    assert restored[1].services[1].requirements["source_region"] == "southeastasia"


class FullyMissingAzureRegionParser:
    async def parse(self, _: str, reporter=None) -> ParsedIntent:  # noqa: ANN001
        return ParsedIntent(
            customer_summary="两项都需要确认地区的 Azure 配置",
            services=[
                ServiceRequirement(
                    service="azure_vm",
                    calculator_service_name="Azure Virtual Machines",
                    requirements={"requested_sku": "Standard_D4s_v5"},
                ),
                ServiceRequirement(
                    service="bandwidth",
                    calculator_service_name="Azure Bandwidth",
                    requirements={"data_transfer_out_gib": 2048},
                ),
            ],
        )


@pytest.mark.asyncio
async def test_component_region_selection_advances_to_configuration_review(
    tmp_path,
) -> None:
    store = ConfirmationSessionStore(tmp_path / "region-submit.sqlite3", "azure")
    service = AzureQuoteService(
        FullyMissingAzureRegionParser(),  # type: ignore[arg-type]
        PartiallyMissingAzureRegionRegistry(),  # type: ignore[arg-type]
        store,
    )
    request_text = "Azure missing regions quote"
    first = await service.preview(
        QuoteRequest(cloud_provider="azure", customer_request=request_text)
    )
    assert len(first.confirmation_items) == 2
    assert "为确保报价准确" in (first.confirmation_text or "")
    submitted = store.submit(
        first.confirmation_token or "",
        {
            item.answer_key or "": "southeastasia"
            for item in first.confirmation_items
        },
    )

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
    assert [item.region for item in refreshed.configuration_items] == [
        "southeastasia",
        "southeastasia",
    ]
    restored = store.restore_draft(first.draft_id)
    assert restored is not None
    assert restored[1].services[1].requirements["source_region"] == "southeastasia"


class UnsupportedRegionCatalog(FakeCatalog):
    async def retail_items(self, **kwargs: object) -> list[dict[str, object]]:
        if kwargs.get("region") == "eastus":
            return [vm_row(price=0.2, sku="Standard_D4s_v5", meter="D4s v5")]
        return []

    async def service_regions(self, service_name: str, *, force_refresh: bool = False) -> list[str]:
        assert service_name == "Virtual Machines"
        return ["eastus", "westus2"]


class ServiceFirstRegionGateCatalog(FakeCatalog):
    async def retail_items(self, **_: object) -> list[dict[str, object]]:
        raise AssertionError("区域不支持时，不应先进入型号或价格查询")

    async def service_regions(
        self,
        service_name: str,
        *,
        force_refresh: bool = False,
    ) -> list[str]:
        assert service_name == "Virtual Machines"
        return ["eastus", "westus2"]


@pytest.mark.asyncio
async def test_component_checks_service_region_before_sku_and_price_lookup() -> None:
    plugin = AzureRetailPlugin(
        "azure_vm",
        ServiceFirstRegionGateCatalog([]),  # type: ignore[arg-type]
    )
    requirement = ServiceRequirement(
        service="azure_vm",
        calculator_service_name="Azure Virtual Machines",
        region="centralindia",
        requirements={"requested_sku": "Standard_D8s_v5"},
    )

    preview = await plugin.preview(
        requirement,
        QuoteRequest(cloud_provider="azure", customer_request="Azure VM"),
        "azure-01",
    )

    assert preview.status == "customer_issue"
    assert preview.issue_code == "azure_service_region_not_supported"
    assert [candidate.model for candidate in preview.candidates] == ["eastus", "westus2"]
    assert [candidate.family for candidate in preview.candidates] == [
        "美国东部 / East US",
        "美国西部 2 / West US 2",
    ]


class UnsupportedRegionParser:
    async def parse(self, _: str, reporter=None) -> ParsedIntent:  # noqa: ANN001
        return ParsedIntent(
            customer_summary="一项区域不兼容的 Azure 配置",
            services=[
                ServiceRequirement(
                    service="azure_vm",
                    calculator_service_name="Azure Virtual Machines",
                    region="invalidazurelocation",
                    requirements={"requested_sku": "Standard_D4s_v5"},
                )
            ],
        )


@pytest.mark.asyncio
async def test_unsupported_region_only_offers_service_supported_regions_and_advances(
    tmp_path,
) -> None:
    store = ConfirmationSessionStore(tmp_path / "supported-regions.sqlite3", "azure")
    service = AzureQuoteService(
        UnsupportedRegionParser(),  # type: ignore[arg-type]
        AzurePluginRegistry(UnsupportedRegionCatalog([])),  # type: ignore[arg-type]
        store,
    )
    request_text = "Azure VM in invalidazurelocation"

    first = await service.preview(
        QuoteRequest(cloud_provider="azure", customer_request=request_text)
    )

    assert first.sales_validation_required is False
    assert len(first.confirmation_items) == 1
    item = first.confirmation_items[0]
    assert [option.value for option in item.options] == ["eastus", "westus2"]

    submitted = store.submit(first.confirmation_token or "", {item.question: "eastus"})
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
    restored = store.restore_draft(first.draft_id)
    assert restored is not None
    assert restored[1].services[0].region == "eastus"


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


class TwoUncertainVmParser:
    async def parse(self, _: str, reporter=None) -> ParsedIntent:  # noqa: ANN001
        return ParsedIntent(
            customer_summary="两台需要选择型号的 Azure 云服务器",
            services=[
                ServiceRequirement(
                    service="azure_vm",
                    calculator_service_name="Azure Virtual Machines",
                    region="southeastasia",
                    requirements={"vcpu": 8, "memory_gib": 32},
                ),
                ServiceRequirement(
                    service="azure_vm",
                    calculator_service_name="Azure Virtual Machines",
                    region="southeastasia",
                    requirements={"vcpu": 8, "memory_gib": 32},
                ),
            ],
        )


class UncertainVmPlugin:
    display_name = "Azure Virtual Machines"

    async def preview(self, requirement, request, component_id):  # noqa: ANN001
        return PreviewSelection(
            component_id=component_id,
            service="azure_vm",
            display_name=self.display_name,
            region=str(requirement.region),
            requirements=requirement.requirements,
            requires_confirmation=True,
            confirmation_reason=(
                "Azure 虚拟机包含多个 Microsoft 官方 SKU，请从下方选择匹配项。"
            ),
            candidates=[
                CandidateOption(
                    model="Standard_D8pls_v6",
                    family="Dpls v6",
                    specifications={"vCPU": 8, "memoryGiB": 32},
                    rationale="符合客户需要",
                ),
                CandidateOption(
                    model="Standard_D8pls_v5",
                    family="Dpls v5",
                    specifications={"vCPU": 8, "memoryGiB": 32},
                    rationale="符合客户需要",
                ),
            ],
            status="customer_issue",
        )


class UncertainVmRegistry:
    def get(self, _: str) -> UncertainVmPlugin:
        return UncertainVmPlugin()


@pytest.mark.asyncio
async def test_all_component_questions_are_batched_with_plain_unique_answer_keys(
    tmp_path,
) -> None:
    store = ConfirmationSessionStore(tmp_path / "batched-questions.sqlite3", "azure")
    service = AzureQuoteService(
        TwoUncertainVmParser(),  # type: ignore[arg-type]
        UncertainVmRegistry(),  # type: ignore[arg-type]
        store,
    )

    preview = await service.preview(
        QuoteRequest(cloud_provider="azure", customer_request="two Azure VMs")
    )

    assert len(preview.confirmation_items) == 2
    assert len({item.answer_key for item in preview.confirmation_items}) == 2
    assert len({item.question for item in preview.confirmation_items}) == 1
    assert "为确保报价准确" in (preview.confirmation_text or "")
    assert all("Azure 官方实例规格（SKU）" in item.question for item in preview.confirmation_items)

    submitted = store.submit(
        preview.confirmation_token or "",
        {
            item.answer_key or "": f"选择 Standard_D8pls_v{6 - index}"
            for index, item in enumerate(preview.confirmation_items)
        },
    )

    assert submitted is not None
    assert len(submitted.answers) == 2


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


class FreeTextAmbiguityAzureParser(ReadyAzureParser):
    async def parse(self, text: str, reporter=None) -> ParsedIntent:  # noqa: ANN001
        parsed = await super().parse(text, reporter)
        parsed.ambiguities = ["请手动描述一个无法生成官方选项的问题。"]
        return parsed


@pytest.mark.asyncio
async def test_azure_never_publishes_free_text_customer_question(tmp_path) -> None:
    store = ConfirmationSessionStore(tmp_path / "no-free-text.sqlite3", "azure")
    service = AzureQuoteService(
        FreeTextAmbiguityAzureParser(),  # type: ignore[arg-type]
        ReadyAzureRegistry(),  # type: ignore[arg-type]
        store,
    )

    preview = await service.preview(
        QuoteRequest(cloud_provider="azure", customer_request="Azure VM quote")
    )

    assert preview.sales_validation_required is True
    assert preview.confirmation_token is None
    assert preview.confirmation_items == []


def test_azure_service_rejects_aws_confirmation_storage(tmp_path) -> None:
    with pytest.raises(ValueError, match="Azure 专用确认存储"):
        AzureQuoteService(
            ReadyAzureParser(),  # type: ignore[arg-type]
            ReadyAzureRegistry(),  # type: ignore[arg-type]
            ConfirmationSessionStore(tmp_path / "aws.sqlite3", "aws"),
        )


@pytest.mark.asyncio
async def test_azure_service_rejects_aws_quote_request(tmp_path) -> None:
    service = AzureQuoteService(
        ReadyAzureParser(),  # type: ignore[arg-type]
        ReadyAzureRegistry(),  # type: ignore[arg-type]
        ConfirmationSessionStore(tmp_path / "azure.sqlite3", "azure"),
    )

    with pytest.raises(QuoteError) as blocked:
        await service.preview(QuoteRequest(cloud_provider="aws", customer_request="AWS EC2 quote"))

    assert blocked.value.code == "cloud_provider_boundary_violation"


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


@pytest.mark.asyncio
async def test_azure_structured_dropdown_edit_updates_only_target_component(
    tmp_path,
) -> None:
    store = ConfirmationSessionStore(tmp_path / "structured-azure.sqlite3", "azure")
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
        component_updates={
            "0": {
                "region": "eastus",
                "quantity": 3,
                "requirements": {"requested_sku": "Standard_E8s_v5"},
            }
        },
    )
    assert submitted is not None
    reprocess = store.begin_configuration_reprocessing(first.confirmation_token)
    assert reprocess is not None

    revised = await service.preview(reprocess)

    assert revised.configuration_review_required is True
    restored = store.restore_draft(first.draft_id)
    assert restored is not None
    component = restored[1].services[0]
    assert component.region == "eastus"
    assert component.quantity == 3
    assert component.requirements["requested_sku"] == "Standard_E8s_v5"
