from __future__ import annotations

from typing import Any

from app.domain.models import ServiceRequirement
from app.integrations.aws import PricingCatalog
from app.services.plugins.minimum_services import SqsPlugin


def sqs_product(*, group: str = "SQS-APIRequest-Tier1") -> dict[str, Any]:
    usage_type = "Requests-RBP"
    return {
        "serviceCode": "AWSQueueService",
        "product": {
            "sku": "sqs-standard",
            "attributes": {
                "servicecode": "AWSQueueService",
                "regionCode": "us-east-1",
                "group": group,
                "groupDescription": "Amazon SQS Requests",
                "queueType": "Standard",
                "usagetype": usage_type,
                "operation": "",
            },
        },
        "terms": {
            "OnDemand": {
                "term": {
                    "priceDimensions": {
                        "dimension": {
                            "beginRange": "0",
                            "unit": "Requests",
                            "pricePerUnit": {"USD": "0.0000004"},
                        }
                    }
                }
            }
        },
    }


class Catalog:
    def __init__(self, products: list[dict[str, Any]], *, stale_first: bool = False):
        self._products = products
        self._stale_first = stale_first
        self.calls: list[tuple[dict[str, str], bool]] = []

    @staticmethod
    def attributes(product: dict[str, Any]) -> dict[str, str]:
        return PricingCatalog.attributes(product)

    def products(
        self,
        service_code: str,
        filters: dict[str, str],
        *,
        max_pages: int = 20,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        del max_pages
        assert service_code == "AWSQueueService"
        self.calls.append((filters, refresh))
        if self._stale_first and not refresh:
            return []
        return [
            product
            for product in self._products
            if all(
                product["product"]["attributes"].get(key) == value
                for key, value in filters.items()
            )
        ]


def requirement() -> ServiceRequirement:
    return ServiceRequirement(
        service="sqs",
        region="us-east-1",
        quantity=1,
        requirements={"requests": 1_000_000},
    )


def test_sqs_matches_current_official_requests_rbp_schema() -> None:
    catalog = Catalog([sqs_product()])

    selected = SqsPlugin(None, catalog).select(requirement(), "us-east-1")  # type: ignore[arg-type]

    assert selected.model == "SQS Standard"
    assert selected.usage_lines[0].usage_type == "Requests-RBP"
    assert selected.usage_lines[0].amount == 1_000_000


def test_sqs_refreshes_stale_catalog_before_failing() -> None:
    catalog = Catalog([sqs_product()], stale_first=True)

    selected = SqsPlugin(None, catalog).select(requirement(), "us-east-1")  # type: ignore[arg-type]

    assert selected.model == "SQS Standard"
    assert catalog.calls[:2] == [
        ({"regionCode": "us-east-1", "group": "SQS-APIRequest-Tier1"}, False),
        ({"regionCode": "us-east-1", "group": "SQS-APIRequest-Tier1"}, True),
    ]


def test_sqs_discovers_standard_queue_when_catalog_group_label_changes() -> None:
    catalog = Catalog([sqs_product(group="Requests")])

    selected = SqsPlugin(None, catalog).select(requirement(), "us-east-1")  # type: ignore[arg-type]

    assert selected.model == "SQS Standard"
    assert ({"regionCode": "us-east-1"}, True) in catalog.calls
