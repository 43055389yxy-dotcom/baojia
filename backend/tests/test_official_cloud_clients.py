from __future__ import annotations

import ssl
from typing import Any

import pytest

from app.services.mcp_v2_pricing import (
    AlibabaPriceQuery,
    BaiduPriceQuery,
    TencentPriceQuery,
)
from app.services.official_cloud_clients import (
    OfficialCloudApiClient,
    OfficialCloudClientError,
)


class _Response:
    def __init__(
        self,
        payload: dict[str, Any],
        *,
        status_code: int = 200,
        url: str = "",
        history: list[Any] | None = None,
        json_error: bool = False,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.url = url
        self.history = history or []
        self.json_error = json_error

    def json(self) -> dict[str, Any]:
        if self.json_error:
            raise ValueError("not JSON")
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


def test_alibaba_unsupported_disk_value_is_a_correctable_parameter_error() -> None:
    recorder = _RequestRecorder(
        _Response(
            {
                "Code": "InvalidSystemDiskCategory.ValueNotSupported",
                "Message": "The specified system disk category is not supported.",
                "RequestId": "request-disk-123",
            },
            status_code=400,
        )
    )
    client = OfficialCloudApiClient(_credentials("alibaba"), request=recorder)
    query = AlibabaPriceQuery(
        query_id="ecs-price",
        endpoint="ecs.cn-hangzhou.aliyuncs.com",
        service="ecs",
        action="DescribePrice",
        version="2014-05-26",
        region="cn-hangzhou",
    )

    with pytest.raises(OfficialCloudClientError) as captured:
        client.execute(query)

    error = captured.value
    assert error.code == "alibaba_invalid_system_disk_category_value_not_supported"
    assert error.category == "invalid_request"
    assert error.retryable is True
    assert error.details["provider_code"] == (
        "InvalidSystemDiskCategory.ValueNotSupported"
    )


@pytest.mark.parametrize(
    ("code", "message", "expected_category", "expected_retryable"),
    [
        (
            "AuthFailure.UnauthorizedOperation",
            "You do not have permission to perform this operation.",
            "authorization",
            False,
        ),
        (
            "InvalidParameter",
            "The selected instance specification is not sold in this region.",
            "invalid_request",
            True,
        ),
        (
            "InternalError",
            "The service encountered an internal error.",
            "provider_unavailable",
            True,
        ),
        (
            "InvalidModuleCode",
            "The supplied pricing module is invalid.",
            "invalid_request",
            True,
        ),
    ],
)
def test_successful_http_with_provider_error_envelope_is_not_a_price_result(
    code: str,
    message: str,
    expected_category: str,
    expected_retryable: bool,
) -> None:
    recorder = _RequestRecorder(
        _Response(
            {
                "Response": {
                    "Error": {"Code": code, "Message": message},
                    "RequestId": "request-business-error-123",
                }
            },
            status_code=200,
        )
    )
    client = OfficialCloudApiClient(_credentials("tencent"), request=recorder)
    query = TencentPriceQuery(
        query_id="tencent-price-error",
        endpoint="redis.tencentcloudapi.com",
        service="redis",
        action="InquiryPriceCreateInstance",
        version="2018-04-12",
        region="ap-singapore",
        response_items_path="Response",
        item_id_paths=["RequestId"],
    )

    with pytest.raises(OfficialCloudClientError) as captured:
        client.execute(query)

    error = captured.value
    assert error.category == expected_category
    assert error.retryable is expected_retryable
    assert error.details == {
        "http_status": 200,
        "provider_code": code,
        "request_id": "request-business-error-123",
    }


def test_official_maintenance_redirect_is_a_retryable_provider_outage() -> None:
    redirect = type("Redirect", (), {"status_code": 302})()
    recorder = _RequestRecorder(
        _Response(
            {},
            status_code=200,
            url="https://www.oracle.com/splash/collabsuite/maintenance/external/index.html",
            history=[redirect],
            json_error=True,
        )
    )
    client = OfficialCloudApiClient(_credentials("alibaba"), request=recorder)
    query = AlibabaPriceQuery(
        query_id="provider-maintenance",
        endpoint="business.aliyuncs.com",
        service="bssopenapi",
        action="QueryPrice",
        version="2017-12-14",
        region="ap-southeast-1",
    )

    with pytest.raises(OfficialCloudClientError) as captured:
        client.execute(query)

    assert captured.value.category == "provider_unavailable"
    assert captured.value.retryable is True
    assert captured.value.details == {
        "http_status": 200,
        "redirect_statuses": [302],
    }


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
