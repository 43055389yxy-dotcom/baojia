import pytest

from app.core.errors import ManualConfirmationRequired
from app.domain.models import (
    CandidateOption,
    ConfirmationOption,
    ParsedIntent,
    PreviewSelection,
    PricedLine,
    PricingScenario,
    QuoteRequest,
    ReferenceRate,
    SelectedResource,
    ServiceKind,
    ServiceRequirement,
    UsageLine,
)
from app.domain.pricing_issues import (
    classify_persisted_pricing_issue,
    legacy_pricing_issue_message,
    should_retry_persisted_pricing_issue,
)
from app.integrations.aws import PricingCatalog
from app.integrations.calculator_web import (
    CalculatorGenericGroupResult,
    CalculatorWebResult,
    GenericCalculatorInput,
)
from app.integrations.deepseek import DeepSeekIntentParser
from app.services.bcm_estimator import BcmQuoteResult
from app.services.confirmation_sessions import (
    CONFIGURATION_COMPONENT_FEEDBACK_PREFIX,
    CONFIGURATION_COMPONENT_UPDATE_PREFIX,
    CONFIGURATION_FEEDBACK_QUESTION,
    ConfirmationSessionStore,
)
from app.services.plugins.base import PluginRegistry
from app.services.plugins.common import (
    CloudFrontPlugin,
    S3Plugin,
    _normalize_s3_storage_class,
)
from app.services.plugins.minimum_services import WafPlugin
from app.services.quote_service import QuoteService


def test_non_pricing_context_is_removed_without_touching_customer_original() -> None:
    redis = ServiceRequirement(
        service="elasticache",
        source_text="Redis 7.x，1主1从，12GB",
        requirements={"engine": "redis", "engine_version": "7.x", "memory_gib": 12},
        field_sources={
            "requirements.engine_version": "customer_text",
            "requirements.memory_gib": "customer_text",
        },
        field_evidence={
            "requirements.engine_version": "Redis 7.x",
            "requirements.memory_gib": "12GB",
        },
        locked_fields=["requirements.engine_version", "requirements.memory_gib"],
        field_match_policies={"engine_version": "exact", "memory_gib": "approximate"},
        field_scopes={"engine_version": "component_total", "memory_gib": "per_node"},
    )
    database = ServiceRequirement(
        service="rds",
        source_text="RDS MySQL 8.0",
        requirements={"engine": "mysql", "engine_version": "8.0"},
    )
    intent = ParsedIntent(customer_summary="test", services=[redis, database])

    QuoteService._strip_non_pricing_context(intent)

    assert redis.source_text == "Redis 7.x，1主1从，12GB"
    assert redis.requirements == {"engine": "redis", "memory_gib": 12}
    assert redis.field_sources == {"requirements.memory_gib": "customer_text"}
    assert redis.field_evidence == {"requirements.memory_gib": "12GB"}
    assert redis.locked_fields == ["requirements.memory_gib"]
    assert redis.field_match_policies == {"memory_gib": "approximate"}
    assert redis.field_scopes == {"memory_gib": "per_node"}
    assert database.requirements["engine_version"] == "8.0"


def test_redis_supported_versions_are_rendered_as_dropdown_options() -> None:
    question = (
        "Redis：您指定的 Redis 8.x 在 ap-east-1 不可用，请改用当前区域支持的版本。"
        "可选版本：7.1、7.0。"
    )

    options = QuoteService._default_confirmation_options(question)

    assert [option.value for option in options] == [
        "cache_engine_version:7.1",
        "cache_engine_version:7.0",
    ]


def test_component_cost_binding_never_confuses_component_1_with_10_or_11() -> None:
    selections = [
        SelectedResource(
            component_id=component_id,
            service="ec2",
            display_name=f"component {component_id}",
            region="ap-southeast-1",
            model="model",
            architecture="test",
            specifications={},
            official_product={},
            rationale="test",
        )
        for component_id in ("0", "9", "10")
    ]
    lines = [
        PricedLine(
            key=key,
            service_code="AmazonEC2",
            usage_type="BoxUsage",
            operation="RunInstances",
            amount=1,
            cost=cost,
        )
        for key, cost in (("s1l1", 1), ("s10l1", 10), ("s11commit", 11))
    ]
    assert QuoteService._component_costs(selections, lines) == {
        "0": 1,
        "9": 10,
        "10": 11,
    }


def test_global_catalog_sizing_invariant_preserves_exact_customer_shape() -> None:
    requirement = ServiceRequirement(
        service="future_database_plugin",
        requirements={"vcpu": 8, "memory_gib": 32},
        field_sources={
            "requirements.vcpu": "customer_text",
            "requirements.memory_gib": "customer_text",
        },
        field_evidence={
            "requirements.vcpu": "单节点8核32GB",
            "requirements.memory_gib": "单节点8核32GB",
        },
        locked_fields=["requirements.vcpu", "requirements.memory_gib"],
    )
    selection = PreviewSelection(
        component_id="0",
        service="future_database_plugin",
        display_name="Future Database",
        region="ap-southeast-1",
        selected_model="db.lower",
        candidates=[
            CandidateOption(
                model="db.lower",
                family="db",
                specifications={"vCPU": 4, "memoryGiB": 32},
                monthly_catalog_cost=100,
                rationale="lower",
            ),
            CandidateOption(
                model="db.valid",
                family="db",
                specifications={"vCPU": 8, "memoryGiB": 64},
                monthly_catalog_cost=150,
                rationale="valid",
            ),
            CandidateOption(
                model="db.valid-expensive",
                family="db",
                specifications={"vCPU": 8, "memoryGiB": 32},
                monthly_catalog_cost=200,
                rationale="valid but expensive",
            ),
        ],
        requires_confirmation=True,
        confirmation_reason="请选择型号",
    )

    resolved = QuoteService._enforce_catalog_sizing_invariant(requirement, selection)

    assert resolved.selected_model == "db.valid-expensive"
    assert resolved.requires_confirmation is False
    assert resolved.confirmation_reason is None
    assert next(
        candidate for candidate in resolved.candidates if candidate.is_default
    ).model == "db.valid-expensive"


def test_global_catalog_sizing_invariant_asks_before_non_exact_substitution() -> None:
    requirement = ServiceRequirement(
        service="future_database_plugin",
        requirements={"vcpu": 8, "memory_gib": 32},
        field_sources={
            "requirements.vcpu": "customer_text",
            "requirements.memory_gib": "customer_text",
        },
        field_evidence={
            "requirements.vcpu": "单节点8核32GB",
            "requirements.memory_gib": "单节点8核32GB",
        },
        locked_fields=["requirements.vcpu", "requirements.memory_gib"],
    )
    selection = PreviewSelection(
        component_id="0",
        service="future_database_plugin",
        display_name="Future Database",
        region="ap-southeast-1",
        selected_model="db.larger",
        candidates=[
            CandidateOption(
                model="db.larger",
                family="db",
                specifications={"vCPU": 8, "memoryGiB": 64},
                monthly_catalog_cost=150,
                rationale="larger",
            )
        ],
    )

    resolved = QuoteService._enforce_catalog_sizing_invariant(requirement, selection)

    assert resolved.selected_model is None
    assert resolved.requires_confirmation is True
    assert resolved.issue_code == "exact_customer_shape_not_available"
    assert "不会自动放大、缩小或替换" in str(resolved.confirmation_reason)


def test_global_catalog_sizing_invariant_honors_approximate_wording_from_source() -> None:
    requirement = ServiceRequirement(
        service="future_cache_plugin",
        source_text="单节点内存约13GB",
        original_source_text="单节点内存约13GB",
        requirements={"memory_gib": 13},
        field_sources={"requirements.memory_gib": "customer_text"},
        field_evidence={"requirements.memory_gib": "13GB"},
        locked_fields=["requirements.memory_gib"],
        field_match_policies={"memory_gib": "exact"},
    )
    selection = PreviewSelection(
        component_id="0",
        service="future_cache_plugin",
        display_name="Future Cache",
        region="ap-east-1",
        selected_model="db.r6g.large",
        candidates=[
            CandidateOption(
                model="db.r6g.large",
                family="cache",
                specifications={"vCPU": 2, "memoryGiB": 13.07},
                monthly_catalog_cost=100,
                rationale="official",
            )
        ],
    )

    resolved = QuoteService._enforce_catalog_sizing_invariant(requirement, selection)

    assert resolved.selected_model == "db.r6g.large"
    assert resolved.requires_confirmation is False


def test_global_catalog_sizing_invariant_does_not_invent_missing_managed_shape() -> None:
    requirement = ServiceRequirement(
        service="work_spaces",
        requirements={"vcpu": 2, "memory_gib": 8},
        field_sources={
            "requirements.vcpu": "customer_text",
            "requirements.memory_gib": "customer_text",
        },
        locked_fields=["requirements.vcpu", "requirements.memory_gib"],
    )
    selection = PreviewSelection(
        component_id="0",
        service="work_spaces",
        display_name="Amazon WorkSpaces",
        region="ap-east-1",
        selected_model="AWS 官方计费维度",
        candidates=[
            CandidateOption(
                model="AWS 官方计费维度",
                family="work_spaces",
                specifications={"vcpu": 2, "memory_gib": 8},
                rationale="官方托管服务计费维度未公开 EC2 型号字段。",
                official_product={"source": "AWS Price List"},
            )
        ],
    )

    resolved = QuoteService._enforce_catalog_sizing_invariant(requirement, selection)

    assert resolved.selected_model == "AWS 官方计费维度"
    assert resolved.requires_confirmation is False
    assert resolved.issue_code is None

def test_final_scenario_requires_one_cost_key_per_independent_component() -> None:
    selections = [
        SelectedResource(
            component_id=component_id,
            service="ec2",
            display_name=f"component {component_id}",
            region="ap-southeast-1",
            model="model",
            architecture="test",
            specifications={},
            official_product={},
            rationale="test",
        )
        for component_id in ("0", "1")
    ]
    complete = PricingScenario(
        label="按需",
        pricing_mode="on_demand",
        quote_id="quote-complete",
        total_cost=10,
        component_costs={"0": 10, "1": 0},
    )
    incomplete = complete.model_copy(
        update={"quote_id": "quote-incomplete", "component_costs": {"0": 10}}
    )

    QuoteService._validate_component_scenarios(selections, [complete])
    with pytest.raises(RuntimeError, match="incomplete component ledger"):
        QuoteService._validate_component_scenarios(selections, [incomplete])


def test_final_specifications_hide_internal_derived_and_false_fields() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        quantity=2,
        requirements={
            "vcpu": 4,
            "memory_gib": 16,
            "system_disk_gib": 200,
            "total_system_disk_gib": 400,
            "volume_type": "gp3",
            "purchase_option": "on_demand",
            "utilization_percent": 100,
            "detailed_monitoring": False,
            "requested_model": "m6i.xlarge",
        },
    )

    specifications = QuoteService._complete_selection_specifications(
        requirement,
        {
            **requirement.requirements,
            "requested_model": "m6i.xlarge",
            "_internal_trace": "hidden",
        },
    )

    assert specifications == {
        "vCPU": 4,
        "memoryGiB": 16,
        "systemDiskGiB": 200,
        "volumeType": "gp3",
    }


def test_catalog_failure_categories_do_not_call_every_failure_a_timeout() -> None:
    rds = ServiceRequirement(
        service="rds", requirements={"engine": "mysql", "engine_version": "5.7.44"}
    )
    generic = ServiceRequirement(service="quicksight")

    assert (
        QuoteService._catalog_issue_category(
            ManualConfirmationRequired("not found", code="rds_discovery_failed"), rds
        )
        == "compatibility"
    )
    assert (
        QuoteService._catalog_issue_category(
            ManualConfirmationRequired("timeout", code="lookup_timeout"), generic
        )
        == "retryable"
    )
    assert (
        QuoteService._catalog_issue_category(
            ManualConfirmationRequired("missing", code="generic_semantic_rate_not_found"), generic
        )
        == "catalog_mapping"
    )


def test_sales_region_fills_only_unresolved_regional_components() -> None:
    intent = ParsedIntent(
        customer_summary="区域前置确认",
        services=[
            ServiceRequirement(service="ec2", region=None),
            ServiceRequirement(service="rds", region="ap-northeast-1"),
            ServiceRequirement(service="cloudfront", region="global"),
        ],
        ambiguities=["请确认这些区域型服务部署在哪个 AWS 区域。", "请确认数据库引擎。"],
    )

    QuoteService._apply_sales_region(intent, "ap-southeast-1")

    assert intent.services[0].region == "ap-southeast-1"
    assert intent.services[0].field_sources["region"] == "sales_confirmation"
    assert intent.services[1].region == "ap-northeast-1"
    assert intent.services[2].region == "global"
    assert intent.ambiguities == ["请确认数据库引擎。"]


def test_legacy_pricing_failures_are_classified_and_retried_without_fake_timeouts() -> None:
    assert (
        classify_persisted_pricing_issue(
            reason="官方目录暂时无响应，确认后系统会自动重试，无需修改配置。",
            service="cloudwatch",
        )
        == "retryable"
    )
    assert (
        classify_persisted_pricing_issue(
            reason="AWS 官方目录没有返回可安全展示的新组件计费项",
            service="quicksight",
        )
        == "catalog_mapping"
    )
    assert (
        classify_persisted_pricing_issue(
            reason="MySQL 5.7.44 在当前区域不再提供维护或订购",
            service="rds",
            requirements={"engine_version": "5.7.44"},
        )
        == "compatibility"
    )
    assert (
        should_retry_persisted_pricing_issue(
            reason="AWS 官方目录没有返回可安全展示的新组件计费项",
            service="quicksight",
        )
        is True
    )
    assert (
        should_retry_persisted_pricing_issue(
            reason="该服务尚未接入官方报价适配器",
            service="unknown",
        )
        is False
    )
    assert (
        should_retry_persisted_pricing_issue(
            reason="AWS Step Functions 尚未建立安全的官方报价映射",
            category="unsupported",
            code="service_region_not_supported",
            service="step_functions",
        )
        is True
    )
    assert (
        classify_persisted_pricing_issue(
            reason="AWS 官方规格接口暂时未返回结果，请稍后重试",
            service="rds",
            requirements={"engine_version": "5.7.44"},
        )
        == "compatibility"
    )
    assert (
        classify_persisted_pricing_issue(
            reason="AWS 官方规格接口暂时未返回结果，请稍后重试",
            service="quicksight",
        )
        == "catalog_mapping"
    )
    assert "无额外服务费" in legacy_pricing_issue_message(
        reason="AWS 官方规格接口暂时未返回结果，请稍后重试",
        category="catalog_mapping",
        service="codedeploy",
        display_name="AWS CodeDeploy",
    )


def test_region_unsupported_message_preserves_official_managed_identity() -> None:
    component = ServiceRequirement(
        service="app_stream",
        calculator_service_name="Amazon AppStream 2.0",
        region="ap-east-1",
    )
    error = ManualConfirmationRequired(
        "区域没有目录",
        code="service_region_not_supported",
        region="ap-east-1",
    )

    message = QuoteService._catalog_issue_message(
        error,
        component,
        "Amazon AppStream 2.0",
        "unsupported",
    )

    assert "AWS 官方托管服务" in message
    assert "ap-east-1" in message
    assert "不会改成 EC2 自建" in message


def test_region_and_replacement_candidates_keep_structured_decision_values() -> None:
    region_options = QuoteService._compact_candidate_options(
        [
            CandidateOption(
                model="ap-southeast-1",
                family="aws_region",
                specifications={"region": "ap-southeast-1", "label": "新加坡"},
                rationale="official",
            )
        ],
        ServiceRequirement(service="app_stream"),
    )
    replacement_options = QuoteService._compact_candidate_options(
        [
            CandidateOption(
                model="改用 Amazon Aurora PostgreSQL",
                family="service_replacement",
                specifications={
                    "decision": "replace_service:rds:aurora_postgresql"
                },
                rationale="official",
            )
        ],
        ServiceRequirement(service="qldb"),
    )

    assert [(option.label, option.value) for option in region_options] == [
        (
            "亚太地区（新加坡） / Asia Pacific (Singapore) · ap-southeast-1",
            "ap-southeast-1",
        )
    ]
    assert replacement_options[0].value == "replace_service:rds:aurora_postgresql"


@pytest.mark.asyncio
async def test_service_region_confirmation_updates_only_bound_component() -> None:
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
    )
    intent = ParsedIntent(
        customer_summary="两个区域组件",
        services=[
            ServiceRequirement(service="app_stream", region="ap-southeast-3"),
            ServiceRequirement(service="keyspaces", region="ap-southeast-3"),
        ],
    )
    question = "Keyspaces 在当前区域不提供，请选择其他区域"

    await service._apply_confirmation_responses(
        intent,
        {question: "ap-southeast-1"},
        response_components={question: 1},
    )

    assert intent.services[0].region == "ap-southeast-3"
    assert intent.services[1].region == "ap-southeast-1"
    assert intent.services[1].field_sources["region"] == "customer_confirmation"
    assert "region" in intent.services[1].locked_fields


@pytest.mark.asyncio
async def test_plain_language_diqu_region_question_is_applied_once() -> None:
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
    )
    question = (
        "Amazon Timestream for LiveAnalytics 在 eu-west-2 不能使用。"
        "您想改到哪个地区？"
    )
    component = ServiceRequirement(
        service="amazon_timestream_for_liveanalytics",
        region="eu-west-2",
        source_text=(
            "Amazon Timestream for LiveAnalytics：区域eu-west-2（伦敦），数量1。"
        ),
    )
    intent = ParsedIntent(customer_summary="Timestream", services=[component])

    await service._apply_confirmation_responses(
        intent,
        {question: "ap-south-1"},
        response_components={question: 0},
    )
    DeepSeekIntentParser._reconcile_explicit_regions(component.source_text, intent)

    assert component.region == "ap-south-1"
    assert component.field_sources["region"] == "customer_confirmation"
    assert "region" in component.locked_fields


def test_generic_official_spec_conflict_is_customer_choice_not_technical_error() -> None:
    component = ServiceRequirement(
        service="neptune",
        calculator_service_name="Amazon Neptune",
        region="ap-east-1",
        requirements={"requested_model": "db.r6g.large", "vcpu": 8, "memory_gib": 32},
    )
    error = ManualConfirmationRequired(
        "规格冲突",
        code="generic_official_specification_not_found",
        requested_model="db.r6g.large",
        requested_vcpu=8,
        requested_memory_gib=32,
    )

    assert QuoteService._is_technical_catalog_error(error) is False
    question = QuoteService._plugin_confirmation_question(
        "Amazon Neptune", component, error
    )
    assert "db.r6g.large" in question
    assert "8 核" in question
    assert "32 GB" in question
    assert "请从下面选择" in question
    assert "官方规格" not in question


def test_quicksight_without_usage_gets_smallest_subscription_default() -> None:
    intent = ParsedIntent(
        customer_summary="BI 可视化",
        services=[
            ServiceRequirement(
                service="quicksight",
                calculator_service_name="Amazon QuickSight",
                quantity=1,
            )
        ],
    )

    notices = QuoteService._apply_calculator_minimum_defaults(intent)

    assert intent.services[0].requirements["edition"] == "enterprise"
    assert intent.services[0].requirements["users"] == 1
    assert any("1 位用户" in notice for notice in notices)


class MixedParser:
    async def parse(self, _: str) -> ParsedIntent:
        return ParsedIntent(
            customer_summary="EC2、RDS 和缓存合并报价",
            services=[
                ServiceRequirement(
                    service="ec2",
                    calculator_service_name="Amazon EC2",
                    region="ap-northeast-1",
                    quantity=2,
                    requirements={"vcpu": 4, "memory_gib": 16},
                ),
                ServiceRequirement(
                    service="rds",
                    calculator_service_name="Amazon RDS for PostgreSQL",
                    region="ap-southeast-1",
                    requirements={"deployment": "multi_az", "storage_gib": 500},
                ),
                ServiceRequirement(
                    service="elasticache",
                    calculator_service_name="Amazon ElastiCache",
                    requirements={"engine": "redis", "memory_gib": 4},
                ),
            ],
        )


@pytest.mark.asyncio
async def test_unresolved_ai_product_identity_never_becomes_customer_choice() -> None:
    class FailedIdentityParser:
        async def parse(self, _: str) -> ParsedIntent:
            return ParsedIntent(
                customer_summary="待识别组件",
                services=[
                    ServiceRequirement(
                        service="unknown_component_storage",
                        calculator_service_name="共享文件存储",
                        region="ap-southeast-1",
                        source_text="共享文件存储：容量8TB。",
                        field_sources={"_identity_resolution_status": "failed"},
                    )
                ],
            )

    service = QuoteService(
        FailedIdentityParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
    )

    with pytest.raises(ManualConfirmationRequired) as exc_info:
        await service.preview(
            QuoteRequest(
                customer_request="共享文件存储：容量8TB。",
                sales_region="ap-southeast-1",
            )
        )

    assert exc_info.value.code == "service_identity_resolution_failed"
    assert "客户选择题" in exc_info.value.message
    assert exc_info.value.details["components"][0]["component_id"] == "1"
    assert exc_info.value.details["components"][0]["display_name"] == "共享文件存储"


class FailingEstimator:
    def quote(self, _: object) -> None:
        raise AssertionError("Calculator flow must not call BCM")


def test_pricing_requirement_copy_cannot_mutate_reviewed_configuration() -> None:
    reviewed = ServiceRequirement(
        service="rds",
        calculator_service_name="Amazon Aurora MySQL",
        region="eu-central-1",
        requirements={
            "engine": "aurora_mysql",
            "deployment": "multi_az",
            "cluster_members": 2,
        },
        field_sources={"requirements.deployment": "customer_text"},
        locked_fields=["requirements.deployment"],
    )

    pricing = QuoteService._pricing_requirement_copy(
        reviewed,
        service_key="rds",
        requirements={**reviewed.requirements, "deployment": "single_az"},
    )
    pricing.requirements["cluster_members"] = 99
    pricing.field_sources["requirements.deployment"] = "adapter"
    pricing.locked_fields.clear()

    assert reviewed.calculator_service_name == "Amazon Aurora MySQL"
    assert reviewed.requirements["deployment"] == "multi_az"
    assert reviewed.requirements["cluster_members"] == 2
    assert reviewed.field_sources["requirements.deployment"] == "customer_text"
    assert reviewed.locked_fields == ["requirements.deployment"]


