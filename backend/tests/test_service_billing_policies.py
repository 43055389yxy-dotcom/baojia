import pytest

from app.domain.models import ServiceRequirement
from app.domain.service_billing_policies import (
    billing_policy_mode,
    no_additional_charge_decision,
)
from app.integrations.service_templates import SERVICE_TEMPLATE_FIELDS
from app.services.plugins.generic_official import GenericOfficialPlugin


@pytest.mark.parametrize(
    ("service", "display_name", "source_text", "requirements"),
    [
        (
            "ecs",
            "Amazon ECS",
            "Amazon ECS，1套集群，EC2 Worker节点4台",
            {"cluster_count": 1, "launch_type": "ec2"},
        ),
        (
            "vpc",
            "Amazon VPC",
            "1套VPC，2个公有子网，2个私有子网",
            {"vpc_count": 1, "public_subnets": 2, "private_subnets": 2},
        ),
        (
            "cloud_formation",
            "AWS CloudFormation",
            "CloudFormation 管理20套AWS资源栈",
            {"resource_count": 20},
        ),
        (
            "code_deploy",
            "AWS CodeDeploy",
            "CodeDeploy 部署到EC2",
            {"deployment_updates": 100},
        ),
        (
            "ec2_auto_scaling",
            "Amazon EC2 Auto Scaling",
            "Auto Scaling Group 2套",
            {"resource_count": 2},
        ),
        (
            "elastic_beanstalk",
            "AWS Elastic Beanstalk",
            "Elastic Beanstalk 2套环境",
            {"resource_count": 2},
        ),
        ("organizations", "AWS Organizations", "1个组织", {"resource_count": 1}),
        ("iam", "AWS IAM", "100个IAM用户", {"user_count": 100}),
        (
            "resource_access_manager",
            "AWS Resource Access Manager",
            "10个资源共享",
            {"resource_count": 10},
        ),
        ("control_tower", "AWS Control Tower", "1套landing zone", {"resource_count": 1}),
        ("proton", "AWS Proton", "2套环境", {"resource_count": 2}),
        ("cloud_shell", "AWS CloudShell", "20个用户", {"user_count": 20}),
        (
            "migration_hub_orchestrator",
            "AWS Migration Hub Orchestrator",
            "5条迁移工作流",
            {"flow_runs": 5},
        ),
        ("ecr", "Amazon ECR", "3个ECR仓库", {"repositories": 3}),
        (
            "eventbridge",
            "Amazon EventBridge",
            "2个Event Bus",
            {"event_buses": 2},
        ),
        (
            "compute_optimizer",
            "AWS Compute Optimizer",
            "标准14天资源建议，不启用Enhanced Infrastructure Metrics",
            {"resource_count": 50},
        ),
    ],
)
def test_reviewed_no_additional_charge_components_never_query_catalog(
    service: str,
    display_name: str,
    source_text: str,
    requirements: dict[str, object],
) -> None:
    class CatalogMustNotBeCalled:
        @staticmethod
        def service_codes() -> list[str]:
            raise AssertionError("free service control plane must not query a paid offer")

        @staticmethod
        def products(*args, **kwargs):
            raise AssertionError("free service control plane must not query a paid offer")

    requirement = ServiceRequirement(
        service=service,
        calculator_service_name=display_name,
        region="ap-southeast-1",
        source_text=source_text,
        requirements=requirements,
    )
    selected = GenericOfficialPlugin(  # type: ignore[arg-type]
        None, CatalogMustNotBeCalled()
    ).select(requirement, "ap-southeast-1")

    assert selected.pricing_status == "free"
    assert selected.usage_lines == []
    assert selected.reference_rates == []
    assert selected.official_product["pricingMode"].startswith("no-")
    assert selected.official_product["source"].startswith("https://")
    assert set(requirements).issubset(set(selected.applied_requirement_fields))


@pytest.mark.parametrize(
    "requirement",
    [
        ServiceRequirement(
            service="ecs",
            source_text="ECS Fargate 运行10个Task",
            requirements={"launch_type": "fargate", "tasks": 10},
        ),
        ServiceRequirement(
            service="code_deploy",
            source_text="CodeDeploy每月更新80台本地服务器",
            requirements={"deployment_updates": 80},
        ),
        ServiceRequirement(
            service="cloud_formation",
            source_text="CloudFormation自定义Hook每月5000次操作",
            requirements={"requests": 5000},
        ),
        ServiceRequirement(
            service="ecr",
            source_text="ECR镜像存储500G",
            requirements={"repositories": 3, "storage_gib": 500},
        ),
        ServiceRequirement(
            service="eventbridge",
            source_text="EventBridge每月100万自定义事件",
            requirements={"event_buses": 1, "events": 1_000_000},
        ),
        ServiceRequirement(
            service="compute_optimizer",
            source_text="启用Enhanced Infrastructure Metrics",
            requirements={"hours_per_month": 730},
        ),
    ],
)
def test_paid_variants_are_not_suppressed_by_free_base_policy(
    requirement: ServiceRequirement,
) -> None:
    assert no_additional_charge_decision(requirement) is None
    assert billing_policy_mode(requirement) == "metered_or_conditional"


def test_every_fixed_template_has_a_deterministic_billing_policy_outcome() -> None:
    # The 52 hand-maintained semantic templates and all dynamic products use
    # the same two-way gate: an explicitly reviewed zero-service-fee decision,
    # or the normal metered/conditional path. No third implicit fallback exists.
    outcomes = {
        service: billing_policy_mode(ServiceRequirement(service=service))
        for service in SERVICE_TEMPLATE_FIELDS
    }

    assert len(outcomes) == 52
    assert set(outcomes.values()) <= {
        "no_additional_charge",
        "metered_or_conditional",
    }
    assert outcomes["ecs"] == "no_additional_charge"
    assert outcomes["vpc"] == "no_additional_charge"
    assert outcomes["ecr"] == "no_additional_charge"
