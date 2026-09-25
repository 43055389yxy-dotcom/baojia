from __future__ import annotations

from app.services.quote_workflow_policy import (
    quote_workflow_policy,
    render_workflow_policy_slice,
    workflow_policy_snapshot,
    workflow_policy_value,
    workflow_policy_version,
)


def test_workflow_policy_is_versioned_and_machine_snapshot_excludes_prompt_prose() -> None:
    policy = quote_workflow_policy()
    snapshot = workflow_policy_snapshot()

    assert policy["schema_version"] == "astraquote-quote-workflow-policy/1"
    assert workflow_policy_version() == "2026-09-25-gpt-direct-api-v1"
    assert snapshot["policy_version"] == workflow_policy_version()
    assert "prompt_directives" not in snapshot
    assert "consumer_slices" not in snapshot


def test_workflow_policy_projects_only_the_rules_needed_by_one_consumer() -> None:
    quote_context = render_workflow_policy_slice("quote_context")
    component_batch = render_workflow_policy_slice("component_batch")

    assert "官方价格 API" in quote_context
    assert "on_demand_fallback" in quote_context
    assert "回到原对话" not in quote_context
    assert "save_component_batch" in component_batch
    assert "逐个组件" in component_batch
    assert "官方价格 API" not in component_batch


def test_workflow_policy_contains_the_shared_batch_and_recovery_limits() -> None:
    assert workflow_policy_value("batching", "components_per_wave") == 5
    assert workflow_policy_value("batching", "waves_per_chat") == 2
    assert workflow_policy_value("batching", "max_active_chats_per_sales_job") == 3
    assert workflow_policy_value("recovery", "deferred_retry_rounds") == 1
    assert workflow_policy_value(
        "pricing", "official_api_network_attempt_limit_per_scope"
    ) == 2
    assert workflow_policy_value(
        "pricing", "prefer_verified_local_aws_pricing_routes"
    ) is False
    assert workflow_policy_value(
        "pricing", "local_aws_route_failure_is_component_scoped"
    ) is True
    assert workflow_policy_value(
        "pricing", "local_aws_route_fallback_to_official_page"
    ) is True
    assert workflow_policy_value(
        "pricing", "official_api_network_attempt_limit_per_scope"
    ) == 1 + workflow_policy_value(
        "pricing", "corrected_api_attempt_limit_per_scope"
    )
    assert workflow_policy_value(
        "batching", "max_deferred_components_per_retry"
    ) <= (
        workflow_policy_value("batching", "components_per_wave")
        * workflow_policy_value("batching", "waves_per_chat")
    )