class ApiEstimator:
    def quote(self, lines: list[UsageLine]) -> BcmQuoteResult:
        from app.domain.models import PricedLine

        return BcmQuoteResult(
            priced_lines=[
                PricedLine(
                    key=line.key,
                    service_code=line.service_code,
                    usage_type=line.usage_type,
                    operation=line.operation,
                    amount=line.amount,
                    cost=100.0,
                )
                for line in lines
            ],
            total_cost=100.0 * len(lines),
            currency="USD",
            rate_type="BEFORE_DISCOUNTS",
            rate_timestamp=None,
            estimate_id="11111111-1111-1111-1111-111111111111",
        )


class RejectOneComponentEstimator:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def quote(self, lines: list[UsageLine]) -> BcmQuoteResult:
        from app.domain.models import PricedLine

        keys = [line.key for line in lines]
        self.calls.append(keys)
        if "s2l1" in keys:
            raise ManualConfirmationRequired(
                "AWS BCM 无法识别一个或多个官方计费维度",
                code="bcm_usage_rejected",
                errors=[
                    {
                        "key": "s2l1",
                        "errorCode": "INVALID_USAGE",
                        "errorMessage": "unsupported dimension",
                    }
                ],
            )
        priced_lines = [
            PricedLine(
                key=line.key,
                service_code=line.service_code,
                usage_type=line.usage_type,
                operation=line.operation,
                amount=line.amount,
                cost=100.0,
            )
            for line in lines
        ]
        return BcmQuoteResult(
            priced_lines=priced_lines,
            total_cost=sum(line.cost for line in priced_lines),
            currency="USD",
            rate_type="BEFORE_DISCOUNTS",
            rate_timestamp=None,
            estimate_id="22222222-2222-2222-2222-222222222222",
        )


class RejectAllComponentsEstimator:
    def quote(self, lines: list[UsageLine]) -> BcmQuoteResult:
        raise ManualConfirmationRequired(
            "AWS BCM 无法识别一个或多个官方计费维度",
            code="bcm_usage_rejected",
            errors=[
                {
                    "key": line.key,
                    "errorCode": "INVALID_USAGE",
                    "errorMessage": "unsupported dimension",
                }
                for line in lines
            ],
        )


class ApiPlugin:
    def __init__(self, kind: ServiceKind, model: str):
        self.kind = kind
        self.model = model
        self.display_name = kind.value

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=requirement.region or default_region,
            model=self.model,
            architecture="official",
            specifications={},
            official_product={"source": "AWS"},
            rationale="official",
            usage_lines=[
                UsageLine(
                    key="line",
                    service_code="AmazonTest",
                    usage_type=f"Usage:{self.model}",
                    operation="Run",
                    amount=1,
                )
            ],
        )

    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        selection = self.select(requirement, default_region)
        return PreviewSelection(
            component_id="component",
            service=self.kind,
            display_name=self.display_name,
            region=selection.region,
            selected_model=selection.model,
            selection_reason="official",
            candidates=[
                CandidateOption(
                    model=selection.model,
                    family="official",
                    specifications={},
                    rationale="official",
                    is_default=True,
                )
            ],
        )


class ReservedUnavailablePlugin(ApiPlugin):
    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        if requirement.requirements.get("purchase_option") != "on_demand":
            raise ManualConfirmationRequired(
                "AWS 官方目录没有返回所选预留期限与付款方式的价格",
                code="reserved_term_not_found",
            )
        return super().select(requirement, default_region)


class ReservedCapablePlugin(ApiPlugin):
    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        selected = super().select(requirement, default_region)
        if requirement.requirements.get("purchase_option") == "on_demand":
            return selected
        term = int(requirement.requirements.get("reserved_term_years") or 1)
        return selected.model_copy(
            update={
                "usage_lines": [],
                "monthly_commitment_cost": 80.0 if term == 1 else 60.0,
            }
        )


class ReviewLockPlugin(ApiPlugin):
    """Mimic an adapter that would choose a cheaper model during pricing."""

    def __init__(
        self,
        kind: ServiceKind,
        review_model: str,
        cheapest_model: str,
    ) -> None:
        super().__init__(kind, cheapest_model)
        self.review_model = review_model
        self.received_models: list[str | None] = []

    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        requested_model = requirement.requirements.get("requested_model")
        self.received_models.append(str(requested_model) if requested_model is not None else None)
        selected_model = str(requested_model or self.model)
        original = self.model
        self.model = selected_model
        try:
            return super().select(requirement, default_region)
        finally:
            self.model = original

    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        selection = PreviewSelection(
            component_id="component",
            service=self.kind,
            display_name=self.display_name,
            region=requirement.region or default_region,
            selected_model=self.review_model,
            selection_reason="official review selection",
            candidates=[
                CandidateOption(
                    model=self.review_model,
                    family="official",
                    specifications={},
                    rationale="official review selection",
                    is_default=True,
                )
            ],
        )
        return selection


class ReferenceOnlyPlugin(ApiPlugin):
    def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
        return SelectedResource(
            service=self.kind,
            display_name=self.display_name,
            region=requirement.region or default_region,
            model=self.model,
            architecture="unit reference only",
            specifications={},
            official_product={"source": "AWS Price List"},
            rationale="official unit rate",
            reference_rates=[
                ReferenceRate(
                    description="官方单位价",
                    unit="GB-Mo",
                    unit_price=0.025,
                    service_code="AmazonS3",
                    usage_type="TimedStorage-ByteHrs",
                    operation="",
                )
            ],
        )

    def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
        selection = self.select(requirement, default_region)
        return PreviewSelection(
            component_id="component",
            service=self.kind,
            display_name=self.display_name,
            region=selection.region,
            selected_model=selection.model,
            selection_reason="official",
            candidates=[
                CandidateOption(
                    model=selection.model,
                    family="official",
                    specifications={},
                    rationale="official",
                    is_default=True,
                )
            ],
        )


def api_registry() -> PluginRegistry:
    return PluginRegistry(
        [
            ApiPlugin(ServiceKind.EC2, "t4g.xlarge"),
            ApiPlugin(ServiceKind.RDS, "db.m7g.xlarge"),
            ApiPlugin(ServiceKind.REDIS, "cache.t4g.medium"),
        ]
    )


def reserved_api_registry() -> PluginRegistry:
    return PluginRegistry(
        [
            ReservedCapablePlugin(ServiceKind.EC2, "t4g.xlarge"),
            ReservedCapablePlugin(ServiceKind.RDS, "db.m7g.xlarge"),
            ReservedCapablePlugin(ServiceKind.REDIS, "cache.t4g.medium"),
        ]
    )


class GenericCalculator:
    def __init__(self) -> None:
        self.inputs: list[GenericCalculatorInput] = []

    async def quote_ai_groups(
        self,
        quote_inputs: list[GenericCalculatorInput],
        reporter: object = None,
    ) -> CalculatorWebResult:
        self.inputs = quote_inputs
        return CalculatorWebResult(
            monthly_total=456.78,
            upfront_total=123,
            share_url="https://calculator.aws/#/estimate?id=generic-test",
            details=["Amazon EC2", "Amazon RDS for PostgreSQL", "Amazon ElastiCache"],
            steps=["三项服务已保存到同一个 Estimate"],
            generic_groups=[
                CalculatorGenericGroupResult("ec2", "Amazon EC2", "t4g.xlarge"),
                CalculatorGenericGroupResult("rds", "Amazon RDS for PostgreSQL", "db.m7g.xlarge"),
                CalculatorGenericGroupResult(
                    "elasticache", "Amazon ElastiCache", "cache.t4g.medium"
                ),
            ],
        )


@pytest.mark.asyncio
async def test_mixed_services_use_one_bcm_estimate() -> None:
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
    )

    quote = await service.create_quote(QuoteRequest(customer_request="混合报价"))

    assert quote.total_cost == 300.0
    assert len(quote.selections) == 3
    assert quote.pricing_source == "AWS BCM Pricing Calculator API"
    # Every plugin in this test deliberately returns an empty specification
    # object. The quote service must still carry the complete confirmed config
    # for all component types into the page and Excel export.
    assert quote.selections[0].specifications["vCPU"] == 4
    assert quote.selections[0].specifications["memoryGiB"] == 16
    assert quote.selections[1].specifications["deploymentOption"] == "multi_az"
    assert quote.selections[1].specifications["storageGiB"] == 500
    assert quote.selections[2].specifications["engine"] == "redis"
    assert quote.selections[2].specifications["memoryGiB"] == 4


@pytest.mark.asyncio
async def test_unpriceable_review_model_requires_customer_confirmation_before_replacement() -> None:
    class StaleModelPlugin(ApiPlugin):
        def __init__(self) -> None:
            super().__init__(ServiceKind.EC2, "m7i.xlarge")
            self.requests: list[tuple[str | None, object, object]] = []

        def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
            requested = requirement.requirements.get("requested_model")
            self.requests.append(
                (
                    str(requested) if requested else None,
                    requirement.requirements.get("vcpu"),
                    requirement.requirements.get("memory_gib"),
                )
            )
            if requested:
                raise ManualConfirmationRequired(
                    "old model has no billing product",
                    code="ec2_billing_product_not_found",
                )
            return super().select(requirement, default_region)

    plugin = StaleModelPlugin()
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([plugin, ApiPlugin(ServiceKind.RDS, "db.m7g.large")]),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
    )
    intent = ParsedIntent(
        customer_summary="EC2 and RDS",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                requirements={
                    "_review_selected_model": "mac1.metal",
                    "_review_selected_specifications": {
                        "vCPU": 12,
                        "memoryGiB": 32,
                    },
                    "operating_system": "windows",
                },
            ),
            ServiceRequirement(
                service="rds",
                region="ap-southeast-1",
                requirements={},
            ),
        ],
    )

    with pytest.raises(ManualConfirmationRequired) as error:
        await service._create_api_quote(
            intent,
            QuoteRequest(customer_request="EC2 and RDS"),
            None,
        )

    assert error.value.code == "batched_component_confirmation_required"
    component_error = error.value.details["component_errors"][0]
    assert component_error.details["requested_model"] == "mac1.metal"
    assert plugin.requests == [("mac1.metal", None, None)]


@pytest.mark.asyncio
async def test_one_catalog_failure_does_not_cancel_independent_components() -> None:
    class AlwaysUnavailablePlugin(ApiPlugin):
        def select(self, requirement: ServiceRequirement, default_region: str) -> SelectedResource:
            raise ManualConfirmationRequired(
                "catalog did not return a billable product",
                code="ec2_billing_product_not_found",
            )

    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry(
            [
                AlwaysUnavailablePlugin(ServiceKind.EC2, "bad"),
                ApiPlugin(ServiceKind.RDS, "db.m7g.large"),
            ]
        ),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
    )
    intent = ParsedIntent(
        customer_summary="independent components",
        services=[
            ServiceRequirement(service="ec2", region="ap-southeast-1"),
            ServiceRequirement(service="rds", region="ap-southeast-1"),
        ],
    )

    quote = await service._create_api_quote(
        intent,
        QuoteRequest(customer_request="independent components"),
        None,
    )

    assert [item.service for item in quote.selections] == [ServiceKind.EC2, ServiceKind.RDS]
    assert quote.selections[0].pricing_status == "unpriced"
    assert quote.selections[0].component_id == "0"
    assert quote.selections[1].component_id == "1"
    assert any("Amazon EC2" in notice or "ec2" in notice for notice in quote.notices)


@pytest.mark.asyncio
async def test_identical_model_questions_update_only_their_bound_components() -> None:
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
    )
    intent = ParsedIntent(
        customer_summary="two EC2 components",
        services=[
            ServiceRequirement(service="ec2", region="ap-southeast-1"),
            ServiceRequirement(service="ec2", region="ap-southeast-1"),
        ],
    )
    question = "EC2 还没有指定型号，请选择需要的型号。"
    first_key = QuoteService._scoped_confirmation_response_key(0, question)
    second_key = QuoteService._scoped_confirmation_response_key(1, question)

    await service._apply_confirmation_responses(
        intent,
        {
            first_key: "选择 t3.small",
            second_key: "选择 c7g.xlarge",
        },
        response_components={first_key: 0, second_key: 1},
    )

    assert intent.services[0].requirements["requested_model"] == "t3.small"
    assert intent.services[1].requirements["requested_model"] == "c7g.xlarge"


def test_new_service_auto_discovery_miss_is_component_isolatable() -> None:
    error = ManualConfirmationRequired(
        "official dimensions unavailable",
        code="generic_semantic_rate_not_found",
    )

    assert QuoteService._is_component_isolatable_pricing_error(error) is True


@pytest.mark.asyncio
async def test_rejected_bcm_component_does_not_cancel_other_prices() -> None:
    estimator = RejectOneComponentEstimator()
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        estimator,  # type: ignore[arg-type]
        None,
    )

    quote = await service.create_quote(QuoteRequest(customer_request="混合报价"))

    assert quote.total_cost == 200.0
    assert {line.key for line in quote.priced_lines} == {"s1l1", "s3l1"}
    assert quote.pricing_scenarios[0].component_costs == {
        "0": 100.0,
        "1": 0.0,
        "2": 100.0,
    }
    assert quote.is_partial is True
    assert quote.incomplete_component_ids == ["1"]
    assert quote.pricing_scenarios[0].is_partial is True
    assert quote.pricing_scenarios[0].incomplete_component_ids == ["1"]
    assert quote.selections[1].pricing_status == "unpriced"
    assert sorted(estimator.calls) == [["s1l1"], ["s2l1"], ["s3l1"]]
    assert any(
        "RDS" in notice and "本次未取得可累计的官方月费" in notice for notice in quote.notices
    )


@pytest.mark.asyncio
async def test_all_rejected_bcm_components_return_reference_only_quote() -> None:
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        RejectAllComponentsEstimator(),  # type: ignore[arg-type]
        None,
    )

    quote = await service.create_quote(QuoteRequest(customer_request="混合报价"))

    assert quote.status.value == "quoted"
    assert quote.total_cost == 0.0
    assert quote.priced_lines == []
    assert len(quote.selections) == 3
    assert quote.rate_type == "REFERENCE_RATES_ONLY"
    assert quote.is_partial is True
    assert quote.incomplete_component_ids == ["0", "1", "2"]
    assert all(selection.pricing_status == "unpriced" for selection in quote.selections)
    assert len([notice for notice in quote.notices if "本次未取得可累计的官方月费" in notice]) == 3


@pytest.mark.asyncio
async def test_reserved_one_and_three_year_terms_produce_two_scenarios_without_reparsing() -> None:
    class CountingParser(MixedParser):
        calls = 0

        async def parse(self, text: str) -> ParsedIntent:
            self.calls += 1
            return await super().parse(text)

    parser = CountingParser()
    service = QuoteService(
        parser,  # type: ignore[arg-type]
        reserved_api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
    )

    quote = await service.create_quote(
        QuoteRequest(
            customer_request="混合报价",
            pricing_mode="standard_reserved",
            reserved_term_options=[3, 1],
            payment_option="no_upfront",
        )
    )

    assert parser.calls == 1
    assert [item.reserved_term_years for item in quote.pricing_scenarios] == [1, 3]
    assert [item.label for item in quote.pricing_scenarios] == [
        "1年无预付",
        "3年无预付",
    ]


@pytest.mark.asyncio
async def test_on_demand_and_reserved_terms_produce_three_comparison_scenarios() -> None:
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        reserved_api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
    )

    quote = await service.create_quote(
        QuoteRequest(
            customer_request="混合报价",
            pricing_mode="standard_reserved",
            reserved_term_options=[1, 3],
            payment_option="all_upfront",
            include_on_demand_scenario=True,
        )
    )

    assert [item.label for item in quote.pricing_scenarios] == [
        "按需",
        "1年全预付",
        "3年全预付",
    ]
    assert all(
        set(item.component_pricing_basis.values()) == {"reserved"}
        for item in quote.pricing_scenarios[1:]
    )


@pytest.mark.asyncio
async def test_services_without_reserved_terms_do_not_show_duplicate_comparison_columns() -> None:
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
    )

    quote = await service.create_quote(
        QuoteRequest(
            customer_request="混合报价",
            pricing_mode="standard_reserved",
            reserved_term_options=[1, 3],
            payment_option="all_upfront",
            include_on_demand_scenario=True,
        )
    )

    assert [item.label for item in quote.pricing_scenarios] == ["按需"]
    assert len([notice for notice in quote.notices if "没有复制按需价" in notice]) == 2


@pytest.mark.asyncio
async def test_unavailable_reserved_offer_does_not_become_customer_question() -> None:
    registry = PluginRegistry(
        [
            ApiPlugin(ServiceKind.EC2, "t4g.xlarge"),
            ApiPlugin(ServiceKind.RDS, "db.m7g.xlarge"),
            ReservedUnavailablePlugin(ServiceKind.REDIS, "cache.m4.xlarge"),
        ]
    )
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        registry,
        ApiEstimator(),  # type: ignore[arg-type]
        None,
    )

    quote = await service.create_quote(
        QuoteRequest(
            customer_request="混合报价",
            pricing_mode="standard_reserved",
            reserved_term_options=[1, 3],
            payment_option="all_upfront",
            include_on_demand_scenario=True,
        )
    )

    assert [item.label for item in quote.pricing_scenarios] == ["按需"]
    assert len(quote.notices) == 2
    assert all("本方案暂不展示" in notice for notice in quote.notices)


def test_official_reserved_price_amortizes_upfront_for_each_term() -> None:
    product = {
        "terms": {
            "Reserved": {
                "one": {
                    "termAttributes": {
                        "LeaseContractLength": "1yr",
                        "PurchaseOption": "Partial Upfront",
                        "OfferingClass": "standard",
                    },
                    "priceDimensions": {
                        "hour": {"unit": "Hrs", "pricePerUnit": {"USD": "0.10"}},
                        "upfront": {"unit": "Quantity", "pricePerUnit": {"USD": "120"}},
                    },
                },
                "three": {
                    "termAttributes": {
                        "LeaseContractLength": "3yr",
                        "PurchaseOption": "Partial Upfront",
                        "OfferingClass": "standard",
                    },
                    "priceDimensions": {
                        "hour": {"unit": "Hrs", "pricePerUnit": {"USD": "0.05"}},
                        "upfront": {"unit": "Quantity", "pricePerUnit": {"USD": "180"}},
                    },
                },
            }
        }
    }

    one = PricingCatalog.reserved_price(
        product,
        years=1,
        payment_option="partial_upfront",
        offering_class="standard",
    )
    three = PricingCatalog.reserved_price(
        product,
        years=3,
        payment_option="partial_upfront",
        offering_class="standard",
    )

    assert one.monthly_amortized == pytest.approx(83.0)
    assert one.upfront == 120
    assert three.monthly_amortized == pytest.approx(41.5)
    assert three.upfront == 180


@pytest.mark.asyncio
async def test_missing_region_uses_api_default_region() -> None:
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
    )

    quote = await service.create_quote(QuoteRequest(customer_request="混合报价"))
    assert quote.selections[2].region == "ap-southeast-1"


@pytest.mark.asyncio
async def test_reference_only_quote_never_calls_bcm_or_adds_fake_monthly_cost() -> None:
    class ReferenceParser:
        async def parse(self, _: str) -> ParsedIntent:
            return ParsedIntent(
                customer_summary="S3 单位参考价",
                services=[ServiceRequirement(service="s3", requirements={})],
            )

    registry = PluginRegistry([])
    registry._plugins[ServiceKind.S3] = ReferenceOnlyPlugin(  # type: ignore[assignment]
        ServiceKind.S3, "S3 Standard"
    )
    service = QuoteService(
        ReferenceParser(),  # type: ignore[arg-type]
        registry,
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    quote = await service.create_quote(QuoteRequest(customer_request="需要 S3，容量待定"))

    assert quote.total_cost == 0
    assert quote.priced_lines == []
    assert quote.rate_type == "REFERENCE_RATES_ONLY"
    assert quote.source_url is None
    assert quote.selections[0].reference_rates[0].unit_price == 0.025


@pytest.mark.asyncio
async def test_preview_validates_plugins_without_calling_estimator() -> None:
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        FailingEstimator(),  # type: ignore[arg-type]
        GenericCalculator(),  # type: ignore[arg-type]
    )

    preview = await service.preview(QuoteRequest(customer_request="混合报价"))

    assert len(preview.selections) == 3
    assert preview.selections[2].display_name == "Amazon ElastiCache for Redis"
    # When the whole request contains one explicit region, components without
    # their own region inherit that quote-wide region.
    assert preview.selections[2].region == "ap-northeast-1"


@pytest.mark.asyncio
async def test_customer_must_approve_complete_configuration_before_pricing(tmp_path) -> None:
    store = ConfirmationSessionStore(tmp_path / "configuration-review.sqlite3")
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
        confirmation_sessions=store,
    )
    request = QuoteRequest(customer_request="混合报价")

    preview = await service.preview(request)

    assert preview.configuration_review_required is True
    assert preview.confirmation_token is not None
    session = store.get(preview.confirmation_token)
    assert session is not None
    assert session.status == "configuration_review"
    assert len(session.configuration_items) == 3

    with pytest.raises(ManualConfirmationRequired) as blocked:
        await service.create_quote(
            QuoteRequest(customer_request="混合报价", draft_id=preview.draft_id)
        )
    assert blocked.value.code == "configuration_review_required"

    store.approve_configuration(preview.confirmation_token)
    quote = await service.create_quote(
        QuoteRequest(customer_request="混合报价", draft_id=preview.draft_id)
    )
    assert quote.total_cost == 300
    completed = store.get(preview.confirmation_token)
    assert completed is not None
    assert completed.status == "completed"

    # A finished confirmation remains authoritative when the customer retries
    # after a transient pricing failure. Requiring a second approval here made
    # the "保留配置并重新报价" button fail immediately.
    retried = await service.create_quote(
        QuoteRequest(customer_request="混合报价", draft_id=preview.draft_id)
    )
    assert retried.total_cost == 300


