from __future__ import annotations

from app.core.config import Settings
from app.core.errors import ManualConfirmationRequired
from app.domain.component_integrity import overlay_customer_fields
from app.domain.fact_ledger import unconsumed_customer_pricing_facts
from app.domain.models import (
    SelectedResource,
    ServiceRequirement,
    UnmappedPricingFact,
    UsageLine,
)
from app.integrations.deepseek import DeepSeekIntentParser, _official_extraction_contract
from app.services.plugins.generic_official import GenericOfficialPlugin
from app.services.quote_service import QuoteService


def test_unknown_customer_field_is_conserved_in_template_overflow() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    source = "应用服务器3台，每台系统盘200GB，数据盘500GB"
    component = ServiceRequirement(service="ec2", source_text=source)

    result = parser._component_from_template_output(
        {
            "component": {
                "requirements": {"data_disk_gib": 500},
                "field_evidence": {
                    "requirements.data_disk_gib": "数据盘500GB"
                },
            }
        },
        component,
    )

    assert "data_disk_gib" not in result.requirements
    assert result.unmapped_pricing_facts == [
        UnmappedPricingFact(
            field_hint="data_disk_gib",
            value=500,
            unit="GB",
            scope="component_total",
            evidence="数据盘500GB",
        )
    ]


def test_template_overflow_survives_automated_component_overlay() -> None:
    original = ServiceRequirement(
        service="ec2",
        source_text="数据盘500GB",
        unmapped_pricing_facts=[
            UnmappedPricingFact(
                field_hint="data_disk_gib",
                value=500,
                unit="GB",
                evidence="数据盘500GB",
            )
        ],
    )
    automated = ServiceRequirement(service="ec2", source_text=original.source_text)

    overlay_customer_fields(automated, original)

    assert automated.unmapped_pricing_facts == original.unmapped_pricing_facts


def test_official_contract_keeps_semantic_fields_and_limits_exact_rows() -> None:
    profile = {
        "status": "verified",
        "display_name": "Amazon Example",
        "field_bindings": [
            {
                "field": "data_in_gib",
                "label": "摄入数据量",
                "unit": "GB",
                "usage_type": "DataIn",
                "operation": "Ingest",
            },
            *(
                {
                    "field": f"official_usage_unrelated_{index}",
                    "label": f"Unrelated feature {index}",
                    "unit": "unit",
                    "usage_type": f"Unrelated-{index}",
                    "operation": "Run",
                }
                for index in range(200)
            ),
        ],
    }

    fields, prompt = _official_extraction_contract(profile, "每月摄入10TB")

    assert fields == ("data_in_gib",)
    assert "data_in_gib" in prompt
    assert "official_usage_unrelated" not in prompt


def test_final_coverage_finds_customer_number_not_used_by_pricing() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        source_text="数据盘500GB",
        requirements={"additional_ebs_volumes": [{"size_gib": 500}]},
        field_sources={"requirements.additional_ebs_volumes": "customer_text"},
        field_evidence={"requirements.additional_ebs_volumes": "数据盘500GB"},
    )
    selection = SelectedResource(
        service="ec2",
        display_name="Amazon EC2",
        region="ap-southeast-1",
        model="m6i.large",
        architecture="1台",
        specifications={},
        official_product={"source": "AWS"},
        rationale="test",
        usage_lines=[
            UsageLine(
                key="ec2",
                service_code="AmazonEC2",
                usage_type="BoxUsage",
                operation="RunInstances",
                amount=730,
                source_fields=["hours_per_month"],
            )
        ],
    )

    assert unconsumed_customer_pricing_facts(requirement, selection) == [
        "requirements.additional_ebs_volumes"
    ]

    selection.usage_lines[0].source_fields.append("additional_ebs_volumes")
    assert unconsumed_customer_pricing_facts(requirement, selection) == []


def test_official_supplement_maps_service_specific_field_without_double_billing(
    monkeypatch,
) -> None:
    plugin = object.__new__(GenericOfficialPlugin)
    requirement = ServiceRequirement(
        service="rds",
        source_text="预置6000 IOPS",
        requirements={"storage_iops": 6000},
        field_sources={"requirements.storage_iops": "customer_text"},
        field_evidence={"requirements.storage_iops": "6000 IOPS"},
    )
    base = SelectedResource(
        service="rds",
        display_name="Amazon RDS",
        region="ap-southeast-1",
        model="db.m6i.large",
        architecture="数据库",
        specifications={},
        official_product={"source": "AWS"},
        rationale="test",
        usage_lines=[],
    )

    def fake_select(candidate, _default_region):
        assert candidate.requirements["iops"] == 6000
        return base.model_copy(
            update={
                "usage_lines": [
                    UsageLine(
                        key="gen1",
                        service_code="AmazonRDS",
                        usage_type="RDS:GP3-PIOPS",
                        operation="CreateDBInstance",
                        amount=6000,
                        source_fields=["iops"],
                    )
                ]
            }
        )

    monkeypatch.setattr(plugin, "select", fake_select)
    result = plugin.supplement_selection(
        requirement,
        base,
        ["requirements.storage_iops"],
        "ap-southeast-1",
    )

    assert len(result.usage_lines) == 1
    assert result.usage_lines[0].source_fields == ["storage_iops"]
    assert result.applied_requirement_fields == ["storage_iops"]
    assert unconsumed_customer_pricing_facts(requirement, result) == []


