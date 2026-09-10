"""Deterministic boundary for ChatGPT-cleaned AWS quote requirements.

This module deliberately contains no model gateway and accepts no customer raw
request.  A caller first obtains trusted, server-owned service contracts, then
submits only isolated cleaned components and their typed facts.  The compiler
proves literal number conservation and ownership before materializing the
existing ``ServiceRequirement`` / ``RequirementIR`` contracts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.domain.cleaned_input import CLEANED_INPUT_POLICY_VERSION, intent_is_cleaned_only
from app.domain.fact_ledger import (
    OWNED_SOURCE_SLICE_EVIDENCE_FIELD,
    OWNED_SOURCE_SLICE_FIELD,
    SOURCE_BLOCK_KEY_FIELD,
    customer_pricing_fact_records,
    customer_quantitative_atoms,
    duplicate_customer_fact_ownership,
    finalize_customer_fact_ledger,
)
from app.domain.models import ParsedIntent, ServiceRequirement
from app.domain.pricing_contracts import apply_pricing_contract
from app.domain.quote_compiler import RequirementFactIR, RequirementIR

STRUCTURED_INTAKE_VERSION = "structured-gpt-v1"

_FORBIDDEN_INPUT_FIELDS = frozenset(
    {
        "customer_request",
        "original_source_text",
        "intake_source_fragments",
        "raw_customer_text",
        "fact_id",
        "customer_pricing_facts",
        "field_sources",
        "locked_fields",
    }
)
_FACT_PATH = re.compile(r"^(?:quantity|hours_per_month|requirements\.[a-z][a-z0-9_]*)$")
_COMPONENT_KEY = re.compile(r"^cmp_[A-Za-z0-9_-]{4,76}$")
_SOURCE_BLOCK_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_SERVICE_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")
_REGION = re.compile(r"^(?:af|ap|ca|cn|eu|il|me|mx|sa|us)(?:-gov)?-[a-z0-9-]+-\d$")

ValueType = Literal["number", "integer", "string", "boolean", "array_object"]
FactScope = Literal["component_total", "aggregate", "per_resource", "per_node"]
MatchPolicy = Literal["exact", "approximate", "minimum"]


class StructuredFieldContract(BaseModel):
    """Trusted server-side definition for one normalized component field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value_type: ValueType
    unit: str | None = Field(default=None, min_length=1, max_length=80)
    allowed_values: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> StructuredFieldContract:
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("minimum cannot exceed maximum")
        if self.allowed_values and self.value_type != "string":
            raise ValueError("allowed_values is supported only for string fields")
        return self


class StructuredMinimumUnitPolicy(BaseModel):
    """Server-owned policy for a service named without measurable usage.

    It creates disclosure metadata only.  It never invents a customer fact or
    a billable usage amount; the owning pricing adapter may return an official
    reference rate, consistent with the existing minimum-unit behavior.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    watched_fields: tuple[str, ...] = Field(min_length=1)
    flag_field: str = Field(
        default="reference_unit_only", pattern=r"^[a-z][a-z0-9_]*$"
    )
    message: str = Field(min_length=1, max_length=500)


class StructuredServiceContract(BaseModel):
    """Versioned field contract supplied by the trusted preparation service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=160)
    contract_id: str = Field(min_length=3, max_length=160)
    schema_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    fields: dict[str, StructuredFieldContract] = Field(default_factory=dict)
    calculator_service_code: str | None = Field(default=None, min_length=1, max_length=240)
    calculator_template_id: str | None = Field(default=None, min_length=1, max_length=240)
    minimum_unit_policy: StructuredMinimumUnitPolicy | None = None

    @model_validator(mode="after")
    def validate_field_names(self) -> StructuredServiceContract:
        invalid = sorted(
            name
            for name in self.fields
            if re.fullmatch(r"[a-z][a-z0-9_]*", name) is None
        )
        if invalid:
            raise ValueError(f"invalid normalized field names: {invalid}")
        policy = self.minimum_unit_policy
        if policy is not None:
            unknown = sorted(set(policy.watched_fields) - set(self.fields))
            if unknown:
                raise ValueError(f"minimum-unit policy references unknown fields: {unknown}")
        return self


