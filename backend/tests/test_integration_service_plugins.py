from __future__ import annotations

from typing import Any

import pytest

from app.core.errors import ManualConfirmationRequired
from app.domain.models import ServiceRequirement
from app.services.plugins import integration_services
from app.services.plugins.auxiliary_services import EbsPlugin, GlobalAcceleratorPlugin
from app.services.plugins.integration_services import (
    ApiGatewayPlugin,
    EventBridgeSchedulerPlugin,
    MskPlugin,
)
from app.services.plugins.search_network_services import NatGatewayPlugin, OpenSearchPlugin


def product(
    service_code: str,
    usage_type: str,
    operation: str,
    rate: float,
    unit: str,
    **attributes: str,
) -> dict[str, Any]:
    attrs = {
        "servicecode": service_code,
        "usagetype": usage_type,
        "operation": operation,
        "regionCode": "ap-southeast-1",
        **attributes,
    }
    return {
        "serviceCode": service_code,
        "product": {"sku": usage_type, "attributes": attrs},
        "terms": {
            "OnDemand": {
                "term": {
                    "priceDimensions": {
                        "dimension": {
                            "beginRange": "0",
                            "unit": unit,
                            "pricePerUnit": {"USD": str(rate)},
                        }
                    }
                }
            }
        },
    }


def add_reserved_term(
    item: dict[str, Any],
    *,
    years: int,
    payment_option: str,
    hourly: float = 0,
    upfront: float = 0,
) -> None:
    purchase = {
        "no_upfront": "No Upfront",
        "partial_upfront": "Partial Upfront",
        "all_upfront": "All Upfront",
    }[payment_option]
    dimensions: dict[str, object] = {}
    if hourly:
        dimensions["hourly"] = {
            "unit": "Hrs",
            "pricePerUnit": {"USD": str(hourly)},
        }
    if upfront:
        dimensions["upfront"] = {
            "unit": "Quantity",
            "pricePerUnit": {"USD": str(upfront)},
        }
    item["terms"].setdefault("Reserved", {})[
        f"{years}-{payment_option}"
    ] = {
        "termAttributes": {
            "LeaseContractLength": f"{years}yr",
            "PurchaseOption": purchase,
            "OfferingClass": "standard",
        },
        "priceDimensions": dimensions,
    }


class FakeCatalog:
    def __init__(self, products: dict[str, list[dict[str, Any]]]):
        self._products = products

    def location(self, region: str) -> str:
        assert region == "ap-southeast-1"
        return "Asia Pacific (Singapore)"

    def products(
        self, service_code: str, filters: dict[str, str], *, max_pages: int = 20
    ) -> list[dict[str, Any]]:
        del max_pages
        result = []
        for item in self._products.get(service_code, []):
            attrs = item["product"]["attributes"]
            if all(attrs.get(key) == value for key, value in filters.items()):
                result.append(item)
        return result


def test_ebs_prices_per_volume_capacity_times_volume_count() -> None:
    storage = product(
        "AmazonEC2",
        "APS1-EBS:VolumeUsage.gp3",
        "",
        0.08,
        "GB-Mo",
        productFamily="Storage",
        volumeApiName="gp3",
    )
    plugin = EbsPlugin(None, FakeCatalog({"AmazonEC2": [storage]}))  # type: ignore[arg-type]

    selected = plugin.select(
        ServiceRequirement(
            service="ebs",
            region="ap-southeast-1",
            quantity=2,
            requirements={
                "volume_type": "gp3",
                "storage_gib": 500,
                "total_storage_gib": 1000,
            },
        ),
        "ap-southeast-1",
    )

    assert selected.architecture == "2 块 × 500 GiB gp3 云盘"
    assert selected.specifications["storageGiB"] == 500
    assert selected.specifications["volumeCount"] == 2
    assert selected.specifications["totalStorageGiB"] == 1000
    assert selected.usage_lines[0].amount == 1000


