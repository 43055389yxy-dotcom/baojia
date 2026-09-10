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
        ("tencent", ["on_demand", "one_year_commitment", "three_year_commitment"]),
        ("alibaba", ["on_demand", "one_year_commitment", "three_year_commitment"]),
        ("huawei", ["on_demand", "one_year_commitment", "three_year_commitment"]),
        ("baidu", ["on_demand", "one_year_commitment", "three_year_commitment"]),
        ("volcengine", ["on_demand", "one_year_commitment", "three_year_commitment"]),
        ("ctyun", ["on_demand", "one_year_commitment", "three_year_commitment"]),
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

    response = TestClient(aws_main.app).post(
        "/api/quote-relay/jobs",
        json={
            "customer_request": "2 核 4 GiB，一台，爱尔兰区域。",
            "cloud_provider": provider,
            "pricing_scenarios": scenarios,
            "utilization_percent": 100,
            "client_request_id": "123e4567-e89b-42d3-a456-426614174000",
        },
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    internal = store.get(job_id)
    assert internal["cloud_provider"] == provider
    assert internal["quote_options"]["cloud_provider"] == provider
    assert internal["quote_options"]["pricing_scenarios"] == scenarios


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
