from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.fact_ledger import customer_pricing_fact_records
from app.domain.models import (
    BillingCalculationAudit,
    PricedLine,
    SelectedResource,
    ServiceRequirement,
    UsageLine,
)

QUOTE_COMPILER_IR_VERSION = "2026-09-05.1"


class RequirementFactIR(BaseModel):
    """One customer-owned fact after the only natural-language pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str
    path: str
    value: Any
    unit: str | None = None
    scope: str
    source_kind: str
    evidence: str
    match_policy: Literal["exact", "approximate", "minimum"] | None = None


class RequirementIR(BaseModel):
    """What the customer requested; contains no AWS product attributes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component_id: str
    component_key: str
    parent_component_key: str | None = None
    service_intent: str
    product_identity: str | None = None
    region: str | None = None
    facts: tuple[RequirementFactIR, ...] = ()
    evidence_fingerprint: str

    @model_validator(mode="after")
    def facts_are_unique(self) -> RequirementIR:
        ids = [fact.fact_id for fact in self.facts]
        if len(ids) != len(set(ids)):
            raise ValueError("requirement IR contains duplicate fact IDs")
        return self


class ResourceIR(BaseModel):
    """The selected AWS resource; customer values stay on RequirementIR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component_id: str
    parent_component_id: str | None = None
    service: str
    region: str
    model: str
    quantity: int
    pricing_status: str
    requested_specifications: dict[str, Any] = Field(default_factory=dict)
    official_specifications: dict[str, Any] = Field(default_factory=dict)
    official_product: dict[str, Any] = Field(default_factory=dict)
    consumed_fact_ids: tuple[str, ...] = ()


class BillingUsageIR(BaseModel):
    """One official AWS billable quantity submitted to the pricing oracle."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component_id: str
    key: str
    group: str | None = None
    service_code: str
    usage_type: str
    operation: str
    amount: float = Field(gt=0)
    source_fact_ids: tuple[str, ...] = ()
    calculation: BillingCalculationAudit | None = None


class PriceIR(BaseModel):
    """The official priced result; it cannot mutate any earlier IR."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    service_code: str
    usage_type: str
    operation: str
    amount: float
    unit: str | None = None
    cost: float = Field(ge=0)
    currency: str
    source_usage_key: str | None = None


class QuoteCompilation(BaseModel):
    """Immutable four-stage compiler output used for final trust checks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = QUOTE_COMPILER_IR_VERSION
    requirements: tuple[RequirementIR, ...]
    resources: tuple[ResourceIR, ...]
    usage: tuple[BillingUsageIR, ...]
    prices: tuple[PriceIR, ...]
    total_cost: float = Field(ge=0)
    is_partial: bool = False


class QuoteCompilationViolation(ValueError):
    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _requirement_ir(
    requirement: ServiceRequirement,
    component_id: str,
) -> RequirementIR:
    facts = []
    for record in customer_pricing_fact_records(requirement):
        field = record.path.removeprefix("requirements.")
        facts.append(
            RequirementFactIR(
                fact_id=record.fact_id,
                path=record.path,
                value=record.value,
                unit=record.unit,
                scope=record.scope,
                source_kind=record.source_kind,
                evidence=record.evidence,
                match_policy=requirement.field_match_policies.get(field),
            )
        )
    evidence_fingerprint = _canonical_hash(
        [
            {
                "fact_id": fact.fact_id,
                "path": fact.path,
                "value": fact.value,
                "evidence": fact.evidence,
            }
            for fact in facts
        ]
    )
    return RequirementIR(
        component_id=component_id,
        component_key=requirement.component_key or f"component-{component_id}",
        parent_component_key=requirement.parent_component_key,
        service_intent=requirement.service,
        product_identity=requirement.product_identity,
        region=requirement.region,
        facts=tuple(facts),
        evidence_fingerprint=evidence_fingerprint,
    )


def _resource_ir(selection: SelectedResource) -> ResourceIR:
    return ResourceIR(
        component_id=selection.component_id or "",
        parent_component_id=selection.parent_component_id,
        service=selection.service,
        region=selection.region,
        model=selection.model,
        quantity=selection.quantity,
        pricing_status=selection.pricing_status,
        requested_specifications=dict(selection.requested_specifications),
        official_specifications=dict(selection.official_specifications),
        official_product=dict(selection.official_product),
        consumed_fact_ids=tuple(sorted({item.fact_id for item in selection.fact_consumptions})),
    )