@pytest.mark.asyncio
async def test_internal_validation_failure_is_sales_only_and_blocks_customer_link(
    tmp_path,
) -> None:
    class OneEc2Parser:
        async def parse(self, _: str) -> ParsedIntent:
            return ParsedIntent(
                customer_summary="EC2",
                services=[
                    ServiceRequirement(
                        service="ec2",
                        region="ap-southeast-1",
                        requirements={"vcpu": 4, "memory_gib": 16},
                    )
                ],
            )

    class InternalFailurePlugin(ApiPlugin):
        def preview(
            self,
            requirement: ServiceRequirement,
            default_region: str,
        ) -> PreviewSelection:
            raise ManualConfirmationRequired(
                "官方计费映射正在内部同步",
                code="pricing_catalog_unavailable",
            )

    store = ConfirmationSessionStore(tmp_path / "sales-only-validation.sqlite3")
    service = QuoteService(
        OneEc2Parser(),  # type: ignore[arg-type]
        PluginRegistry([InternalFailurePlugin(ServiceKind.EC2, "m7i.xlarge")]),
        FailingEstimator(),  # type: ignore[arg-type]
        confirmation_sessions=store,
    )

    preview = await service.preview(QuoteRequest(customer_request="EC2 4核16G"))

    assert preview.sales_validation_required is True
    assert preview.confirmation_token is None
    assert preview.configuration_review_required is False
    assert store.status_by_draft(preview.draft_id) is None


@pytest.mark.asyncio
async def test_unexpected_adapter_error_isolated_to_its_component(tmp_path) -> None:
    class TwoServiceParser:
        async def parse(self, _: str) -> ParsedIntent:
            return ParsedIntent(
                customer_summary="两个独立组件",
                services=[
                    ServiceRequirement(
                        service="waf",
                        region="ap-southeast-1",
                        requirements={"web_acls": 1, "rules": 2, "requests": 1_000_000},
                    ),
                    ServiceRequirement(
                        service="s3",
                        region="ap-southeast-1",
                        requirements={"storage_gib": 100},
                    ),
                ],
            )

    class CrashingPlugin(ApiPlugin):
        def preview(self, requirement, default_region):
            raise ValueError("malformed cached numeric value")

    service = QuoteService(
        TwoServiceParser(),  # type: ignore[arg-type]
        PluginRegistry(
            [
                CrashingPlugin(ServiceKind.WAF, "WAF"),
                ApiPlugin(ServiceKind.S3, "S3 Standard"),
            ]
        ),
        FailingEstimator(),  # type: ignore[arg-type]
        confirmation_sessions=ConfirmationSessionStore(
            tmp_path / "component-exception-isolation.sqlite3"
        ),
    )

    preview = await service.preview(QuoteRequest(customer_request="WAF 和 S3"))

    assert len(preview.selections) == 2
    assert preview.selections[0].status == "technical_issue"
    assert preview.selections[0].issue_category == "retryable"
    assert preview.selections[1].status == "ready"
    assert preview.sales_validation_required is True
    assert preview.confirmation_token is None


@pytest.mark.asyncio
async def test_internal_retry_revalidates_only_failed_component(tmp_path) -> None:
    class TwoComponentParser:
        async def parse(self, _: str) -> ParsedIntent:
            return ParsedIntent(
                customer_summary="两个独立组件",
                services=[
                    ServiceRequirement(
                        service="waf",
                        region="ap-southeast-1",
                        requirements={"web_acls": 1, "rules": 2, "requests": 1_000_000},
                    ),
                    ServiceRequirement(
                        service="sqs",
                        region="ap-southeast-1",
                        requirements={"requests": 1_000_000},
                    ),
                    ServiceRequirement(
                        service="s3",
                        region="ap-southeast-1",
                        requirements={"storage_gib": 100},
                    ),
                ],
            )

    class CountingReadyPlugin(ApiPlugin):
        calls = 0

        def preview(self, requirement, default_region):
            self.calls += 1
            return super().preview(requirement, default_region)

    class RecoveringPlugin(ApiPlugin):
        calls = 0

        def preview(self, requirement, default_region):
            self.calls += 1
            if self.calls <= 3:
                raise ManualConfirmationRequired(
                    "目录正在同步",
                    code="pricing_catalog_unavailable",
                )
            return super().preview(requirement, default_region)

    class CustomerChoicePlugin(ApiPlugin):
        calls = 0

        def preview(self, requirement, default_region):
            self.calls += 1
            return PreviewSelection(
                component_id="component",
                service=ServiceKind.SQS,
                display_name="Amazon SQS",
                region=requirement.region or default_region,
                candidates=[
                    CandidateOption(
                        model="Standard",
                        family="sqs",
                        specifications={"decision": "standard"},
                        rationale="official",
                    ),
                    CandidateOption(
                        model="FIFO",
                        family="sqs",
                        specifications={"decision": "fifo"},
                        rationale="official",
                    ),
                ],
                requires_confirmation=True,
                confirmation_reason="请选择 SQS 队列类型",
            )

    ready = CountingReadyPlugin(ServiceKind.WAF, "WAF Basic Protection")
    customer_choice = CustomerChoicePlugin(ServiceKind.SQS, "SQS")
    recovering = RecoveringPlugin(ServiceKind.S3, "S3 Standard")
    service = QuoteService(
        TwoComponentParser(),  # type: ignore[arg-type]
        PluginRegistry([ready, customer_choice, recovering]),
        FailingEstimator(),  # type: ignore[arg-type]
        confirmation_sessions=ConfirmationSessionStore(
            tmp_path / "component-scoped-retry.sqlite3"
        ),
    )

    first = await service.preview(QuoteRequest(customer_request="WAF 和 S3"))
    assert first.sales_validation_required is True
    ready_calls_after_first_pass = ready.calls
    customer_choice_calls_after_first_pass = customer_choice.calls
    failed_calls_after_first_pass = recovering.calls
    assert ready_calls_after_first_pass >= 1
    assert failed_calls_after_first_pass >= 3
    assert service._drafts[first.draft_id][1].services[1].requirements[
        "_review_status"
    ] == "customer_issue"

    second = await service.preview(
        QuoteRequest(
            customer_request="WAF 和 S3",
            draft_id=first.draft_id,
            retry_component_ids=[2],
        )
    )

    assert second.sales_validation_required is False
    assert ready.calls == ready_calls_after_first_pass
    assert customer_choice.calls == customer_choice_calls_after_first_pass
    assert recovering.calls > failed_calls_after_first_pass
    assert [selection.status for selection in second.selections] == [
        "ready",
        "customer_issue",
        "ready",
    ]
    assert "SQS" in (second.selections[1].confirmation_reason or "")
    assert {candidate.model for candidate in second.selections[1].candidates} == {
        "Standard",
        "FIFO",
    }


@pytest.mark.asyncio
async def test_later_invalid_component_edit_stays_on_configuration_table(tmp_path) -> None:
    class RevisionParser:
        async def parse(self, _: str) -> ParsedIntent:
            raise AssertionError("saved draft must be reused")

        async def revise_component_from_feedback(
            self, _original: str, component: ServiceRequirement, _feedback: str
        ) -> ServiceRequirement:
            revised = component.model_copy(deep=True)
            revised.requirements = {"vcpu": 7, "memory_gib": 28}
            revised.source_text = "客户把规格改为7核28G"
            return revised

    class UnavailableShapePlugin(ApiPlugin):
        def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
            return PreviewSelection(
                component_id="component",
                service=ServiceKind.EC2,
                display_name="Amazon EC2",
                region=requirement.region or default_region,
                candidates=[
                    CandidateOption(
                        model="m7i.xlarge",
                        family="general_purpose",
                        specifications={"vCPU": 4, "memoryGiB": 16},
                        rationale="official",
                    ),
                    CandidateOption(
                        model="m7i.2xlarge",
                        family="general_purpose",
                        specifications={"vCPU": 8, "memoryGiB": 32},
                        rationale="official",
                    ),
                ],
                requires_confirmation=True,
                confirmation_reason="AWS 没有完全相同的规格",
            )

    store = ConfirmationSessionStore(tmp_path / "edit-stays-on-table.sqlite3")
    service = QuoteService(
        RevisionParser(),  # type: ignore[arg-type]
        PluginRegistry([UnavailableShapePlugin(ServiceKind.EC2, "m7i.xlarge")]),
        FailingEstimator(),  # type: ignore[arg-type]
        confirmation_sessions=store,
    )
    original = ParsedIntent(
        customer_summary="EC2",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                quantity=3,
                requirements={
                    "requested_model": "m7i.xlarge",
                    "_review_selected_model": "m7i.xlarge",
                    "_review_selected_specifications": {
                        "vCPU": 4,
                        "memoryGiB": 16,
                    },
                    "vcpu": 4,
                    "memory_gib": 16,
                    "operating_system": "linux",
                    "storage_gib": 500,
                },
                source_text="EC2 m7i.xlarge，4核16G，3台，500G磁盘",
            )
        ],
    )
    service._drafts["bad-edit-001"] = ("EC2", original)

    preview = await service.preview(
        QuoteRequest(
            customer_request="EC2",
            draft_id="bad-edit-001",
            confirmation_responses={
                f"{CONFIGURATION_COMPONENT_FEEDBACK_PREFIX}0": "规格改成7核28G"
            },
        )
    )

    assert preview.configuration_review_required is True
    assert preview.confirmation_items == []
    assert preview.confirmation_text is None
    session = store.get(preview.confirmation_token or "")
    assert session is not None
    assert session.status == "configuration_review"
    assert session.confirmation_text == ("当前区域没有完全相同的规格，已保留原配置，请重新修改。")
    restored_item = session.configuration_items[0]
    assert restored_item.pricing_status == "ready"
    assert restored_item.selected_model == "m7i.xlarge"
    assert restored_item.quantity == 3
    assert restored_item.requirements["vcpu"] == 4
    assert restored_item.requirements["memory_gib"] == 16
    assert restored_item.requirements["operating_system"] == "linux"
    assert restored_item.requirements["storage_gib"] == 500
    saved_draft = service._drafts["bad-edit-001"][1].services[0]
    assert saved_draft.requirements["requested_model"] == "m7i.xlarge"
    assert saved_draft.requirements["memory_gib"] == 16


@pytest.mark.asyncio
async def test_new_component_with_missing_choice_opens_its_own_question_window(tmp_path) -> None:
    class AdditionParser:
        async def parse(self, _: str) -> ParsedIntent:
            raise AssertionError("saved draft must be reused")

        async def revise_configuration_from_feedback(
            self,
            _original: str,
            intent: ParsedIntent,
            feedback: str,
        ) -> ParsedIntent:
            assert feedback == "请新增以下配置：\n俩台 EC2，7核28G"
            revised = intent.model_copy(deep=True)
            revised.services.append(
                ServiceRequirement(
                    service="ec2",
                    region="ap-southeast-1",
                    quantity=2,
                    requirements={"vcpu": 7, "memory_gib": 28},
                    source_text="俩台 EC2，7核28G",
                )
            )
            return revised

    class AdditionChoicePlugin(ApiPlugin):
        def preview(
            self,
            requirement: ServiceRequirement,
            default_region: str,
        ) -> PreviewSelection:
            return PreviewSelection(
                component_id="component",
                service=ServiceKind.EC2,
                display_name="Amazon EC2",
                region=requirement.region or default_region,
                quantity=requirement.quantity,
                candidates=[
                    CandidateOption(
                        model="m7i.xlarge",
                        family="general_purpose",
                        specifications={"vCPU": 4, "memoryGiB": 16},
                        rationale="official",
                    ),
                    CandidateOption(
                        model="m7i.2xlarge",
                        family="general_purpose",
                        specifications={"vCPU": 8, "memoryGiB": 32},
                        rationale="official",
                    ),
                ],
                requires_confirmation=True,
                confirmation_reason="新增的 EC2 没有完全一样的规格，请选择合适配置。",
            )

    store = ConfirmationSessionStore(tmp_path / "isolated-addition.sqlite3")
    service = QuoteService(
        AdditionParser(),  # type: ignore[arg-type]
        PluginRegistry([AdditionChoicePlugin(ServiceKind.EC2, "m7i.xlarge")]),
        FailingEstimator(),  # type: ignore[arg-type]
        confirmation_sessions=store,
    )
    original = ParsedIntent(
        customer_summary="现有 EC2",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                quantity=1,
                requirements={
                    "requested_model": "m7i.large",
                    "_review_selected_model": "m7i.large",
                    "_review_selected_specifications": {"vCPU": 2, "memoryGiB": 8},
                    "_review_status": "ready",
                },
                source_text="原有 EC2 一台",
            )
        ],
    )
    draft_id = "add-row-0001"
    service._drafts[draft_id] = ("原有 EC2 一台", original.model_copy(deep=True))
    token = store.create_or_replace(
        draft_id=draft_id,
        customer_request="原有 EC2 一台",
        customer_summary=original.customer_summary,
        intent=original,
        confirmation_text="请确认",
        items=[],
        quote_request=QuoteRequest(customer_request="原有 EC2 一台", draft_id=draft_id),
    )
    store.prepare_configuration_review(draft_id=draft_id, intent=original)

    preview = await service.preview(
        QuoteRequest(
            customer_request="原有 EC2 一台",
            draft_id=draft_id,
            confirmation_responses={
                CONFIGURATION_FEEDBACK_QUESTION: "请新增以下配置：\n俩台 EC2，7核28G"
            },
        )
    )

    assert preview.confirmation_token == token
    assert preview.configuration_review_required is False
    assert len(preview.confirmation_items) == 1
    assert preview.confirmation_items[0].component_id == "1"
    session = store.get(token)
    assert session is not None
    assert session.status == "pending"
    assert len(session.configuration_items) == 2
    assert session.configuration_items[0].source_text == "原有 EC2 一台"
    assert session.configuration_items[1].source_text == "俩台 EC2，7核28G"
    assert session.configuration_items[1].quantity == 2


@pytest.mark.asyncio
async def test_customer_answers_are_ai_reviewed_before_configuration_review() -> None:
    class AnswerReviewParser:
        def __init__(self) -> None:
            self.reviewed_answers: dict[str, str] | None = None

        async def parse(self, _: str) -> ParsedIntent:
            raise AssertionError("customer answers must reuse the saved draft")

        async def finalize_confirmed_intent(
            self,
            _original: str,
            intent: ParsedIntent,
            responses: dict[str, str],
        ) -> ParsedIntent:
            self.reviewed_answers = dict(responses)
            reviewed = intent.model_copy(deep=True)
            reviewed.ambiguities = ["客户回答仍不明确，请确认需要 2 台还是 3 台。"]
            return reviewed

    parser = AnswerReviewParser()
    service = QuoteService(
        parser,  # type: ignore[arg-type]
        PluginRegistry([ApiPlugin(ServiceKind.EC2, "m7i.xlarge")]),
        FailingEstimator(),  # type: ignore[arg-type]
    )
    intent = ParsedIntent(
        customer_summary="EC2",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                quantity=2,
                requirements={"requested_model": "m7i.xlarge"},
            )
        ],
    )
    service._drafts["ans-review01"] = ("EC2", intent)

    result = await service.preview(
        QuoteRequest(
            customer_request="EC2",
            draft_id="ans-review01",
            confirmation_responses={"请确认服务器数量。": "两三台都可以"},
        )
    )

    assert parser.reviewed_answers == {"请确认服务器数量。": "两三台都可以"}
    assert result.configuration_review_required is False
    assert result.confirmation_text is not None
    assert any("2 台还是 3 台" in item.question for item in result.confirmation_items)


@pytest.mark.asyncio
async def test_structured_catalog_answer_does_not_call_ai_finalizer() -> None:
    class StructuredAnswerParser:
        async def parse(self, _: str) -> ParsedIntent:
            raise AssertionError("structured answer must reuse the saved draft")

        async def finalize_confirmed_intent(self, *args, **kwargs) -> ParsedIntent:
            raise AssertionError("structured catalog answers do not need AI review")

    service = QuoteService(
        StructuredAnswerParser(),  # type: ignore[arg-type]
        PluginRegistry([ApiPlugin(ServiceKind.EC2, "m7i.xlarge")]),
        FailingEstimator(),  # type: ignore[arg-type]
    )
    intent = ParsedIntent(
        customer_summary="EC2",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                requirements={"requested_model": "m7i.xlarge"},
            )
        ],
    )
    service._drafts["structured01"] = ("EC2", intent)

    result = await service.preview(
        QuoteRequest(
            customer_request="EC2",
            draft_id="structured01",
            confirmation_responses={
                "收费方式": "billing_variant:requests:APS1-Requests"
            },
        )
    )

    assert result.confirmation_items == []


@pytest.mark.asyncio
async def test_structured_component_edit_revalidates_only_the_changed_component(
    tmp_path,
) -> None:
    class SavedDraftParser:
        async def parse(self, _: str) -> ParsedIntent:
            raise AssertionError("a final-table edit must reuse the saved draft")

    class CountingPlugin(ApiPlugin):
        def __init__(self, kind: ServiceKind, model: str) -> None:
            super().__init__(kind, model)
            self.preview_calls = 0

        def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
            self.preview_calls += 1
            return super().preview(requirement, default_region)

    ec2_plugin = CountingPlugin(ServiceKind.EC2, "m7i.xlarge")
    rds_plugin = CountingPlugin(ServiceKind.RDS, "db.m7g.large")
    store = ConfirmationSessionStore(tmp_path / "scoped-edit.sqlite3")
    service = QuoteService(
        SavedDraftParser(),  # type: ignore[arg-type]
        PluginRegistry([ec2_plugin, rds_plugin]),
        FailingEstimator(),  # type: ignore[arg-type]
        confirmation_sessions=store,
    )
    intent = ParsedIntent(
        customer_summary="EC2 和 RDS",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                requirements={
                    "requested_model": "m7i.xlarge",
                    "storage_gib": 100,
                    "_review_selected_model": "m7i.xlarge",
                    "_review_selected_specifications": {
                        "vCPU": 4,
                        "memoryGiB": 16,
                    },
                    "_review_available_shapes": [{"vcpu": 4, "memory_gib": 16}],
                },
            ),
            ServiceRequirement(
                service="rds",
                region="ap-southeast-1",
                requirements={
                    "requested_model": "db.m7g.large",
                    "_review_selected_model": "db.m7g.large",
                    "_review_selected_specifications": {
                        "vCPU": 2,
                        "memoryGiB": 8,
                    },
                    "_review_available_shapes": [{"vcpu": 2, "memory_gib": 8}],
                },
            ),
        ],
    )
    draft_id = "scope-edit01"
    service._drafts[draft_id] = ("EC2 和 RDS", intent.model_copy(deep=True))
    store.create_or_replace(
        draft_id=draft_id,
        customer_request="EC2 和 RDS",
        customer_summary=intent.customer_summary,
        intent=intent,
        confirmation_text="请确认",
        items=[],
    )
    store.prepare_configuration_review(draft_id=draft_id, intent=intent)

    result = await service.preview(
        QuoteRequest(
            customer_request="EC2 和 RDS",
            draft_id=draft_id,
            confirmation_responses={
                f"{CONFIGURATION_COMPONENT_UPDATE_PREFIX}0": (
                    '{"region":"eu-central-1","requirements":{"storage_gib":20480}}'
                )
            },
        )
    )

    assert result.configuration_review_required is True
    assert ec2_plugin.preview_calls >= 1
    assert rds_plugin.preview_calls == 0
    session = store.get(result.confirmation_token or "")
    assert session is not None
    assert session.configuration_items[0].requirements["storage_gib"] == 20480
    assert session.configuration_items[1].selected_model == "db.m7g.large"
    assert session.configuration_items[1].available_shapes == [{"vcpu": 2.0, "memory_gib": 8.0}]

    ec2_plugin.preview_calls = 0
    rds_plugin.preview_calls = 0
    quantity_result = await service.preview(
        QuoteRequest(
            customer_request="EC2 和 RDS",
            draft_id=draft_id,
            confirmation_responses={f"{CONFIGURATION_COMPONENT_UPDATE_PREFIX}0": '{"quantity":9}'},
        )
    )
    assert ec2_plugin.preview_calls == 0
    assert rds_plugin.preview_calls == 0
    quantity_session = store.get(quantity_result.confirmation_token or "")
    assert quantity_session is not None
    assert quantity_session.configuration_items[0].quantity == 9


@pytest.mark.asyncio
async def test_reviewed_models_are_locked_across_restart_for_all_service_plugins(tmp_path) -> None:
    class ReviewedParser:
        async def parse(self, _: str) -> ParsedIntent:
            return ParsedIntent(
                customer_summary="EC2 和 RDS",
                services=[
                    ServiceRequirement(
                        service="ec2",
                        region="ap-southeast-1",
                        quantity=4,
                        requirements={"vcpu": 8, "memory_gib": 16},
                    ),
                    ServiceRequirement(
                        service="rds",
                        region="ap-southeast-1",
                        requirements={
                            "engine": "mysql",
                            "deployment": "multi_az",
                            "storage_gib": 800,
                        },
                    ),
                ],
            )

    store = ConfirmationSessionStore(tmp_path / "review-model-lock.sqlite3")
    ec2 = ReviewLockPlugin(ServiceKind.EC2, "c6g.2xlarge", "a1.2xlarge")
    rds = ReviewLockPlugin(ServiceKind.RDS, "db.m6g.large", "db.t3.micro")
    registry = PluginRegistry([ec2, rds])  # type: ignore[list-item]
    request = QuoteRequest(customer_request="EC2 与 RDS 报价")
    preview_service = QuoteService(
        ReviewedParser(),  # type: ignore[arg-type]
        registry,
        ApiEstimator(),  # type: ignore[arg-type]
        confirmation_sessions=store,
    )

    preview = await preview_service.preview(request)
    assert preview.confirmation_token is not None
    store.approve_configuration(preview.confirmation_token)

    # A fresh service instance simulates a process restart.  The quote must
    # restore the reviewed draft and may not fall back to a cheaper model.
    pricing_service = QuoteService(
        ReviewedParser(),  # type: ignore[arg-type]
        registry,
        ApiEstimator(),  # type: ignore[arg-type]
        confirmation_sessions=store,
    )
    quote = await pricing_service.create_quote(
        QuoteRequest(
            customer_request="EC2 与 RDS 报价",
            draft_id=preview.draft_id,
        )
    )

    assert [selection.model for selection in quote.selections] == [
        "c6g.2xlarge",
        "db.m6g.large",
    ]
    assert ec2.received_models[-1] == "c6g.2xlarge"
    assert rds.received_models[-1] == "db.m6g.large"


def test_private_review_metadata_is_not_sent_to_pricing_adapters() -> None:
    requirements = QuoteService._calculator_requirements(
        {
            "vcpu": 8,
            "_review_selected_model": "c6g.2xlarge",
            "_review_selected_specifications": {"vCPU": 8},
            "_quote_skip_reason": "internal",
        },
        1,
        "ec2",
    )

    assert requirements == {"vcpu": 8}