@pytest.mark.parametrize(
    "transfer_field", ["data_transfer_out_gib", "data_transfer_gib"]
)
def test_global_accelerator_prices_customer_transfer_with_canonical_or_legacy_field(
    transfer_field: str,
) -> None:
    fixed = product(
        "AWSGlobalAccelerator",
        "Global-Accelerator-fixed-fee",
        "",
        0.025,
        "Hrs",
    )
    transfer = product(
        "AWSGlobalAccelerator",
        "AP-AP-OUT-Bytes-Internet",
        "Dominant",
        0.015,
        "GB",
        fromLocation="AP",
        toLocation="AP",
    )
    plugin = GlobalAcceleratorPlugin(
        None, FakeCatalog({"AWSGlobalAccelerator": [fixed, transfer]})
    )  # type: ignore[arg-type]

    selected = plugin.select(
        ServiceRequirement(
            service="global_accelerator",
            region="global",
            quantity=1,
            hours_per_month=730,
            requirements={"accelerators": 1, transfer_field: 1000},
        ),
        "ap-southeast-1",
    )

    assert selected.specifications["dataTransferOutGiB"] == 1000
    assert [(line.key, line.amount) for line in selected.usage_lines] == [
        ("gah", 730),
        ("gadt", 1000),
    ]


def test_global_accelerator_uses_source_path_instead_of_global_minimum() -> None:
    fixed = product(
        "AWSGlobalAccelerator",
        "Global-Accelerator-fixed-fee",
        "",
        0.025,
        "Hrs",
    )
    australia = product(
        "AWSGlobalAccelerator",
        "AU-AU-OUT-Bytes-Internet",
        "Dominant",
        0.007,
        "GB",
        fromLocation="AU",
        toLocation="AU",
    )
    asia_pacific = product(
        "AWSGlobalAccelerator",
        "AP-AP-OUT-Bytes-Internet",
        "Dominant",
        0.010,
        "GB",
        fromLocation="AP",
        toLocation="AP",
    )
    plugin = GlobalAcceleratorPlugin(
        None,
        FakeCatalog(
            {"AWSGlobalAccelerator": [fixed, australia, asia_pacific]}
        ),
    )  # type: ignore[arg-type]

    selected = plugin.select(
        ServiceRequirement(
            service="global_accelerator",
            region="global",
            quantity=1,
            hours_per_month=730,
            requirements={"accelerators": 1, "data_transfer_out_gib": 1000},
        ),
        "ap-southeast-1",
    )

    assert selected.usage_lines[1].usage_type == "AP-AP-OUT-Bytes-Internet"


def test_msk_quotes_broker_hours_and_per_broker_storage() -> None:
    broker = product(
        "AmazonMSK",
        "APS1-Kafka.m7g.large",
        "RunBroker",
        0.255,
        "hours",
        location="Asia Pacific (Singapore)",
        group="Broker",
        computeFamily="m7g.large",
        vcpu="2",
        memoryGib="8",
    )
    storage = product(
        "AmazonMSK",
        "APS1-Kafka.Storage.GP2",
        "RunVolume",
        0.12,
        "GB-Mo",
        location="Asia Pacific (Singapore)",
        group="Storage",
    )
    plugin = MskPlugin(None, FakeCatalog({"AmazonMSK": [broker, storage]}))  # type: ignore[arg-type]
    selected = plugin.select(
        ServiceRequirement(
            service="msk",
            region="ap-southeast-1",
            quantity=1,
            hours_per_month=730,
            requirements={
                "requested_model": "m7g.large",
                "broker_count": 3,
                "storage_gib_per_broker": 510,
            },
        ),
        "ap-southeast-1",
    )

    assert selected.model == "m7g.large"
    assert [(line.key, line.amount) for line in selected.usage_lines] == [
        ("mskbroker", 2190),
        ("mskstore", 1530),
    ]


def test_msk_configuration_candidates_include_all_regional_broker_shapes() -> None:
    small = product(
        "AmazonMSK", "APS1-Kafka.t3.small", "RunBroker", 0.1, "hours",
        group="Broker", computeFamily="t3.small", vcpu="2", memoryGib="2",
    )
    large = product(
        "AmazonMSK", "APS1-Kafka.m7g.large", "RunBroker", 0.3, "hours",
        group="Broker", computeFamily="m7g.large", vcpu="2", memoryGib="8",
    )
    xlarge = product(
        "AmazonMSK", "APS1-Kafka.m7g.xlarge", "RunBroker", 0.6, "hours",
        group="Broker", computeFamily="m7g.xlarge", vcpu="4", memoryGib="16",
    )
    plugin = MskPlugin(
        None, FakeCatalog({"AmazonMSK": [small, large, xlarge]})
    )  # type: ignore[arg-type]

    candidates = plugin.configuration_candidates(
        ServiceRequirement(service="msk", region="ap-southeast-1"),
        "ap-southeast-1",
    )

    assert [item.model for item in candidates] == [
        "t3.small", "m7g.large", "m7g.xlarge"
    ]
    assert [item.specifications for item in candidates] == [
        {"vCPU": 2.0, "memoryGiB": 2.0},
        {"vCPU": 2.0, "memoryGiB": 8.0},
        {"vCPU": 4.0, "memoryGiB": 16.0},
    ]


