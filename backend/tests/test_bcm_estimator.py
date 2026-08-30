from app.domain.models import UsageLine
from types import SimpleNamespace

from app.services.bcm_estimator import BcmWorkloadEstimator, _match_source_line


def test_match_source_line_narrows_duplicate_dimensions_to_bcm_group() -> None:
    requested = [
        UsageLine(
            key="A1",
            group="service-1",
            service_code="AmazonEC2",
            usage_type="APS1-BoxUsage:a1.2xlarge",
            operation="RunInstances",
            amount=720,
        ),
        UsageLine(
            key="A2",
            group="service-5",
            service_code="AmazonEC2",
            usage_type="APS1-BoxUsage:a1.2xlarge",
            operation="RunInstances",
            amount=730,
        ),
        UsageLine(
            key="A3",
            group="service-5",
            service_code="AWSDataTransfer",
            usage_type="APS1-DataTransfer-Out-Bytes",
            operation="",
            amount=1000,
        ),
    ]

    compute = _match_source_line(
        {
            "group": "service-5",
            "serviceCode": "AmazonEC2",
            "usageType": "APS1-BoxUsage:a1.2xlarge",
            "operation": "RunInstances",
        },
        requested,
    )
    transfer = _match_source_line(
        {
            "group": "service-5",
            "serviceCode": "AWSDataTransfer",
            "usageType": "APS1-DataTransfer-Out-Bytes",
            "operation": "",
        },
        requested,
    )

    assert compute is not None and compute.key == "A2"
    assert transfer is not None and transfer.key == "A3"


def test_match_source_line_uses_quantity_when_bcm_omits_duplicate_keys() -> None:
    requested = [
        UsageLine(
            key="ebs",
            group="service-1",
            service_code="AmazonEC2",
            usage_type="APS1-EBS:VolumeUsage.gp3",
            operation="",
            amount=600,
        ),
        UsageLine(
            key="ebs2",
            group="service-1",
            service_code="AmazonEC2",
            usage_type="APS1-EBS:VolumeUsage.gp3",
            operation="",
            amount=1800,
        ),
    ]

    matched = _match_source_line(
        {
            "group": "service-1",
            "serviceCode": "AmazonEC2",
            "usageType": "APS1-EBS:VolumeUsage.gp3",
            "operation": "",
            "quantity": {"unit": "GB-Mo", "amount": 1800},
        },
        requested,
    )

    assert matched is not None and matched.key == "ebs2"


def test_wait_for_result_consumes_keyless_duplicate_dimensions_once_each() -> None:
    requested = [
        UsageLine(
            key="ebs",
            group="service-1",
            service_code="AmazonEC2",
            usage_type="APS1-EBS:VolumeUsage.gp3",
            operation="",
            amount=600,
        ),
        UsageLine(
            key="ebs2",
            group="service-1",
            service_code="AmazonEC2",
            usage_type="APS1-EBS:VolumeUsage.gp3",
            operation="",
            amount=1800,
        ),
    ]
    items = [
        {
            "group": "service-1",
            "serviceCode": "AmazonEC2",
            "usageType": "APS1-EBS:VolumeUsage.gp3",
            "operation": "",
            "quantity": {"unit": "GB-Mo", "amount": 600},
            "cost": 57.6,
            "currency": "USD",
            "status": "VALID",
        },
        {
            "group": "service-1",
            "serviceCode": "AmazonEC2",
            "usageType": "APS1-EBS:VolumeUsage.gp3",
            "operation": "",
            "quantity": {"unit": "GB-Mo", "amount": 1800},
            "cost": 172.8,
            "currency": "USD",
            "status": "VALID",
        },
    ]

    estimator = object.__new__(BcmWorkloadEstimator)
    estimator._settings = SimpleNamespace(  # type: ignore[attr-defined]
        bcm_poll_timeout_seconds=1,
        bcm_poll_interval_seconds=0,
        bcm_rate_type="BEFORE_DISCOUNTS",
    )
    estimator._bcm = SimpleNamespace(  # type: ignore[attr-defined]
        get_workload_estimate=lambda **_kwargs: {
            "status": "VALID",
            "totalCost": 230.4,
            "costCurrency": "USD",
            "rateType": "BEFORE_DISCOUNTS",
        }
    )
    estimator._list_usage = lambda _estimate_id: items  # type: ignore[method-assign]

    result = estimator._wait_for_result("estimate", requested)

    assert [line.key for line in result.priced_lines] == ["ebs", "ebs2"]
    assert result.total_cost == 230.4