def test_confirmed_model_mismatch_is_never_silently_accepted() -> None:
    with pytest.raises(ManualConfirmationRequired) as blocked:
        QuoteService._require_confirmed_model_match(
            "c6g.2xlarge",
            "a1.2xlarge",
            component_id="0",
            service="ec2",
            display_name="Amazon EC2",
        )

    assert blocked.value.code == "confirmed_model_mismatch"
    assert blocked.value.details["confirmed_model"] == "c6g.2xlarge"
    assert blocked.value.details["priced_model"] == "a1.2xlarge"


@pytest.mark.asyncio
async def test_preview_sends_one_component_error_back_to_ai_and_retries() -> None:
    class RepairingParser:
        repairs = 0

        async def parse(self, _: str) -> ParsedIntent:
            return ParsedIntent(
                customer_summary="S3",
                services=[ServiceRequirement(service="s3", requirements={"bad_unit": "500GB"})],
            )

        async def repair_quote_component(
            self,
            original_text: str,
            component: ServiceRequirement,
            **_: object,
        ) -> ServiceRequirement:
            self.repairs += 1
            return component.model_copy(update={"requirements": {"storage_gib": 500}})

    class RepairableS3Plugin(ApiPlugin):
        def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
            if requirement.requirements.get("storage_gib") != 500:
                raise ManualConfirmationRequired(
                    "storage field is invalid", code="invalid_requirement"
                )
            return super().preview(requirement, default_region)

    parser = RepairingParser()
    registry = PluginRegistry([])
    registry._plugins[ServiceKind.S3] = RepairableS3Plugin(  # type: ignore[assignment]
        ServiceKind.S3, "S3 Standard"
    )
    service = QuoteService(
        parser,  # type: ignore[arg-type]
        registry,
        FailingEstimator(),  # type: ignore[arg-type]
    )

    preview = await service.preview(QuoteRequest(customer_request="S3 500GB"))

    assert parser.repairs == 1
    assert preview.selections[0].status == "ready"
    assert preview.selections[0].requirements["storage_gib"] == 500
    assert any("系统自动修正 1 次" in event.message for event in preview.execution_trace)


@pytest.mark.asyncio
async def test_preview_keeps_ec2_system_and_data_disks_separate_for_display() -> None:
    class Ec2DiskParser:
        async def parse(self, _: str) -> ParsedIntent:
            return ParsedIntent(
                customer_summary="EC2 disks",
                services=[
                    ServiceRequirement(
                        service="ec2",
                        region="ap-southeast-1",
                        quantity=3,
                        requirements={
                            "vcpu": 8,
                            "memory_gib": 32,
                            "system_disk_gib": 200,
                            "additional_ebs_volumes": [
                                {
                                    "size_gib": 500,
                                    "volume_type": "gp3",
                                    "count_per_instance": 1,
                                }
                            ],
                        },
                    )
                ],
            )

    service = QuoteService(
        Ec2DiskParser(),  # type: ignore[arg-type]
        PluginRegistry([ApiPlugin(ServiceKind.EC2, "t4g.2xlarge")]),
        FailingEstimator(),  # type: ignore[arg-type]
    )

    preview = await service.preview(QuoteRequest(customer_request="EC2 disks"))

    requirements = preview.selections[0].requirements
    assert requirements["system_disk_gib"] == 200
    assert requirements["additional_ebs_volumes"] == [
        {"size_gib": 500, "volume_type": "gp3", "count_per_instance": 1}
    ]


@pytest.mark.asyncio
async def test_missing_region_precedes_redis_size_question() -> None:
    class RedisParser:
        async def parse(self, _: str) -> ParsedIntent:
            return ParsedIntent(
                customer_summary="Redis 主从",
                services=[
                    ServiceRequirement(
                        service="elasticache",
                        calculator_service_name="Amazon ElastiCache",
                        quantity=2,
                        requirements={"engine": "redis", "shards": 1, "replicas_per_shard": 1},
                    )
                ],
            )

    class MissingSizePlugin:
        def preview(self, *_: object) -> PreviewSelection:
            raise ManualConfirmationRequired(
                "Redis 需求缺少节点型号、内存或 vCPU",
                code="insufficient_redis_requirements",
            )

    registry = PluginRegistry([])
    registry._plugins[ServiceKind.REDIS] = MissingSizePlugin()  # type: ignore[assignment]
    service = QuoteService(
        RedisParser(),  # type: ignore[arg-type]
        registry,
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    preview = await service.preview(QuoteRequest(customer_request="Redis 1主1从"))

    assert len(preview.selections) == 1
    assert preview.selections[0].status == "customer_issue"
    assert preview.selections[0].requires_confirmation is True
    assert preview.selections[0].quantity == 1
    assert preview.confirmation_text is not None
    assert "部署在哪个 AWS 区域" in preview.confirmation_text
    assert "每节点大概需要 1G、4G 还是 8G 内存" not in preview.confirmation_text


@pytest.mark.asyncio
async def test_redis_capacity_confirmation_updates_saved_draft_without_looping() -> None:
    question = (
        "您已选 Redis 1 主 1 从，但还缺少单节点容量。"
        "每节点大概需要 1G、4G 还是 8G 内存？型号由系统自动选择。"
    )

    class RedisParser:
        calls = 0

        async def parse(self, _: str) -> ParsedIntent:
            self.calls += 1
            return ParsedIntent(
                customer_summary="Redis 主从",
                services=[
                    ServiceRequirement(
                        service="elasticache",
                        calculator_service_name="Amazon ElastiCache",
                        quantity=1,
                        requirements={
                            "engine": "redis",
                            "shards": 1,
                            "replicas_per_shard": 1,
                        },
                    )
                ],
            )

    class RedisCapacityPlugin(ApiPlugin):
        def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
            if requirement.requirements.get("memory_gib") is None:
                raise ManualConfirmationRequired(
                    "Redis 需求缺少节点型号、内存或 vCPU",
                    code="insufficient_redis_requirements",
                )
            selection = super().preview(requirement, default_region)
            selection.candidates[0].specifications["memoryGiB"] = requirement.requirements[
                "memory_gib"
            ]
            return selection

    parser = RedisParser()
    registry = PluginRegistry([])
    registry._plugins[ServiceKind.REDIS] = RedisCapacityPlugin(  # type: ignore[assignment]
        ServiceKind.REDIS, "cache.t3.small"
    )
    service = QuoteService(
        parser,  # type: ignore[arg-type]
        registry,
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    first = await service.preview(QuoteRequest(customer_request="Redis 1主1从"))
    assert first.confirmation_text is not None

    second = await service.preview(
        QuoteRequest(
            customer_request="Redis 1主1从",
            draft_id=first.draft_id,
            confirmation_responses={question: "1"},
        )
    )

    assert second.confirmation_text is None
    assert second.selections[0].candidates[0].specifications["memoryGiB"] == 1
    assert parser.calls == 1


@pytest.mark.asyncio
async def test_second_question_page_is_replaced_by_cheapest_matching_model() -> None:
    class CachedOnlyParser:
        async def parse(self, _: str) -> ParsedIntent:
            raise AssertionError("saved draft must be reused")

    class FollowUpCatalogPlugin(ApiPlugin):
        def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
            return PreviewSelection(
                component_id="component",
                service=ServiceKind.EC2,
                display_name="EKS Worker",
                region=requirement.region or default_region,
                candidates=[
                    CandidateOption(
                        model="expensive.xlarge",
                        family="test",
                        specifications={"vCPU": 4, "memoryGiB": 16},
                        monthly_catalog_cost=80,
                        rationale="official",
                    ),
                    CandidateOption(
                        model="cheap.xlarge",
                        family="test",
                        specifications={"vCPU": 4, "memoryGiB": 16},
                        monthly_catalog_cost=20,
                        rationale="official",
                    ),
                ],
                requires_confirmation=True,
                confirmation_reason="还没有指定型号",
            )

    draft_id = "onepage00001"
    intent = ParsedIntent(
        customer_summary="EKS Worker",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-northeast-1",
                requirements={"vcpu": 4, "memory_gib": 16},
            )
        ],
    )
    service = QuoteService(
        CachedOnlyParser(),  # type: ignore[arg-type]
        PluginRegistry([FollowUpCatalogPlugin(ServiceKind.EC2, "fallback")]),
        FailingEstimator(),  # type: ignore[arg-type]
    )
    service._drafts[draft_id] = ("EKS Worker", intent)
    service._confirmation_rounds[draft_id] = 1

    preview = await service.preview(QuoteRequest(customer_request="EKS Worker", draft_id=draft_id))

    assert preview.confirmation_items == []
    assert preview.confirmation_text is None
    assert preview.selections[0].selected_model == "cheap.xlarge"
    assert preview.selections[0].selection_reason == "已自动选择满足配置的最低价官方型号"


def test_rephrased_shape_question_uses_the_same_confirmation_key() -> None:
    assert QuoteService._confirmation_question_key(
        "OpenSearch 还没有指定型号，请选择官方型号。"
    ) == QuoteService._confirmation_question_key("请选择 OpenSearch 的处理器、内存和规格。")


def test_rephrased_rds_engine_question_uses_the_same_confirmation_key() -> None:
    first = (
        "Amazon RDS 数据库没有说明数据库类型，请选择 MySQL、PostgreSQL、"
        "MariaDB、SQL Server、Oracle 或 Db2。"
    )
    second = (
        "请先选择 RDS 数据库类型（MySQL、PostgreSQL、MariaDB、SQL Server、"
        "Oracle 或 Db2）。请从下方可用配置中选择，或补充业务规格。"
    )

    assert QuoteService._confirmation_question_key(first) == "rds|engine"
    assert QuoteService._confirmation_question_key(second) == "rds|engine"


def test_rephrased_rds_version_question_uses_the_same_confirmation_key() -> None:
    first = (
        "RDS MySQL：当前 mysql 5.7.44 在 us-east-1 已不再提供维护或订购，"
        "请改用下方仍受支持的数据库版本。可选版本：8.4.11、8.0.46。"
    )
    second = (
        "RDS MySQL：当前 mysql 8.4.11 在 us-east-1 已不再提供维护或订购，"
        "请改用下方仍受支持的数据库版本。可选版本：8.4.10、8.0.46。"
    )

    assert QuoteService._confirmation_question_key(first) == "rds|engine_version"
    assert QuoteService._confirmation_question_key(second) == "rds|engine_version"


def test_duplicate_rds_engine_questions_are_merged_within_one_component() -> None:
    first = "Amazon RDS 数据库没有说明数据库类型，请选择数据库引擎。"
    second = "请先选择 RDS 数据库类型，并从下方可用配置中选择。"
    scope = ("1", "rds")

    assert QuoteService._deduplicate_confirmation_notices(
        [first, second],
        {first: scope, second: scope},
    ) == [first]


def test_rds_engine_questions_for_independent_components_are_not_merged() -> None:
    first = "第一套 RDS 数据库没有说明数据库类型，请选择数据库引擎。"
    second = "第二套 RDS 数据库没有说明数据库类型，请选择数据库引擎。"

    assert QuoteService._deduplicate_confirmation_notices(
        [first, second],
        {first: ("0", "rds"), second: ("1", "rds")},
    ) == [first, second]


def test_missing_redis_capacity_generates_clickable_options() -> None:
    question = (
        "您已选 Redis 1 主 1 从，但还缺少单节点容量。"
        "每节点大概需要 1G、4G 还是 8G 内存？型号由系统自动选择。"
    )

    options = QuoteService._default_confirmation_options(question)

    assert [option.value for option in options] == ["1G", "4G", "8G"]


def test_selection_wording_can_never_fall_back_to_text_entry() -> None:
    question = (
        "RDS MySQL：AWS 在当前区域没有完全一致的规格，"
        "请从下方当前区域支持的配置中重新选择。"
    )

    assert QuoteService._confirmation_selection_mode(question, []) == "buttons"
    assert QuoteService._confirmation_selection_mode("请补充业务用途。", []) == "text"


@pytest.mark.asyncio
async def test_replacement_model_confirmation_targets_matching_duplicate_service() -> None:
    """A choice for the second EC2 card must never mutate the first card."""

    intent = ParsedIntent(
        customer_summary="two EC2-shaped workloads",
        services=[
            ServiceRequirement(
                service="ec2",
                requirements={"vcpu": 4, "memory_gib": 16},
                source_text="应用节点 4核16G",
            ),
            ServiceRequirement(
                service="ec2",
                requirements={"vcpu": 4, "memory_gib": 100},
                source_text="数据库节点 4核100G",
            ),
        ],
    )
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )
    question = "服务器没有 4核100G，选4核64G（偏低），还是4核122G（不低配）？"

    await service._apply_confirmation_responses(intent, {question: "选择 x1e.xlarge"})

    assert intent.services[0].requirements == {"vcpu": 4, "memory_gib": 16}
    assert intent.services[1].requirements == {"requested_model": "x1e.xlarge"}
    assert (
        intent.services[1].field_sources["_customer_shape_replaced_by_model"]
        == "customer_confirmation"
    )
    assert intent.services[1].field_sources["requirements.vcpu"] == (
        "customer_confirmation_removed"
    )
    assert intent.services[1].field_sources["requirements.memory_gib"] == (
        "customer_confirmation_removed"
    )


@pytest.mark.asyncio
async def test_region_and_redis_model_answers_resolve_confirmation_without_loop() -> None:
    region_question = "请确认这些区域型服务部署在哪个 AWS 区域。"
    redis_question = (
        "客户需要 Redis 每节点约 8G；AWS 相邻规格为 cache.m4.large（6.42G，偏低）、"
        "cache.r4.large（12.3G，不低配），请选择。"
    )
    intent = ParsedIntent(
        customer_summary="Redis、MSK 与 S3",
        services=[
            ServiceRequirement(
                service="elasticache",
                requirements={"memory_gib": 8, "shards": 2},
            ),
            ServiceRequirement(service="msk", requirements={"broker_count": 3}),
            ServiceRequirement(service="s3", requirements={"storage_gib": 500}),
        ],
        ambiguities=[region_question, redis_question],
    )
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    await service._apply_confirmation_responses(
        intent,
        {
            region_question: "东京",
            redis_question: "选择 cache.m4.large",
        },
    )

    assert [item.region for item in intent.services] == [
        "ap-northeast-1",
        "ap-northeast-1",
        "ap-northeast-1",
    ]
    assert intent.services[0].requirements == {
        "shards": 2,
        "requested_model": "cache.m4.large",
    }
    assert intent.ambiguities == []


def test_customer_confirmed_model_wins_over_stale_review_model() -> None:
    component = ServiceRequirement(
        service="elasticache",
        requirements={
            "requested_model": "cache.m4.xlarge",
            "_review_selected_model": "cache.r6g.xlarge",
        },
        field_sources={
            "requirements.requested_model": "customer_confirmation",
        },
    )

    assert QuoteService._confirmed_pricing_model(component) == "cache.m4.xlarge"


@pytest.mark.asyncio
async def test_compact_cpu_memory_confirmation_is_applied_without_unit_expansion() -> None:
    intent = ParsedIntent(
        customer_summary="RDS",
        services=[
            ServiceRequirement(
                service="rds",
                requirements={"engine": "mysql"},
                source_text="RDS MySQL",
            )
        ],
    )
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    await service._apply_confirmation_responses(
        intent,
        {"请确认数据库大概需要几核、多少内存；型号由系统自动选择。": "8c16G"},
    )

    assert intent.services[0].requirements["vcpu"] == 8
    assert intent.services[0].requirements["memory_gib"] == 16


@pytest.mark.asyncio
async def test_confirmed_preview_draft_reuses_intent_without_second_ai_call() -> None:
    parser = MixedParser()
    parser.calls = 0  # type: ignore[attr-defined]
    original_parse = parser.parse

    async def counted_parse(text: str) -> ParsedIntent:
        parser.calls += 1  # type: ignore[attr-defined]
        return await original_parse(text)

    parser.parse = counted_parse  # type: ignore[method-assign]
    service = QuoteService(
        parser,  # type: ignore[arg-type]
        api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
    )
    request = QuoteRequest(customer_request="混合报价")

    preview = await service.preview(request)
    quote = await service.create_quote(
        QuoteRequest(customer_request="混合报价", draft_id=preview.draft_id)
    )

    assert quote.total_cost == 300
    assert parser.calls == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_windows_arm_confirmation_updates_saved_draft_without_looping() -> None:
    question = (
        "您指定 c7g.xlarge 并要求 Windows Server；该型号是 ARM 架构，不支持 Windows。"
        "请确认：改用 Linux 保留 c7g.xlarge，还是保留 Windows 并改选同规格 x86 型号"
        "（例如 c7i.xlarge）？"
    )

    class ArmParser:
        calls = 0

        async def parse(self, _: str) -> ParsedIntent:
            self.calls += 1
            return ParsedIntent(
                customer_summary="Windows EC2",
                services=[
                    ServiceRequirement(
                        service="ec2",
                        region="ap-northeast-1",
                        requirements={
                            "requested_model": "c7g.xlarge",
                            "operating_system": "Windows",
                        },
                    )
                ],
            )

    class ArmPlugin(ApiPlugin):
        def specified_model_compatibility_notice(
            self, requirement: ServiceRequirement, _: str
        ) -> str | None:
            return (
                question
                if requirement.requirements.get("requested_model") == "c7g.xlarge"
                and requirement.requirements.get("operating_system") == "Windows"
                else None
            )

        def compatible_x86_model(self, *_: object) -> str:
            return "c7i.xlarge"

        def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
            self.model = str(requirement.requirements["requested_model"])
            return super().preview(requirement, default_region)

    parser = ArmParser()
    registry = PluginRegistry([ArmPlugin(ServiceKind.EC2, "c7g.xlarge")])
    service = QuoteService(
        parser,  # type: ignore[arg-type]
        registry,
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    first = await service.preview(QuoteRequest(customer_request="Windows c7g.xlarge"))
    assert first.confirmation_text is not None

    second = await service.preview(
        QuoteRequest(
            customer_request="Windows c7g.xlarge",
            draft_id=first.draft_id,
            confirmation_responses={question: "保留 Windows 并改选同规格 x86 型号"},
        )
    )

    assert second.confirmation_text is None
    assert second.selections[0].selected_model == "c7i.xlarge"
    assert parser.calls == 1


@pytest.mark.asyncio
async def test_shape_only_requirement_does_not_trigger_legacy_nearest_shape_question() -> None:
    class ShapeParser:
        async def parse(self, _: str) -> ParsedIntent:
            return ParsedIntent(
                customer_summary="非标准 EC2 规格",
                services=[
                    ServiceRequirement(
                        service="ec2",
                        region="ap-southeast-1",
                        requirements={"vcpu": 2, "memory_gib": 45},
                    )
                ],
            )

    class ShapePlugin(ApiPlugin):
        def nearest_shape_options(self, *_: object) -> list[dict[str, object]]:
            return [
                {
                    "label": "较低配置（可能低于业务需求）",
                    "vcpu": 2.0,
                    "memory_gib": 32.0,
                    "example_model": "r7i.xlarge",
                },
                {
                    "label": "较高配置（不低于客户需求）",
                    "vcpu": 4.0,
                    "memory_gib": 64.0,
                    "example_model": "r7i.2xlarge",
                },
            ]

        def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
            return PreviewSelection(
                component_id="component",
                service="ec2",
                display_name="Amazon EC2",
                region=requirement.region or default_region,
                selected_model="m6g.4xlarge",
                selection_reason="fallback",
                candidates=[
                    CandidateOption(
                        model="m6g.4xlarge",
                        family="general_purpose",
                        specifications={"vCPU": 16, "memoryGiB": 64},
                        rationale="unrelated broad candidate",
                        is_default=True,
                    )
                ],
                requires_confirmation=False,
                confirmation_reason=None,
            )

    registry = PluginRegistry([ShapePlugin(ServiceKind.EC2, "m6g.4xlarge")])
    service = QuoteService(
        ShapeParser(),  # type: ignore[arg-type]
        registry,
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    preview = await service.preview(QuoteRequest(customer_request="EC2 2核45G"))

    assert preview.notices == []
    assert preview.confirmation_items == []


def test_compact_candidate_options_exposes_the_full_supported_catalog() -> None:
    requirement = ServiceRequirement(
        service="rds",
        requirements={"vcpu": 3, "memory_gib": 12},
    )
    candidates = [
        CandidateOption(
            model="db.large-a",
            family="db",
            specifications={"vCPU": 2, "memoryGiB": 8},
            rationale="lower",
        ),
        CandidateOption(
            model="db.large-b",
            family="db",
            specifications={"vCPU": 2, "memoryGiB": 10},
            rationale="closer lower",
        ),
        CandidateOption(
            model="db.xlarge-a",
            family="db",
            specifications={"vCPU": 4, "memoryGiB": 16},
            rationale="upper",
        ),
        CandidateOption(
            model="db.2xlarge",
            family="db",
            specifications={"vCPU": 8, "memoryGiB": 32},
            rationale="far upper",
        ),
    ]

    options = QuoteService._compact_candidate_options(candidates, requirement)

    assert len(options) == 4
    assert options[0].value == "选择 db.large-b"
    assert {option.value for option in options} == {
        "选择 db.large-a",
        "选择 db.large-b",
        "选择 db.xlarge-a",
        "选择 db.2xlarge",
    }
    assert all(option.model for option in options)
    assert options[0].specifications == {"vCPU": 2, "memoryGiB": 10}


def test_official_error_candidates_are_available_to_global_confirmation_flow() -> None:
    error = ManualConfirmationRequired(
        "客户指定的型号在目标区域不存在",
        code="official_model_not_found",
        nearby_candidates=[
            {
                "model": "mq.m5.large",
                "vcpu": 2,
                "memory_gib": 8,
                "rationale": "official lower",
            },
            {
                "model": "mq.m5.xlarge",
                "specifications": {"vCPU": 4, "memoryGiB": 16},
                "official_product": {"source": "AWS Price List"},
            },
            {"model": "mq.m5.large", "vcpu": 2, "memory_gib": 8},
        ],
    )

    candidates = QuoteService._candidate_options_from_error(error)

    assert [candidate.model for candidate in candidates] == [
        "mq.m5.large",
        "mq.m5.xlarge",
    ]
    assert candidates[0].specifications == {"vCPU": 2, "memoryGiB": 8}
    assert candidates[1].official_product == {"source": "AWS Price List"}


@pytest.mark.asyncio
async def test_bcm_flow_does_not_require_browser_calculator() -> None:
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
    )
    quote = await service.create_quote(QuoteRequest(customer_request="混合报价"))
    assert quote.status.value == "quoted"


def test_per_instance_transfer_is_converted_to_calculator_total() -> None:
    normalized = QuoteService._calculator_requirements(
        {"data_transfer_out_gib_per_instance": 1024}, 3
    )

    assert normalized["data_transfer_out_gib"] == 3072
    assert "data_transfer_out_gib_per_instance" not in normalized


def test_transfer_only_ec2_item_is_merged_into_single_compute_workload() -> None:
    intent = ParsedIntent(
        customer_summary="EC2 和公网流量",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                quantity=3,
                requirements={"vcpu": 8, "memory_gib": 32, "system_disk_gib": 200},
                source_text="应用层：新加坡区域，3 台 Linux，8 核 32G",
            ),
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                requirements={"data_transfer_out_gib": 1024},
                source_text="公网流量：应用服务器额外约 1TB/月",
            ),
        ],
    )

    merged = QuoteService._merge_transfer_only_ec2_services(intent)

    assert merged == 1
    assert len(intent.services) == 1
    assert intent.services[0].requirements["data_transfer_out_gib"] == 1024
    assert "公网流量" in intent.services[0].source_text