def test_msk_enriches_missing_price_list_shape_from_ec2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = product(
        "AmazonMSK",
        "APS1-Kafka.m7g.xlarge",
        "RunBroker",
        0.5,
        "hours",
        location="Asia Pacific (Singapore)",
        group="Broker",
        computeFamily="m7g.xlarge",
    )
    storage = product(
        "AmazonMSK",
        "APS1-Kafka.Storage.GP2",
        "RunVolume",
        0.12,
        "GB-Mo",
        location="Asia Pacific (Singapore)",
        group="Storage",
    )

    class FakeExecutor:
        def __init__(self, _clients: object):
            pass

        def execute(self, **_kwargs: object) -> dict[str, object]:
            return {
                "InstanceTypes": [
                    {
                        "InstanceType": "m7g.xlarge",
                        "VCpuInfo": {"DefaultVCpus": 4},
                        "MemoryInfo": {"SizeInMiB": 16384},
                    }
                ]
            }

    monkeypatch.setattr(integration_services, "ReadOnlyAwsQueryExecutor", FakeExecutor)
    plugin = MskPlugin(object(), FakeCatalog({"AmazonMSK": [broker, storage]}))  # type: ignore[arg-type]

    selected = plugin.select(
        ServiceRequirement(
            service="msk",
            region="ap-southeast-1",
            requirements={
                "broker_count": 3,
                "vcpu": 4,
                "memory_gib": 16,
                "storage_gib_per_broker": 1024,
            },
        ),
        "ap-southeast-1",
    )

    assert selected.model == "m7g.xlarge"
    assert selected.specifications["vCPU"] == 4
    assert selected.specifications["memoryGiB"] == 16


def test_msk_explains_shape_uplift_without_changing_broker_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = product(
        "AmazonMSK",
        "APS1-Kafka.m7g.2xlarge",
        "RunBroker",
        0.8,
        "hours",
        location="Asia Pacific (Singapore)",
        group="Broker",
        computeFamily="m7g.2xlarge",
    )
    storage = product(
        "AmazonMSK",
        "APS1-Kafka.Storage.GP2",
        "RunVolume",
        0.12,
        "GB-Mo",
        location="Asia Pacific (Singapore)",
        group="Storage",
    )

    class FakeExecutor:
        def __init__(self, _clients: object):
            pass

        def execute(self, **_kwargs: object) -> dict[str, object]:
            return {
                "InstanceTypes": [
                    {
                        "InstanceType": "m7g.2xlarge",
                        "VCpuInfo": {"DefaultVCpus": 8},
                        "MemoryInfo": {"SizeInMiB": 32768},
                    }
                ]
            }

    monkeypatch.setattr(integration_services, "ReadOnlyAwsQueryExecutor", FakeExecutor)
    plugin = MskPlugin(object(), FakeCatalog({"AmazonMSK": [broker, storage]}))  # type: ignore[arg-type]
    selected = plugin.select(
        ServiceRequirement(
            service="msk",
            region="ap-southeast-1",
            requirements={
                "broker_count": 2,
                "vcpu": 8,
                "memory_gib": 16,
                "storage_gib_per_broker": 500,
            },
        ),
        "ap-southeast-1",
    )

    assert selected.specifications["brokerCount"] == 2
    assert selected.specifications["memoryGiB"] == 32
    assert "Broker 数量仍为 2" in (selected.substitution_notice or "")
    assert "8 vCPU、16 GiB 内存" in (selected.substitution_notice or "")


@pytest.mark.parametrize(
    ("api_type", "operation"),
    [("http", "ApiGatewayHttpApi"), ("rest", "ApiGatewayRequest")],
)
def test_api_gateway_uses_official_request_dimension(api_type: str, operation: str) -> None:
    api = product(
        "AmazonApiGateway",
        f"APS1-{operation}",
        operation,
        0.00000125,
        "Requests",
        location="Asia Pacific (Singapore)",
    )
    plugin = ApiGatewayPlugin(  # type: ignore[arg-type]
        None, FakeCatalog({"AmazonApiGateway": [api]})
    )
    selected = plugin.select(
        ServiceRequirement(
            service="apigateway",
            region="ap-southeast-1",
            requirements={"api_type": api_type, "requests": 1_000_000},
        ),
        "ap-southeast-1",
    )

    assert selected.usage_lines[0].amount == 1_000_000
    assert selected.usage_lines[0].operation == operation


