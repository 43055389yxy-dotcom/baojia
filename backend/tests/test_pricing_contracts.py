from __future__ import annotations

import pytest

from app.domain.models import ServiceRequirement
from app.domain.pricing_contracts import apply_pricing_contract


def test_aurora_removes_system_owned_ebs_defaults() -> None:
    requirement = ServiceRequirement(
        service="rds",
        requirements={
            "engine": "aurora_mysql",
            "storage_gib": 2048,
            "storage_type": "gp3",
            "system_disk_gib": 100,
        },
        field_sources={
            "requirements.storage_type": "system_default",
            "requirements.system_disk_gib": "system_default",
        },
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert requirement.requirements == {
        "engine": "aurora_mysql",
        "storage_gib": 2048,
    }


def test_product_role_count_owns_same_evidence_instead_of_generic_quantity() -> None:
    requirement = ServiceRequirement(
        service="waf",
        quantity=2,
        requirements={"web_acls": 2, "rules": 30, "requests": 3_000_000_000},
        field_sources={
            "quantity": "customer_text",
            "requirements.web_acls": "customer_text",
            "requirements.rules": "customer_text",
            "requirements.requests": "customer_text",
        },
        field_evidence={
            "quantity": "2个Web ACL",
            "requirements.web_acls": "2个Web ACL",
            "requirements.rules": "每个30条规则",
            "requirements.requests": "每月合计检查30亿次请求",
        },
        field_scopes={
            "web_acls": "component_total",
            "rules": "per_resource",
            "requests": "aggregate",
        },
        locked_fields=[
            "quantity",
            "requirements.web_acls",
            "requirements.rules",
            "requirements.requests",
        ],
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert requirement.quantity == 2
    assert requirement.requirements["web_acls"] == 2
    assert requirement.field_sources["quantity"] == "system_derived"
    assert requirement.field_evidence["quantity"] == "system_derived"
    assert "quantity" not in requirement.locked_fields
    assert requirement.field_sources["requirements.web_acls"] == "customer_text"


def test_aurora_rejects_customer_requested_gp3_instead_of_silently_pricing_it() -> None:
    requirement = ServiceRequirement(
        service="rds",
        requirements={"engine": "aurora_mysql", "storage_type": "gp3"},
        field_sources={"requirements.storage_type": "customer_text"},
        field_evidence={"requirements.storage_type": "磁盘类型gp3"},
        locked_fields=["requirements.storage_type"],
    )

    issues = apply_pricing_contract(requirement)

    assert len(issues) == 1
    assert issues[0].field == "storage_type"
    assert "Aurora" in issues[0].message


def test_elasticache_moves_legacy_source_disk_to_non_billable_context() -> None:
    requirement = ServiceRequirement(
        service="elasticache",
        requirements={"engine": "redis", "storage_gib": 500},
        field_sources={"requirements.storage_gib": "customer_text"},
        field_evidence={"requirements.storage_gib": "每节点存储500G"},
        locked_fields=["requirements.storage_gib"],
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert "storage_gib" not in requirement.requirements
    assert requirement.requirements["source_storage_gib_per_node"] == 500
    assert (
        requirement.field_sources["requirements.source_storage_gib_per_node"]
        == "customer_text"
    )


def test_ec2_plain_disk_defaults_to_gp3_without_claiming_customer_source() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        requirements={"system_disk_gib": 500},
        field_sources={"requirements.system_disk_gib": "customer_text"},
        field_evidence={"requirements.system_disk_gib": "磁盘500G"},
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert requirement.requirements["volume_type"] == "gp3"
    assert requirement.field_sources["requirements.volume_type"] == "system_default"


def test_ec2_collapses_one_unqualified_disk_fact_before_pricing() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        quantity=3,
        requirements={
            "system_disk_gib": 2048,
            "additional_ebs_volumes": [
                {"size_gib": 2048, "volume_type": "gp3", "count_per_instance": 1}
            ],
        },
        field_sources={
            "requirements.system_disk_gib": "customer_text",
            "requirements.additional_ebs_volumes": "customer_text",
        },
        field_evidence={
            "requirements.system_disk_gib": "单节点16核64G，每节点存储2T",
            "requirements.additional_ebs_volumes": "每节点存储2T",
        },
        locked_fields=[
            "requirements.system_disk_gib",
            "requirements.additional_ebs_volumes",
        ],
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert requirement.requirements["system_disk_gib"] == 2048
    assert "additional_ebs_volumes" not in requirement.requirements
    assert "requirements.additional_ebs_volumes" not in requirement.field_sources
    assert "requirements.additional_ebs_volumes" not in requirement.field_evidence
    assert "requirements.additional_ebs_volumes" not in requirement.locked_fields
    assert requirement.quantity * requirement.requirements["system_disk_gib"] == 6144


def test_ec2_preserves_explicit_system_and_data_disks_of_the_same_size() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        requirements={
            "system_disk_gib": 500,
            "additional_ebs_volumes": [
                {"size_gib": 500, "volume_type": "gp3", "count_per_instance": 1}
            ],
        },
        field_sources={
            "requirements.system_disk_gib": "customer_text",
            "requirements.additional_ebs_volumes": "customer_text",
        },
        field_evidence={
            "requirements.system_disk_gib": "每台系统盘500G",
            "requirements.additional_ebs_volumes": "每台数据盘500G",
        },
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert requirement.requirements["system_disk_gib"] == 500
    assert requirement.requirements["additional_ebs_volumes"] == [
        {"size_gib": 500, "volume_type": "gp3", "count_per_instance": 1}
    ]


def test_ec2_explicit_data_disk_wins_over_duplicate_generic_system_disk() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        requirements={
            "system_disk_gib": 500,
            "additional_ebs_volumes": [
                {"size_gib": 500, "volume_type": "gp3", "count_per_instance": 1}
            ],
        },
        field_sources={
            "requirements.system_disk_gib": "customer_text",
            "requirements.additional_ebs_volumes": "customer_text",
        },
        field_evidence={
            "requirements.system_disk_gib": "单台8核32G，数据盘500G",
            "requirements.additional_ebs_volumes": "数据盘500G",
        },
        locked_fields=[
            "requirements.system_disk_gib",
            "requirements.additional_ebs_volumes",
        ],
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert "system_disk_gib" not in requirement.requirements
    assert requirement.requirements["additional_ebs_volumes"] == [
        {"size_gib": 500, "volume_type": "gp3", "count_per_instance": 1}
    ]


def test_ec2_collapses_matching_per_instance_and_total_system_disk() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        quantity=3,
        requirements={"system_disk_gib": 2048, "total_system_disk_gib": 6144},
        field_sources={
            "requirements.system_disk_gib": "customer_text",
            "requirements.total_system_disk_gib": "system_derived",
        },
        field_evidence={
            "requirements.system_disk_gib": "3台，每台存储2T",
            "requirements.total_system_disk_gib": "system_derived",
        },
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert requirement.requirements["system_disk_gib"] == 2048
    assert "total_system_disk_gib" not in requirement.requirements


def test_ec2_rejects_conflicting_per_instance_and_total_system_disk() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        quantity=3,
        requirements={"system_disk_gib": 2048, "total_system_disk_gib": 12288},
        field_sources={
            "requirements.system_disk_gib": "customer_text",
            "requirements.total_system_disk_gib": "customer_text",
        },
        field_evidence={
            "requirements.system_disk_gib": "每台存储2T",
            "requirements.total_system_disk_gib": "总存储12T",
        },
    )

    issues = apply_pricing_contract(requirement)

    assert len(issues) == 1
    assert issues[0].field == "total_system_disk_gib"
    assert "总量" in issues[0].message


def test_ec2_collapses_matching_per_instance_and_total_transfer() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        quantity=4,
        requirements={
            "data_transfer_out_gib_per_instance": 100,
            "data_transfer_out_gib": 400,
        },
        field_sources={
            "requirements.data_transfer_out_gib_per_instance": "customer_text",
            "requirements.data_transfer_out_gib": "system_derived",
        },
        field_evidence={
            "requirements.data_transfer_out_gib_per_instance": "每台出站100G",
            "requirements.data_transfer_out_gib": "system_derived",
        },
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert requirement.requirements["data_transfer_out_gib_per_instance"] == 100
    assert "data_transfer_out_gib" not in requirement.requirements


def test_monthly_runtime_has_one_top_level_fact_owner() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        hours_per_month=300,
        requirements={"vcpu": 8, "hours_per_month": 300},
        field_sources={
            "hours_per_month": "customer_text",
            "requirements.hours_per_month": "customer_text",
        },
        field_evidence={
            "hours_per_month": "每月运行300小时",
            "requirements.hours_per_month": "每月运行300小时",
        },
        locked_fields=["hours_per_month", "requirements.hours_per_month"],
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert requirement.hours_per_month == 300
    assert "hours_per_month" not in requirement.requirements
    assert requirement.locked_fields == ["hours_per_month"]


def test_legacy_nested_runtime_moves_to_top_level_before_ledger_is_sealed() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        requirements={"hours_per_month": 300},
        field_sources={"requirements.hours_per_month": "customer_text"},
        field_evidence={"requirements.hours_per_month": "每月运行300小时"},
        locked_fields=["requirements.hours_per_month"],
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert requirement.hours_per_month == 300
    assert requirement.field_sources["hours_per_month"] == "customer_text"
    assert requirement.field_evidence["hours_per_month"] == "每月运行300小时"
    assert "hours_per_month" not in requirement.requirements


def test_s3_cannot_inherit_ec2_compute_fields() -> None:
    requirement = ServiceRequirement(
        service="s3",
        requirements={"storage_gib": 15360, "vcpu": 16},
        field_sources={"requirements.vcpu": "customer_text"},
        field_evidence={"requirements.vcpu": "16核"},
    )

    issues = apply_pricing_contract(requirement)

    assert len(issues) == 1
    assert issues[0].field == "vcpu"
    assert requirement.requirements["storage_gib"] == 15360


@pytest.mark.parametrize("service", ["backup", "aws_backup", "amazon_backup"])
def test_backup_collapses_same_capacity_into_one_backup_storage_fact(service: str) -> None:
    requirement = ServiceRequirement(
        service=service,
        requirements={
            "storage_gib": 5120,
            "backup_storage_gib": 5120,
            "backup_retention_days": 30,
        },
        field_sources={
            "requirements.storage_gib": "customer_text",
            "requirements.backup_storage_gib": "customer_text",
            "requirements.backup_retention_days": "customer_text",
        },
        field_evidence={
            "requirements.storage_gib": "备份数据容量5T",
            "requirements.backup_storage_gib": "备份数据容量5T",
            "requirements.backup_retention_days": "保留30天",
        },
        locked_fields=[
            "requirements.storage_gib",
            "requirements.backup_storage_gib",
            "requirements.backup_retention_days",
        ],
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert requirement.requirements == {
        "backup_storage_gib": 5120,
        "backup_retention_days": 30,
    }
    assert "requirements.storage_gib" not in requirement.locked_fields


def test_backup_rejects_two_different_customer_storage_meanings() -> None:
    requirement = ServiceRequirement(
        service="backup",
        requirements={"storage_gib": 10240, "backup_storage_gib": 5120},
        field_sources={
            "requirements.storage_gib": "customer_text",
            "requirements.backup_storage_gib": "customer_text",
        },
        field_evidence={
            "requirements.storage_gib": "受保护数据10T",
            "requirements.backup_storage_gib": "实际备份存储5T",
        },
    )

    issues = apply_pricing_contract(requirement)

    assert len(issues) == 1
    assert issues[0].field == "storage_gib"
    assert "避免重复计费" in issues[0].message


@pytest.mark.parametrize(
    ("service", "requirements", "evidence", "removed"),
    [
        (
            "cloudwatch",
            {
                "log_ingestion_gib": 500,
                "data_in_gib": 500,
                "log_storage_gib": 1024,
                "storage_gib": 1024,
            },
            {
                "log_ingestion_gib": "每月写入500G",
                "data_in_gib": "每月写入500G",
                "log_storage_gib": "存储1T",
                "storage_gib": "存储1T",
            },
            {"data_in_gib", "storage_gib"},
        ),
        (
            "cloudfront",
            {"https_requests": 80_000_000, "requests": 80_000_000},
            {
                "https_requests": "HTTPS请求8000万次",
                "requests": "每月HTTPS请求8000万次",
            },
            {"requests"},
        ),
        (
            "s3",
            {
                "put_copy_post_list_requests": 5_000_000,
                "get_select_requests": 50_000_000,
                "requests": 5_000_000,
            },
            {
                "put_copy_post_list_requests": "每月上传请求500万次",
                "get_select_requests": "下载请求5000万次",
                "requests": "每月上传请求500万次",
            },
            {"requests"},
        ),
    ],
)
def test_product_contract_collapses_ai_aliases_from_the_same_customer_fact(
    service: str,
    requirements: dict[str, float],
    evidence: dict[str, str],
    removed: set[str],
) -> None:
    requirement = ServiceRequirement(
        service=service,
        requirements=requirements,
        field_sources={
            f"requirements.{field}": "customer_text" for field in requirements
        },
        field_evidence={
            f"requirements.{field}": value for field, value in evidence.items()
        },
        locked_fields=[f"requirements.{field}" for field in requirements],
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    for field in removed:
        assert field not in requirement.requirements
        assert f"requirements.{field}" not in requirement.field_sources
        assert f"requirements.{field}" not in requirement.field_evidence
        assert f"requirements.{field}" not in requirement.locked_fields


def test_equal_numbers_with_different_evidence_remain_separate_facts() -> None:
    requirement = ServiceRequirement(
        service="s3",
        requirements={
            "put_copy_post_list_requests": 5_000_000,
            "requests": 5_000_000,
        },
        field_sources={
            "requirements.put_copy_post_list_requests": "customer_text",
            "requirements.requests": "customer_text",
        },
        field_evidence={
            "requirements.put_copy_post_list_requests": "上传请求500万次",
            "requirements.requests": "另有批处理请求500万次",
        },
    )

    apply_pricing_contract(requirement)

    assert "requests" in requirement.requirements


@pytest.mark.parametrize(
    ("service", "alias_field", "canonical_field", "value", "evidence"),
    [
        ("msk", "storage_gib", "storage_gib_per_broker", 500, "每个 Broker 存储500G"),
        ("mq", "storage_gib", "storage_gib_per_broker", 300, "每个 Broker 存储300G"),
        (
            "opensearch",
            "storage_gib",
            "storage_gib_per_node",
            800,
            "每个数据节点存储800G",
        ),
    ],
)
def test_topology_storage_alias_has_one_customer_fact_owner(
    service: str,
    alias_field: str,
    canonical_field: str,
    value: float,
    evidence: str,
) -> None:
    requirement = ServiceRequirement(
        service=service,
        requirements={alias_field: value, canonical_field: value},
        field_sources={
            f"requirements.{alias_field}": "customer_text",
            f"requirements.{canonical_field}": "customer_text",
        },
        field_evidence={
            f"requirements.{alias_field}": evidence,
            f"requirements.{canonical_field}": evidence,
        },
        locked_fields=[
            f"requirements.{alias_field}",
            f"requirements.{canonical_field}",
        ],
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert requirement.requirements[canonical_field] == value
    assert alias_field not in requirement.requirements
    assert f"requirements.{alias_field}" not in requirement.field_sources
    assert f"requirements.{alias_field}" not in requirement.field_evidence
    assert f"requirements.{alias_field}" not in requirement.locked_fields


@pytest.mark.parametrize(
    ("service", "quantity", "requirements", "per_field"),
    [
        (
            "msk",
            2,
            {
                "broker_count": 3,
                "storage_gib_per_broker": 500,
                "total_storage_gib": 3000,
            },
            "storage_gib_per_broker",
        ),
        (
            "mq",
            1,
            {
                "broker_count": 3,
                "storage_gib_per_broker": 300,
                "total_storage_gib": 900,
            },
            "storage_gib_per_broker",
        ),
        (
            "opensearch",
            1,
            {
                "data_nodes": 3,
                "storage_gib_per_node": 800,
                "total_storage_gib": 2400,
            },
            "storage_gib_per_node",
        ),
        (
            "ebs",
            4,
            {"storage_gib": 250, "total_storage_gib": 1000},
            "storage_gib",
        ),
    ],
)
def test_topology_storage_removes_system_derived_total(
    service: str,
    quantity: int,
    requirements: dict[str, float],
    per_field: str,
) -> None:
    requirement = ServiceRequirement(
        service=service,
        quantity=quantity,
        requirements=requirements,
        field_sources={
            f"requirements.{per_field}": "customer_text",
            "requirements.total_storage_gib": "system_derived",
        },
        field_evidence={
            f"requirements.{per_field}": "客户给出的单资源容量",
            "requirements.total_storage_gib": "system_derived",
        },
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert per_field in requirement.requirements
    assert "total_storage_gib" not in requirement.requirements


@pytest.mark.parametrize(
    ("service", "quantity", "requirements", "per_field"),
    [
        (
            "msk",
            1,
            {
                "broker_count": 3,
                "storage_gib_per_broker": 500,
                "total_storage_gib": 1000,
            },
            "storage_gib_per_broker",
        ),
        (
            "opensearch",
            1,
            {
                "data_nodes": 3,
                "storage_gib_per_node": 800,
                "total_storage_gib": 1600,
            },
            "storage_gib_per_node",
        ),
        (
            "ebs",
            4,
            {"storage_gib": 250, "total_storage_gib": 750},
            "storage_gib",
        ),
    ],
)
def test_topology_storage_rejects_conflicting_customer_total(
    service: str,
    quantity: int,
    requirements: dict[str, float],
    per_field: str,
) -> None:
    requirement = ServiceRequirement(
        service=service,
        quantity=quantity,
        requirements=requirements,
        field_sources={
            f"requirements.{per_field}": "customer_text",
            "requirements.total_storage_gib": "customer_text",
        },
        field_evidence={
            f"requirements.{per_field}": "客户给出的单资源容量",
            "requirements.total_storage_gib": "客户给出的总容量",
        },
    )

    issues = apply_pricing_contract(requirement)

    assert len(issues) == 1
    assert issues[0].field == "total_storage_gib"
    assert "总量不一致" in issues[0].message


@pytest.mark.parametrize(
    ("service", "quantity", "requirements", "per_field"),
    [
        (
            "msk",
            1,
            {
                "broker_count": 3,
                "storage_gib_per_broker": 500,
                "total_storage_gib": 1500,
            },
            "storage_gib_per_broker",
        ),
        (
            "opensearch",
            1,
            {
                "data_nodes": 3,
                "storage_gib_per_node": 800,
                "total_storage_gib": 2400,
            },
            "storage_gib_per_node",
        ),
        (
            "ebs",
            4,
            {"storage_gib": 250, "total_storage_gib": 1000},
            "storage_gib",
        ),
    ],
)
def test_customer_total_can_keep_a_matching_system_derived_per_unit_helper(
    service: str,
    quantity: int,
    requirements: dict[str, float],
    per_field: str,
) -> None:
    requirement = ServiceRequirement(
        service=service,
        quantity=quantity,
        requirements=requirements,
        field_sources={
            f"requirements.{per_field}": "system_derived",
            "requirements.total_storage_gib": "customer_text",
        },
        field_evidence={
            f"requirements.{per_field}": "system_derived",
            "requirements.total_storage_gib": "客户给出的总容量",
        },
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert per_field in requirement.requirements
    assert requirement.field_sources[f"requirements.{per_field}"] == "system_derived"
    assert requirement.requirements["total_storage_gib"] == requirements[
        "total_storage_gib"
    ]


def test_stale_system_derived_per_unit_is_recalculated_from_customer_total() -> None:
    requirement = ServiceRequirement(
        service="msk",
        requirements={
            "broker_count": 3,
            "storage_gib_per_broker": 400,
            "total_storage_gib": 1500,
        },
        field_sources={
            "requirements.storage_gib_per_broker": "system_derived",
            "requirements.total_storage_gib": "customer_text",
        },
        field_evidence={
            "requirements.storage_gib_per_broker": "system_derived",
            "requirements.total_storage_gib": "总存储1500G",
        },
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert requirement.requirements["storage_gib_per_broker"] == 500
    assert (
        requirement.field_sources["requirements.storage_gib_per_broker"]
        == "system_derived"
    )
    assert requirement.requirements["total_storage_gib"] == 1500


@pytest.mark.parametrize(
    ("service", "count_field"),
    [("eks", "cluster_count"), ("nat_gateway", "gateway_count")],
)
def test_deployable_service_count_has_one_canonical_fact_owner(
    service: str,
    count_field: str,
) -> None:
    requirement = ServiceRequirement(
        service=service,
        quantity=1,
        requirements={count_field: 2},
        field_sources={f"requirements.{count_field}": "customer_text"},
        field_evidence={f"requirements.{count_field}": "2个"},
        locked_fields=[f"requirements.{count_field}"],
    )

    issues = apply_pricing_contract(requirement)

    assert issues == []
    assert requirement.quantity == 2
    assert count_field not in requirement.requirements
    assert requirement.field_sources["quantity"] == "customer_text"
    assert requirement.field_evidence["quantity"] == "2个"
    assert requirement.locked_fields == ["quantity"]