def _component_id_for_usage(line: UsageLine) -> str:
    group = str(line.group or "")
    if group.startswith("service-") and group.removeprefix("service-").isdigit():
        return str(int(group.removeprefix("service-")) - 1)
    return ""


def _billing_ir(line: UsageLine) -> BillingUsageIR:
    return BillingUsageIR(
        component_id=_component_id_for_usage(line),
        key=line.key,
        group=line.group,
        service_code=line.service_code,
        usage_type=line.usage_type,
        operation=line.operation,
        amount=line.amount,
        source_fact_ids=tuple(sorted(set(line.source_fact_ids))),
        calculation=line.calculation,
    )


def _price_ir(line: PricedLine, usage_by_key: dict[str, BillingUsageIR]) -> PriceIR:
    usage = usage_by_key.get(line.key)
    return PriceIR(
        key=line.key,
        service_code=line.service_code,
        usage_type=line.usage_type,
        operation=line.operation,
        amount=line.amount,
        unit=line.unit,
        cost=line.cost,
        currency=line.currency,
        source_usage_key=usage.key if usage is not None else None,
    )


def compile_quote_ir(
    *,
    requirements: list[ServiceRequirement],
    selections: list[SelectedResource],
    usage_lines: list[UsageLine],
    priced_lines: list[PricedLine],
    total_cost: float,
    is_partial: bool,
) -> QuoteCompilation:
    """Build and verify the four isolated quote stages.

    This function is intentionally product-neutral.  Service adapters declare
    official usage; the compiler only proves identity, lineage and arithmetic
    continuity between stages.
    """

    requirement_irs = tuple(
        _requirement_ir(requirement, str(index)) for index, requirement in enumerate(requirements)
    )
    resource_irs = tuple(_resource_ir(selection) for selection in selections)
    billing_irs = tuple(_billing_ir(line) for line in usage_lines)
    usage_by_key = {line.key: line for line in billing_irs}
    price_irs = tuple(_price_ir(line, usage_by_key) for line in priced_lines)
    compilation = QuoteCompilation(
        requirements=requirement_irs,
        resources=resource_irs,
        usage=billing_irs,
        prices=price_irs,
        total_cost=total_cost,
        is_partial=is_partial,
    )
    validate_quote_ir(compilation)
    return compilation


