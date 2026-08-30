from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.domain.customer_facts import CUSTOMER_FACT_SOURCES, infer_field_scope
from app.domain.models import (
    CustomerPricingFact,
    FactConsumption,
    SelectedResource,
    ServiceRequirement,
    UnmappedPricingFact,
)

_UNIT_PATTERN = re.compile(
    r"(?<![a-z])(?:tib|tb|gib|gb|mib|mb|kib|kb|kpu|dpu|vcpu|核|"
    r"毫秒|ms|秒|分钟|小时|天|次|条|个|台|节点|broker|端点)(?![a-z])",
    re.IGNORECASE,
)

# Product-neutral number inventory.  This deliberately knows only lexical
# units, not AWS service semantics.  It is the conservation boundary used to
# prove that every explicit customer quantity reached either a typed field or
# the lossless overflow table.  Product names/models are excluded by the left
# boundary (EC2, S3, r6g.4xlarge, ap-south-1 must not become fake quantities).
_QUANTITATIVE_ATOM_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._-])"
    r"(?P<number>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<magnitude>万|亿)?\s*"
    r"(?P<unit>"
    r"(?:mi?b|mb)\s*/\s*s(?:\s*/\s*tib)?|"
    r"tib|tb|gib|gb|mib|mb|kib|kb|毫秒|ms|t|g|m|"
    r"v\s*cpu|vcpu|核|iops|rps|qps|%|％|"
    r"秒|分钟|小时|天|年|"
    r"requests?|请求|调用|次|条|封|"
    r"nodes?|节点|shards?|tasks?|brokers?|"
    r"台|套|个|项"
    r")",
    re.IGNORECASE,
)

FACT_LEDGER_FINGERPRINT_FIELD = "_customer_fact_ledger_fingerprint"
SOURCE_BLOCK_KEY_FIELD = "_source_block_key"
OWNED_SOURCE_SLICE_FIELD = "_owned_source_slice"
OWNED_SOURCE_SLICE_EVIDENCE_FIELD = "_owned_source_slice_text"
# Increment whenever the one-pass extraction contract changes.  Persisted
# drafts whose materialized ledger was produced by an older contract must be
# reopened once, otherwise an internally self-consistent but incomplete table
# can survive forever (for example ``OpenSearch，5台`` captured before the
# component had been mapped to the OpenSearch template).
FACT_LEDGER_SCHEMA_VERSION = 7


def customer_owned_source(requirement: ServiceRequirement) -> str:
    """Return the customer prose slice whose facts this component owns."""

    if requirement.field_sources.get(OWNED_SOURCE_SLICE_FIELD) == "system_policy":
        owned = str(
            requirement.field_evidence.get(OWNED_SOURCE_SLICE_EVIDENCE_FIELD) or ""
        ).strip()
        if owned:
            return owned
        return requirement.source_text
    source = str(requirement.source_text or "").strip()
    original = str(requirement.original_source_text or "").strip()
    if source and original:
        compact_source = re.sub(r"\s+", "", source).casefold()
        compact_original = re.sub(r"\s+", "", original).casefold()
        # Backward compatibility for drafts created before the explicit
        # ownership marker existed: compound parent/child rows already stored
        # the component-owned clause in ``source_text`` and the complete sales
        # row in ``original_source_text``.  A strict literal subset is safe to
        # reuse; unrelated rewrites still fall back to the immutable original.
        if compact_source != compact_original and compact_source in compact_original:
            return source
    return original or source


@dataclass(frozen=True, slots=True)
class CustomerPricingFactRecord:
    """One immutable, component-owned customer pricing fact.

    This is the shared contract consumed by validation and pricing.  Product
    adapters still receive the compact ``ServiceRequirement`` object, but no
    later stage needs to reinterpret prose to discover what a number means.
    """

    fact_id: str
    component_key: str
    source_block_key: str | None
    path: str
    value: Any
    unit: str | None
    scope: str
    evidence: str
    source_kind: str


