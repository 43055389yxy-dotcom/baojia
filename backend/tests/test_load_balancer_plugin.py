from app.domain.models import ServiceRequirement
from app.services.plugins.common import AlbPlugin


def _priced_product(usage_type: str, operation: str, unit: str, price: float) -> dict:
    return {
        "serviceCode": "AWSELB",
        "product": {
            "sku": usage_type,
            "attributes": {
                "regionCode": "ap-south-1",
                "usagetype": usage_type,
                "operation": operation,
            },
        },
        "terms": {
            "OnDemand": {
                "term": {
                    "priceDimensions": {
                        "dimension": {
                            "beginRange": "0",
                            "unit": unit,
                            "pricePerUnit": {"USD": str(price)},
                        }
                    }
                }
            }
        },
    }


class LoadBalancerCatalog:
    products_by_operation = {
        "LoadBalancing:Application": [
            _priced_product(
                "APS3-LoadBalancerUsage", "LoadBalancing:Application", "Hrs", 0.025
            ),
            _priced_product(
                "APS3-LCUUsage", "LoadBalancing:Application", "LCU-Hrs", 0.008
            ),
        ],
        "LoadBalancing:Network": [
            _priced_product(
                "APS3-LoadBalancerUsage", "LoadBalancing:Network", "Hrs", 0.025
            ),
            _priced_product(
                "APS3-NLCUUsage", "LoadBalancing:Network", "NLCU-Hrs", 0.006
            ),
        ],
    }

    @classmethod
    def products(
        cls,
        service_code: str,
        filters: dict[str, str],
        *,
        max_pages: int = 3,
        refresh: bool = False,
    ) -> list[dict]:
        assert service_code == "AWSELB"
        products = cls.products_by_operation.get(filters.get("operation", ""), [])
        return [
            product
            for product in products
            if all(
                product["product"]["attributes"].get(key) == value
                for key, value in filters.items()
            )
        ]


def test_network_load_balancer_uses_network_product_and_operation() -> None:
    requirement = ServiceRequirement(
        service="elb",
        calculator_service_name="Network Load Balancer",
        region="ap-south-1",
        quantity=2,
        requirements={"load_balancer_type": "network"},
    )

    selected = AlbPlugin(None, LoadBalancerCatalog()).select(  # type: ignore[arg-type]
        requirement, "ap-south-1"
    )

    assert selected.display_name == "Network Load Balancer"
    assert selected.model == "Network Load Balancer"
    assert selected.quantity == 2
    assert selected.architecture == "2 个 NLB"
    assert selected.specifications["load_balancer_type"] == "network"
    assert selected.official_product["operation"] == "LoadBalancing:Network"
    assert selected.usage_lines[0].operation == "LoadBalancing:Network"
    assert selected.reference_rates[0].operation == "LoadBalancing:Network"
    assert "NLB" in (selected.substitution_notice or "")
