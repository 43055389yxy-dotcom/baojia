from __future__ import annotations

from app.core.config import Settings
from app.core.errors import ManualConfirmationRequired
from app.domain.component_integrity import overlay_customer_fields
from app.domain.customer_facts import field_scope, record_customer_fact_metadata
from app.domain.fact_ledger import (
    bind_fact_consumptions,
    customer_fact_ledger_is_current,
    customer_quantitative_atoms,
    customer_pricing_fact_records,
    duplicate_customer_fact_ownership,
    finalize_customer_fact_ledger,
    remove_facts_mapped_to_fields,
    selection_fact_contract_violations,
    unconsumed_customer_pricing_facts,
)
from app.domain.models import (
    ParsedIntent,
    SelectedResource,
    ServiceRequirement,
    UnmappedPricingFact,
    UsageLine,
)
from app.domain.structured_component_updates import bind_selected_model_specifications
from app.integrations.deepseek import DeepSeekIntentParser, _official_extraction_contract
from app.services.plugins.generic_official import GenericOfficialPlugin
from app.services.quote_service import QuoteService


def test_adjacent_resource_scope_survives_short_ai_evidence_token() -> None:
    requirement = ServiceRequirement(
        service="dms",
        source_text=(
            "DMS 数据迁移，2个复制实例，单实例4核16G，"
            "磁盘200G，每月运行730小时"
        ),
    )

    record_customer_fact_metadata(requirement, "storage_gib", "磁盘200G")

    assert field_scope(requirement, "storage_gib") == "per_resource"


def test_product_neutral_number_inventory_ignores_names_and_keeps_all_units() -> None:
    source = (
        "Amazon ECS Fargate运行20个Task，单Task 2 vCPU、4G内存，每月730小时；"
        "Kinesis写入15T，保留30天；型号r6g.4xlarge，区域ap-south-1"
    )

    atoms = customer_quantitative_atoms(source)

    assert [(atom.raw, atom.value) for atom in atoms] == [
        ("20个", 20),
        ("2 vCPU", 2),
        ("4G", 4),
        ("730小时", 730),
        ("15T", 15 * 1024),
        ("30天", 30),
    ]


def test_selected_official_model_preserves_original_shape_facts_for_every_service() -> None:
    requirement = ServiceRequirement(
        service="elasticache",
        component_key="cmp_redis_shape_1",
        source_text="Amazon ElastiCache Redis，2个节点，单节点4核16G，1主1从",
        original_source_text="Amazon ElastiCache Redis，2个节点，单节点4核16G，1主1从",
        requirements={
            "vcpu": 4,
            "memory_gib": 16,
            "_review_confirmation_candidates": [
                {
                    "model": "cache.m5.xlarge",
                    "specifications": {"vCPU": 4, "memoryGiB": 12.93},
                }
            ],
        },
        field_sources={
            "requirements.vcpu": "customer_text",
            "requirements.memory_gib": "customer_text",
        },
        field_evidence={
            "requirements.vcpu": "4核16G",
            "requirements.memory_gib": "单节点4核16G",
        },
    )
    finalize_customer_fact_ledger(requirement)

    assert bind_selected_model_specifications(
        requirement, "cache.m5.xlarge"
    )
    assert requirement.requirements["memory_gib"] == 12.93
    assert customer_fact_ledger_is_current(requirement)

    records = {
        record.path: record for record in customer_pricing_fact_records(requirement)
    }
    assert records["requirements.vcpu"].value == 4
    assert records["requirements.vcpu"].evidence == "4核16G"
    assert records["requirements.memory_gib"].value == 16
    assert records["requirements.memory_gib"].evidence == "单节点4核16G"

    selection = SelectedResource(
        service="elasticache",
        display_name="Amazon ElastiCache",
        region="ap-southeast-3",
        model="cache.m5.xlarge",
        architecture="2 nodes",
        specifications={"vCPU": 4, "memoryGiB": 12.93},
        official_product={"source": "AWS"},
        rationale="customer selected official model",
        usage_lines=[
            UsageLine(
                key="redis",
                service_code="AmazonElastiCache",
                usage_type="NodeUsage:cache.m5.xlarge",
                operation="CreateCacheCluster:0002",
                amount=1460,
                source_fields=["vcpu", "memory_gib"],
            )
        ],
    )
    assert unconsumed_customer_pricing_facts(requirement, selection) == []


