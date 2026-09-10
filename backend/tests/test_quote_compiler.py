from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.domain.fact_ledger import finalize_customer_fact_ledger
from app.domain.models import (
    BillingCalculationAudit,
    FactConsumption,
    PricedLine,
    SelectedResource,
    ServiceRequirement,
    UsageLine,
)
from app.domain.quote_compiler import (
    QuoteCompilationViolation,
    compile_quote_ir,
    quote_ir_audit_metadata,
)
from app.services.quote_service import QuoteService


def _requirement(*, component_key: str, memory_gib: float) -> ServiceRequirement:
    requirement = ServiceRequirement(
        service="redis",
        component_key=component_key,
        product_identity="elasticache_redis",
        region="ap-southeast-1",
        requirements={"memory_gib": memory_gib},
        source_text=f"Redis，单节点 {memory_gib:g} GiB",
        original_source_text=f"Redis，单节点 {memory_gib:g} GiB",
        field_sources={"requirements.memory_gib": "customer_text"},
        field_evidence={"requirements.memory_gib": f"{memory_gib:g} GiB"},
        field_scopes={"memory_gib": "per_node"},
    )
    finalize_customer_fact_ledger(requirement)
    return requirement


def _selection(
    requirement: ServiceRequirement,
    *,
    component_id: str,
    fact_id: str,
) -> SelectedResource:
    return SelectedResource(
        component_id=component_id,
        service="redis",
        display_name="Amazon ElastiCache for Redis",
        region="ap-southeast-1",
        model="cache.r6g.xlarge",
        quantity=2,
        architecture="arm64",
        requested_specifications={"memory_gib": 16},
        official_specifications={"memory_gib": 26.32},
        specifications={"memory_gib": 26.32},
        official_product={"source": "AWS Price List"},
        rationale="官方规格满足客户需求",
        usage_lines=[],
        fact_consumptions=[
            FactConsumption(
                fact_id=fact_id,
                path="requirements.memory_gib",
                consumer_type="selection",
                consumer_key="cache.r6g.xlarge",
                purpose="选择满足内存下限的官方型号",
            )
        ],
    )


def _usage(*, fact_id: str, group: str = "service-1") -> UsageLine:
    return UsageLine(
        key="s1l1",
        service_code="AmazonElastiCache",
        usage_type="APS1-NodeUsage:cache.r6g.xlarge",
        operation="CreateCacheCluster:0002",
        amount=1460,
        group=group,
        source_fields=["memory_gib"],
        source_fact_ids=[fact_id],
    )


def _price(*, amount: float = 1460) -> PricedLine:
    return PricedLine(
        key="s1l1",
        service_code="AmazonElastiCache",
        usage_type="APS1-NodeUsage:cache.r6g.xlarge",
        operation="CreateCacheCluster:0002",
        amount=amount,
        unit="Hrs",
        cost=389.42,
    )


def test_quote_compiler_keeps_customer_and_official_specs_isolated() -> None:
    requirement = _requirement(component_key="component_redis_01", memory_gib=16)
    fact_id = requirement.customer_pricing_facts[0].fact_id

    compilation = compile_quote_ir(
        requirements=[requirement],
        selections=[
            _selection(requirement, component_id="0", fact_id=fact_id),
        ],
        usage_lines=[_usage(fact_id=fact_id)],
        priced_lines=[_price()],
        total_cost=389.42,
        is_partial=False,
    )

    assert compilation.requirements[0].facts[0].value == 16
    assert compilation.resources[0].requested_specifications["memory_gib"] == 16
    assert compilation.resources[0].official_specifications["memory_gib"] == 26.32
    assert "source_text" not in compilation.model_dump_json()
    assert quote_ir_audit_metadata(compilation)["verification_status"] == "verified"


def test_quote_compiler_rejects_unknown_fact_lineage() -> None:
    requirement = _requirement(component_key="component_redis_01", memory_gib=16)
    fact_id = requirement.customer_pricing_facts[0].fact_id

    with pytest.raises(QuoteCompilationViolation, match="未知客户事实"):
        compile_quote_ir(
            requirements=[requirement],
            selections=[
                _selection(requirement, component_id="0", fact_id=fact_id),
            ],
            usage_lines=[_usage(fact_id="fact_0123456789abcdef")],
            priced_lines=[_price()],
            total_cost=389.42,
            is_partial=False,
        )


