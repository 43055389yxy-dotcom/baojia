from __future__ import annotations

import math
import re
from importlib import import_module
from types import MappingProxyType

from app.integrations.aws_component_templates.base import ComponentTemplateSpec

# This file intentionally contains only module addresses. Component fields,
# rules and official sources remain owned by the five independent code pages.
PRIMARY_TEMPLATE_MODULES = {
    "ec2": "app.integrations.aws_component_templates.ec2",
    "rds": "app.integrations.aws_component_templates.rds",
    "elasticache": "app.integrations.aws_component_templates.redis",
    "s3": "app.integrations.aws_component_templates.s3",
    "elb": "app.integrations.aws_component_templates.alb",
}


def _load_templates() -> dict[str, ComponentTemplateSpec]:
    loaded: dict[str, ComponentTemplateSpec] = {}
    for service, module_name in PRIMARY_TEMPLATE_MODULES.items():
        template = import_module(module_name).TEMPLATE
        if not isinstance(template, ComponentTemplateSpec):
            raise TypeError(f"{module_name}.TEMPLATE must be ComponentTemplateSpec")
        if template.service_key != service:
            raise ValueError(f"{module_name} declares {template.service_key}, expected {service}")
        loaded[service] = template
    return loaded


_TEMPLATES = MappingProxyType(_load_templates())
_ALIASES = MappingProxyType(
    {
        alias.casefold(): template.service_key
        for template in _TEMPLATES.values()
        for alias in template.aliases
    }
)


def _canonical_identity_label(value: str) -> str:
    """Normalize punctuation-only AWS product spelling differences.

    The aliases remain provider-owned declarations in each component module.
    This normalization only makes spaces, underscores and the conventional
    ``Amazon/AWS ... for ...`` spelling equivalent; it does not scan customer
    prose for service keywords.
    """

    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    return "".join(token for token in tokens if token not in {"amazon", "aws", "for"})


def _canonical_aliases() -> dict[str, str]:
    owners: dict[str, str] = {}
    collisions: set[str] = set()
    for template in _TEMPLATES.values():
        for label in (template.service_key, *template.aliases):
            canonical = _canonical_identity_label(label)
            previous = owners.get(canonical)
            if previous is not None and previous != template.service_key:
                collisions.add(canonical)
            else:
                owners[canonical] = template.service_key
    return {label: service for label, service in owners.items() if label not in collisions}


_CANONICAL_ALIASES = MappingProxyType(_canonical_aliases())


def primary_component_templates() -> dict[str, ComponentTemplateSpec]:
    return dict(_TEMPLATES)


def component_template_spec(service: str) -> ComponentTemplateSpec | None:
    key = service.strip().casefold()
    direct = _ALIASES.get(key, key)
    if direct in _TEMPLATES:
        return _TEMPLATES[direct]
    canonical = _canonical_identity_label(service)
    return _TEMPLATES.get(_CANONICAL_ALIASES.get(canonical, canonical))


def component_template_variant(service_or_label: str) -> str | None:
    """Return a declared product variant present in an exact template label."""

    template = component_template_spec(service_or_label)
    if template is None:
        return None
    tokens = set(re.findall(r"[a-z0-9]+", service_or_label.casefold()))
    matches = [
        variant
        for variant in template.primary_variants
        if set(re.findall(r"[a-z0-9]+", variant.casefold())) <= tokens
    ]
    return matches[0] if len(matches) == 1 else None


def component_template_aliases() -> dict[str, str]:
    return dict(_ALIASES)


def component_template_field_sets() -> dict[str, tuple[str, ...]]:
    return {key: template.field_names for key, template in _TEMPLATES.items()}


def component_template_prompt_modules() -> dict[str, str]:
    return {key: template.prompt_contract() for key, template in _TEMPLATES.items()}


def validate_component_template_values(
    service: str,
    requirements: dict[str, object],
) -> dict[str, object]:
    """Validate an AI candidate against the owning component schema.

    Runtime-generated official fields are intentionally passed through. They
    are validated by the generated Price List profile that introduced them;
    this function owns only fields declared by the five primary templates.
    """

    template = component_template_spec(service)
    if template is None:
        return dict(requirements)
    fields = {item.name: item for item in template.fields}
    normalized = dict(requirements)
    for name, value in requirements.items():
        definition = fields.get(name)
        if definition is None or value is None:
            continue
        value_type = definition.value_type
        valid = True
        if value_type == "string":
            valid = isinstance(value, str)
        elif value_type == "number":
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif value_type == "integer":
            valid = (isinstance(value, int) and not isinstance(value, bool)) or (
                isinstance(value, float) and value.is_integer()
            )
            if valid:
                normalized[name] = int(value)
        elif value_type == "boolean":
            valid = isinstance(value, bool)
        elif value_type == "array<object>":
            valid = isinstance(value, list) and all(isinstance(item, dict) for item in value)
        else:
            raise ValueError(f"{name} has unsupported template type {value_type}")
        if not valid:
            raise ValueError(f"{name} must be {value_type}")
        if value_type in {"number", "integer"}:
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if definition.allowed_values:
            comparable = str(value).strip().casefold()
            allowed = {item.casefold(): item for item in definition.allowed_values}
            if comparable not in allowed:
                raise ValueError(
                    f"{name} must match allowed_values: " + ", ".join(definition.allowed_values)
                )
            normalized[name] = allowed[comparable]
    return normalized
