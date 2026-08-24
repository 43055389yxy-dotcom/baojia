from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ServiceKind(StrEnum):
    EC2 = "ec2"
    RDS = "rds"
    REDIS = "redis"
    S3 = "s3"
    ELB = "elb"
    CLOUDFRONT = "cloudfront"
    ROUTE53 = "route53"
    WAF = "waf"
    SQS = "sqs"
    SES = "ses"
    CLOUDWATCH = "cloudwatch"
    EBS = "ebs"
    DATA_TRANSFER = "data_transfer"
    GLOBAL_ACCELERATOR = "global_accelerator"
    MSK = "msk"
    API_GATEWAY = "apigateway"
    SCHEDULER = "scheduler"
    OPENSEARCH = "opensearch"
    NAT_GATEWAY = "nat_gateway"


class QueryAction(StrEnum):
    DISCOVER_EC2_INSTANCES = "discover_ec2_instances"
    DISCOVER_RDS_INSTANCES = "discover_rds_instances"
    DISCOVER_REDIS_NODES = "discover_redis_nodes"


class QuoteStatus(StrEnum):
    QUOTED = "quoted"
    MANUAL_CONFIRMATION = "manual_confirmation"


class ExecutionEvent(BaseModel):
    stage: str
    message: str
    status: Literal["completed", "warning"] = "completed"


class ServiceRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Kept as a string on purpose: the Calculator agent must be able to quote a
    # newly-added AWS service without waiting for a backend enum release.
    service: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9_\-]+$")
    calculator_service_name: str | None = Field(default=None, min_length=2, max_length=160)
    # Stable customer-facing product identity. Several AWS products share a
    # Price List service code or a backend plugin (for example RDS/Aurora and
    # ALB/NLB), but that implementation detail must never collapse the product
    # the customer actually requested.
    product_identity: str | None = Field(
        default=None, min_length=2, max_length=100, pattern=r"^[a-z0-9_\-]+$"
    )
    region: str | None = None
    quantity: int = Field(default=1, ge=1, le=10000)
    hours_per_month: float = Field(default=730, gt=0, le=744)
    requirements: dict[str, Any] = Field(default_factory=dict)
    source_text: str = ""
    query_action: str | None = None
    # Exact snippets copied from this component's customer text. Keys use
    # ``region``, ``quantity`` or ``requirements.<field>`` paths.  This is
    # separate from ``field_sources`` so provenance type and human-verifiable
    # evidence cannot overwrite each other.
    field_evidence: dict[str, str] = Field(default_factory=dict)
    # Audit metadata is not sent to AWS adapters.  It makes every accepted
    # value traceable and prevents a later AI/default pass from silently
    # overwriting customer-written or customer-confirmed facts.
    field_sources: dict[str, str] = Field(default_factory=dict)
    locked_fields: list[str] = Field(default_factory=list)


class ParsedIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_summary: str
    services: list[ServiceRequirement] = Field(min_length=1, max_length=25)
    ambiguities: list[str] = Field(default_factory=list)


class QuoteRequest(BaseModel):
    cloud_provider: Literal["aws", "azure"] = "aws"
    customer_request: str = Field(min_length=3, max_length=12000)
    selected_models: dict[str, str] = Field(default_factory=dict)
    draft_id: str | None = Field(default=None, min_length=12, max_length=12)
    confirmation_responses: dict[str, str] = Field(default_factory=dict)
    pricing_mode: Literal[
        "on_demand", "standard_reserved", "convertible_reserved"
    ] = "on_demand"
    reserved_term_years: Literal[1, 3] | None = None
    reserved_term_options: list[Literal[1, 3]] | None = None
    payment_option: Literal["no_upfront", "partial_upfront", "all_upfront"] | None = None
    include_on_demand_scenario: bool = False
    utilization_percent: int = Field(default=100, ge=1, le=100)
    azure_pricing_mode: Literal[
        "pay_as_you_go", "reservation", "savings_plan", "spot"
    ] = "pay_as_you_go"
    azure_term_years: Literal[1, 3] | None = None
    azure_payment_option: Literal["monthly", "upfront"] | None = None

    @model_validator(mode="after")
    def normalize_sales_pricing_choice(self) -> QuoteRequest:
        if self.cloud_provider == "azure":
            if self.azure_pricing_mode in {"reservation", "savings_plan"}:
                self.azure_term_years = self.azure_term_years or 1
                self.azure_payment_option = self.azure_payment_option or "monthly"
            else:
                self.azure_term_years = None
                self.azure_payment_option = None
            return self
        if self.pricing_mode == "on_demand":
            self.reserved_term_years = None
            self.reserved_term_options = None
            self.payment_option = None
        else:
            terms = sorted(set(self.reserved_term_options or []))
            if not terms:
                terms = [self.reserved_term_years or 1]
            self.reserved_term_options = terms
            self.reserved_term_years = terms[0]
            self.payment_option = self.payment_option or "no_upfront"
        return self


