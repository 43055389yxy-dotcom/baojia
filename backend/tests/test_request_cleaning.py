"""Every request is cleaned by AI before the official per-component pass."""

import pytest
import httpx

from app.core.config import Settings
from app.core.errors import ManualConfirmationRequired
from app.domain.component_integrity import capture_customer_ledger, restore_customer_ledger
from app.domain.fact_ledger import customer_owned_source
from app.domain.cleaned_input import discard_original_input
from app.domain.models import ParsedIntent, ServiceRequirement
from app.integrations.deepseek import DeepSeekIntentParser
from app.integrations.prompt_library import build_inventory_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["1、", ""])
async def test_entire_request_reaches_cleaner_before_component_templates(monkeypatch, prefix):
    raw = "Amazon EC2：3个实例，数量3，每台内存16GB。"
    cleaned = "Amazon EC2｜数量：3个｜每台内存：16GB"
    request = prefix + raw
    parser = DeepSeekIntentParser(Settings(ai_api_key="test"))
    calls = []

    async def clean(**kwargs):
        calls.append(kwargs)
        return {"customer_summary": "测试", "services": [{
            "service": "ec2", "calculator_service_name": "Amazon EC2",
            "component_key": "cmp_source_0001", "quantity": 3,
            "requirements": {"memory_gib": 16},
            "source_text": cleaned, "original_source_text": raw,
        }], "ambiguities": []}

    async def templates(intent, **kwargs):
        assert len(calls) == 1
        assert calls[0]["user_content"] == request
        component = intent.services[0]
        assert component.source_text == cleaned
        assert customer_owned_source(component) == cleaned
        assert component.original_source_text is None
        assert component.intake_source_fragments == []
        raise ManualConfirmationRequired("probe", code="test_template_boundary")

    monkeypatch.setattr(parser, "_complete_intake_json", clean)
    monkeypatch.setattr(parser, "_prepare_official_calculator_intake", templates)
    with pytest.raises(ManualConfirmationRequired) as error:
        await parser.parse(request)
    assert error.value.code == "test_template_boundary"


def test_first_cleaning_prompt_covers_semantics_without_guessing():
    prompt = build_inventory_prompt()
    for rule in ("重复表达", "前端", "后端", "单台", "总量", "original_source_text",
                 "冲突", "不能仅因数字相同", "缺失", "不选 AWS 型号"):
        assert rule in prompt


def test_downstream_evidence_only_uses_cleaned_text_after_raw_is_discarded():
    parser = DeepSeekIntentParser(Settings(ai_api_key="test"))
    original = ServiceRequirement(service="ec2", quantity=3,
        source_text="EC2｜数量：3台｜每台内存：16GB",
        original_source_text="EC2三台机器，每台16G内存。")
    discard_original_input(ParsedIntent(customer_summary="test", services=[original]))
    assert original.original_source_text is None
    valid = {"quantity": 3, "requirements": {"memory_gib": 16},
             "field_evidence": {"quantity": "数量：3台", "requirements.memory_gib": "16GB"}}
    parsed = parser._component_from_template_output(valid, original)
    assert parsed.field_evidence["quantity"] == "数量：3台"
    with pytest.raises(ValueError, match="原文证据不存在"):
        parser._component_from_template_output({**valid,
            "field_evidence": {"quantity": "三台", "requirements.memory_gib": "16GB"}}, original)


@pytest.mark.parametrize("service,value", [("elb", 1), ("ec2", 3), ("future_product", 7)])
def test_same_field_repair_retains_all_literal_evidence(service, value):
    source = f"资源：{value} 个，数量 {value}。"
    original = ServiceRequirement(service=service, source_text=f"资源｜数量：{value}个",
                                  original_source_text=source)
    first = ServiceRequirement(service=service, quantity=value,
        field_evidence={"quantity": f"{value} 个"})
    second = ServiceRequirement(service=service, quantity=value,
        field_evidence={"quantity": f"数量 {value}"})
    merged = DeepSeekIntentParser._merge_monotonic_component_repair(original, first, second)
    assert merged.quantity == value
    assert f"{value} 个" in merged.field_evidence["quantity"]
    assert f"数量 {value}" in merged.field_evidence["quantity"]
    assert merged.field_evidence["quantity"] in source
    assert not DeepSeekIntentParser._uncovered_quantitative_claim_issues(source, merged)


