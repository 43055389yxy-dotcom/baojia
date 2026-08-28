from __future__ import annotations

import json
import re
from typing import Any

from app.domain.customer_facts import CUSTOMER_FACT_SOURCES, infer_field_scope
from app.domain.models import SelectedResource, ServiceRequirement, UnmappedPricingFact

_UNIT_PATTERN = re.compile(
    r"(?<![a-z])(?:tib|tb|gib|gb|mib|mb|kib|kb|kpu|dpu|vcpu|核|"
    r"毫秒|ms|秒|分钟|小时|天|次|条|个|台|节点|broker|端点)(?![a-z])",
    re.IGNORECASE,
)


def _fact_identity(fact: UnmappedPricingFact) -> tuple[str, str, str, str]:
    return (
        re.sub(r"\s+", "", fact.field_hint).casefold(),
        json.dumps(fact.value, ensure_ascii=False, sort_keys=True, default=str),
        str(fact.unit or "").strip().casefold(),
        re.sub(r"\s+", "", fact.evidence).casefold(),
    )


def infer_fact_unit(evidence: str) -> str | None:
    match = _UNIT_PATTERN.search(str(evidence or ""))
    return match.group(0) if match else None


def unmapped_fact_from_field(
    *,
    field: str,
    value: Any,
    evidence: str,
) -> UnmappedPricingFact:
    """Conserve an AI-understood value that a current template cannot name."""

    return UnmappedPricingFact(
        field_hint=field,
        value=value,
        unit=infer_fact_unit(evidence),
        scope=infer_field_scope(evidence),
        evidence=evidence,
    )


def merge_unmapped_pricing_facts(
    target: ServiceRequirement,
    source: ServiceRequirement,
) -> None:
    """Merge the lossless overflow without duplicating the same customer fact."""

    existing = {_fact_identity(item) for item in target.unmapped_pricing_facts}
    for fact in source.unmapped_pricing_facts:
        identity = _fact_identity(fact)
        if identity in existing:
            continue
        target.unmapped_pricing_facts.append(fact.model_copy(deep=True))
        existing.add(identity)


def remove_facts_mapped_to_fields(requirement: ServiceRequirement) -> None:
    """Drop overflow entries only after the same evidenced value is mapped.

    Field names alone are not enough: an AI repair may reuse a vague hint for
    another value.  Requiring matching evidence and value prevents a newly
    captured customer number from being accidentally cleared.
    """

    mapped: set[tuple[str, str]] = set()
    for field, value in requirement.requirements.items():
        path = f"requirements.{field}"
        evidence = str(requirement.field_evidence.get(path) or "").strip()
        if not evidence:
            continue
        mapped.add(
            (
                json.dumps(value, ensure_ascii=False, sort_keys=True, default=str),
                re.sub(r"\s+", "", evidence).casefold(),
            )
        )
    requirement.unmapped_pricing_facts = [
        fact
        for fact in requirement.unmapped_pricing_facts
        if (
            json.dumps(fact.value, ensure_ascii=False, sort_keys=True, default=str),
            re.sub(r"\s+", "", fact.evidence).casefold(),
        )
        not in mapped
    ]


def _value_at(requirement: ServiceRequirement, path: str) -> Any:
    if path == "quantity":
        return requirement.quantity
    if path == "hours_per_month":
        return requirement.hours_per_month
    if path.startswith("requirements."):
        return requirement.requirements.get(path.split(".", 1)[1])
    return None


def _contains_number(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, list):
        return any(_contains_number(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_number(item) for item in value.values())
    return False


def explicit_numeric_fact_paths(requirement: ServiceRequirement) -> set[str]:
    """Return customer-owned numeric paths that a quote must account for."""

    result: set[str] = set()
    for path, source in requirement.field_sources.items():
        if source not in CUSTOMER_FACT_SOURCES:
            continue
        evidence = str(requirement.field_evidence.get(path) or "").strip()
        if not evidence or evidence in {"system_minimum", "system_derived"}:
            continue
        if _contains_number(_value_at(requirement, path)):
            result.add(path)
    return result


def unconsumed_customer_pricing_facts(
    requirement: ServiceRequirement,
    selection: SelectedResource,
) -> list[str]:
    """Find explicit customer numbers that reached neither selection nor price.

    Product selection and direct billing are intentionally separate outcomes:
    CPU/memory may select an instance without becoming a usage amount, while
    storage or requests normally become a billing line.  Both must be declared
    explicitly by the adapter; merely copying a value into display metadata is
    not evidence that the quote used it.
    """

    consumed = set(selection.applied_requirement_fields)
    for line in selection.usage_lines:
        consumed.update(line.source_fields)

    normalized_consumed = {
        field if field in {"quantity", "hours_per_month"} or field.startswith("requirements.")
        else f"requirements.{field}"
        for field in consumed
    }
    return sorted(explicit_numeric_fact_paths(requirement) - normalized_consumed)


def unresolved_fact_messages(requirement: ServiceRequirement) -> list[str]:
    return [
        f"{fact.evidence}（尚未确定对应的报价项目）"
        for fact in requirement.unmapped_pricing_facts
    ]