class UsageLine(BaseModel):
    key: str = Field(pattern=r"^[A-Za-z0-9]{1,10}$")
    service_code: str
    usage_type: str
    operation: str
    amount: float = Field(gt=0)
    group: str | None = None


class ReferenceRate(BaseModel):
    """Official catalog unit price shown when the customer supplied no usage."""

    description: str
    unit: str
    unit_price: float = Field(ge=0)
    currency: Literal["USD"] = "USD"
    service_code: str
    usage_type: str
    operation: str


class SelectedResource(BaseModel):
    service: str
    display_name: str
    region: str
    model: str
    # Customer-requested resource/group count. Usage lines may already be
    # multiplied by this value, but the quote and Excel output must still show
    # it so a multi-instance total is never mistaken for a single-unit price.
    quantity: int = Field(default=1, ge=1)
    architecture: str
    specifications: dict[str, Any]
    official_product: dict[str, Any]
    rationale: str
    substitution_notice: str | None = None
    # Informational dependencies/coverage notes for the final quote. These do
    # not trigger customer confirmation and never add cost by themselves.
    remarks: list[str] = Field(default_factory=list)
    usage_lines: list[UsageLine] = Field(default_factory=list)
    reference_rates: list[ReferenceRate] = Field(default_factory=list)
    monthly_commitment_cost: float = Field(default=0, ge=0)
    upfront_commitment_cost: float = Field(default=0, ge=0)


class CandidateOption(BaseModel):
    model: str
    family: str
    specifications: dict[str, Any]
    monthly_catalog_cost: float | None = None
    catalog_currency: Literal["USD"] = "USD"
    rationale: str
    official_product: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class PreviewSelection(BaseModel):
    component_id: str
    service: str
    display_name: str
    region: str
    quantity: int = 1
    requirements: dict[str, Any] = Field(default_factory=dict)
    source_text: str = ""
    requested_model: str | None = None
    selected_model: str | None = None
    selection_reason: str = ""
    candidates: list[CandidateOption] = Field(default_factory=list)
    requires_confirmation: bool = False
    confirmation_reason: str | None = None
    status: Literal["ready", "customer_issue", "technical_issue", "unsupported"] = "ready"
    issue_message: str | None = None


class ConfirmationOption(BaseModel):
    label: str
    value: str
    model: str | None = None
    specifications: dict[str, Any] = Field(default_factory=dict)
    monthly_catalog_cost: float | None = None


class ConfirmationItem(BaseModel):
    question: str
    options: list[ConfirmationOption] = Field(default_factory=list)
    dependent_options: list[ConfirmationOption] = Field(default_factory=list)
    dependent_on_values: list[str] = Field(default_factory=list)
    component_id: str | None = None
    service: str | None = None
    selection_mode: Literal["buttons", "catalog"] = "buttons"


class ExpertReview(BaseModel):
    run_id: str
    provider: str
    mode: Literal["single_pass_read_only"] = "single_pass_read_only"
    status: Literal["ready", "awaiting_customer", "partial"]
    ai_calls: int = Field(default=0, ge=0)
    components: int = Field(default=0, ge=0)
    official_checks: int = Field(default=0, ge=0)
    customer_questions: int = Field(default=0, ge=0)
    unsupported_components: int = Field(default=0, ge=0)
    safeguards: list[str] = Field(default_factory=list)


class QuotePreviewResponse(BaseModel):
    draft_id: str
    customer_summary: str
    selections: list[PreviewSelection]
    notices: list[str] = Field(default_factory=list)
    confirmation_text: str | None = None
    confirmation_items: list[ConfirmationItem] = Field(default_factory=list)
    confirmation_token: str | None = None
    configuration_review_required: bool = False
    execution_trace: list[ExecutionEvent] = Field(default_factory=list)
    expert_review: ExpertReview | None = None


class ConfirmationSubmission(BaseModel):
    answers: dict[str, str] = Field(min_length=1)

    @field_validator("answers")
    @classmethod
    def non_empty_answers(cls, value: dict[str, str]) -> dict[str, str]:
        cleaned = {question: answer.strip() for question, answer in value.items() if answer.strip()}
        if not cleaned:
            raise ValueError("至少需要填写一项确认回复")
        return cleaned


