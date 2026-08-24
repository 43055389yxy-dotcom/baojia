import pytest

from app.core.config import Settings
from app.core.errors import ManualConfirmationRequired
from app.domain.customer_configuration import preserve_customer_configuration
from app.domain.models import ParsedIntent, ServiceRequirement
from app.integrations.deepseek import DeepSeekIntentParser


class RepairingGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_json(self, **_: object) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            return {
                "customer_summary": "Redis 报价",
                "services": [
                    {
                        "service": "elasticache",
                        "calculator_service_name": "Amazon ElastiCache",
                        "quantity": "两台",
                        "requirements": {"engine": "redis", "memory_gib": 8},
                    }
                ],
                "ambiguities": [],
            }
        return {
            "customer_summary": "Redis 报价",
            "services": [
                {
                    "service": "elasticache",
                    "calculator_service_name": "Amazon ElastiCache",
                    "quantity": 2,
                    "requirements": {"engine": "redis", "memory_gib": 8},
                    "source_text": "Redis 一主一从，每节点 8 GiB",
                    "query_action": None,
                }
            ],
            "ambiguities": [],
        }


class RepairStillMissingServiceGateway(RepairingGateway):
    """The repair succeeds structurally but still omits an explicit service."""


class MissingSummaryGateway:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_json(self, **_: object) -> dict[str, object]:
        self.calls += 1
        return {
            "services": [
                {
                    "service": "elasticache",
                    "calculator_service_name": "Amazon ElastiCache",
                    "quantity": 2,
                    "requirements": {"engine": "redis", "memory_gib": 8},
                    "source_text": "Redis 一主一从，每节点 8 GiB",
                }
            ],
            "ambiguities": [],
        }


class CapturingWorkloadGateway(MissingSummaryGateway):
    def __init__(self) -> None:
        super().__init__()
        self.system_prompts: list[str] = []
        self.user_contents: list[str] = []

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.system_prompts.append(str(kwargs.get("system_prompt", "")))
        self.user_contents.append(str(kwargs.get("user_content", "")))
        return await super().complete_json(**kwargs)


class ComponentCorrectionGateway:
    def __init__(self) -> None:
        self.user_contents: list[str] = []

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.user_contents.append(str(kwargs.get("user_content", "")))
        return {
            "component": {
                "service": "s3",
                "calculator_service_name": "Amazon S3",
                "region": "ap-southeast-1",
                "quantity": 1,
                "hours_per_month": 730,
                "requirements": {
                    "storage_gib": 30720,
                    "storage_class": "standard",
                },
                "field_evidence": {
                    "requirements.storage_gib": "S3 容量改为 30TB",
                },
                "source_text": "S3 30TB",
                "query_action": None,
            }
        }


class ComponentFieldRepairGateway:
    def __init__(self) -> None:
        self.calls = 0
        self.user_contents: list[str] = []

    async def complete_json(self, **kwargs: object) -> dict[str, object]:
        self.calls += 1
        self.user_contents.append(str(kwargs.get("user_content", "")))
        field = "monthly_accelerated_traffic_gb" if self.calls == 1 else "data_transfer_out_gib"
        return {
            "component": {
                "service": "global_accelerator",
                "calculator_service_name": "AWS Global Accelerator",
                "region": "global",
                "quantity": 1,
                "hours_per_month": 730,
                "requirements": {"accelerators": 1, field: 1000},
                "field_evidence": {
                    "requirements.accelerators": "1个加速器",
                    f"requirements.{field}": "每月加速流量1000GB",
                },
                "source_text": "配置1个加速器，每月加速流量1000GB",
                "query_action": None,
            }
        }


class UnchangedEc2CorrectionGateway:
    async def complete_json(self, **_: object) -> dict[str, object]:
        # Simulate a model that overlooks the purchase-plan sentence. The
        # deterministic closed-vocabulary reconciliation must still apply it.
        return {
            "component": {
                "service": "ec2",
                "calculator_service_name": "Amazon EC2",
                "region": "ap-southeast-1",
                "quantity": 2,
                "hours_per_month": 730,
                "requirements": {
                    "requested_model": "c6g.xlarge",
                    "vcpu": 4,
                    "memory_gib": 8,
                    "operating_system": "linux",
                    "purchase_option": "on_demand",
                },
                "field_evidence": {},
                "source_text": "2台 EC2 c6g.xlarge，按需付费",
                "query_action": None,
            }
        }


class UnchangedComponentCorrectionGateway:
    """Simulate a valid response that ignores the customer's latest edit."""

    def __init__(self, component: ServiceRequirement) -> None:
        self.component = component
        self.calls = 0

    async def complete_json(self, **_: object) -> dict[str, object]:
        self.calls += 1
        return {
            "component": {
                "service": self.component.service,
                "calculator_service_name": self.component.calculator_service_name,
                "region": self.component.region,
                "quantity": self.component.quantity,
                "hours_per_month": self.component.hours_per_month,
                "requirements": dict(self.component.requirements),
                "field_evidence": {},
                "source_text": self.component.source_text,
                "query_action": None,
            }
        }


@pytest.mark.asyncio
async def test_component_feedback_sends_only_the_changed_component_to_ai() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = ComponentCorrectionGateway()
    parser._gateway = gateway  # type: ignore[assignment]
    component = ServiceRequirement(
        service="s3",
        calculator_service_name="Amazon S3",
        region="ap-southeast-1",
        quantity=1,
        requirements={"storage_gib": 20480, "storage_class": "standard"},
        source_text="S3 20TB",
    )

    revised = await parser.revise_component_from_feedback(
        "EC2 4 台；RDS MySQL 1 套；S3 20TB",
        component,
        "S3 容量改为 30TB",
    )

    assert revised.requirements["storage_gib"] == 30720
    assert revised.service == "s3"
    # Simple single-field edits need one isolated template pass; a second
    # network audit is reserved for related fields such as per-node vs total.
    assert len(gateway.user_contents) == 1
    assert "RDS MySQL" not in gateway.user_contents[0]
    assert "EC2 4 台" not in gateway.user_contents[0]
    assert "客户最新修改（最高优先级）" in gateway.user_contents[0]
    assert "该组件当前完整旧配置（只用于补全客户没有修改的字段）" in gateway.user_contents[0]
    assert "该组件客户历史原话（只用于核对来源）" in gateway.user_contents[0]
    assert "当前旧配置" not in gateway.user_contents[0]


def test_component_feedback_uses_only_configured_stable_ai() -> None:
    parser = DeepSeekIntentParser(
        Settings(
            ai_provider="bedrock",
            bedrock_api_key="test",
            bedrock_model="zai.glm-4.7-flash",
            component_revision_model="deepseek.v3.2",
        )
    )
    gateways = parser._component_ai_gateways()

    assert len(gateways) == 1
    assert gateways[0]._settings.ai_model == "deepseek.v3.2"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("feedback", "expected"),
    [
        ("硬盘10个T", {"system_disk_gib": 10240}),
        ("改成4核8G", {"vcpu": 4, "memory_gib": 8}),
    ],
)
async def test_ec2_literal_revision_is_never_lost(
    feedback: str, expected: dict[str, float]
) -> None:
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        region="ap-northeast-1",
        quantity=3,
        requirements={
            "requested_model": "t4g.small",
            "vcpu": 2,
            "memory_gib": 2,
            "operating_system": "linux",
        },
        source_text="EC2 3台，t4g.small",
    )
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = UnchangedComponentCorrectionGateway(component)  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text, component, feedback
    )

    for field, value in expected.items():
        assert revised.requirements[field] == value
        assert revised.field_sources[f"requirements.{field}"] == "customer_confirmation"


def test_component_template_derives_missing_ec2_total_disk() -> None:
    payload: dict[str, object] = {
        "service": "ec2",
        "quantity": 8,
        "requirements": {"system_disk_gib": 10240},
        "field_evidence": {"requirements.system_disk_gib": "硬盘10个T"},
    }

    DeepSeekIntentParser._complete_repeated_storage_template(payload)

    assert payload["requirements"] == {
        "system_disk_gib": 10240,
        "total_system_disk_gib": 81920,
    }
    assert payload["field_evidence"] == {
        "requirements.system_disk_gib": "硬盘10个T",
        "requirements.total_system_disk_gib": "system_derived",
    }


@pytest.mark.asyncio
async def test_s3_revision_rebuilds_capacity_and_drops_old_reference_default() -> None:
    class S3RevisionGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            return {
                "component": {
                    "service": "s3",
                    "calculator_service_name": "Amazon S3",
                    "region": "ap-northeast-1",
                    "quantity": 1,
                    "hours_per_month": 730,
                    "requirements": {"storage_class": "standard"},
                    "field_evidence": {"requirements.storage_class": "Standard"},
                    "source_text": "S3 Standard",
                    "query_action": None,
                }
            }

    component = ServiceRequirement(
        service="s3",
        calculator_service_name="Amazon S3",
        region="ap-northeast-1",
        requirements={
            "storage_class": "standard",
            "reference_unit_only": True,
            "system_default_assumption": "客户未提供 S3 容量",
        },
        source_text="Amazon S3，存储类型 Standard",
    )
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = S3RevisionGateway()  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text, component, "存储改为20个T"
    )

    assert revised.requirements["storage_gib"] == 20 * 1024
    assert "reference_unit_only" not in revised.requirements
    assert "system_default_assumption" not in revised.requirements
    assert revised.field_sources["requirements.storage_gib"] == "customer_confirmation"


@pytest.mark.asyncio
async def test_unknown_component_field_is_returned_for_targeted_repair() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = ComponentFieldRepairGateway()
    parser._gateway = gateway  # type: ignore[assignment]
    component = ServiceRequirement(
        service="global_accelerator",
        calculator_service_name="AWS Global Accelerator",
        region="global",
        quantity=1,
        source_text="配置1个加速器，每月加速流量1000GB",
    )

    cleaned = await parser._cleanup_components(
        component.source_text,
        ParsedIntent(customer_summary="GA 报价", services=[component]),
    )

    assert gateway.calls == 2
    assert "monthly_accelerated_traffic_gb" in gateway.user_contents[1]
    assert "data_transfer_out_gib" in gateway.user_contents[1]
    assert cleaned.services[0].requirements["data_transfer_out_gib"] == 1000


def test_legacy_component_field_is_normalized_without_retry() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="global_accelerator",
        calculator_service_name="AWS Global Accelerator",
        source_text="配置1个加速器，每月加速流量1000GB",
    )
    raw = {
        "component": {
            "service": "global_accelerator",
            "requirements": {"accelerators": 1, "data_transfer_gib": 1000},
            "field_evidence": {
                "requirements.accelerators": "1个加速器",
                "requirements.data_transfer_gib": "每月加速流量1000GB",
            },
        }
    }

    cleaned = parser._component_from_template_output(raw, component)

    assert cleaned.requirements == {
        "accelerators": 1,
        "data_transfer_out_gib": 1000,
    }
    assert "requirements.data_transfer_out_gib" in cleaned.field_evidence


@pytest.mark.asyncio
async def test_component_feedback_deterministically_applies_reserved_purchase_plan() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = UnchangedEc2CorrectionGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        region="ap-southeast-1",
        quantity=2,
        requirements={
            "requested_model": "c6g.xlarge",
            "vcpu": 4,
            "memory_gib": 8,
            "operating_system": "linux",
            "purchase_option": "on_demand",
        },
        source_text="2台 EC2 c6g.xlarge，按需付费",
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "改成一年全预付",
    )

    assert revised.requirements["purchase_option"] == "standard_reserved"
    assert revised.requirements["reserved_term_years"] == 1
    assert revised.requirements["payment_option"] == "all_upfront"
    assert revised.field_sources["requirements.purchase_option"] == "customer_confirmation"
    assert revised.field_sources["requirements.reserved_term_years"] == "customer_confirmation"
    assert revised.field_sources["requirements.payment_option"] == "customer_confirmation"


@pytest.mark.asyncio
async def test_latest_rds_capacity_correction_cannot_be_overwritten_by_old_source() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="rds",
        calculator_service_name="Amazon RDS MySQL",
        region="ap-southeast-1",
        requirements={"engine": "mysql", "storage_gib": 500},
        source_text="RDS MySQL，存储容量500GB",
    )
    gateway = UnchangedComponentCorrectionGateway(component)
    parser._gateway = gateway  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "存储容量改成2000G吧",
    )

    assert gateway.calls == 2
    assert revised.requirements["storage_gib"] == 2000
    assert revised.field_sources["requirements.storage_gib"] == "customer_confirmation"
    assert revised.source_text.startswith("客户最新修改：存储容量改成2000G吧")


@pytest.mark.asyncio
async def test_latest_redis_capacity_correction_wins_even_when_model_returns_old_value() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="elasticache",
        calculator_service_name="Amazon ElastiCache for Redis",
        region="ap-east-1",
        requirements={
            "engine": "redis",
            "memory_gib": 52.82,
            "shards": 1,
            "replicas_per_shard": 2,
        },
        source_text="Redis，内存52.82GiB，一主两从",
    )
    gateway = UnchangedComponentCorrectionGateway(component)
    parser._gateway = gateway  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "改成2000G",
    )

    assert gateway.calls == 2
    assert revised.requirements["memory_gib"] == 2000
    assert revised.requirements["replicas_per_shard"] == 2


@pytest.mark.asyncio
async def test_latest_redshift_node_and_storage_correction_are_authoritative() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="redshift",
        calculator_service_name="Amazon Redshift",
        region="ap-southeast-1",
        quantity=1,
        requirements={
            "requested_model": "ra3.large",
            "nodes": 2,
            "storage_gib": 10240,
            "managed_storage_gib": 10240,
        },
        source_text="Redshift，2个计算节点，存储容量10TB",
    )
    gateway = UnchangedComponentCorrectionGateway(component)
    parser._gateway = gateway  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "计算节点改成6个，存储容量改成20T",
    )

    assert gateway.calls == 2
    assert revised.requirements["nodes"] == 6
    assert revised.requirements["storage_gib"] == 20480
    assert revised.requirements["managed_storage_gib"] == 20480
    assert revised.field_sources["requirements.nodes"] == "customer_confirmation"
    assert revised.field_sources["requirements.storage_gib"] == "customer_confirmation"
    assert "requirements.nodes" in revised.locked_fields
    assert "requirements.storage_gib" in revised.locked_fields


@pytest.mark.asyncio
async def test_latest_emr_core_node_quantity_correction_is_authoritative() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="emr",
        calculator_service_name="Amazon EMR",
        region="ap-southeast-1",
        quantity=1,
        requirements={
            "requested_model": "c6g.xlarge",
            "applications": "spark",
            "master_nodes": 1,
            "core_nodes": 5,
        },
        source_text="Amazon EMR，Spark，主节点1个，核心节点5个",
    )
    gateway = UnchangedComponentCorrectionGateway(component)
    parser._gateway = gateway  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "核心节点数改成7个",
    )

    assert revised.requirements["core_nodes"] == 7
    assert "nodes" not in revised.requirements
    assert revised.field_sources["requirements.core_nodes"] == "customer_confirmation"
    assert "requirements.core_nodes" in revised.locked_fields


@pytest.mark.asyncio
async def test_role_specific_broker_count_does_not_overwrite_generic_nodes() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="msk",
        calculator_service_name="Amazon MSK",
        region="ap-southeast-1",
        quantity=1,
        requirements={"broker_count": 3, "requested_model": "m7g.xlarge"},
        source_text="Amazon MSK，3个 Broker 节点",
    )
    gateway = UnchangedComponentCorrectionGateway(component)
    parser._gateway = gateway  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "消息代理节点数量改为4个",
    )

    assert revised.requirements["broker_count"] == 4
    assert "nodes" not in revised.requirements