def test_quote_compiler_rejects_cross_component_fact_lineage() -> None:
    first = _requirement(component_key="component_redis_01", memory_gib=16)
    second = _requirement(component_key="component_redis_02", memory_gib=32)
    first_fact_id = first.customer_pricing_facts[0].fact_id
    second_fact_id = second.customer_pricing_facts[0].fact_id

    with pytest.raises(QuoteCompilationViolation, match="跨组件"):
        compile_quote_ir(
            requirements=[first, second],
            selections=[
                _selection(first, component_id="0", fact_id=first_fact_id),
                _selection(second, component_id="1", fact_id=second_fact_id),
            ],
            usage_lines=[_usage(fact_id=second_fact_id)],
            priced_lines=[_price()],
            total_cost=389.42,
            is_partial=False,
        )


def test_quote_compiler_rejects_price_usage_drift() -> None:
    requirement = _requirement(component_key="component_redis_01", memory_gib=16)
    fact_id = requirement.customer_pricing_facts[0].fact_id

    with pytest.raises(QuoteCompilationViolation, match="官方用量不一致"):
        compile_quote_ir(
            requirements=[requirement],
            selections=[
                _selection(requirement, component_id="0", fact_id=fact_id),
            ],
            usage_lines=[_usage(fact_id=fact_id)],
            priced_lines=[_price(amount=730)],
            total_cost=389.42,
            is_partial=False,
        )


@pytest.mark.parametrize("tamper", ["amount", "inputs", "version", "unit", "expression"])
def test_quote_compiler_rejects_changed_executable_calculation(tamper: str) -> None:
    from app.integrations.aws_component_templates.registry import component_template_spec

    requirement = _requirement(component_key="component_redis_01", memory_gib=16)
    fact_id = requirement.customer_pricing_facts[0].fact_id
    audit = (
        component_template_spec("redis")
        .evaluate(
            "cache_node_hours",
            {
                "quantity": 2,
                "hours_per_month": 730,
                "node_count": 2,
            },
        )
        .audit()
    )
    if tamper == "inputs":
        audit["inputs"]["node_count"] = "20"
    else:
        audit[
            {
                "amount": "amount",
                "version": "rule_version",
                "unit": "unit",
                "expression": "expression",
            }[tamper]
        ] = {
            "amount": "1",
            "version": "old-version",
            "unit": "GB",
            "expression": "quantity * 0",
        }[tamper]
    usage = _usage(fact_id=fact_id)
    usage.calculation = BillingCalculationAudit.model_validate(audit)
    with pytest.raises(QuoteCompilationViolation, match="计算底稿"):
        compile_quote_ir(
            requirements=[requirement],
            selections=[_selection(requirement, component_id="0", fact_id=fact_id)],
            usage_lines=[usage],
            priced_lines=[_price()],
            total_cost=389.42,
            is_partial=False,
        )


def test_quote_compiler_preserves_rule_version_in_its_fingerprint():
    from app.integrations.aws_component_templates.registry import component_template_spec

    requirement = _requirement(component_key="component_redis_01", memory_gib=16)
    fact_id = requirement.customer_pricing_facts[0].fact_id
    usage = _usage(fact_id=fact_id)
    usage.calculation = BillingCalculationAudit.model_validate(
        component_template_spec("redis")
        .evaluate(
            "cache_node_hours",
            {
                "quantity": 2,
                "hours_per_month": 730,
                "node_count": 2,
            },
        )
        .audit()
    )
    compilation = compile_quote_ir(
        requirements=[requirement],
        selections=[_selection(requirement, component_id="0", fact_id=fact_id)],
        usage_lines=[usage],
        priced_lines=[_price()],
        total_cost=389.42,
        is_partial=False,
    )
    assert compilation.usage[0].calculation.rule_version == usage.calculation.rule_version
    assert quote_ir_audit_metadata(compilation)["executable_usage_line_count"] == 1


