from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.domain.billing_rules import (
    BillingEvaluation,
    Expression,
    evaluate_expression,
    number,
    product,
)

FieldRole = Literal["selection", "usage", "context"]


@dataclass(frozen=True)
class OfficialSource:
    """One AWS page that defines a field or billing dimension."""

    title: str
    url: str
    supports: str


@dataclass(frozen=True)
class TemplateField:
    """A normalized customer input accepted by one component template."""

    name: str
    value_type: str
    description: str
    role: FieldRole = "selection"
    unit: str | None = None
    allowed_values: tuple[str, ...] = ()

    def prompt_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "type": self.value_type,
            "role": self.role,
            "description": self.description,
            "value": None,
        }
        if self.unit:
            payload["unit"] = self.unit
        if self.allowed_values:
            payload["allowed_values"] = list(self.allowed_values)
        return payload


@dataclass(frozen=True)
class BillingOutput:
    """How validated template values become one official billing quantity."""

    key: str
    official_dimension: str
    source_fields: tuple[str, ...]
    formula: str
    calculation: Expression | None = None
    unit: str | None = None
    scope_field: str | None = None


@dataclass(frozen=True)
class ComponentTemplateSpec:
    """Single source for AI extraction, review fields and billing inputs."""

    service_key: str
    display_name: str
    aliases: tuple[str, ...]
    primary_variants: tuple[str, ...]
    official_sources: tuple[OfficialSource, ...]
    fields: tuple[TemplateField, ...]
    billing_outputs: tuple[BillingOutput, ...]
    guidance: str
    critical_rule: str
    example_customer_text: str
    safe_defaults: dict[str, Any] = field(default_factory=dict)
    non_pricing_fields: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        names = self.field_names
        if len(names) != len(set(names)):
            raise ValueError(f"{self.service_key} template contains duplicate fields")
        unknown_defaults = set(self.safe_defaults) - set(names)
        if unknown_defaults:
            raise ValueError(
                f"{self.service_key} template metadata references unknown fields: "
                f"{sorted(unknown_defaults)}"
            )
        allowed_sources = {"quantity", "hours_per_month", *names}
        for output in self.billing_outputs:
            unknown = set(output.source_fields) - allowed_sources
            if unknown:
                raise ValueError(
                    f"{self.service_key}.{output.key} references unknown fields: {sorted(unknown)}"
                )
            if output.calculation is not None:
                if not output.unit:
                    raise ValueError(f"{output.key} must declare its billing unit")
                unknown = output.calculation.fields() - set(output.source_fields)
                if unknown:
                    raise ValueError(
                        f"{output.key} calculation references undeclared fields: {unknown}"
                    )
        keys = [output.key for output in self.billing_outputs]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{self.service_key} contains duplicate billing rule IDs")

    @property
    def rule_version(self) -> str:
        payload = json.dumps(
            {
                "outputs": [asdict(item) for item in self.billing_outputs],
                "fields": [asdict(item) for item in self.fields],
                "sources": [asdict(item) for item in self.official_sources],
            },
            sort_keys=True,
        )
        return "v1-" + hashlib.sha256(payload.encode()).hexdigest()[:16]

    def evaluate(
        self,
        key: str,
        values: dict[str, Any],
        *,
        scopes: dict[str, str] | None = None,
        item_index: int | None = None,
    ) -> BillingEvaluation:
        output = next((item for item in self.billing_outputs if item.key == key), None)
        if output is None or output.calculation is None:
            raise ValueError(f"{self.service_key}.{key} is not executable")
        calculation = output.calculation
        applied_scopes = {}
        if output.scope_field:
            scope = (scopes or {}).get(output.scope_field, "total")
            if scope not in {"total", "component_total", "aggregate", "per_resource"}:
                raise ValueError(f"unsupported billing scope {scope!r} for {output.scope_field}")
            applied_scopes[output.scope_field] = scope
            if scope == "per_resource":
                calculation = product(calculation, number("quantity", minimum=1, integer=True))
        result = evaluate_expression(
            calculation,
            values,
            rule_id=f"{self.service_key}.{key}",
            rule_version=self.rule_version,
            unit=output.unit or "",
            item_index=item_index,
        )
        return replace(result, scopes=applied_scopes)

    def replay(self, audit: dict[str, Any]) -> BillingEvaluation:
        """Recompute from declared rules, never from the audit's formula text."""
        if audit["rule_version"] != self.rule_version:
            raise ValueError("billing rule version changed; regenerate the quote")
        values: dict[str, Any] = {}
        for path, raw in audit["inputs"].items():
            try:
                value = Decimal(raw)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValueError(f"invalid calculation input {path}") from exc
            parts = path.split(".")
            if len(parts) == 1:
                values[path] = value
            elif len(parts) == 3 and parts[1].isdigit() and int(parts[1]) < 1000:
                rows = values.setdefault(parts[0], [])
                if not isinstance(rows, list):
                    raise ValueError("conflicting calculation input paths")
                while len(rows) <= int(parts[1]):
                    rows.append({})
                rows[int(parts[1])][parts[2]] = value
            else:
                raise ValueError(f"invalid calculation input path {path}")
        service, _, key = audit["rule_id"].partition(".")
        if service != self.service_key:
            raise ValueError("calculation rule belongs to another service")
        result = self.evaluate(
            key, values, scopes=audit.get("scopes"), item_index=audit.get("item_index")
        )
        if result.amount is None or result.amount != Decimal(audit["amount"]):
            raise ValueError("calculation result differs from executable rule")
        for name in ("unit", "expression", "inputs", "defaults", "scopes", "item_index"):
            if getattr(result, name) != audit.get(name):
                raise ValueError(f"calculation {name} differs from executable rule")
        if audit.get("missing_fields"):
            raise ValueError("incomplete calculation cannot be billed")
        return result

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.fields)

    @property
    def metered_fields(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.fields if item.role == "usage")

    def prompt_contract(self) -> str:
        """Render the full schema into the component-scoped AI prompt."""

        sources = "\n".join(
            f"- {item.title}: {item.url}（{item.supports}）" for item in self.official_sources
        )
        field_schema = {item.name: item.prompt_payload() for item in self.fields}
        blank_template = {
            "service": self.service_key,
            "region": None,
            "quantity": None,
            "hours_per_month": None,
            "requirements": {name: None for name in self.field_names},
        }
        billing = [
            {
                "output": item.key,
                "official_dimension": item.official_dimension,
                "source_fields": list(item.source_fields),
                "formula": item.calculation.document() if item.calculation else item.formula,
                "execution_status": "executable" if item.calculation else "adapter_required",
                "unit": item.unit,
                "scope_field": item.scope_field,
                "rule_version": self.rule_version,
            }
            for item in self.billing_outputs
        ]
        return (
            f"【{self.display_name}】\n{self.guidance.strip()}\n\n"
            "【官方依据】\n"
            f"{sources}\n\n"
            "【官方字段模板（完整注入）】\n"
            f"{json.dumps(field_schema, ensure_ascii=False, indent=2)}\n\n"
            "【空白填写对象】\n"
            f"{json.dumps(blank_template, ensure_ascii=False, indent=2)}\n\n"
            "【计费用量映射】\n"
            f"{json.dumps(billing, ensure_ascii=False, indent=2)}\n"
            "字段名、类型、单位、枚举和作用域均为闭集；未在客户原话出现的值保持 null。"
        )