class StructuredFactInput(BaseModel):
    """One GPT-proposed field value; identity and provenance are server-owned."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=160, pattern=_FACT_PATH.pattern)
    value: Any
    unit: str | None = Field(default=None, min_length=1, max_length=80)
    scope: FactScope = "component_total"
    match_policy: MatchPolicy = "exact"
    evidence: str = Field(min_length=1, max_length=300)


class StructuredComponentInput(BaseModel):
    """One isolated component after ChatGPT's sole natural-language pass."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    component_key: str = Field(min_length=8, max_length=80, pattern=_COMPONENT_KEY.pattern)
    parent_component_key: str | None = Field(
        default=None, min_length=8, max_length=80, pattern=_COMPONENT_KEY.pattern
    )
    derived_from_service: str | None = Field(
        default=None, min_length=2, max_length=80, pattern=_SERVICE_KEY.pattern
    )
    service: str = Field(min_length=2, max_length=80, pattern=_SERVICE_KEY.pattern)
    product_identity: str | None = Field(
        default=None, min_length=2, max_length=100, pattern=r"^[a-z0-9][a-z0-9_-]+$"
    )
    region: str | None = Field(default=None, max_length=40, pattern=_REGION.pattern)
    contract_id: str = Field(min_length=3, max_length=160)
    contract_schema_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    cleaned_source: str = Field(min_length=1, max_length=12000)
    source_block_key: str = Field(
        min_length=1, max_length=80, pattern=_SOURCE_BLOCK_KEY.pattern
    )
    facts: tuple[StructuredFactInput, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def cleaned_source_is_normalized(self) -> StructuredComponentInput:
        if self.cleaned_source != self.cleaned_source.strip():
            raise ValueError("cleaned_source must not contain outer whitespace")
        return self


class StructuredIntakeSubmission(BaseModel):
    """Closed input schema exposed by the future MCP validation tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    components: tuple[StructuredComponentInput, ...] = Field(min_length=1, max_length=25)


class StructuredIntakeResult(BaseModel):
    """Cleaned-only domain output ready for the ordinary four-layer compiler."""

    model_config = ConfigDict(extra="forbid")

    policy_version: str = CLEANED_INPUT_POLICY_VERSION
    semantic_mapping_version: str = STRUCTURED_INTAKE_VERSION
    intent: ParsedIntent
    requirement_ir: tuple[RequirementIR, ...]


class StructuredIntakeViolation(ValueError):
    """All deterministic failures found before a draft may be persisted."""

    def __init__(self, violations: list[str]):
        self.violations = tuple(dict.fromkeys(violations))
        super().__init__("；".join(self.violations))


@dataclass(frozen=True, slots=True)
class _ValidatedFact:
    component_key: str
    source_block_key: str
    path: str
    value: Any
    unit: str | None
    unit_family: str | None
    evidence: str
    ledger_evidence: str
    evidence_start: int
    matched_source_atom_starts: tuple[int, ...]
    scope: FactScope
    match_policy: MatchPolicy


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _walk_forbidden_fields(value: object, path: str = "input") -> list[str]:
    violations: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            current = f"{path}.{name}"
            if name.casefold() in _FORBIDDEN_INPUT_FIELDS:
                violations.append(f"{current} 禁止提交字段 {name}")
            violations.extend(_walk_forbidden_fields(item, current))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            violations.extend(_walk_forbidden_fields(item, f"{path}[{index}]"))
    return violations


def _validation_error_messages(error: ValidationError) -> list[str]:
    messages: list[str] = []
    for issue in error.errors(include_url=False):
        location = ".".join(str(part) for part in issue.get("loc", ())) or "input"
        messages.append(f"结构化清洗输入格式错误 {location}: {issue.get('msg', 'invalid value')}")
    return messages


def _unit_token(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"[\s_-]+", "", value).casefold()


def _unit_family(value: str | None) -> str | None:
    token = _unit_token(value)
    if token is None:
        return None
    groups = {
        "gib": {
            "gib",
            "gb",
            "g",
            "tib",
            "tb",
            "t",
            "mib",
            "mb",
            "m",
        },
        "count": {
            "count",
            "个",
            "台",
            "套",
            "项",
            "条",
            "节点",
            "node",
            "nodes",
            "shard",
            "shards",
            "task",
            "tasks",
            "broker",
            "brokers",
        },
        "vcpu": {"vcpu", "核"},
        "lcu": {"lcu"},
        "hours": {"hour", "hours", "小时"},
        "days": {"day", "days", "天"},
        "iops": {"iops"},
        "requests": {"request", "requests", "请求", "次", "调用"},
        "percent": {"%", "％"},
        "seconds": {"second", "seconds", "秒", "ms", "毫秒"},
    }
    for family, aliases in groups.items():
        if token in aliases:
            return family
    return token


def _units_equal(actual: str | None, expected: str | None) -> bool:
    if actual is None or expected is None:
        return actual is expected
    return _unit_token(actual) == _unit_token(expected)


def _numbers_equal(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)


def _strict_value(
    value: Any,
    contract: StructuredFieldContract,
    *,
    path: str,
) -> tuple[Any, list[str]]:
    violations: list[str] = []
    normalized = value
    if contract.value_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value, [f"{path} 必须是 number，不能提交字符串或布尔值"]
        if not math.isfinite(float(value)):
            return value, [f"{path} 必须是有限 number"]
    elif contract.value_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return value, [f"{path} 必须是 integer"]
    elif contract.value_type == "string":
        if not isinstance(value, str) or not value.strip():
            return value, [f"{path} 必须是非空 string"]
        normalized = value.strip()
    elif contract.value_type == "boolean":
        if not isinstance(value, bool):
            return value, [f"{path} 必须是 boolean"]
    elif contract.value_type == "array_object":
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            return value, [f"{path} 必须是 array<object>"]

    try:
        json.dumps(normalized, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        violations.append(f"{path} 必须是可序列化且不含 NaN/Infinity 的 JSON 值")

    if contract.value_type in {"number", "integer"} and not violations:
        number = float(normalized)
        if contract.minimum is not None and number < contract.minimum:
            violations.append(f"{path} 小于服务端合同最小值 {contract.minimum:g}")
        if contract.maximum is not None and number > contract.maximum:
            violations.append(f"{path} 大于服务端合同最大值 {contract.maximum:g}")
    if contract.allowed_values and isinstance(normalized, str):
        allowed = {item.casefold(): item for item in contract.allowed_values}
        canonical = allowed.get(normalized.casefold())
        if canonical is None:
            violations.append(
                f"{path} 必须是服务端允许值之一：{', '.join(contract.allowed_values)}"
            )
        else:
            normalized = canonical
    return normalized, violations


def _numeric_value(value: Any, value_type: ValueType) -> float | None:
    if value_type not in {"number", "integer"}:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _matched_numeric_atoms(
    *,
    source: str,
    evidence: str,
    expected_value: float,
    expected_unit: str | None,
) -> tuple[int, tuple[int, ...], str | None, list[str]]:
    source_folded = source.casefold()
    evidence_start = source_folded.find(evidence.casefold())
    if evidence_start < 0:
        return -1, (), None, [f"证据“{evidence}”不属于该组件的 cleaned_source"]

    expected_family = _unit_family(expected_unit)
    matches: list[tuple[int, str]] = []
    for atom in customer_quantitative_atoms(evidence):
        atom_family = _unit_family(atom.unit)
        if expected_family is not None and atom_family != expected_family:
            continue
        if _numbers_equal(atom.value, expected_value):
            matches.append((evidence_start + atom.start, atom.raw))
    if not matches:
        return evidence_start, (), None, [
            f"{evidence} 的数字证据与结构化值 {expected_value:g} {expected_unit or ''} 不一致"
        ]
    if len(matches) > 1:
        return evidence_start, (), None, [
            f"证据“{evidence}”包含多个相同数字，请缩小到唯一逐字证据"
        ]
    position, literal = matches[0]
    return evidence_start, (position,), literal, []


def _contract_for_path(
    path: str,
    contract: StructuredServiceContract,
) -> StructuredFieldContract | None:
    if path == "quantity":
        return StructuredFieldContract(
            value_type="integer", unit="count", minimum=1, maximum=10000
        )
    if path == "hours_per_month":
        return StructuredFieldContract(
            value_type="number", unit="hours", minimum=0.000001, maximum=744
        )
    if not path.startswith("requirements."):
        return None
    return contract.fields.get(path.split(".", 1)[1])


def _validate_component_fact(
    component: StructuredComponentInput,
    fact: StructuredFactInput,
    contract: StructuredServiceContract,
) -> tuple[_ValidatedFact | None, list[str]]:
    field_contract = _contract_for_path(fact.path, contract)
    if field_contract is None:
        return None, [
            f"{component.component_key}.{fact.path} 未在服务端字段合同 "
            f"{contract.contract_id} 中声明"
        ]

    value, violations = _strict_value(
        fact.value,
        field_contract,
        path=f"{component.component_key}.{fact.path}",
    )
    if not _units_equal(fact.unit, field_contract.unit):
        violations.append(
            f"{component.component_key}.{fact.path} 单位必须是 {field_contract.unit or 'null'}，"
            f"实际为 {fact.unit or 'null'}"
        )

    evidence_start = component.cleaned_source.casefold().find(fact.evidence.casefold())
    matched_starts: tuple[int, ...] = ()
    ledger_evidence = fact.evidence
    numeric = _numeric_value(value, field_contract.value_type)
    if numeric is not None and not violations:
        (
            evidence_start,
            matched_starts,
            numeric_literal,
            numeric_violations,
        ) = _matched_numeric_atoms(
            source=component.cleaned_source,
            evidence=fact.evidence,
            expected_value=numeric,
            expected_unit=fact.unit,
        )
        if numeric_literal is not None:
            # Persist the smallest literal evidence.  Besides making the
            # ledger unambiguous, this prevents surrounding words such as
            # ``每个`` from being mistaken for the fact unit before ``LCU``.
            ledger_evidence = numeric_literal
        violations.extend(
            f"{component.component_key}.{fact.path} 数字证据错误：{message}"
            for message in numeric_violations
        )
    elif evidence_start < 0:
        violations.append(
            f"{component.component_key}.{fact.path} 证据“{fact.evidence}”"
            "不属于该组件的 cleaned_source"
        )
    if violations:
        return None, violations
    return (
        _ValidatedFact(
            component_key=component.component_key,
            source_block_key=component.source_block_key,
            path=fact.path,
            value=value,
            unit=fact.unit,
            unit_family=_unit_family(fact.unit),
            evidence=fact.evidence,
            ledger_evidence=ledger_evidence,
            evidence_start=evidence_start,
            matched_source_atom_starts=matched_starts,
            scope=fact.scope,
            match_policy=fact.match_policy,
        ),
        [],
    )


def _component_coverage_violations(
    component: StructuredComponentInput,
    facts: list[_ValidatedFact],
) -> list[str]:
    covered = {
        position
        for fact in facts
        for position in fact.matched_source_atom_starts
    }
    return [
        f"{component.component_key} cleaned_source 中的数字“{atom.raw}”没有对应结构化事实"
        for atom in customer_quantitative_atoms(component.cleaned_source)
        if atom.start not in covered
    ]


def _duplicate_claim_violations(facts: list[_ValidatedFact]) -> list[str]:
    violations: list[str] = []

    # Within one isolated component, two paths may not claim the same literal
    # number. CPU and memory in one evidence sentence remain valid because
    # their unit families and source atom offsets differ.
    local: dict[tuple[str, int, str | None], list[_ValidatedFact]] = {}
    for fact in facts:
        for position in fact.matched_source_atom_starts:
            local.setdefault(
                (fact.component_key, position, fact.unit_family), []
            ).append(fact)
    for records in local.values():
        paths = {record.path for record in records}
        if len(paths) > 1:
            violations.append(
                f"{records[0].component_key} 的同一数字事实被分配给多个字段："
                + ", ".join(sorted(paths))
            )

    # Across components there is no shared character offset after source
    # isolation. ``source_block_key`` carries the one cleaned source-block
    # identity, and overlapping evidence proves that both rows claimed the
    # same statement rather than two coincidentally equal customer values.
    for index, left in enumerate(facts):
        if not left.matched_source_atom_starts:
            continue
        for right in facts[index + 1 :]:
            if left.component_key == right.component_key:
                continue
            if left.source_block_key != right.source_block_key:
                continue
            if left.unit_family != right.unit_family:
                continue
            if json.dumps(left.value, sort_keys=True, default=str) != json.dumps(
                right.value, sort_keys=True, default=str
            ):
                continue
            left_evidence = re.sub(r"\s+", "", left.evidence).casefold()
            right_evidence = re.sub(r"\s+", "", right.evidence).casefold()
            if not (
                left_evidence == right_evidence
                or left_evidence in right_evidence
                or right_evidence in left_evidence
            ):
                continue
            violations.append(
                "跨组件重复归属：同一清洗事实被 "
                f"{left.component_key}.{left.path} 与 {right.component_key}.{right.path} 同时占用"
            )
    return violations


def _component_graph_violations(
    components: tuple[StructuredComponentInput, ...],
) -> list[str]:
    violations: list[str] = []
    keys = [component.component_key for component in components]
    duplicated = sorted(key for key in set(keys) if keys.count(key) > 1)
    if duplicated:
        violations.append(f"组件 identity 重复：{', '.join(duplicated)}")
    key_set = set(keys)
    parents = {
        component.component_key: component.parent_component_key
        for component in components
        if component.component_key in key_set
    }
    for component in components:
        parent = component.parent_component_key
        if parent is None:
            continue
        if parent == component.component_key:
            violations.append(f"{component.component_key} 不能以自身作为父组件")
        elif parent not in key_set:
            violations.append(f"{component.component_key} 引用了不存在的父组件 {parent}")

    for key in keys:
        seen: set[str] = set()
        cursor: str | None = key
        while cursor is not None and cursor in parents:
            if cursor in seen:
                violations.append(f"父子组件关系存在环：{key}")
                break
            seen.add(cursor)
            cursor = parents.get(cursor)
    return violations


def _materialize_requirement(
    component: StructuredComponentInput,
    contract: StructuredServiceContract,
    facts: list[_ValidatedFact],
) -> tuple[ServiceRequirement | None, list[str]]:
    requirement = ServiceRequirement(
        service=component.service,
        calculator_service_name=contract.display_name,
        official_calculator_service_code=contract.calculator_service_code,
        official_calculator_template_id=contract.calculator_template_id,
        official_calculator_schema_hash=contract.schema_hash,
        component_key=component.component_key,
        parent_component_key=component.parent_component_key,
        derived_from_service=component.derived_from_service,
        product_identity=component.product_identity,
        region=component.region,
        source_text=component.cleaned_source,
        original_source_text=None,
        intake_source_fragments=[],
        field_sources={
            SOURCE_BLOCK_KEY_FIELD: component.source_block_key,
            OWNED_SOURCE_SLICE_FIELD: "system_policy",
            "_source_retention_policy": CLEANED_INPUT_POLICY_VERSION,
            "_semantic_fact_mapping": STRUCTURED_INTAKE_VERSION,
            "_structured_contract_id": contract.contract_id,
            "_structured_contract_hash": contract.schema_hash,
        },
        field_evidence={
            OWNED_SOURCE_SLICE_EVIDENCE_FIELD: component.cleaned_source,
        },
    )
    for fact in facts:
        if fact.path == "quantity":
            requirement.quantity = fact.value
            field = "quantity"
        elif fact.path == "hours_per_month":
            requirement.hours_per_month = fact.value
            field = "hours_per_month"
        else:
            field = fact.path.split(".", 1)[1]
            requirement.requirements[field] = fact.value
        requirement.field_sources[fact.path] = "customer_text"
        requirement.field_evidence[fact.path] = fact.ledger_evidence
        requirement.field_scopes[field] = fact.scope
        requirement.field_match_policies[field] = fact.match_policy
        requirement.locked_fields.append(fact.path)
    requirement.locked_fields = sorted(set(requirement.locked_fields))

    contract_issues = apply_pricing_contract(requirement)
    if contract_issues:
        return None, [
            f"{component.component_key}.{issue.field}: {issue.message}"
            for issue in contract_issues
        ]

    policy = contract.minimum_unit_policy
    if policy is not None and not any(
        field in requirement.requirements for field in policy.watched_fields
    ):
        requirement.requirements[policy.flag_field] = True
        requirement.requirements["system_default_assumption"] = policy.message
        requirement.field_sources[f"requirements.{policy.flag_field}"] = "system_minimum"
        requirement.field_evidence[f"requirements.{policy.flag_field}"] = "system_minimum"
        requirement.field_sources["requirements.system_default_assumption"] = (
            "system_minimum"
        )
        requirement.field_evidence["requirements.system_default_assumption"] = (
            "system_minimum"
        )

    finalize_customer_fact_ledger(requirement)
    return requirement, []


def _requirement_ir(
    requirement: ServiceRequirement,
    component_id: str,
) -> RequirementIR:
    facts = tuple(
        RequirementFactIR(
            fact_id=record.fact_id,
            path=record.path,
            value=record.value,
            unit=record.unit,
            scope=record.scope,
            source_kind=record.source_kind,
            evidence=record.evidence,
            match_policy=requirement.field_match_policies.get(
                record.path.removeprefix("requirements.")
            ),
        )
        for record in customer_pricing_fact_records(requirement)
    )
    return RequirementIR(
        component_id=component_id,
        component_key=requirement.component_key or component_id,
        parent_component_key=requirement.parent_component_key,
        service_intent=requirement.service,
        product_identity=requirement.product_identity,
        region=requirement.region,
        facts=facts,
        evidence_fingerprint=_canonical_hash(
            [
                {
                    "fact_id": fact.fact_id,
                    "path": fact.path,
                    "value": fact.value,
                    "evidence": fact.evidence,
                }
                for fact in facts
            ]
        ),
    )


class StructuredIntakeCompiler:
    """Compile trusted-contract, GPT-cleaned JSON without calling any AI."""

    def __init__(self, contracts: Mapping[str, StructuredServiceContract | Mapping[str, Any]]):
        normalized: dict[str, StructuredServiceContract] = {}
        for _key, raw_contract in contracts.items():
            contract = (
                raw_contract
                if isinstance(raw_contract, StructuredServiceContract)
                else StructuredServiceContract.model_validate(raw_contract)
            )
            # One internal adapter service may own several official forms
            # (for example RDS MySQL and Aurora).  Contract identity, never the
            # coarse service route, is therefore the lookup key.
            normalized_key = contract.contract_id
            if normalized_key in normalized:
                raise ValueError(f"duplicate structured service contract {normalized_key}")
            normalized[normalized_key] = contract
        self._contracts = normalized

    def compile(
        self,
        payload: StructuredIntakeSubmission | Mapping[str, Any],
    ) -> StructuredIntakeResult:
        if isinstance(payload, StructuredIntakeSubmission):
            submission = payload
        else:
            forbidden = _walk_forbidden_fields(payload)
            if forbidden:
                raise StructuredIntakeViolation(forbidden)
            try:
                submission = StructuredIntakeSubmission.model_validate(payload)
            except ValidationError as exc:
                raise StructuredIntakeViolation(_validation_error_messages(exc)) from exc

        violations = _component_graph_violations(submission.components)
        validated_by_component: dict[str, list[_ValidatedFact]] = {}
        all_validated: list[_ValidatedFact] = []

        for component in submission.components:
            contract = self._contracts.get(component.contract_id)
            if contract is None:
                violations.append(
                    f"{component.component_key} 的合同 {component.contract_id} 不存在或已过期"
                )
                continue
            if component.service != contract.service:
                violations.append(
                    f"{component.component_key} 的合同 {contract.contract_id} 属于 "
                    f"{contract.service}，不能用于 {component.service}"
                )
            if component.contract_schema_hash != contract.schema_hash:
                violations.append(
                    f"{component.component_key} schema_hash 已过期，必须重新进行需求清洗"
                )

            paths = [fact.path for fact in component.facts]
            repeated_paths = sorted(path for path in set(paths) if paths.count(path) > 1)
            if repeated_paths:
                violations.append(
                    f"{component.component_key} 重复提交字段：{', '.join(repeated_paths)}"
                )

            component_facts: list[_ValidatedFact] = []
            for fact in component.facts:
                validated, fact_violations = _validate_component_fact(
                    component, fact, contract
                )
                violations.extend(fact_violations)
                if validated is not None:
                    component_facts.append(validated)
            validated_by_component[component.component_key] = component_facts
            all_validated.extend(component_facts)
            violations.extend(_component_coverage_violations(component, component_facts))

        violations.extend(_duplicate_claim_violations(all_validated))
        if violations:
            raise StructuredIntakeViolation(violations)

        requirements: list[ServiceRequirement] = []
        for component in submission.components:
            contract = self._contracts[component.contract_id]
            requirement, materialization_violations = _materialize_requirement(
                component,
                contract,
                validated_by_component[component.component_key],
            )
            violations.extend(materialization_violations)
            if requirement is not None:
                requirements.append(requirement)
        if violations:
            raise StructuredIntakeViolation(violations)

        duplicate_groups = duplicate_customer_fact_ownership(requirements)
        if duplicate_groups:
            raise StructuredIntakeViolation(
                [
                    "事实账本检测到跨组件重复归属："
                    + ", ".join(
                        f"{record.component_key}.{record.path}" for record in records
                    )
                    for records in duplicate_groups
                ]
            )

        intent = ParsedIntent(
            customer_summary=f"已验证 {len(requirements)} 项 GPT 清洗配置。",
            services=requirements,
            ambiguities=[],
        )
        if not intent_is_cleaned_only(intent):
            raise StructuredIntakeViolation(["结构化清洗结果未通过 cleaned-only 边界"])

        requirement_ir = tuple(
            _requirement_ir(requirement, str(index))
            for index, requirement in enumerate(requirements)
        )
        return StructuredIntakeResult(
            intent=intent,
            requirement_ir=requirement_ir,
        )


def compile_structured_intake(
    payload: StructuredIntakeSubmission | Mapping[str, Any],
    *,
    contracts: Mapping[str, StructuredServiceContract | Mapping[str, Any]],
) -> StructuredIntakeResult:
    """Functional facade used by an HTTP/MCP adapter in a later change."""

    return StructuredIntakeCompiler(contracts).compile(payload)