def validate_quote_ir(compilation: QuoteCompilation) -> None:
    violations: list[str] = []
    requirement_by_component = {item.component_id: item for item in compilation.requirements}
    resource_by_component = {item.component_id: item for item in compilation.resources}
    requirement_id_by_key = {
        item.component_key: item.component_id for item in compilation.requirements
    }

    if len(requirement_by_component) != len(compilation.requirements):
        violations.append("需求层存在重复组件身份")
    if len(resource_by_component) != len(compilation.resources):
        violations.append("资源层存在重复组件身份")
    component_keys = [item.component_key for item in compilation.requirements]
    duplicate_component_keys = sorted(
        key for key, count in Counter(component_keys).items() if count > 1
    )
    if duplicate_component_keys:
        violations.append(f"需求层存在重复组件键 {duplicate_component_keys}")
    if any(not key.strip() for key in component_keys):
        violations.append("需求层存在空组件键")
    missing_resource_components = sorted(set(requirement_by_component) - set(resource_by_component))
    if missing_resource_components:
        violations.append(f"需求组件 {missing_resource_components} 没有生成资源结果")
    for requirement in compilation.requirements:
        parent_key = requirement.parent_component_key
        if parent_key and parent_key not in requirement_id_by_key:
            violations.append(
                f"需求组件 {requirement.component_id} 引用了不存在的父组件 {parent_key}"
            )

    parent_by_key = {
        requirement.component_key: requirement.parent_component_key
        for requirement in compilation.requirements
        if requirement.parent_component_key
    }
    for start in parent_by_key:
        seen: set[str] = set()
        current: str | None = start
        while current is not None:
            if current in seen:
                violations.append(f"父子组件关系存在环，涉及组件 {start}")
                break
            seen.add(current)
            current = parent_by_key.get(current)
    for resource in compilation.resources:
        requirement = requirement_by_component.get(resource.component_id)
        expected_parent_id = (
            requirement_id_by_key.get(requirement.parent_component_key)
            if requirement is not None and requirement.parent_component_key
            else None
        )
        if resource.parent_component_id != expected_parent_id:
            violations.append(f"资源 {resource.component_id} 的父组件关系与需求层不一致")

    known_fact_ids = {
        fact.fact_id for requirement in compilation.requirements for fact in requirement.facts
    }
    fact_owner = {
        fact.fact_id: requirement.component_id
        for requirement in compilation.requirements
        for fact in requirement.facts
    }
    all_fact_ids = [
        fact.fact_id for requirement in compilation.requirements for fact in requirement.facts
    ]
    duplicate_fact_ids = sorted(
        fact_id for fact_id, count in Counter(all_fact_ids).items() if count > 1
    )
    if duplicate_fact_ids:
        violations.append(f"需求层存在重复客户事实身份 {duplicate_fact_ids}")
    for resource in compilation.resources:
        if resource.component_id not in requirement_by_component:
            violations.append(f"资源 {resource.component_id} 没有对应需求组件")
        unknown = set(resource.consumed_fact_ids) - known_fact_ids
        if unknown:
            violations.append(f"资源 {resource.component_id} 使用了未知客户事实 {sorted(unknown)}")
        wrong_owner = sorted(
            fact_id
            for fact_id in resource.consumed_fact_ids
            if fact_owner.get(fact_id) not in {None, resource.component_id}
        )
        if wrong_owner:
            violations.append(f"资源 {resource.component_id} 跨组件使用客户事实 {wrong_owner}")
        requirement = requirement_by_component.get(resource.component_id)
        if requirement is not None:
            for fact in requirement.facts:
                if fact.path == "quantity":
                    if _canonical_hash(resource.quantity) != _canonical_hash(fact.value):
                        violations.append(f"资源 {resource.component_id} 改写了客户数量 quantity")
                    continue
                field = fact.path.removeprefix("requirements.")
                if not fact.path.startswith("requirements."):
                    continue
                if field not in resource.requested_specifications:
                    continue
                copied_value = resource.requested_specifications[field]
                if _canonical_hash(copied_value) != _canonical_hash(fact.value):
                    violations.append(
                        f"资源 {resource.component_id} 的客户需求副本改写了 {fact.path}"
                    )

    usage_keys = [line.key for line in compilation.usage]
    duplicate_usage_keys = sorted(key for key, count in Counter(usage_keys).items() if count > 1)
    if duplicate_usage_keys:
        violations.append(f"计费用量层存在重复键 {duplicate_usage_keys}")

    executed_meters: set[tuple[str, str, int | None]] = set()
    for usage in compilation.usage:
        if usage.calculation is not None:
            meter_identity = (
                usage.component_id,
                usage.calculation.rule_id,
                usage.calculation.item_index,
            )
            if meter_identity in executed_meters:
                violations.append(
                    f"计费用量 {usage.key} 重复执行计费规则 {usage.calculation.rule_id}"
                )
            executed_meters.add(meter_identity)
            try:
                _validate_usage_calculation(usage, requirement_by_component.get(usage.component_id))
            except (ValueError, TypeError, ArithmeticError, KeyError) as exc:
                violations.append(f"计费用量 {usage.key} 的计算底稿不一致：{exc}")
        if usage.component_id not in requirement_by_component:
            violations.append(f"计费用量 {usage.key} 没有对应需求组件")
        unknown = set(usage.source_fact_ids) - known_fact_ids
        if unknown:
            violations.append(f"计费用量 {usage.key} 引用了未知客户事实 {sorted(unknown)}")
        wrong_owner = sorted(
            fact_id
            for fact_id in usage.source_fact_ids
            if fact_owner.get(fact_id) not in {None, usage.component_id}
        )
        if wrong_owner:
            violations.append(f"计费用量 {usage.key} 跨组件引用客户事实 {wrong_owner}")
        resource = resource_by_component.get(usage.component_id)
        if resource is not None and resource.pricing_status == "free":
            violations.append(f"免费资源 {resource.component_id} 不得生成计费用量 {usage.key}")

    usage_by_key = {line.key: line for line in compilation.usage}
    price_keys = [line.key for line in compilation.prices]
    duplicate_price_keys = sorted(key for key, count in Counter(price_keys).items() if count > 1)
    if duplicate_price_keys:
        violations.append(f"价格层存在重复键 {duplicate_price_keys}")

    for price in compilation.prices:
        usage = usage_by_key.get(price.key)
        if usage is None:
            # Reserved commitments are produced from official Price List terms
            # rather than BCM usage quantities and therefore have no usage row.
            if price.operation != "Reserved":
                violations.append(f"价格行 {price.key} 没有对应计费用量")
            continue
        if (
            price.service_code,
            price.usage_type,
            price.operation,
        ) != (
            usage.service_code,
            usage.usage_type,
            usage.operation,
        ):
            violations.append(f"价格行 {price.key} 与提交的官方计费身份不一致")
        if not math.isclose(price.amount, usage.amount, rel_tol=1e-9, abs_tol=1e-9):
            violations.append(f"价格行 {price.key} 与提交的官方用量不一致")

    expected_total = sum(line.cost for line in compilation.prices)
    if not math.isclose(
        expected_total,
        compilation.total_cost,
        rel_tol=1e-9,
        abs_tol=0.01,
    ):
        violations.append(
            f"价格行合计 {expected_total:.6f} 与报价总计 {compilation.total_cost:.6f} 不一致"
        )

    if not compilation.is_partial:
        unpriced_resources = sorted(
            resource.component_id
            for resource in compilation.resources
            if resource.pricing_status == "unpriced"
        )
        if unpriced_resources:
            violations.append(f"存在未核价资源却未标记为部分报价 {unpriced_resources}")
        consumed_fact_ids = {
            fact_id for resource in compilation.resources for fact_id in resource.consumed_fact_ids
        } | {fact_id for usage in compilation.usage for fact_id in usage.source_fact_ids}
        unconsumed_fact_ids = sorted(known_fact_ids - consumed_fact_ids)
        if unconsumed_fact_ids:
            violations.append(f"完整报价仍有客户事实未被资源或计费用量消费 {unconsumed_fact_ids}")
        priced_usage_keys = {
            line.source_usage_key
            for line in compilation.prices
            if line.source_usage_key is not None
        }
        missing_prices = sorted(set(usage_by_key) - priced_usage_keys)
        if missing_prices:
            violations.append(f"完整报价仍有未核价用量 {missing_prices}")

    if violations:
        raise QuoteCompilationViolation(violations)


