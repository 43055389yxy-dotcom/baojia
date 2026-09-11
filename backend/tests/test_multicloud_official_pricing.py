from __future__ import annotations

from typing import Any

import pytest

from app.services.mcp_v2_pricing import (
    AlibabaPriceQuery,
    AzurePriceQuery,
    BaiduPriceQuery,
    CommercialRateField,
    CtyunPriceQuery,
    GcpPriceQuery,
    GetPricesRequest,
    HuaweiPriceQuery,
    OciPriceQuery,
    OfficialPricingService,
    TencentPriceQuery,
    VolcenginePriceQuery,
)
from app.services.official_cloud_clients import OfficialCloudClientError


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


class _AuthenticatedRecorder:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = iter(payloads)
        self.calls: list[Any] = []

    def __call__(self, query: Any) -> dict[str, Any]:
        self.calls.append(query)
        return next(self.payloads)


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


def test_oci_catalog_exposes_each_tier_as_a_stable_rate_candidate() -> None:
    http = _HttpRecorder(
        [
            {
                "items": [
                    {
                        "partNumber": "B93297",
                        "displayName": "Compute - Standard - A1 - OCPU",
                        "metricName": "OCPU Per Hour",
                        "currencyCodeLocalizations": [
                            {
                                "currencyCode": "USD",
                                "prices": [
                                    {
                                        "model": "PAY_AS_YOU_GO",
                                        "value": 0,
                                        "rangeMin": 0,
                                        "rangeMax": 3000,
                                    },
                                    {
                                        "model": "PAY_AS_YOU_GO",
                                        "value": 0.01,
                                        "rangeMin": 3000,
                                        "rangeMax": 999999999999999,
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
        ]
    )
    service = OfficialPricingService(_UnusedAwsExecutor(), http_get=http)

    result = service.get_prices(
        GetPricesRequest(
            queries=[
                OciPriceQuery(
                    query_id="oci-a1-ocpu",
                    part_number="B93297",
                    currency_code="USD",
                )
            ]
        )
    )["results"][0]

    rates = result["official_rate_candidates"]
    assert len(rates) == 2
    assert len({rate["rate_id"] for rate in rates}) == 2
    assert {rate["official_item_id"] for rate in rates} == {"B93297"}
    assert [rate["unit_price"] for rate in rates] == ["0", "0.01"]
    assert [rate["is_zero_rate"] for rate in rates] == [True, False]
    assert rates[1]["tier_start"] == "3000"
    assert rates[1]["unit"] == "OCPU Per Hour"


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


def test_gcp_response_filters_narrow_paginated_official_results_without_mcp_selection() -> None:
    http = _HttpRecorder(
        [
            {
                "services": [
                    {"serviceId": "first", "displayName": "Unrelated Service"},
                    {"serviceId": "compute", "displayName": "Compute Engine"},
                ],
                "nextPageToken": "page-2",
            },
            {
                "services": [
                    {"serviceId": "other", "displayName": "Another Service"},
                ]
            },
        ]
    )
    service = OfficialPricingService(
        _UnusedAwsExecutor(), http_get=http, gcp_api_key="not-a-real-secret"
    )

    result = service.get_prices(
        GetPricesRequest(
            queries=[
                GcpPriceQuery(
                    query_id="gcp-compute-service",
                    operation="list_services",
                    response_filters={"displayName": "Compute Engine"},
                    max_pages=4,
                )
            ]
        )
    )["results"][0]

    assert result["status"] == "exact"
    assert result["official_item_ids"] == ["compute"]
    assert result["services"] == [
        {"serviceId": "compute", "displayName": "Compute Engine"}
    ]
    assert len(http.calls) == 2
    assert http.calls[1][1]["pageToken"] == "page-2"


def test_oci_response_filters_are_caller_supplied_and_return_only_exact_matches() -> None:
    http = _HttpRecorder(
        [
            {
                "items": [
                    {"partNumber": "B1", "displayName": "Virtual Machine Standard"},
                    {"partNumber": "B2", "displayName": "Object Storage"},
                ]
            }
        ]
    )
    service = OfficialPricingService(_UnusedAwsExecutor(), http_get=http)

    result = service.get_prices(
        GetPricesRequest(
            queries=[
                OciPriceQuery(
                    query_id="oci-object-storage",
                    response_filters={"displayName": "Object Storage"},
                )
            ]
        )
    )["results"][0]

    assert result["status"] == "exact"
    assert result["official_item_ids"] == ["B2"]
    assert result["items"] == [
        {"partNumber": "B2", "displayName": "Object Storage"}
    ]


@pytest.mark.parametrize(
    ("query_type", "provider", "endpoint"),
    [
        (TencentPriceQuery, "tencent", "cvm.tencentcloudapi.com"),
        (AlibabaPriceQuery, "alibaba", "ecs.cn-hangzhou.aliyuncs.com"),
        (HuaweiPriceQuery, "huawei", "bss.myhuaweicloud.com"),
        (BaiduPriceQuery, "baidu", "bcc.bj.baidubce.com"),
        (VolcenginePriceQuery, "volcengine", "open.volcengineapi.com"),
        (CtyunPriceQuery, "ctyun", "ctapi-global.ctapi.ctyun.cn"),
    ],
)
def test_authenticated_clouds_preserve_raw_candidates_and_declared_rates(
    query_type: type[Any],
    provider: str,
    endpoint: str,
) -> None:
    authenticated = _AuthenticatedRecorder(
        [
            {
                "result": {
                    "items": [
                        {
                            "sku": "small-2c4g",
                            "name": "2 vCPU / 4 GiB",
                            "price": "0.125",
                            "currency": "CNY",
                            "unit": "hour",
                        }
                    ]
                }
            }
        ]
    )
    service = OfficialPricingService(
        _UnusedAwsExecutor(),
        authenticated_request=authenticated,
        provider_credentials={
            provider: {"access_key_id": "configured", "secret_access_key": "configured"}
        },
    )
    query = query_type(
        query_id=f"{provider}-compute",
        endpoint=endpoint,
        service="compute",
        action="QueryPrice" if provider != "ctyun" else "",
        version="2020-01-01" if provider not in {"huawei", "baidu", "ctyun"} else None,
        region="cn-test-1",
        method="POST",
        path="/v1/query-price" if provider in {"huawei", "baidu", "ctyun"} else "/",
        response_items_path="result.items",
        item_id_paths=["sku"],
        rate_fields=[
            CommercialRateField(
                unit_price_path="price",
                currency_path="currency",
                unit_path="unit",
                description_path="name",
            )
        ],
    )

    result = service.get_prices(GetPricesRequest(queries=[query]))["results"][0]

    assert result["provider"] == provider
    assert result["status"] == "exact"
    assert result["official_item_ids"] == ["small-2c4g"]
    assert result["items"][0]["name"] == "2 vCPU / 4 GiB"
    assert result["official_rate_candidates"][0]["unit_price"] == "0.125"
    assert result["official_rate_candidates"][0]["official_item_id"] == "small-2c4g"
    assert authenticated.calls[0].provider == provider


def test_authenticated_cloud_large_result_requires_refinement_without_truncation() -> None:
    authenticated = _AuthenticatedRecorder(
        [
            {
                "result": {
                    "items": [
                        {"sku": f"sku-{index}", "family": "general"}
                        for index in range(11)
                    ]
                }
            }
        ]
    )
    service = OfficialPricingService(
        _UnusedAwsExecutor(),
        authenticated_request=authenticated,
        provider_credentials={
            "tencent": {"access_key_id": "configured", "secret_access_key": "configured"}
        },
    )

    result = service.get_prices(
        GetPricesRequest(
            queries=[
                TencentPriceQuery(
                    query_id="tencent-too-broad",
                    endpoint="cvm.tencentcloudapi.com",
                    service="cvm",
                    action="DescribeInstanceTypeConfigs",
                    version="2017-03-12",
                    region="ap-guangzhou",
                    response_items_path="result.items",
                    item_id_paths=["sku"],
                )
            ]
        )
    )["results"][0]

    assert result["status"] == "needs_refinement"
    assert result["matched_count"] == 11
    assert result["official_item_ids"] == []
    assert "items" not in result
    assert result["refinement_fields"]


@pytest.mark.parametrize(
    ("query_type", "endpoint"),
    [
        (TencentPriceQuery, "example.com"),
        (AlibabaPriceQuery, "ecs.example.com"),
        (HuaweiPriceQuery, "localhost"),
        (BaiduPriceQuery, "127.0.0.1"),
        (VolcenginePriceQuery, "metadata.google.internal"),
        (CtyunPriceQuery, "example.cn"),
    ],
)
def test_authenticated_cloud_queries_reject_non_official_hosts(
    query_type: type[Any], endpoint: str
) -> None:
    with pytest.raises(ValueError, match="official endpoint"):
        query_type(
            query_id="bad-host",
            endpoint=endpoint,
            service="compute",
            action="QueryPrice",
            version="2020-01-01",
            region="cn-test-1",
            path="/v1/query-price",
        )


@pytest.mark.parametrize(
    ("query_type", "endpoint"),
    [
        (TencentPriceQuery, "cvm.tencentcloudapi.com"),
        (AlibabaPriceQuery, "ecs.cn-hangzhou.aliyuncs.com"),
        (HuaweiPriceQuery, "ecs.cn-north-4.myhuaweicloud.com"),
        (BaiduPriceQuery, "bcc.bj.baidubce.com"),
        (VolcenginePriceQuery, "open.volcengineapi.com"),
        (CtyunPriceQuery, "ctapi-global.ctapi.ctyun.cn"),
    ],
)
def test_authenticated_cloud_queries_reject_mutating_operations(
    query_type: type[Any], endpoint: str
) -> None:
    with pytest.raises(ValueError, match="read-only"):
        query_type(
            query_id="unsafe-write",
            endpoint=endpoint,
            service="compute",
            action="CreateInstance",
            version="2020-01-01",
            region="cn-test-1",
            path="/v1/create-instance",
        )


def test_authenticated_query_failure_returns_machine_recovery_plan() -> None:
    def fail(_: Any) -> dict[str, Any]:
        raise OfficialCloudClientError(
            "The official request is missing OrderType.",
            code="alibaba_missing_parameter",
            category="invalid_request",
            retryable=True,
            details={
                "http_status": 400,
                "provider_code": "MissingParameter",
                "request_id": "request-123",
            },
        )

    service = OfficialPricingService(
        _UnusedAwsExecutor(),
        authenticated_request=fail,
        provider_credentials={
            "alibaba": {
                "access_key_id": "configured",
                "secret_access_key": "configured",
            }
        },
    )

    result = service.get_prices(
        GetPricesRequest(
            queries=[
                AlibabaPriceQuery(
                    query_id="tair-price",
                    endpoint="r-kvstore.ap-southeast-1.aliyuncs.com",
                    service="r-kvstore",
                    action="DescribePrice",
                    version="2015-01-01",
                    region="ap-southeast-1",
                )
            ]
        )
    )["results"][0]

    assert result["status"] == "query_failed"
    assert result["terminal"] is False
    assert result["error_category"] == "invalid_request"
    assert result["recovery"]["next_action"] == "repair_official_request_schema"
    assert result["recovery"]["allowed_sources"] == [
        "official_documentation",
        "official_sdk",
        "official_openapi",
        "official_pricing_calculator",
    ]


def test_successful_authenticated_route_returns_verifiable_learning_metadata() -> None:
    authenticated = _AuthenticatedRecorder(
        [{"result": {"items": [{"sku": "sku-1", "price": "1.25"}]}}]
    )
    service = OfficialPricingService(
        _UnusedAwsExecutor(),
        authenticated_request=authenticated,
        provider_credentials={
            "alibaba": {
                "access_key_id": "configured",
                "secret_access_key": "configured",
            }
        },
    )
    result = service.get_prices(
        GetPricesRequest(
            queries=[
                AlibabaPriceQuery(
                    query_id="ecs-price",
                    endpoint="ecs.ap-southeast-1.aliyuncs.com",
                    service="ecs",
                    action="DescribePrice",
                    version="2014-05-26",
                    region="ap-southeast-1",
                    response_items_path="result.items",
                    item_id_paths=["sku"],
                    rate_fields=[CommercialRateField(unit_price_path="price")],
                    official_source_url=(
                        "https://help.aliyun.com/document_detail/25499.html"
                    ),
                )
            ]
        )
    )["results"][0]

    route = result["route_verification"]
    assert route["auth_scheme"] == "alibaba_rpc_hmac_sha1"
    assert route["official_source_url"].startswith("https://help.aliyun.com/")
    assert route["request_schema_hash"].startswith("sha256:")
    assert route["response_schema_hash"].startswith("sha256:")
    assert route["sdk_version"] == "astraquote-direct-signer/1"
    assert route["failure_count"] == 0
    assert route["confidence"] > 0
    assert route["expires_at"] > route["last_verified_at"]


def test_transient_official_transport_failure_is_retried_without_changing_query() -> None:
    calls = 0

    def eventually_succeeds(_: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OfficialCloudClientError(
                "The official endpoint temporarily disconnected.",
                code="tencent_transport_error",
                category="transport",
                retryable=True,
            )
        return {"result": {"items": [{"sku": "sku-1", "price": "1.25"}]}}

    service = OfficialPricingService(
        _UnusedAwsExecutor(),
        authenticated_request=eventually_succeeds,
        provider_credentials={
            "tencent": {
                "access_key_id": "configured",
                "secret_access_key": "configured",
            }
        },
    )
    result = service.get_prices(
        GetPricesRequest(
            queries=[
                TencentPriceQuery(
                    query_id="cvm-price",
                    endpoint="cvm.tencentcloudapi.com",
                    service="cvm",
                    action="InquiryPriceRunInstances",
                    version="2017-03-12",
                    region="ap-guangzhou",
                    response_items_path="result.items",
                    item_id_paths=["sku"],
                    rate_fields=[CommercialRateField(unit_price_path="price")],
                )
            ]
        )
    )["results"][0]

    assert calls == 2
    assert result["status"] == "exact"
    assert result["attempt_count"] == 2
