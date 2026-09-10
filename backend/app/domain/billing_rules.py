"""Small, closed arithmetic language. Never evaluates Python or customer prose.

Service-owned declarations generate both prompt formulas and billable amounts.
All arithmetic is Decimal; floats are only produced at the existing AWS boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal, localcontext
from functools import reduce
from operator import mul
from typing import Any


@dataclass(frozen=True)
class Expression:
    op: str
    args: tuple[Expression, ...] = ()
    name: str = ""
    literal: str | None = None
    minimum: str = "0"
    maximum: str | None = None
    integer: bool = False
    required: tuple[str, ...] = ()

    def fields(self) -> set[str]:
        if self.op == "each":
            return {self.name} | (self.args[0].fields() - set(self.required))
        return ({self.name} if self.op == "field" else set()).union(
            self.required, *(arg.fields() for arg in self.args)
        )

    def document(self) -> str:
        if self.op == "field":
            return self.name if self.literal is None else f"({self.name} ?? {self.literal})"
        if self.op == "constant":
            return str(self.literal)
        if self.op == "each":
            return f"sum_each({self.name}; {self.args[0].document()})"
        args = [arg.document() for arg in self.args]
        operators = {"product": " × ", "sum": " + ", "divide": " ÷ ", "subtract": " − "}
        if self.op in operators:
            return "(" + operators[self.op].join(args) + ")"
        if self.op == "when_complete":
            return f"when_complete({', '.join(self.required)}; {args[0]})"
        return f"{self.op}({', '.join(args)})"

    def when_complete(self, *fields: str) -> Expression:
        return Expression("when_complete", (self,), required=tuple(fields))

    def evaluate(self, values: dict[str, Any], trace: _Trace) -> Decimal | None:
        if self.op == "each":
            rows = values.get(self.name)
            if rows is None:
                trace.missing.add(self.name)
                return None
            if not isinstance(rows, list):
                raise ValueError(f"{self.name} must be an array")
            if trace.item_index is not None and not 0 <= trace.item_index < len(rows):
                raise ValueError("billing item index is out of range")
            total = Decimal(0)
            for index, row in enumerate(rows):
                if trace.item_index is not None and index != trace.item_index:
                    continue
                if not isinstance(row, dict):
                    raise ValueError(f"{self.name}.{index} must be an object")
                local = _Trace()
                # Nested objects cannot shadow quantity/hours or other outer facts.
                local_values = {**values, **{key: row.get(key) for key in self.required}}
                amount = self.args[0].evaluate(local_values, local)
                if amount is None:
                    raise ValueError(f"{self.name}.{index} is missing {sorted(local.missing)}")
                for source, target in (
                    (local.inputs, trace.inputs),
                    (local.defaults, trace.defaults),
                ):
                    for key, value in source.items():
                        target[f"{self.name}.{index}.{key}" if key in self.required else key] = (
                            value
                        )
                total += amount
            return total
        if self.op == "constant":
            return Decimal(str(self.literal))
        if self.op == "field":
            value = values.get(self.name)
            if value is None:
                if self.literal is None:
                    trace.missing.add(self.name)
                    return None
                trace.defaults[self.name] = self.literal
                value = Decimal(self.literal)
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                raise ValueError(f"{self.name} must be a finite number")
            amount = Decimal(str(value))
            if not amount.is_finite() or amount < Decimal(self.minimum):
                raise ValueError(f"{self.name} must be finite and >= {self.minimum}")
            if self.maximum is not None and amount > Decimal(self.maximum):
                raise ValueError(f"{self.name} must be <= {self.maximum}")
            if self.integer and amount != amount.to_integral_value():
                raise ValueError(f"{self.name} must be an integer")
            if self.name not in trace.defaults:
                trace.inputs[self.name] = format(amount.normalize(), "f")
            return amount
        if self.op == "when_complete":
            absent = {key for key in self.required if values.get(key) is None}
            if absent:
                trace.missing.update(absent)
                return None
            return self.args[0].evaluate(values, trace)
        if self.op == "coalesce":
            for arg in self.args:
                candidate = arg.evaluate(values, trace)
                if candidate is not None:
                    return candidate
            return None
        amounts = [arg.evaluate(values, trace) for arg in self.args]
        available = [value for value in amounts if value is not None]
        if self.op == "maximum":
            return max(available) if available else None
        if self.op == "reconcile":
            if not available:
                return None
            if any(value != available[0] for value in available[1:]):
                raise ValueError(f"billing scope conflict: {self.document()}")
            return available[0]
        if len(available) != len(amounts):
            return None
        if self.op == "product":
            return reduce(mul, available, Decimal(1))
        if self.op == "sum":
            return sum(available, Decimal(0))
        if self.op == "divide":
            if available[1] == 0:
                raise ValueError("billing divisor must be greater than zero")
            return available[0] / available[1]
        if self.op == "subtract":
            return available[0] - available[1]
        raise ValueError(f"unsupported billing operator {self.op}")


@dataclass
class _Trace:
    inputs: dict[str, str] = field(default_factory=dict)
    defaults: dict[str, str] = field(default_factory=dict)
    missing: set[str] = field(default_factory=set)
    item_index: int | None = None


@dataclass(frozen=True)
class BillingEvaluation:
    rule_id: str
    rule_version: str
    unit: str
    expression: str
    amount: Decimal | None
    inputs: dict[str, str]
    defaults: dict[str, str]
    missing_fields: tuple[str, ...]
    scopes: dict[str, str] = field(default_factory=dict)
    item_index: int | None = None

    def audit(self) -> dict[str, Any]:
        return {**asdict(self), "amount": str(self.amount) if self.amount is not None else None}


def evaluate_expression(
    expression: Expression,
    values: dict[str, Any],
    *,
    rule_id: str,
    rule_version: str,
    unit: str,
    item_index: int | None = None,
) -> BillingEvaluation:
    trace = _Trace(item_index=item_index)
    with localcontext() as context:
        context.prec = 38
        amount = expression.evaluate(values, trace)
    if amount is not None and (not amount.is_finite() or amount < 0):
        raise ValueError(f"{rule_id} produced invalid usage")
    return BillingEvaluation(
        rule_id,
        rule_version,
        unit,
        expression.document(),
        amount,
        trace.inputs,
        trace.defaults,
        tuple(sorted(trace.missing)) if amount is None else (),
        item_index=item_index,
    )


def number(
    name: str,
    *,
    default: int | float | None = None,
    minimum: int = 0,
    maximum: int | None = None,
    integer: bool = False,
) -> Expression:
    return Expression(
        "field",
        name=name,
        literal=str(default) if default is not None else None,
        minimum=str(minimum),
        maximum=str(maximum) if maximum is not None else None,
        integer=integer,
    )


def constant(value: int | float) -> Expression:
    return Expression("constant", literal=str(value))


def product(*args: Expression) -> Expression:
    return Expression("product", args)


def sum_of(*args: Expression) -> Expression:
    return Expression("sum", args)


def divide(left: Expression, right: Expression) -> Expression:
    return Expression("divide", (left, right))


def subtract(left: Expression, right: Expression) -> Expression:
    return Expression("subtract", (left, right))


def maximum(*args: Expression) -> Expression:
    return Expression("maximum", args)


def coalesce(*args: Expression) -> Expression:
    return Expression("coalesce", args)


def reconcile(*args: Expression) -> Expression:
    """Use one amount, but prove equality when multiple representations exist."""
    return Expression("reconcile", args)


def each(name: str, expression: Expression, *, local_fields: tuple[str, ...]) -> Expression:
    return Expression("each", (expression,), name=name, required=local_fields)