def test_transfer_only_ec2_item_is_not_guessed_across_multiple_compute_groups() -> None:
    intent = ParsedIntent(
        customer_summary="两个区域的 EC2 和未指定归属的流量",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-southeast-1",
                requirements={"vcpu": 4, "memory_gib": 16},
            ),
            ServiceRequirement(
                service="ec2",
                region="ap-northeast-1",
                requirements={"vcpu": 8, "memory_gib": 32},
            ),
            ServiceRequirement(
                service="ec2",
                requirements={"data_transfer_out_gib": 1024},
            ),
        ],
    )

    assert QuoteService._merge_transfer_only_ec2_services(intent) == 0
    assert len(intent.services) == 3


@pytest.mark.parametrize(
    "service_name",
    [
        "elb",
        "elbv2",
        "application_load_balancer",
        "elastic_load_balancing",
        "elasticloadbalancing",
    ],
)
def test_alb_service_aliases_use_the_existing_bcm_adapter(service_name: str) -> None:
    assert QuoteService._service_kind(service_name) == ServiceKind.ELB


@pytest.mark.parametrize("service_name", ["waf", "wafv2", "aws_waf", "awswafv2"])
def test_waf_service_aliases_use_the_existing_bcm_adapter(service_name: str) -> None:
    assert QuoteService._service_kind(service_name) == ServiceKind.WAF


@pytest.mark.parametrize(
    ("service_name", "expected"),
    [
        ("cloud_front", ServiceKind.CLOUDFRONT),
        ("opensearch", ServiceKind.OPENSEARCH),
        ("amazon_opensearch", ServiceKind.OPENSEARCH),
        ("Amazon OpenSearch Service", ServiceKind.OPENSEARCH),
        ("nat_gateway", ServiceKind.NAT_GATEWAY),
        ("aws_nat_gateway", ServiceKind.NAT_GATEWAY),
        ("AWS NAT Gateway", ServiceKind.NAT_GATEWAY),
    ],
)
def test_search_and_nat_aliases_reach_registered_adapters(
    service_name: str, expected: ServiceKind
) -> None:
    assert QuoteService._service_kind(service_name) == expected


def test_customer_confirmation_questions_are_short_and_colloquial() -> None:
    assert (
        QuoteService._compact_customer_question("Single-AZ 与主备自动故障切换要求冲突")
        == "您原选 Single-AZ，但它不提供主备自动切换；要自动切换需改为 Multi-AZ，是否同意？"
    )
    assert (
        QuoteService._compact_customer_question("Application Load Balancer 不支持固定公网 IP")
        == "您要求 ALB 使用固定公网 IP，但 ALB 的 IP 会变化；是否改用支持固定 IP 的 NLB 或 Global Accelerator？"
    )
    assert (
        QuoteService._compact_customer_question("Redis 整套 1G 与每个节点至少 8G 的要求冲突")
        == "您原填写 Redis 整套 1G、每节点 8G，两者不一致；请确认以哪个为准？"
    )
    assert QuoteService._compact_customer_question(
        "客户需要 Redis 每节点约 8G；AWS 相邻规格为"
        "cache.m4.large（6.42G，偏低）、cache.r4.large（12.3G，不低配），请选择。"
    ) == (
        "客户需要 Redis 每节点约 8G；AWS 相邻规格为"
        "cache.m4.large（6.42G，偏低）、cache.r4.large（12.3G，不低配），请选择？"
    )


def test_customer_question_is_never_truncated_and_prefix_duplicate_is_removed() -> None:
    full = (
        "Amazon RDS for PostgreSQL（客户原话：PostgreSQL 配置4核16G、存储800GB）"
        "未说明部署方式，请选择单可用区，还是主备高可用（Multi-AZ）。"
    )
    truncated = "Amazon RDS for PostgreSQL（客户原话：PostgreSQL 配置4核16G…？"

    assert QuoteService._compact_customer_question(full) == full.rstrip("。") + "？"
    assert QuoteService._deduplicate_confirmation_notices([full, truncated]) == [full]


def test_component_region_question_is_not_rewritten_as_shared_region_question() -> None:
    question = (
        "Amazon S3 是区域型服务，不能使用“全球”作为报价区域，请确认该组件实际部署在哪个 AWS 区域。"
    )

    assert QuoteService._deduplicate_confirmation_notices([question]) == [question]


def test_prefix_questions_from_different_components_are_not_deduplicated() -> None:
    first = "请选择当前区域支持的处理器和内存配置。"
    second = "请选择当前区域支持的处理器和内存配置，并选择最终型号。"

    assert QuoteService._deduplicate_confirmation_notices(
        [first, second],
        {
            first: ("component-a", "Amazon EC2"),
            second: ("component-b", "Amazon RDS"),
        },
    ) == [first, second]


def test_aws_discovery_failures_are_internal_not_customer_questions() -> None:
    error = ManualConfirmationRequired(
        "EC2 官方 API 无法确认区域",
        code="ec2_discovery_failed",
    )

    assert QuoteService._is_technical_catalog_error(error)


@pytest.mark.parametrize(
    "code",
    [
        "unsupported_or_unknown_region",
        "pricing_catalog_unavailable",
        "pricing_attribute_values_unavailable",
        "reference_unit_rate_not_found",
        "bcm_service_adapter_not_ready",
        "bcm_estimate_create_failed",
        "bcm_invalid_response",
        "bcm_ownership_check_failed",
        "bcm_usage_read_failed",
        "bcm_pool_cleanup_failed",
        "bcm_estimate_cleanup_failed",
        "bcm_usage_create_failed",
        "bcm_usage_rejected",
        "bcm_result_read_failed",
        "bcm_estimate_invalid",
        "bcm_estimate_timeout",
        "bcm_incomplete_line_result",
        "bcm_incomplete_result",
        "too_many_usage_lines",
        "unparseable_official_specification",
        "unsupported_service",
        "reserved_term_not_found",
        "reserved_price_dimensions_missing",
        "pricing_scenarios_unavailable",
    ],
)
def test_catalog_failures_never_become_customer_questions(code: str) -> None:
    error = ManualConfirmationRequired("AWS internal catalog failure", code=code)

    assert QuoteService._is_technical_catalog_error(error)


def test_global_waf_uses_edge_catalog_without_region_lookup() -> None:
    def product(group: str, usage_type: str, *, priced: bool = False) -> dict[str, object]:
        value: dict[str, object] = {
            "serviceCode": "awswaf",
            "product": {
                "sku": usage_type,
                "attributes": {
                    "location": "Any",
                    "locationType": "AWS Edge Location",
                    "regionCode": "",
                    "group": group,
                    "usagetype": usage_type,
                    "operation": "",
                },
            },
        }
        if priced:
            value["terms"] = {
                "OnDemand": {
                    "term": {
                        "priceDimensions": {
                            "dimension": {
                                "beginRange": "0",
                                "unit": "Requests",
                                "pricePerUnit": {"USD": "0.0000006"},
                            }
                        }
                    }
                }
            }
        return value

    class GlobalWafCatalog:
        def location(self, region: str) -> str:
            raise AssertionError(f"global WAF must not query a regional location: {region}")

        def products(
            self, service_code: str, filters: dict[str, str], *, max_pages: int
        ) -> list[dict[str, object]]:
            assert service_code == "awswaf"
            assert filters["location"] == "Any"
            group = filters["group"]
            return {
                "Web ACL": [product(group, "Global-WebACLV2")],
                "Rule": [product(group, "Global-RuleV2")],
                "Request": [product(group, "Global-RequestV2-Tier0", priced=True)],
            }[group]

        @staticmethod
        def attributes(value: dict[str, object]) -> dict[str, str]:
            return value["product"]["attributes"]  # type: ignore[index,return-value]

    plugin = WafPlugin(None, GlobalWafCatalog())  # type: ignore[arg-type]
    selection = plugin.select(
        ServiceRequirement(service="waf", region="global", requirements={}),
        "ap-southeast-1",
    )

    assert selection.region == "Global"
    assert [line.usage_type for line in selection.usage_lines] == [
        "Global-WebACLV2",
        "Global-RuleV2",
    ]
    assert selection.reference_rates[0].usage_type == "Global-RequestV2-Tier0"


def test_waf_prices_per_acl_dimensions_using_the_acl_count() -> None:
    def product(group: str, usage_type: str) -> dict[str, object]:
        return {
            "serviceCode": "awswaf",
            "product": {
                "sku": usage_type,
                "attributes": {
                    "location": "Any",
                    "locationType": "AWS Edge Location",
                    "regionCode": "",
                    "group": group,
                    "usagetype": usage_type,
                    "operation": "",
                },
            },
        }

    class GlobalWafCatalog:
        def products(
            self,
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int,
            refresh: bool = False,
        ) -> list[dict[str, object]]:
            assert service_code == "awswaf"
            group = filters["group"]
            usage_type = {
                "Web ACL": "Global-WebACLV2",
                "Rule": "Global-RuleV2",
                "Request": "Global-RequestV2-Tier0",
            }[group]
            return [product(group, usage_type)]

        @staticmethod
        def attributes(value: dict[str, object]) -> dict[str, str]:
            return value["product"]["attributes"]  # type: ignore[index,return-value]

    requirement = ServiceRequirement(
        service="waf",
        region="global",
        quantity=2,
        requirements={"web_acls": 2, "rules": 12, "requests": 60_000_000},
        field_scopes={"rules": "per_resource", "requests": "per_resource"},
    )
    selection = WafPlugin(None, GlobalWafCatalog()).select(  # type: ignore[arg-type]
        requirement,
        "ap-southeast-1",
    )

    assert [line.amount for line in selection.usage_lines] == [2, 24, 120_000_000]
    assert selection.architecture == "2 个 Web ACL · 每个 12 条规则"
    assert selection.specifications == {
        "webACLs": 2,
        "rules": 24,
        "rulesPerWebACL": 12,
        "requests": 120_000_000,
        "requestsPerWebACL": 60_000_000,
    }


def test_optional_omissions_do_not_create_customer_questions() -> None:
    intent = ParsedIntent(
        customer_summary="defaults",
        services=[ServiceRequirement(service="ec2", requirements={"vcpu": 2, "memory_gib": 4})],
        ambiguities=[
            "Redis 未指定引擎版本，默认按 6.x 处理",
            "RDS 未指定 IOPS 和吞吐量，默认采用 gp3 最小值",
            "S3 未指定对象数量，仅按存储容量计费",
            "应用服务器未指定是否开启详细监控，默认关闭",
            "EC2 单可用区部署与跨可用区自动切换要求冲突",
        ],
    )

    assert QuoteService._confirmation_notices(intent) == [
        "EC2 单可用区部署与跨可用区自动切换要求冲突"
    ]


def test_missing_compute_operating_system_and_catalog_errors_are_not_customer_questions() -> None:
    intent = ParsedIntent(
        customer_summary="EKS and MSK",
        services=[ServiceRequirement(service="eks"), ServiceRequirement(service="msk")],
        ambiguities=[
            "EKS 节点未指定操作系统，请确认节点操作系统。",
            "Amazon MSK 暂时无法确定配置：AWS 官方目录没有返回相符的计费项。",
        ],
    )

    assert QuoteService._confirmation_notices(intent) == []


def test_ec2_without_operating_system_defaults_to_linux() -> None:
    intent = ParsedIntent(
        customer_summary="EC2",
        services=[ServiceRequirement(service="ec2", requirements={"vcpu": 4, "memory_gib": 16})],
    )

    QuoteService._apply_calculator_minimum_defaults(intent)

    assert intent.services[0].requirements["operating_system"] == "Linux"


def test_confirmation_keeps_all_business_questions_but_hides_technical_errors() -> None:
    intent = ParsedIntent(
        customer_summary="multiple decisions",
        services=[ServiceRequirement(service="apigateway")],
        ambiguities=[
            "请确认部署区域。",
            "API Gateway 的 5120MB 是指单个请求体大小限制，还是每月总流量？",
            "AWS 官方规格接口未返回结果。",
            "Redis 未指定引擎版本，默认按 6.x 处理。",
        ],
    )

    assert QuoteService._confirmation_notices(intent) == [
        "请确认这些区域型服务部署在哪个 AWS 区域；如各服务区域不同，请分别说明。",
        "API Gateway 的 5120MB 是指单个请求体大小限制，还是每月总流量？",
    ]


def test_missing_region_questions_are_merged_by_meaning() -> None:
    intent = ParsedIntent(
        customer_summary="one workload without a region",
        services=[
            ServiceRequirement(service="msk"),
            ServiceRequirement(service="apigateway"),
            ServiceRequirement(service="scheduler"),
            ServiceRequirement(service="s3"),
        ],
        ambiguities=[
            "请确认部署区域，以便提供准确的区域型服务报价。",
            "请确认部署区域。",
            "请确认部署区域，以便提供准确的区域型服务报价？",
            "请确认部署区域？",
            "API Gateway 的 5120MB 是指数据处理流量还是请求体大小？",
        ],
    )

    assert QuoteService._confirmation_notices(intent) == [
        "请确认这些区域型服务部署在哪个 AWS 区域；如各服务区域不同，请分别说明。",
        "API Gateway 的 5120MB 是指数据处理流量还是请求体大小？",
    ]


def test_resolved_region_and_optional_opensearch_roles_do_not_ask_customer() -> None:
    intent = ParsedIntent(
        customer_summary="新加坡 OpenSearch",
        services=[
            ServiceRequirement(service="ec2", region="ap-southeast-1"),
            ServiceRequirement(service="opensearch", region="ap-southeast-1"),
        ],
        ambiguities=[
            "请确认这些区域型服务部署在哪个 AWS 区域。",
            (
                "OpenSearch 3节点架构：未明确是3个独立节点还是包含Master、Data、"
                "Coordinating角色的集群，请确认。"
            ),
            "OpenSearch 3节点架构：未明确是3个独立节点还是包含Master、Data、Coor…？",
        ],
    )

    assert QuoteService._confirmation_notices(intent) == []


def test_late_global_region_question_is_removed_when_components_have_regions() -> None:
    question = "请确认这些区域型服务部署在哪个 AWS 区域；如各服务区域不同，请分别说明。"
    intent = ParsedIntent(
        customer_summary="全部部署在新加坡",
        services=[
            ServiceRequirement(service="ec2", region="ap-southeast-1"),
            ServiceRequirement(service="rds", region="ap-southeast-1"),
            ServiceRequirement(service="cloudfront", region="global"),
        ],
    )

    assert QuoteService._drop_resolved_region_questions(intent, [question]) == []


@pytest.mark.parametrize(
    "question",
    [
        (
            "您之前要求【Amazon RDS MySQL】：区域：新加坡，部署方式：Multi-AZ。"
            "AWS 可订购的数据库规格与客户要求不是完全匹配，请确认推荐配置。"
        ),
        (
            "您之前要求【Amazon OpenSearch Service】：区域：新加坡。"
            "请选择节点型号；列表仅展示当前部署区域可用的官方型号。"
        ),
    ],
)
def test_component_configuration_question_is_not_misclassified_as_region(
    question: str,
) -> None:
    assert QuoteService._is_region_confirmation_notice(question) is False


def test_every_confirmation_card_gets_a_component_question() -> None:
    intent = ParsedIntent(
        customer_summary="RDS 和 Redis",
        services=[
            ServiceRequirement(service="rds", region="ap-southeast-1"),
            ServiceRequirement(service="elasticache", region="ap-southeast-1"),
        ],
    )
    redis_question = "Redis 需要选择官方规格。"
    rds_question = "RDS 需要选择官方规格。"
    selections = [
        PreviewSelection(
            component_id="0",
            service="rds",
            display_name="Amazon RDS",
            region="ap-southeast-1",
            requires_confirmation=True,
            confirmation_reason=rds_question,
        ),
        PreviewSelection(
            component_id="1",
            service="elasticache",
            display_name="Amazon ElastiCache",
            region="ap-southeast-1",
            requires_confirmation=True,
            confirmation_reason=redis_question,
        ),
    ]
    components = {redis_question: ("1", "elasticache")}
    options = {}

    notices = QuoteService._ensure_selection_confirmation_notices(
        intent, selections, [redis_question], components, options
    )

    assert notices == [redis_question, rds_question]
    assert components[rds_question] == ("0", "rds")


@pytest.mark.parametrize(
    ("selection", "requirement", "expected"),
    [
        (
            PreviewSelection(
                component_id="0",
                service="rds",
                display_name="Amazon RDS MySQL",
                region="ap-southeast-1",
            ),
            ServiceRequirement(
                service="rds",
                region="ap-southeast-1",
                requirements={"vcpu": 10, "memory_gib": 40},
            ),
            (
                "您填写的 RDS MySQL 是 10 核、40 GB，但没有完全一样的型号。"
                "请从下面选择一个合适的配置。"
            ),
        ),
        (
            PreviewSelection(
                component_id="3",
                service="dms",
                display_name="AWS DMS",
                region="us-east-1",
            ),
            ServiceRequirement(
                service="dms",
                region="us-east-1",
                source_text="将 PostgreSQL 和 MongoDB 迁入 AWS",
                requirements={"vcpu": 4, "memory_gib": 16},
            ),
            (
                "您填写的 AWS DMS 是 4 核、16 GB，但没有完全一样的型号。"
                "请从下面选择一个合适的配置。"
            ),
        ),
        (
            PreviewSelection(
                component_id="1",
                service="ec2",
                display_name="Amazon EC2 (EKS Worker Nodes)",
                region="ap-southeast-1",
            ),
            ServiceRequirement(service="ec2", region="ap-southeast-1"),
            "EKS 工作节点还没写需要几核、多少内存。请在下面选择。",
        ),
        (
            PreviewSelection(
                component_id="2",
                service="future_database",
                display_name="Amazon Neptune",
                region="ap-southeast-1",
                requested_model="db.example.large",
            ),
            ServiceRequirement(
                service="future_database",
                region="ap-southeast-1",
                requirements={"requested_model": "db.example.large"},
            ),
            (
                "您填写的 Neptune 型号 db.example.large 在这个地区不能使用。"
                "请从下面选择一个可用型号。"
            ),
        ),
        (
            PreviewSelection(
                component_id="3",
                service="future_database",
                display_name="Amazon Neptune",
                region="ap-east-1",
                requested_model="db.r6g.large",
            ),
            ServiceRequirement(
                service="future_database",
                region="ap-east-1",
                requirements={
                    "requested_model": "db.r6g.large",
                    "vcpu": 8,
                    "memory_gib": 32,
                },
            ),
            (
                "您同时填写了 Neptune 型号 db.r6g.large 和 8 核、32 GB 内存，"
                "但这两个配置对不上。请在下面确认要用哪一个。"
            ),
        ),
    ],
)
def test_model_questions_use_plain_customer_language(
    selection: PreviewSelection,
    requirement: ServiceRequirement,
    expected: str,
) -> None:
    question = QuoteService._plain_model_selection_question(selection, requirement)

    assert question == expected
    assert "您之前要求" not in question
    assert "推荐规格与客户原始要求不是完全匹配" not in question


def test_every_component_question_uses_the_same_customer_language_boundary() -> None:
    requirement = ServiceRequirement(
        service="future_queue",
        source_text="一整段很长的客户原始需求，不应重复显示在问题页面。",
    )

    question = QuoteService._customer_confirmation_question(
        "Amazon MQ",
        requirement,
        "还缺少 Broker 数量，请填写需要几个节点。",
    )

    assert question == "MQ：还缺少 Broker 数量，请填写需要几个节点。"
    assert requirement.source_text not in question


def test_component_region_variants_and_optional_product_choices_do_not_repeat() -> None:
    intent = ParsedIntent(
        customer_summary="workload defaults",
        services=[ServiceRequirement(service="elasticache")],
        ambiguities=[
            "请确认 ElastiCache Redis 的部署区域，以便选择正确的可用区。",
            "请确认 MSK 集群的部署区域。",
            "请确认 S3 存储桶的部署区域。",
            "请确认这些区域型服务部署在哪个 AWS 区域。",
            "请确认 ElastiCache Redis 的版本（如 6.x 或 7.x）及是否需要开启集群模式。",
            "请确认 MSK 集群的类型（Standard 或 Serverless）。",
            "请确认 MSK 集群的存储类型（EBS 或 Tiered Storage）。",
            "请确认 API Gateway 的类型（REST API、HTTP API 或 WebSocket API）。",
        ],
    )

    assert QuoteService._confirmation_notices(intent) == [
        "请确认这些区域型服务部署在哪个 AWS 区域；如各服务区域不同，请分别说明。"
    ]


def test_product_defaults_use_lowest_standard_billing_paths() -> None:
    intent = ParsedIntent(
        customer_summary="defaults",
        services=[
            ServiceRequirement(service="elasticache", requirements={"shards": 2}),
            ServiceRequirement(service="msk", requirements={}),
            ServiceRequirement(service="apigateway", requirements={}),
        ],
    )

    QuoteService._apply_calculator_minimum_defaults(intent)

    assert intent.services[0].requirements["cluster_mode"] is True
    assert intent.services[0].requirements["backup_retention_days"] == 0
    assert intent.services[1].requirements == {
        "broker_count": 2,
        "cluster_type": "provisioned",
        "storage_type": "ebs",
    }
    assert intent.services[2].requirements["api_type"] == "http"


