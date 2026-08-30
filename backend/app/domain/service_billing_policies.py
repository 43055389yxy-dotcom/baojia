from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.domain.models import ServiceRequirement


SERVICE_BILLING_POLICY_VERSION = "2026-08-29.1"


@dataclass(frozen=True, slots=True)
class NoAdditionalChargeDecision:
    """A reviewed AWS service boundary that must never invent a service fee.

    The service may create or operate paid child resources.  Those children
    remain separate quote components; this decision covers only the named
    control plane/base object.
    """

    model: str
    architecture: str
    rationale: str
    source_url: str
    pricing_mode: str
    applied_fields: tuple[str, ...] = ()
    notice: str | None = None


def _identity(value: str) -> str:
    result = re.sub(r"[^a-z0-9]", "", value.casefold())
    if result.startswith("amazon"):
        result = result[len("amazon") :]
    elif result.startswith("aws"):
        result = result[len("aws") :]
    if result.endswith("service"):
        result = result[: -len("service")]
    return result


def service_identities(requirement: ServiceRequirement) -> frozenset[str]:
    return frozenset(
        identity
        for identity in (
            _identity(requirement.service),
            _identity(requirement.calculator_service_name or ""),
        )
        if identity
    )


def _has_positive(requirement: ServiceRequirement, *fields: str) -> bool:
    return any(
        isinstance(value := requirement.requirements.get(field), (int, float))
        and not isinstance(value, bool)
        and value > 0
        for field in fields
    )


def _all_present_fields(requirement: ServiceRequirement) -> tuple[str, ...]:
    fields = [
        field
        for field, value in requirement.requirements.items()
        if value not in (None, "", [], {}) and not field.startswith("_")
    ]
    # Top-level quantity can be customer-owned even though it is not inside
    # ``requirements``.  Marking it as applied lets the fact ledger prove that
    # a free control-plane count was preserved rather than silently discarded.
    fields.append("quantity")
    return tuple(dict.fromkeys(fields))