class ConfigurationFeedbackSubmission(BaseModel):
    feedback: str | None = Field(default=None, max_length=4000)
    component_feedback: dict[str, str] = Field(default_factory=dict)
    component_updates: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("feedback")
    @classmethod
    def clean_feedback(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    @field_validator("component_feedback")
    @classmethod
    def clean_component_feedback(cls, value: dict[str, str]) -> dict[str, str]:
        return {
            str(component_id): feedback.strip()
            for component_id, feedback in value.items()
            if feedback.strip()
        }

    @field_validator("component_updates")
    @classmethod
    def clean_component_updates(
        cls, value: dict[str, dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        allowed_top_level = {"region", "quantity", "requirements"}
        cleaned: dict[str, dict[str, Any]] = {}
        for component_id, update in value.items():
            if not isinstance(update, dict):
                continue
            unknown = set(update) - allowed_top_level
            if unknown:
                raise ValueError("结构化配置包含不支持的字段")
            requirements = update.get("requirements", {})
            if not isinstance(requirements, dict):
                raise ValueError("组件参数格式不正确")
            safe_requirements: dict[str, Any] = {}
            for key, field_value in requirements.items():
                if not isinstance(key, str) or not key or key.startswith("_"):
                    raise ValueError("组件参数名称不正确")
                if field_value is not None and not isinstance(
                    field_value, (str, int, float, bool)
                ):
                    raise ValueError("组件参数只支持文字、数字或开关")
                safe_requirements[key] = field_value
            candidate: dict[str, Any] = {}
            if "region" in update:
                if not isinstance(update["region"], str) or not update["region"].strip():
                    raise ValueError("区域格式不正确")
                candidate["region"] = update["region"]
            if "quantity" in update:
                if isinstance(update["quantity"], bool) or not isinstance(
                    update["quantity"], int
                ):
                    raise ValueError("数量必须是整数")
                if not 1 <= update["quantity"] <= 10000:
                    raise ValueError("数量必须在 1 到 10000 之间")
                candidate["quantity"] = update["quantity"]
            if safe_requirements:
                candidate["requirements"] = safe_requirements
            if candidate:
                cleaned[str(component_id)] = candidate
        return cleaned

    @model_validator(mode="after")
    def has_feedback(self) -> "ConfigurationFeedbackSubmission":
        if not self.feedback and not self.component_feedback and not self.component_updates:
            raise ValueError("请至少填写一项需要修改的内容")
        return self


class ConfigurationReviewItem(BaseModel):
    component_id: str
    service: str
    display_name: str
    region: str | None = None
    quantity: int = 1
    selected_model: str | None = None
    official_specifications: dict[str, Any] = Field(default_factory=dict)
    pricing_status: Literal["ready", "unpriced"] = "ready"
    pricing_notice: str | None = None
    requirements: dict[str, Any] = Field(default_factory=dict)
    source_text: str = ""


class ConfirmationSessionResponse(BaseModel):
    token: str
    cloud_provider: Literal["aws", "azure"] = "aws"
    status: Literal[
        "pending",
        "submitted",
        "reviewing",
        "configuration_review",
        "approved",
        "completed",
    ]
    customer_summary: str
    confirmation_text: str
    confirmation_items: list[ConfirmationItem]
    answers: dict[str, str] = Field(default_factory=dict)
    configuration_items: list[ConfigurationReviewItem] = Field(default_factory=list)
    created_at: datetime
    submitted_at: datetime | None = None
class PricedLine(BaseModel):
    key: str
    service_code: str
    usage_type: str
    operation: str
    amount: float
    unit: str | None = None
    cost: float
    currency: Literal["USD"] = "USD"


class QuoteResponse(BaseModel):
    quote_id: str
    status: QuoteStatus
    customer_summary: str
    selections: list[SelectedResource]
    priced_lines: list[PricedLine]
    total_cost: float
    upfront_cost: float = 0
    currency: Literal["USD"] = "USD"
    rate_type: str
    rate_timestamp: datetime | None = None
    notices: list[str] = Field(default_factory=list)
    execution_trace: list[ExecutionEvent] = Field(default_factory=list)
    pricing_source: str = "AWS official pricing source"
    source_url: str | None = None
    share_url: str | None = None
    calculator_details: list[str] = Field(default_factory=list)
    pricing_scenarios: list[PricingScenario] = Field(default_factory=list)

    @field_validator("total_cost")
    @classmethod
    def non_negative_total(cls, value: float) -> float:
        if value < 0:
            raise ValueError("total_cost cannot be negative")
        return value


class PricingScenario(BaseModel):
    label: str
    pricing_mode: Literal[
        "on_demand", "standard_reserved", "convertible_reserved",
        "pay_as_you_go", "reservation", "savings_plan", "spot",
    ]
    reserved_term_years: Literal[1, 3] | None = None
    payment_option: Literal["no_upfront", "partial_upfront", "all_upfront"] | None = None
    quote_id: str
    total_cost: float = Field(ge=0)
    upfront_cost: float = Field(default=0, ge=0)
    currency: Literal["USD"] = "USD"
    priced_lines: list[PricedLine] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    status: QuoteStatus = QuoteStatus.MANUAL_CONFIRMATION
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
