from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.aws_main as aws_main
from app.services.gpt_quote_relay import GptQuoteRelayStore
from app.services.mcp_v2_pricing import OfficialPricingService


@pytest.mark.parametrize(
    ("provider", "scenarios"),
    [
        ("aws", ["on_demand", "one_year_commitment", "three_year_commitment"]),
        ("azure", ["on_demand", "one_year_commitment", "three_year_commitment"]),
        ("oci", ["on_demand"]),
        ("gcp", ["on_demand", "one_year_commitment", "three_year_commitment"]),
        ("tencent", ["on_demand", "one_month_subscription", "one_year_subscription"]),
        ("alibaba", ["on_demand", "one_month_subscription", "one_year_subscription"]),
        ("huawei", ["on_demand", "one_month_subscription", "one_year_subscription"]),
        ("baidu", ["on_demand", "one_month_subscription", "one_year_subscription"]),
        ("volcengine", ["on_demand", "one_month_subscription", "one_year_subscription"]),
        ("ctyun", ["on_demand", "one_month_subscription", "one_year_subscription"]),
    ],
)
def test_sales_api_preserves_the_provider_and_exact_selected_scenarios(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
    scenarios: list[str],
) -> None:
    store = GptQuoteRelayStore(tmp_path / provider)
    monkeypatch.setattr(aws_main, "gpt_quote_relay", store)
    client = TestClient(aws_main.app)
    catalog = client.get(f"/api/quote-relay/providers/{provider}/regions").json()
    preferred_region = catalog["regions"][0]["code"]

    response = client.post(
        "/api/quote-relay/jobs",
        json={
            "customer_request": "2 核 4 GiB，一台，爱尔兰区域。",
            "cloud_provider": provider,
            "pricing_scenarios": scenarios,
            "utilization_percent": 100,
            "preferred_region": preferred_region,
            "client_request_id": "123e4567-e89b-42d3-a456-426614174000",
        },
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    internal = store.get(job_id)
    assert internal["cloud_provider"] == provider
    assert internal["quote_options"]["cloud_provider"] == provider
    assert internal["quote_options"]["pricing_scenarios"] == scenarios
    assert internal["quote_options"]["preferred_region"] == preferred_region


def test_provider_catalog_exposes_only_its_own_sales_pricing_scenarios() -> None:
    client = TestClient(aws_main.app)

    aws = client.get("/api/quote-relay/providers/aws/regions").json()
    tencent = client.get("/api/quote-relay/providers/tencent/regions").json()
    oci = client.get("/api/quote-relay/providers/oci/regions").json()

    assert [item["key"] for item in aws["pricing_scenarios"]] == [
        "on_demand", "one_year_commitment", "three_year_commitment",
    ]
    assert [item["key"] for item in tencent["pricing_scenarios"]] == [
        "on_demand", "one_month_subscription", "one_year_subscription",
    ]
    assert [item["label"] for item in tencent["pricing_scenarios"]] == [
        "按量计费", "包月", "包年（1 年）",
    ]
    assert oci["pricing_scenarios"] == [
        {"key": "on_demand", "label": "OCI 公开按量价", "term_months": None},
    ]


def test_sales_api_rejects_a_scenario_not_offered_by_the_selected_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(tmp_path / "provider-scenarios")
    monkeypatch.setattr(aws_main, "gpt_quote_relay", store)

    response = TestClient(aws_main.app).post(
        "/api/quote-relay/jobs",
        json={
            "customer_request": "CVM 2 核 4 GiB，一台。",
            "cloud_provider": "tencent",
            "pricing_scenarios": ["three_year_commitment"],
            "utilization_percent": 100,
            "preferred_region": "ap-singapore",
            "client_request_id": "123e4567-e89b-42d3-a456-426614174019",
        },
    )

    assert response.status_code == 422


def test_sales_region_catalog_is_scoped_to_the_configured_provider_site() -> None:
    response = TestClient(aws_main.app).get("/api/quote-relay/providers/alibaba/regions")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "alibaba"
    assert payload["market_profile"] == "alibaba-cn"
    assert payload["regions"]
    assert all(item["code"] and item["label"] for item in payload["regions"])


@pytest.mark.parametrize(
    ("provider", "code", "label"),
    [
        ("aws", "ap-southeast-1", "新加坡"),
        ("alibaba", "eu-west-1", "伦敦"),
        ("huawei", "ap-southeast-1", "香港"),
        ("volcengine", "ap-southeast-1", "柔佛"),
        ("oci", "ap-singapore-1", "新加坡"),
        ("tencent", "ap-singapore", "新加坡"),
    ],
)
def test_sales_region_labels_are_provider_scoped(
    provider: str,
    code: str,
    label: str,
) -> None:
    payload = TestClient(aws_main.app).get(
        f"/api/quote-relay/providers/{provider}/regions"
    ).json()
    labels = {item["code"]: item["label"] for item in payload["regions"]}

    assert label in labels[code]


def test_sales_region_catalog_has_no_duplicate_codes_and_exposes_source() -> None:
    client = TestClient(aws_main.app)
    for provider in (
        "aws", "azure", "oci", "gcp", "tencent", "alibaba",
        "huawei", "baidu", "volcengine", "ctyun",
    ):
        payload = client.get(f"/api/quote-relay/providers/{provider}/regions").json()
        codes = [item["code"] for item in payload["regions"]]
        assert len(codes) == len(set(codes)), provider
        assert payload["official_source_url"].startswith("https://"), provider
        assert payload["catalog_role"] == "official_provider_regions_only"
        assert payload["catalog_checked_at"] == "2026-09-12"


def test_alibaba_uses_the_official_hohhot_region_code() -> None:
    payload = TestClient(aws_main.app).get(
        "/api/quote-relay/providers/alibaba/regions"
    ).json()
    codes = {item["code"] for item in payload["regions"]}

    assert "cn-huhehaote" in codes
    assert "cn-hohhot" not in codes


def test_provider_catalog_keeps_current_official_region_codes_and_access_labels() -> None:
    client = TestClient(aws_main.app)
    azure = client.get("/api/quote-relay/providers/azure/regions").json()
    azure_labels = {item["code"]: item["label"] for item in azure["regions"]}
    assert "受限" not in azure_labels["australiacentral"]
    assert "受限" in azure_labels["australiacentral2"]

    huawei = client.get("/api/quote-relay/providers/huawei/regions").json()
    huawei_codes = {item["code"] for item in huawei["regions"]}
    assert {"cn-south-4", "cn-north-11", "cn-north-12"} <= huawei_codes


def test_sales_keeps_an_unknown_region_as_a_recoverable_preference(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(tmp_path / "new-region")
    monkeypatch.setattr(aws_main, "gpt_quote_relay", store)
    response = TestClient(aws_main.app).post(
        "/api/quote-relay/jobs",
        json={
            "customer_request": "ECS 2 核 4 GiB，一台。",
            "cloud_provider": "alibaba",
            "preferred_region": "not-a-real-region",
            "pricing_scenarios": ["on_demand"],
            "utilization_percent": 100,
            "client_request_id": "123e4567-e89b-42d3-a456-426614174000",
        },
    )

    assert response.status_code == 200
    assert response.json()["preferred_region"] == "not-a-real-region"


def test_sales_accepts_a_provider_scoped_region_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(tmp_path / "valid-region")
    monkeypatch.setattr(aws_main, "gpt_quote_relay", store)
    response = TestClient(aws_main.app).post(
        "/api/quote-relay/jobs",
        json={
            "customer_request": "ECS 2 核 4 GiB，一台。",
            "cloud_provider": "alibaba",
            "preferred_region": "ap-southeast-1",
            "pricing_scenarios": ["on_demand"],
            "utilization_percent": 100,
            "client_request_id": "123e4567-e89b-42d3-a456-426614174001",
        },
    )

    assert response.status_code == 200
    assert response.json()["preferred_region"] == "ap-southeast-1"


def test_public_health_marks_only_unconfigured_gcp_catalog_as_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(tmp_path / "relay")
    monkeypatch.setattr(aws_main, "gpt_quote_relay", store)
    monkeypatch.setattr(
        aws_main,
        "mcp_v2_pricing",
        OfficialPricingService(aws_main.mcp_v2_pricing._executor, gcp_api_key=""),
    )

    response = TestClient(aws_main.app).get("/api/quote-relay/health")

    assert response.status_code == 200
    catalogs = response.json()["provider_catalogs"]
    assert catalogs["aws"]["available"] is True
    assert catalogs["azure"]["available"] is True
    assert catalogs["oci"]["available"] is True
    assert catalogs["gcp"]["available"] is False
    assert "API Key" in catalogs["gcp"]["message"]


def test_sales_can_retry_only_the_failed_part_of_a_partial_quote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(tmp_path / "partial-retry")
    monkeypatch.setattr(aws_main, "gpt_quote_relay", store)
    public = store.create("原始需求随后会被删除。", {})
    store.claim_next("worker-a")
    store.purge_source(public["job_id"])
    store.update(
        public["job_id"],
        {
            "status": "partial",
            "quick_quote_result": {
                "schema_version": "astraquote-page-result/1",
                "is_partial": True,
                "unpriced_components": [{"service_name": "对象存储"}],
            },
            "quote_download_url": "https://example.test/partial.xlsx",
        },
    )

    response = TestClient(aws_main.app).post(
        f"/api/quote-relay/jobs/{public['job_id']}/retry-failed"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    internal = store.get(public["job_id"])
    assert internal["customer_request"] == ""
    assert internal["partial_retry_generation"] == 1
    assert internal["partial_retry_pending"] is True