@pytest.mark.asyncio
async def test_vague_value_answers_update_draft_before_configuration_review() -> None:
    intent = ParsedIntent(
        customer_summary="vague",
        services=[
            ServiceRequirement(service="ec2", requirements={"vcpu": 4, "memory_gib": 16}),
            ServiceRequirement(service="elasticache"),
            ServiceRequirement(service="s3"),
            ServiceRequirement(service="msk"),
        ],
    )
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry(),
        FailingEstimator(),  # type: ignore[arg-type]
    )

    await service._apply_confirmation_responses(
        intent,
        {
            "Amazon EC2（客户原话：后台服务4核16G，两三台）的数量写的是“两三台”，请确认具体数量。": "2台",
            "Amazon ElastiCache Redis 的容量不是明确数值，请确认每个节点需要多少 GiB 内存。": "16GiB",
            "Amazon S3 的存储容量不是明确数值，请确认预计存储多少 GiB 或 TiB。": "40TB",
            "Amazon MSK 的 Broker 数量不是明确数值，请确认具体需要几个 Broker 节点。": "3",
        },
    )

    assert intent.services[0].quantity == 2
    assert intent.services[1].requirements["memory_gib"] == 16
    assert intent.services[2].requirements["storage_gib"] == 40 * 1024
    assert intent.services[3].requirements["broker_count"] == 3


def test_minimum_defaults_are_applied_only_to_requested_services() -> None:
    intent = ParsedIntent(
        customer_summary="minimum",
        services=[
            ServiceRequirement(service="cloudfront", requirements={}),
            ServiceRequirement(service="s3", requirements={}),
            ServiceRequirement(service="elb", requirements={}),
            ServiceRequirement(service="elasticache", requirements={"memory_gib": 8}),
        ],
    )

    notices = QuoteService._apply_calculator_minimum_defaults(intent)

    assert intent.services[0].requirements["reference_unit_only"] is True
    assert "data_transfer_out_gib" not in intent.services[0].requirements
    assert intent.services[1].requirements["reference_unit_only"] is True
    assert "storage_gib" not in intent.services[1].requirements
    assert intent.services[2].requirements["reference_lcu_unit_only"] is True
    assert "processed_bytes_ec2_ip_gib_per_hour" not in intent.services[2].requirements
    assert intent.services[3].requirements["backup_retention_days"] == 0
    assert len(notices) == 3


def test_rds_retention_days_are_not_sent_as_calculator_cost_input() -> None:
    normalized = QuoteService._calculator_requirements(
        {"storage_gib": 500, "backup_retention_days": 7}, 1, "rds"
    )

    assert normalized["storage_gib"] == 500
    assert "backup_retention_days" not in normalized


def test_same_type_ec2_volumes_are_aggregated_for_calculator_storage_field() -> None:
    normalized = QuoteService._calculator_requirements(
        {
            "system_disk_gib": 100,
            "volume_type": "gp3",
            "additional_ebs_volumes": [
                {"size_gib": 300, "volume_type": "gp3", "count_per_instance": 1}
            ],
        },
        4,
        "ec2",
    )

    assert normalized["system_disk_gib"] == 400
    assert "additional_ebs_volumes" not in normalized
    assert "系统盘 100 GiB" in normalized["ebs_storage_breakdown"]


def test_legacy_ec2_disk_alias_is_preserved_for_calculator() -> None:
    normalized = QuoteService._calculator_requirements(
        {"system_disk_size_gib": 100, "volume_type": "gp3"}, 2, "ec2"
    )

    assert normalized["system_disk_gib"] == 100
    assert "system_disk_size_gib" not in normalized


def test_sales_pricing_choice_overrides_purchase_words_from_customer() -> None:
    intent = ParsedIntent(
        customer_summary="客户原文说预留，但销售选择按需",
        services=[
            ServiceRequirement(
                service="ec2",
                requirements={
                    "requested_model": "m7i.xlarge",
                    "purchase_option": "standard_reserved",
                    "reserved_term_years": 3,
                    "payment_option": "all_upfront",
                },
            ),
            ServiceRequirement(
                service="rds",
                requirements={"purchase_option": "reserved", "reserved_term_years": 3},
            ),
            ServiceRequirement(
                service="s3",
                requirements={"purchase_option": "reserved", "storage_gib": 100},
            ),
        ],
    )

    QuoteService._apply_sales_pricing_choice(
        intent,
        QuoteRequest(customer_request="客户要求三年全预付", pricing_mode="on_demand"),
    )

    assert intent.services[0].requirements["purchase_option"] == "on_demand"
    assert intent.services[0].requirements["utilization_percent"] == 100
    assert "reserved_term_years" not in intent.services[0].requirements
    assert intent.services[1].requirements["purchase_option"] == "on_demand"
    assert intent.services[2].requirements["purchase_option"] == "on_demand"


def test_explicit_customer_purchase_fields_take_priority_over_sales_default() -> None:
    intent = ParsedIntent(
        customer_summary="客户明确要求一年全预付",
        services=[
            ServiceRequirement(
                service="ec2",
                requirements={
                    "purchase_option": "standard_reserved",
                    "reserved_term_years": 1,
                    "payment_option": "all_upfront",
                },
                field_sources={
                    "requirements.purchase_option": "customer_text",
                    "requirements.reserved_term_years": "customer_text",
                    "requirements.payment_option": "customer_text",
                },
            )
        ],
    )

    QuoteService._apply_sales_pricing_choice(
        intent,
        QuoteRequest(customer_request="一年全预付", pricing_mode="on_demand"),
    )

    assert intent.services[0].requirements == {
        "purchase_option": "standard_reserved",
        "reserved_term_years": 1,
        "payment_option": "all_upfront",
    }


def test_sales_reserved_choice_maps_service_specific_purchase_modes() -> None:
    intent = ParsedIntent(
        customer_summary="销售选择预留实例",
        services=[
            ServiceRequirement(service="ec2", requirements={"purchase_option": "on_demand"}),
            ServiceRequirement(service="rds", requirements={"purchase_option": "on_demand"}),
            ServiceRequirement(service="redis", requirements={"purchase_option": "on_demand"}),
            ServiceRequirement(service="memorydb", requirements={"purchase_option": "on_demand"}),
        ],
    )
    request = QuoteRequest(
        customer_request="客户原文写按需",
        pricing_mode="standard_reserved",
        reserved_term_years=3,
        payment_option="all_upfront",
    )

    QuoteService._apply_sales_pricing_choice(intent, request)

    assert intent.services[0].requirements["purchase_option"] == "standard_reserved"
    assert intent.services[1].requirements["purchase_option"] == "reserved"
    assert intent.services[2].requirements["purchase_option"] == "reserved"
    assert intent.services[3].requirements["purchase_option"] == "reserved"
    for service in intent.services:
        assert service.requirements["reserved_term_years"] == 3
        assert service.requirements["payment_option"] == "all_upfront"


def test_final_component_purchase_correction_survives_sales_default_reapplication() -> None:
    intent = ParsedIntent(
        customer_summary="销售初始选择按需，客户在最终配置页修改第二项",
        services=[
            ServiceRequirement(
                service="ec2",
                requirements={"purchase_option": "on_demand"},
            ),
            ServiceRequirement(
                service="ec2",
                requirements={
                    "purchase_option": "standard_reserved",
                    "reserved_term_years": 1,
                    "payment_option": "all_upfront",
                },
                field_sources={
                    "requirements.purchase_option": "customer_confirmation",
                    "requirements.reserved_term_years": "customer_confirmation",
                    "requirements.payment_option": "customer_confirmation",
                },
                locked_fields=[
                    "requirements.purchase_option",
                    "requirements.reserved_term_years",
                    "requirements.payment_option",
                ],
            ),
        ],
    )

    QuoteService._apply_sales_pricing_choice(
        intent,
        QuoteRequest(customer_request="两项 EC2 报价", pricing_mode="on_demand"),
    )

    assert intent.services[0].requirements["purchase_option"] == "on_demand"
    assert intent.services[1].requirements == {
        "purchase_option": "standard_reserved",
        "reserved_term_years": 1,
        "payment_option": "all_upfront",
    }


def test_transfer_only_source_drops_inherited_ai_shape_before_merge() -> None:
    intent = ParsedIntent(
        customer_summary="EC2 与额外公网流量",
        services=[
            ServiceRequirement(
                service="ec2",
                region="ap-northeast-1",
                quantity=3,
                source_text="应用服务器 3 台 Linux，每台 8 核 32G。",
                requirements={"requested_model": "m7i.xlarge"},
            ),
            ServiceRequirement(
                service="ec2",
                region="ap-northeast-1",
                source_text="公网流量：应用服务器额外约 1TB/月。",
                requirements={
                    "vcpu": 8,
                    "memory_gib": 32,
                    "data_transfer_out_gib": 1024,
                },
            ),
        ],
    )

    assert QuoteService._merge_transfer_only_ec2_services(intent) == 1
    assert len(intent.services) == 1
    assert intent.services[0].requirements["data_transfer_out_gib"] == 1024


def test_cache_size_without_model_does_not_require_pre_quote_confirmation() -> None:
    intent = ParsedIntent(
        customer_summary="Redis 1G",
        services=[
            ServiceRequirement(
                service="elasticache",
                calculator_service_name="Amazon ElastiCache",
                requirements={"engine": "redis", "memory_gib": 1},
            )
        ],
    )

    notices = QuoteService._confirmation_notices(intent)
    copy = QuoteService._confirmation_text(notices)

    assert notices == []
    assert copy is None


def test_missing_cache_size_uses_lowest_cost_default_without_question() -> None:
    intent = ParsedIntent(
        customer_summary="Redis 主从",
        services=[
            ServiceRequirement(
                service="elasticache",
                calculator_service_name="Amazon ElastiCache",
                quantity=2,
                requirements={"engine": "redis", "shards": 1, "replicas_per_shard": 1},
            )
        ],
    )

    notices = QuoteService._missing_spec_confirmation_notices(intent)

    assert notices == []


def test_cache_memory_can_be_recovered_from_ai_detail_or_source_text() -> None:
    requirement = ServiceRequirement(
        service="elasticache",
        calculator_service_name="Amazon ElastiCache",
        source_text="客户说明每个节点内存不低于 8 GiB",
        requirements={"engine": "redis"},
    )

    assert QuoteService._cache_requested_memory(requirement, []) == 8


@pytest.mark.asyncio
async def test_generic_ai_cache_node_notice_does_not_block_preview() -> None:
    class CacheParser:
        async def parse(self, _: str) -> ParsedIntent:
            return ParsedIntent(
                customer_summary="Redis 8G",
                ambiguities=["节点内存要求为不低于 8 GiB，需确认实际节点规格"],
                services=[
                    ServiceRequirement(
                        service="elasticache",
                        calculator_service_name="Amazon ElastiCache",
                        region="ap-southeast-1",
                        source_text="每个节点内存不低于 8 GiB",
                        requirements={"engine": "redis"},
                    )
                ],
            )

    class CachePlugin:
        def preview(self, requirement: ServiceRequirement, default_region: str) -> PreviewSelection:
            return PreviewSelection(
                component_id="component",
                service=ServiceKind.REDIS,
                display_name="Amazon ElastiCache",
                region=requirement.region or default_region,
                selected_model="cache.m7g.xlarge",
                selection_reason="official",
                candidates=[
                    CandidateOption(
                        model="cache.m7g.xlarge",
                        family="memory_optimized",
                        specifications={"memoryGiB": 12.93},
                        rationale="official",
                        is_default=True,
                    )
                ],
            )

    registry = PluginRegistry([])
    registry._plugins[ServiceKind.REDIS] = CachePlugin()  # type: ignore[assignment]
    service = QuoteService(
        CacheParser(),  # type: ignore[arg-type]
        registry,
        FailingEstimator(),  # type: ignore[arg-type]
        GenericCalculator(),  # type: ignore[arg-type]
    )

    preview = await service.preview(QuoteRequest(customer_request="Redis 8G"))

    assert preview.notices == []
    assert preview.confirmation_text is None


def test_alb_without_lcu_metrics_gets_reference_rate_without_fake_usage() -> None:
    intent = ParsedIntent(
        customer_summary="一个 ALB",
        services=[
            ServiceRequirement(
                service="elb",
                calculator_service_name="Elastic Load Balancing",
                requirements={"load_balancer_type": "application"},
            )
        ],
    )

    notices = QuoteService._apply_calculator_minimum_defaults(intent)

    assert len(notices) == 1
    assert "单位价" in notices[0]
    assert "不计入月费合计" in notices[0]
    assert intent.services[0].requirements["reference_lcu_unit_only"] is True
    assert "processed_bytes_ec2_ip_gib_per_hour" not in intent.services[0].requirements
    assert "requests_per_second" not in intent.services[0].requirements
    assert "new_connections_per_second" not in intent.services[0].requirements
    assert "system_default_assumption" in intent.services[0].requirements


def test_ai_alb_lcu_ambiguity_does_not_block_preview() -> None:
    intent = ParsedIntent(
        customer_summary="一个 ALB",
        ambiguities=["ALB 缺少 LCU 业务量，请确认每秒请求数"],
        services=[
            ServiceRequirement(
                service="elb",
                calculator_service_name="Elastic Load Balancing",
                requirements={"load_balancer_type": "application"},
            )
        ],
    )

    assert QuoteService._confirmation_notices(intent) == []


@pytest.mark.asyncio
async def test_late_business_issue_creates_one_follow_up_confirmation(tmp_path) -> None:
    store = ConfirmationSessionStore(tmp_path / "confirmations.sqlite3")
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
        confirmation_sessions=store,
    )
    draft_id = "lateissue001"
    intent = ParsedIntent(
        customer_summary="Redis 报价",
        services=[
            ServiceRequirement(
                service="redis",
                calculator_service_name="Amazon ElastiCache",
                requirements={"memory_gib": 8},
            )
        ],
    )
    service._drafts[draft_id] = ("Redis 8G", intent.model_copy(deep=True))
    error = ManualConfirmationRequired(
        "规格需要选择",
        code="redis_specification_not_found",
        service_index=0,
        display_name="Amazon ElastiCache",
        nearby_candidates=[
            {"model": "cache.small", "vcpu": 2, "memory_gib": 6.5},
            {"model": "cache.large", "vcpu": 2, "memory_gib": 12.3},
        ],
    )

    follow_up = await service._late_customer_confirmation(
        error,
        request=QuoteRequest(customer_request="Redis 8G", draft_id=draft_id),
        intent=intent,
    )

    assert follow_up.code == "late_customer_confirmation_required"
    assert follow_up.details["confirmation_round"] == 1
    assert len(follow_up.details["confirmation_items"][0]["options"]) == 2
    token = follow_up.details["confirmation_token"]
    assert store.get(token) is not None

    repeated = await service._late_customer_confirmation(
        error,
        request=QuoteRequest(customer_request="Redis 8G", draft_id=draft_id),
        intent=intent,
    )
    assert repeated.code == "confirmation_answer_not_applied"
    assert "confirmation_token" not in repeated.details


@pytest.mark.asyncio
async def test_late_finite_choice_recovers_catalog_options_when_error_has_none(
    tmp_path,
) -> None:
    class RecoverableRdsPlugin(ApiPlugin):
        def configuration_candidates(
            self,
            requirement: ServiceRequirement,
            default_region: str,
        ) -> list[CandidateOption]:
            del requirement, default_region
            return [
                CandidateOption(
                    model="db.r6g.xlarge",
                    family="db.r6g",
                    specifications={"vCPU": 4, "memoryGiB": 32},
                    monthly_catalog_cost=280,
                    rationale="AWS 官方当前区域可购买规格。",
                ),
                CandidateOption(
                    model="db.r6g.2xlarge",
                    family="db.r6g",
                    specifications={"vCPU": 8, "memoryGiB": 64},
                    monthly_catalog_cost=560,
                    rationale="AWS 官方当前区域可购买规格。",
                ),
            ]

    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([RecoverableRdsPlugin(ServiceKind.RDS, "unused")]),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
        confirmation_sessions=ConfirmationSessionStore(tmp_path / "confirmations.sqlite3"),
    )
    intent = ParsedIntent(
        customer_summary="Aurora PostgreSQL 报价",
        services=[
            ServiceRequirement(
                service="rds",
                calculator_service_name="Amazon Aurora PostgreSQL",
                region="ap-southeast-1",
                requirements={
                    "engine": "aurora_postgresql",
                    "requested_model": "db.t4g.medium",
                    "vcpu": 8,
                    "memory_gib": 32,
                },
            )
        ],
    )
    error = ManualConfirmationRequired(
        "AWS 官方 RDS 目录中没有满足需求的候选实例",
        code="rds_specification_not_found",
        service_index=0,
        display_name="Amazon Aurora PostgreSQL",
    )

    result = await service._late_customer_confirmation(
        error,
        request=QuoteRequest(
            customer_request="Aurora PostgreSQL 8核32G",
            draft_id="rdschoices01",
        ),
        intent=intent,
    )

    assert result.code == "late_customer_confirmation_required"
    item = result.details["confirmation_items"][0]
    assert item["selection_mode"] == "catalog"
    assert [option["model"] for option in item["options"]] == [
        "db.r6g.xlarge",
        "db.r6g.2xlarge",
    ]


@pytest.mark.asyncio
async def test_late_business_issue_does_not_repeat_after_process_restart(tmp_path) -> None:
    store = ConfirmationSessionStore(tmp_path / "confirmations.sqlite3")
    draft_id = "laterestart1"
    intent = ParsedIntent(
        customer_summary="OpenSearch 报价",
        services=[
            ServiceRequirement(
                service="opensearch",
                calculator_service_name="Amazon OpenSearch Service",
                requirements={"vcpu": 4, "memory_gib": 8},
            )
        ],
    )
    error = ManualConfirmationRequired(
        "规格需要选择",
        code="opensearch_specification_not_found",
        service_index=0,
        display_name="OpenSearch",
        nearby_candidates=[
            {"model": "m6g.large.search", "vcpu": 2, "memory_gib": 8},
        ],
    )
    first_service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
        confirmation_sessions=store,
    )
    first_service._drafts[draft_id] = (
        "OpenSearch 4核8G",
        intent.model_copy(deep=True),
    )
    first = await first_service._late_customer_confirmation(
        error,
        request=QuoteRequest(
            customer_request="OpenSearch 4核8G",
            draft_id=draft_id,
        ),
        intent=intent,
    )
    assert first.code == "late_customer_confirmation_required"

    restarted_service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
        confirmation_sessions=store,
    )
    repeated = await restarted_service._late_customer_confirmation(
        error,
        request=QuoteRequest(
            customer_request="OpenSearch 4核8G",
            draft_id=draft_id,
        ),
        intent=intent,
    )

    assert repeated.code == "confirmation_answer_not_applied"
    assert "confirmation_token" not in repeated.details


@pytest.mark.asyncio
async def test_late_technical_issue_is_never_turned_into_customer_question(tmp_path) -> None:
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
        confirmation_sessions=ConfirmationSessionStore(tmp_path / "confirmations.sqlite3"),
    )
    intent = ParsedIntent(
        customer_summary="Redis 报价",
        services=[ServiceRequirement(service="redis", requirements={"memory_gib": 8})],
    )
    error = ManualConfirmationRequired(
        "官方接口超时",
        code="pricing_catalog_unavailable",
        service_index=0,
    )

    result = await service._late_customer_confirmation(
        error,
        request=QuoteRequest(customer_request="Redis 8G", draft_id="technical001"),
        intent=intent,
    )

    assert result is error
    assert "confirmation_token" not in result.details


@pytest.mark.asyncio
async def test_late_business_questions_are_batched_into_one_customer_page(tmp_path) -> None:
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
        confirmation_sessions=ConfirmationSessionStore(tmp_path / "confirmations.sqlite3"),
    )
    intent = ParsedIntent(
        customer_summary="两项待确认配置",
        services=[
            ServiceRequirement(service="rds", requirements={}),
            ServiceRequirement(service="redis", requirements={}),
        ],
    )
    error = ManualConfirmationRequired(
        "批量确认",
        code="batched_component_confirmation_required",
        component_errors=[
            ManualConfirmationRequired(
                "缺少数据库引擎",
                code="missing_rds_engine",
                service_index=0,
                display_name="Amazon RDS",
            ),
            ManualConfirmationRequired(
                "缺少 Redis 容量",
                code="missing_redis_capacity",
                service_index=1,
                display_name="Amazon ElastiCache",
            ),
        ],
    )

    result = await service._late_customer_confirmation(
        error,
        request=QuoteRequest(customer_request="数据库和缓存", draft_id="batchconf001"),
        intent=intent,
    )

    assert result.code == "late_customer_confirmation_required"
    assert len(result.details["confirmation_items"]) == 2
    assert {item["component_id"] for item in result.details["confirmation_items"]} == {
        "0",
        "1",
    }


@pytest.mark.parametrize(
    "code",
    [
        "bcm_estimate_create_failed",
        "bcm_usage_rejected",
        "bcm_estimate_invalid",
        "bcm_estimate_timeout",
        "bcm_incomplete_result",
    ],
)
@pytest.mark.asyncio
async def test_late_bcm_failure_never_creates_customer_confirmation(
    tmp_path, code: str
) -> None:
    store = ConfirmationSessionStore(tmp_path / "confirmations.sqlite3")
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        api_registry(),
        ApiEstimator(),  # type: ignore[arg-type]
        None,
        confirmation_sessions=store,
    )
    intent = ParsedIntent(
        customer_summary="AWS 报价",
        services=[ServiceRequirement(service="generic", requirements={})],
    )
    error = ManualConfirmationRequired(
        "AWS BCM official pricing failure",
        code=code,
        service_index=0,
    )

    result = await service._late_customer_confirmation(
        error,
        request=QuoteRequest(customer_request="AWS 报价", draft_id="bcmtech00001"),
        intent=intent,
    )

    assert result is error
    assert "confirmation_token" not in result.details
    assert not service._is_ai_repairable_component_error(error)


def test_cloudfront_placeholder_request_count_is_removed_when_customer_did_not_ask() -> None:
    intent = ParsedIntent(
        customer_summary="CloudFront 5TB 下行",
        services=[
            ServiceRequirement(
                service="cloudfront",
                calculator_service_name="Amazon CloudFront",
                source_text="CloudFront 每月约 5TB 下行流量",
                requirements={
                    "data_transfer_out_gib": 5120,
                    "https_requests": "...",
                },
            )
        ],
    )

    QuoteService._apply_calculator_minimum_defaults(intent)

    assert "https_requests" not in intent.services[0].requirements
    assert intent.services[0].requirements["data_transfer_out_gib"] == 5120


