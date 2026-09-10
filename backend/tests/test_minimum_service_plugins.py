from __future__ import annotations

from typing import Any

from app.domain.models import ServiceRequirement
from app.integrations.aws import PricingCatalog
from app.services.plugins.minimum_services import Route53Plugin, SqsPlugin


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


def route53_product(usage_type: str, unit: str, rate: float, *, group: str) -> dict[str, Any]:
    item = sqs_product(group=group)
    item["serviceCode"] = "AmazonRoute53"
    item["product"]["sku"] = usage_type
    item["product"]["attributes"].update(
        {
            "servicecode": "AmazonRoute53",
            "usagetype": usage_type,
            "operation": "",
        }
    )
    dimension = next(
        iter(next(iter(item["terms"]["OnDemand"].values()))["priceDimensions"].values())
    )
    dimension["unit"] = unit
    dimension["pricePerUnit"]["USD"] = str(rate)
    return item


class Route53Catalog(Catalog):
    def products(
        self,
        service_code: str,
        filters: dict[str, str],
        *,
        max_pages: int = 20,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        del max_pages
        assert service_code == "AmazonRoute53"
        self.calls.append((filters, refresh))
        return [
            product
            for product in self._products
            if all(
                product["product"]["attributes"].get(key) == value
                for key, value in filters.items()
            )
        ]


def test_route53_resolver_prices_network_interfaces_and_dns_queries() -> None:
    catalog = Route53Catalog(
        [
            route53_product(
                "USE1-ResolverNetworkInterface", "Hours", 0.125, group="DNS Query"
            ),
            route53_product("USE1-DNS-Queries", "Queries", 0.0000004, group="DNS Query"),
            route53_product("HostedZone", "HostedZone", 0.50, group="HostedZone"),
        ]
    )
    requirement = ServiceRequirement(
        service="route53",
        region="us-east-1",
        hours_per_month=730,
        requirements={
            "route53_type": "resolver",
            "resolver_endpoints": 6,
            "resolver_ip_addresses_per_endpoint": 2,
            "dns_queries": 800_000_000,
        },
    )

    selected = Route53Plugin(None, catalog).select(requirement, "us-east-1")  # type: ignore[arg-type]

    assert selected.region == "us-east-1"
    assert selected.model == "Route 53 Resolver Endpoint"
    assert [(line.usage_type, line.amount) for line in selected.usage_lines] == [
        ("USE1-ResolverNetworkInterface", 6 * 2 * 730),
        ("USE1-DNS-Queries", 800_000_000),
    ]