def _validate_usage_calculation(usage: BillingUsageIR, requirement: RequirementIR | None) -> None:
    from app.integrations.aws_component_templates.registry import component_template_spec

    audit = usage.calculation
    if audit is None:
        return
    template = component_template_spec(audit.rule_id.partition(".")[0])
    if template is None:
        raise ValueError("unknown executable billing template")
    result = template.replay(audit.model_dump())
    if float(result.amount) != usage.amount:
        raise ValueError("submitted usage differs from rule result")
    if requirement is None:
        return
    expected_template = component_template_spec(requirement.service_intent)
    if expected_template is not None and expected_template.service_key != template.service_key:
        raise ValueError("calculation belongs to a different component service")
    # Compare every recorded numeric input for which RequirementIR owns an
    # explicit customer fact. Defaults stay in their own plane, never facts.
    for fact in requirement.facts:
        path = fact.path.removeprefix("requirements.")
        value = fact.value
        for input_path, input_value in audit.inputs.items():
            candidate = value
            if input_path == path:
                pass
            elif input_path.startswith(path + ".") and isinstance(value, list):
                _, index, key = input_path.split(".", 2)
                try:
                    candidate = value[int(index)][key]
                except (IndexError, KeyError, TypeError) as exc:
                    raise ValueError("disk calculation does not refer to a customer item") from exc
            else:
                continue
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                if Decimal(str(candidate)) != Decimal(input_value):
                    raise ValueError(f"calculation changed customer fact {fact.path}")


def quote_ir_audit_metadata(compilation: QuoteCompilation) -> dict[str, Any]:
    payload = compilation.model_dump(mode="json")
    return {
        "ir_version": compilation.version,
        "ir_fingerprint": _canonical_hash(payload),
        "requirement_component_count": len(compilation.requirements),
        "resource_component_count": len(compilation.resources),
        "billing_usage_line_count": len(compilation.usage),
        "official_price_line_count": len(compilation.prices),
        "executable_usage_line_count": sum(
            line.calculation is not None for line in compilation.usage
        ),
        "billing_rule_versions": sorted(
            {line.calculation.rule_version for line in compilation.usage if line.calculation}
        ),
        "verification_status": "verified",
    }