def test_mapped_top_level_quantity_removes_same_overflow_fact() -> None:
    requirement = ServiceRequirement(
        service="eks",
        quantity=1,
        field_sources={"quantity": "customer_text"},
        field_evidence={"quantity": "集群（EKS），1套"},
        unmapped_pricing_facts=[
            UnmappedPricingFact(
                field_hint="集群套数",
                value=1,
                unit="套",
                scope="aggregate",
                evidence="1套",
            )
        ],
    )

    remove_facts_mapped_to_fields(requirement)

    assert requirement.unmapped_pricing_facts == []


def test_equal_overflow_value_with_distinct_evidence_is_preserved() -> None:
    requirement = ServiceRequirement(
        service="future_service",
        quantity=1,
        field_sources={"quantity": "customer_text"},
        field_evidence={"quantity": "1套部署"},
        unmapped_pricing_facts=[
            UnmappedPricingFact(
                field_hint="并发任务",
                value=1,
                unit="个",
                scope="aggregate",
                evidence="另有1个并发任务",
            )
        ],
    )

    remove_facts_mapped_to_fields(requirement)

    assert len(requirement.unmapped_pricing_facts) == 1


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


def test_official_contract_deduplicates_regional_rows_and_hides_opaque_rows_from_known_templates() -> None:
    profile = {
        "status": "verified",
        "display_name": "Amazon FSx",
        "field_bindings": [
            *(
                {
                    "field": "hours_per_month",
                    "label": "运行时长",
                    "unit": "Hrs",
                    "usage_type": f"Region-{index}-Hours",
                    "operation": "CreateFileSystem",
                }
                for index in range(40)
            ),
            *(
                {
                    "field": f"official_usage_ontap_{index}",
                    "label": "NetApp ONTAP operation",
                    "unit": "Operations",
                    "usage_type": f"ONTAP-{index}",
                    "operation": "CreateFileSystem",
                }
                for index in range(40)
            ),
        ],
    }

    fields, prompt = _official_extraction_contract(
        profile,
        "FSx for NetApp ONTAP，吞吐512MB/s",
        service_key="fsx",
    )

    # Runtime is already a shared top-level field and must not be emitted a
    # second time inside the dynamically discovered requirements template.
    assert fields == ()
    assert "hours_per_month" not in prompt
    assert "official_usage_ontap" not in prompt


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


def test_finalized_fact_ledger_prevents_later_prose_reinterpretation(monkeypatch) -> None:
    source = "EC2 Worker节点4台，单台8核16G，磁盘300G"
    requirement = ServiceRequirement(
        service="ec2",
        component_key="cmp_worker_1234",
        source_text=source,
        requirements={"vcpu": 8, "memory_gib": 16, "system_disk_gib": 300},
        field_sources={
            "quantity": "customer_text",
            "requirements.vcpu": "customer_text",
            "requirements.memory_gib": "customer_text",
            "requirements.system_disk_gib": "customer_text",
        },
        field_evidence={
            "quantity": "4台",
            "requirements.vcpu": "8核16G",
            "requirements.memory_gib": "8核16G",
            "requirements.system_disk_gib": "磁盘300G",
        },
        quantity=4,
    )
    finalize_customer_fact_ledger(requirement)
    intent = ParsedIntent(customer_summary=source, services=[requirement])

    assert customer_fact_ledger_is_current(requirement)
    monkeypatch.setattr(
        DeepSeekIntentParser,
        "_overlay_literal_component_facts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("finalized facts must not parse prose again")
        ),
    )

    DeepSeekIntentParser.reconcile_customer_pricing_facts(intent)