@pytest.mark.asyncio
async def test_rabbitmq_high_availability_correction_forces_three_brokers() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="mq",
        calculator_service_name="Amazon MQ for RabbitMQ",
        region="ap-northeast-1",
        requirements={
            "engine_type": "rabbitmq",
            "requested_model": "mq.t3.micro",
            "broker_count": 1,
        },
        source_text="RabbitMQ，需要消息队列服务",
    )
    gateway = UnchangedComponentCorrectionGateway(component)
    parser._gateway = gateway  # type: ignore[assignment]

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "需要高可用和故障切换",
    )

    assert gateway.calls == 2
    assert revised.requirements["broker_count"] == 3
    assert revised.requirements["deployment_mode"] == "cluster_multi_az"


def test_rabbitmq_high_availability_is_reconciled_during_initial_extraction() -> None:
    component = ServiceRequirement(
        service="mq",
        requirements={"engine_type": "rabbitmq", "broker_count": 1},
        source_text="RabbitMQ，需要消息队列服务，并且要求高可用。",
    )
    intent = ParsedIntent(customer_summary="RabbitMQ", services=[component])

    DeepSeekIntentParser._reconcile_explicit_service_architecture(
        component.source_text, intent
    )

    assert component.requirements["broker_count"] == 3
    assert component.requirements["deployment_mode"] == "cluster_multi_az"


@pytest.mark.asyncio
async def test_exact_model_confirmation_does_not_wait_for_ai() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    class FailingGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            raise AssertionError("closed model choice must not call the model")

    parser._gateway = FailingGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="elasticache",
        calculator_service_name="Amazon ElastiCache",
        region="ap-east-1",
        quantity=1,
        requirements={
            "engine": "redis",
            "memory_gib": 16,
            "shards": 1,
            "replicas_per_shard": 1,
            "_review_selected_model": "cache.r6g.xlarge",
            "_review_selected_specifications": {"memoryGiB": 26.32},
        },
        source_text="Redis 16GB，1主1从",
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "问题：AWS 相邻规格请选择。\n客户回答：选择 cache.m7g.xlarge",
    )

    assert revised.requirements["requested_model"] == "cache.m7g.xlarge"
    assert (
        revised.field_sources["requirements.requested_model"]
        == "customer_confirmation"
    )
    assert "requirements.requested_model" in revised.locked_fields
    assert "memory_gib" not in revised.requirements
    assert "_review_selected_model" not in revised.requirements
    assert "_review_selected_specifications" not in revised.requirements


@pytest.mark.asyncio
async def test_exact_model_answer_always_replaces_old_cpu_and_memory() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    class FailingGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            raise AssertionError("closed model choice must not call the model")

    parser._gateway = FailingGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        region="ap-northeast-1",
        requirements={"vcpu": 6, "memory_gib": 24},
        source_text="EC2 6核24GB",
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        (
            "问题：AWS 没有完全相同的型号，请在下方重新选择您需要的型号。\n"
            "客户回答：选择 t2.micro"
        ),
    )

    assert revised.requirements["requested_model"] == "t2.micro"
    assert "vcpu" not in revised.requirements
    assert "memory_gib" not in revised.requirements


@pytest.mark.asyncio
async def test_unrelated_component_edit_preserves_review_model_and_overwrites_old_shape() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    class QuantityGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            return {
                "component": {
                    "service": "ec2",
                    "calculator_service_name": "Amazon EC2",
                    "region": "ap-northeast-1",
                    "quantity": 8,
                    "hours_per_month": 730,
                    "requirements": {
                        "requested_model": "t2.micro",
                        "vcpu": 6,
                        "memory_gib": 24,
                        "operating_system": "linux",
                    },
                    "field_evidence": {"quantity": "8台"},
                    "source_text": "EC2 6核24GB，选择 t2.micro",
                    "query_action": None,
                }
            }

    parser._gateway = QuantityGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        region="ap-northeast-1",
        quantity=3,
        requirements={
            "requested_model": "t2.micro",
            "vcpu": 6,
            "memory_gib": 24,
            "operating_system": "linux",
            "_review_selected_model": "t2.micro",
            "_review_selected_specifications": {
                "vCPU": 1,
                "memoryGiB": 1,
            },
        },
        source_text="EC2 6核24GB，客户已选择 t2.micro",
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "改成8台机器吧",
    )

    assert revised.quantity == 8
    assert revised.requirements["requested_model"] == "t2.micro"
    assert revised.requirements["vcpu"] == 1
    assert revised.requirements["memory_gib"] == 1
    assert "_review_selected_model" not in revised.requirements
    assert "_review_selected_specifications" not in revised.requirements


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service", "model", "base_requirements", "official_vcpu", "official_memory"),
    [
        ("rds", "db.m5.large", {"engine": "mysql"}, 2, 8),
        ("elasticache", "cache.m7g.large", {"engine": "redis"}, 2, 6.38),
        ("opensearch", "r6g.large.search", {}, 2, 16),
    ],
)
async def test_quantity_edit_rebuilds_any_component_from_latest_confirmed_model(
    service: str,
    model: str,
    base_requirements: dict[str, object],
    official_vcpu: float,
    official_memory: float,
) -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    old_requirements = {
        **base_requirements,
        "requested_model": model,
        "memory_gib": 999,
    }
    if service != "elasticache":
        old_requirements["vcpu"] = 99

    class QuantityGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            return {
                "component": {
                    "service": service,
                    "region": "ap-southeast-1",
                    "quantity": 2,
                    "hours_per_month": 730,
                    "requirements": old_requirements,
                    "field_evidence": {"quantity": "数量改成2台"},
                    "source_text": f"{service} 数量1，已选择 {model}",
                    "query_action": None,
                }
            }

    parser._gateway = QuantityGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service=service,
        region="ap-southeast-1",
        quantity=1,
        requirements={
            **old_requirements,
            "_review_selected_model": model,
            "_review_selected_specifications": {
                "vCPU": official_vcpu,
                "memoryGiB": official_memory,
            },
        },
        source_text=f"{service} 数量1，已选择 {model}",
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "数量改成2台",
    )

    assert revised.quantity == 2
    assert revised.requirements["requested_model"] == model
    if service == "elasticache":
        assert "vcpu" not in revised.requirements
    else:
        assert revised.requirements["vcpu"] == official_vcpu
    assert revised.requirements["memory_gib"] == official_memory


@pytest.mark.asyncio
async def test_repeated_component_edits_rebuild_from_the_latest_result() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    class SequentialStorageGateway:
        def __init__(self) -> None:
            self.values = iter((30720, 40960))

        async def complete_json(self, **_: object) -> dict[str, object]:
            storage = next(self.values)
            return {
                "component": {
                    "service": "s3",
                    "region": "ap-southeast-1",
                    "quantity": 1,
                    "hours_per_month": 730,
                    "requirements": {
                        "storage_gib": storage,
                        "storage_class": "standard",
                    },
                    "field_evidence": {
                        "requirements.storage_gib": f"{storage / 1024:g}TB"
                    },
                    "source_text": f"S3 {storage / 1024:g}TB",
                    "query_action": None,
                }
            }

    parser._gateway = SequentialStorageGateway()  # type: ignore[assignment]
    original = ServiceRequirement(
        service="s3",
        region="ap-southeast-1",
        requirements={"storage_gib": 20480, "storage_class": "standard"},
        source_text="S3 20TB",
    )

    first = await parser.revise_component_from_feedback(
        original.source_text, original, "容量改成30TB"
    )
    second = await parser.revise_component_from_feedback(
        first.source_text, first, "容量再改成40TB"
    )

    assert first.requirements["storage_gib"] == 30720
    assert second.requirements["storage_gib"] == 40960
    assert second.source_text.startswith("客户最新修改：容量再改成40TB")


@pytest.mark.asyncio
async def test_generic_official_model_confirmation_is_authoritative() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    class FailingGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            raise AssertionError("closed official model choice must not call the model")

    parser._gateway = FailingGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="mq",
        calculator_service_name="Amazon MQ for RabbitMQ",
        region="ap-southeast-1",
        requirements={
            "engine_type": "rabbitmq",
            "requested_model": "mq.t3.micro",
            "broker_count": 3,
        },
        source_text="RabbitMQ，高可用，3个 Broker",
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text,
        component,
        "问题：原型号不可用，请选择官方型号。\n客户回答：选择 mq.m5.large",
    )

    assert revised.requirements["requested_model"] == "mq.m5.large"
    assert (
        revised.field_sources["requirements.requested_model"]
        == "customer_confirmation"
    )
    assert "requirements.requested_model" in revised.locked_fields


@pytest.mark.asyncio
async def test_component_model_choice_reads_answer_instead_of_question_candidates() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )

    class FailingGateway:
        async def complete_json(self, **_: object) -> dict[str, object]:
            raise AssertionError("closed customer choice must not call the model")

    parser._gateway = FailingGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="elasticache",
        calculator_service_name="Amazon ElastiCache for Redis",
        region="ap-northeast-1",
        requirements={
            "engine": "redis",
            "memory_gib": 16,
            "_review_selected_model": "cache.r6g.xlarge",
            "_review_selected_specifications": {"memoryGiB": 26.32},
        },
        source_text="Redis 每节点约16GB",
    )
    feedback = (
        "问题：客户需要 Redis 每节点约16G；AWS 相邻规格为"
        "cache.m4.xlarge（14.28G，偏低）、cache.r6g.xlarge（26.32G，不低配），请选择。\n"
        "客户回答：选择 cache.m4.xlarge"
    )

    revised = await parser.revise_component_from_feedback(
        component.source_text, component, feedback
    )

    assert revised.requirements["requested_model"] == "cache.m4.xlarge"
    assert "memory_gib" not in revised.requirements
    assert "_review_selected_model" not in revised.requirements


def test_explicit_purchase_plan_is_reconciled_from_each_component_source() -> None:
    component = ServiceRequirement(
        service="ec2",
        source_text="应用服务器 4 台，8核16G，购买方式三年全预付",
        requirements={"vcpu": 8, "memory_gib": 16},
    )
    intent = ParsedIntent(customer_summary="EC2 报价", services=[component])

    DeepSeekIntentParser._reconcile_explicit_capacities(component.source_text, intent)

    assert component.requirements["purchase_option"] == "standard_reserved"
    assert component.requirements["reserved_term_years"] == 3
    assert component.requirements["payment_option"] == "all_upfront"


class MutatingComponentGateway:
    async def complete_json(self, **_: object) -> dict[str, object]:
        return {
            "customer_summary": "AI 改写结果",
            "services": [
                {
                    "service": "ec2",
                    "calculator_service_name": "错误名称",
                    "region": "us-east-1",
                    "quantity": 99,
                    "hours_per_month": 100,
                    "requirements": {
                        "vcpu": 32,
                        "memory_gib": 128,
                        "operating_system": "windows",
                        "tenancy": "shared",
                    },
                    "source_text": "AI 改写的原文",
                }
            ],
            "ambiguities": [],
        }


@pytest.mark.asyncio
async def test_component_template_cannot_overwrite_customer_locked_fields() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    parser._gateway = MutatingComponentGateway()  # type: ignore[assignment]
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        region="ap-southeast-1",
        quantity=2,
        hours_per_month=730,
        requirements={"vcpu": 4, "memory_gib": 16, "operating_system": "linux"},
        source_text="新加坡 2 台 Linux EC2，每台 4 核 16G",
    )

    cleaned = await parser._cleanup_components(
        component.source_text,
        ParsedIntent(customer_summary="原始摘要", services=[component]),
    )

    result = cleaned.services[0]
    assert result.calculator_service_name == "Amazon EC2"
    assert result.region == "ap-southeast-1"
    assert result.quantity == 2
    assert result.hours_per_month == 730
    assert result.source_text == component.source_text
    assert result.requirements["vcpu"] == 4
    assert result.requirements["memory_gib"] == 16
    assert result.requirements["operating_system"] == "linux"
    assert "tenancy" not in result.requirements


def test_customer_correction_is_restored_after_a_stale_result() -> None:
    original = ServiceRequirement(
        service="opensearch",
        calculator_service_name="Amazon OpenSearch Service",
        region="ap-southeast-1",
        quantity=1,
        requirements={"data_nodes": 3, "total_storage_gib": 1024},
        field_sources={
            "requirements.total_storage_gib": "customer_correction",
            "requirements.data_nodes": "customer_confirmation",
        },
        locked_fields=[
            "requirements.total_storage_gib",
            "requirements.data_nodes",
        ],
    )
    stale = ServiceRequirement(
        service="opensearch",
        calculator_service_name="Amazon OpenSearch Service",
        region="ap-southeast-1",
        quantity=1,
        requirements={"data_nodes": 500, "total_storage_gib": 500 * 1024},
    )

    DeepSeekIntentParser._restore_authoritative_component_fields(original, stale)

    assert stale.requirements["data_nodes"] == 3
    assert stale.requirements["total_storage_gib"] == 1024
    assert stale.field_sources["requirements.total_storage_gib"] == "customer_correction"


@pytest.mark.asyncio
async def test_initial_intake_then_each_component_gets_its_own_prompt() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = CapturingWorkloadGateway()
    parser._gateway = gateway  # type: ignore[assignment]

    await parser.parse("Redis 一主一从，每节点 8 GiB；S3 对象存储 500GB")

    assert gateway.calls >= 5
    assert "只负责把客户原文按独立组件拆开" in gateway.system_prompts[0]
    assert "replicas_per_shard" not in gateway.system_prompts[0]
    assert any("replicas_per_shard" in prompt for prompt in gateway.system_prompts[1:])
    assert any("storage_class" in content for content in gateway.user_contents[1:])
    assert not any("结构化结果审核员" in prompt for prompt in gateway.system_prompts[1:])



@pytest.mark.asyncio
async def test_invalid_ai_structure_is_repaired_once() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = RepairingGateway()
    parser._gateway = gateway  # type: ignore[assignment]

    parsed = await parser.parse("Redis 一主一从，每节点 8 GiB")

    # Invalid intake gets one repair, then one validated component extraction.
    assert gateway.calls == 3
    assert parsed.services[0].quantity == 2


@pytest.mark.asyncio
async def test_missing_ai_summary_uses_customer_text_after_component_cleanup() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = MissingSummaryGateway()
    parser._gateway = gateway  # type: ignore[assignment]
    text = "Redis 一主一从，每节点 8 GiB"

    parsed = await parser.parse(text)

    assert gateway.calls == 2
    assert parsed.customer_summary == "已识别 1 项 AWS 配置；区域：待确认；Amazon ElastiCache for Redis × 2。"


@pytest.mark.asyncio
async def test_schema_repair_does_not_add_services_after_ai_cleanup() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = RepairStillMissingServiceGateway()
    parser._gateway = gateway  # type: ignore[assignment]

    parsed = await parser.parse("Redis 一主一从，每节点 8 GiB；对象存储使用 S3 1TB")

    # Intake repair plus extraction and audit for each explicit component.
    assert gateway.calls >= 6
    # AI owns interpretation, while the lossless completeness guard preserves
    # an explicitly named service if a cleanup pass accidentally drops it.
    assert {item.service for item in parsed.services} == {"elasticache", "s3"}


