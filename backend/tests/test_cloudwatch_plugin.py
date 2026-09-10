from __future__ import annotations

from typing import Any

from app.domain.models import ServiceRequirement
from app.services.plugins.minimum_services import CloudWatchPlugin


def _product(
    sku: str,
    *,
    group: str,
    usage_type: str,
    operation: str = "",
    service_code: str = "AmazonCloudWatch",
    **attributes: str,
) -> dict[str, Any]:
    return {
        "serviceCode": service_code,
        "product": {
            "sku": sku,
            "attributes": {
                "regionCode": "ap-south-1",
                "group": group,
                "usagetype": usage_type,
                "operation": operation,
                **attributes,
            },
        },
        "terms": {
            "OnDemand": {
                "term": {
                    "priceDimensions": {
                        "dimension": {
                            "beginRange": "0",
                            "unit": "GB",
                            "pricePerUnit": {"USD": "0.50"},
                        }
                    }
                }
            }
        },
    }


class _CloudWatchCatalog:
    @staticmethod
    def attributes(product: dict[str, Any]) -> dict[str, str]:
        return product["product"]["attributes"]

    @staticmethod
    def products(
        service_code: str,
        filters: dict[str, str],
        *,
        max_pages: int = 20,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        del max_pages, refresh
        if service_code == "AmazonS3":
            return [
                _product(
                    "s3-storage",
                    group="",
                    usage_type="APS3-TimedStorage-ByteHrs",
                    service_code="AmazonS3",
                    productFamily="Storage",
                    storageClass="General Purpose",
                    volumeType="Standard",
                )
            ]
        assert service_code == "AmazonCloudWatch"
        group = filters.get("group")
        products = {
            "Ingested Logs": _product(
                "ingestion",
                group="Ingested Logs",
                usage_type="APS3-DataProcessing-Bytes",
                operation="PutLogEvents",
            ),
            "Amazon CloudWatch Standard Storage pricing current": _product(
                "storage",
                group="Amazon CloudWatch Standard Storage pricing current",
                usage_type="APS3-TimedStorage-ByteHrs",
            ),
            "Metric": _product(
                "metric",
                group="Metric",
                usage_type="APS3-CW:MetricMonitorUsage",
            ),
            "Alarm": _product(
                "alarm",
                group="Alarm",
                usage_type="APS3-CW:AlarmMonitorUsage",
            ),
            "Delivered Logs": _product(
                "delivered-s3",
                group="Delivered Logs",
                usage_type="APS3-S3-Egress-Bytes",
            ),
        }
        if group:
            product = products.get(group)
            return [product] if product is not None else []
        return list(products.values())


def test_cloudwatch_consumes_every_supported_customer_billing_field() -> None:
    selection = CloudWatchPlugin(None, _CloudWatchCatalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="cloudwatch",
            region="ap-south-1",
            requirements={
                "include_logs": True,
                "include_metrics": True,
                "log_ingestion_gib": 500,
                "log_storage_gib": 1024,
                "log_retention_days": 30,
                "custom_metrics": 100,
                "alarms": 20,
            },
        ),
        "ap-southeast-1",
    )

    assert {
        line.key: (line.amount, tuple(line.source_fields))
        for line in selection.usage_lines
    } == {
        "cwlog": (500, ("log_ingestion_gib", "include_logs")),
        "cwlogstore": (1024, ("log_storage_gib", "include_logs")),
        "cwmet": (100, ("custom_metrics", "include_metrics")),
        "cwalarm": (20, ("alarms",)),
    }
    assert selection.specifications["logStorageGiB"] == 1024
    assert selection.specifications["logRetentionDays"] == 30
    assert selection.applied_requirement_fields == ["log_retention_days"]


def test_cloudwatch_storage_uses_the_official_standard_storage_meter() -> None:
    selection = CloudWatchPlugin(None, _CloudWatchCatalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="cloudwatch",
            region="ap-south-1",
            requirements={
                "include_logs": True,
                "include_metrics": False,
                "log_ingestion_gib": 500,
                "log_storage_gib": 1024,
            },
        ),
        "ap-southeast-1",
    )

    storage = next(line for line in selection.usage_lines if line.key == "cwlogstore")
    assert storage.usage_type == "APS3-TimedStorage-ByteHrs"
    assert storage.amount == 1024
    assert storage.source_fields == ["log_storage_gib", "include_logs"]


def test_logs_only_request_does_not_add_unrequested_metric_reference_rate() -> None:
    selection = CloudWatchPlugin(None, _CloudWatchCatalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="cloudwatch",
            region="ap-south-1",
            requirements={
                "include_logs": True,
                "log_ingestion_gib": 500,
                "log_storage_gib": 1024,
                "log_retention_days": 30,
            },
        ),
        "ap-southeast-1",
    )

    assert [line.key for line in selection.usage_lines] == ["cwlog", "cwlogstore"]
    assert selection.reference_rates == []
    assert selection.substitution_notice is None


def test_logs_only_request_without_volume_uses_official_minimum_unit_reference() -> None:
    selection = CloudWatchPlugin(None, _CloudWatchCatalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="cloudwatch",
            region="ap-south-1",
            requirements={
                "include_logs": True,
                "include_metrics": False,
            },
        ),
        "ap-southeast-1",
    )

    assert selection.usage_lines == []
    assert len(selection.reference_rates) == 1
    assert selection.reference_rates[0].usage_type == "APS3-DataProcessing-Bytes"
    assert "未提供 CloudWatch 用量" in (selection.substitution_notice or "")


def test_vpc_flow_logs_to_s3_prices_delivery_and_retained_s3_storage() -> None:
    selection = CloudWatchPlugin(None, _CloudWatchCatalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="cloudwatch",
            region="ap-south-1",
            requirements={
                "include_logs": True,
                "include_metrics": False,
                "log_destination": "s3",
                "log_delivery_to_s3_gib": 100,
                "log_retention_days": 30,
            },
        ),
        "ap-southeast-1",
    )

    assert {
        line.key: (line.service_code, line.usage_type, line.amount)
        for line in selection.usage_lines
    } == {
        "cwlogs3del": (
            "AmazonCloudWatch",
            "APS3-S3-Egress-Bytes",
            100,
        ),
        "cwlogs3sto": (
            "AmazonS3",
            "APS3-TimedStorage-ByteHrs",
            100,
        ),
    }
    assert selection.usage_lines[1].source_fields == [
        "log_delivery_to_s3_gib",
        "log_retention_days",
        "log_destination",
    ]