def test_api_gateway_websocket_quotes_messages_and_connection_minutes() -> None:
    message = product(
        "AmazonApiGateway",
        "APS1-ApiGatewayMessage",
        "ApiGatewayWebSocket",
        0.000001,
        "Messages",
        location="Asia Pacific (Singapore)",
    )
    minute = product(
        "AmazonApiGateway",
        "APS1-ApiGatewayMinute",
        "ApiGatewayWebSocket",
        0.00000025,
        "minutes",
        location="Asia Pacific (Singapore)",
    )
    plugin = ApiGatewayPlugin(  # type: ignore[arg-type]
        None, FakeCatalog({"AmazonApiGateway": [message, minute]})
    )

    selected = plugin.select(
        ServiceRequirement(
            service="apigateway",
            region="ap-southeast-1",
            requirements={
                "api_type": "websocket",
                "messages": 60_000_000,
                "connection_minutes": 15_000_000,
            },
        ),
        "ap-southeast-1",
    )

    assert selected.model == "WebSocket API"
    assert {line.operation for line in selected.usage_lines} == {"ApiGatewayWebSocket"}
    assert {line.amount for line in selected.usage_lines} == {60_000_000, 15_000_000}
    assert selected.specifications["messages"] == 60_000_000
    assert selected.specifications["connectionMinutes"] == 15_000_000


def test_scheduler_without_usage_returns_reference_rate_not_customer_question() -> None:
    scheduler = product(
        "AWSEvents",
        "APS1-ScheduledInvocation",
        "Invocation",
        0.0,
        "Invocations",
        location="Asia Pacific (Singapore)",
    )
    plugin = EventBridgeSchedulerPlugin(  # type: ignore[arg-type]
        None, FakeCatalog({"AWSEvents": [scheduler]})
    )
    preview = plugin.preview(
        ServiceRequirement(service="scheduler", region="ap-southeast-1"),
        "ap-southeast-1",
    )

    assert preview.requires_confirmation is False
    selected = plugin.select(
        ServiceRequirement(service="scheduler", region="ap-southeast-1"),
        "ap-southeast-1",
    )
    assert selected.usage_lines == []
    assert selected.reference_rates[0].unit_price == 0


def test_opensearch_quotes_nodes_and_storage_per_node() -> None:
    unrelated_charge = product(
        "AmazonES", "APS1-DirectQueryOCU", "DirectQueryOCU", 0.10, "OCU-Hrs",
        location="Asia Pacific (Singapore)",
        productFamily="Amazon OpenSearch Service Instance",
    )
    instance = product(
        "AmazonES", "APS1-ESInstance:m7g.xlarge", "ESDomain", 0.24, "Hrs",
        location="Asia Pacific (Singapore)",
        productFamily="Amazon OpenSearch Service Instance",
        instanceType="m7g.xlarge.search", vcpu="4", memoryGib="16",
    )
    storage = product(
        "AmazonES", "APS1-ES:GP3-Storage", "ESDomain", 0.12, "GB-Mo",
        location="Asia Pacific (Singapore)",
        productFamily="Amazon OpenSearch Service Volume", storageMedia="GP3",
    )
    plugin = OpenSearchPlugin(  # type: ignore[arg-type]
        None, FakeCatalog({"AmazonES": [unrelated_charge, instance, storage]})
    )
    selected = plugin.select(
        ServiceRequirement(
            service="opensearch", region="ap-southeast-1", hours_per_month=730,
            requirements={"vcpu": 4, "memory_gib": 16, "data_nodes": 3, "storage_gib_per_node": 500},
        ),
        "ap-southeast-1",
    )

    assert selected.model == "m7g.xlarge.search"
    assert [(line.key, line.amount) for line in selected.usage_lines] == [
        ("osnode", 2190), ("osstore", 1500)
    ]
    preview = plugin.preview(
        ServiceRequirement(
            service="opensearch", region="ap-southeast-1", hours_per_month=730,
            requirements={"vcpu": 4, "memory_gib": 16, "data_nodes": 3, "storage_gib_per_node": 500},
        ),
        "ap-southeast-1",
    )
    assert preview.selected_model == "m7g.xlarge.search"
    assert preview.requires_confirmation is False
    assert preview.confirmation_reason is None


