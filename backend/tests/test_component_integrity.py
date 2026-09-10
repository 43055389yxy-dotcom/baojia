from app.domain.component_integrity import (
    capture_customer_ledger,
    deduplicate_derived_components,
    enforce_component_integrity,
    ensure_component_keys,
    restore_customer_ledger,
)
from app.domain.models import ParsedIntent, ServiceRequirement
from app.domain.models import UnmappedPricingFact
from app.integrations.deepseek import DeepSeekIntentParser


def test_customer_fields_restore_by_component_key_after_reorder() -> None:
    intent = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="rds",
                component_key="cmp_rds_1234",
                requirements={"engine_version": "8.4.11", "storage_gib": 500},
                field_sources={
                    "requirements.engine_version": "customer_confirmation",
                    "requirements.storage_gib": "customer_text",
                },
            ),
            ServiceRequirement(
                service="s3",
                component_key="cmp_s3_12345",
                requirements={"storage_gib": 8000},
                field_sources={"requirements.storage_gib": "customer_confirmation"},
            ),
        ],
    )
    ledger = capture_customer_ledger(intent)
    intent.services.reverse()
    intent.services[0].requirements["storage_gib"] = 1
    intent.services[1].requirements = {"engine_version": "5.7.44"}

    restore_customer_ledger(intent, ledger)

    by_key = {item.component_key: item for item in intent.services}
    assert by_key["cmp_rds_1234"].requirements == {
        "engine_version": "8.4.11",
        "storage_gib": 500,
    }
    assert by_key["cmp_s3_12345"].requirements["storage_gib"] == 8000


def test_explicit_customer_removal_cannot_be_resurrected() -> None:
    intent = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="ec2",
                component_key="cmp_ec2_1234",
                requirements={},
                field_sources={
                    "requirements.system_disk_gib": "customer_confirmation_removed"
                },
                locked_fields=["requirements.system_disk_gib"],
            )
        ],
    )
    ledger = capture_customer_ledger(intent)
    intent.services[0].requirements["system_disk_gib"] = 40

    restore_customer_ledger(intent, ledger)

    assert "system_disk_gib" not in intent.services[0].requirements


def test_component_keys_are_stable_across_edit_annotations() -> None:
    original = ParsedIntent(
        customer_summary="x",
        services=[ServiceRequirement(service="s3", source_text="3、S3：存储8000GB")],
    )
    edited = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="s3",
                source_text="客户通过配置表直接修改：storage_gib\n3、S3：存储8000GB",
            )
        ],
    )

    ensure_component_keys(original)
    ensure_component_keys(edited)

    assert original.services[0].component_key == edited.services[0].component_key
    assert edited.services[0].original_source_text == "3、S3：存储8000GB"


def test_generic_derived_component_deduplication_preserves_customer_override() -> None:
    source = "新产品：需要一个独立计费子资源，容量100GB"
    intent = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="new_parent",
                component_key="cmp_parent_new1",
                source_text=source,
            ),
            ServiceRequirement(
                service="new_child",
                component_key="cmp_child_edit1",
                parent_component_key="cmp_parent_new1",
                derived_from_service="new_parent",
                source_text=f"客户通过配置表直接修改：storage_gib\n{source}",
                requirements={"storage_gib": 500},
                field_sources={"requirements.storage_gib": "customer_confirmation"},
            ),
            ServiceRequirement(
                service="new_child",
                component_key="cmp_child_auto1",
                parent_component_key="cmp_parent_new1",
                derived_from_service="new_parent",
                source_text=source,
                requirements={"storage_gib": 100, "requests": 2000},
                field_sources={"requirements.storage_gib": "customer_text"},
            ),
        ],
    )

    deduplicate_derived_components(intent)

    children = [item for item in intent.services if item.service == "new_child"]
    assert len(children) == 1
    assert children[0].requirements["storage_gib"] == 500
    assert children[0].requirements["requests"] == 2000
    assert children[0].field_sources["requirements.storage_gib"] == (
        "customer_confirmation"
    )