@dataclass(frozen=True, slots=True)
class CustomerQuantitativeAtom:
    """One literal number/unit pair found before product interpretation."""

    raw: str
    value: float
    unit: str
    start: int
    end: int


def customer_quantitative_atoms(text: str) -> list[CustomerQuantitativeAtom]:
    """Inventory all explicit number/unit pairs without guessing their field.

    The extractor is intentionally stable across products and phrasing.  AI
    owns semantic mapping; this function only guarantees that a number cannot
    disappear because a new service or Chinese verb was not in a hand-written
    product rule.
    """

    atoms: list[CustomerQuantitativeAtom] = []
    for match in _QUANTITATIVE_ATOM_PATTERN.finditer(text or ""):
        raw_number = match.group("number").replace(",", "")
        unit = re.sub(r"\s+", "", match.group("unit")).casefold()
        magnitude = match.group("magnitude") or ""
        value = float(raw_number)
        value *= {"万": 10_000, "亿": 100_000_000}.get(magnitude, 1)
        if unit in {"t", "tb", "tib"}:
            value *= 1024
        elif unit in {"m", "mb", "mib"}:
            value /= 1024
        # A four-digit calendar year is deployment context, not an AWS usage
        # amount.  One/three-year reservation terms remain valid atoms.
        if unit == "年" and 1900 <= value <= 2100:
            continue
        atoms.append(
            CustomerQuantitativeAtom(
                raw=match.group(0).strip(),
                value=value,
                unit=unit,
                start=match.start(),
                end=match.end(),
            )
        )
    return atoms