def test_finalized_fact_ledger_prevents_quote_boundary_prose_reinterpretation(
    monkeypatch,
) -> None:
    source = "EC2 2台，单台4核16G"
    requirement = ServiceRequirement(
        service="ec2",
        component_key="cmp_quote_1234",
        source_text=source,
        quantity=2,
        requirements={"vcpu": 4, "memory_gib": 16},
        field_sources={
            "quantity": "customer_text",
            "requirements.vcpu": "customer_text",
            "requirements.memory_gib": "customer_text",
        },
        field_evidence={
            "quantity": "2台",
            "requirements.vcpu": "4核",
            "requirements.memory_gib": "16G",
        },
    )
    finalize_customer_fact_ledger(requirement)
    intent = ParsedIntent(customer_summary=source, services=[requirement])
    monkeypatch.setattr(
        DeepSeekIntentParser,
        "_uncovered_quantitative_claim_issues",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("quote boundary must consume the finalized table")
        ),
    )

    QuoteService._require_complete_literal_fact_coverage(intent)


def test_fact_consumption_allows_one_fact_to_drive_selection_and_billing() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        component_key="cmp_usage_1234",
        source_text="EC2 3台",
        quantity=3,
        field_sources={"quantity": "customer_text"},
        field_evidence={"quantity": "3台"},
    )
    finalize_customer_fact_ledger(requirement)
    selection = SelectedResource(
        service="ec2",
        display_name="Amazon EC2",
        region="ap-south-1",
        model="m7g.large",
        architecture="arm64",
        specifications={},
        official_product={"source": "AWS"},
        rationale="test",
        applied_requirement_fields=["quantity"],
        usage_lines=[
            UsageLine(
                key="ec2",
                service_code="AmazonEC2",
                usage_type="BoxUsage",
                operation="RunInstances",
                amount=3 * 730,
                source_fields=["quantity"],
            )
        ],
    )

    bind_fact_consumptions(requirement, selection)

    assert [item.consumer_type for item in selection.fact_consumptions] == [
        "selection",
        "usage_line",
    ]
    assert len({item.fact_id for item in selection.fact_consumptions}) == 1
    assert selection.usage_lines[0].source_fact_ids == [
        selection.fact_consumptions[0].fact_id
    ]
    assert unconsumed_customer_pricing_facts(requirement, selection) == []


def test_fact_binding_rebuilds_ids_and_never_trusts_stale_adapter_ids() -> None:
    requirement = ServiceRequirement(
        service="s3",
        component_key="cmp_storage_1234",
        source_text="S3 15T",
        requirements={"storage_gib": 15360},
        field_sources={"requirements.storage_gib": "customer_text"},
        field_evidence={"requirements.storage_gib": "15T"},
    )
    finalize_customer_fact_ledger(requirement)
    line = UsageLine(
        key="s3",
        service_code="AmazonS3",
        usage_type="TimedStorage-ByteHrs",
        operation="",
        amount=15360,
        source_fields=["storage_gib"],
        source_fact_ids=["fact_000000000000000000000000"],
    )
    selection = SelectedResource(
        service="s3",
        display_name="Amazon S3",
        region="ap-south-1",
        model="S3 Standard",
        architecture="managed",
        specifications={},
        official_product={},
        rationale="test",
        usage_lines=[line],
    )

    assert selection_fact_contract_violations(requirement, selection) == {}
    assert line.source_fact_ids == [requirement.customer_pricing_facts[0].fact_id]


def test_fact_ledger_normalizes_all_system_derived_fields_as_unlocked() -> None:
    requirement = ServiceRequirement(
        service="mq",
        component_key="cmp_mq_derived_1",
        source_text="3个Broker，每个500G",
        requirements={
            "broker_count": 3,
            "storage_gib_per_broker": 500,
            "total_storage_gib": 1500,
        },
        field_sources={
            "requirements.broker_count": "customer_text",
            "requirements.storage_gib_per_broker": "customer_text",
            "requirements.total_storage_gib": "customer_text",
        },
        field_evidence={
            "requirements.broker_count": "3个Broker",
            "requirements.storage_gib_per_broker": "每个500G",
            "requirements.total_storage_gib": "system_derived",
        },
        locked_fields=[
            "requirements.broker_count",
            "requirements.storage_gib_per_broker",
            "requirements.total_storage_gib",
        ],
    )

    finalize_customer_fact_ledger(requirement)

    assert requirement.field_sources["requirements.total_storage_gib"] == "system_derived"
    assert "requirements.total_storage_gib" not in requirement.locked_fields
    assert {fact.path for fact in requirement.customer_pricing_facts} == {
        "requirements.broker_count",
        "requirements.storage_gib_per_broker",
    }


