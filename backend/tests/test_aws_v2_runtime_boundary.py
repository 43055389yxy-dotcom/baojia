from __future__ import annotations

from app import aws_main


def test_aws_runtime_exposes_only_v2_pricing_and_sales_relay_routes() -> None:
    paths = {route.path for route in aws_main.app.routes}

    assert {
        "/api/health",
        "/api/mcp/v2/health",
        "/api/mcp/v2/describe-service",
        "/api/mcp/v2/attribute-values",
        "/api/mcp/v2/search-products",
        "/api/mcp/v2/prices",
        "/api/quote-relay/jobs",
        "/api/quote-relay/jobs/{job_id}",
        "/api/quote-relay/jobs/{job_id}/cancel",
        "/api/quote-relay/health",
    } <= paths

    for legacy_path in {
        "/api/quotes",
        "/api/quotes/preview",
        "/api/quote-jobs",
        "/api/preview-jobs",
        "/api/prompt-library",
        "/api/cache/status",
        "/api/aws-product-registry",
        "/api/gpt-relay/jobs",
        "/api/aws/calculator-contracts",
    }:
        assert legacy_path not in paths


def test_aws_runtime_does_not_initialize_legacy_selection_engine() -> None:
    assert not hasattr(aws_main, "quote_service")
    assert not hasattr(aws_main, "quote_jobs")
    assert not hasattr(aws_main, "DeepSeekIntentParser")
    assert not hasattr(aws_main, "PluginRegistry")
