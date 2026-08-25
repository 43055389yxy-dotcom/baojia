from app.domain.component_hierarchy import component_hierarchy
from app.domain.component_integrity import ensure_component_keys
from app.domain.models import ParsedIntent, ServiceRequirement


def test_derived_components_use_decimal_numbers_under_their_parent() -> None:
    services = [
        ServiceRequirement(
            service="eks",
            calculator_service_name="Amazon EKS",
            source_text="EKS：容器集群和工作节点",
        ),
        ServiceRequirement(
            service="ec2",
            calculator_service_name="Amazon EC2（EKS 工作节点）",
            source_text="用于 EKS 工作负载",
        ),
        ServiceRequirement(
            service="ec2",
            calculator_service_name="Amazon EC2（EKS Worker）",
            source_text="承载容器 Pod",
        ),
        ServiceRequirement(service="s3", source_text="S3 对象存储"),
    ]

    hierarchy = component_hierarchy(services)

    assert [item.component_number for item in hierarchy] == ["1", "1.1", "1.2", "2"]
    assert hierarchy[1].parent_component_id == "0"
    assert hierarchy[1].parent_display_name == "Amazon EKS"


def test_explicit_ec2_remains_a_top_level_component() -> None:
    services = [
        ServiceRequirement(service="eks", source_text="EKS 集群"),
        ServiceRequirement(
            service="ec2",
            calculator_service_name="Amazon EC2 云服务器",
            source_text="EC2 API 服务：4C8G",
        ),
    ]

    hierarchy = component_hierarchy(services)

    assert [item.component_number for item in hierarchy] == ["1", "2"]
    assert hierarchy[1].parent_component_id is None


def test_persisted_lineage_keeps_a_late_child_under_its_parent() -> None:
    services = [
        ServiceRequirement(
            service="eks",
            calculator_service_name="Amazon EKS",
            source_text="11、EKS：控制面和工作节点",
        ),
        ServiceRequirement(service="s3", source_text="12、S3 对象存储"),
        ServiceRequirement(
            service="ec2",
            calculator_service_name="Amazon EC2 (EKS Worker Nodes)",
            derived_from_service="eks",
            source_text="11、EKS：控制面和工作节点",
            requirements={"vcpu": 4, "memory_gib": 8},
        ),
    ]

    hierarchy = component_hierarchy(services)

    assert [item.component_number for item in hierarchy] == ["1", "2", "1.1"]
    assert hierarchy[2].parent_component_id == "0"
    assert hierarchy[2].parent_component_number == "1"


def test_parent_key_survives_customer_edit_prefix_without_source_matching() -> None:
    intent = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="eks",
                component_key="cmp_parent_123",
                calculator_service_name="Amazon EKS",
                source_text="1、EKS：每个集群3个Worker节点",
            ),
            ServiceRequirement(service="s3", source_text="2、S3：100GB"),
            ServiceRequirement(
                service="ec2",
                component_key="cmp_child_1234",
                parent_component_key="cmp_parent_123",
                derived_from_service="eks",
                source_text="客户通过配置表直接修改：system_disk_gib\n完全不同的可变文本",
            ),
        ],
    )
    ensure_component_keys(intent)

    hierarchy = component_hierarchy(intent.services)

    assert hierarchy[2].component_number == "1.1"
    assert hierarchy[2].parent_component_id == "0"
