from __future__ import annotations

import pytest

from app.domain.models import ServiceRequirement
from app.services.plugins.common import AlbPlugin, S3Plugin


def _product(
    service_code: str,
    sku: str,
    usage_type: str,
    *,
    operation: str = "",
    region: str = "ap-northeast-1",
) -> dict[str, object]:
    return {
        "serviceCode": service_code,
        "product": {
            "sku": sku,
            "attributes": {
                "regionCode": region,
                "usagetype": usage_type,
                "operation": operation,
                "groupDescription": (
                    "PUT, COPY, POST, LIST requests"
                    if "Tier1" in usage_type
                    else "GET and all other requests"
                ),
            },
        },
        "terms": {
            "OnDemand": {
                "term": {
                    "priceDimensions": {
                        "dimension": {
                            "beginRange": "0",
                            "unit": "GB",
                            "pricePerUnit": {"USD": "0.01"},
                        }
                    }
                }
            }
        },
    }


class AlbCatalog:
    @staticmethod
    def products(
        service_code: str,
        filters: dict[str, str],
        *,
        max_pages: int = 3,
        refresh: bool = False,
    ) -> list[dict[str, object]]:
        del max_pages, refresh
        assert service_code == "AWSELB"
        operation = filters["operation"]
        products = [
            _product(
                "AWSELB",
                "alb-hours",
                "APN1-LoadBalancerUsage",
                operation=operation,
            ),
            _product(
                "AWSELB",
                "alb-lcu",
                "APN1-LCUUsage",
                operation=operation,
            ),
        ]
        return [
            product
            for product in products
            if all(
                product["product"]["attributes"].get(key) == value  # type: ignore[index,union-attr]
                for key, value in filters.items()
            )
        ]


def test_alb_direct_lcu_field_becomes_lcu_hours_from_the_same_template() -> None:
    selected = AlbPlugin(None, AlbCatalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="elb",
            region="ap-northeast-1",
            quantity=2,
            hours_per_month=730,
            requirements={"load_balancer_type": "application", "lcu_count": 5},
        ),
        "ap-northeast-1",
    )

    assert [(line.key, line.amount) for line in selected.usage_lines] == [
        ("albh", 1460),
        ("alblcu", 7300),
    ]
    assert selected.usage_lines[1].source_fields == [
        "quantity",
        "hours_per_month",
        "lcu_count",
    ]
    assert selected.reference_rates == []


def test_alb_bottom_up_metrics_use_max_dimension_not_sum() -> None:
    selected = AlbPlugin(None, AlbCatalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="elb",
            region="ap-northeast-1",
            quantity=1,
            hours_per_month=730,
            requirements={
                "load_balancer_type": "application",
                "new_connections_per_second": 50,
                "active_connections_per_minute": 3000,
                "processed_bytes_ec2_ip_gib_per_hour": 1,
                "rule_evaluations_per_second": 1000,
            },
        ),
        "ap-northeast-1",
    )

    # Dimensions are 2, 1, 1 and 1 LCU. AWS bills the maximum: 2 LCU-hours.
    assert selected.usage_lines[1].amount == 1460


def test_alb_component_total_monthly_traffic_is_not_multiplied_by_alb_count() -> None:
    selected = AlbPlugin(None, AlbCatalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="elb",
            region="ap-northeast-1",
            quantity=2,
            hours_per_month=730,
            requirements={
                "load_balancer_type": "application",
                "processed_bytes_gib": 3072,
            },
        ),
        "ap-northeast-1",
    )

    assert selected.usage_lines[1].amount == 3072
    assert selected.usage_lines[1].source_fields == ["processed_bytes_gib"]


def test_alb_explicit_per_load_balancer_monthly_traffic_is_multiplied_by_count() -> None:
    selected = AlbPlugin(None, AlbCatalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="elb",
            region="ap-northeast-1",
            quantity=2,
            hours_per_month=730,
            requirements={
                "load_balancer_type": "application",
                "processed_bytes_gib_per_load_balancer": 3072,
            },
        ),
        "ap-northeast-1",
    )

    assert selected.usage_lines[1].amount == 6144
    assert selected.usage_lines[1].source_fields == [
        "quantity",
        "processed_bytes_gib_per_load_balancer",
    ]


