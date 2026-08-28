from app.domain.models import ServiceRequirement
from app.domain.requirement_fields import canonical_requirement_field_name
from app.integrations.deepseek import DeepSeekIntentParser
from app.integrations.prompt_library import (
    PROMPT_META,
    SERVICE_KEYWORDS,
    SERVICE_PROMPTS,
    build_component_extraction_prompt,
    build_inventory_prompt,
    build_intake_prompt,
    build_service_prompt,
    build_system_prompt,
    prompt_library_payload,
    prompt_keys_for_request,
    prompt_size_for_request,
)
from app.integrations.service_templates import (
    BILLING_DIMENSION_FIELDS,
    DYNAMIC_SEMANTIC_TEMPLATE_FIELDS,
    SERVICE_TEMPLATE_FIELDS,
    component_template,
    requires_official_field_profile,
)


def test_intake_and_component_prompts_are_physically_separated() -> None:
    intake = build_intake_prompt()
    ec2 = build_service_prompt("ec2")

    assert "客户问题识别" in intake
    assert "additional_ebs_volumes" not in intake
    assert "additional_ebs_volumes" in ec2
    assert "replicas_per_shard" not in ec2
    assert "AWS 相邻档位确认" not in intake
    assert "客户问题识别" not in ec2
    assert len(ec2) < len(intake)


def test_component_extraction_loads_exactly_its_own_full_service_prompt() -> None:
    ec2 = build_component_extraction_prompt(
        "ec2", "EC2 m6i.xlarge (4C16G) + gp3 500GB，数量1"
    )
    rds = build_component_extraction_prompt(
        "rds", "RDS MySQL db.t3.large Multi-AZ + 100GB，数量1"
    )

    assert "additional_ebs_volumes" in ec2
    assert "16G 是内存，500GB 才是磁盘" in ec2
    assert "deployment 只能" not in ec2
    assert "db.t3.large 只能是" in rds
    assert "additional_ebs_volumes" not in rds


def test_requirement_cleaning_prompts_keep_fallback_and_component_scopes_separate() -> None:
    inventory = build_inventory_prompt()
    component = build_component_extraction_prompt(
        "lambda", "AWS Lambda：1024MB，800ms，每月调用2000万次"
    )

    assert "第一步数据清洗员" in inventory
    assert "拆分、去除干扰、统一格式" in inventory
    assert "已由程序单独拆出" in component
    assert "只处理这一项" in component
    assert "完整字段清单" in component


def test_quicksight_component_uses_native_independent_template_prompt() -> None:
    prompt = build_component_extraction_prompt(
        "quicksight", "QuickSight Enterprise (10用户)，数量1"
    )

    assert "字段：edition, users" in prompt
    assert "不得改写成“BI 可视化自建软件”" in prompt
    assert "additional_ebs_volumes" not in prompt


def test_lowest_cost_guard_is_loaded_in_every_ai_stage() -> None:
    intake = build_intake_prompt()
    component = build_service_prompt("ec2")
    workload = build_system_prompt("EC2 4核16G")

    for prompt in (intake, component, workload):
        assert "不可覆盖的最低成本硬规则" in prompt
        assert "满足全部明确下限" in prompt
        assert "官方单价最低者" in prompt


def test_minimum_runnable_guard_is_loaded_after_editable_service_rules() -> None:
    for prompt in (
        build_intake_prompt(),
        build_service_prompt("ec2"),
        build_service_prompt("opensearch"),
        build_system_prompt("Nacos 2 台；ELK 1 套；S3 对象存储"),
    ):
        assert "不可覆盖的最低可运行配置规则" in prompt
        assert "目标是“能运行”" in prompt
        assert "绝不能填写 requested_model" in prompt
        assert "system_default_assumption" in prompt

    ec2 = build_service_prompt("ec2")
    assert ec2.rfind("不可覆盖的最低可运行配置规则") > ec2.rfind("【EC2】")
    assert "Nacos" in ec2


def test_remaining_service_prompts_apply_minimum_unit_policy() -> None:
    for service in ("eks", "ecr", "backup", "secrets_manager"):
        assert "不可覆盖的最低成本硬规则" in build_service_prompt(service)

    assert "最低计费控制面方案" in build_service_prompt("eks")
    assert "最小计费单位" in build_service_prompt("ecr")
    assert "最低存储计费单位" in build_service_prompt("backup")
    assert "1 个 Secret" in build_service_prompt("secrets_manager")