def test_same_executable_meter_cannot_be_charged_twice_under_different_keys():
    from app.integrations.aws_component_templates.registry import component_template_spec

    requirement = _requirement(component_key="component_redis_01", memory_gib=16)
    fact_id = requirement.customer_pricing_facts[0].fact_id
    usage = _usage(fact_id=fact_id)
    usage.calculation = BillingCalculationAudit.model_validate(
        component_template_spec("redis")
        .evaluate(
            "cache_node_hours",
            {
                "quantity": 2,
                "hours_per_month": 730,
                "node_count": 2,
            },
        )
        .audit()
    )
    with pytest.raises(QuoteCompilationViolation, match="重复执行计费规则"):
        compile_quote_ir(
            requirements=[requirement],
            selections=[_selection(requirement, component_id="0", fact_id=fact_id)],
            usage_lines=[usage, usage.model_copy(update={"key": "s1l2"})],
            priced_lines=[_price(), _price().model_copy(update={"key": "s1l2"})],
            total_cost=778.84,
            is_partial=False,
        )


def test_complete_quote_compiler_rejects_missing_component_resource() -> None:
    first = _requirement(component_key="component_redis_01", memory_gib=16)
    second = _requirement(component_key="component_redis_02", memory_gib=32)
    first_fact_id = first.customer_pricing_facts[0].fact_id

    with pytest.raises(QuoteCompilationViolation, match="没有生成资源结果"):
        compile_quote_ir(
            requirements=[first, second],
            selections=[
                _selection(first, component_id="0", fact_id=first_fact_id),
            ],
            usage_lines=[_usage(fact_id=first_fact_id)],
            priced_lines=[_price()],
            total_cost=389.42,
            is_partial=False,
        )


def test_complete_quote_compiler_rejects_unconsumed_customer_fact() -> None:
    requirement = _requirement(component_key="component_redis_01", memory_gib=16)
    fact_id = requirement.customer_pricing_facts[0].fact_id
    selection = _selection(requirement, component_id="0", fact_id=fact_id)
    selection.fact_consumptions = []

    with pytest.raises(QuoteCompilationViolation, match="客户事实未被资源或计费用量消费"):
        compile_quote_ir(
            requirements=[requirement],
            selections=[selection],
            usage_lines=[],
            priced_lines=[],
            total_cost=0,
            is_partial=False,
        )


def test_quote_compiler_rejects_orphan_parent_child_relationship() -> None:
    requirement = _requirement(component_key="component_redis_child", memory_gib=16)
    requirement.parent_component_key = "component_missing_parent"
    fact_id = requirement.customer_pricing_facts[0].fact_id

    with pytest.raises(QuoteCompilationViolation, match="父组件"):
        compile_quote_ir(
            requirements=[requirement],
            selections=[
                _selection(requirement, component_id="0", fact_id=fact_id),
            ],
            usage_lines=[_usage(fact_id=fact_id)],
            priced_lines=[_price()],
            total_cost=389.42,
            is_partial=False,
        )


def test_quote_compiler_rejects_duplicate_component_keys() -> None:
    first = _requirement(component_key="component_same", memory_gib=16)
    second = _requirement(component_key="component_same", memory_gib=32)
    first_fact_id = first.customer_pricing_facts[0].fact_id
    second_fact_id = second.customer_pricing_facts[0].fact_id

    with pytest.raises(QuoteCompilationViolation, match="重复组件键"):
        compile_quote_ir(
            requirements=[first, second],
            selections=[
                _selection(first, component_id="0", fact_id=first_fact_id),
                _selection(second, component_id="1", fact_id=second_fact_id),
            ],
            usage_lines=[],
            priced_lines=[],
            total_cost=0,
            is_partial=True,
        )


def test_quote_compiler_rejects_parent_cycle() -> None:
    first = _requirement(component_key="component_first", memory_gib=16)
    second = _requirement(component_key="component_second", memory_gib=32)
    first.parent_component_key = second.component_key
    second.parent_component_key = first.component_key
    first_fact_id = first.customer_pricing_facts[0].fact_id
    second_fact_id = second.customer_pricing_facts[0].fact_id
    first_selection = _selection(first, component_id="0", fact_id=first_fact_id)
    second_selection = _selection(second, component_id="1", fact_id=second_fact_id)
    first_selection.parent_component_id = "1"
    second_selection.parent_component_id = "0"

    with pytest.raises(QuoteCompilationViolation, match="父子组件关系存在环"):
        compile_quote_ir(
            requirements=[first, second],
            selections=[first_selection, second_selection],
            usage_lines=[],
            priced_lines=[],
            total_cost=0,
            is_partial=True,
        )