def test_official_supplement_merges_trace_into_same_existing_usage_line(monkeypatch) -> None:
    plugin = object.__new__(GenericOfficialPlugin)
    requirement = ServiceRequirement(
        service="ebs",
        source_text="3000 IOPS",
        requirements={"iops": 3000},
        field_sources={"requirements.iops": "customer_text"},
        field_evidence={"requirements.iops": "3000 IOPS"},
    )
    existing_line = UsageLine(
        key="existing",
        service_code="AmazonEC2",
        usage_type="EBS:VolumeP-IOPS.gp3",
        operation="",
        amount=3000,
        source_fields=[],
    )
    base = SelectedResource(
        service="ebs",
        display_name="Amazon EBS",
        region="ap-southeast-1",
        model="gp3",
        architecture="云盘",
        specifications={},
        official_product={"source": "AWS"},
        rationale="test",
        usage_lines=[existing_line],
    )
    monkeypatch.setattr(
        plugin,
        "select",
        lambda _candidate, _region: base.model_copy(
            update={
                "usage_lines": [
                    existing_line.model_copy(update={"source_fields": ["iops"]})
                ]
            }
        ),
    )

    result = plugin.supplement_selection(
        requirement,
        base,
        ["requirements.iops"],
        "ap-southeast-1",
    )

    assert len(result.usage_lines) == 1
    assert result.usage_lines[0].source_fields == ["iops"]


def test_pricing_copy_canonicalizes_field_value_and_provenance_together() -> None:
    reviewed = ServiceRequirement(
        service="ec2",
        source_text="内存16GB",
        requirements={"memory_gb": 16},
        field_sources={"requirements.memory_gb": "customer_text"},
        field_evidence={"requirements.memory_gb": "内存16GB"},
        field_scopes={"requirements.memory_gb": "per_resource"},
        locked_fields=["requirements.memory_gb"],
    )

    normalized = QuoteService._calculator_requirements(
        reviewed.requirements,
        reviewed.quantity,
        "ec2",
    )
    pricing = QuoteService._pricing_requirement_copy(
        reviewed,
        service_key="ec2",
        requirements=normalized,
    )

    assert pricing.requirements == {"memory_gib": 16}
    assert pricing.field_sources == {"requirements.memory_gib": "customer_text"}
    assert pricing.field_evidence == {"requirements.memory_gib": "内存16GB"}
    assert pricing.field_scopes == {"requirements.memory_gib": "per_resource"}
    assert pricing.locked_fields == ["requirements.memory_gib"]


def test_internal_fact_mapping_failures_never_become_customer_questions() -> None:
    assert QuoteService._is_technical_catalog_error(
        ManualConfirmationRequired(
            "字段尚未映射",
            code="unmapped_customer_pricing_facts",
        )
    )
    assert QuoteService._is_technical_catalog_error(
        ManualConfirmationRequired(
            "字段尚未进入价格",
            code="unconsumed_customer_pricing_facts",
        )
    )


def test_plain_machine_count_cannot_disappear_behind_default_quantity() -> None:
    source = "ElasticSearch 这边预计5台，单台16核128G，磁盘4T。"
    original = ServiceRequirement(
        service="opensearch",
        source_text=source,
    )
    filled = ServiceRequirement(
        service="opensearch",
        source_text=source,
        quantity=1,
        requirements={
            "vcpu": 16,
            "memory_gib": 128,
            "storage_gib_per_node": 4096,
        },
        field_evidence={
            "quantity": "预计5台",
            "requirements.vcpu": "16核",
            "requirements.memory_gib": "128G",
            "requirements.storage_gib_per_node": "磁盘4T",
        },
    )

    issues = DeepSeekIntentParser._deterministic_component_audit_issues(
        original,
        filled,
    )

    assert any("5台" in issue and "没有进入" in issue for issue in issues)


def test_plain_machine_count_uses_managed_component_count_field() -> None:
    source = "ElasticSearch 这边预计5台，单台16核128G，磁盘4T。"
    component = ServiceRequirement(
        service="opensearch",
        source_text=source,
        quantity=1,
    )

    DeepSeekIntentParser._overlay_literal_component_facts(source, component)

    assert component.quantity == 1
    assert component.requirements["data_nodes"] == 5
    assert component.field_sources["requirements.data_nodes"] == "customer_text"
    assert component.field_evidence["requirements.data_nodes"] == "5台"


def test_plain_machine_count_uses_top_level_quantity_when_template_has_no_member_count() -> None:
    source = "应用服务器预计3台，单台16核128G，磁盘1T。"
    component = ServiceRequirement(
        service="ec2",
        source_text=source,
        quantity=1,
    )

    DeepSeekIntentParser._overlay_literal_component_facts(source, component)

    assert component.quantity == 3
    assert component.field_sources["quantity"] == "customer_text"
    assert component.field_evidence["quantity"] == "3台"


def test_default_quantity_is_not_marked_as_customer_text() -> None:
    original = ServiceRequirement(service="s3", source_text="S3 存储15TB")
    filled = ServiceRequirement(
        service="s3",
        source_text=original.source_text,
        quantity=1,
        requirements={"storage_gib": 15360},
        field_evidence={"requirements.storage_gib": "15TB"},
    )

    DeepSeekIntentParser._mark_component_field_sources(
        original,
        filled,
        runtime_defaults={},
    )

    assert filled.field_sources["quantity"] == "system_minimum"
    assert "quantity" not in filled.locked_fields
