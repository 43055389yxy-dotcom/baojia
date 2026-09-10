from __future__ import annotations

import importlib

import pytest

from app.domain.models import ServiceRequirement
from app.integrations.aws_component_templates.registry import (
    PRIMARY_TEMPLATE_MODULES,
    component_template_spec,
    primary_component_templates,
    validate_component_template_values,
)
from app.integrations.prompt_library import build_component_extraction_prompt
from app.integrations.service_templates import (
    billing_dimension_fields,
    component_template,
    normalized_service_key,
    requirement_fields,
    safe_requirement_defaults,
    strip_non_pricing_context_fields,
)

EXPECTED_MODULES = {
    "ec2": "app.integrations.aws_component_templates.ec2",
    "rds": "app.integrations.aws_component_templates.rds",
    "elasticache": "app.integrations.aws_component_templates.redis",
    "s3": "app.integrations.aws_component_templates.s3",
    "elb": "app.integrations.aws_component_templates.alb",
}


def test_each_primary_component_template_lives_in_its_own_code_module() -> None:
    assert PRIMARY_TEMPLATE_MODULES == EXPECTED_MODULES
    templates = primary_component_templates()

    assert set(templates) == set(EXPECTED_MODULES)
    for service, module_name in EXPECTED_MODULES.items():
        module = importlib.import_module(module_name)
        assert module.TEMPLATE is templates[service]


@pytest.mark.parametrize("service", EXPECTED_MODULES)
def test_primary_template_is_officially_sourced_and_self_consistent(
    service: str,
) -> None:
    template = component_template_spec(service)
    assert template is not None
    assert template.official_sources
    assert all(
        source.url.startswith("https://aws.amazon.com/")
        or source.url.startswith("https://docs.aws.amazon.com/")
        for source in template.official_sources
    )
    assert len(template.field_names) == len(set(template.field_names))
    assert set(template.safe_defaults) <= set(template.field_names)
    assert set(template.metered_fields) <= set(template.field_names)
    assert template.billing_outputs
    for output in template.billing_outputs:
        assert output.source_fields
        assert set(output.source_fields) <= {
            "quantity",
            "hours_per_month",
            *template.field_names,
        }


@pytest.mark.parametrize("service", EXPECTED_MODULES)
def test_ai_receives_the_entire_component_template_not_only_a_field_list(
    service: str,
) -> None:
    template = component_template_spec(service)
    assert template is not None
    prompt = build_component_extraction_prompt(service, template.example_customer_text)

    assert "【官方字段模板（完整注入）】" in prompt
    assert "【官方依据】" in prompt
    assert '"requirements"' in prompt
    for field in template.field_names:
        assert f'"{field}"' in prompt
    for source in template.official_sources:
        assert source.url in prompt


@pytest.mark.parametrize("service", EXPECTED_MODULES)
def test_review_and_pricing_contracts_share_the_same_component_template(
    service: str,
) -> None:
    template = component_template_spec(service)
    assert template is not None

    assert requirement_fields(service) == (
        *template.field_names,
        "system_default_assumption",
    )
    blank = component_template(ServiceRequirement(service=service))
    assert tuple(blank["requirements"]) == requirement_fields(service)
    assert safe_requirement_defaults(service) == dict(template.safe_defaults)
    assert billing_dimension_fields(service) == template.metered_fields

    populated = {field: field for field in template.field_names}
    projected = strip_non_pricing_context_fields(service, populated)
    assert set(projected) == set(template.field_names) - set(template.non_pricing_fields)


def test_customer_aliases_resolve_to_the_standalone_component_templates() -> None:
    assert normalized_service_key("Redis") == "elasticache"
    assert normalized_service_key("valkey") == "elasticache"
    assert normalized_service_key("elbv2") == "elb"
    assert normalized_service_key("application_load_balancer") == "elb"
    assert normalized_service_key("RDS MySQL") == "rds"
    assert normalized_service_key("Amazon RDS for PostgreSQL") == "rds"


def test_first_rds_template_explicitly_covers_mysql_and_postgresql() -> None:
    template = component_template_spec("rds")
    assert template is not None
    assert {"mysql", "postgresql"} <= set(template.primary_variants)


def test_first_load_balancer_template_explicitly_targets_alb() -> None:
    template = component_template_spec("elb")
    assert template is not None
    assert template.primary_variants == ("application",)


def test_program_validates_ai_types_and_closed_enums_before_pricing() -> None:
    normalized = validate_component_template_values(
        "ec2",
        {
            "architecture": "ARM64",
            "operating_system": "LINUX",
            "vcpu": 4,
            "detailed_monitoring": False,
        },
    )
    assert normalized["architecture"] == "arm64"
    assert normalized["operating_system"] == "linux"

    with pytest.raises(ValueError, match="vcpu.*number"):
        validate_component_template_values("ec2", {"vcpu": "four"})
    with pytest.raises(ValueError, match="architecture.*allowed_values"):
        validate_component_template_values("ec2", {"architecture": "sparc"})
    with pytest.raises(ValueError, match="detailed_monitoring.*boolean"):
        validate_component_template_values("ec2", {"detailed_monitoring": "yes"})


def test_runtime_discovered_fields_remain_outside_primary_template_validation() -> None:
    assert validate_component_template_values("ec2", {"official_usage_x": 12, "memory_gib": 8}) == {
        "official_usage_x": 12,
        "memory_gib": 8,
    }
