"""The official-template extractor is the only semantic writer."""

import ast
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.errors import ManualConfirmationRequired
from app.domain.models import ParsedIntent, ServiceRequirement
from app.domain.fact_ledger import customer_owned_source
from app.integrations.deepseek import DeepSeekIntentParser


@pytest.mark.asyncio
async def test_failed_extraction_cannot_seal_inventory_as_rule_engine_result(monkeypatch):
    parser = DeepSeekIntentParser(Settings(ai_api_key="test"))
    async def no_work(*args, **kwargs):
        return None
    async def no_defaults(*args, **kwargs):
        return {}, None
    async def failed(*args, **kwargs):
        raise ValueError("invalid template JSON")
    monkeypatch.setattr(parser, "_resolve_unknown_component_service", no_work)
    monkeypatch.setattr(parser, "_auto_discover_component", no_work)
    monkeypatch.setattr(parser, "_minimum_runtime_defaults", no_defaults)
    monkeypatch.setattr(parser, "_fill_component_template_with_retries", failed)
    component = ServiceRequirement(service="s3", source_text="S3 存储20TB")
    with pytest.raises(ManualConfirmationRequired) as error:
        await parser._cleanup_components(
            component.source_text,
            ParsedIntent(customer_summary="storage", services=[component]),
        )
    assert error.value.code == "official_template_extraction_failed"
    assert "_semantic_fact_mapping" not in component.field_sources


@pytest.mark.asyncio
async def test_unresolved_semantic_review_never_escapes_as_success(monkeypatch):
    parser = DeepSeekIntentParser(Settings(ai_api_key="test"))
    async def noop(*args, **kwargs):
        return None
    async def defaults(*args, **kwargs):
        return {}, None
    async def incomplete(**kwargs):
        return kwargs["component"].model_copy(deep=True)
    async def failed_audit(**kwargs):
        return ["customer quantity has no field owner"]
    for name in ("_resolve_unknown_component_service", "_auto_discover_component"):
        monkeypatch.setattr(parser, name, noop)
    monkeypatch.setattr(parser, "_minimum_runtime_defaults", defaults)
    monkeypatch.setattr(parser, "_fill_component_template_with_retries", incomplete)
    monkeypatch.setattr(parser, "_component_audit_issues", failed_audit)
    monkeypatch.setattr(parser, "_deterministic_component_audit_issues",
                        lambda *args: ["customer quantity has no field owner"])
    original = ServiceRequirement(service="future_service", source_text="服务用量4GB")
    with pytest.raises(ManualConfirmationRequired):
        await parser._cleanup_components(original.source_text,
            ParsedIntent(customer_summary="missing fact", services=[original]))
    assert "_semantic_fact_mapping" not in original.field_sources


@pytest.mark.parametrize("child", ["publicIpv4Address", "futurePaidFeature"])
def test_selected_child_cannot_inherit_parent_free_price_policy(child):
    from app.domain.service_billing_policies import no_additional_charge_decision
    component = ServiceRequirement(service="vpc",
        official_calculator_service_code=child,
        field_sources={"_official_calculator_parent_service_code": "amazonVirtualPrivateCloud"})
    assert no_additional_charge_decision(component) is None


def test_fact_reconciliation_never_extracts_fields_from_unsealed_drafts():
    component = ServiceRequirement(
        service="s3", source_text="S3 存储20TB", requirements={}
    )
    intent = ParsedIntent(customer_summary="old draft", services=[component])
    DeepSeekIntentParser.reconcile_customer_pricing_facts(intent)
    assert component.requirements == {}
    assert "_semantic_fact_mapping" not in component.field_sources


def test_component_cleaning_has_no_rule_writer_or_rule_fallback():
    root = Path(__file__).parents[1] / "app"
    tree = ast.parse((root / "integrations/deepseek.py").read_text())
    cleanup = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                   and n.name == "_cleanup_components")
    forbidden = {"_overlay_literal_component_facts", "preserve_customer_configuration",
                 "_normalize_database_group_quantity", "_normalize_redis_topology",
                 "_normalize_cluster_group_quantities", "_normalize_prometheus_managed_service"}
    calls = {n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
             for n in ast.walk(cleanup) if isinstance(n, ast.Call)
             and isinstance(n.func, (ast.Name, ast.Attribute))}
    assert calls.isdisjoint(forbidden), calls & forbidden