def test_opensearch_uses_exact_one_and_three_year_reserved_terms() -> None:
    instance = product(
        "AmazonES", "APS1-ESInstance:m7g.xlarge", "ESDomain", 0.24, "Hrs",
        location="Asia Pacific (Singapore)",
        productFamily="Amazon OpenSearch Service Instance",
        instanceType="m7g.xlarge.search", vcpu="4", memoryGib="16",
    )
    add_reserved_term(
        instance,
        years=1,
        payment_option="all_upfront",
        upfront=1200,
    )
    add_reserved_term(
        instance,
        years=3,
        payment_option="all_upfront",
        upfront=2400,
    )
    storage = product(
        "AmazonES", "APS1-ES:GP3-Storage", "ESDomain", 0.12, "GB-Mo",
        location="Asia Pacific (Singapore)",
        productFamily="Amazon OpenSearch Service Volume", storageMedia="GP3",
    )
    plugin = OpenSearchPlugin(  # type: ignore[arg-type]
        None, FakeCatalog({"AmazonES": [instance, storage]})
    )
    base = {
        "service": "opensearch",
        "region": "ap-southeast-1",
        "hours_per_month": 730,
    }
    requirements = {
        "requested_model": "m7g.xlarge.search",
        "data_nodes": 3,
        "storage_gib_per_node": 500,
        "purchase_option": "reserved",
        "payment_option": "all_upfront",
    }

    one_year = plugin.select(
        ServiceRequirement(
            **base,
            requirements={**requirements, "reserved_term_years": 1},
        ),
        "ap-southeast-1",
    )
    three_year = plugin.select(
        ServiceRequirement(
            **base,
            requirements={**requirements, "reserved_term_years": 3},
        ),
        "ap-southeast-1",
    )

    assert one_year.monthly_commitment_cost == pytest.approx(300)
    assert one_year.upfront_commitment_cost == pytest.approx(3600)
    assert three_year.monthly_commitment_cost == pytest.approx(200)
    assert three_year.upfront_commitment_cost == pytest.approx(7200)
    # EBS is not covered by the instance reservation and remains a separate
    # official monthly usage line in both scenarios.
    assert [(line.key, line.amount) for line in one_year.usage_lines] == [
        ("osstore", 1500)
    ]
    assert [(line.key, line.amount) for line in three_year.usage_lines] == [
        ("osstore", 1500)
    ]


def test_opensearch_missing_reserved_offer_is_a_scenario_error_not_customer_question() -> None:
    instance = product(
        "AmazonES", "APS1-ESInstance:m7g.xlarge", "ESDomain", 0.24, "Hrs",
        location="Asia Pacific (Singapore)",
        productFamily="Amazon OpenSearch Service Instance",
        instanceType="m7g.xlarge.search", vcpu="4", memoryGib="16",
    )
    plugin = OpenSearchPlugin(  # type: ignore[arg-type]
        None, FakeCatalog({"AmazonES": [instance]})
    )

    with pytest.raises(ManualConfirmationRequired) as captured:
        plugin.select(
            ServiceRequirement(
                service="opensearch",
                region="ap-southeast-1",
                requirements={
                    "requested_model": "m7g.xlarge.search",
                    "data_nodes": 3,
                    "purchase_option": "reserved",
                    "reserved_term_years": 3,
                    "payment_option": "all_upfront",
                },
            ),
            "ap-southeast-1",
        )

    assert captured.value.code == "reserved_term_not_found"


def test_opensearch_accepts_ai_data_node_field_aliases() -> None:
    too_small = product(
        "AmazonES", "APS1-ESInstance:t2.micro", "ESDomain", 0.01, "Hrs",
        location="Asia Pacific (Singapore)",
        productFamily="Amazon OpenSearch Service Instance",
        instanceType="t2.micro.search", vcpu="1", memoryGib="1",
    )
    matching = product(
        "AmazonES", "APS1-ESInstance:m7g.xlarge", "ESDomain", 0.24, "Hrs",
        location="Asia Pacific (Singapore)",
        productFamily="Amazon OpenSearch Service Instance",
        instanceType="m7g.xlarge.search", vcpu="4", memoryGib="16",
    )
    storage = product(
        "AmazonES", "APS1-ES:GP3-Storage", "ESDomain", 0.12, "GB-Mo",
        location="Asia Pacific (Singapore)",
        productFamily="Amazon OpenSearch Service Volume", storageMedia="GP3",
    )
    plugin = OpenSearchPlugin(  # type: ignore[arg-type]
        None, FakeCatalog({"AmazonES": [too_small, matching, storage]})
    )

    selected = plugin.select(
        ServiceRequirement(
            service="opensearch",
            region="ap-southeast-1",
            hours_per_month=730,
            requirements={
                "data_nodes": 3,
                "data_node_vcpu": 4,
                "data_node_memory_gib": 16,
                "data_node_storage_gib": 500,
            },
        ),
        "ap-southeast-1",
    )

    assert selected.model == "m7g.xlarge.search"
    assert selected.specifications == {
        "dataNodes": 3,
        "vCPU": 4.0,
        "memoryGiB": 16.0,
        "storageGiBPerNode": 500.0,
    }
    assert [(line.key, line.amount) for line in selected.usage_lines] == [
        ("osnode", 2190), ("osstore", 1500)
    ]