def no_additional_charge_decision(
    requirement: ServiceRequirement,
) -> NoAdditionalChargeDecision | None:
    """Return a zero-service-fee policy before any Price List rate selection.

    This is deliberately separate from product templates and offer-code
    discovery.  A Price List offer can contain paid optional features even
    when the customer's base service is free.  Catalog presence therefore is
    never evidence that the base object itself should be charged.
    """

    identities = service_identities(requirement)
    source = (requirement.source_text or "").casefold()
    applied = _all_present_fields(requirement)

    if "ecs" in identities:
        launch_type = str(
            requirement.requirements.get("launch_type") or "ec2"
        ).strip().casefold()
        if launch_type not in {
            "fargate",
            "managed_instances",
            "managed instances",
            "managedinstances",
            "anywhere",
            "external",
        }:
            return NoAdditionalChargeDecision(
                model="ECS 集群控制面（EC2 启动类型）",
                architecture="控制面无单独服务费；计算资源由 EC2 Worker 子组件计费",
                rationale=(
                    "Amazon ECS 的 EC2 启动类型不对集群控制面另收服务费；"
                    "EC2、EBS 和公网 IPv4 等资源必须由各自子组件独立计费。"
                ),
                source_url="https://aws.amazon.com/ecs/pricing/",
                pricing_mode="no-additional-charge-for-ec2-launch-type",
                applied_fields=applied,
            )

    if "vpc" in identities or "virtualprivatecloud" in identities:
        return NoAdditionalChargeDecision(
            model="VPC + Subnets",
            architecture=f"{max(requirement.quantity, 1)} 套 VPC 基础网络",
            rationale=(
                "VPC、子网、路由表、安全组和网络 ACL 的基础对象不另收费；"
                "NAT Gateway、公网 IPv4、VPN、端点和流量必须作为独立资源计费。"
            ),
            source_url="https://aws.amazon.com/vpc/faqs/",
            pricing_mode="no-additional-charge-for-base-vpc",
            applied_fields=applied,
            notice=(
                "VPC 与子网本身不收取基础费用；NAT Gateway、公网 IPv4、VPN、"
                "端点和流量等独立资源按实际配置另行计费。"
            ),
        )

    if "codedeploy" in identities:
        on_premises = any(
            marker in source
            for marker in (
                "on-prem",
                "on premises",
                "onpremises",
                "本地实例",
                "本地服务器",
                "本地部署",
            )
        )
        if not on_premises:
            if "ec2" in source:
                deployment_model = "EC2 部署（无额外服务费）"
                deployment_architecture = "使用 AWS CodeDeploy 部署到 Amazon EC2"
            elif "lambda" in source:
                deployment_model = "Lambda 部署（无额外服务费）"
                deployment_architecture = "使用 AWS CodeDeploy 部署到 AWS Lambda"
            elif "ecs" in source:
                deployment_model = "ECS 部署（无额外服务费）"
                deployment_architecture = "使用 AWS CodeDeploy 部署到 Amazon ECS"
            else:
                deployment_model = "AWS 计算资源部署（无额外服务费）"
                deployment_architecture = "使用 AWS CodeDeploy 部署到 EC2、Lambda 或 ECS"
            return NoAdditionalChargeDecision(
                model=deployment_model,
                architecture=deployment_architecture,
                rationale=(
                    "CodeDeploy 部署到 Amazon EC2、AWS Lambda 或 Amazon ECS 时"
                    "不收取额外服务费；本地实例部署才使用单独收费项。"
                ),
                source_url="https://aws.amazon.com/codedeploy/pricing/",
                pricing_mode="no-additional-charge-for-aws-compute",
                applied_fields=applied,
            )

    if "cloudformation" in identities:
        paid_extension = any(
            marker in source
            for marker in (
                "third-party",
                "third party",
                "第三方资源",
                "第三方 provider",
                "custom hook",
                "customhook",
                "自定义 hook",
                "自定义hook",
                "自定义钩子",
            )
        ) or any("official_usage_" in field for field in requirement.requirements)
        if not paid_extension:
            return NoAdditionalChargeDecision(
                model="CloudFormation（AWS 自有资源）",
                architecture="编排层无额外服务费；模板创建的 AWS 资源分别计费",
                rationale=(
                    "使用 AWS::* 或 Alexa::* 资源提供程序时 CloudFormation 不另收费；"
                    "第三方资源提供程序和自定义 Hook 的处理操作属于条件收费功能。"
                ),
                source_url="https://aws.amazon.com/cloudformation/pricing/",
                pricing_mode="no-additional-charge-for-aws-resource-providers",
                applied_fields=applied,
            )

    # ECR repository count is deployment metadata, not a billable unit.  ECR
    # charges stored data, selected scanning/signing features, and eligible
    # internet/cross-region transfer.  If any such customer usage exists, the
    # normal metered adapter continues below.
    if "ecr" in identities and not _has_positive(
        requirement,
        "storage_gib",
        "image_scans",
        "data_transfer_out_gib",
    ) and not any(field.startswith("official_usage_") for field in requirement.requirements):
        return NoAdditionalChargeDecision(
            model="ECR Repository",
            architecture="仓库对象无单独月费；镜像存储与符合条件的流量另行计费",
            rationale=(
                "Amazon ECR 不按仓库数量收费；只有客户明确的存储、收费扫描/签名"
                "或符合条件的数据传输才能进入价格计算。"
            ),
            source_url="https://aws.amazon.com/ecr/pricing/",
            pricing_mode="no-charge-for-repository-object",
            applied_fields=applied,
        )

    # An EventBridge bus object has no count-based monthly fee. Event
    # ingestion/delivery, Pipes and schema discovery remain normal metered
    # dimensions and therefore bypass this decision when present.
    event_bus_named = _has_positive(requirement, "event_buses") or bool(
        re.search(r"(?<![a-z0-9])event\s*bus(?:es)?(?![a-z])|事件总线", source, re.I)
    )
    if "eventbridge" in identities and not _has_positive(
        requirement,
        "events",
        "schema_discovery_events",
        "pipes_requests",
    ) and event_bus_named:
        return NoAdditionalChargeDecision(
            model="EventBridge Event Bus",
            architecture="事件总线对象不按数量收费；事件与其他功能按实际用量计费",
            rationale=(
                "Event bus 数量不是 AWS 收费单位；客户未填写事件或 Pipes 等用量时，"
                "不能把总线数量套入任意事件单价。"
            ),
            source_url="https://aws.amazon.com/eventbridge/pricing/",
            pricing_mode="no-charge-for-event-bus-object",
            applied_fields=applied,
        )

    if "computeoptimizer" in identities:
        enhanced_named = any(
            marker in source
            for marker in (
                "enhanced infrastructure metrics",
                "enhanced metrics",
                "增强基础设施指标",
                "增强指标",
            )
        )
        enhanced_disabled = bool(
            re.search(
                r"(?:不启用|未启用|不开启|未开启|关闭|禁用|不使用|未使用)"
                r"[^。；;\n]{0,24}(?:enhanced\s+(?:infrastructure\s+)?metrics|"
                r"增强(?:基础设施)?指标)|"
                r"(?:enhanced\s+(?:infrastructure\s+)?metrics|增强(?:基础设施)?指标)"
                r"[^。；;\n]{0,16}(?:disabled|off|关闭|禁用)",
                source,
                re.I,
            )
        )
        enhanced = enhanced_named and not enhanced_disabled
        if not enhanced:
            return NoAdditionalChargeDecision(
                model="Compute Optimizer 标准建议",
                architecture="标准 14 天分析无额外服务费",
                rationale=(
                    "Compute Optimizer 标准建议免费；只有客户明确启用 Enhanced "
                    "Infrastructure Metrics 时才按资源小时计费。"
                ),
                source_url="https://aws.amazon.com/compute-optimizer/pricing/",
                pricing_mode="no-additional-charge-without-enhanced-metrics",
                applied_fields=applied,
            )

    free_control_planes: dict[
        str, tuple[str, str, str, str]
    ] = {
        "ec2autoscaling": (
            "EC2 Auto Scaling 控制面",
            "Auto Scaling 本身无额外服务费；EC2、EBS 和 CloudWatch 分别计费",
            "Amazon EC2 Auto Scaling 不另收费，只支付实际使用的下游 AWS 资源。",
            "https://docs.aws.amazon.com/autoscaling/ec2/userguide/what-is-amazon-ec2-auto-scaling.html",
        ),
        "autoscaling": (
            "EC2 Auto Scaling 控制面",
            "Auto Scaling 本身无额外服务费；EC2、EBS 和 CloudWatch 分别计费",
            "Amazon EC2 Auto Scaling 不另收费，只支付实际使用的下游 AWS 资源。",
            "https://docs.aws.amazon.com/autoscaling/ec2/userguide/what-is-amazon-ec2-auto-scaling.html",
        ),
        "applicationautoscaling": (
            "Application Auto Scaling 控制面",
            "自动扩缩控制面无额外服务费；被扩缩资源分别计费",
            "Application Auto Scaling 的控制动作不应被当成计算实例收费。",
            "https://aws.amazon.com/autoscaling/pricing/",
        ),
        "elasticbeanstalk": (
            "Elastic Beanstalk 环境控制面",
            "平台编排无额外服务费；EC2、ELB、S3 等下游资源分别计费",
            "Elastic Beanstalk 本身不另收费，只支付环境实际创建的 AWS 资源。",
            "https://aws.amazon.com/elasticbeanstalk/pricing/",
        ),
        "beanstalk": (
            "Elastic Beanstalk 环境控制面",
            "平台编排无额外服务费；EC2、ELB、S3 等下游资源分别计费",
            "Elastic Beanstalk 本身不另收费，只支付环境实际创建的 AWS 资源。",
            "https://aws.amazon.com/elasticbeanstalk/pricing/",
        ),
        "organizations": (
            "AWS Organizations",
            "组织管理本身无额外服务费；成员账号中的 AWS 资源分别计费",
            "AWS Organizations 本身不另收费。",
            "https://docs.aws.amazon.com/organizations/latest/userguide/pricing.html",
        ),
        "iam": (
            "AWS IAM",
            "身份与权限管理本身无额外服务费",
            "IAM、IAM Identity Center 和 STS 基础能力不另收费；Access Analyzer 的部分高级分析除外。",
            "https://docs.aws.amazon.com/IAM/latest/UserGuide/IAM_Concepts.html",
        ),
        "identityandaccessmanagement": (
            "AWS IAM",
            "身份与权限管理本身无额外服务费",
            "IAM 基础能力不另收费；其他 AWS 资源及 Access Analyzer 的收费功能分别计费。",
            "https://docs.aws.amazon.com/IAM/latest/UserGuide/IAM_Concepts.html",
        ),
        "iamidentitycenter": (
            "AWS IAM Identity Center",
            "身份中心基础能力无额外服务费",
            "IAM Identity Center 基础服务不另收费，连接的目录和其他 AWS 资源分别计费。",
            "https://docs.aws.amazon.com/IAM/latest/UserGuide/IAM_Concepts.html",
        ),
        "securitytoken": (
            "AWS STS",
            "临时安全凭证服务无额外服务费",
            "AWS STS 本身不另收费，使用凭证访问的 AWS 资源分别计费。",
            "https://docs.aws.amazon.com/IAM/latest/UserGuide/IAM_Concepts.html",
        ),
        "sts": (
            "AWS STS",
            "临时安全凭证服务无额外服务费",
            "AWS STS 本身不另收费，使用凭证访问的 AWS 资源分别计费。",
            "https://docs.aws.amazon.com/IAM/latest/UserGuide/IAM_Concepts.html",
        ),
        "resourceaccessmanager": (
            "AWS Resource Access Manager",
            "资源共享控制面无额外服务费；被共享资源按所属服务计费",
            "AWS RAM 及资源共享本身不另收费。",
            "https://docs.aws.amazon.com/ram/latest/userguide/what-is.html",
        ),
        "ram": (
            "AWS Resource Access Manager",
            "资源共享控制面无额外服务费；被共享资源按所属服务计费",
            "AWS RAM 及资源共享本身不另收费。",
            "https://docs.aws.amazon.com/ram/latest/userguide/what-is.html",
        ),
        "controltower": (
            "AWS Control Tower",
            "治理控制面无额外服务费；Config、CloudTrail、S3 等下游资源分别计费",
            "AWS Control Tower 本身不另收费。",
            "https://docs.aws.amazon.com/controltower/latest/userguide/pricing.html",
        ),
        "proton": (
            "AWS Proton",
            "平台编排无额外服务费；部署出的 AWS 资源分别计费",
            "AWS Proton 本身不另收费。",
            "https://aws.amazon.com/proton/pricing/",
        ),
        "cloudshell": (
            "AWS CloudShell",
            "CloudShell 环境无额外服务费；运行的其他资源和标准流量分别计费",
            "AWS CloudShell 本身不另收费。",
            "https://docs.aws.amazon.com/cloudshell/latest/userguide/welcome.html",
        ),
        "migrationhuborchestrator": (
            "Migration Hub Orchestrator",
            "迁移编排本身无额外服务费；迁移过程中创建的 AWS 资源分别计费",
            "Migration Hub Orchestrator 本身不另收费。",
            "https://docs.aws.amazon.com/migrationhub-orchestrator/latest/userguide/what-is-migrationhub-orchestrator.html",
        ),
    }
    for identity in identities:
        if identity not in free_control_planes:
            continue
        model, architecture, rationale, source_url = free_control_planes[identity]
        return NoAdditionalChargeDecision(
            model=model,
            architecture=architecture,
            rationale=rationale,
            source_url=source_url,
            pricing_mode="no-additional-service-charge",
            applied_fields=applied,
        )

    return None


def billing_policy_mode(requirement: ServiceRequirement) -> Literal[
    "no_additional_charge", "metered_or_conditional"
]:
    return (
        "no_additional_charge"
        if no_additional_charge_decision(requirement) is not None
        else "metered_or_conditional"
    )