def test_prompt_library_lists_common_rules_before_frequent_services() -> None:
    items = prompt_library_payload()["items"]

    assert [item["key"] for item in items[:7]] == [
        "intake_format",
        "issue_detection",
        "nearest_tier_policy",
        "lowest_cost_policy",
        "ec2",
        "rds",
        "elasticache",
    ]


def test_ec2_only_loads_ec2_module() -> None:
    text = "东京需要 2 台 EC2，4 核 16G，每台 200G gp3。"

    assert prompt_keys_for_request(text) == ["ec2"]
    prompt = build_system_prompt(text)
    assert "additional_ebs_volumes" in prompt
    assert "replicas_per_shard" not in prompt


def test_mixed_request_loads_only_matching_modules() -> None:
    text = "MySQL 数据库、Redis 缓存和 S3 对象存储都放在新加坡。"

    assert prompt_keys_for_request(text) == ["rds", "elasticache", "s3"]
    prompt = build_system_prompt(text)
    assert "deployment 只能" in prompt
    assert "replicas_per_shard" in prompt
    assert "storage_class" in prompt
    assert "additional_ebs_volumes" not in prompt


def test_database_instance_does_not_accidentally_load_ec2() -> None:
    assert prompt_keys_for_request("需要一个 MySQL 数据库实例") == ["rds"]


def test_small_request_uses_smaller_prompt_than_mixed_request() -> None:
    ec2 = "2 台 EC2"
    mixed = "EC2、MySQL、Redis、ALB、S3 和 CloudFront"

    assert prompt_size_for_request(ec2) < prompt_size_for_request(mixed)


def test_unknown_service_gets_generic_module() -> None:
    prompt = build_system_prompt("需要一套 Amazon AppFlow 数据同步报价")

    assert "其他 AWS 服务" in prompt
    assert "不得虚构型号或价格" in prompt


def test_auxiliary_services_load_separate_prompt_modules() -> None:
    text = "云硬盘 1TB；公网出网流量 1000GB；全球访问加速 GA 1 个"

    assert prompt_keys_for_request(text) == [
        "ebs",
        "data_transfer",
        "global_accelerator",
    ]
    prompt = build_system_prompt(text)
    assert "Amazon EBS 独立云盘" in prompt
    assert "AWS Data Transfer 独立公网流量" in prompt
    assert "AWS Global Accelerator" in prompt


def test_every_supported_service_has_inventory_prompt_keywords_and_full_template() -> None:
    """A new adapter cannot silently miss one stage of the extraction pipeline."""

    template_keys = set(SERVICE_TEMPLATE_FIELDS)
    inventory_keys = {
        key for key, _display, _markers in DeepSeekIntentParser._INVENTORY_DEFINITIONS
    }
    assert template_keys == set(SERVICE_PROMPTS)
    assert template_keys <= set(SERVICE_KEYWORDS)
    assert template_keys <= set(PROMPT_META)
    assert template_keys == inventory_keys

    for service, fields in SERVICE_TEMPLATE_FIELDS.items():
        template = component_template(ServiceRequirement(service=service))
        assert set(fields) <= set(template["requirements"])
        assert {
            f"requirements.{field}" for field in fields
        } <= set(template["field_evidence"])


def test_every_runtime_template_field_is_present_in_effective_component_prompt() -> None:
    """The editable prose and executable allow-list may never drift apart."""

    for service, fields in SERVICE_TEMPLATE_FIELDS.items():
        prompt = build_service_prompt(service)
        assert set(fields) <= {field for field in fields if field in prompt}


def test_generic_services_receive_complete_dynamic_billing_vocabulary() -> None:
    assert BILLING_DIMENSION_FIELDS <= set(DYNAMIC_SEMANTIC_TEMPLATE_FIELDS)
    assert requires_official_field_profile("lambda") is True
    assert requires_official_field_profile("step_functions") is True
    assert requires_official_field_profile("ec2") is True
    assert requires_official_field_profile("elasticache") is True


