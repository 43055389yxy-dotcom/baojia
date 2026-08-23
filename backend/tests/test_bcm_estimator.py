from app.domain.models import UsageLine
from app.services.bcm_estimator import _match_source_line


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
