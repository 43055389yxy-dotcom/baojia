from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import app.aws_main as aws_main
from app.services.mcp_v2_pricing import (
    GetPricesRequest,
    OfficialPricingService,
    PriceQuery,
)


class FakeExecutor:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def execute(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _price_product(sku: str, dimensions: dict[str, object]) -> str:
    return json.dumps(
        {
            "product": {
                "sku": sku,
                "productFamily": "Compute Instance",
                "attributes": {
                    "instanceType": "m7g.large",
                    "regionCode": "ap-southeast-1",
                    "operatingSystem": "Linux",
                    "tenancy": "Shared",
                },
            },
            "serviceCode": "AmazonEC2",
            "terms": {
                "OnDemand": {
                    f"{sku}.term": {
                        "offerTermCode": "JRTCKXETXF",
                        "effectiveDate": "2026-09-01T00:00:00Z",
                        "priceDimensions": dimensions,
                        "termAttributes": {},
                    }
                }
            },
        }
    )


def _reserved_price_product(sku: str) -> str:
    return json.dumps(
        {
            "product": {
                "sku": sku,
                "productFamily": "Compute Instance",
                "attributes": {
                    "instanceType": "m7g.large",
                    "regionCode": "ap-southeast-1",
                    "operatingSystem": "Linux",
                    "tenancy": "Shared",
                },
            },
            "serviceCode": "AmazonEC2",
            "terms": {
                "Reserved": {
                    f"{sku}.one-year": {
                        "offerTermCode": "ONEYEAR",
                        "effectiveDate": "2026-09-01T00:00:00Z",
                        "termAttributes": {
                            "LeaseContractLength": "1yr",
                            "PurchaseOption": "All Upfront",
                            "OfferingClass": "standard",
                        },
                        "priceDimensions": {
                            f"{sku}.one-year-upfront": {
                                "rateCode": f"{sku}.one-year-upfront",
                                "description": "One year all upfront",
                                "beginRange": "0",
                                "endRange": "Inf",
                                "unit": "Quantity",
                                "pricePerUnit": {"USD": "540.0000000000"},
                            }
                        },
                    },
                    f"{sku}.three-year": {
                        "offerTermCode": "THREEYEAR",
                        "effectiveDate": "2026-09-01T00:00:00Z",
                        "termAttributes": {
                            "LeaseContractLength": "3yr",
                            "PurchaseOption": "All Upfront",
                            "OfferingClass": "standard",
                        },
                        "priceDimensions": {
                            f"{sku}.three-year-upfront": {
                                "rateCode": f"{sku}.three-year-upfront",
                                "description": "Three year all upfront",
                                "beginRange": "0",
                                "endRange": "Inf",
                                "unit": "Quantity",
                                "pricePerUnit": {"USD": "1080.0000000000"},
                            }
                        },
                    },
                }
            },
        }
    )


def test_get_prices_batches_queries_and_returns_every_official_dimension() -> None:
    executor = FakeExecutor(
        [
            {
                "PriceList": [
                    _price_product(
                        "SKU1",
                        {
                            "SKU1.hour": {
                                "rateCode": "SKU1.hour",
                                "description": "Linux instance hours",
                                "beginRange": "0",
                                "endRange": "Inf",
                                "unit": "Hrs",
                                "pricePerUnit": {"USD": "0.1020000000"},
                            },
                            "SKU1.upfront": {
                                "rateCode": "SKU1.upfront",
                                "description": "Additional official dimension",
                                "beginRange": "0",
                                "endRange": "Inf",
                                "unit": "Quantity",
                                "pricePerUnit": {"USD": "1.0000000000"},
                            },
                        },
                    )
                ]
            },
            {"PriceList": []},
        ]
    )
    service = OfficialPricingService(executor)

    result = service.get_prices(
        GetPricesRequest(
            queries=[
                PriceQuery(
                    query_id="ec2",
                    service_code="AmazonEC2",
                    region="ap-southeast-1",
                    filters={
                        "instanceType": "m7g.large",
                        "operatingSystem": "Linux",
                        "tenancy": "Shared",
                    },
                ),
                PriceQuery(
                    query_id="missing",
                    service_code="AmazonS3",
                    region="ap-southeast-1",
                    filters={"storageClass": "Standard"},
                ),
            ]
        )
    )

    assert [item["status"] for item in result["results"]] == ["exact", "not_found"]
    exact = result["results"][0]
    assert exact["product_count"] == 1
    assert exact["price_dimension_count"] == 2
    assert [dimension["unit"] for dimension in exact["products"][0]["price_dimensions"]] == [
        "Hrs",
        "Quantity",
    ]
    assert exact["products"][0]["price_dimensions"][0]["price_per_unit"] == {
        "USD": "0.1020000000"
    }
    assert all(call["service"] == "pricing" for call in executor.calls)
    assert executor.calls[0]["parameters"]["ServiceCode"] == "AmazonEC2"
    assert {
        (item["Field"], item["Value"])
        for item in executor.calls[0]["parameters"]["Filters"]
    } >= {
        ("regionCode", "ap-southeast-1"),
        ("instanceType", "m7g.large"),
    }


def test_get_prices_marks_multiple_official_products_ambiguous_without_guessing() -> None:
    one = _price_product("SKU1", {})
    two = _price_product("SKU2", {})
    service = OfficialPricingService(FakeExecutor([{"PriceList": [one, two]}]))

    result = service.get_prices(
        GetPricesRequest(
            queries=[
                PriceQuery(
                    query_id="ambiguous",
                    service_code="AmazonEC2",
                    region="ap-southeast-1",
                    filters={"instanceType": "m7g.large"},
                )
            ]
        )
    )

    item = result["results"][0]
    assert item["status"] == "ambiguous"
    assert {product["sku"] for product in item["products"]} == {"SKU1", "SKU2"}
    assert "selected_product" not in item


def test_get_prices_requires_refinement_instead_of_returning_a_large_sku_set() -> None:
    products = [_price_product(f"SKU{index}", {}) for index in range(11)]
    service = OfficialPricingService(FakeExecutor([{"PriceList": products}]))

    result = service.get_prices(
        GetPricesRequest(
            queries=[
                PriceQuery(
                    query_id="too-broad",
                    service_code="AWSLambda",
                    region="ap-southeast-1",
                    filters={},
                )
            ]
        )
    )

    item = result["results"][0]
    assert item["status"] == "needs_refinement"
    assert item["matched_count"] == 11
    assert item["query"]["service_code"] == "AWSLambda"
    assert item["official_item_ids"] == []
    assert "products" not in item
    assert item["refinement_fields"]


def test_reserved_prices_use_price_list_terms_instead_of_account_offering_apis() -> None:
    executor = FakeExecutor([{"PriceList": [_reserved_price_product("SKU-RI")]}])
    service = OfficialPricingService(executor)

    result = service.get_prices(
        GetPricesRequest(
            queries=[
                PriceQuery(
                    query_id="ec2-ri-1y",
                    service_code="AmazonEC2",
                    region="ap-southeast-1",
                    filters={
                        "instanceType": "m7g.large",
                        "operatingSystem": "Linux",
                        "tenancy": "Shared",
                    },
                    pricing_model="reserved",
                    term_years=1,
                    payment_option="all_upfront",
                    offering_class="standard",
                )
            ]
        )
    )

    item = result["results"][0]
    assert item["status"] == "exact"
    assert item["source"] == "AWS Price List API"
    assert item["product_count"] == 1
    assert item["price_dimension_count"] == 1
    assert item["products"][0]["terms"][0]["term_attributes"] == {
        "LeaseContractLength": "1yr",
        "PurchaseOption": "All Upfront",
        "OfferingClass": "standard",
    }
    assert item["products"][0]["price_dimensions"][0]["price_per_unit"] == {
        "USD": "540.0000000000"
    }
    assert len(executor.calls) == 1
    assert executor.calls[0]["service"] == "pricing"
    assert executor.calls[0]["operation"] == "get_products"


def test_reserved_price_is_not_found_when_the_requested_official_term_is_absent() -> None:
    executor = FakeExecutor([{"PriceList": [_reserved_price_product("SKU-RI")]}])
    service = OfficialPricingService(executor)

    result = service.get_prices(
        GetPricesRequest(
            queries=[
                PriceQuery(
                    query_id="ec2-ri-3y-convertible",
                    service_code="AmazonEC2",
                    region="ap-southeast-1",
                    filters={"instanceType": "m7g.large"},
                    pricing_model="reserved",
                    term_years=3,
                    payment_option="all_upfront",
                    offering_class="convertible",
                )
            ]
        )
    )

    item = result["results"][0]
    assert item["status"] == "not_found"
    assert item["products"] == []
    assert item["price_dimension_count"] == 0


def test_v2_sales_pricing_boundary_does_not_offer_savings_plans_as_reserved_price() -> None:
    with pytest.raises(ValueError):
        PriceQuery.model_validate(
            {
                "query_id": "wrong-commitment-branch",
                "service_code": "AmazonEC2",
                "region": "ap-southeast-1",
                "filters": {"instanceType": "m7g.large"},
                "pricing_model": "savings_plan",
                "term_years": 1,
                "payment_option": "all_upfront",
                "savings_plan_type": "compute",
            }
        )


def test_v2_price_request_rejects_raw_customer_text() -> None:
    try:
        GetPricesRequest.model_validate(
            {
                "customer_request": "客户原话",
                "queries": [
                    {
                        "query_id": "ec2",
                        "service_code": "AmazonEC2",
                        "region": "ap-southeast-1",
                    }
                ],
            }
        )
    except Exception as exc:
        assert "customer_request" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("raw customer text must not enter the V2 price boundary")


def test_v2_api_is_authenticated_and_old_seven_step_routes_are_removed(monkeypatch) -> None:
    monkeypatch.setattr(aws_main.settings, "astraquote_mcp_internal_token", "test-token")
    client = TestClient(aws_main.app)
    payload = {
        "queries": [
            {
                "provider": "aws",
                "query_id": "ec2",
                "service_code": "AmazonEC2",
                "region": "ap-southeast-1",
                "filters": {"instanceType": "m7g.large"},
            }
        ]
    }

    missing = client.post("/api/mcp/v2/prices", json=payload)
    old = client.post(
        "/api/mcp/quotes/prepare",
        json={"components": []},
        headers={"X-AstraQuote-MCP-Token": "test-token"},
    )

    assert missing.status_code == 401
    assert old.status_code == 404


def test_v2_api_returns_complete_batch_from_official_pricing_service(monkeypatch) -> None:
    monkeypatch.setattr(aws_main.settings, "astraquote_mcp_internal_token", "test-token")

    def get_prices(payload):
        assert payload.queries[0].query_id == "ec2"
        return {
            "status": "completed",
            "result_count": 1,
            "results": [{"query_id": "ec2", "status": "exact", "products": []}],
        }

    monkeypatch.setattr(aws_main.mcp_v2_pricing, "get_prices", get_prices)
    response = TestClient(aws_main.app).post(
        "/api/mcp/v2/prices",
        json={
            "queries": [
                {
                    "provider": "aws",
                    "query_id": "ec2",
                    "service_code": "AmazonEC2",
                    "region": "ap-southeast-1",
                    "filters": {"instanceType": "m7g.large"},
                }
            ]
        },
        headers={"X-AstraQuote-MCP-Token": "test-token"},
    )

    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "exact"