def test_quote_compiler_rejects_free_resource_with_billable_usage() -> None:
    requirement = _requirement(component_key="component_free", memory_gib=16)
    fact_id = requirement.customer_pricing_facts[0].fact_id
    selection = _selection(requirement, component_id="0", fact_id=fact_id)
    selection.pricing_status = "free"

    with pytest.raises(QuoteCompilationViolation, match="免费资源.*计费用量"):
        compile_quote_ir(
            requirements=[requirement],
            selections=[selection],
            usage_lines=[_usage(fact_id=fact_id)],
            priced_lines=[],
            total_cost=0,
            is_partial=True,
        )


def test_quote_compiler_rejects_customer_requirement_rewritten_by_selection() -> None:
    requirement = _requirement(component_key="component_redis_01", memory_gib=16)
    fact_id = requirement.customer_pricing_facts[0].fact_id
    selection = _selection(requirement, component_id="0", fact_id=fact_id)
    selection.requested_specifications["memory_gib"] = 26.32

    with pytest.raises(QuoteCompilationViolation, match="客户需求副本"):
        compile_quote_ir(
            requirements=[requirement],
            selections=[selection],
            usage_lines=[],
            priced_lines=[],
            total_cost=0,
            is_partial=True,
        )


def test_quote_compiler_rejects_customer_quantity_rewritten_by_selection() -> None:
    requirement = _requirement(component_key="component_redis_01", memory_gib=16)
    requirement.quantity = 3
    requirement.field_sources["quantity"] = "customer_text"
    requirement.field_evidence["quantity"] = "3个节点"
    finalize_customer_fact_ledger(requirement)
    fact_id = next(
        fact.fact_id
        for fact in requirement.customer_pricing_facts
        if fact.path == "requirements.memory_gib"
    )
    selection = _selection(requirement, component_id="0", fact_id=fact_id)
    selection.quantity = 2

    with pytest.raises(QuoteCompilationViolation, match="客户数量"):
        compile_quote_ir(
            requirements=[requirement],
            selections=[selection],
            usage_lines=[],
            priced_lines=[],
            total_cost=0,
            is_partial=True,
        )


def test_quote_compiler_requires_partial_status_for_unpriced_resource() -> None:
    requirement = _requirement(component_key="component_redis_01", memory_gib=16)
    fact_id = requirement.customer_pricing_facts[0].fact_id
    selection = _selection(requirement, component_id="0", fact_id=fact_id)
    selection.pricing_status = "unpriced"

    with pytest.raises(QuoteCompilationViolation, match="未核价资源却未标记为部分报价"):
        compile_quote_ir(
            requirements=[requirement],
            selections=[selection],
            usage_lines=[],
            priced_lines=[],
            total_cost=0,
            is_partial=False,
        )


def test_quote_compiler_allows_unpriced_usage_only_for_partial_quote() -> None:
    requirement = _requirement(component_key="component_redis_01", memory_gib=16)
    fact_id = requirement.customer_pricing_facts[0].fact_id
    selection = _selection(requirement, component_id="0", fact_id=fact_id)
    usage = _usage(fact_id=fact_id)

    with pytest.raises(QuoteCompilationViolation, match="未核价用量"):
        compile_quote_ir(
            requirements=[requirement],
            selections=[selection],
            usage_lines=[usage],
            priced_lines=[],
            total_cost=0,
            is_partial=False,
        )

    compilation = compile_quote_ir(
        requirements=[requirement],
        selections=[selection],
        usage_lines=[usage],
        priced_lines=[],
        total_cost=0,
        is_partial=True,
    )
    assert compilation.is_partial is True


def test_pricing_boundary_removes_prose_but_keeps_structured_facts() -> None:
    requirement = _requirement(component_key="component_redis_01", memory_gib=16)

    pricing_copy = QuoteService._pricing_requirement_copy(
        requirement,
        service_key="redis",
        requirements=dict(requirement.requirements),
    )

    assert pricing_copy.source_text == ""
    assert pricing_copy.original_source_text is None
    assert pricing_copy.requirements["memory_gib"] == 16
    assert pricing_copy.customer_pricing_facts == requirement.customer_pricing_facts


def test_aws_pricing_plugins_cannot_reopen_customer_prose() -> None:
    plugin_directory = Path(__file__).parents[1] / "app" / "services" / "plugins"
    violations: list[str] = []
    for path in sorted(plugin_directory.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in {
                "source_text",
                "original_source_text",
            }:
                violations.append(f"{path.name}:{node.lineno}:{node.attr}")

    assert violations == []
