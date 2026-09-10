from __future__ import annotations

from typing import Any

import pytest

from app.services.mcp_v2_pricing import (
    AzurePriceQuery,
    GcpPriceQuery,
    GetPricesRequest,
    OciPriceQuery,
    OfficialPricingService,
)


class _UnusedAwsExecutor:
    def execute(self, **_: Any) -> dict[str, Any]:
        raise AssertionError("AWS executor must not be used by public catalog queries")


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _HttpRecorder:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = iter(payloads)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, *, params: dict[str, Any], timeout: float) -> _FakeResponse:
        self.calls.append((url, dict(params)))
        return _FakeResponse(next(self.payloads))


def test_azure_and_oci_are_raw_official_catalog_queries() -> None:
    http = _HttpRecorder(
        [
            {
                "Items": [
                    {
                        "meterId": "meter-1",
                        "type": "Consumption",
                        "armSkuName": "Standard_D2s_v5",
                        "retailPrice": 0.1,
                    }
                ],
                "NextPageLink": None,
            },
            {
                "items": [
                    {
                        "partNumber": "B107951",
                        "displayName": "OCI Compute",
                        "prices": [{"currencyCode": "USD", "value": 0.2}],
                    }
                ]
            },
        ]
    )
    service = OfficialPricingService(_UnusedAwsExecutor(), http_get=http)

    result = service.get_prices(
        GetPricesRequest(
            queries=[
                AzurePriceQuery(
                    query_id="azure-vm",
                    filter="serviceName eq 'Virtual Machines'",
                    currency_code="USD",
                ),
                OciPriceQuery(
                    query_id="oci-compute",
                    part_number="B107951",
                    currency_code="USD",
                ),
            ]
        )
    )

    assert [item["provider"] for item in result["results"]] == ["azure", "oci"]
    assert result["results"][0]["official_item_ids"]
    assert result["results"][1]["official_item_ids"] == ["B107951"]
    assert http.calls[0][0] == "https://prices.azure.com/api/retail/prices"
    assert http.calls[0][1]["$filter"] == "serviceName eq 'Virtual Machines'"
    assert http.calls[1][0] == "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/"
    assert http.calls[1][1] == {"partNumber": "B107951", "currencyCode": "USD"}


def test_gcp_catalog_requires_key_and_returns_raw_skus() -> None:
    without_key = OfficialPricingService(_UnusedAwsExecutor(), http_get=_HttpRecorder([]))
    missing_key_result = without_key.get_prices(
        GetPricesRequest(
            queries=[
                GcpPriceQuery(
                    query_id="gcp-skus",
                    operation="list_skus",
                    service_id="6F81-5844-456A",
                )
            ]
        )
    )
    assert missing_key_result["results"][0]["code"] == "gcp_api_key_not_configured"

    http = _HttpRecorder(
        [{"skus": [{"name": "services/s/skus/sku-1", "skuId": "sku-1"}]}]
    )
    service = OfficialPricingService(
        _UnusedAwsExecutor(), http_get=http, gcp_api_key="not-a-real-secret"
    )
    result = service.get_prices(
        GetPricesRequest(
            queries=[
                GcpPriceQuery(
                    query_id="gcp-skus",
                    operation="list_skus",
                    service_id="6F81-5844-456A",
                )
            ]
        )
    )
    assert result["results"][0]["official_item_ids"] == ["sku-1"]
    assert http.calls[0][0].endswith("/v1/services/6F81-5844-456A/skus")
    assert http.calls[0][1]["key"] == "not-a-real-secret"
    assert http.calls[0][1]["currencyCode"] == "USD"


def test_gcp_service_discovery_does_not_send_sku_only_currency_parameter() -> None:
    http = _HttpRecorder([{"services": [{"serviceId": "6F81-5844-456A"}]}])
    service = OfficialPricingService(
        _UnusedAwsExecutor(), http_get=http, gcp_api_key="not-a-real-secret"
    )

    result = service.get_prices(
        GetPricesRequest(
            queries=[GcpPriceQuery(query_id="gcp-services", operation="list_services")]
        )
    )

    assert result["results"][0]["official_item_ids"] == ["6F81-5844-456A"]
    assert "currencyCode" not in http.calls[0][1]


def test_azure_next_page_url_is_restricted_to_official_host() -> None:
    with pytest.raises(ValueError, match="prices.azure.com"):
        AzurePriceQuery(
            query_id="bad-page",
            next_page_url="https://example.com/steal",
        )
