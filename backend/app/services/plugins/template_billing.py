"""Boundary adapter from a selected resource's structured inputs to template rules."""

from __future__ import annotations

from typing import Any

from app.core.errors import ManualConfirmationRequired
from app.domain.billing_rules import BillingEvaluation
from app.domain.customer_facts import field_scope
from app.domain.models import ServiceRequirement
from app.integrations.aws_component_templates.registry import component_template_spec


def template_usage(
    service: str,
    key: str,
    requirement: ServiceRequirement,
    requested: dict[str, Any],
    *,
    item_index: int | None = None,
) -> BillingEvaluation:
    # The formal component quantity/hours cannot be overwritten by legacy
    # copies inside requirements. No source text or merged specifications here.
    template = component_template_spec(service)
    scopes = (
        {
            output.scope_field: field_scope(requirement, output.scope_field)
            for output in template.billing_outputs
            if output.scope_field
        }
        if template
        else {}
    )
    return template_usage_values(
        service,
        key,
        {
            **requested,
            "quantity": requirement.quantity,
            "hours_per_month": requirement.hours_per_month,
        },
        scopes=scopes,
        item_index=item_index,
    )


def template_usage_values(
    service: str,
    key: str,
    values: dict[str, Any],
    *,
    scopes: dict[str, str] | None = None,
    item_index: int | None = None,
) -> BillingEvaluation:
    template = component_template_spec(service)
    if template is None:
        raise ValueError(f"No executable template for {service}")
    try:
        return template.evaluate(key, values, scopes=scopes, item_index=item_index)
    except ValueError as exc:
        raise ManualConfirmationRequired(
            f"计费规则 {template.service_key}.{key} 无法通过校验：{exc}",
            code="billing_rule_validation_failed",
            rule_id=f"{template.service_key}.{key}",
            rule_version=template.rule_version,
        ) from exc