def test_contract_rejects_locked_derived_fact_for_every_component_type() -> None:
    requirement = ServiceRequirement(
        service="future_service",
        component_key="cmp_future_1234",
        source_text="总量30",
        requirements={"total_units": 30},
        field_sources={"requirements.total_units": "system_derived"},
        field_evidence={"requirements.total_units": "system_derived"},
        locked_fields=["requirements.total_units"],
    )
    selection = SelectedResource(
        service="future_service",
        display_name="Future Service",
        region="ap-south-1",
        model="official",
        architecture="managed",
        specifications={},
        official_product={},
        rationale="test",
    )

    assert selection_fact_contract_violations(requirement, selection) == {
        "locked_system_derived_facts": ["requirements.total_units"]
    }


def test_fact_table_records_unit_scope_evidence_and_component_owner() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        component_key="cmp_worker_5678",
        source_text="EC2 Worker节点4台，单台8核16G",
        quantity=4,
        requirements={"vcpu": 8, "memory_gib": 16},
        field_sources={
            "_source_block_key": "src_block_1",
            "quantity": "customer_text",
            "requirements.vcpu": "customer_text",
            "requirements.memory_gib": "customer_text",
        },
        field_evidence={
            "quantity": "4台",
            "requirements.vcpu": "8核16G",
            "requirements.memory_gib": "8核16G",
        },
        field_scopes={"vcpu": "per_node", "memory_gib": "per_node"},
    )

    records = customer_pricing_fact_records(requirement)

    assert [(record.path, record.unit, record.scope) for record in records] == [
        ("quantity", "台", "component_total"),
        ("requirements.memory_gib", "G", "per_node"),
        ("requirements.vcpu", "vCPU", "per_node"),
    ]
    assert all(record.component_key == "cmp_worker_5678" for record in records)
    assert all(record.source_block_key == "src_block_1" for record in records)


def test_same_source_fact_owned_by_two_components_is_rejected() -> None:
    def component(key: str, block: str) -> ServiceRequirement:
        return ServiceRequirement(
            service="ecs",
            component_key=key,
            source_text="Amazon ECS，1套集群",
            requirements={"cluster_count": 1},
            field_sources={
                "_source_block_key": block,
                "requirements.cluster_count": "customer_text",
            },
            field_evidence={"requirements.cluster_count": "1套集群"},
        )

    duplicates = duplicate_customer_fact_ownership(
        [component("cmp_ecs_1111", "src_same"), component("cmp_ecs_2222", "src_same")]
    )
    separate = duplicate_customer_fact_ownership(
        [component("cmp_ecs_1111", "src_one"), component("cmp_ecs_2222", "src_two")]
    )

    assert len(duplicates) == 1
    assert separate == []


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


def test_ledger_upgrade_rebinds_plain_count_after_managed_identity_resolution() -> None:
    """A pre-canonical ledger must not freeze a managed node count as missing."""

    source = "ElasticSearch，5台，单台16核128G，磁盘4T"
    component = ServiceRequirement(
        service="opensearch",
        source_text=source,
        quantity=1,
        requirements={
            "vcpu": 16,
            "memory_gib": 128,
            "storage_gib_per_node": 4096,
        },
        field_sources={
            "requirements.vcpu": "customer_text",
            "requirements.memory_gib": "customer_text",
            "requirements.storage_gib_per_node": "customer_text",
        },
        field_evidence={
            "requirements.vcpu": "16核128G",
            "requirements.memory_gib": "16核128G",
            "requirements.storage_gib_per_node": "单台16核128G，磁盘4T",
        },
        # Version 2 could be internally self-consistent while still missing
        # the count because identity resolution happened after extraction.
        fact_ledger_version=2,
    )
    intent = ParsedIntent(customer_summary=source, services=[component])

    DeepSeekIntentParser.reconcile_customer_pricing_facts(intent)

    repaired = intent.services[0]
    assert repaired.quantity == 1
    assert repaired.requirements["data_nodes"] == 5
    assert repaired.field_evidence["requirements.data_nodes"] == "5台"
    assert any(
        fact.path == "requirements.data_nodes" and fact.value == 5
        for fact in repaired.customer_pricing_facts
    )


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