def test_cloudfront_structured_traffic_is_not_erased_by_short_source_excerpt() -> None:
    intent = ParsedIntent(
        customer_summary="CloudFront 每月公网流量 10TB",
        services=[
            ServiceRequirement(
                service="cloudfront",
                calculator_service_name="Amazon CloudFront",
                source_text="需要 CloudFront CDN 加速",
                requirements={
                    "data_transfer_out_gib": 10240,
                    "reference_unit_only": True,
                    "system_default_assumption": (
                        "客户未提供 CloudFront 下行量；仅展示 1 GiB 对应的官方单位价，不计入月费合计"
                    ),
                },
            )
        ],
    )

    QuoteService._apply_calculator_minimum_defaults(intent)

    requirements = intent.services[0].requirements
    assert requirements["data_transfer_out_gib"] == 10240
    assert "reference_unit_only" not in requirements
    assert "system_default_assumption" not in requirements


def test_s3_customer_revision_capacity_is_not_erased_by_defaulting() -> None:
    intent = ParsedIntent(
        customer_summary="S3 20TB",
        services=[
            ServiceRequirement(
                service="s3",
                calculator_service_name="Amazon S3",
                source_text="客户最新修改：存储改为20个T",
                requirements={
                    "storage_gib": 20480,
                    "storage_class": "standard",
                    "reference_unit_only": True,
                    "system_default_assumption": "客户未提供 S3 容量",
                },
                field_sources={
                    "requirements.storage_gib": "customer_confirmation",
                },
            )
        ],
    )

    snapshot = QuoteService._customer_confirmed_snapshot(intent)
    QuoteService._apply_calculator_minimum_defaults(intent)
    QuoteService._restore_customer_confirmed_snapshot(intent, snapshot)

    requirements = intent.services[0].requirements
    assert requirements["storage_gib"] == 20480
    assert "reference_unit_only" not in requirements
    assert "system_default_assumption" not in requirements


def test_actual_usage_placeholders_are_removed_before_official_checks() -> None:
    intent = ParsedIntent(
        customer_summary="Redis 与公网流量按实际使用",
        services=[
            ServiceRequirement(
                service="elasticache",
                requirements={
                    "engine": "redis",
                    "memory_gib": "按实际使用量计费",
                    "shards": 1,
                    "replicas_per_shard": 1,
                },
            ),
            ServiceRequirement(
                service="data_transfer",
                requirements={"data_transfer_out_gib": "按实际使用流量计费"},
            ),
        ],
    )

    QuoteService._strip_non_numeric_placeholders(intent)

    assert "memory_gib" not in intent.services[0].requirements
    assert "data_transfer_out_gib" not in intent.services[1].requirements
    assert "shards" in intent.services[0].requirements


@pytest.mark.parametrize(
    ("plugin_class", "kind", "model"),
    [
        (S3Plugin, ServiceKind.S3, "S3 Standard"),
        (CloudFrontPlugin, ServiceKind.CLOUDFRONT, "CloudFront Pay-as-you-go"),
    ],
)
def test_minimum_s3_and_cloudfront_defaults_never_require_customer_confirmation(
    plugin_class: type[S3Plugin] | type[CloudFrontPlugin],
    kind: ServiceKind,
    model: str,
) -> None:
    plugin = object.__new__(plugin_class)
    plugin.select = lambda requirement, default_region: SelectedResource(  # type: ignore[method-assign]
        service=kind,
        display_name=model,
        region=default_region,
        model=model,
        architecture="minimum official unit",
        specifications={},
        official_product={"source": "AWS"},
        rationale="official minimum",
        substitution_notice="客户未提供用量，采用官方最低计费单位",
        usage_lines=[
            UsageLine(
                key="minimum",
                service_code="AmazonTest",
                usage_type="MinimumUsage",
                operation="",
                amount=1,
            )
        ],
    )

    preview = plugin.preview(ServiceRequirement(service=kind.value), "ap-southeast-1")

    assert preview.requires_confirmation is False
    assert preview.confirmation_reason is None


@pytest.mark.parametrize(
    "label",
    [
        "standard",
        "S3 Standard",
        "s3_standard",
        "Amazon S3 Standard",
        "General Purpose",
        "标准",
        "标准存储",
    ],
)
def test_s3_standard_labels_are_normalized_before_adapter_validation(label: str) -> None:
    assert _normalize_s3_storage_class(label) == "standard"


def test_s3_non_standard_tier_is_not_silently_changed() -> None:
    assert _normalize_s3_storage_class("Standard-IA") == "standard_ia"


def test_s3_standard_official_label_produces_storage_usage_line() -> None:
    class S3Catalog:
        @staticmethod
        def location(region: str) -> str:
            assert region == "ap-southeast-3"
            return "Asia Pacific (Jakarta)"

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 3,
        ) -> list[dict[str, object]]:
            assert service_code == "AmazonS3"
            assert filters["storageClass"] == "General Purpose"
            return [
                {
                    "serviceCode": "AmazonS3",
                    "product": {
                        "sku": "s3-standard-test",
                        "attributes": {
                            "usagetype": "APS4-TimedStorage-ByteHrs",
                            "operation": "",
                            "regionCode": "ap-southeast-3",
                        },
                    },
                }
            ]

    plugin = S3Plugin(None, S3Catalog())  # type: ignore[arg-type]
    selection = plugin.select(
        ServiceRequirement(
            service="s3",
            region="ap-southeast-3",
            requirements={"storage_class": "S3 Standard", "storage_gib": 100},
        ),
        "ap-southeast-1",
    )

    assert selection.model == "S3 Standard"
    assert selection.specifications["storageGiB"] == 100
    assert len(selection.usage_lines) == 1
    assert selection.usage_lines[0].amount == 100


def test_s3_customer_request_counts_create_independent_billing_lines() -> None:
    class S3Catalog:
        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 3,
            refresh: bool = False,
        ) -> list[dict[str, object]]:
            del max_pages, refresh
            assert service_code == "AmazonS3"
            if filters.get("productFamily") == "Storage":
                usage_type, operation, sku = "APE1-TimedStorage-ByteHrs", "", "storage"
            elif filters.get("group") == "S3-API-Tier1":
                usage_type, operation, sku = "APE1-Requests-Tier1", "", "put"
            elif filters.get("group") == "S3-API-Tier2":
                usage_type, operation, sku = "APE1-Requests-Tier2", "", "get"
            else:
                return []
            return [{
                "serviceCode": "AmazonS3",
                "product": {
                    "sku": sku,
                    "attributes": {
                        "usagetype": usage_type,
                        "operation": operation,
                        "regionCode": "ap-east-1",
                    },
                },
            }]

    selected = S3Plugin(None, S3Catalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="s3",
            region="ap-east-1",
            requirements={
                "storage_class": "standard",
                "storage_gib": 12_288,
                "put_copy_post_list_requests": 3_000_000,
                "get_select_requests": 50_000_000,
            },
        ),
        "ap-southeast-1",
    )

    assert [(line.key, line.amount) for line in selected.usage_lines] == [
        ("s3", 12_288),
        ("s3put", 3_000_000),
        ("s3get", 50_000_000),
    ]


def test_s3_request_selection_excludes_annotation_meters_in_the_same_group() -> None:
    class S3Catalog:
        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 3,
            refresh: bool = False,
        ) -> list[dict[str, object]]:
            del max_pages, refresh
            assert service_code == "AmazonS3"

            def product(sku: str, usage_type: str, description: str) -> dict[str, object]:
                return {
                    "serviceCode": "AmazonS3",
                    "product": {
                        "sku": sku,
                        "attributes": {
                            "usagetype": usage_type,
                            "operation": "",
                            "regionCode": "ap-southeast-1",
                            "groupDescription": description,
                        },
                    },
                }

            if filters.get("productFamily") == "Storage":
                return [product("storage", "APS1-TimedStorage-ByteHrs", "Storage")]
            if filters.get("group") == "S3-API-Tier1":
                return [
                    product(
                        "annotation-put",
                        "APS1-Requests-Annotation-Tier1",
                        "Tier1 Annotation Requests",
                    ),
                    product(
                        "ordinary-put",
                        "APS1-Requests-Tier1",
                        "PUT/COPY/POST or LIST requests",
                    ),
                ]
            if filters.get("group") == "S3-API-Tier2":
                return [
                    product(
                        "annotation-get",
                        "APS1-Requests-Annotation-Tier2",
                        "Tier2 Annotation Requests",
                    ),
                    product(
                        "ordinary-get",
                        "APS1-Requests-Tier2",
                        "GET and all other requests",
                    ),
                ]
            return []

    selected = S3Plugin(None, S3Catalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="s3",
            region="ap-southeast-1",
            requirements={
                "storage_class": "standard",
                "storage_gib": 20_480,
                "put_copy_post_list_requests": 5_000_000,
                "get_select_requests": 80_000_000,
            },
        ),
        "ap-southeast-1",
    )

    request_identities = {
        line.key: (line.usage_type, line.amount) for line in selected.usage_lines
    }
    assert request_identities["s3put"] == ("APS1-Requests-Tier1", 5_000_000)
    assert request_identities["s3get"] == ("APS1-Requests-Tier2", 80_000_000)


def test_cloudfront_never_infers_traffic_geography_from_deployment_region() -> None:
    plugin = CloudFrontPlugin(None, object())  # type: ignore[arg-type]

    with pytest.raises(ManualConfirmationRequired) as error:
        plugin.select(
            ServiceRequirement(
                service="cloudfront",
                region="ap-east-1",
                requirements={
                    "data_transfer_out_gib": 8192,
                    "https_requests": 120_000_000,
                },
            ),
            "ap-southeast-1",
        )

    assert error.value.code == "cloudfront_traffic_geography_required"


def test_invalid_internal_requirement_is_not_a_customer_question() -> None:
    error = ManualConfirmationRequired(
        "需求字段 https_requests 必须是数值",
        code="invalid_requirement",
        field="https_requests",
    )

    assert QuoteService._is_technical_catalog_error(error)


def test_first_submission_repairs_internal_field_to_detected_product_identity() -> None:
    reviewed = ServiceRequirement(
        service="elasticache",
        product_identity="elasticache_valkey",
        requirements={"engine": "valkey"},
    )
    matching = ServiceRequirement(
        service="elasticache",
        requirements={"engine": "valkey"},
    )

    QuoteService._align_pricing_product_identity(reviewed, matching)

    mismatched = ServiceRequirement(
        service="elasticache",
        requirements={"engine": "redis"},
    )
    QuoteService._align_pricing_product_identity(reviewed, mismatched)

    assert mismatched.requirements["engine"] == "valkey"
    assert reviewed.requirements["engine"] == "valkey"


@pytest.mark.parametrize(
    ("identity", "field", "value"),
    [
        ("rds_mysql", "engine", "mysql"),
        ("aurora_postgresql", "engine", "aurora_postgresql"),
        ("network_load_balancer", "load_balancer_type", "network"),
        ("amazon_mq_rabbitmq", "engine_type", "rabbitmq"),
        ("api_gateway_websocket", "api_type", "websocket"),
        ("amazon_msk_serverless", "cluster_type", "serverless"),
        ("amazon_fsx_lustre", "file_system_type", "lustre"),
    ],
)
def test_product_family_variants_keep_independent_pricing_identity(
    identity: str,
    field: str,
    value: str,
) -> None:
    reviewed = ServiceRequirement(
        service="generic",
        product_identity=identity,
        requirements={field: value},
    )
    pricing_copy = ServiceRequirement(
        service="generic",
        requirements={field: value},
    )

    QuoteService._align_pricing_product_identity(reviewed, pricing_copy)

    assert pricing_copy.requirements[field] == value


@pytest.mark.parametrize(
    ("identity", "field", "wrong_value", "expected_value"),
    [
        ("rds_mysql", "engine", "aurora_mysql", "mysql"),
        ("aurora_postgresql", "engine", "postgresql", "aurora_postgresql"),
        ("network_load_balancer", "load_balancer_type", "application", "network"),
        ("amazon_mq_rabbitmq", "engine_type", "activemq", "rabbitmq"),
        ("api_gateway_websocket", "api_type", "rest", "websocket"),
        ("amazon_msk_serverless", "cluster_type", "provisioned", "serverless"),
        ("amazon_fsx_lustre", "file_system_type", "windows", "lustre"),
    ],
)
def test_all_shared_adapter_families_repair_stale_internal_identity(
    identity: str,
    field: str,
    wrong_value: str,
    expected_value: str,
) -> None:
    reviewed = ServiceRequirement(
        service="generic",
        product_identity=identity,
        requirements={field: expected_value},
    )
    pricing_copy = ServiceRequirement(
        service="generic",
        requirements={field: wrong_value},
    )

    QuoteService._align_pricing_product_identity(reviewed, pricing_copy)

    assert pricing_copy.requirements[field] == expected_value
    assert reviewed.requirements[field] == expected_value


def test_dependency_notes_do_not_invent_eks_workers() -> None:
    eks = ServiceRequirement(
        service="eks",
        quantity=1,
        source_text="Amazon EKS\nKubernetes集群\n一套",
    )
    notes = QuoteService._dependency_remarks(eks, [eks])

    assert len(notes) == 1
    assert "仅含 EKS 集群控制平面" in notes[0]
    assert "未计入" in notes[0]


def test_dependency_notes_explain_cloudfront_origin_without_adding_cost() -> None:
    cloudfront = ServiceRequirement(service="cloudfront", source_text="流量：5TB/月")
    s3 = ServiceRequirement(service="s3", requirements={"storage_gib": 20 * 1024})

    assert QuoteService._dependency_remarks(cloudfront, [cloudfront, s3]) == [
        "CloudFront 源站 S3 的存储与请求费用在 S3 项中单独计费。"
    ]


def test_missing_rds_deployment_question_has_customer_choices() -> None:
    options = QuoteService._default_confirmation_options(
        "Amazon RDS 数据库未说明部署方式，请选择单可用区还是主备高可用（Multi-AZ）。"
    )

    assert [(item.label, item.value) for item in options] == [
        ("单可用区", "single_az"),
        ("主备高可用（Multi-AZ）", "multi_az"),
    ]


def test_missing_rds_engine_question_has_customer_choices() -> None:
    options = QuoteService._default_confirmation_options(
        "Amazon RDS 数据库没有说明数据库类型，请选择 MySQL、PostgreSQL、MariaDB、"
        "SQL Server、Oracle 或 Db2。"
    )

    assert [item.value for item in options] == [
        "mysql",
        "postgresql",
        "mariadb",
        "sql_server_standard",
        "oracle",
        "db2",
    ]


def test_unsupported_rds_version_uses_plain_language_and_dropdown_choices() -> None:
    requirement = ServiceRequirement(
        service="rds",
        region="ap-southeast-1",
        requirements={"engine": "mysql", "engine_version": "5.7.44"},
    )
    error = ManualConfirmationRequired(
        "unsupported",
        code="unsupported_rds_engine_or_region",
        engine="mysql",
        requested_version="5.7.44",
        region="ap-southeast-1",
        supported_engine_versions=["8.4.7", "8.0.43"],
    )

    question = QuoteService._plugin_confirmation_question("Amazon RDS MySQL", requirement, error)
    options = QuoteService._default_confirmation_options(question)

    assert "5.7.44" in question
    assert "已不能新购" in question
    assert [option.value for option in options] == [
        "engine_version:8.4.7",
        "engine_version:8.0.43",
    ]
    assert "推荐" in options[0].label
    assert "标准支持" not in options[0].label
    assert "旧版本，会额外收费" in options[1].label


def test_rds_engine_version_wording_still_generates_dropdown_choices() -> None:
    question = (
        "您指定的 MySQL 引擎版本为 5.7.44，但在 ap-southeast-2 区域，"
        "RDS 支持的 MySQL 版本是 5.7.44-rds.20260624。"
        "请问您是否接受使用官方支持的版本 5.7.44-rds.20260624？"
    )

    options = QuoteService._default_confirmation_options(question)

    assert [option.value for option in options] == [
        "engine_version:8.4",
        "engine_version:5.7.44-rds.20260624",
    ]
    assert "推荐" in options[0].label
    assert "额外收费" in options[1].label


def test_derived_ec2_question_includes_parent_requirement_and_reason() -> None:
    parent = ServiceRequirement(
        service="vpc",
        calculator_service_name="Amazon VPC (Private)",
        source_text="Private-VPC：数量1，用于 EC2 集群 / Pod 实例",
    )
    derived = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        source_text="用于 EC2 集群 / Pod 实例",
    )
    intent = ParsedIntent(
        customer_summary="VPC 和衍生 EC2",
        services=[parent, derived],
    )
    selection = PreviewSelection(
        component_id="1",
        service=ServiceKind.EC2,
        display_name="Amazon EC2",
        region="ap-southeast-2",
        candidates=[],
        requires_confirmation=True,
    )

    question = QuoteService._plain_model_selection_question(selection, derived, intent)

    assert "在“Private-VPC”中提到了" in question
    assert "用于 EC2 集群 / Pod 实例" in question
    assert "几核、多少内存" in question
    assert "请在下面选择" in question


def test_derived_ec2_question_finds_parent_with_the_same_source_text() -> None:
    source = "用于 EC2 集群 / Pod 实例"
    parent = ServiceRequirement(
        service="vpc",
        calculator_service_name="Amazon VPC (Private)",
        source_text=source,
    )
    derived = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        source_text=source,
    )
    intent = ParsedIntent(customer_summary="VPC 和衍生 EC2", services=[parent, derived])
    selection = PreviewSelection(
        component_id="1",
        service=ServiceKind.EC2,
        display_name="Amazon EC2",
        region="ap-southeast-2",
        candidates=[],
        requires_confirmation=True,
    )

    question = QuoteService._plain_model_selection_question(selection, derived, intent)

    assert "在“Amazon VPC (Private)”中提到了" in question
    assert "几核、多少内存" in question


def test_model_question_includes_explicit_component_source() -> None:
    requirement = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        source_text="应用服务器：数量1，用于后台任务",
    )
    selection = PreviewSelection(
        component_id="0",
        service=ServiceKind.EC2,
        display_name="Amazon EC2",
        region="ap-southeast-2",
        candidates=[],
        requires_confirmation=True,
    )

    question = QuoteService._plain_model_selection_question(selection, requirement)

    assert "客户提到了“应用服务器”" in question
    assert "需要几核、多少内存" in question


def test_derived_ec2_finds_adjacent_private_vpc_parent_after_ai_splits_source() -> None:
    parent = ServiceRequirement(
        service="vpc",
        calculator_service_name="Amazon Virtual Private Cloud (VPC)",
        source_text="Amazon VPC（Private）：数量1，用于私有网络环境，",
    )
    derived = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2",
        source_text="承载内部EC2服务器、EKS工作负载及其他内部业务资源",
    )
    intent = ParsedIntent(customer_summary="私有网络", services=[parent, derived])
    selection = PreviewSelection(
        component_id="1",
        service=ServiceKind.EC2,
        display_name="Amazon EC2",
        region="ap-east-1",
        candidates=[],
        requires_confirmation=True,
    )

    question = QuoteService._plain_model_selection_question(selection, derived, intent)

    assert "在“Amazon VPC（Private）”中提到了" in question
    assert "承载内部EC2服务器、EKS工作负载" in question


@pytest.mark.asyncio
async def test_rds_version_confirmation_updates_only_its_database_component() -> None:
    question = (
        "Amazon RDS MySQL：当前 mysql 5.7.44 已不再提供维护或订购，"
        "请改用仍受支持的数据库版本。可选版本：8.4.7、8.0.43。"
    )
    intent = ParsedIntent(
        customer_summary="数据库和服务器",
        services=[
            ServiceRequirement(service="ec2", requirements={"vcpu": 4}),
            ServiceRequirement(
                service="rds",
                requirements={"engine": "mysql", "engine_version": "5.7.44"},
            ),
        ],
        ambiguities=[question],
    )
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    await service._apply_confirmation_responses(intent, {question: "engine_version:8.4.7"})

    assert intent.services[0].requirements == {"vcpu": 4}
    assert intent.services[1].requirements["engine_version"] == "8.4.7"
    assert intent.services[1].requirements["_review_field_options"]["engine_version"] == [
        "8.4.7",
        "8.0.43",
    ]
    assert intent.ambiguities == []


@pytest.mark.asyncio
async def test_rds_chinese_engine_version_dropdown_is_applied_deterministically() -> None:
    question = (
        "您指定的 MySQL 引擎版本 5.8 在 ap-southeast-2 区域不受支持。"
        "该区域支持的 MySQL 版本是 8.4.11、8.0.46-rds.20260624 或 "
        "5.7.44-rds.20260624。请问您希望使用哪个支持的版本？"
    )
    intent = ParsedIntent(
        customer_summary="RDS",
        services=[
            ServiceRequirement(
                service="rds",
                requirements={"engine": "mysql", "engine_version": "5.8"},
            )
        ],
        ambiguities=[question],
    )
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    await service._apply_confirmation_responses(intent, {question: "engine_version:8.4.11"})

    assert intent.services[0].requirements["engine_version"] == "8.4.11"
    assert intent.ambiguities == []


@pytest.mark.asyncio
async def test_rds_version_answer_updates_only_its_bound_component() -> None:
    question = (
        "RDS MySQL：当前 mysql 5.7.44 在 us-east-1 已不再提供维护或订购，"
        "请改用下方仍受支持的数据库版本。"
    )
    response_key = QuoteService._scoped_confirmation_response_key(1, question)
    intent = ParsedIntent(
        customer_summary="两套独立数据库",
        services=[
            ServiceRequirement(
                service="rds",
                requirements={"engine": "mysql", "engine_version": "8.0.46"},
            ),
            ServiceRequirement(
                service="rds",
                requirements={"engine": "mysql", "engine_version": "5.7.44"},
            ),
        ],
        ambiguities=[question],
    )
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    await service._apply_confirmation_responses(
        intent,
        {response_key: "engine_version:8.4.11"},
        response_components={response_key: 1},
    )

    assert intent.services[0].requirements["engine_version"] == "8.0.46"
    assert intent.services[1].requirements["engine_version"] == "8.4.11"
    assert intent.services[1].field_sources["requirements.engine_version"] == (
        "customer_confirmation"
    )


def test_unsupported_rds_version_bypasses_ai_repair() -> None:
    error = ManualConfirmationRequired(
        "RDS 引擎或版本不受支持",
        code="unsupported_rds_engine_or_region",
    )

    assert QuoteService._is_ai_repairable_component_error(error) is False


