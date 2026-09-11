from __future__ import annotations

import ssl
from typing import Any

import pytest

from app.services.mcp_v2_pricing import AlibabaPriceQuery, BaiduPriceQuery
from app.services.official_cloud_clients import (
    OfficialCloudApiClient,
    OfficialCloudClientError,
)


class _Response:
    def __init__(self, payload: dict[str, Any], *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _RequestRecorder:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> _Response:
        self.calls.append((method, url, kwargs))
        return self.response


def _credentials(provider: str) -> dict[str, dict[str, str]]:
    return {
        provider: {
            "access_key_id": "configured-access-key",
            "secret_access_key": "configured-secret-key",
        }
    }


def test_alibaba_rpc_request_flattens_repeated_fields_and_omits_empty_values() -> None:
    recorder = _RequestRecorder(_Response({"Success": True}))
    client = OfficialCloudApiClient(_credentials("alibaba"), request=recorder)
    query = AlibabaPriceQuery(
        query_id="bss-price",
        endpoint="business.aliyuncs.com",
        service="bssopenapi",
        action="QueryPrice",
        version="2017-12-14",
        region="ap-southeast-1",
        region_parameter="none",
        query_parameters={"RegionId": [], "OptionalValue": None},
        body={
            "ModuleList": [
                {
                    "ModuleCode": "InstanceType",
                    "Config": "ecs.g8i.xlarge",
                }
            ]
        },
    )

    client.execute(query)

    params = recorder.calls[0][2]["params"]
    assert "RegionId" not in params
    assert "OptionalValue" not in params
    assert params["ModuleList.1.ModuleCode"] == "InstanceType"
    assert params["ModuleList.1.Config"] == "ecs.g8i.xlarge"
    assert "ModuleList" not in params


def test_alibaba_rpc_request_never_invents_a_region_parameter() -> None:
    recorder = _RequestRecorder(_Response({"Success": True}))
    client = OfficialCloudApiClient(_credentials("alibaba"), request=recorder)
    query = AlibabaPriceQuery(
        query_id="bss-global",
        endpoint="business.aliyuncs.com",
        service="bssopenapi",
        action="QueryPrice",
        version="2017-12-14",
        region="ap-southeast-1",
        query_parameters={"ProductCode": "ecs"},
    )

    client.execute(query)

    params = recorder.calls[0][2]["params"]
    assert "RegionId" not in params
    assert "Region" not in params


def test_official_error_is_structured_before_http_raise_and_never_leaks_signed_url() -> None:
    recorder = _RequestRecorder(
        _Response(
            {
                "Code": "MissingParameter",
                "Message": "The parameter OrderType is required.",
                "RequestId": "request-123",
            },
            status_code=400,
        )
    )
    client = OfficialCloudApiClient(_credentials("alibaba"), request=recorder)
    query = AlibabaPriceQuery(
        query_id="tair-price",
        endpoint="r-kvstore.ap-southeast-1.aliyuncs.com",
        service="r-kvstore",
        action="DescribePrice",
        version="2015-01-01",
        region="ap-southeast-1",
    )

    with pytest.raises(OfficialCloudClientError) as captured:
        client.execute(query)

    error = captured.value
    assert error.code == "alibaba_missing_parameter"
    assert error.category == "invalid_request"
    assert error.retryable is True
    assert error.details == {
        "http_status": 400,
        "provider_code": "MissingParameter",
        "request_id": "request-123",
    }
    assert "configured-access-key" not in str(error)
    assert "Signature" not in str(error)


def test_baidu_known_official_hostname_mismatch_uses_chain_verified_tls_only() -> None:
    recorder = _RequestRecorder(_Response({"result": {"items": []}}))
    client = OfficialCloudApiClient(_credentials("baidu"), request=recorder)
    query = BaiduPriceQuery(
        query_id="bcc-singapore",
        endpoint="bcc.sin.baidubce.com",
        service="bcc",
        region="sin",
        method="GET",
        path="/v1/instance/spec",
    )

    client.execute(query)

    request_options = recorder.calls[0][2]
    tls_context = request_options["verify"]
    assert isinstance(tls_context, ssl.SSLContext)
    assert tls_context.check_hostname is False
    assert tls_context.verify_mode == ssl.CERT_REQUIRED
    assert request_options["follow_redirects"] is False


def test_baidu_other_official_hosts_keep_normal_hostname_verification() -> None:
    recorder = _RequestRecorder(_Response({"result": {"items": []}}))
    client = OfficialCloudApiClient(_credentials("baidu"), request=recorder)
    query = BaiduPriceQuery(
        query_id="billing-global",
        endpoint="billing.baidubce.com",
        service="billing",
        region="sin",
        method="GET",
        path="/v1/product/list",
    )

    client.execute(query)

    assert "verify" not in recorder.calls[0][2]
    assert recorder.calls[0][2]["follow_redirects"] is False