def test_compact_mixed_service_capacities_and_annual_transfer_are_lossless() -> None:
    text = (
        "Amazon EC2：4 vCPU、8 GiB 内存，40GB 系统盘、60GB 数据盘，数量 35。\n"
        "Amazon ElastiCache for Redis：Redis 主从，2 GB，数量 1。\n"
        "Amazon S3：对象存储，容量约 3 TB。\n"
        "公网流量：4 TB/年。"
    )
    parsed = ParsedIntent(
        customer_summary="mixed",
        services=[
            ServiceRequirement(
                service="ec2", quantity=35, source_text=text.splitlines()[0]
            ),
            ServiceRequirement(
                service="elasticache", source_text=text.splitlines()[1]
            ),
            ServiceRequirement(service="s3", source_text=text.splitlines()[2]),
            ServiceRequirement(
                service="data_transfer", source_text=text.splitlines()[3]
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert parsed.services[0].requirements["system_disk_gib"] == 40
    assert parsed.services[0].requirements["additional_ebs_volumes"] == [
        {"size_gib": 60, "volume_type": "gp3", "count_per_instance": 1}
    ]
    assert parsed.services[1].requirements["memory_gib"] == 2
    assert parsed.services[2].requirements["storage_gib"] == 3072
    assert parsed.services[3].requirements["data_transfer_out_gib"] == pytest.approx(
        4096 / 12
    )


def test_modern_service_audit_preserves_identity_units_and_eks_workers() -> None:
    text = """区域：亚太地区（东京）
Amazon Lambda｜请求量500万/月｜内存512MB｜运行时间3秒
Amazon DynamoDB｜存储500GB｜读写容量按需模式
Amazon EKS｜3个Worker节点｜m7g.large
Amazon Fargate｜CPU 4 vCPU｜内存16GB｜运行任务
Amazon Kinesis Data Streams｜2个Shard｜数据流处理
Amazon Athena｜每月查询数据量5TB｜数据分析
Amazon Glue｜10个ETL任务｜数据处理
Amazon SageMaker｜ml.m5.xlarge｜机器学习环境
Amazon Cognito｜10万用户｜用户认证服务
Amazon Secrets Manager｜100个Secret｜密钥管理
Amazon MQ｜RabbitMQ｜mq.m5.large｜消息队列"""
    parsed = ParsedIntent(
        customer_summary="audit",
        services=[
            ServiceRequirement(
                service="lambda",
                source_text=text.splitlines()[1],
                requirements={"request_count": 5_000_000, "memory_mb": 512},
            ),
            ServiceRequirement(
                service="dynamodb",
                source_text=text.splitlines()[2],
                requirements={"storage_gib": 512_000},
            ),
            ServiceRequirement(service="eks", source_text=text.splitlines()[3]),
            ServiceRequirement(
                service="fargate",
                source_text=text.splitlines()[4],
                requirements={"vcpu": 4, "memory_gib": 16_384},
            ),
            ServiceRequirement(service="kinesis", source_text=text.splitlines()[5]),
            ServiceRequirement(
                service="athena",
                source_text=text.splitlines()[6],
                requirements={"data_scanned_gib": 5_120_000},
            ),
            ServiceRequirement(service="glue", source_text=text.splitlines()[7]),
            ServiceRequirement(service="sagemaker", source_text=text.splitlines()[8]),
            ServiceRequirement(service="cognito", source_text=text.splitlines()[9]),
            ServiceRequirement(
                service="secrets_manager", source_text=text.splitlines()[10]
            ),
            ServiceRequirement(
                service="mq",
                calculator_service_name="Amazon MQ",
                source_text=text.splitlines()[11],
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._append_explicit_minimum_services(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_models(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    by_service = {item.service: item for item in parsed.services}
    assert "sqs" not in by_service
    assert by_service["lambda"].requirements == {
        "memory_mb": 512.0,
        "requests": 5_000_000.0,
        "duration_ms": 3000.0,
    }
    assert by_service["dynamodb"].requirements["storage_gib"] == 500
    assert by_service["dynamodb"].requirements["capacity_mode"] == "on_demand"
    assert by_service["fargate"].requirements["task_vcpu"] == 4
    assert by_service["fargate"].requirements["task_memory_gib"] == 16
    assert by_service["athena"].requirements["data_scanned_gib"] == 5120
    assert by_service["glue"].requirements["job_count"] == 10
    assert by_service["cognito"].requirements["user_count"] == 100_000
    assert by_service["secrets_manager"].requirements["secret_count"] == 100
    assert by_service["mq"].requirements["requested_model"] == "mq.m5.large"
    assert by_service["mq"].requirements["engine_type"] == "rabbitmq"
    worker = next(
        item for item in parsed.services if item.service == "ec2" and "Worker" in item.source_text
    )
    assert worker.quantity == 3
    assert worker.requirements["requested_model"] == "m7g.large"


def test_explicit_models_and_redis_node_count_are_recovered_from_customer_text() -> None:
    text = (
        "EC2：c7i.xlarge × 2，4核 8G。\n"
        "RDS MySQL：db.m7g.large，2核 8G。\n"
        "Redis：cache.t4g.small × 2，1主1从。"
    )
    parsed = ParsedIntent(
        customer_summary="explicit",
        services=[
            ServiceRequirement(service="ec2", quantity=2, source_text=text.splitlines()[0]),
            ServiceRequirement(service="rds", source_text=text.splitlines()[1]),
            ServiceRequirement(
                service="elasticache",
                quantity=2,
                source_text=text.splitlines()[2],
                requirements={"shards": 1, "replicas_per_shard": 1},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_models(text, parsed)
    DeepSeekIntentParser._normalize_redis_group_quantity(parsed)

    assert parsed.services[0].requirements["requested_model"] == "c7i.xlarge"
    assert parsed.services[1].requirements["requested_model"] == "db.m7g.large"
    assert parsed.services[2].requirements["requested_model"] == "cache.t4g.small"
    assert parsed.services[2].quantity == 1


def test_explicit_rds_and_cache_engines_survive_component_cleanup() -> None:
    text = (
        "Amazon RDS for PostgreSQL：db.t4g.large，2核8G，Multi-AZ。\n"
        "Amazon ElastiCache for Redis：Redis OSS，1主1从。"
    )
    parsed = ParsedIntent(
        customer_summary="explicit engines",
        services=[
            ServiceRequirement(
                service="rds",
                source_text=text.splitlines()[0],
                requirements={"requested_model": "db.t4g.large"},
            ),
            ServiceRequirement(
                service="elasticache",
                source_text=text.splitlines()[1],
                requirements={"shards": 1, "replicas_per_shard": 1},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_engines(text, parsed)

    assert parsed.services[0].requirements["engine"] == "postgresql"
    assert parsed.services[1].requirements["engine"] == "redis"


def test_explicit_rds_storage_deployment_and_cache_topology_survive_cleanup() -> None:
    text = (
        "RDS PostgreSQL：db.t4g.large，2核8G，Multi-AZ，gp3 100GB，1套。\n"
        "Redis：1主1从，共2个节点，1套。"
    )
    parsed = ParsedIntent(
        customer_summary="lossless",
        services=[
            ServiceRequirement(
                service="rds",
                source_text=text.splitlines()[0],
                requirements={"system_disk_gib": 100},
            ),
            ServiceRequirement(
                service="elasticache",
                quantity=2,
                source_text=text.splitlines()[1],
                requirements={},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_service_architecture(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert parsed.services[0].requirements["deployment"] == "multi_az"
    assert parsed.services[0].requirements["storage_gib"] == 100
    assert "system_disk_gib" not in parsed.services[0].requirements
    assert parsed.services[1].requirements["shards"] == 1
    assert parsed.services[1].requirements["replicas_per_shard"] == 1


def test_explicit_load_balancer_omission_is_detected() -> None:
    parsed = ParsedIntent(
        customer_summary="EC2 和静态文件",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
            )
        ],
    )

    assert DeepSeekIntentParser._missing_explicit_services("前面需要一个负载均衡", parsed) == [
        "elastic-load-balancing"
    ]


def test_explicit_ec2_and_rds_omission_is_detected() -> None:
    parsed = ParsedIntent(
        customer_summary="Redis",
        services=[
            ServiceRequirement(service="elasticache", calculator_service_name="Amazon ElastiCache")
        ],
    )

    assert DeepSeekIntentParser._missing_explicit_services(
        "应用服务器：新加坡区域，3 台 Linux；数据库：MySQL 8.0", parsed
    ) == ["ec2", "rds"]


def test_explicit_capacities_override_wrong_model_values() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(
                service="elasticache",
                calculator_service_name="Amazon ElastiCache",
                source_text="Redis：单节点内存约 8G",
                requirements={"memory_gib": 16},
            ),
            ServiceRequirement(
                service="s3",
                calculator_service_name="Amazon S3",
                source_text="对象存储：存储约 3TB 图片",
                requirements={"storage_gib": 5120},
            ),
            ServiceRequirement(
                service="cloudfront",
                calculator_service_name="Amazon CloudFront",
                source_text="CDN：每月预计向公网下行约 5TB",
                requirements={"data_transfer_out_gib": 8192},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    assert parsed.services[0].requirements["memory_gib"] == 8
    assert parsed.services[1].requirements["storage_gib"] == 3072
    assert parsed.services[2].requirements["data_transfer_out_gib"] == 5120


def test_named_models_do_not_create_unstated_cpu_or_memory_constraints() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(
                service="ec2",
                source_text="应用服务器：型号 m7i.xlarge，每台 150GB gp3 系统盘",
                requirements={
                    "requested_model": "m7i.xlarge",
                    "vcpu": 8,
                    "memory_gib": 32,
                },
            ),
            ServiceRequirement(
                service="rds",
                source_text="数据库：型号 db.m7i.2xlarge，Multi-AZ",
                requirements={
                    "requested_model": "db.m7i.2xlarge",
                    "vcpu": 8,
                    "memory_gib": 64,
                },
            ),
            ServiceRequirement(
                service="elasticache",
                source_text="Redis：型号 cache.r7g.large",
                requirements={
                    "requested_model": "cache.r7g.large",
                    "vcpu": 2,
                    "memory_gib": 8,
                },
            ),
        ],
    )

    DeepSeekIntentParser._drop_specs_inferred_from_models("", parsed)

    for service in parsed.services:
        assert "vcpu" not in service.requirements
        assert "memory_gib" not in service.requirements


def test_named_model_keeps_explicit_customer_shape() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(
                service="ec2",
                source_text="应用服务器：型号 m7i.xlarge，客户明确要求 4 核 16G 内存",
                requirements={
                    "requested_model": "m7i.xlarge",
                    "vcpu": 4,
                    "memory_gib": 16,
                },
            )
        ],
    )

    DeepSeekIntentParser._drop_specs_inferred_from_models("", parsed)

    assert parsed.services[0].requirements["vcpu"] == 4
    assert parsed.services[0].requirements["memory_gib"] == 16


def test_single_workload_region_is_inherited_by_regional_services() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                region="ap-southeast-1",
            ),
            ServiceRequirement(service="rds", calculator_service_name="Amazon RDS"),
            ServiceRequirement(service="elasticache", calculator_service_name="Amazon ElastiCache"),
            ServiceRequirement(service="cloudfront", calculator_service_name="Amazon CloudFront"),
        ],
    )

    DeepSeekIntentParser._inherit_single_workload_region(parsed)

    assert parsed.services[1].region == "ap-southeast-1"
    assert parsed.services[2].region == "ap-southeast-1"
    assert parsed.services[3].region is None


def test_single_workload_region_ignores_global_and_removes_stale_question() -> None:
    parsed = ParsedIntent(
        customer_summary="新加坡工作负载",
        services=[
            ServiceRequirement(service="ec2", region="ap-southeast-1"),
            ServiceRequirement(service="rds"),
            ServiceRequirement(service="cloudfront", region="global"),
        ],
        ambiguities=["请确认这些区域型服务部署在哪个 AWS 区域。"],
    )

    DeepSeekIntentParser._inherit_single_workload_region(parsed)

    assert parsed.services[1].region == "ap-southeast-1"
    assert parsed.services[2].region == "global"
    assert parsed.ambiguities == []


def test_regional_s3_global_label_inherits_the_only_concrete_region() -> None:
    parsed = ParsedIntent(
        customer_summary="新加坡工作负载",
        services=[
            ServiceRequirement(service="ec2", region="ap-southeast-1"),
            ServiceRequirement(
                service="s3",
                calculator_service_name="Amazon S3",
                region="全球",
                requirements={"storage_gib": 30720, "storage_class": "standard"},
            ),
        ],
    )

    DeepSeekIntentParser._normalize_invalid_global_regions(parsed)
    DeepSeekIntentParser._inherit_single_workload_region(parsed)

    assert parsed.services[1].region == "ap-southeast-1"
    assert parsed.ambiguities == []


def test_regional_s3_without_region_inherits_first_region_in_multi_region_quote() -> None:
    parsed = ParsedIntent(
        customer_summary="多区域工作负载",
        services=[
            ServiceRequirement(service="ec2", region="ap-northeast-1"),
            ServiceRequirement(service="rds", region="us-west-2"),
            ServiceRequirement(
                service="s3",
                calculator_service_name="Amazon S3",
                region="global",
                requirements={"storage_gib": 30720, "storage_class": "standard"},
            ),
            ServiceRequirement(service="cloudfront", region="global"),
        ],
    )

    DeepSeekIntentParser._normalize_invalid_global_regions(parsed)
    DeepSeekIntentParser._inherit_single_workload_region(
        parsed,
        source_text=(
            "区域：东京（ap-northeast-1）\n"
            "另一个组件：俄勒冈（us-west-2）\n"
            "S3：30TB"
        ),
    )

    assert parsed.services[2].region == "ap-northeast-1"
    assert parsed.services[3].region == "global"
    assert parsed.ambiguities == []


def test_component_region_conflict_is_not_silently_inherited() -> None:
    parsed = ParsedIntent(
        customer_summary="区域冲突",
        services=[
            ServiceRequirement(service="ec2", region="ap-northeast-1"),
            ServiceRequirement(
                service="s3",
                calculator_service_name="Amazon S3",
                source_text="S3 部署在东京或悉尼",
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_regions(
        "区域：东京\nS3 部署在东京或悉尼", parsed
    )
    DeepSeekIntentParser._inherit_single_workload_region(
        parsed, source_text="区域：东京\nS3 部署在东京或悉尼"
    )

    assert parsed.services[1].region is None
    assert parsed.services[1].field_sources["region"] == "customer_region_conflict"
    assert len(parsed.ambiguities) == 1


def test_opensearch_optional_node_role_question_is_not_blocking() -> None:
    question = (
        "OpenSearch 3节点架构：未明确是3个独立节点还是包含Master、Data、"
        "Coordinating角色的集群，请确认。"
    )

    assert DeepSeekIntentParser._is_optional_opensearch_role_question(question)


def test_generic_kafka_is_normalized_to_managed_msk_without_question() -> None:
    parsed = ParsedIntent(
        customer_summary="Kafka",
        services=[ServiceRequirement(service="s3")],
        ambiguities=[],
    )

    DeepSeekIntentParser._append_explicit_minimum_services(
        "Kafka消息队列，3节点，每台4核16G。", parsed
    )

    assert any(item.service == "msk" for item in parsed.services)
    assert parsed.ambiguities == []


def test_explicit_self_hosted_kafka_still_uses_managed_msk_policy() -> None:
    keys = DeepSeekIntentParser._inventory_keys_for_line(
        "EC2 自建 Kafka，3 个节点。"
    )

    assert [key for key, _ in keys] == ["msk"]


def test_explicit_jakarta_region_overrides_ai_sydney_guess() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(service="ec2", region="ap-southeast-2"),
            ServiceRequirement(service="rds", region="ap-southeast-2"),
            ServiceRequirement(service="cloudfront", region=None),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_regions(
        "部署区域：亚太地区（雅加达）。", parsed
    )

    assert parsed.services[0].region == "ap-southeast-3"
    assert parsed.services[1].region == "ap-southeast-3"
    assert parsed.services[2].region is None


def test_component_region_overrides_global_default_without_affecting_siblings() -> None:
    parsed = ParsedIntent(
        customer_summary="多区域测试",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                source_text="应用服务器：4台，8核16G。",
            ),
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                source_text=(
                    "Amazon EC2：需要1台服务器，区域为悉尼（ap-southeast-2），"
                    "Linux系统，8核16G。"
                ),
            ),
            ServiceRequirement(
                service="data_transfer",
                region="ap-southeast-1",
                source_text="公网出站流量：新加坡区域每月5TB。",
            ),
            ServiceRequirement(service="cloudfront", region=None),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_regions(
        "区域：新加坡（ap-southeast-1）\n"
        "1、应用服务器：4台，8核16G。\n"
        "2、Amazon EC2：需要1台服务器，区域为悉尼（ap-southeast-2）。\n"
        "3、公网出站流量：新加坡区域每月5TB。",
        parsed,
    )

    assert parsed.services[0].region == "ap-southeast-1"
    assert parsed.services[0].field_sources["region"] == "customer_global_default"
    assert "region" not in parsed.services[0].locked_fields
    assert parsed.services[1].region == "ap-southeast-2"
    assert parsed.services[1].field_sources["region"] == "customer_text"
    assert "region" in parsed.services[1].locked_fields
    assert parsed.services[2].region == "ap-southeast-1"
    assert parsed.services[3].region is None


def test_service_line_regions_are_not_mistaken_for_a_global_default() -> None:
    parsed = ParsedIntent(
        customer_summary="多区域测试",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                source_text="EC2：区域为悉尼，1台。",
            ),
            ServiceRequirement(
                service="rds",
                region="ap-southeast-1",
                source_text="RDS：区域为新加坡，1套。",
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_regions(
        "1、EC2：区域为悉尼，1台。\n2、RDS：区域为新加坡，1套。",
        parsed,
    )

    assert parsed.services[0].region == "ap-southeast-2"
    assert parsed.services[1].region == "ap-southeast-1"


def test_conflicting_regions_inside_one_component_require_confirmation() -> None:
    parsed = ParsedIntent(
        customer_summary="冲突测试",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                source_text="EC2 区域写了新加坡，同时又写悉尼，请确认。",
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_regions("", parsed)

    assert parsed.services[0].region is None
    assert len(parsed.ambiguities) == 1
    assert "多个区域" in parsed.ambiguities[0]


def test_nonnumeric_usage_and_redis_set_count_cannot_become_capacity() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(
                service="elasticache",
                source_text="Amazon ElastiCache for Redis：Redis，1主1从，共2个节点，1套。",
                requirements={"memory_gib": 1024},
            ),
            ServiceRequirement(
                service="s3",
                source_text="Amazon S3：标准对象存储，按实际存储量计费。",
                requirements={"storage_gib": 1},
            ),
            ServiceRequirement(
                service="data_transfer",
                source_text="公网出网流量：按实际使用流量计费。",
                requirements={"data_transfer_out_gib": 1},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    assert "memory_gib" not in parsed.services[0].requirements
    assert "storage_gib" not in parsed.services[1].requirements
    assert "data_transfer_out_gib" not in parsed.services[2].requirements


def test_explicit_redis_node_memory_is_preserved() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(
                service="elasticache",
                source_text="Redis 一主一从，每个节点内存不低于 8 GiB。",
                requirements={"memory_gib": 1024},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    assert parsed.services[0].requirements["memory_gib"] == 8


def test_compact_compute_rows_keep_disk_memory_and_bandwidth_separate() -> None:
    parsed = ParsedIntent(
        customer_summary="compute and database",
        services=[
            ServiceRequirement(
                service="ec2",
                source_text="Amazon EC2｜4核16G｜500GB系统盘｜20Mbps公网带宽",
                requirements={
                    "vcpu": 4,
                    "memory_gib": 16,
                    "system_disk_gib": 16,
                    "data_transfer_out_gib": 20,
                },
            ),
            ServiceRequirement(
                service="rds",
                source_text="Amazon RDS MySQL｜8核32G｜500GB存储｜主备高可用",
                requirements={"vcpu": 8, "memory_gib": 32768, "storage_gib": 500},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    assert parsed.services[0].requirements["system_disk_gib"] == 500
    assert parsed.services[0].requirements["memory_gib"] == 16
    assert "data_transfer_out_gib" not in parsed.services[0].requirements
    assert parsed.services[1].requirements["vcpu"] == 8
    assert parsed.services[1].requirements["memory_gib"] == 32
    assert parsed.services[1].requirements["storage_gib"] == 500


def test_redis_service_alias_is_normalized_to_elasticache() -> None:
    normalized = DeepSeekIntentParser._normalize(
        {
            "customer_summary": "Redis",
            "services": [
                {
                    "service": "redis",
                    "quantity": 2,
                    "requirements": {"engine": "redis", "memory_gib": 8},
                }
            ],
            "ambiguities": [],
        }
    )

    assert normalized["services"][0]["service"] == "elasticache"  # type: ignore[index]


def test_single_service_returned_at_root_is_wrapped_as_services_list() -> None:
    normalized = DeepSeekIntentParser._normalize(
        {
            "service": "ec2",
            "calculator_service_name": "Amazon EC2",
            "quantity": 3,
            "requirements": {"vcpu": 1, "memory_gib": 8},
            "ambiguities": [],
        },
        fallback_summary="东京 EC2",
    )

    parsed = ParsedIntent.model_validate(normalized)

    assert parsed.customer_summary == "东京 EC2"
    assert len(parsed.services) == 1
    assert parsed.services[0].service == "ec2"


def test_alb_backend_reference_does_not_create_extra_ec2_workload() -> None:
    parsed = ParsedIntent(
        customer_summary="负载均衡",
        services=[
            ServiceRequirement(
                service="elastic-load-balancing",
                calculator_service_name="Elastic Load Balancing",
            ),
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                source_text="后端挂 3 台应用服务器",
            ),
        ],
    )

    DeepSeekIntentParser._drop_referenced_only_ec2(
        "负载均衡：1 个 ALB，后端挂 3 台应用服务器，HTTPS 访问。",
        parsed,
    )

    assert [item.service for item in parsed.services] == ["elastic-load-balancing"]


def test_explicit_architecture_conflicts_survive_small_model_output() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(service="rds", requirements={"deployment": "single_az"}),
            ServiceRequirement(service="elasticache", requirements={"memory_gib": 8}),
            ServiceRequirement(service="elb", requirements={"load_balancer_type": "application"}),
        ],
    )
    text = (
        "数据库用 Single-AZ，但要求主备自动故障切换。"
        "Redis 整套缓存只需要 1G，但每个节点至少 8G。"
        "使用 Application Load Balancer，固定一个公网 IP，IP 永远不变。"
    )

    DeepSeekIntentParser._append_explicit_design_conflicts(text, parsed)

    assert parsed.ambiguities == [
        "RDS Single-AZ 与主备自动故障切换冲突",
        "ALB 不支持固定公网 IP",
        "Redis 整套 1G 与每节点 8G 的要求冲突",
    ]


def test_cross_service_design_conflicts_are_detected_before_pricing() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[ServiceRequirement(service="ec2", requirements={"vcpu": 4})],
    )
    text = (
        "服务器全部放在一个可用区，同时要求可用区故障时切到另一个可用区。"
        "RDS 使用 Multi-AZ，并让备用库跑只读查询。"
        "Redis 两节点部署在同一个可用区，但要求可用区故障时自动切换。"
        "NLB 按 URL 路径把 /api 和 /static 转发到不同目标。"
        "S3 Standard 七天后自动转成 S3 Express One Zone。"
        "CloudFront 要求固定不变的公网 IP。"
    )

    DeepSeekIntentParser._append_explicit_design_conflicts(text, parsed)

    assert len(parsed.ambiguities) == 6
    assert "NLB 不支持按 URL 路径转发" in parsed.ambiguities
    assert any("Anycast Static IP" in item for item in parsed.ambiguities)


def test_ec2_availability_zone_conflict_is_not_assigned_to_redis() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(service="ec2", requirements={"vcpu": 4}),
            ServiceRequirement(service="elasticache", requirements={"memory_gib": 8}),
        ],
    )
    text = (
        "Redis 一主一从，共 2 个节点，单节点 8G 内存。\n"
        "3 台 EC2 全部放在同一个可用区，但希望单个可用区故障时应用自动保持高可用。"
    )

    DeepSeekIntentParser._append_explicit_design_conflicts(text, parsed)

    assert parsed.ambiguities == ["EC2 单可用区部署与跨可用区自动切换要求冲突"]


def test_numbered_customer_acceptance_resolves_the_matching_question() -> None:
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[ServiceRequirement(service="ec2", requirements={"vcpu": 4})],
    )
    text = (
        "3 台 EC2 全部放在同一个可用区，但希望单个可用区故障时应用自动保持高可用。\n\n"
        "【客户确认回复】\n1 同意"
    )

    DeepSeekIntentParser._append_explicit_design_conflicts(text, parsed)

    assert parsed.ambiguities == []


def test_plain_customer_acceptance_resolves_all_questions_on_current_page() -> None:
    notices = [
        "EC2 单可用区部署与跨可用区自动切换要求冲突",
        "ALB 不支持固定公网 IP",
    ]
    text = "原始需求\n\n【客户确认回复】\n同意"

    remaining = DeepSeekIntentParser._apply_confirmation_replies(notices, text)

    assert remaining == []


def test_plain_acceptance_is_not_sent_back_as_a_new_workload() -> None:
    text = "东京 2 台 EC2。\n\n【客户确认回复】\n同意"

    assert DeepSeekIntentParser._text_for_ai(text) == "东京 2 台 EC2。"


def test_customer_reply_with_new_configuration_is_kept_as_supplement() -> None:
    text = "Redis 一主一从。\n\n【客户确认回复】\nRedis 每节点 8G"

    assert DeepSeekIntentParser._text_for_ai(text) == (
        "Redis 一主一从。\n\n客户补充确认：\nRedis 每节点 8G"
    )


def test_model_added_services_are_removed_when_customer_only_asks_for_ec2() -> None:
    parsed = ParsedIntent(
        customer_summary="错误地带入旧需求",
        services=[
            ServiceRequirement(
                service="ec2",
                quantity=3,
                requirements={"vcpu": 1, "memory_gib": 8, "system_disk_gib": 100},
                source_text="旧的 EC2 需求",
            ),
            ServiceRequirement(service="rds", requirements={"engine": "mysql"}),
            ServiceRequirement(service="elasticache", requirements={"engine": "redis"}),
            ServiceRequirement(service="elastic-load-balancing"),
            ServiceRequirement(service="s3", requirements={"storage_gib": 2048}),
            ServiceRequirement(service="cloudfront", requirements={"data_transfer_out_gib": 5120}),
        ],
    )
    text = "东京区域需要 2 台 Linux EC2，4核16G，每台 200GB gp3 系统盘，按需运行整月。"

    DeepSeekIntentParser._drop_unrequested_services(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert [item.service for item in parsed.services] == ["ec2"]
    assert parsed.services[0].quantity == 2
    assert parsed.services[0].requirements["vcpu"] == 4
    assert parsed.services[0].requirements["memory_gib"] == 16
    assert parsed.services[0].requirements["system_disk_gib"] == 200


def test_bare_ec2_instance_models_count_as_explicit_ec2_request() -> None:
    text = (
        "开发环境：m6g.large，2核8G，100G 存储，1 台\n"
        "生产环境：c6g.xlarge，4核8G，100G 存储，2 台"
    )
    parsed = ParsedIntent(
        customer_summary=text,
        services=[
            ServiceRequirement(service="amazon_ec2", source_text=text.splitlines()[0]),
            ServiceRequirement(service="ec2", source_text=text.splitlines()[1]),
        ],
    )

    DeepSeekIntentParser._drop_unrequested_services(text, parsed)
    DeepSeekIntentParser._drop_referenced_only_ec2(text, parsed)

    assert len(parsed.services) == 2
    assert DeepSeekIntentParser._service_key("amazon_ec2") == "ec2"


def test_environment_lines_with_bare_models_survive_full_service_filter() -> None:
    text = (
        "开发环境：m6g.large，2核8G，100G 存储，1 台\n"
        "测试环境：m6g.large，2核8G，100G 存储，1 台\n"
        "生产环境：c6g.xlarge，4核8G，100G 存储，2 台"
    )
    parsed = ParsedIntent(
        customer_summary=text,
        services=[
            ServiceRequirement(
                service="compute",
                source_text=line,
                requirements={"requested_model": line.split("：", 1)[1].split("，", 1)[0]},
            )
            for line in text.splitlines()
        ],
    )

    DeepSeekIntentParser._drop_unrequested_services(text, parsed)
    DeepSeekIntentParser._drop_referenced_only_ec2(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_models(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert [item.service for item in parsed.services] == ["ec2", "ec2", "ec2"]
    assert [item.quantity for item in parsed.services] == [1, 1, 2]
    assert [item.requirements["requested_model"] for item in parsed.services] == [
        "m6g.large",
        "m6g.large",
        "c6g.xlarge",
    ]


def test_ai_ec2_disk_alias_is_canonicalized_before_validation() -> None:
    normalized = DeepSeekIntentParser._normalize(
        {
            "customer_summary": "开发环境 100G 存储",
            "services": [
                {
                    "service": "ec2",
                    "region": "eu-west-2",
                    "quantity": 1,
                    "requirements": {
                        "requested_model": "m6g.large",
                        "system_disk_size_gib": 100,
                    },
                    "source_text": "开发环境：m6g.large，100G 存储",
                }
            ],
        }
    )

    requirements = normalized["services"][0]["requirements"]
    assert requirements["system_disk_gib"] == 100
    assert "system_disk_size_gib" not in requirements


def test_explicit_auxiliary_services_are_recovered_with_customer_capacities() -> None:
    text = (
        "云硬盘\t全球\tgp3 云盘，每台 500GB，共 1000GB\n"
        "公网出网流量\t新加坡、悉尼、香港流量到国内用户\t"
        "按 1000GB/月 公网出网流量估算\n"
        "全球访问加速 GA\t全球\tAWS Global Accelerator，1 个加速器，"
        "按 1000GB/月 加速流量估算"
    )
    parsed = ParsedIntent.model_construct(customer_summary="辅助服务", services=[], ambiguities=[])

    DeepSeekIntentParser._append_explicit_minimum_services(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    services = {item.service: item for item in parsed.services}
    assert services["ebs"].quantity == 2
    assert services["ebs"].requirements["storage_gib"] == 500
    assert services["ebs"].requirements["total_storage_gib"] == 1000
    assert services["ebs"].requirements["volume_type"] == "gp3"
    assert services["data_transfer"].requirements["data_transfer_out_gib"] == 1000
    assert services["global_accelerator"].requirements["accelerators"] == 1
    assert services["global_accelerator"].requirements["data_transfer_out_gib"] == 1000


def test_cloudfront_traffic_cannot_bleed_into_global_accelerator() -> None:
    text = (
        "1、Amazon CloudFront：静态资源加速，每月公网流量8TB。\n"
        "2、AWS Global Accelerator：配置1个加速器。"
    )
    parsed = ParsedIntent(
        customer_summary="内容分发与全球加速",
        services=[
            ServiceRequirement(
                service="cloudfront",
                calculator_service_name="Amazon CloudFront",
                source_text="Amazon CloudFront：静态资源加速，每月公网流量8TB。",
            ),
            ServiceRequirement(
                service="global_accelerator",
                calculator_service_name="AWS Global Accelerator",
                source_text="AWS Global Accelerator：配置1个加速器。",
                # Simulate a model copying the neighbouring CloudFront field.
                requirements={"data_transfer_out_gib": 8192},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    services = {item.service: item for item in parsed.services}
    assert services["cloudfront"].requirements["data_transfer_out_gib"] == 8192
    assert "data_transfer_out_gib" not in services["global_accelerator"].requirements
    assert services["global_accelerator"].requirements["accelerators"] == 1


def test_repeated_storage_derives_missing_count_from_per_unit_and_total() -> None:
    parsed = ParsedIntent(
        customer_summary="重复资源",
        services=[
            ServiceRequirement(
                service="ebs",
                calculator_service_name="Amazon EBS",
                source_text="云硬盘：gp3，每块500GB，共1000GB",
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    disk = parsed.services[0]
    assert disk.quantity == 2
    assert disk.requirements == {
        "storage_gib": 500,
        "total_storage_gib": 1000,
        "volume_type": "gp3",
    }
    assert disk.field_sources["quantity"] == "customer_text"
    assert not parsed.ambiguities


@pytest.mark.parametrize(
    ("service", "source", "count_field", "per_field"),
    [
        ("msk", "Kafka每个Broker存储500GB，共1500GB", "broker_count", "storage_gib_per_broker"),
        (
            "opensearch",
            "OpenSearch每个数据节点存储500GB，总容量1500GB",
            "data_nodes",
            "storage_gib_per_node",
        ),
        ("mq", "RabbitMQ每个Broker存储100GB，合计300GB", "broker_count", "storage_gib_per_broker"),
    ],
)
def test_repeated_node_services_share_capacity_consistency_guard(
    service: str,
    source: str,
    count_field: str,
    per_field: str,
) -> None:
    parsed = ParsedIntent(
        customer_summary="重复节点",
        services=[ServiceRequirement(service=service, source_text=source)],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    requirements = parsed.services[0].requirements
    assert requirements[count_field] == 3
    assert requirements[per_field] in {100, 500}
    assert requirements["total_storage_gib"] in {300, 1500}
    assert not parsed.ambiguities


def test_conflicting_repeated_storage_becomes_one_customer_question() -> None:
    parsed = ParsedIntent(
        customer_summary="冲突容量",
        services=[
            ServiceRequirement(
                service="ebs",
                calculator_service_name="Amazon EBS",
                source_text="云硬盘数量3块，每块500GB，共1000GB",
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    assert parsed.services[0].quantity == 3
    assert len(parsed.ambiguities) == 1
    assert "不一致" in parsed.ambiguities[0]
    assert "500" in parsed.ambiguities[0]
    assert "1000" in parsed.ambiguities[0]


@pytest.mark.parametrize(
    ("source", "expected_count", "expected_per", "expected_total"),
    [
        ("云硬盘数量2块，每块500GB", 2, 500, 1000),
        ("2块云硬盘，总容量1000GB", 2, 500, 1000),
    ],
)
def test_ebs_derives_any_missing_member_of_capacity_equation(
    source: str,
    expected_count: int,
    expected_per: int,
    expected_total: int,
) -> None:
    parsed = ParsedIntent(
        customer_summary="云硬盘",
        services=[ServiceRequirement(service="ebs", source_text=source)],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    disk = parsed.services[0]
    assert disk.quantity == expected_count
    assert disk.requirements["storage_gib"] == expected_per
    assert disk.requirements["total_storage_gib"] == expected_total


def test_node_memory_is_not_mistaken_for_per_node_storage() -> None:
    parsed = ParsedIntent(
        customer_summary="节点内存",
        services=[
            ServiceRequirement(
                service="msk",
                source_text="Kafka 3个节点，每个节点16GB内存",
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    assert "storage_gib_per_broker" not in parsed.services[0].requirements
    assert "total_storage_gib" not in parsed.services[0].requirements


def test_opensearch_per_node_capacity_cannot_become_node_count() -> None:
    source = "Amazon OpenSearch Service，3个节点，每节点500GB存储，数量1套"
    parsed = ParsedIntent(
        customer_summary="OpenSearch",
        services=[ServiceRequirement(service="opensearch", source_text=source)],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)
    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    requirements = parsed.services[0].requirements
    assert requirements["data_nodes"] == 3
    assert requirements["storage_gib_per_node"] == 500
    assert requirements["total_storage_gib"] == 1500
    assert not parsed.ambiguities


def test_inventory_short_alias_does_not_match_inside_another_service_name() -> None:
    keys = {
        key
        for key, _ in DeepSeekIntentParser._inventory_keys_for_line(
            "Amazon EKS，Kubernetes集群，数量1套"
        )
    }

    assert keys == {"eks"}


def test_post_component_inventory_removes_cross_service_duplicate() -> None:
    text = """1、Amazon OpenSearch Service：3个节点，每节点500GB存储。
2、Amazon EKS：Kubernetes集群，数量1套。"""
    parsed = ParsedIntent(
        customer_summary="搜索与容器",
        services=[
            ServiceRequirement(
                service="opensearch",
                source_text="Amazon OpenSearch Service：3个节点，每节点500GB存储。",
            ),
            ServiceRequirement(
                service="eks",
                source_text="Amazon EKS：Kubernetes集群，数量1套。",
            ),
            ServiceRequirement(
                service="opensearch",
                source_text="Amazon EKS：Kubernetes集群，数量1套。",
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)

    assert [item.service for item in parsed.services] == ["opensearch", "eks"]


def test_component_evidence_rejects_capacity_used_as_node_count() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="opensearch",
        source_text="OpenSearch 3个节点，每节点500GB存储",
    )
    raw = {
        "component": {
            "service": "opensearch",
            "region": None,
            "quantity": 1,
            "requirements": {"data_nodes": 500},
            "field_evidence": {"requirements.data_nodes": "节点500"},
            "source_text": component.source_text,
            "query_action": None,
        }
    }

    with pytest.raises(ValueError, match="不是明确的数量表达"):
        parser._component_from_template_output(raw, component)


def test_selective_audit_only_flags_suspicious_incomplete_repeated_component() -> None:
    source = "OpenSearch 3个节点，每节点500GB存储"
    original = ServiceRequirement(service="opensearch", source_text=source)
    incomplete = ServiceRequirement(
        service="opensearch",
        source_text=source,
        requirements={"data_nodes": 3},
        field_evidence={"requirements.data_nodes": "3个节点"},
    )
    complete = ServiceRequirement(
        service="opensearch",
        source_text=source,
        requirements={
            "data_nodes": 3,
            "storage_gib_per_node": 500,
            "total_storage_gib": 1500,
        },
        field_evidence={
            "requirements.data_nodes": "3个节点",
            "requirements.storage_gib_per_node": "每节点500GB存储",
            "requirements.total_storage_gib": "system_derived",
        },
    )

    assert DeepSeekIntentParser._needs_selective_component_audit(original, incomplete)
    assert not DeepSeekIntentParser._needs_selective_component_audit(original, complete)


def test_component_template_derives_missing_ai_disk_count() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="ebs",
        calculator_service_name="Amazon EBS",
        source_text="云硬盘：gp3，每块500GB，共1000GB",
    )
    raw = {
        "component": {
            "service": "ebs",
            "calculator_service_name": "Amazon EBS",
            "region": None,
            "quantity": None,
            "hours_per_month": None,
            "requirements": {
                "storage_gib": 500,
                "total_storage_gib": 1000,
                "volume_type": "gp3",
            },
            "field_evidence": {
                "requirements.storage_gib": "每块500GB",
                "requirements.total_storage_gib": "共1000GB",
                "requirements.volume_type": "gp3",
            },
            "source_text": component.source_text,
            "query_action": None,
        }
    }

    result = parser._component_from_template_output(raw, component)

    assert result.quantity == 2
    assert result.field_evidence["quantity"] == "system_derived"


def test_component_template_accepts_ai_derived_disk_count_after_self_correction() -> None:
    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    component = ServiceRequirement(
        service="ebs",
        calculator_service_name="Amazon EBS",
        source_text="云硬盘：gp3，每块500GB，共1000GB",
    )
    raw = {
        "component": {
            "service": "ebs",
            "calculator_service_name": "Amazon EBS",
            "region": None,
            "quantity": 2,
            "hours_per_month": None,
            "requirements": {
                "storage_gib": 500,
                "total_storage_gib": 1000,
                "volume_type": "gp3",
            },
            "field_evidence": {
                "quantity": "system_derived",
                "requirements.storage_gib": "每块500GB",
                "requirements.total_storage_gib": "共1000GB",
                "requirements.volume_type": "gp3",
            },
            "source_text": component.source_text,
            "query_action": None,
        }
    }

    result = parser._component_from_template_output(raw, component)

    assert result.quantity == 2
    assert result.requirements["storage_gib"] == 500
    assert result.requirements["total_storage_gib"] == 1000


def test_ec2_ebs_disk_wording_does_not_create_standalone_ebs_service() -> None:
    text = "EC2 两台，每台 100GB EBS gp3 系统盘"
    parsed = ParsedIntent(
        customer_summary="EC2",
        services=[ServiceRequirement(service="ec2", source_text=text)],
    )

    DeepSeekIntentParser._append_explicit_minimum_services(text, parsed)

    assert [item.service for item in parsed.services] == ["ec2"]


def test_explicit_platform_services_are_never_lost_when_ai_omits_them() -> None:
    text = (
        "Amazon EKS：1 个集群。\n"
        "Amazon ECR 私有仓库：1 个。\n"
        "Amazon MSK：kafka.t3.small，3 个 Broker，每 Broker 100GB。\n"
        "Amazon OpenSearch：t3.small.search，2 个数据节点，每节点 50GB。\n"
        "AWS Secrets Manager：5 个 Secret。"
    )
    parsed = ParsedIntent.model_construct(customer_summary="平台组件", services=[], ambiguities=[])

    DeepSeekIntentParser._append_explicit_minimum_services(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_models(text, parsed)

    services = {item.service: item for item in parsed.services}
    assert set(services) == {"eks", "ecr", "msk", "opensearch", "secrets_manager"}
    assert services["eks"].requirements["cluster_count"] == 1
    assert services["ecr"].requirements["repositories"] == 1
    assert services["msk"].requirements["requested_model"] == "kafka.t3.small"
    assert services["msk"].requirements["broker_count"] == 3
    assert services["opensearch"].requirements["requested_model"] == "t3.small.search"
    assert services["opensearch"].requirements["data_nodes"] == 2
    assert services["secrets_manager"].requirements["secret_count"] == 5


def test_worker_node_root_disk_is_not_duplicated_as_standalone_ebs() -> None:
    source = "EKS Worker Node：t3.xlarge，2 个节点，每节点 gp3 100GB，Managed Node Group。"
    parsed = ParsedIntent(
        customer_summary="EKS",
        services=[
            ServiceRequirement(
                service="ec2",
                source_text=source,
                requirements={"requested_model": "t3.xlarge", "system_disk_gib": 100},
            ),
            ServiceRequirement(
                service="ebs",
                source_text=source,
                requirements={"storage_gib": 100, "volume_type": "gp3"},
            ),
        ],
    )

    DeepSeekIntentParser._drop_embedded_ebs_duplicates(parsed)

    assert [item.service for item in parsed.services] == ["ec2"]


def test_single_aggregate_auxiliary_line_is_not_multiplied_by_regions() -> None:
    text = (
        "云硬盘：全球，gp3，共 1000GB\n"
        "公网出网流量：新加坡、悉尼、香港合计 1000GB/月\n"
        "WAF：1 个 Web ACL，1000 万次请求/月"
    )
    parsed = ParsedIntent(
        customer_summary="测试",
        services=[
            ServiceRequirement(service="ebs", region="ap-southeast-1"),
            ServiceRequirement(service="ebs", region="ap-southeast-2"),
            ServiceRequirement(service="data_transfer", region="ap-southeast-1"),
            ServiceRequirement(service="data_transfer", region="ap-southeast-2"),
            ServiceRequirement(service="waf", region="global"),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    DeepSeekIntentParser._collapse_explicit_auxiliary_duplicates(text, parsed)

    assert [item.service for item in parsed.services] == ["ebs", "data_transfer", "waf"]
    assert parsed.services[0].region == "global"
    assert parsed.services[0].requirements["storage_gib"] == 1000
    assert parsed.services[1].requirements["data_transfer_out_gib"] == 1000
    assert parsed.services[2].requirements["requests"] == 10_000_000


def test_compact_redis_msk_and_s3_rows_preserve_literal_customer_fields() -> None:
    text = (
        "Amazon ElastiCache for Redis｜8GB × 2分片\n"
        "Amazon MSK｜3 Broker节点 m7g.large｜存储510GB\n"
        "Amazon S3｜500GB"
    )
    parsed = ParsedIntent(
        customer_summary="compact rows",
        services=[
            ServiceRequirement(
                service="elasticache",
                source_text=text.splitlines()[0],
                requirements={"requested_model": "8gb × 2分片", "engine": "redis"},
            ),
            ServiceRequirement(
                service="msk",
                source_text=text.splitlines()[1],
                requirements={"storage_gib": 510},
            ),
            ServiceRequirement(service="s3", source_text=text.splitlines()[2]),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    DeepSeekIntentParser._drop_specs_inferred_from_models(text, parsed)

    redis = parsed.services[0].requirements
    assert redis["memory_gib"] == 8
    assert redis["shards"] == 2
    assert "requested_model" not in redis

    msk = parsed.services[1].requirements
    assert msk["requested_model"] == "m7g.large"
    assert msk["broker_count"] == 3
    assert msk["storage_gib_per_broker"] == 510
    assert "storage_gib" not in msk

    assert parsed.services[2].requirements["storage_gib"] == 500


def test_inventory_preserves_mongodb_and_keeps_elk_separate_from_es() -> None:
    text = "\n".join(
        [
            "ES集群 5节点 16G内存",
            "MongoDB 2T",
            "日志系统 ELK 1套",
        ]
    )
    parsed = ParsedIntent(
        customer_summary="bad ai result",
        services=[
            ServiceRequirement(
                service="opensearch",
                source_text="ES集群 5节点 16G内存",
                requirements={"nodes": 5, "memory_gib": 16},
            ),
            ServiceRequirement(
                service="ec2",
                source_text="日志系统 ELK 1套",
                requirements={"vcpu": 2, "memory_gib": 4},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert [item.service for item in parsed.services] == [
        "opensearch",
        "documentdb",
        "opensearch",
    ]
    documentdb = parsed.services[1]
    assert documentdb.requirements["storage_gib"] == 2048
    assert "MongoDB" in documentdb.source_text
    assert "ELK" in parsed.services[2].source_text


def test_explicit_inventory_preserves_every_named_component_and_removes_msk_as_ec2() -> None:
    lines = [
        "Amazon ECS / Fargate",
        "Amazon EC2",
        "Amazon Aurora MySQL",
        "Amazon ElastiCache for Redis（8GB × 2分片）",
        "Amazon OpenSearch Service",
        "Amazon MSK（3 Broker节点 m7g.large，存储510GB）",
        "Amazon EMR",
        "AWS Glue",
        "Amazon Redshift",
        "Amazon S3（500GB）",
        "Amazon EFS",
        "Amazon API Gateway（5120MB最大入口请求）",
        "Amazon EventBridge Scheduler（1套）",
    ]
    parsed = ParsedIntent(
        customer_summary="bad ai result",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                source_text="Amazon MSK (3 Broker节点 m7g.large, 存储 510GB)",
                requirements={"requested_model": "m7g.large", "system_disk_gib": 510},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory("\n".join(lines), parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities("\n".join(lines), parsed)
    DeepSeekIntentParser._ensure_missing_region_ambiguity(parsed)

    assert [item.service for item in parsed.services] == [
        "ecs",
        "ec2",
        "rds",
        "elasticache",
        "opensearch",
        "msk",
        "emr",
        "glue",
        "redshift",
        "s3",
        "efs",
        "apigateway",
        "scheduler",
    ]
    assert len(parsed.services) == len(lines)
    msk = next(item for item in parsed.services if item.service == "msk")
    assert msk.requirements == {
        "requested_model": "m7g.large",
        "broker_count": 3,
        "storage_gib_per_broker": 510,
    }
    redis = next(item for item in parsed.services if item.service == "elasticache")
    assert redis.requirements["memory_gib"] == 8
    assert redis.requirements["shards"] == 2
    s3 = next(item for item in parsed.services if item.service == "s3")
    assert s3.requirements["storage_gib"] == 500
    assert parsed.ambiguities == ["请确认部署区域。"]


def test_analytics_services_keep_their_own_official_fields() -> None:
    parsed = ParsedIntent(
        customer_summary="大数据分析报价",
        services=[
            ServiceRequirement(
                service="emr",
                calculator_service_name="Amazon EMR",
                source_text="Spark大数据计算集群，主节点1个，核心节点5个",
                requirements={"requested_model": "t1.micro"},
            ),
            ServiceRequirement(
                service="redshift",
                calculator_service_name="Amazon Redshift",
                source_text="数据仓库集群，存储容量：20TB",
                requirements={},
            ),
            ServiceRequirement(
                service="athena",
                calculator_service_name="Amazon Athena",
                source_text="用于查询S3数据湖中的分析数据",
                requirements={
                    "requested_model": "t1.micro",
                    "cluster_count": 1,
                    "storage_gib": 1,
                },
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    emr, redshift, athena = parsed.services
    assert emr.requirements["applications"] == ["spark"]
    assert emr.requirements["master_nodes"] == 1
    assert emr.requirements["core_nodes"] == 5
    assert "requested_model" not in emr.requirements
    assert redshift.requirements["storage_gib"] == 20 * 1024
    assert athena.requirements == {}


def test_service_identity_guard_replaces_wrong_rabbitmq_ec2_and_recovers_api_gateway() -> None:
    text = """区域：ap-southeast-1（新加坡）
1、消息队列：目前使用RabbitMQ，准备迁移到AWS，预计3个节点。
2、接口服务：需要提供API给外部系统调用。"""
    parsed = ParsedIntent(
        customer_summary="bad classification",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                quantity=3,
                source_text="消息队列：目前使用RabbitMQ，准备迁移到AWS，预计3个节点。",
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    assert [item.service for item in parsed.services] == ["mq", "apigateway"]
    rabbitmq = parsed.services[0]
    assert rabbitmq.quantity == 1
    assert rabbitmq.requirements == {
        "engine_type": "rabbitmq",
        "broker_count": 3,
    }
    assert "RabbitMQ" in rabbitmq.source_text
    assert "提供API给外部系统调用" in parsed.services[1].source_text


def test_service_identity_guard_enforces_managed_first_and_api_direction() -> None:
    self_hosted = DeepSeekIntentParser._inventory_keys_for_line(
        "明确要求在 EC2 自建 RabbitMQ 三节点集群"
    )
    outbound_only = DeepSeekIntentParser._inventory_keys_for_line(
        "应用服务器需要调用外部系统的 API"
    )

    assert [key for key, _ in self_hosted] == ["mq"]
    assert "apigateway" not in {key for key, _ in outbound_only}


def test_nacos_product_identity_beats_partial_capability_match() -> None:
    keys = DeepSeekIntentParser._inventory_keys_for_line(
        "Nacos：服务注册发现和配置中心，部署数量：3个节点"
    )

    assert keys == [("ec2", "Amazon EC2")]


def test_nacos_requires_clear_managed_or_self_hosted_decision_and_keeps_nodes() -> None:
    parsed = ParsedIntent(
        customer_summary="Nacos",
        services=[
            ServiceRequirement(
                service="cloud_map",
                calculator_service_name="AWS Cloud Map",
                quantity=1,
                region="ap-southeast-1",
                source_text="Nacos：服务注册发现和配置中心，部署数量：3个节点",
            )
        ],
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed)

    assert parsed.services[0].service == "ec2"
    assert parsed.services[0].quantity == 3
    assert parsed.services[0].requirements["operating_system"] == "linux"
    assert parsed.services[0].field_sources["_pending_architecture_decision"] == "system_policy"
    assert len(parsed.ambiguities) == 1
    assert "Cloud Map + AppConfig" in parsed.ambiguities[0]
    assert "3 个节点" in parsed.ambiguities[0]


def test_all_named_self_hosted_partial_replacements_enter_staged_workflow() -> None:
    parsed = ParsedIntent(
        customer_summary="XXL-JOB",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2（自建 XXL-JOB）",
                quantity=2,
                source_text="XXL-JOB 调度中心，部署 2 个节点",
            )
        ],
        ambiguities=[
            "XXL-JOB 没有完全等价的 AWS 托管服务，请选择 AWS 托管方案还是保留原产品自建。"
        ],
    )

    DeepSeekIntentParser._append_third_party_managed_decisions(parsed)

    assert (
        parsed.services[0].field_sources["_pending_architecture_decision"]
        == "system_policy"
    )


def test_multiline_blocks_repair_units_and_split_eks_worker_nodes() -> None:
    text = """Amazon EC2
区域：ap-southeast-1
规格：r6g.2xlarge
数量：2
系统：Linux

EC2云服务器
区域：新加坡
配置：4核16G
数量：3
系统：CentOS

Amazon RDS MySQL
规格：db.r6g.large
CPU：2核
内存：16GB
存储：500GB
部署：Multi-AZ

Redis缓存服务
配置：8GB
架构：主从
数量：1

Amazon S3
容量：50TB
存储类型：Standard

Kafka消息队列
配置：3节点
每台：4核16G
磁盘：500GB

Amazon EKS
Kubernetes集群
节点规格：8核32G
节点数量：3"""
    blocks = [part.strip() for part in text.split("\n\n")]
    parsed = ParsedIntent(
        customer_summary="bad scaled AI output",
        services=[
            ServiceRequirement(
                service="ec2",
                source_text=blocks[0],
                quantity=2,
                requirements={"requested_model": "r6g.2xlarge", "memory_gib": 65536},
            ),
            ServiceRequirement(
                service="ec2",
                source_text=blocks[1],
                quantity=3,
                requirements={"vcpu": 4, "memory_gib": 16384},
            ),
            ServiceRequirement(
                service="rds",
                source_text=blocks[2],
                requirements={"vcpu": 2, "memory_gib": 16384, "storage_gib": 512000},
            ),
            ServiceRequirement(
                service="elasticache",
                source_text=blocks[3],
                requirements={"memory_gib": 8192},
            ),
            ServiceRequirement(
                service="s3",
                source_text=blocks[4],
                requirements={"storage_gib": 52428800},
            ),
            ServiceRequirement(
                service="msk",
                source_text=blocks[5],
                requirements={"memory_gib": 16384, "system_disk_gib": 512000},
            ),
            ServiceRequirement(
                service="eks",
                source_text=blocks[6],
                requirements={"vcpu": 8, "memory_gib": 32768},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_engines(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    shaped_ec2 = next(
        item
        for item in parsed.services
        if item.service == "ec2" and item.requirements.get("vcpu") == 4
    )
    assert shaped_ec2.quantity == 3
    assert shaped_ec2.requirements["memory_gib"] == 16

    rds = next(item for item in parsed.services if item.service == "rds")
    assert rds.requirements["vcpu"] == 2
    assert rds.requirements["memory_gib"] == 16
    assert rds.requirements["storage_gib"] == 500

    redis = next(item for item in parsed.services if item.service == "elasticache")
    assert redis.requirements["memory_gib"] == 8

    s3 = next(item for item in parsed.services if item.service == "s3")
    assert s3.requirements["storage_gib"] == 50 * 1024

    msk = next(item for item in parsed.services if item.service == "msk")
    assert msk.requirements["broker_count"] == 3
    assert msk.requirements["vcpu"] == 4
    assert msk.requirements["memory_gib"] == 16
    assert msk.requirements["storage_gib_per_broker"] == 500
    assert "system_disk_gib" not in msk.requirements

    eks = next(item for item in parsed.services if item.service == "eks")
    assert "vcpu" not in eks.requirements
    assert "memory_gib" not in eks.requirements
    worker = next(
        item
        for item in parsed.services
        if item.service == "ec2" and item.calculator_service_name == "Amazon EC2 (EKS Worker Nodes)"
    )
    assert worker.quantity == 3
    assert worker.requirements == {
        "vcpu": 8,
        "memory_gib": 32,
        "operating_system": "Linux",
    }


def test_inventory_binds_following_form_lines_to_the_service_heading() -> None:
    text = """EC2云服务器
区域：新加坡
配置：4核16G
数量：3

Amazon ElastiCache Redis
配置：8GB
架构：主从
数量：1

Amazon MSK
Broker数量：3
每台：4核16G
磁盘：500GB"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(service="ec2", source_text="EC2云服务器"),
            ServiceRequirement(service="elasticache", source_text="Amazon ElastiCache Redis"),
            ServiceRequirement(service="msk", source_text="Amazon MSK"),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)

    ec2, redis, msk = parsed.services
    assert ec2.quantity == 3
    assert ec2.requirements == {"vcpu": 4, "memory_gib": 16}
    assert redis.requirements["memory_gib"] == 8
    assert msk.requirements == {
        "broker_count": 3,
        "vcpu": 4,
        "memory_gib": 16,
        "storage_gib_per_broker": 500,
    }


def test_vague_customer_values_are_questions_not_silent_guesses() -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                quantity=3,
                source_text="后台服务4核16G，两三台",
                requirements={"vcpu": 4, "memory_gib": 16},
            ),
            ServiceRequirement(
                service="elasticache",
                calculator_service_name="Amazon ElastiCache Redis",
                source_text="Redis大概十几个G，一主一从",
            ),
            ServiceRequirement(
                service="s3",
                calculator_service_name="Amazon S3",
                source_text="图片预计几十T存储",
            ),
            ServiceRequirement(
                service="msk",
                calculator_service_name="Amazon MSK",
                source_text="Kafka大概几个节点",
            ),
            ServiceRequirement(
                service="eks",
                calculator_service_name="Amazon EKS",
                source_text="K8S环境需要跑几个服务",
            ),
        ],
    )

    DeepSeekIntentParser._append_vague_value_questions(parsed)

    combined = "\n".join(parsed.ambiguities)
    assert "两三台" in combined
    assert "Redis" in combined and "每个节点" in combined
    assert "S3" in combined and "存储容量" in combined
    assert "MSK" in combined and "Broker" in combined
    assert "EKS" not in combined


def test_labeled_broker_count_cannot_be_taken_from_region_suffix() -> None:
    source = """Amazon MSK
区域：ap-southeast-1
Broker数量：3
规格：kafka.m5.large
磁盘：500GB/节点"""
    parsed = ParsedIntent(
        customer_summary="AI hallucinated another region",
        services=[
            ServiceRequirement(
                service="msk",
                calculator_service_name="Amazon MSK",
                region="ap-southeast-1",
                requirements={
                    "broker_count": 3,
                    "requested_model": "kafka.m5.large",
                    "storage_gib_per_broker": 500,
                },
                source_text=source,
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)
    DeepSeekIntentParser._replace_untrusted_customer_summary(parsed)

    assert parsed.services[0].requirements["broker_count"] == 3
    assert parsed.services[0].requirements["requested_model"] == "m5.large"
    assert parsed.services[0].requirements["storage_gib_per_broker"] == 500
    assert "ap-southeast-1" in parsed.customer_summary
    assert "雅加达" not in parsed.customer_summary


def test_ai_invented_model_is_removed_when_customer_never_wrote_it() -> None:
    source = "Amazon MSK：三个 Broker，每节点存储按客户容量要求"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="msk",
                source_text=source,
                requirements={"requested_model": "m7g.large", "broker_count": 3},
            )
        ],
    )

    DeepSeekIntentParser._drop_unwritten_requested_models(source, parsed)

    assert "requested_model" not in parsed.services[0].requirements


def test_explicit_model_is_retained_only_for_its_source_component() -> None:
    msk_source = "Amazon MSK：型号 kafka.m5.large，三个 Broker"
    redis_source = "Amazon ElastiCache Redis：主从部署"
    text = f"{msk_source}\n{redis_source}"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="msk",
                source_text=msk_source,
                requirements={"requested_model": "kafka.m5.large"},
            ),
            ServiceRequirement(
                service="elasticache",
                source_text=redis_source,
                requirements={"requested_model": "kafka.m5.large"},
            ),
        ],
    )

    DeepSeekIntentParser._drop_unwritten_requested_models(text, parsed)

    assert parsed.services[0].requirements["requested_model"] == "kafka.m5.large"
    assert "requested_model" not in parsed.services[1].requirements


def test_colon_labeled_memory_survives_when_customer_also_gives_model() -> None:
    rds_source = """Amazon RDS PostgreSQL
规格：db.m6g.large
CPU：2 vCPU
内存：8 GiB
存储：300GB"""
    redis_source = """Amazon ElastiCache Redis
规格：cache.r7g.large
内存：13GB"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="rds",
                source_text=rds_source,
                requirements={"requested_model": "db.m6g.large", "vcpu": 2, "memory_gib": 8},
            ),
            ServiceRequirement(
                service="elasticache",
                source_text=redis_source,
                requirements={"requested_model": "cache.r7g.large", "memory_gib": 13},
            ),
        ],
    )

    DeepSeekIntentParser._drop_specs_inferred_from_models(
        f"{rds_source}\n\n{redis_source}", parsed
    )

    assert parsed.services[0].requirements["memory_gib"] == 8
    assert parsed.services[1].requirements["memory_gib"] == 13


def test_aurora_cluster_members_and_opensearch_nodes_are_lossless() -> None:
    aurora_source = """Amazon Aurora MySQL
区域：ap-southeast-1
实例规格：db.r7g.large
节点数量：2
存储：500GB
部署方式：高可用"""
    search_source = """Amazon OpenSearch Service
区域：ap-southeast-1
节点数量：3
节点规格：r6g.large.search
磁盘：500GB/节点"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="rds",
                quantity=1,
                source_text=aurora_source,
                requirements={
                    "requested_model": "db.r7g.large",
                    "storage_gib": 500,
                    "multi_az": True,
                },
            ),
            ServiceRequirement(
                service="opensearch",
                quantity=1,
                source_text=search_source,
                requirements={
                    "requested_model": "r6g.large.search",
                    "storage_gib": 500,
                },
            ),
        ],
    )

    text = f"{aurora_source}\n\n{search_source}"
    DeepSeekIntentParser._reconcile_explicit_engines(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_service_architecture(text, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    preserve_customer_configuration(parsed)

    aurora, search = parsed.services
    assert aurora.quantity == 1
    assert aurora.requirements["engine"] == "aurora_mysql"
    assert aurora.requirements["aurora_cluster"] is True
    assert aurora.requirements["deployment"] == "multi_az"
    assert aurora.requirements["cluster_members"] == 2
    assert "multi_az" not in aurora.requirements
    assert aurora.calculator_service_name == "Amazon Aurora MySQL"
    assert search.requirements["data_nodes"] == 3
    assert search.requirements["storage_gib_per_node"] == 500
    assert "storage_gib" not in search.requirements


def test_aurora_high_availability_uses_minimum_members_without_rewriting_product() -> None:
    source = (
        "Amazon Aurora MySQL，MySQL兼容数据库，高可用部署，"
        "存储容量2TB，数量1套"
    )
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="rds",
                calculator_service_name="Amazon RDS / Amazon Aurora",
                quantity=1,
                source_text=source,
                requirements={"engine": "aurora_mysql", "storage_gib": 2048},
            )
        ],
    )

    preserve_customer_configuration(parsed)

    aurora = parsed.services[0]
    assert aurora.service == "rds"
    assert aurora.calculator_service_name == "Amazon Aurora MySQL"
    assert aurora.quantity == 1
    assert aurora.requirements["deployment"] == "multi_az"
    assert aurora.requirements["cluster_members"] == 2
    assert aurora.field_sources["requirements.cluster_members"] == "system_minimum"


def test_plain_rds_mysql_never_uses_the_combined_rds_aurora_display_name() -> None:
    parsed = ParsedIntent(
        customer_summary="RDS MySQL",
        services=[
            ServiceRequirement(
                service="rds",
                calculator_service_name="Amazon RDS / Amazon Aurora",
                source_text=(
                    "Amazon RDS MySQL：区域：新加坡（ap-southeast-1），"
                    "配置4核16GB，存储500GB，Multi-AZ高可用"
                ),
                requirements={
                    "engine": "mysql",
                    "vcpu": 4,
                    "memory_gib": 16,
                    "storage_gib": 500,
                    "deployment": "multi_az",
                    "aurora_cluster": True,
                },
            )
        ],
    )

    preserve_customer_configuration(parsed)

    database = parsed.services[0]
    assert database.calculator_service_name == "Amazon RDS MySQL"
    assert database.product_identity == "rds_mysql"
    assert database.requirements["engine"] == "mysql"
    assert "aurora_cluster" not in database.requirements


@pytest.mark.parametrize(
    ("service", "source", "requirements", "identity", "display", "locked_field", "locked_value"),
    [
        (
            "rds",
            "Amazon Aurora PostgreSQL，高可用部署",
            {"engine": "postgresql"},
            "aurora_postgresql",
            "Amazon Aurora PostgreSQL",
            "engine",
            "aurora_postgresql",
        ),
        (
            "elasticache",
            "Amazon ElastiCache for Valkey，2个节点",
            {"engine": "redis"},
            "elasticache_valkey",
            "Amazon ElastiCache for Valkey",
            "engine",
            "valkey",
        ),
        (
            "elb",
            "使用 Network Load Balancer（NLB）",
            {"load_balancer_type": "application"},
            "network_load_balancer",
            "Network Load Balancer",
            "load_balancer_type",
            "network",
        ),
        (
            "mq",
            "Amazon MQ for RabbitMQ，3个 Broker",
            {"engine_type": "activemq"},
            "amazon_mq_rabbitmq",
            "Amazon MQ for RabbitMQ",
            "engine_type",
            "rabbitmq",
        ),
        (
            "apigateway",
            "Amazon API Gateway REST API",
            {"api_type": "http"},
            "api_gateway_rest",
            "Amazon API Gateway REST API",
            "api_type",
            "rest",
        ),
        (
            "msk",
            "Amazon MSK Serverless",
            {"cluster_type": "provisioned"},
            "amazon_msk_serverless",
            "Amazon MSK Serverless",
            "cluster_type",
            "serverless",
        ),
        (
            "fsx",
            "Amazon FSx for Lustre，10TB",
            {"file_system_type": "windows"},
            "amazon_fsx_lustre",
            "Amazon FSx for Lustre",
            "file_system_type",
            "lustre",
        ),
    ],
)
def test_shared_pricing_families_keep_independent_customer_product_identity(
    service: str,
    source: str,
    requirements: dict[str, object],
    identity: str,
    display: str,
    locked_field: str,
    locked_value: str,
) -> None:
    parsed = ParsedIntent(
        customer_summary="product identity",
        services=[
            ServiceRequirement(
                service=service,
                source_text=source,
                requirements=requirements,
            )
        ],
    )

    preserve_customer_configuration(parsed)

    item = parsed.services[0]
    assert item.product_identity == identity
    assert item.calculator_service_name == display
    assert item.requirements[locked_field] == locked_value
    assert f"requirements.{locked_field}" in item.locked_fields


def test_customer_confirmed_product_variant_has_priority_over_original_text() -> None:
    parsed = ParsedIntent(
        customer_summary="corrected cache engine",
        services=[
            ServiceRequirement(
                service="elasticache",
                source_text="客户原来写 Redis",
                requirements={"engine": "valkey"},
                field_sources={"requirements.engine": "customer_confirmation"},
                locked_fields=["requirements.engine"],
            )
        ],
    )

    preserve_customer_configuration(parsed)

    cache = parsed.services[0]
    assert cache.product_identity == "elasticache_valkey"
    assert cache.calculator_service_name == "Amazon ElastiCache for Valkey"
    assert cache.requirements["engine"] == "valkey"
    assert cache.field_sources["requirements.engine"] == "customer_confirmation"


def test_opensearch_per_node_cpu_is_not_mistaken_for_node_count() -> None:
    source = "搜索这块现在是ES，想换OpenSearch，3个节点，每个节点4核16G，500G盘。"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="opensearch",
                source_text=source,
                requirements={
                    "data_nodes": 3,
                    "data_node_vcpu": 4,
                    "data_node_memory_gib": 16,
                    "data_node_storage_gib": 500,
                },
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_service_architecture(source, parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)
    requirements = parsed.services[0].requirements

    assert requirements["data_nodes"] == 3
    assert "nodes" not in requirements
    assert requirements["vcpu"] == 4
    assert requirements["memory_gib"] == 16


def test_eks_exact_worker_model_and_count_are_split_into_ec2() -> None:
    source = """Amazon EKS
区域：ap-southeast-1
Kubernetes 集群：1 套
Worker 节点型号：m7g.large
Worker 节点数量：5 台"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="eks",
                region="ap-southeast-1",
                quantity=1,
                source_text=source,
                requirements={"requested_model": "m7g.large", "node_count": 5},
            )
        ],
    )

    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    eks = next(item for item in parsed.services if item.service == "eks")
    worker = next(item for item in parsed.services if item.service == "ec2")
    assert eks.quantity == 1
    assert "requested_model" not in eks.requirements
    assert "node_count" not in eks.requirements
    assert worker.quantity == 5
    assert worker.requirements == {
        "requested_model": "m7g.large",
        "operating_system": "Linux",
    }


def test_eks_workers_per_cluster_are_multiplied_without_losing_shape() -> None:
    source = """部分业务使用Kubernetes。
先部署2套集群，
每套worker节点4台，
配置8核32G。"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="eks",
                region="ap-east-1",
                quantity=2,
                source_text=source,
                requirements={"cluster_count": 2},
            )
        ],
    )

    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    worker = next(item for item in parsed.services if item.service == "ec2")
    assert worker.quantity == 8
    assert worker.requirements["vcpu"] == 8
    assert worker.requirements["memory_gib"] == 32
    assert worker.source_text == source


def test_eks_worker_quantity_word_is_multiplied_by_cluster_count() -> None:
    source = (
        "Amazon EKS：区域：东京（ap-northeast-1），用途：微服务容器平台，"
        "集群数量：2个，每个集群Worker节点数量：3台"
    )
    parsed = ParsedIntent(
        customer_summary="EKS",
        services=[
            ServiceRequirement(
                service="eks",
                region="ap-northeast-1",
                quantity=2,
                source_text=source,
                requirements={"cluster_count": 2},
            )
        ],
    )

    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    worker = next(item for item in parsed.services if item.service == "ec2")
    assert worker.quantity == 6


def test_prometheus_identity_overrides_cloudwatch_mapping() -> None:
    parsed = ParsedIntent(
        customer_summary="Prometheus",
        services=[
            ServiceRequirement(
                service="cloudwatch",
                calculator_service_name="Amazon CloudWatch",
                source_text="Prometheus：用于 Kubernetes 指标监控",
            )
        ],
    )

    DeepSeekIntentParser._normalize_prometheus_managed_service(parsed)

    component = parsed.services[0]
    assert component.service == "amp"
    assert component.calculator_service_name == (
        "Amazon Managed Service for Prometheus (AMP)"
    )
    assert component.product_identity == "prometheus"


def test_numbered_shorthand_blocks_restore_opensearch_storage_and_unsized_eks_workers() -> None:
    """A partial first-pass result cannot discard later facts in its numbered block."""

    text = """7、搜索服务：目前使用ES做搜索和日志分析，预计3个节点，每个节点4核16G，存储500GB。

8、容器：部分应用准备放到Kubernetes里面，先部署1套EKS集群，worker节点3台。"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="opensearch",
                source_text="搜索服务：目前使用ES做搜索和日志分析，预计3个节点，每个节点4核16G。",
                requirements={"data_nodes": 3, "vcpu": 4, "memory_gib": 16},
            ),
            ServiceRequirement(
                service="eks",
                quantity=1,
                source_text="容器：先部署1套EKS集群。",
                requirements={"cluster_count": 1},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)
    DeepSeekIntentParser._isolate_shared_component_sources(parsed)
    DeepSeekIntentParser._reconcile_explicit_capacities(text, parsed)
    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    search = next(item for item in parsed.services if item.service == "opensearch")
    worker = next(
        item
        for item in parsed.services
        if item.service == "ec2"
        and item.calculator_service_name == "Amazon EC2 (EKS Worker Nodes)"
    )
    assert search.requirements["data_nodes"] == 3
    assert search.requirements["storage_gib_per_node"] == 500
    assert worker.quantity == 3
    assert worker.requirements == {"operating_system": "Linux"}
    assert "worker节点3台" in worker.source_text


def test_existing_narrow_worker_fragment_is_updated_not_duplicated() -> None:
    source = """部分业务使用Kubernetes。
先部署2套集群，
每套worker节点4台，
配置8核32G。"""
    worker_fragment = "每套worker节点4台，配置8核32G。"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="eks",
                region="ap-east-1",
                quantity=2,
                source_text=source,
                requirements={"cluster_count": 2},
            ),
            ServiceRequirement(
                service="ec2",
                region="ap-east-1",
                quantity=4,
                source_text=worker_fragment,
                requirements={"vcpu": 8, "memory_gib": 32},
            ),
        ],
    )

    DeepSeekIntentParser._split_eks_worker_nodes(parsed)

    workers = [item for item in parsed.services if item.service == "ec2"]
    assert len(workers) == 1
    assert workers[0].quantity == 8
    assert workers[0].requirements["memory_gib"] == 32
    assert workers[0].source_text == source


def test_shared_numbered_block_is_split_by_service_before_extraction() -> None:
    shared = """网络：
需要公网负载均衡，
另外需要CDN加速，
每月流量大概10TB。"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(service="elb", source_text=shared),
            ServiceRequirement(service="cloudfront", source_text=shared),
        ],
    )

    DeepSeekIntentParser._isolate_shared_component_sources(parsed)

    elb = next(item for item in parsed.services if item.service == "elb")
    cloudfront = next(item for item in parsed.services if item.service == "cloudfront")
    assert "10TB" not in elb.source_text
    assert "负载均衡" in elb.source_text
    assert "10TB" in cloudfront.source_text
    assert "CDN" in cloudfront.source_text


def test_one_line_shared_block_is_also_split_by_service() -> None:
    shared = "网络：需要公网负载均衡，另外需要CDN加速，每月流量大概10TB。"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(service="elb", source_text=shared),
            ServiceRequirement(service="cloudfront", source_text=shared),
        ],
    )

    DeepSeekIntentParser._isolate_shared_component_sources(parsed)

    elb = next(item for item in parsed.services if item.service == "elb")
    cloudfront = next(item for item in parsed.services if item.service == "cloudfront")
    assert "10TB" not in elb.source_text
    assert "10TB" in cloudfront.source_text


def test_inventory_keeps_related_services_without_duplicating_attached_alb() -> None:
    lines = [
        "AWS VPC + 子网 数量 1 套",
        "AWS ALB 数量 1 套",
        "AWS WAF 数量 1 套",
        "规格/说明：挂载 ALB",
        "AWS DMS 数量 1 套 dms.t3.large",
        "Secrets Manager / KMS 1套",
        "CloudWatch + X-Ray 1套",
    ]
    keys = [
        key
        for line in lines
        for key, _ in DeepSeekIntentParser._inventory_keys_for_line(line)
    ]

    assert keys.count("elb") == 1
    assert {"vpc", "waf", "dms", "secrets_manager", "kms", "cloudwatch", "xray"}.issubset(keys)


def test_redis_memory_times_total_nodes_is_not_interpreted_as_shards() -> None:
    source = "ElastiCache Redis 数量1集群 8GB × 3节点"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="elasticache",
                source_text=source,
                requirements={"memory_gib": 8, "shards": 3},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_service_architecture(source, parsed)

    assert parsed.services[0].requirements["memory_gib"] == 8
    assert parsed.services[0].requirements["shards"] == 1
    assert parsed.services[0].requirements["replicas_per_shard"] == 2


@pytest.mark.parametrize(
    ("source", "expected_replicas"),
    [
        ("Redis 架构：一主两从", 2),
        ("Redis 架构：一主二从", 2),
        ("Redis 架构：1主2从", 2),
        ("Redis 架构：一主三从", 3),
        ("Redis 主备模式", 1),
    ],
)
def test_redis_chinese_primary_replica_topology_is_exact(
    source: str, expected_replicas: int
) -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="elasticache",
                source_text=source,
                requirements={"shards": 1, "replicas_per_shard": 1},
            )
        ],
    )

    DeepSeekIntentParser._normalize_redis_topology(parsed)

    assert parsed.services[0].requirements["shards"] == 1
    assert parsed.services[0].requirements["replicas_per_shard"] == expected_replicas


def test_sales_numbering_is_a_hard_component_boundary() -> None:
    text = """区域：新加坡
1、Amazon EC2
数量：4台
配置：8核32G
2、Amazon RDS MySQL
数量：1
存储：500GB
3、Amazon MSK
Broker数量：3
每节点：4核16G
磁盘：500GB"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(service="ec2", source_text="Amazon EC2"),
            ServiceRequirement(service="rds", source_text="Amazon RDS MySQL"),
            ServiceRequirement(service="msk", source_text="Amazon MSK"),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)

    ec2, rds, msk = parsed.services
    assert "数量：4台" in ec2.source_text
    assert "Amazon RDS" not in ec2.source_text
    assert "存储：500GB" in rds.source_text
    assert "Broker数量" not in rds.source_text
    assert "每节点：4核16G" in msk.source_text


def test_cluster_nodes_do_not_multiply_cluster_quantity() -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="msk",
                quantity=3,
                source_text="Kafka预计3个节点",
                requirements={"broker_count": 3},
            ),
            ServiceRequirement(
                service="opensearch",
                quantity=3,
                source_text="ES集群预计3个节点",
                requirements={"data_nodes": 3},
            ),
        ],
    )

    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    assert [item.quantity for item in parsed.services] == [1, 1]


def test_msk_literal_node_count_overrides_generic_quantity_and_minimum_default() -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="msk",
                quantity=3,
                source_text=(
                    "消息队列：目前有Kafka需求，预计3个节点，"
                    "每个节点4核16G，磁盘500GB。"
                ),
                requirements={"broker_count": 2, "vcpu": 4, "memory_gib": 16},
            ),
        ],
    )

    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    assert parsed.services[0].quantity == 1
    assert parsed.services[0].requirements["broker_count"] == 3
    assert "requirements.broker_count" in parsed.services[0].locked_fields


def test_msk_deployment_quantity_label_with_broker_suffix_is_not_cluster_count() -> None:
    parsed = ParsedIntent(
        customer_summary="Kafka",
        services=[
            ServiceRequirement(
                service="msk",
                quantity=3,
                source_text=(
                    "Apache Kafka：区域：新加坡，用途：业务消息队列和实时数据流处理，"
                    "部署数量：3个Broker节点"
                ),
                requirements={"broker_count": 3},
            )
        ],
    )

    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    assert parsed.services[0].quantity == 1
    assert parsed.services[0].requirements["broker_count"] == 3


@pytest.mark.parametrize(
    ("service", "field", "source"),
    [
        ("mq", "broker_count", "RabbitMQ 部署数量：3个 Broker 节点"),
        ("opensearch", "data_nodes", "OpenSearch 部署数量：4个数据节点"),
        ("eks", "worker_node_count", "EKS 包含 6 个 Worker 节点"),
        ("documentdb", "instance_count", "DocumentDB 集群包含 3 个数据库实例"),
        ("redshift", "nodes", "Redshift 集群包含 2 个计算节点"),
        ("ecs", "tasks", "ECS 服务运行 8 个任务"),
    ],
)
def test_internal_topology_never_becomes_complete_deployment_quantity(
    service: str, field: str, source: str
) -> None:
    parsed = ParsedIntent(
        customer_summary=service,
        services=[
            ServiceRequirement(
                service=service,
                quantity=3,
                source_text=source,
                requirements={field: 3},
            )
        ],
    )

    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    assert parsed.services[0].quantity == 1


def test_explicit_independent_cluster_count_is_preserved() -> None:
    parsed = ParsedIntent(
        customer_summary="Kafka",
        services=[
            ServiceRequirement(
                service="msk",
                quantity=3,
                source_text="Kafka 集群数量：2，每个集群 3 个 Broker 节点",
                requirements={"broker_count": 3},
            )
        ],
    )

    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    assert parsed.services[0].quantity == 2
    assert parsed.services[0].requirements["broker_count"] == 3


def test_rabbitmq_nodes_shape_and_deployment_quantity_are_reconciled_together() -> None:
    source = "目前使用RabbitMQ，预计3个节点，每个节点4核16G。"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="mq",
                calculator_service_name="Amazon MQ",
                quantity=3,
                source_text=source,
                requirements={"broker_count": 4, "requested_model": "mq.t3.micro"},
            )
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)
    DeepSeekIntentParser._normalize_cluster_group_quantities(parsed)

    item = parsed.services[0]
    assert item.quantity == 1
    assert item.requirements["broker_count"] == 3
    assert item.requirements["vcpu"] == 4
    assert item.requirements["memory_gib"] == 16
    assert "requirements.broker_count" in item.locked_fields


def test_numeric_field_evidence_must_support_the_filled_value() -> None:
    component = ServiceRequirement(
        service="mq",
        source_text="RabbitMQ预计3个节点，每个节点4核16G。",
        requirements={"broker_count": 4},
        field_evidence={"requirements.broker_count": "3个节点"},
    )

    with pytest.raises(ValueError, match="与原文证据中的数值"):
        DeepSeekIntentParser._validate_component_evidence(
            component,
            provided_payload={"requirements": {"broker_count": 4}},
            source_text=component.source_text,
            original=ServiceRequirement(service="mq", source_text=component.source_text),
        )


def test_unsupported_managed_mq_topology_becomes_customer_question_before_quote() -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="mq",
                calculator_service_name="Amazon MQ",
                source_text="RabbitMQ需要2个节点",
                requirements={"engine_type": "rabbitmq", "broker_count": 2},
            )
        ],
    )

    DeepSeekIntentParser._append_vague_value_questions(parsed)

    assert len(parsed.ambiguities) == 1
    assert "1个还是3个" in parsed.ambiguities[0]


def test_cloudfront_summary_and_usage_fragments_merge_and_false_vpc_drops() -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(service="vpc", source_text="网络："),
            ServiceRequirement(service="cloudfront", source_text="需要CDN加速"),
            ServiceRequirement(
                service="cloudfront",
                source_text="CDN流量预计每月5TB",
                requirements={"data_transfer_out_gib": 5120},
            ),
        ],
    )

    DeepSeekIntentParser._drop_unrequested_section_services(
        "网络：\n需要CDN加速\nCDN流量预计每月5TB", parsed
    )
    DeepSeekIntentParser._merge_duplicate_service_fragments(parsed)

    assert [item.service for item in parsed.services] == ["cloudfront"]
    assert parsed.services[0].requirements["data_transfer_out_gib"] == 5120


def test_s3_capacity_is_recovered_from_natural_numbered_wording() -> None:
    source = "文件存储预计30TB左右，主要存图片和业务文件。"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[ServiceRequirement(service="s3", source_text=source)],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(source, parsed)

    assert parsed.services[0].requirements["storage_gib"] == 30 * 1024


def test_capacity_recovery_uses_canonical_service_identity_for_all_aliases() -> None:
    """Display-name spellings must not bypass the component's own source guard."""

    cases = [
        (
            ServiceRequirement(
                service="amazon_msk",
                source_text="Kafka：预计3个节点，每个节点8核32G，磁盘2TB。",
                requirements={"broker_count": 3, "memory_gib": 16},
            ),
            {"broker_count": 3, "vcpu": 8, "memory_gib": 32,
             "storage_gib_per_broker": 2048},
        ),
        (
            ServiceRequirement(
                service="amazon_opensearch_service",
                source_text="ES：预计5个节点，每节点8核32G，磁盘1TB。",
                requirements={"data_nodes": 3, "storage_gib_per_node": 2048},
            ),
            {"data_nodes": 5, "vcpu": 8, "memory_gib": 32,
             "storage_gib_per_node": 1024},
        ),
        (
            ServiceRequirement(
                service="amazon_ec2",
                source_text="每套worker节点4台，配置8核32G。",
                requirements={"vcpu": 8, "memory_gib": 16},
            ),
            {"vcpu": 8, "memory_gib": 32},
        ),
    ]

    parsed = ParsedIntent(customer_summary="x", services=[item for item, _ in cases])
    DeepSeekIntentParser._reconcile_explicit_capacities("", parsed)

    for item, (_, expected) in zip(parsed.services, cases, strict=True):
        for field, value in expected.items():
            assert item.requirements[field] == value


def test_cleaned_component_source_never_falls_back_to_full_quote() -> None:
    """Line folding must not let one component read a neighbour's numbers."""

    full_text = """业务服务器：8核16G
Kafka：3个节点
每个节点8核32G
磁盘2TB
ES：5个节点
每节点8核32G
磁盘1TB
Kubernetes：每套worker节点4台
配置8核32G"""
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="msk",
                source_text="Kafka：3个节点，每个节点8核32G，磁盘2TB",
                requirements={"memory_gib": 16},
            ),
            ServiceRequirement(
                service="opensearch",
                source_text="ES：5个节点，每节点8核32G，磁盘1TB",
                requirements={"data_nodes": 3, "storage_gib_per_node": 2048},
            ),
            ServiceRequirement(
                service="ec2",
                source_text="Kubernetes：每套worker节点4台，配置8核32G",
                requirements={"memory_gib": 16},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_capacities(full_text, parsed)

    msk, search, worker = parsed.services
    assert msk.requirements["memory_gib"] == 32
    assert msk.requirements["storage_gib_per_broker"] == 2048
    assert search.requirements["data_nodes"] == 5
    assert search.requirements["storage_gib_per_node"] == 1024
    assert worker.requirements["memory_gib"] == 32


def test_duplicate_merge_cannot_overwrite_customer_locked_component_fields() -> None:
    source = "ES：预计5个节点，每节点8核32G，磁盘1TB。"
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="opensearch",
                source_text=source,
                requirements={
                    "data_nodes": 5,
                    "vcpu": 8,
                    "memory_gib": 32,
                    "storage_gib_per_node": 1024,
                },
                field_evidence={
                    "requirements.data_nodes": "5个节点",
                    "requirements.memory_gib": "32G",
                },
                field_sources={
                    "requirements.data_nodes": "customer_text",
                    "requirements.memory_gib": "customer_text",
                },
                locked_fields=[
                    "requirements.data_nodes",
                    "requirements.memory_gib",
                ],
            ),
            ServiceRequirement(
                    service="amazon_opensearch_service",
                source_text=source,
                requirements={
                    "data_nodes": 3,
                    "memory_gib": 16,
                    "storage_gib_per_node": 2048,
                },
            ),
        ],
    )

    DeepSeekIntentParser._merge_duplicate_service_fragments(parsed)

    assert len(parsed.services) == 1
    requirements = parsed.services[0].requirements
    assert requirements["data_nodes"] == 5
    assert requirements["memory_gib"] == 32


def test_rds_primary_standby_is_one_deployment() -> None:
    parsed = ParsedIntent(
        customer_summary="x",
        services=[
            ServiceRequirement(
                service="rds",
                quantity=2,
                source_text="数据库现在用MySQL，500G，需要主备高可用。",
                requirements={"engine": "mysql", "deployment": "multi_az"},
            ),
            ServiceRequirement(
                service="rds",
                quantity=1,
                source_text="PostgreSQL 数据库，存储300GB。",
                requirements={"engine": "postgresql", "storage_gib": 300},
            ),
        ],
    )

    DeepSeekIntentParser._normalize_database_group_quantity(parsed)
    assert parsed.services[0].quantity == 1
    assert "deployment" not in parsed.services[1].requirements


@pytest.mark.asyncio
async def test_component_template_error_is_sent_back_to_ai_for_self_repair() -> None:
    class SelfRepairGateway:
        def __init__(self) -> None:
            self.calls = 0
            self.inputs: list[str] = []

        async def complete_json(self, **kwargs: object) -> dict[str, object]:
            self.calls += 1
            self.inputs.append(str(kwargs.get("user_content") or ""))
            if self.calls == 1:
                return {"component": "not-an-object"}
            if self.calls == 2:
                return {
                    "component": {
                        "service": "s3",
                        "calculator_service_name": "Amazon S3",
                        "quantity": 1,
                        "hours_per_month": 730,
                            "requirements": {"storage_gib": 1024},
                            "field_evidence": {
                                "requirements.storage_gib": "S3 1TB",
                            },
                        "source_text": "S3 1TB",
                        "query_action": None,
                    }
                }
            return {"corrections": {}, "customer_questions": []}

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = SelfRepairGateway()
    parser._gateway = gateway  # type: ignore[assignment]
    intent = ParsedIntent(
        customer_summary="S3",
        services=[
            ServiceRequirement(
                service="s3",
                calculator_service_name="Amazon S3",
                source_text="S3 1TB",
            )
        ],
    )

    repaired = await parser._cleanup_components("S3 1TB", intent)

    assert gateway.calls == 2
    assert "程序校验错误" in gateway.inputs[1]
    assert repaired.services[0].requirements["storage_gib"] == 1024


def test_numbered_block_rebinds_unknown_model_alias_and_merges_fragments() -> None:
    text = """5、公网出站流量：
新加坡区域，每月公网出网流量1000GB。"""
    parsed = ParsedIntent(
        customer_summary="公网流量",
        services=[
            ServiceRequirement(
                service="data_transfer",
                calculator_service_name="AWS Data Transfer",
                source_text="5、公网出站流量：",
            ),
            ServiceRequirement(
                # Deliberately use an unseen generated alias.  The parser must
                # not need an alias-table entry to recover the component.
                service="monthly_public_egress",
                calculator_service_name="Data Transfer",
                source_text="新加坡区域，每月公网出网流量1000GB。",
                requirements={"data_transfer_out_gib": 1000},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)

    assert len(parsed.services) == 1
    component = parsed.services[0]
    assert component.service == "data_transfer"
    assert component.calculator_service_name == "AWS Data Transfer"
    assert component.source_text == "公网出站流量：\n新加坡区域，每月公网出网流量1000GB。"
    assert component.requirements["data_transfer_out_gib"] == 1000


def test_same_service_numbered_blocks_remain_independent_by_source() -> None:
    text = """1、AWS Data Transfer：新加坡每月公网出网1000GB。
2、AWS Data Transfer：悉尼每月公网出网500GB。"""
    parsed = ParsedIntent(
        customer_summary="两地公网流量",
        services=[
            ServiceRequirement(
                service="egress_sydney_generated_name",
                source_text="悉尼每月公网出网500GB。",
                requirements={"data_transfer_out_gib": 500},
            ),
            ServiceRequirement(
                service="egress_singapore_generated_name",
                source_text="新加坡每月公网出网1000GB。",
                requirements={"data_transfer_out_gib": 1000},
            ),
        ],
    )

    DeepSeekIntentParser._reconcile_explicit_component_inventory(text, parsed)

    assert len(parsed.services) == 2
    assert [item.requirements["data_transfer_out_gib"] for item in parsed.services] == [
        1000,
        500,
    ]
    assert all(item.service == "data_transfer" for item in parsed.services)


def test_space_numbered_components_do_not_split_numbered_service_fields() -> None:
    text = """1 Amazon EC2
区域：新加坡
数量：1
2 Amazon MSK
区域：新加坡
3 Broker节点
Kafka集群
3 Amazon CloudFront
每月流量5TB"""

    blocks = DeepSeekIntentParser._numbered_requirement_blocks(text)

    assert len(blocks) == 3
    assert blocks[1] == "Amazon MSK\n区域：新加坡\n3 Broker节点\nKafka集群"
    assert DeepSeekIntentParser._numbered_requirement_match("3 Broker节点") is None
    assert DeepSeekIntentParser._numbered_requirement_match("4核16G") is None


def test_unknown_official_component_keeps_its_own_numbered_block() -> None:
    text = """1 Amazon EC2
数量1
2 Amazon Managed Grafana
用于数据可视化"""

    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)

    assert parsed is not None
    assert len(parsed.services) == 2
    assert parsed.services[0].source_text == "Amazon EC2\n数量1"
    assert parsed.services[1].service == "amazon_managed_grafana"
    assert parsed.services[1].calculator_service_name == "Amazon Managed Grafana"
    assert parsed.services[1].source_text == "Amazon Managed Grafana\n用于数据可视化"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("1、Amazon EC2", "Amazon EC2"),
        ("1，Amazon EC2", "Amazon EC2"),
        ("1, Amazon EC2", "Amazon EC2"),
        ("1。Amazon EC2", "Amazon EC2"),
        ("1；Amazon EC2", "Amazon EC2"),
        ("1：Amazon EC2", "Amazon EC2"),
        ("1 Amazon EC2", "Amazon EC2"),
        ("（1）Amazon EC2", "Amazon EC2"),
        ("(1) Amazon EC2", "Amazon EC2"),
    ],
)
def test_sales_number_prefix_accepts_common_punctuation(
    line: str, expected: str
) -> None:
    match = DeepSeekIntentParser._numbered_requirement_match(line)

    assert match is not None
    assert match.group(2).strip() == expected


def test_numbered_inventory_can_skip_workload_wide_classification() -> None:
    text = """区域：新加坡
1，应用服务器：4台，8核16G，Linux
2 数据库：MySQL，500GB，主备高可用
（3）缓存：Redis，16GB，主从
4、对象存储：20TB"""

    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)

    assert parsed is not None
    assert [item.service for item in parsed.services] == [
        "ec2",
        "rds",
        "elasticache",
        "s3",
    ]
    assert parsed.services[1].source_text.startswith("数据库：MySQL")


def test_managed_server_wording_does_not_create_an_extra_ec2_component() -> None:
    text = """1、Redis服务器：16GB，主从
2、数据库服务器：MySQL，500GB，主备高可用"""

    parsed = DeepSeekIntentParser._intent_from_numbered_blocks(text)

    assert parsed is not None
    assert [item.service for item in parsed.services] == ["elasticache", "rds"]


def test_non_sequential_punctuated_field_does_not_create_component() -> None:
    text = """1、Amazon MSK
3，Broker节点
每节点4核16G
2、Amazon S3
存储20TB"""

    blocks = DeepSeekIntentParser._numbered_requirement_blocks(text)

    assert blocks == [
        "Amazon MSK\n3，Broker节点\n每节点4核16G",
        "Amazon S3\n存储20TB",
    ]


def test_opensearch_total_storage_shorthand_overrides_stale_per_node_value() -> None:
    parsed = ParsedIntent(
        customer_summary="OpenSearch 修改",
        services=[
            ServiceRequirement(
                service="opensearch",
                calculator_service_name="Amazon OpenSearch Service",
                source_text=(
                    "Amazon OpenSearch Service\n3节点\n每节点4核16GB\n"
                    "总存储1TB\n客户最新修改：总容量为1TB"
                ),
                requirements={
                    "data_nodes": 3,
                    # Simulate the stale, incorrect value returned before the
                    # customer correction is reconciled.
                    "storage_gib_per_node": 1024,
                },
            )
        ],
    )

    DeepSeekIntentParser._reconcile_repeated_unit_storage(parsed)

    requirements = parsed.services[0].requirements
    assert requirements["data_nodes"] == 3
    assert requirements["total_storage_gib"] == 1024
    assert requirements["storage_gib_per_node"] == pytest.approx(1024 / 3)
    assert not parsed.ambiguities


@pytest.mark.asyncio
async def test_unknown_generated_name_is_classified_before_template_extraction() -> None:
    class UnknownNameGateway:
        def __init__(self) -> None:
            self.calls = 0

        async def complete_json(self, **_: object) -> dict[str, object]:
            self.calls += 1
            if self.calls == 1:
                return {"service": "s3", "confidence": "high"}
            return {
                "component": {
                    "service": "s3",
                    "calculator_service_name": "Amazon S3",
                    "region": None,
                    "quantity": 1,
                    "requirements": {"storage_gib": 51200},
                    "field_evidence": {
                        "requirements.storage_gib": "对象文件预计50TB"
                    },
                    "source_text": "对象文件预计50TB",
                    "query_action": None,
                }
            }

    parser = DeepSeekIntentParser(
        Settings(ai_api_key="test", ai_base_url="https://example.invalid")
    )
    gateway = UnknownNameGateway()
    parser._gateway = gateway  # type: ignore[assignment]
    intent = ParsedIntent(
        customer_summary="对象存储",
        services=[
            ServiceRequirement(
                service="object_file_service_generated_name",
                calculator_service_name="对象文件服务",
                source_text="对象文件预计50TB",
            )
        ],
    )

    cleaned = await parser._cleanup_components("对象文件预计50TB", intent)

    assert gateway.calls == 2
    assert cleaned.services[0].service == "s3"
    assert cleaned.services[0].calculator_service_name == "Amazon Simple Storage Service (S3)"
    assert cleaned.services[0].requirements["storage_gib"] == 51200