@pytest.mark.asyncio
async def test_rds_engine_confirmation_updates_only_the_database_component() -> None:
    question = (
        "Amazon RDS 数据库没有说明数据库类型，请选择 MySQL、PostgreSQL、MariaDB、"
        "SQL Server、Oracle 或 Db2。"
    )
    intent = ParsedIntent(
        customer_summary="数据库和服务器",
        services=[
            ServiceRequirement(service="ec2", requirements={"vcpu": 4}),
            ServiceRequirement(
                service="rds",
                requirements={"vcpu": 8, "memory_gib": 32},
            ),
        ],
        ambiguities=[question],
    )
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    await service._apply_confirmation_responses(intent, {question: "postgresql"})

    assert intent.services[0].requirements == {"vcpu": 4}
    assert intent.services[1].requirements["engine"] == "postgresql"
    assert intent.ambiguities == []


def test_nacos_partial_managed_replacement_has_two_clear_choices() -> None:
    options = QuoteService._default_confirmation_options(
        "您需要 Nacos 的服务发现和配置中心。是继续自建 Nacos（3 个节点），"
        "还是改用 AWS 托管的 Cloud Map + AppConfig？托管方案不再按 Nacos 节点部署。"
    )

    assert [item.value for item in options] == [
        "nacos_self_hosted",
        "aws_managed_cloudmap_appconfig",
    ]


def test_all_partial_managed_replacements_use_the_same_two_step_choice() -> None:
    options = QuoteService._default_confirmation_options(
        "XXL-JOB 没有完全等价的 AWS 托管服务，请选择采用 AWS 托管方案还是保留原产品自建。"
    )

    assert options[0].label == "采用 AWS Step Functions + EventBridge Scheduler"
    assert options[0].value.startswith("managed:step_functions:")
    assert "托管任务编排和定时触发" in str(options[0].description)
    assert options[1].label == "保留原产品，在 Amazon EC2 上自建"
    assert options[1].value == "self_hosted"


def test_managed_recommendation_option_includes_product_and_function() -> None:
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2（自建 Elasticsearch）",
    )
    options = QuoteService._default_confirmation_options(
        "AWS 没有与 Elasticsearch 完全等价的托管服务，您要采用 AWS 托管替代方案（功能可能不同），"
        "还是按原配置在 EC2 上自建 Elasticsearch？",
        component=component,
    )

    assert options[0].label == "采用 Amazon OpenSearch Service"
    assert "主要用途：搜索与分析" in str(options[0].description)
    assert options[0].value.startswith("managed:opensearch:")


def test_doris_managed_recommendation_explains_product_function_and_difference() -> None:
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2（自建 Doris）",
        source_text="2、Doris：每节点 16 核 128GB，共 3 节点",
    )

    options = QuoteService._default_confirmation_options(
        "AWS 没有与 Doris 完全等价的托管服务。您要采用 AWS 托管方案，"
        "还是按原配置在 EC2 上自建 Doris？",
        component=component,
    )

    assert options[0].label == "采用 Amazon Redshift"
    assert options[0].value.startswith("managed:redshift:")
    assert "托管数据仓库和分析查询" in str(options[0].description)
    assert "不是 Apache Doris" in str(options[0].description)


def test_unknown_managed_alternative_is_not_published_as_a_blind_choice() -> None:
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2（自建 CompletelyUnknownProduct）",
    )

    options = QuoteService._default_confirmation_options(
        "AWS 没有与 CompletelyUnknownProduct 完全等价的托管服务。"
        "请选择采用 AWS 托管方案还是保留原产品自建。",
        component=component,
    )

    assert options == []


@pytest.mark.asyncio
async def test_applying_managed_recommendation_updates_component_to_native_service() -> None:
    question = (
        "AWS 没有与 Elasticsearch 完全等价的托管服务，您要采用 AWS 托管替代方案"
        "（功能可能不同），还是按原配置在 EC2 上自建 Elasticsearch？"
    )
    component = ServiceRequirement(
        service="ec2",
        calculator_service_name="Amazon EC2（自建 Elasticsearch）",
        source_text="1、Elasticsearch：3 个节点，每节点 16C64G",
        field_sources={"_pending_architecture_decision": "system_policy"},
    )
    intent = ParsedIntent(
        customer_summary="elasticsearch",
        services=[component],
        ambiguities=[question],
    )
    service = QuoteService.__new__(QuoteService)

    await service._apply_confirmation_responses(
        intent,
        {
            service._scoped_confirmation_response_key(0, question): (
                "managed:opensearch:Amazon OpenSearch Service:搜索与分析"
            )
        },
        response_components={service._scoped_confirmation_response_key(0, question): 0},
    )

    updated = intent.services[0]
    assert updated.service == "opensearch"
    assert updated.calculator_service_name == "Amazon OpenSearch Service"
    assert updated.field_sources.get("_architecture_decision") == "aws_managed"
    assert "_pending_architecture_decision" not in updated.field_sources


def test_workflow_controls_bypass_free_form_ai_revision() -> None:
    assert QuoteService._is_structured_workflow_answer("engine_version:8.4.7")
    assert QuoteService._is_structured_workflow_answer(
        "billing_variant:data_processed_gib:APN2-Traffic-GB-Processed"
    )
    assert QuoteService._is_structured_workflow_answer(
        "managed:opensearch:Amazon OpenSearch Service:搜索与分析"
    )
    assert QuoteService._is_structured_workflow_answer("replace_service:rds:postgresql")
    assert QuoteService._is_structured_workflow_answer("exclude_component")
    assert QuoteService._is_structured_workflow_answer("self_hosted")
    assert QuoteService._is_structured_workflow_answer("选择 m7g.large；机器数量 3")
    assert not QuoteService._is_structured_workflow_answer("改成单可用区")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "expected_services", "expected_quantities"),
    [
        ("nacos_self_hosted", ["ec2"], [3]),
        (
            "aws_managed_cloudmap_appconfig",
            ["cloud_map", "appconfig"],
            [1, 1],
        ),
    ],
)
async def test_nacos_confirmation_preserves_self_host_topology_or_splits_managed_services(
    answer: str,
    expected_services: list[str],
    expected_quantities: list[int],
) -> None:
    question = (
        "您需要 Nacos 的服务发现和配置中心。是继续自建 Nacos（3 个节点），"
        "还是改用 AWS 托管的 Cloud Map + AppConfig？托管方案不再按 Nacos 节点部署。"
    )
    intent = ParsedIntent(
        customer_summary="Nacos",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2（自建 Nacos）",
                region="ap-southeast-1",
                quantity=3,
                requirements={
                    "operating_system": "linux",
                    "vcpu": 1,
                    "memory_gib": 2,
                },
                field_sources={
                    "requirements.vcpu": "system_minimum",
                    "requirements.memory_gib": "system_minimum",
                },
                source_text="Nacos：服务注册发现和配置中心，部署数量：3个节点",
            )
        ],
        ambiguities=[question],
    )
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    await service._apply_confirmation_responses(intent, {question: answer})

    assert [item.service for item in intent.services] == expected_services
    assert [item.quantity for item in intent.services] == expected_quantities
    assert all(item.region == "ap-southeast-1" for item in intent.services)
    if answer == "nacos_self_hosted":
        assert "_pending_architecture_decision" not in intent.services[0].field_sources
        assert (
            intent.services[0].field_sources["_customer_select_configuration"]
            == "customer_confirmation"
        )
    else:
        assert all(
            "_pending_architecture_decision" not in item.field_sources for item in intent.services
        )
    assert intent.ambiguities == []


@pytest.mark.asyncio
async def test_self_hosted_machine_selection_applies_model_and_machine_count() -> None:
    intent = ParsedIntent(
        customer_summary="自建服务",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2（自建服务）",
                quantity=3,
                field_sources={"_customer_select_configuration": "customer_confirmation"},
            )
        ],
    )
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    await service._apply_confirmation_responses(
        intent,
        {"EC2 自建服务：请选择自建服务的机器台数和每台 EC2 配置。": "选择 m7g.large；机器数量 5"},
    )

    component = intent.services[0]
    assert component.quantity == 5
    assert component.requirements["requested_model"] == "m7g.large"
    assert "_pending_architecture_decision" not in component.field_sources
    assert "_customer_select_configuration" not in component.field_sources
    assert component.requirements["requested_model"] == "m7g.large"
    assert "_customer_select_configuration" not in component.field_sources


@pytest.mark.asyncio
async def test_self_hosted_choice_and_machine_configuration_apply_in_one_answer() -> None:
    question = "请选择 AWS 托管方案还是保留原产品自建。"
    intent = ParsedIntent(
        customer_summary="自建产品",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2（自建产品）",
                quantity=3,
                field_sources={"_pending_architecture_decision": "system_policy"},
            )
        ],
        ambiguities=[question],
    )
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    await service._apply_confirmation_responses(
        intent,
        {question: "self_hosted；选择 m7g.large；机器数量 5"},
    )

    component = intent.services[0]
    assert component.quantity == 5


@pytest.mark.asyncio
async def test_self_hosted_choice_reuses_customer_cpu_and_memory_without_second_picker() -> None:
    question = "请选择 AWS 托管方案还是保留原产品自建。"
    intent = ParsedIntent(
        customer_summary="ClickHouse",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2（自建 ClickHouse）",
                quantity=3,
                requirements={"vcpu": 8, "memory_gib": 32, "system_disk_gib": 1024},
                field_sources={"_pending_architecture_decision": "system_policy"},
            )
        ],
    )

    service = QuoteService.__new__(QuoteService)
    await service._apply_confirmation_responses(
        intent,
        {question: "self_hosted"},
        response_components={question: 0},
    )

    component = intent.services[0]
    assert component.quantity == 3
    assert component.requirements["vcpu"] == 8
    assert component.requirements["memory_gib"] == 32
    assert component.requirements["system_disk_gib"] == 1024
    assert "_customer_select_configuration" not in component.field_sources
    assert "_pending_architecture_decision" not in component.field_sources


@pytest.mark.asyncio
async def test_selected_model_uses_confirmation_component_id_with_multiple_ec2() -> None:
    question = "您还没有指定 EKS 工作节点的 CPU 和内存，请选择型号。"
    intent = ParsedIntent(
        customer_summary="应用服务器和 EKS",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                requirements={"requested_model": "t4g.micro"},
            ),
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2 (EKS Worker Nodes)",
            ),
        ],
    )
    service = QuoteService(
        MixedParser(),  # type: ignore[arg-type]
        PluginRegistry([]),
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    await service._apply_confirmation_responses(
        intent,
        {question: "选择 m7g.xlarge"},
        response_components={question: 1},
    )

    assert intent.services[0].requirements["requested_model"] == "t4g.micro"
    assert intent.services[1].requirements["requested_model"] == "m7g.xlarge"


@pytest.mark.asyncio
async def test_pending_managed_decision_embeds_self_hosted_configuration() -> None:
    question = (
        "您需要 Nacos 的服务发现和配置中心。是继续自建 Nacos（3 个节点），"
        "还是改用 AWS 托管的 Cloud Map + AppConfig？托管方案不再按 Nacos 节点部署。"
    )

    class PendingArchitectureParser:
        async def parse(self, _: str) -> ParsedIntent:
            return ParsedIntent(
                customer_summary="Nacos",
                services=[
                    ServiceRequirement(
                        service="ec2",
                        calculator_service_name="Amazon EC2（自建 Nacos）",
                        region="ap-southeast-1",
                        quantity=3,
                        source_text="Nacos，3个节点",
                        field_sources={"_pending_architecture_decision": "system_policy"},
                    )
                ],
                ambiguities=[question],
            )

    service = QuoteService(
        PendingArchitectureParser(),  # type: ignore[arg-type]
        PluginRegistry([ApiPlugin(ServiceKind.EC2, "t4g.small")]),
        FailingEstimator(),  # type: ignore[arg-type]
        None,
    )

    preview = await service.preview(QuoteRequest(customer_request="Nacos 3个节点"))

    assert [item.question for item in preview.confirmation_items] == [question]
    assert [option.value for option in preview.confirmation_items[0].options] == [
        "nacos_self_hosted",
        "aws_managed_cloudmap_appconfig",
    ]
    assert [option.model for option in preview.confirmation_items[0].dependent_options] == [
        "t4g.small"
    ]
    assert preview.confirmation_items[0].dependent_on_values == [
        "nacos_self_hosted",
        "self_hosted",
    ]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("选择 kafka.m7g.large", "kafka.m7g.large"),
        ("选择 r7g.large.search", "r7g.large.search"),
        ("选择 mq.m5.large", "mq.m5.large"),
        ("选择 db.r6g.2xlarge", "db.r6g.2xlarge"),
    ],
)
def test_confirmation_model_parser_accepts_official_model_families(
    answer: str, expected: str
) -> None:
    assert QuoteService._model_from_confirmation_answer(answer) == expected


@pytest.mark.parametrize(
    ("question", "model", "expected"),
    [
        ("Amazon MSK Broker 规格请选择", "kafka.m7g.large", ServiceKind.MSK),
        (
            "Amazon OpenSearch 数据节点规格请选择",
            "r7g.large.search",
            ServiceKind.OPENSEARCH,
        ),
    ],
)
def test_confirmation_model_choice_is_bound_to_the_correct_service(
    question: str, model: str, expected: ServiceKind
) -> None:
    assert QuoteService._confirmation_service_kind(question, model) == expected


@pytest.mark.asyncio
async def test_unknown_third_party_product_becomes_architecture_question() -> None:
    customer_request = (
        "1、Amazon EC2：区域：新加坡，数量：3台\n"
        "5、ClickHouse：用途：实时数据分析和报表查询，部署数量：3个节点，"
        "每节点配置：8核32GB，存储容量：1TB/节点"
    )

    class ClickHouseParser:
        async def parse(self, _: str) -> ParsedIntent:
            return ParsedIntent(
                customer_summary="ClickHouse",
                services=[
                    ServiceRequirement(
                        service="clickhouse",
                        calculator_service_name="ClickHouse",
                        region="ap-southeast-1",
                        source_text="ClickHouse：",
                    )
                ],
            )

    class MissingGenericCatalog:
        def preview(self, *_: object) -> None:
            raise ManualConfirmationRequired(
                "没有找到官方服务代码",
                code="generic_service_code_not_found",
            )

    service = QuoteService(
        ClickHouseParser(),  # type: ignore[arg-type]
        PluginRegistry([ApiPlugin(ServiceKind.EC2, "m6i.2xlarge")]),
        FailingEstimator(),  # type: ignore[arg-type]
        None,
        generic_plugin=MissingGenericCatalog(),  # type: ignore[arg-type]
    )

    preview = await service.preview(QuoteRequest(customer_request=customer_request))

    clickhouse_items = [
        item for item in preview.confirmation_items if "ClickHouse" in item.question
    ]
    assert len(clickhouse_items) == 1, (preview.confirmation_items, preview.selections)
    assert clickhouse_items[0].options[0].value.startswith("managed:redshift:")
    assert clickhouse_items[0].options[0].label == "采用 Amazon Redshift"
    assert clickhouse_items[0].options[1].value == "self_hosted"
    assert [option.model for option in clickhouse_items[0].dependent_options] == ["m6i.2xlarge"]
    recovered = service._drafts[preview.draft_id][1].services[0]
    assert recovered.quantity == 3
    assert recovered.requirements["vcpu"] == 8
    assert recovered.requirements["memory_gib"] == 32
    assert recovered.requirements["system_disk_gib"] == 1024
    assert recovered.field_sources["_pending_architecture_decision"] == "system_policy"


def test_multiple_architecture_questions_bind_to_their_own_components() -> None:
    intent = ParsedIntent(
        customer_summary="two self-hosted products",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2（自建 Apache Kafka）",
                field_sources={
                    "_pending_architecture_decision": "system_policy",
                    "_third_party_product": "Apache Kafka",
                },
            ),
            ServiceRequirement(
                service="clickhouse",
                calculator_service_name="Amazon EC2（自建 ClickHouse）",
                field_sources={
                    "_pending_architecture_decision": "system_policy",
                    "_third_party_product": "ClickHouse",
                },
            ),
        ],
    )
    pending = {"0", "1"}

    assert (
        QuoteService._architecture_notice_component_id(
            intent,
            "AWS 没有与 Apache Kafka 完全等价的托管服务，采用托管还是自建？",
            pending,
        )
        == "0"
    )
    assert (
        QuoteService._architecture_notice_component_id(
            intent,
            "AWS 没有与 ClickHouse 完全等价的托管服务，采用托管还是自建？",
            pending,
        )
        == "1"
    )


def test_model_availability_question_waits_until_region_is_selected() -> None:
    region_question = "请确认这些区域型服务部署在哪个 AWS 区域；如各服务区域不同，请分别说明。"
    model_question = (
        "EC2（自建 ClickHouse）：AWS 当前区域没有这个型号，请从当前区域支持的规格中选择。"
    )
    intent = ParsedIntent(
        customer_summary="ClickHouse",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2（自建 ClickHouse）",
                region=None,
                requirements={"requested_model": "m6i.xlarge"},
            )
        ],
    )

    assert QuoteService._drop_pre_region_catalog_questions(
        intent,
        [region_question, model_question],
        {model_question: ("0", "ec2")},
        {model_question: [ConfirmationOption(label="m6i.xlarge", value="m6i.xlarge")]},
    ) == [region_question]

    # A preview-only fallback may already be present on the component.  As long
    # as the customer-facing region question is still on the page, model
    # choices built from that fallback must stay hidden.
    intent.services[0].region = "ap-southeast-1"
    assert QuoteService._drop_pre_region_catalog_questions(
        intent,
        [region_question, model_question],
        {model_question: ("0", "ec2")},
        {model_question: [ConfirmationOption(label="m6i.xlarge", value="m6i.xlarge")]},
    ) == [region_question]

    # Once the region question is gone, the official model decision may be
    # shown (or auto-resolved by the next confirmation-round pass).
    assert QuoteService._drop_pre_region_catalog_questions(
        intent,
        [model_question],
        {model_question: ("0", "ec2")},
        {model_question: [ConfirmationOption(label="m6i.xlarge", value="m6i.xlarge")]},
    ) == [model_question]


def test_rds_version_question_waits_until_region_is_selected() -> None:
    region_question = "请确认这些区域型服务部署在哪个 AWS 区域。"
    version_question = (
        "RDS MySQL：当前 MySQL 5.7.44 在 ap-southeast-1 已不再提供维护或订购，"
        "请改用仍受支持的数据库版本。"
    )
    intent = ParsedIntent(
        customer_summary="RDS",
        services=[
            ServiceRequirement(
                service="rds",
                region="ap-southeast-1",
                requirements={"engine": "mysql", "engine_version": "5.7.44"},
            )
        ],
    )

    assert QuoteService._drop_pre_region_catalog_questions(
        intent,
        [region_question, version_question],
        {version_question: ("0", "rds")},
        {},
    ) == [region_question]


def test_region_question_is_the_only_first_round_decision() -> None:
    region_question = "请确认这些区域型服务部署在哪个 AWS 区域。"
    engine_question = "Amazon RDS 数据库没有说明数据库类型，请选择。"
    architecture_question = "AWS 没有与 ClickHouse 完全等价的托管服务，采用托管还是自建？"
    shape_question = "Redis 的内存没有完全相同的型号，请重新选择。"
    intent = ParsedIntent(
        customer_summary="区域未指定的多组件报价",
        services=[
            ServiceRequirement(service="rds", region=None),
            ServiceRequirement(service="clickhouse", region=None),
            ServiceRequirement(service="elasticache", region=None),
        ],
    )

    assert QuoteService._drop_pre_region_catalog_questions(
        intent,
        [
            engine_question,
            region_question,
            architecture_question,
            shape_question,
        ],
        {
            engine_question: ("0", "rds"),
            architecture_question: ("1", "clickhouse"),
            shape_question: ("2", "elasticache"),
        },
        {},
    ) == [region_question]


def test_opensearch_catalog_rewording_uses_one_confirmation_key() -> None:
    first = "OpenSearch 还没有指定型号，请在下方选择您需要的型号。"
    second = (
        "AWS 官方目录没有返回符合要求的 OpenSearch 节点规格。"
        "请从下方可用配置中选择，或补充业务规格。"
    )

    assert QuoteService._confirmation_question_key(first) == "opensearch|shape_model"
    assert QuoteService._confirmation_question_key(second) == "opensearch|shape_model"


@pytest.mark.asyncio
async def test_billing_variant_answer_is_bound_to_only_its_component() -> None:
    question = "Network Firewall 的每月处理流量有几种收费方式，请选择实际使用的一种。"
    intent = ParsedIntent(
        customer_summary="两个独立组件",
        services=[
            ServiceRequirement(
                service="appflow",
                requirements={"data_processed_gib": 100},
            ),
            ServiceRequirement(
                service="network_firewall",
                requirements={"data_processed_gib": 1024},
            ),
        ],
    )
    service = QuoteService.__new__(QuoteService)

    await service._apply_confirmation_responses(
        intent,
        {question: "billing_variant:data_processed_gib:APS1-Traffic-GB-Processed"},
        response_components={question: 1},
    )

    assert "_billing_variant_data_processed_gib" not in intent.services[0].requirements
    assert (
        intent.services[1].requirements["_billing_variant_data_processed_gib"]
        == "APS1-Traffic-GB-Processed"
    )


def test_billing_variant_question_stays_short_and_customer_facing() -> None:
    requirement = ServiceRequirement(
        service="network_firewall",
        calculator_service_name="AWS Network Firewall",
    )
    error = ManualConfirmationRequired(
        "AWS Network Firewall 的‘每月处理流量’有几种收费方式，价格不一样。"
        "请选择实际使用的那一种。",
        code="billing_variant_required",
    )

    question = QuoteService._plugin_confirmation_question(
        "AWS Network Firewall",
        requirement,
        error,
    )

    assert question == error.message
    assert "不能计算价格" not in question


def test_calculator_copy_keeps_confirmed_billing_variant_but_drops_review_metadata() -> None:
    normalized = QuoteService._calculator_requirements(
        {
            "requests": 1000,
            "_billing_variant_requests": "APS1-SingleAuthorizationRequest",
            "_review_status": "customer_issue",
            "_review_confirmation_reason": "old question",
        },
        1,
        "verified_permissions",
    )

    assert normalized == {
        "requests": 1000,
        "_billing_variant_requests": "APS1-SingleAuthorizationRequest",
    }