def test_every_component_template_uses_only_canonical_pricing_field_names() -> None:
    """A model-facing field must be the same field consumed by pricing.

    If a template exposes a legacy alias, extraction can succeed while a later
    normalization pass silently renames the value before the adapter reads it.
    This contract check blocks that class of data-loss bug for every service.
    """

    conflicts = {
        service: {
            field: canonical_requirement_field_name(field, service=service)
            for field in fields
            if canonical_requirement_field_name(field, service=service) != field
        }
        for service, fields in SERVICE_TEMPLATE_FIELDS.items()
    }
    assert {service: values for service, values in conflicts.items() if values} == {}


def test_documentdb_and_glue_templates_keep_all_adapter_fields() -> None:
    assert {"instance_count", "vcpu", "memory_gib"} <= set(
        SERVICE_TEMPLATE_FIELDS["documentdb"]
    )
    assert "job_count" in SERVICE_TEMPLATE_FIELDS["glue"]


def test_api_aliases_are_forbidden_and_backup_has_its_own_module() -> None:
    intake = build_intake_prompt()
    backup = build_service_prompt("backup")
    elb = build_service_prompt("elb")
    waf = build_service_prompt("waf")

    assert "禁止输出 elbv2、wafv2" in intake
    assert "service 必须写 elb" in elb
    assert "service 必须写 waf" in waf
    assert "AWS Backup / RDS Backup" in backup
    assert prompt_keys_for_request("需要 AWS Backup 集中备份") == ["backup"]


def test_platform_and_data_services_load_independent_modules() -> None:
    text = (
        "Amazon EKS 1 个集群；Amazon ECR 1 个私有仓库；"
        "Amazon MSK kafka.t3.small；Amazon OpenSearch t3.small.search；"
        "AWS Secrets Manager 5 个 Secret"
    )

    assert prompt_keys_for_request(text) == [
        "eks",
        "ecr",
        "msk",
        "opensearch",
        "secrets_manager",
    ]
    assert "broker_count" in build_service_prompt("msk")
    assert "storage_gib_per_node" in build_service_prompt("opensearch")
    assert "secret_count" in build_service_prompt("secrets_manager")


def test_generic_kafka_loads_managed_msk_rules() -> None:
    assert prompt_keys_for_request("Kafka 消息队列，3 个节点") == ["msk"]
    prompt = build_system_prompt("Kafka 消息队列，3 个节点")
    assert "默认识别为 AWS 托管 Amazon MSK" in prompt
    assert "不得询问\u201c托管还是自建\u201d" in prompt


def test_managed_queue_and_public_api_business_phrases_load_their_templates() -> None:
    text = "消息队列使用 RabbitMQ；接口服务需要提供API给外部系统调用"

    keys = prompt_keys_for_request(text)

    assert "mq" in keys
    assert "apigateway" in keys


def test_operating_system_default_is_global_and_linux() -> None:
    for prompt in (
        build_intake_prompt(),
        build_service_prompt("ec2"),
        build_service_prompt("fargate"),
        build_system_prompt("EKS 工作节点 4核16G"),
    ):
        assert "客户未指定时，一律使用 Linux" in prompt


def test_extended_services_are_independent_editable_prompt_cards() -> None:
    expected = {
        "lambda",
        "ecs",
        "fargate",
        "dynamodb",
        "efs",
        "fsx",
        "sns",
        "kinesis",
        "emr",
        "redshift",
        "athena",
        "glue",
        "step_functions",
        "bedrock",
        "cloud_map",
        "appconfig",
        "eventbridge",
    }
    items = prompt_library_payload()["items"]
    keys = {item["key"] for item in items}

    assert expected <= keys
    for key in expected:
        prompt = build_service_prompt(key)
        assert "不可覆盖的最低成本硬规则" in prompt
        assert "其他 AWS 服务" not in prompt


def test_extended_service_keywords_load_only_their_own_modules() -> None:
    text = (
        "AWS Lambda 函数；Amazon DynamoDB；Amazon EFS；Amazon Redshift；"
        "AWS Step Functions；Amazon Bedrock；AWS Cloud Map；AWS AppConfig"
    )

    assert prompt_keys_for_request(text) == [
        "lambda",
        "dynamodb",
        "efs",
        "redshift",
        "step_functions",
        "bedrock",
        "cloud_map",
        "appconfig",
    ]


def test_eventbridge_scheduler_does_not_load_event_bus_prompt() -> None:
    assert prompt_keys_for_request("需要一套 Amazon EventBridge Scheduler 定时任务") == [
        "scheduler"
    ]
    assert prompt_keys_for_request("需要一个 EventBridge 事件总线") == ["eventbridge"]