def test_quantity_conflict_is_not_deduplicated_as_repeated_wording():
    source = "资源：3个，数量4。"
    original = ServiceRequirement(service="future_product", source_text=source)
    first = ServiceRequirement(service="future_product", quantity=3,
                               field_evidence={"quantity": "3个"})
    second = ServiceRequirement(service="future_product", quantity=4,
                                field_evidence={"quantity": "数量4"})
    merged = DeepSeekIntentParser._merge_monotonic_component_repair(original, first, second)
    assert DeepSeekIntentParser._uncovered_quantitative_claim_issues(source, merged)


def test_joining_evidence_cannot_absorb_unexplained_intervening_digits():
    source = "资源3个，另有3个工作节点，数量3"
    assert DeepSeekIntentParser._cover_proved_evidence(
        source, ["资源3个", "数量3"], ["资源3个", "数量3"]
    ) is None


def test_cleaned_shared_block_still_isolates_original_evidence():
    raw = "Redis 一主一从，每节点8GiB；S3对象存储500GB"
    first = ServiceRequirement(service="elasticache", original_source_text=raw,
        source_text="Redis｜每节点内存：8GiB｜一主一从")
    second = ServiceRequirement(service="s3", source_text=raw, original_source_text=raw)
    intent = ParsedIntent(customer_summary="shared", services=[first, second])
    DeepSeekIntentParser._isolate_shared_component_sources(intent)
    assert "500GB" not in customer_owned_source(first)
    assert "8GiB" not in customer_owned_source(second)
    discard_original_input(intent)
    assert all(item.original_source_text is None for item in intent.services)
    assert customer_owned_source(first) == first.source_text


def test_cleaner_role_groups_have_scoped_evidence_and_shared_clauses():
    raw = "EC2前端4台4核16G；后端6台8核32G；全部Linux；每台系统盘100G；后端数据盘500G"
    first = ServiceRequirement.model_validate({"service": "ec2", "component_key": "cmp_source_0001_a",
        "source_text": "EC2前端｜4台4核16G｜Linux｜系统盘100G", "original_source_text": raw,
        "intake_source_fragments": ["EC2前端4台4核16G", "全部Linux", "每台系统盘100G"]})
    second = ServiceRequirement.model_validate({"service": "ec2", "component_key": "cmp_source_0001_b",
        "source_text": "EC2后端｜6台8核32G｜Linux｜系统盘100G｜数据盘500G", "original_source_text": raw,
        "intake_source_fragments": ["后端6台8核32G", "全部Linux", "每台系统盘100G", "后端数据盘500G"]})
    intent = ParsedIntent(customer_summary="roles", services=[first, second])
    DeepSeekIntentParser._validate_cleaned_source_bindings("1、" + raw, intent)
    assert "6台" not in customer_owned_source(first)
    assert "500G" not in customer_owned_source(first)
    assert "4台" not in customer_owned_source(second)
    assert "系统盘100G" in customer_owned_source(first)
    assert "系统盘100G" in customer_owned_source(second)
    cleaned_request = discard_original_input(intent)
    assert raw not in cleaned_request
    assert all(item.original_source_text is None for item in intent.services)
    assert all(item.intake_source_fragments == [] for item in intent.services)
    assert customer_owned_source(first) == first.source_text


def test_first_cleaning_cannot_hide_usage_in_unassigned_fragment():
    raw = "服务4台，磁盘100GB"
    item = ServiceRequirement.model_validate({"service": "future_product", "source_text": "服务4台",
        "original_source_text": raw, "intake_source_fragments": ["服务4台"]})
    with pytest.raises(ValueError, match="100GB"):
        DeepSeekIntentParser._validate_cleaned_source_bindings(raw,
            ParsedIntent(customer_summary="missing", services=[item]))


