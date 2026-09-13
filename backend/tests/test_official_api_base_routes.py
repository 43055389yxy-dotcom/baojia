from __future__ import annotations

import pytest

from app.services.official_api_base_routes import official_api_base_route


@pytest.mark.parametrize(
    ("provider", "service", "region", "expected_endpoint"),
    [
        ("tencent", "cvm", "ap-shanghai", "cvm.tencentcloudapi.com"),
        ("tencent", "redis", "ap-shanghai", "redis.tencentcloudapi.com"),
        ("alibaba", "bssopenapi", "cn-hangzhou", "business.aliyuncs.com"),
        ("alibaba", "ecs", "cn-hangzhou", "business.aliyuncs.com"),
        ("alibaba", "r-kvstore", "cn-hangzhou", "business.aliyuncs.com"),
        (
            "alibaba_intl",
            "ecs",
            "ap-southeast-1",
            "business.ap-southeast-1.aliyuncs.com",
        ),
        ("huawei", "bss", "cn-north-4", "bss.myhuaweicloud.com"),
        ("huawei_intl", "ecs", "ap-southeast-3", "bss-intl.myhuaweicloud.com"),
        ("tencent", "ckafka", "ap-shanghai", "ckafka.tencentcloudapi.com"),
        ("tencent", "eip", "ap-shanghai", "vpc.tencentcloudapi.com"),
        ("baidu", "bcc", "bj", "bcc.bj.baidubce.com"),
        ("volcengine", "ecs", "cn-beijing", "open.volcengineapi.com"),
        ("ctyun", "ecs", "200000001790", "ctecs-global.ctapi.ctyun.cn"),
        ("ctyun", "redis", "200000001790", "dcs2-global.ctapi.ctyun.cn"),
        ("ctyun", "zos", "200000001790", "zos-global.ctapi.ctyun.cn"),
        ("ctyun", "cbr", "200000001790", "ctcbr-global.ctapi.ctyun.cn"),
        ("ctyun", "cce", "200000001790", "ccse-global.ctapi.ctyun.cn"),
        ("ctyun", "lts", "200000001790", "ctlts-global.ctapi.ctyun.cn"),
    ],
)
def test_official_base_routes_resolve_verified_provider_hosts(
    provider: str, service: str, region: str, expected_endpoint: str
) -> None:
    route = official_api_base_route(provider, service, region)

    assert route is not None
    assert route["endpoint"] == expected_endpoint
    assert route["capability"] == "quote_api"
    assert route["official_source_url"].startswith("https://")


def test_base_routes_do_not_guess_an_unknown_service() -> None:
    assert official_api_base_route("ctyun", "unknown-product", "200000001790") is None


def test_base_routes_reject_an_unsafe_region_interpolation() -> None:
    assert official_api_base_route("alibaba", "ecs", "cn-hangzhou/evil") is None


@pytest.mark.parametrize(
    ("provider", "service"),
    [
        ("tencent", "cos"),
        ("tencent", "cdn"),
        ("tencent", "waf"),
        ("tencent", "apigateway"),
        ("tencent", "elasticsearch"),
        ("tencent", "scf"),
        ("baidu", "billing"),
        ("baidu", "tsdb"),
        ("baidu", "vcr"),
        ("baidu", "rtc"),
        ("baidu", "doc"),
        ("baidu", "speech"),
        ("volcengine", "cdn"),
        ("ctyun", "kafka"),
    ],
)
def test_services_without_a_public_quote_api_are_routed_to_the_official_price_page(
    provider: str, service: str
) -> None:
    route = official_api_base_route(provider, service, "cn-test-1")

    assert route is not None
    assert route["capability"] == "official_page_only"
    assert route["endpoint"] == ""
    assert route["official_source_url"].startswith("https://")