def test_customer_fields_survive_reorder_for_unrelated_service_families() -> None:
    cases = [
        ("rds", "engine_version", "8.4.11"),
        ("ec2", "system_disk_gib", 1000),
        ("s3", "storage_gib", 8000),
        ("elasticache", "memory_gib", 20),
        ("elb", "requests", 50_000_000),
        ("future_official_product", "collector_hours", 744),
    ]
    intent = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service=service,
                source_text=f"{index}、{service} {field}={value}",
                requirements={field: value},
                field_sources={f"requirements.{field}": "customer_confirmation"},
                locked_fields=[f"requirements.{field}"],
            )
            for index, (service, field, value) in enumerate(cases, start=1)
        ],
    )
    enforce_component_integrity(intent)
    ledger = capture_customer_ledger(intent)
    expected = {
        item.component_key: dict(item.requirements) for item in intent.services
    }

    intent.services.reverse()
    for item in intent.services:
        item.requirements.clear()
    restore_customer_ledger(intent, ledger)

    assert {
        item.component_key: item.requirements for item in intent.services
    } == expected


def test_pre_split_ledger_cannot_overwrite_parent_child_source_ownership() -> None:
    """A whole-row AI result must not contaminate either side after splitting."""

    source = (
        "Kubernetes 集群（EKS），1套，Worker 节点4台，"
        "单台8核32G，磁盘400G"
    )
    intent = ParsedIntent(
        customer_summary=source,
        services=[
            ServiceRequirement(
                service="eks",
                source_text=source,
                original_source_text=source,
                quantity=4,
                requirements={
                    "vcpu": 8,
                    "memory_gib": 32,
                    "system_disk_gib": 400,
                },
                field_sources={
                    "quantity": "customer_text",
                    "requirements.vcpu": "customer_text",
                    "requirements.memory_gib": "customer_text",
                    "requirements.system_disk_gib": "customer_text",
                },
                field_evidence={
                    "quantity": "Worker 节点4台",
                    "requirements.vcpu": "单台8核",
                    "requirements.memory_gib": "32G",
                    "requirements.system_disk_gib": "磁盘400G",
                },
                unmapped_pricing_facts=[
                    UnmappedPricingFact(
                        field_hint="集群套数",
                        value=1,
                        unit="set",
                        scope="aggregate",
                        evidence="1套",
                    )
                ],
            )
        ],
    )
    whole_row_ledger = capture_customer_ledger(intent)

    DeepSeekIntentParser._split_eks_worker_nodes(intent)
    restore_customer_ledger(
        intent, whole_row_ledger, restore_missing_components=False
    )
    DeepSeekIntentParser.reconcile_customer_pricing_facts(intent)

    parent = next(item for item in intent.services if item.service == "eks")
    worker = next(item for item in intent.services if item.service == "ec2")
    assert parent.quantity == 1
    assert parent.requirements["cluster_count"] == 1
    assert parent.field_evidence["quantity"].endswith("1套")
    assert "vcpu" not in parent.requirements
    assert "memory_gib" not in parent.requirements
    assert "system_disk_gib" not in parent.requirements
    assert worker.quantity == 4
    assert worker.requirements["vcpu"] == 8
    assert worker.requirements["memory_gib"] == 32
    assert worker.requirements["system_disk_gib"] == 400
    assert DeepSeekIntentParser._incomplete_fact_ledger_components(intent) == []


def test_current_cleaner_output_cannot_cross_owned_source_boundary() -> None:
    item = ServiceRequirement(
        service="future_parent",
        source_text="未来集群，1套；工作节点6台，单台8核32G",
        original_source_text="未来集群，1套；工作节点6台，单台8核32G",
        quantity=6,
        requirements={"cluster_count": 1, "worker_vcpu": 8, "memory_gib": 32},
        field_sources={
            "_owned_source_slice": "system_policy",
            "quantity": "customer_text",
            "requirements.cluster_count": "customer_text",
            "requirements.worker_vcpu": "customer_text",
            "requirements.memory_gib": "customer_confirmation",
        },
        field_evidence={
            "_owned_source_slice_text": "未来集群，1套",
            "quantity": "工作节点6台",
            "requirements.cluster_count": "1套",
            "requirements.worker_vcpu": "单台8核",
            "requirements.memory_gib": "客户确认改为32G",
        },
        locked_fields=[
            "quantity",
            "requirements.cluster_count",
            "requirements.worker_vcpu",
            "requirements.memory_gib",
        ],
    )
    intent = ParsedIntent(customer_summary="x", services=[item])

    enforce_component_integrity(intent)

    assert item.quantity == 1
    assert item.requirements["cluster_count"] == 1
    assert "worker_vcpu" not in item.requirements
    # An explicit confirmation is a later structured override and is not
    # invalidated merely because its wording is absent from the original slice.
    assert item.requirements["memory_gib"] == 32
    assert "quantity" not in item.locked_fields
    assert "requirements.worker_vcpu" not in item.locked_fields
