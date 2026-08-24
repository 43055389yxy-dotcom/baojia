import pytest

from app.core.errors import ManualConfirmationRequired
from app.domain.models import (
    CandidateOption,
    ParsedIntent,
    PreviewSelection,
    QuoteRequest,
    ReferenceRate,
    SelectedResource,
    ServiceKind,
    ServiceRequirement,
    UsageLine,
)
from app.integrations.aws import PricingCatalog
from app.integrations.calculator_web import (
    CalculatorGenericGroupResult,
    CalculatorWebResult,
    GenericCalculatorInput,
)
from app.services.bcm_estimator import BcmQuoteResult
from app.services.confirmation_sessions import (
    CONFIGURATION_COMPONENT_FEEDBACK_PREFIX,
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
            total_cost=300.0,
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
        self.received_models.append(
            str(requested_model) if requested_model is not None else None
        )
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
    assert estimator.calls == [
        ["s1l1", "s2l1", "s3l1"],
        ["s1l1", "s3l1"],
    ]
    assert any(
        "RDS" in notice and "本次未取得可累计的官方月费" in notice
        for notice in quote.notices
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
    assert len(
        [
            notice
            for notice in quote.notices
            if "本次未取得可累计的官方月费" in notice
        ]
    ) == 3


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
        api_registry(),
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

    assert [item.label for item in quote.pricing_scenarios] == [
        "按需",
        "1年全预付",
        "3年全预付",
    ]


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
        def preview(
            self, requirement: ServiceRequirement, default_region: str
        ) -> PreviewSelection:
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
    assert session.confirmation_text == (
        "当前区域没有完全相同的规格，已保留原配置，请重新修改。"
    )
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
        def preview(
            self, requirement: ServiceRequirement, default_region: str
        ) -> PreviewSelection:
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
async def test_missing_redis_size_becomes_customer_question_not_api_error() -> None:
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
    assert preview.selections[0].quantity == 2
    assert preview.confirmation_text is not None
    assert "每节点大概需要 1G、4G 还是 8G 内存" in preview.confirmation_text
    assert "型号由系统自动选择" in preview.confirmation_text


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
        def preview(
            self, requirement: ServiceRequirement, default_region: str
        ) -> PreviewSelection:
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


def test_missing_redis_capacity_generates_clickable_options() -> None:
    question = (
        "您已选 Redis 1 主 1 从，但还缺少单节点容量。"
        "每节点大概需要 1G、4G 还是 8G 内存？型号由系统自动选择。"
    )

    options = QuoteService._default_confirmation_options(question)

    assert [option.value for option in options] == ["1G", "4G", "8G"]


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

    await service._apply_confirmation_responses(
        intent, {question: "选择 x1e.xlarge"}
    )

    assert intent.services[0].requirements == {"vcpu": 4, "memory_gib": 16}
    assert intent.services[1].requirements == {"requested_model": "x1e.xlarge"}


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

        def preview(
            self, requirement: ServiceRequirement, default_region: str
        ) -> PreviewSelection:
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

        def preview(
            self, requirement: ServiceRequirement, default_region: str
        ) -> PreviewSelection:
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
    assert QuoteService._compact_customer_question(
        "Single-AZ 与主备自动故障切换要求冲突"
    ) == "您原选 Single-AZ，但它不提供主备自动切换；要自动切换需改为 Multi-AZ，是否同意？"
    assert QuoteService._compact_customer_question(
        "Application Load Balancer 不支持固定公网 IP"
    ) == "您要求 ALB 使用固定公网 IP，但 ALB 的 IP 会变化；是否改用支持固定 IP 的 NLB 或 Global Accelerator？"
    assert QuoteService._compact_customer_question(
        "Redis 整套 1G 与每个节点至少 8G 的要求冲突"
    ) == "您原填写 Redis 整套 1G、每节点 8G，两者不一致；请确认以哪个为准？"
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
        "Amazon S3 是区域型服务，不能使用“全球”作为报价区域，"
        "请确认该组件实际部署在哪个 AWS 区域。"
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
                "您要求 RDS MySQL 的配置为 10 核 40 GB，但 AWS 没有完全相同的型号，"
                "请在下方重新选择您需要的型号。"
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
            "您还没有指定 EKS 工作节点的 CPU 和内存，请在下方选择您需要的型号。",
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
                "您要求 Neptune 使用 db.example.large，但 AWS 当前区域没有这个型号，"
                "请在下方重新选择您需要的型号。"
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
    assert "purchase_option" not in intent.services[2].requirements


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
        def preview(
            self, requirement: ServiceRequirement, default_region: str
        ) -> PreviewSelection:
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


def test_late_business_issue_creates_one_follow_up_confirmation(tmp_path) -> None:
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

    follow_up = service._late_customer_confirmation(
        error,
        request=QuoteRequest(customer_request="Redis 8G", draft_id=draft_id),
        intent=intent,
    )

    assert follow_up.code == "late_customer_confirmation_required"
    assert follow_up.details["confirmation_round"] == 1
    assert len(follow_up.details["confirmation_items"][0]["options"]) == 2
    token = follow_up.details["confirmation_token"]
    assert store.get(token) is not None

    repeated = service._late_customer_confirmation(
        error,
        request=QuoteRequest(customer_request="Redis 8G", draft_id=draft_id),
        intent=intent,
    )
    assert repeated.code == "confirmation_answer_not_applied"
    assert "confirmation_token" not in repeated.details


def test_late_technical_issue_is_never_turned_into_customer_question(tmp_path) -> None:
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

    result = service._late_customer_confirmation(
        error,
        request=QuoteRequest(customer_request="Redis 8G", draft_id="technical001"),
        intent=intent,
    )

    assert result is error
    assert "confirmation_token" not in result.details


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
def test_late_bcm_failure_never_creates_customer_confirmation(
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

    result = service._late_customer_confirmation(
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

    preview = plugin.preview(
        ServiceRequirement(service=kind.value), "ap-southeast-1"
    )

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

    assert [(item.label, item.value) for item in options] == [
        ("采用 AWS 托管方案", "aws_managed"),
        ("保留原产品自建", "self_hosted"),
    ]


def test_workflow_controls_bypass_free_form_ai_revision() -> None:
    assert QuoteService._is_structured_workflow_answer("self_hosted")
    assert QuoteService._is_structured_workflow_answer(
        "选择 m7g.large；机器数量 3"
    )
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
            "_pending_architecture_decision" not in item.field_sources
            for item in intent.services
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
                field_sources={
                    "_customer_select_configuration": "customer_confirmation"
                },
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
        {
            "EC2 自建服务：请选择自建服务的机器台数和每台 EC2 配置。":
                "选择 m7g.large；机器数量 5"
        },
    )

    component = intent.services[0]
    assert component.quantity == 5
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
                field_sources={
                    "_pending_architecture_decision": "system_policy"
                },
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
    assert component.requirements["requested_model"] == "m7g.large"
    assert "_pending_architecture_decision" not in component.field_sources
    assert "_customer_select_configuration" not in component.field_sources


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
                        field_sources={
                            "_pending_architecture_decision": "system_policy"
                        },
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
    assert [
        option.model for option in preview.confirmation_items[0].dependent_options
    ] == ["t4g.small"]
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