def _fact_id(
    *,
    component_key: str,
    source_block_key: str | None,
    path: str,
    value: Any,
    evidence: str,
) -> str:
    payload = json.dumps(
        {
            "component_key": component_key,
            "source_block_key": source_block_key,
            "path": path,
            "value": value,
            "evidence": re.sub(r"\s+", "", evidence).casefold(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "fact_" + hashlib.sha256(payload).hexdigest()[:24]


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


def infer_field_fact_unit(path: str, evidence: str) -> str | None:
    """Infer the unit for one semantic field, not merely the first token."""

    field = path.removeprefix("requirements.").casefold()
    if field in {"vcpu", "cpu", "cores"} or field.endswith("_vcpu"):
        return "vCPU"
    if any(marker in field for marker in ("memory", "storage", "disk", "capacity")):
        size_units = re.findall(
            r"(?<![a-z])(tib|tb|t|gib|gb|g|mib|mb|m)(?![a-z])",
            evidence,
            re.IGNORECASE,
        )
        if size_units:
            return size_units[-1]
    return infer_fact_unit(evidence)


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

    mapped: list[tuple[str, str]] = []
    for field, value in requirement.requirements.items():
        path = f"requirements.{field}"
        evidence = str(requirement.field_evidence.get(path) or "").strip()
        if not evidence:
            continue
        mapped.append(
            (
                json.dumps(value, ensure_ascii=False, sort_keys=True, default=str),
                re.sub(r"\s+", "", evidence).casefold(),
            )
        )
    # Top-level quantity and hours are first-class fact-table columns too.
    # Previously an AI could correctly map ``1套`` to quantity while the same
    # value remained in the lossless overflow table and blocked the final
    # quote as "unmapped".
    for path, value in (
        ("quantity", requirement.quantity),
        ("hours_per_month", requirement.hours_per_month),
    ):
        evidence = str(requirement.field_evidence.get(path) or "").strip()
        if not evidence:
            continue
        mapped.append(
            (
                json.dumps(value, ensure_ascii=False, sort_keys=True, default=str),
                re.sub(r"\s+", "", evidence).casefold(),
            )
        )

    def is_mapped(fact: UnmappedPricingFact) -> bool:
        fact_value = json.dumps(
            fact.value, ensure_ascii=False, sort_keys=True, default=str
        )
        fact_evidence = re.sub(r"\s+", "", fact.evidence).casefold()
        return any(
            fact_value == mapped_value
            and bool(fact_evidence and mapped_evidence)
            and (
                fact_evidence == mapped_evidence
                or fact_evidence in mapped_evidence
                or mapped_evidence in fact_evidence
            )
            for mapped_value, mapped_evidence in mapped
        )

    requirement.unmapped_pricing_facts = [
        fact
        for fact in requirement.unmapped_pricing_facts
        if not is_mapped(fact)
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


def customer_pricing_fact_records(
    requirement: ServiceRequirement,
) -> list[CustomerPricingFactRecord]:
    """Materialize the single structured fact table for one component."""

    component_key = str(requirement.component_key or "")
    source_block_key = requirement.field_sources.get(SOURCE_BLOCK_KEY_FIELD)
    result: list[CustomerPricingFactRecord] = []
    for path in sorted(explicit_numeric_fact_paths(requirement)):
        evidence = str(requirement.field_evidence.get(path) or "").strip()
        field = path.removeprefix("requirements.")
        source_kind = str(requirement.field_sources.get(path) or "customer_text")
        result.append(
            CustomerPricingFactRecord(
                fact_id=_fact_id(
                    component_key=component_key,
                    source_block_key=source_block_key,
                    path=path,
                    value=_value_at(requirement, path),
                    evidence=evidence,
                ),
                component_key=component_key,
                source_block_key=source_block_key,
                path=path,
                value=_value_at(requirement, path),
                unit=infer_field_fact_unit(path, evidence),
                scope=requirement.field_scopes.get(
                    field,
                    "component_total" if path in {"quantity", "hours_per_month"}
                    else infer_field_scope(evidence),
                ),
                evidence=evidence,
                source_kind=source_kind,
            )
        )
    # Choosing an official instance model is an explicit replacement decision,
    # not a rewrite of what the customer originally asked for.  The component
    # keeps the selected model's official CPU/memory for display, while the
    # immutable fact ledger must keep the original requested shape as the fact
    # that led to that decision.  Recomputing these two rows from the mutable
    # display fields used to discard the original values and made the final
    # literal audit report perfectly valid requests such as ``4核16G`` as lost.
    #
    # This rule is deliberately product-neutral: EC2, RDS, ElastiCache, MSK,
    # OpenSearch and every future model-sized service use the same marker and
    # therefore the same conservation boundary.
    if (
        requirement.field_sources.get("_customer_shape_replaced_by_model")
        == "customer_confirmation"
        and requirement.requirements.get("requested_model")
        and requirement.customer_pricing_facts
    ):
        shape_paths = {"requirements.vcpu", "requirements.memory_gib"}
        preserved_shape = [
            CustomerPricingFactRecord(
                fact_id=fact.fact_id,
                component_key=fact.component_key,
                source_block_key=fact.source_block_key,
                path=fact.path,
                value=fact.value,
                unit=fact.unit,
                scope=fact.scope,
                evidence=fact.evidence,
                source_kind=fact.source_kind,
            )
            for fact in requirement.customer_pricing_facts
            if fact.path in shape_paths and fact.source_kind == "customer_text"
        ]
        if preserved_shape:
            preserved_paths = {record.path for record in preserved_shape}
            result = [record for record in result if record.path not in preserved_paths]
            result.extend(preserved_shape)
            result.sort(key=lambda record: record.path)
    return result


def customer_fact_ledger_fingerprint(requirement: ServiceRequirement) -> str:
    """Fingerprint only customer-owned facts, not pricing/default metadata."""

    records = customer_pricing_fact_records(requirement)
    payload = {
        "component_key": requirement.component_key,
        "parent_component_key": requirement.parent_component_key,
        "derived_from_service": requirement.derived_from_service,
        "service": requirement.service,
        "owned_source": customer_owned_source(requirement),
        "facts": [
            {
                "path": item.path,
                "value": item.value,
                "unit": item.unit,
                "scope": item.scope,
                "evidence": item.evidence,
                "source_kind": item.source_kind,
            }
            for item in records
        ],
        "unmapped": [fact.model_dump(mode="json") for fact in requirement.unmapped_pricing_facts],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def customer_fact_ledger_is_current(requirement: ServiceRequirement) -> bool:
    if requirement.fact_ledger_version != FACT_LEDGER_SCHEMA_VERSION:
        return False
    if requirement.field_sources.get(FACT_LEDGER_FINGERPRINT_FIELD) != (
        customer_fact_ledger_fingerprint(requirement)
    ):
        return False
    expected = [
        CustomerPricingFact(
            fact_id=record.fact_id,
            component_key=record.component_key,
            source_block_key=record.source_block_key,
            path=record.path,
            value=record.value,
            unit=record.unit,
            scope=record.scope,
            evidence=record.evidence,
            source_kind=record.source_kind,
        )
        for record in customer_pricing_fact_records(requirement)
    ]
    return requirement.customer_pricing_facts == expected


def finalize_customer_fact_ledger(requirement: ServiceRequirement) -> None:
    # Derived values are deterministic arithmetic results, not customer-owned
    # facts.  Normalize this centrally so an AI/template implementation cannot
    # accidentally lock a derived total/count and later make the quote ledger
    # treat it as a second customer number.
    derived_paths = {
        path
        for path, evidence in requirement.field_evidence.items()
        if evidence == "system_derived"
    } | {
        path
        for path, source in requirement.field_sources.items()
        if source == "system_derived"
    }
    if derived_paths:
        locked = set(requirement.locked_fields)
        for path in derived_paths:
            requirement.field_sources[path] = "system_derived"
            locked.discard(path)
        requirement.locked_fields = sorted(locked)
    records = customer_pricing_fact_records(requirement)
    requirement.fact_ledger_version = FACT_LEDGER_SCHEMA_VERSION
    requirement.customer_pricing_facts = [
        CustomerPricingFact(
            fact_id=record.fact_id,
            component_key=record.component_key,
            source_block_key=record.source_block_key,
            path=record.path,
            value=record.value,
            unit=record.unit,
            scope=record.scope,
            evidence=record.evidence,
            source_kind=record.source_kind,
        )
        for record in records
    ]
    requirement.field_sources[FACT_LEDGER_FINGERPRINT_FIELD] = (
        customer_fact_ledger_fingerprint(requirement)
    )


def duplicate_customer_fact_ownership(
    requirements: list[ServiceRequirement],
) -> list[list[CustomerPricingFactRecord]]:
    """Find one source-owned fact assigned to multiple sibling components.

    ``source_block_key`` distinguishes two intentionally identical numbered
    rows while still exposing duplicates created from the same customer row.
    Different semantic paths in the same evidence (for example CPU and memory
    in ``8核16G``) remain independent facts.
    """

    grouped: dict[tuple[str, str, str, str], list[CustomerPricingFactRecord]] = {}
    for requirement in requirements:
        for record in customer_pricing_fact_records(requirement):
            if not record.source_block_key:
                continue
            identity = (
                record.source_block_key,
                record.path,
                json.dumps(record.value, ensure_ascii=False, sort_keys=True, default=str),
                re.sub(r"\s+", "", record.evidence).casefold(),
            )
            grouped.setdefault(identity, []).append(record)
    return [
        records
        for records in grouped.values()
        if len({record.component_key for record in records}) > 1
    ]


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

    consumed = {item.path for item in fact_consumptions(requirement, selection)}
    return sorted(explicit_numeric_fact_paths(requirement) - consumed)


def fact_consumptions(
    requirement: ServiceRequirement,
    selection: SelectedResource,
) -> list[FactConsumption]:
    """Bind every declared downstream use to a stable fact identifier.

    Multiple records for one fact are valid: a node count can constrain an
    official shape and also multiply compute/storage usage.  Missing records,
    not repeated justified records, are the fail-closed condition.
    """

    records = customer_pricing_fact_records(requirement)
    by_path = {record.path: record for record in records}

    def normalize(field: str) -> str:
        if field in {"quantity", "hours_per_month"} or field.startswith(
            "requirements."
        ):
            return field
        return f"requirements.{field}"

    result: list[FactConsumption] = []
    seen: set[tuple[str, str, str]] = set()

    def add(path: str, consumer_type: str, consumer_key: str, purpose: str) -> None:
        record = by_path.get(path)
        if record is None:
            return
        identity = (record.fact_id, consumer_type, consumer_key)
        if identity in seen:
            return
        seen.add(identity)
        result.append(
            FactConsumption(
                fact_id=record.fact_id,
                path=path,
                consumer_type=consumer_type,
                consumer_key=consumer_key,
                purpose=purpose,
            )
        )

    usage_paths = {
        normalize(field)
        for line in selection.usage_lines
        for field in line.source_fields
    }
    for field in selection.applied_requirement_fields:
        path = normalize(field)
        add(
            path,
            "non_billable_context" if path not in usage_paths else "selection",
            selection.model or selection.service,
            (
                "保留为客户迁移/配置上下文，不作为独立收费项"
                if path not in usage_paths
                else "用于 AWS 官方产品、型号或配置匹配"
            ),
        )
    for line in selection.usage_lines:
        for field in line.source_fields:
            add(
                normalize(field),
                "usage_line",
                line.key,
                f"用于官方计费维度 {line.usage_type or line.service_code}",
            )
    return result


def bind_fact_consumptions(
    requirement: ServiceRequirement,
    selection: SelectedResource,
) -> None:
    records_by_path = {
        record.path: record
        for record in customer_pricing_fact_records(requirement)
    }

    def normalize(field: str) -> str:
        if field in {"quantity", "hours_per_month"} or field.startswith(
            "requirements."
        ):
            return field
        return f"requirements.{field}"

    # Rebuild IDs from semantic fields every time.  This intentionally ignores
    # any persisted/adapter-provided IDs so stale cache data cannot bind a
    # charge to a neighboring component or an earlier quote run.
    for line in selection.usage_lines:
        line.source_fact_ids = sorted(
            {
                record.fact_id
                for field in line.source_fields
                if (record := records_by_path.get(normalize(field))) is not None
            }
        )
    selection.fact_consumptions = fact_consumptions(requirement, selection)


def selection_fact_contract_violations(
    requirement: ServiceRequirement,
    selection: SelectedResource,
) -> dict[str, list[str]]:
    """Validate the product-neutral boundary shared by every adapter.

    The function also performs the authoritative fact-ID binding.  Therefore
    preview and final pricing can call one contract instead of maintaining two
    subtly different checks.
    """

    bind_fact_consumptions(requirement, selection)
    violations: dict[str, list[str]] = {}
    missing = unconsumed_customer_pricing_facts(requirement, selection)
    if missing:
        violations["unconsumed_customer_pricing_facts"] = missing

    # A source marked as an arithmetic derivation must never remain locked as
    # customer input.  This catches malformed legacy/cache payloads even before
    # their ledger is rematerialized.
    locked = set(requirement.locked_fields)
    invalid_derived = sorted(
        path
        for path, source in requirement.field_sources.items()
        if source == "system_derived" and path in locked
    )
    if invalid_derived:
        violations["locked_system_derived_facts"] = invalid_derived

    return violations


def unresolved_fact_messages(requirement: ServiceRequirement) -> list[str]:
    return [
        f"{fact.evidence}（尚未确定对应的报价项目）"
        for fact in requirement.unmapped_pricing_facts
    ]