def test_quote_entry_points_have_no_legacy_prose_repair_branch():
    root = Path(__file__).parents[1] / "app"
    tree = ast.parse((root / "services/quote_service.py").read_text())
    forbidden = {"preserve_customer_configuration", "_reconcile_explicit_component_inventory",
                 "_split_eks_worker_nodes", "_normalize_database_group_quantity",
                 "_normalize_redis_topology", "_normalize_cluster_group_quantities",
                 "_reconcile_explicit_regions", "_inherit_single_workload_region",
                 "_merge_transfer_only_ec2_services"}
    for method in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                   and n.name in {"preview", "create_quote"}]:
        calls = {n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
                 for n in ast.walk(method) if isinstance(n, ast.Call)
                 and isinstance(n.func, (ast.Name, ast.Attribute))}
        assert calls.isdisjoint(forbidden), (method.name, calls & forbidden)


def test_browser_price_fallback_is_physically_removed():
    from app.services.quote_service import QuoteService
    assert not hasattr(QuoteService, "_create_calculator_quote")
    assert not hasattr(QuoteService, "_require_calculator")


def test_confirmation_storage_cannot_reopen_source_or_rebuild_inventory():
    path = Path(__file__).parents[1] / "app/services/confirmation_sessions.py"
    tree = ast.parse(path.read_text())
    forbidden = {"preserve_customer_configuration", "_split_eks_worker_nodes",
                 "_reconcile_explicit_component_inventory", "_reconcile_explicit_regions",
                 "_normalize_review_group_quantities"}
    calls = {n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
             for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, (ast.Name, ast.Attribute))}
    assert calls.isdisjoint(forbidden)


def test_downstream_defaults_and_selection_never_read_customer_prose():
    path = Path(__file__).parents[1] / "app/services/quote_service.py"
    tree = ast.parse(path.read_text())
    methods = {"_dependency_remarks", "_customer_requested_special_hardware",
               "_apply_calculator_minimum_defaults", "_require_complete_literal_fact_coverage"}
    for method in ast.walk(tree):
        if isinstance(method, ast.FunctionDef) and method.name in methods:
            assert not [n for n in ast.walk(method) if isinstance(n, ast.Attribute)
                        and n.attr in {"source_text", "original_source_text"}], method.name


@pytest.mark.asyncio
async def test_saved_raw_draft_is_rejected_before_any_downstream_upgrade(monkeypatch):
    parser = DeepSeekIntentParser(Settings(ai_api_key="test"))
    parent = ServiceRequirement(
        service="ecs", component_key="cmp_parent", source_text="ECS 1套，Worker 4台8核16G",
        field_sources={"_owned_source_slice": "system_policy"},
        field_evidence={"_owned_source_slice_text": "ECS 1套"},
        requirements={"cluster_count": 4, "_review_selected_model": "old"},
        official_calculator_configuration={"confirmed": 7, "guessed": 9},
    )
    parent.field_sources["official_calculator_configuration.confirmed"] = "customer_confirmation"
    child = ServiceRequirement(
        service="ec2", component_key="cmp_child", parent_component_key="cmp_parent",
        derived_from_service="ecs", source_text="Worker 4台8核16G", quantity=4,
        requirements={"vcpu": 8, "memory_gib": 16},
        field_sources={"_semantic_fact_mapping": "ai_cleaning",
                       "_intake_pipeline_version": "official-only-v4",
                       "_source_retention_policy": "cleaned-only-v1"},
    )
    intent = ParsedIntent(customer_summary="old", services=[parent, child],
                          ambiguities=["兄弟组件仍待确认"])
    async def forbidden(*args, **kwargs):
        raise AssertionError("raw draft entered downstream official intake")
    monkeypatch.setattr(parser, "resume_official_calculator_intake", forbidden)
    with pytest.raises(ManualConfirmationRequired) as error:
        await parser.revalidate_saved_intent(intent)
    assert error.value.code == "cleaned_input_upgrade_required"


@pytest.mark.asyncio
async def test_current_draft_never_reextracts(monkeypatch):
    parser = DeepSeekIntentParser(Settings(ai_api_key="test"))
    intent = ParsedIntent(customer_summary="current", services=[ServiceRequirement(
        service="s3", field_sources={"_intake_pipeline_version": "official-only-v4",
                                      "_source_retention_policy": "cleaned-only-v1"})])
    async def forbidden(*args, **kwargs):
        raise AssertionError("current draft reentered AI")
    monkeypatch.setattr(parser, "resume_official_calculator_intake", forbidden)
    assert await parser.revalidate_saved_intent(intent) is intent


def test_official_customer_answer_wins_over_ai_and_cache():
    original = ServiceRequirement(
        service="s3", official_calculator_configuration={"storage": 20},
        field_sources={"official_calculator_configuration.storage": "customer_confirmation"})
    filled = ServiceRequirement(service="s3", official_calculator_configuration={"storage": 99})
    DeepSeekIntentParser._restore_authoritative_component_fields(original, filled)
    assert filled.official_calculator_configuration["storage"] == 20