class S3TransferCatalog:
    @staticmethod
    def location(region: str) -> str:
        assert region == "ap-northeast-1"
        return "Asia Pacific (Tokyo)"

    @staticmethod
    def products(
        service_code: str,
        filters: dict[str, str],
        *,
        max_pages: int = 3,
        refresh: bool = False,
    ) -> list[dict[str, object]]:
        del max_pages, refresh
        if service_code == "AWSDataTransfer":
            assert filters == {
                "fromLocation": "Asia Pacific (Tokyo)",
                "toLocation": "External",
                "transferType": "AWS Outbound",
            }
            return [
                _product(
                    "AWSDataTransfer",
                    "internet-out",
                    "APN1-DataTransfer-Out-Bytes",
                )
            ]
        assert service_code == "AmazonS3"
        if filters.get("productFamily") == "Storage":
            return [_product("AmazonS3", "storage", "APN1-TimedStorage-ByteHrs")]
        if filters.get("group") == "S3-API-Tier1":
            return [_product("AmazonS3", "put", "APN1-Requests-Tier1")]
        if filters.get("group") == "S3-API-Tier2":
            return [_product("AmazonS3", "get", "APN1-Requests-Tier2")]
        return []


def test_s3_standard_template_prices_storage_requests_and_internet_transfer() -> None:
    selected = S3Plugin(None, S3TransferCatalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="s3",
            region="ap-northeast-1",
            requirements={
                "storage_class": "standard",
                "storage_gib": 12_288,
                "put_copy_post_list_requests": 5_000_000,
                "get_select_requests": 80_000_000,
                "data_retrieval_gib": 200,
                "data_transfer_out_gib": 2048,
            },
        ),
        "ap-northeast-1",
    )

    assert [(line.key, line.amount) for line in selected.usage_lines] == [
        ("s3", 12_288),
        ("s3put", 5_000_000),
        ("s3get", 80_000_000),
        ("s3out", 2048),
    ]
    assert "data_retrieval_gib" in selected.applied_requirement_fields
    assert "dataTransferOutGiB" in selected.specifications


def test_s3_zero_requests_are_consumed_without_a_charge_or_missing_input_error():
    selected = S3Plugin(None, S3TransferCatalog()).select(
        ServiceRequirement(
            service="s3",
            region="ap-northeast-1",
            requirements={
                "storage_gib": 100,
                "put_copy_post_list_requests": 0,
                "get_select_requests": 0,
                "data_transfer_out_gib": 0,
            },
        ),
        "ap-northeast-1",
    )
    assert [(line.key, line.amount) for line in selected.usage_lines] == [("s3", 100)]
    assert {"put_copy_post_list_requests", "get_select_requests", "data_transfer_out_gib"} <= set(
        selected.applied_requirement_fields
    )
    assert selected.usage_lines[0].calculation.rule_id == "s3.storage"


@pytest.mark.parametrize(
    "field", ["put_copy_post_list_requests", "get_select_requests", "data_transfer_out_gib"]
)
def test_s3_runtime_scope_is_recorded_and_scaled_once(field):
    selected = S3Plugin(None, S3TransferCatalog()).select(
        ServiceRequirement(
            service="s3",
            region="ap-northeast-1",
            quantity=3,
            requirements={"storage_gib": 100, field: 10},
            field_scopes={field: "per_resource"},
        ),
        "ap-northeast-1",
    )
    usage = selected.usage_lines[1]
    assert usage.amount == 30
    assert usage.calculation.inputs[field] == "10"
    assert usage.calculation.inputs["quantity"] == "3"
    assert usage.calculation.scopes[field] == "per_resource"


def test_alb_runtime_attaches_executable_rule_not_just_template_description():
    selected = AlbPlugin(None, AlbCatalog()).select(
        ServiceRequirement(service="elb", quantity=3, requirements={"lcu_count": 8}),
        "ap-northeast-1",
    )
    for line in selected.usage_lines:
        assert line.calculation
        assert float(line.calculation.amount) == line.amount


def test_alb_zero_lcu_is_explicit_zero_not_missing_business_usage():
    selected = AlbPlugin(None, AlbCatalog()).select(
        ServiceRequirement(service="elb", quantity=3, requirements={"lcu_count": 0}),
        "ap-northeast-1",
    )
    assert [(line.key, line.amount) for line in selected.usage_lines] == [("albh", 2190)]
    assert selected.reference_rates == []
    assert "lcu_count" in selected.applied_requirement_fields