def test_opensearch_nonstandard_shape_auto_selects_non_underprovisioned_option() -> None:
    lower = product(
        "AmazonES", "APS1-ESInstance:m7g.large", "ESDomain", 0.12, "Hrs",
        location="Asia Pacific (Singapore)",
        productFamily="Amazon OpenSearch Service Instance",
        instanceType="m7g.large.search", vcpu="2", memoryGib="8",
    )
    upper = product(
        "AmazonES", "APS1-ESInstance:m7g.xlarge", "ESDomain", 0.24, "Hrs",
        location="Asia Pacific (Singapore)",
        productFamily="Amazon OpenSearch Service Instance",
        instanceType="m7g.xlarge.search", vcpu="4", memoryGib="16",
    )
    plugin = OpenSearchPlugin(  # type: ignore[arg-type]
        None, FakeCatalog({"AmazonES": [lower, upper]})
    )

    preview = plugin.preview(
        ServiceRequirement(
            service="opensearch",
            region="ap-southeast-1",
            requirements={"vcpu": 3, "memory_gib": 12, "data_nodes": 3},
        ),
        "ap-southeast-1",
    )

    assert preview.requires_confirmation is False
    assert preview.selected_model == "m7g.xlarge.search"
    assert [option.model for option in preview.candidates] == ["m7g.xlarge.search"]


def test_opensearch_unavailable_model_auto_selects_cheapest_same_shape() -> None:
    lower = product(
        "AmazonES", "APS1-ESInstance:m7g.large", "ESDomain", 0.12, "Hrs",
        location="Asia Pacific (Singapore)",
        productFamily="Amazon OpenSearch Service Instance",
        instanceType="m7g.large.search", vcpu="2", memoryGib="8",
    )
    upper = product(
        "AmazonES", "APS1-ESInstance:m7g.xlarge", "ESDomain", 0.24, "Hrs",
        location="Asia Pacific (Singapore)",
        productFamily="Amazon OpenSearch Service Instance",
        instanceType="m7g.xlarge.search", vcpu="4", memoryGib="16",
    )
    plugin = OpenSearchPlugin(  # type: ignore[arg-type]
        None, FakeCatalog({"AmazonES": [lower, upper]})
    )

    selected = plugin.select(
        ServiceRequirement(
            service="opensearch",
            region="ap-southeast-1",
            requirements={
                "requested_model": "missing.xlarge.search",
                "vcpu": 4,
                "memory_gib": 8,
                "data_nodes": 1,
            },
        ),
        "ap-southeast-1",
    )

    assert selected.model == "m7g.xlarge.search"
    assert "自动替换为最低价" in (selected.substitution_notice or "")


def test_nat_gateway_without_traffic_quotes_hours_and_returns_unit_rate() -> None:
    hourly = product(
        "AmazonEC2", "APS1-NatGateway-Hours", "NatGateway", 0.059, "Hrs",
        location="Asia Pacific (Singapore)", productFamily="NAT Gateway",
    )
    processed = product(
        "AmazonEC2", "APS1-NatGateway-Bytes", "NatGateway", 0.059, "GB",
        location="Asia Pacific (Singapore)", productFamily="NAT Gateway",
    )
    plugin = NatGatewayPlugin(None, FakeCatalog({"AmazonEC2": [hourly, processed]}))  # type: ignore[arg-type]
    selected = plugin.select(
        ServiceRequirement(
            service="nat_gateway", region="ap-southeast-1", quantity=2, hours_per_month=730,
        ),
        "ap-southeast-1",
    )

    assert [(line.key, line.amount) for line in selected.usage_lines] == [("nathour", 1460)]
    assert selected.reference_rates[0].unit_price == 0.059