def test_pre_cleaning_snapshot_cannot_pollute_narrowed_parent_or_child():
    raw = "控制面2套，工作节点6台，每台8核32G，系统盘100G"
    parent = ServiceRequirement(service="future_control", component_key="cmp_parent",
        source_text=raw, original_source_text=raw, quantity=6,
        requirements={"vcpu": 8, "memory_gib": 32, "system_disk_gib": 100},
        field_sources={path: "customer_text" for path in (
            "quantity", "requirements.vcpu", "requirements.memory_gib", "requirements.system_disk_gib")},
        field_evidence={"quantity": "6台", "requirements.vcpu": "8核",
            "requirements.memory_gib": "32G", "requirements.system_disk_gib": "系统盘100G"})
    snapshot = capture_customer_ledger(ParsedIntent(customer_summary="old", services=[parent]))
    parent.quantity = 2
    parent.requirements = {}
    parent.field_evidence = {"quantity": "2套", "_owned_source_slice_text": "控制面2套"}
    parent.field_sources = {"quantity": "customer_text", "_owned_source_slice": "system_policy"}
    child = ServiceRequirement(service="ec2", component_key="cmp_child", parent_component_key="cmp_parent",
        quantity=6, source_text="工作节点6台，每台8核32G，系统盘100G",
        original_source_text="工作节点6台，每台8核32G，系统盘100G",
        requirements={"vcpu": 8, "memory_gib": 32, "system_disk_gib": 100})
    before = child.model_dump()
    intent = ParsedIntent(customer_summary="split", services=[parent, child])
    restore_customer_ledger(intent, snapshot, restore_missing_components=False)
    assert parent.quantity == 2
    assert parent.requirements == {}
    assert child.model_dump() == before


@pytest.mark.asyncio
async def test_transient_intake_timeout_retries_routes_before_failing(monkeypatch):
    parser = DeepSeekIntentParser(Settings(
        ai_api_key="test",
        intake_ai_hedge_delay_seconds=0.01,
        intake_ai_primary_timeout_seconds=0.01,
        intake_ai_recovery_timeout_seconds=0.01,
        intake_ai_retry_timeout_seconds=0.02,
        intake_ai_retry_delay_seconds=0,
    ))

    class FlakyGateway:
        def __init__(self):
            self.calls = 0

        async def complete_json(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise httpx.ReadTimeout("temporary timeout")
            return {"customer_summary": "ok", "services": [], "ambiguities": []}

    gateway = FlakyGateway()
    monkeypatch.setattr(parser, "_intake_ai_gateways", lambda: [gateway])
    events = []

    async def report(stage, message):
        events.append((stage, message))

    result = await parser._complete_intake_json(
        system_prompt="clean",
        user_content="request",
        reporter=report,
    )

    assert result["customer_summary"] == "ok"
    assert gateway.calls == 2
    assert any(stage == "intake_retry" for stage, _ in events)


@pytest.mark.asyncio
async def test_persistent_intake_timeout_is_reported_as_service_failure(monkeypatch):
    parser = DeepSeekIntentParser(Settings(ai_api_key="test"))

    async def timeout(**kwargs):
        raise httpx.ReadTimeout("temporary timeout")

    monkeypatch.setattr(parser, "_complete_intake_json", timeout)
    with pytest.raises(ManualConfirmationRequired) as error:
        await parser.parse("Amazon EC2 两台")

    assert error.value.code == "ai_cleaning_temporarily_unavailable"
    assert "连接超时" in error.value.message


@pytest.mark.asyncio
async def test_schema_repair_uses_the_resilient_intake_router(monkeypatch):
    parser = DeepSeekIntentParser(Settings(ai_api_key="test"))
    calls = []

    async def routed_cleaner(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {
                "customer_summary": "EC2",
                "services": [{
                    "service": "ec2",
                    "quantity": "两台",
                    "source_text": "Amazon EC2｜数量：2台",
                    "original_source_text": "Amazon EC2 两台",
                }],
                "ambiguities": [],
            }
        return {
            "customer_summary": "EC2",
            "services": [{
                "service": "ec2",
                "quantity": 2,
                "source_text": "Amazon EC2｜数量：2台",
                "original_source_text": "Amazon EC2 两台",
            }],
            "ambiguities": [],
        }

    async def stop_after_cleaning(*args, **kwargs):
        raise ManualConfirmationRequired("probe", code="repair_routed")

    events = []
    async def report(stage, message):
        events.append(stage)

    monkeypatch.setattr(parser, "_complete_intake_json", routed_cleaner)
    monkeypatch.setattr(parser, "_prepare_official_calculator_intake", stop_after_cleaning)
    with pytest.raises(ManualConfirmationRequired) as error:
        await parser.parse("Amazon EC2 两台", reporter=report)

    assert error.value.code == "repair_routed"
    assert len(calls) == 2
    assert calls[1]["reporter"] is report
    assert "intake_schema_repair" in events
