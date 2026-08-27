from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from datetime import UTC, datetime

from app.core.errors import ManualConfirmationRequired, QuoteError
from app.domain.models import (
    ConfirmationItem,
    ConfirmationOption,
    ConfirmationSessionResponse,
    ExecutionEvent,
    ExpertReview,
    ParsedIntent,
    PreviewSelection,
    PricingScenario,
    QuotePreviewResponse,
    QuoteRequest,
    QuoteResponse,
    QuoteStatus,
    ServiceRequirement,
)
from app.domain.structured_component_updates import (
    apply_component_update,
    decode_component_update,
)
from app.integrations.azure_intent import AzureIntentParser
from app.services.azure_plugins import (
    AZURE_REGION_BILINGUAL_NAMES,
    AZURE_REGION_FALLBACK,
    AzurePluginRegistry,
)
from app.services.confirmation_sessions import (
    CONFIGURATION_COMPONENT_DELETE,
    CONFIGURATION_COMPONENT_FEEDBACK_PREFIX,
    CONFIGURATION_COMPONENT_UPDATE_PREFIX,
    CONFIGURATION_FEEDBACK_QUESTION,
    ConfirmationSessionStore,
)

_AZURE_GLOBAL_REGION_LINE = re.compile(
    r"^\s*(?:\d{1,3}\s*[、.．):：-]\s*)?"
    r"(?:(?:默认|统一|整体|全部|所有|部署)\s*)?"
    r"(?:区域|地区|region)\s*"
    r"(?:为|是|选择|选用|使用|设为|定为|改为|改成|[:：])",
    re.IGNORECASE,
)
_AZURE_LOCATION_FIRST_REGION_LINE = re.compile(
    r"^\s*(?:\d{1,3}\s*[、.．):：-]\s*)?"
    r"(?P<label>[^\d,，;；|｜]{1,48}?)\s*(?:地区|区域|region)\s*[。.]?\s*$",
    re.IGNORECASE,
)
_AZURE_WORKLOAD_REGION_LINE = re.compile(
    r"^\s*(?:应用|系统|业务|工作负载|全部|统一|整体)\s*"
    r"(?:部署|运行|放置|位于)\s*(?:到|在|至)?\s*",
    re.IGNORECASE,
)
_AZURE_COMMON_REGION_ALIASES: dict[str, tuple[str, ...]] = {
    "southeastasia": ("新加坡", "singapore", "东南亚"),
    "eastasia": ("香港", "hong kong", "东亚"),
    "japaneast": ("东京", "tokyo", "日本东部"),
    "japanwest": ("大阪", "osaka", "日本西部"),
    "koreacentral": ("首尔", "seoul", "韩国中部"),
    "australiaeast": ("悉尼", "sydney", "澳大利亚东部"),
    "eastus": ("美国东部", "us east"),
    "westus": ("美国西部", "us west"),
    "westeurope": ("西欧", "荷兰", "west europe"),
    "northeurope": ("北欧", "爱尔兰", "north europe"),
    "germanywestcentral": ("法兰克福", "德国中西部"),
    "uksouth": ("伦敦", "英国南部"),
}


class AzureQuoteService:
    """Azure counterpart to QuoteService with isolated AI and official adapters."""

    def __init__(
        self,
        parser: AzureIntentParser,
        plugins: AzurePluginRegistry,
        confirmation_sessions: ConfirmationSessionStore | None = None,
        ai_provider: str = "Configured AI",
    ):
        self._parser = parser
        self._plugins = plugins
        self._confirmation_sessions = confirmation_sessions
        if confirmation_sessions is not None and confirmation_sessions.cloud_provider != "azure":
            raise ValueError("Azure 报价系统只能连接 Azure 专用确认存储")
        self._ai_provider = ai_provider
        self._drafts: dict[str, tuple[str, ParsedIntent]] = {}

    async def identify_sales_region(self, text: str) -> dict[str, object]:
        """Resolve one quote-wide Azure region before component AI starts."""

        options = await self._official_region_options()
        region, declaration_found = self._explicit_sales_region(text, options)
        if region is not None:
            return {
                "regions": [region],
                "requires_confirmation": False,
                "reason": "客户原文已明确给出统一 Azure 部署地区。",
                "options": options,
            }
        return {
            "regions": [],
            "requires_confirmation": True,
            "reason": (
                "客户填写的地区无法映射到当前 Microsoft 官方区域，请销售确认。"
                if declaration_found
                else "客户原文未填写统一 Azure 部署地区，请销售确认。"
            ),
            "options": options,
        }

    async def _official_region_options(self) -> list[tuple[str, str]]:
        loader = getattr(self._plugins, "region_options", None)
        if not callable(loader):
            return list(AZURE_REGION_FALLBACK)
        options = await loader()
        cleaned = [
            (str(code).strip().casefold(), str(label).strip())
            for code, label in options
            if str(code).strip() and str(label).strip()
        ]
        return cleaned or list(AZURE_REGION_FALLBACK)

    async def configuration_field_options(
        self,
        requirement: ServiceRequirement,
    ) -> dict[str, object]:
        return await self._plugins.configuration_field_options(requirement)

    def professionalize_confirmation_session(
        self,
        session: ConfirmationSessionResponse,
    ) -> ConfirmationSessionResponse:
        """Upgrade pending legacy links to the current customer-facing copy."""

        configurations = {
            item.component_id: item for item in session.configuration_items
        }
        polished_items: list[ConfirmationItem] = []
        for item in session.confirmation_items:
            configuration = configurations.get(item.component_id or "")
            requirement = None
            selection = None
            if configuration is not None:
                requirement = ServiceRequirement(
                    service=configuration.service,
                    calculator_service_name=configuration.display_name,
                    region=configuration.region,
                    quantity=configuration.quantity,
                    requirements=configuration.requirements,
                    source_text=configuration.source_text,
                )
                selection = PreviewSelection(
                    component_id=configuration.component_id,
                    service=item.service or configuration.service,
                    display_name=configuration.display_name,
                    region=configuration.region or "未指定区域",
                    quantity=configuration.quantity,
                    requirements=configuration.requirements,
                    source_text=configuration.source_text,
                )
            question = self._plain_customer_question(
                item.question,
                selection,
                requirement,
            )
            polished_items.append(item.model_copy(update={"question": question}))
        return session.model_copy(
            update={
                "confirmation_text": "为确保报价准确，请确认以下配置选项。",
                "confirmation_items": polished_items,
            }
        )

    async def preview(self, request: QuoteRequest, reporter=None) -> QuotePreviewResponse:
        if request.cloud_provider != "azure":
            raise QuoteError(
                "cloud_provider_boundary_violation",
                "非 Azure 请求已被 Azure 报价系统拒绝。",
                {"provider": "azure"},
                409,
            )
        if request.draft_id and request.draft_id.startswith("aw"):
            raise QuoteError(
                "cloud_provider_boundary_violation",
                "检测到 AWS 草稿被提交到 Azure 报价系统，已阻止处理。",
                {"provider": "azure"},
                409,
            )
        official_region_options = await self._official_region_options()
        official_regions = {code for code, _ in official_region_options}
        if request.sales_region and request.sales_region not in official_regions:
            raise ManualConfirmationRequired(
                "所选地区不是当前可用的 Microsoft Azure 官方区域，请销售重新选择。",
                code="azure_sales_region_confirmation_required",
                options=sorted(official_regions),
            )
        # The sales page normally performs this preflight. Keep the API as a
        # second trust boundary so a direct request cannot start component AI
        # or pricing before its quote-wide Azure region has been confirmed.
        if (
            isinstance(self._parser, AzureIntentParser)
            and not request.sales_region
            and not request.draft_id
        ):
            region_result = await self.identify_sales_region(request.customer_request)
            detected = [
                str(region)
                for region in region_result.get("regions", [])
                if isinstance(region, str) and region in official_regions
            ]
            if bool(region_result.get("requires_confirmation")) or len(detected) != 1:
                raise ManualConfirmationRequired(
                    "客户地区缺失或不是可用的 Azure 官方区域，请销售先在内部页面确认地区。",
                    code="azure_sales_region_confirmation_required",
                    options=sorted(official_regions),
                )
            request = request.model_copy(update={"sales_region": detected[0]})
        trace: list[ExecutionEvent] = []

        async def report(stage: str, message: str) -> None:
            trace.append(ExecutionEvent(stage=stage, message=message))
            if reporter is not None:
                await reporter(stage, message)

        intent = await self._intent_for_request(request, report)
        effective_sales_region = request.sales_region or self._single_shared_region(intent)
        self._apply_sales_region(intent, effective_sales_region)
        self._repair_legacy_vm_shape_conflicts(intent)
        selections = []

        async def preview_one(index: int, service):
            try:
                plugin = self._plugins.get(service.service)
            except ManualConfirmationRequired as exc:
                return index, PreviewSelection(
                    component_id=str(index),
                    service=service.service,
                    display_name=service.calculator_service_name or service.service,
                    region=service.region or "未指定区域",
                    quantity=service.quantity,
                    requirements=service.requirements,
                    source_text=service.source_text,
                    candidates=[],
                    status="unsupported",
                    issue_message=exc.message,
                    selection_reason=exc.message,
                )
            await report(
                "official_start",
                f"组件 {index + 1}｜{plugin.display_name}｜正在查询 Microsoft 官方目录",
            )
            try:
                selection = await plugin.preview(service, request, str(index))
            except ManualConfirmationRequired as exc:
                selection = plugin._confirmation_preview(
                    service, str(index), exc.message
                ).model_copy(
                    update={
                        "requires_confirmation": False,
                        "confirmation_reason": None,
                        "status": "technical_issue",
                        "issue_message": exc.message,
                        "issue_code": exc.code,
                        "issue_category": "technical",
                    }
                )
            except QuoteError as exc:
                selection = plugin._confirmation_preview(
                    service, str(index), exc.message
                ).model_copy(
                    update={
                        "requires_confirmation": False,
                        "confirmation_reason": None,
                        "status": "technical_issue",
                        "issue_message": exc.message,
                    }
                )
            question_text = str(selection.confirmation_reason or selection.issue_message or "")
            if (
                selection.requires_confirmation
                and not selection.candidates
                and not self._is_region_question(question_text)
            ):
                # Azure customer questions must be selectable. If Microsoft
                # returns no official choices, keep retrying/isolating it as a
                # system issue instead of presenting a blank text box.
                selection = selection.model_copy(
                    update={
                        "requires_confirmation": False,
                        "confirmation_reason": None,
                        "status": "technical_issue",
                        "issue_message": "Microsoft 官方目录暂未返回可选配置，系统将自动重试。",
                        "issue_code": (
                            selection.issue_code or "azure_official_choices_unavailable"
                        ),
                        "issue_category": "catalog_mapping",
                    }
                )
            await report(
                "official_done",
                f"组件 {index + 1}｜{plugin.display_name}｜Microsoft 官方核验完成",
            )
            return index, selection

        results = await asyncio.gather(
            *(preview_one(index, service) for index, service in enumerate(intent.services))
        )
        for index, selection in sorted(results):
            selections.append(selection)
            if selection.status == "ready" and selection.selected_model:
                intent.services[index].requirements["_review_selected_model"] = (
                    selection.selected_model
                )
                selected_candidate = next(
                    (
                        candidate
                        for candidate in selection.candidates
                        if candidate.model == selection.selected_model
                    ),
                    None,
                )
                if selected_candidate:
                    intent.services[index].requirements["_review_selected_specifications"] = (
                        selected_candidate.specifications
                    )
            elif selection.status in {"technical_issue", "unsupported"}:
                intent.services[index].requirements["_quote_skip_reason"] = str(
                    selection.issue_message or selection.selection_reason
                )

        has_global_region_question = any(
            ambiguity.strip() and self._is_region_question(ambiguity)
            for ambiguity in intent.ambiguities
        )
        has_component_region_question = any(
            selection.requires_confirmation
            and self._is_region_question(
                str(selection.confirmation_reason or selection.issue_message or "")
            )
            for selection in selections
        )
        region_options = (
            await self._plugins.region_options()
            if has_global_region_question or has_component_region_question
            else []
        )
        questions: list[tuple[str, str | None, str | None, list[ConfirmationOption]]] = []
        unresolved_customer_ambiguities: list[str] = []
        for ambiguity in intent.ambiguities:
            if ambiguity.strip():
                options = self._region_options(ambiguity, region_options)
                if options:
                    questions.append(
                        (
                            self._plain_customer_question(ambiguity.strip()),
                            None,
                            None,
                            options,
                        )
                    )
                else:
                    # Azure customer questions must always be backed by finite
                    # official choices. Keep free-form ambiguities internal.
                    unresolved_customer_ambiguities.append(ambiguity.strip())
        for selection in selections:
            if not selection.requires_confirmation:
                continue
            question = str(
                selection.confirmation_reason or selection.issue_message or "请确认配置。"
            )
            is_region_question = self._is_region_question(question)
            is_supported_region_choice = (
                selection.issue_code == "azure_service_region_not_supported"
            )
            if has_global_region_question and is_region_question:
                continue
            if is_supported_region_choice:
                options = [
                    ConfirmationOption(
                        label=(
                            f"{candidate.family}（{candidate.model}）"
                            if candidate.family != candidate.model
                            else candidate.model
                        ),
                        value=candidate.model,
                        specifications=candidate.specifications,
                    )
                    for candidate in selection.candidates
                ]
            elif is_region_question:
                options = self._region_options(question, region_options)
            else:
                options = [
                    ConfirmationOption(
                        label=candidate.model,
                        value=f"选择 {candidate.model}",
                        model=candidate.model,
                        specifications=candidate.specifications,
                        monthly_catalog_cost=candidate.monthly_catalog_cost,
                    )
                    for candidate in selection.candidates
                ]
            requirement = (
                intent.services[int(selection.component_id)]
                if selection.component_id is not None
                and selection.component_id.isdigit()
                and int(selection.component_id) < len(intent.services)
                else None
            )
            questions.append(
                (
                    self._plain_customer_question(question, selection, requirement),
                    selection.component_id,
                    selection.service,
                    options,
                )
            )
        deduplicated = []
        seen: set[tuple[str, str | None]] = set()
        for item in questions:
            key = (item[0].strip().casefold(), item[1])
            if key not in seen:
                seen.add(key)
                deduplicated.append(item)
        confirmation_items = [
            ConfirmationItem(
                question=question,
                answer_key=self._confirmation_answer_key(component_id, question),
                component_id=component_id,
                service=service,
                options=options,
                selection_mode=(
                    "catalog"
                    if any(option.model for option in options) or len(options) > 6
                    else "buttons"
                ),
            )
            for question, component_id, service, options in deduplicated
        ]
        if any(
            self._is_region_question(item.question) and not item.options
            for item in confirmation_items
        ):
            raise QuoteError(
                "azure_region_options_empty",
                "Azure 官方区域选项暂未准备完成，系统不会向客户显示空白输入框。",
                {"provider": "azure"},
                503,
            )
        confirmation_text = (
            "为确保报价准确，请确认以下配置选项：\n"
            + "\n".join(
                f"{index + 1}. {item.question}" for index, item in enumerate(confirmation_items)
            )
            if confirmation_items
            else None
        )
        draft_id = request.draft_id or f"az{uuid.uuid4().hex[:10]}"
        self._drafts[draft_id] = (
            request.customer_request,
            intent.model_copy(deep=True),
        )
        confirmation_token = None
        configuration_review_required = False
        unsupported_components = sum(selection.status == "unsupported" for selection in selections)
        technical_components = sum(
            selection.status == "technical_issue" for selection in selections
        )
        internal_issue_count = (
            unsupported_components + technical_components + len(unresolved_customer_ambiguities)
        )
        sales_validation_required = internal_issue_count > 0
        sales_validation_message = (
            f"还有 {internal_issue_count} 项内部官方配置未通过核验，"
            "请在销售端同步并重新核验；通过前不会生成客户链接。"
            if sales_validation_required
            else None
        )
        customer_link_publication_allowed = not sales_validation_required
        if self._confirmation_sessions is not None and customer_link_publication_allowed:
            if confirmation_items:
                confirmation_token = self._confirmation_sessions.create_or_replace(
                    draft_id=draft_id,
                    customer_request=request.customer_request,
                    customer_summary=intent.customer_summary,
                    intent=intent,
                    confirmation_text=confirmation_text or "请确认 Azure 配置。",
                    items=confirmation_items,
                )
            else:
                # Keep the Azure customer journey identical to the proven AWS
                # flow: even when there are no questions, create one stable
                # customer URL for the final, price-free configuration table.
                # Official pricing cannot start until the customer approves it.
                confirmation_token = self._confirmation_sessions.create_or_replace(
                    draft_id=draft_id,
                    customer_request=request.customer_request,
                    customer_summary=intent.customer_summary,
                    intent=intent,
                    confirmation_text="请确认最终配置清单，确认后系统才会开始报价。",
                    items=[],
                )
                confirmation_token = self._confirmation_sessions.prepare_configuration_review(
                    draft_id=draft_id,
                    intent=intent,
                )
                configuration_review_required = confirmation_token is not None

        expert_review = ExpertReview(
            run_id=f"azure-review-{uuid.uuid4().hex[:8]}",
            provider=self._ai_provider,
            status="awaiting_customer" if confirmation_items else "ready",
            ai_calls=len(intent.services),
            components=len(selections),
            official_checks=sum(selection.status != "unsupported" for selection in selections),
            customer_questions=len(confirmation_items),
            unsupported_components=unsupported_components,
            safeguards=[
                "销售编号作为组件硬边界",
                "每个组件使用独立 AI 上下文",
                "服务名、SKU 与 Meter 由 Microsoft 官方目录核验",
                "无法唯一匹配时停止报价",
            ],
        )
        return QuotePreviewResponse(
            draft_id=draft_id,
            customer_summary=intent.customer_summary,
            selections=selections,
            notices=[
                "价格来源：Microsoft Azure Retail Prices API（公开零售价）",
                "未连接 Azure 订阅时，VM 规格自动选型需要客户提供官方 SKU。",
            ],
            confirmation_text=confirmation_text,
            confirmation_items=confirmation_items,
            confirmation_token=confirmation_token,
            configuration_review_required=configuration_review_required,
            sales_validation_required=sales_validation_required,
            sales_validation_message=sales_validation_message,
            execution_trace=trace,
            expert_review=expert_review,
        )

    async def create_quote(self, request: QuoteRequest, reporter=None) -> QuoteResponse:
        if request.cloud_provider != "azure":
            raise QuoteError(
                "cloud_provider_boundary_violation",
                "非 Azure 请求已被 Azure 报价系统拒绝。",
                {"provider": "azure"},
                409,
            )
        if request.draft_id and request.draft_id.startswith("aw"):
            raise QuoteError(
                "cloud_provider_boundary_violation",
                "检测到 AWS 草稿被提交到 Azure 报价系统，已阻止报价。",
                {"provider": "azure"},
                409,
            )
        if request.draft_id and self._confirmation_sessions is not None:
            review_status = self._confirmation_sessions.status_by_draft(request.draft_id)
            if review_status is not None and review_status != "approved":
                raise ManualConfirmationRequired(
                    "客户尚未确认最终配置清单，系统不会提前开始报价",
                    code="configuration_review_required",
                    draft_id=request.draft_id,
                    confirmation_status=review_status,
                )
        intent = await self._intent_for_request(request, reporter)
        self._repair_legacy_vm_shape_conflicts(intent)
        unresolved = [item for item in intent.ambiguities if item.strip()]
        if unresolved:
            raise ManualConfirmationRequired(
                "Azure 配置仍有客户待确认项，禁止生成不完整报价。",
                code="azure_confirmation_required",
                confirmation_text="您好，请确认：\n"
                + "\n".join(f"{index + 1}. {item}" for index, item in enumerate(unresolved)),
                draft_id=request.draft_id,
            )
        selections = []
        priced_lines = []
        upfront_cost = 0.0
        trace: list[ExecutionEvent] = []
        partial_notices: list[str] = []
        for index, service in enumerate(intent.services):
            skip_reason = str(service.requirements.get("_quote_skip_reason") or "")
            if skip_reason:
                partial_notices.append(
                    f"{service.calculator_service_name or service.service} 未计入合计："
                    f"{skip_reason}"
                )
                continue
            plugin = self._plugins.get(service.service)
            message = f"组件 {index + 1}｜{plugin.display_name}｜正在执行 Microsoft 官方核价"
            if reporter:
                await reporter("official_start", message)
            trace.append(ExecutionEvent(stage="official_start", message=message))
            component = await plugin.quote(service, request, index)
            selections.append(component.selection)
            priced_lines.extend(component.priced_lines)
            upfront_cost += component.upfront_cost
            done = f"组件 {index + 1}｜{plugin.display_name}｜Microsoft 官方核价完成"
            if reporter:
                await reporter("official_done", done)
            trace.append(ExecutionEvent(stage="official_done", message=done))
        if not selections:
            raise ManualConfirmationRequired(
                "没有任何 Azure 组件完成官方核价。",
                code="azure_no_priced_components",
            )
        if request.draft_id and self._confirmation_sessions is not None:
            self._confirmation_sessions.complete_by_draft(request.draft_id)
        total_cost = sum(line.cost for line in priced_lines)
        label = self._pricing_label(request)
        quote_id = f"azure-{uuid.uuid4().hex[:10]}"
        scenario = PricingScenario(
            label=label,
            pricing_mode=request.azure_pricing_mode,
            reserved_term_years=request.azure_term_years,
            quote_id=quote_id,
            total_cost=total_cost,
            upfront_cost=upfront_cost,
            priced_lines=priced_lines,
        )
        return QuoteResponse(
            quote_id=quote_id,
            status=QuoteStatus.QUOTED,
            customer_summary=intent.customer_summary,
            selections=selections,
            priced_lines=priced_lines,
            total_cost=total_cost,
            upfront_cost=upfront_cost,
            currency="USD",
            rate_type=label,
            rate_timestamp=datetime.now(UTC),
            notices=[
                "Microsoft Azure 官方公开零售价；不含 EA/MCA/CSP 协议折扣、税费和抵扣。",
                "未提供用量的组件仅展示官方单位参考价，不计入月度合计。",
                *partial_notices,
            ],
            execution_trace=trace,
            pricing_source="Microsoft Azure Retail Prices API",
            source_url="https://prices.azure.com/api/retail/prices",
            pricing_scenarios=[scenario],
        )

    async def _intent_for_request(self, request: QuoteRequest, reporter=None) -> ParsedIntent:
        cached = self._drafts.get(request.draft_id or "")
        if cached is None and request.draft_id and self._confirmation_sessions is not None:
            cached = self._confirmation_sessions.restore_draft(request.draft_id)
        if cached and cached[0] == request.customer_request:
            intent = cached[1].model_copy(deep=True)
            responses = dict(request.confirmation_responses)
            component_feedback: dict[int, str] = {}
            component_updates: dict[int, dict[str, object]] = {}
            for question in list(responses):
                if question.startswith(CONFIGURATION_COMPONENT_UPDATE_PREFIX):
                    component_id = question.removeprefix(CONFIGURATION_COMPONENT_UPDATE_PREFIX)
                    update = decode_component_update(responses.pop(question))
                    if component_id.isdigit() and update is not None:
                        component_updates[int(component_id)] = update
                    continue
                if not question.startswith(CONFIGURATION_COMPONENT_FEEDBACK_PREFIX):
                    continue
                component_id = question.removeprefix(CONFIGURATION_COMPONENT_FEEDBACK_PREFIX)
                feedback = responses.pop(question).strip()
                if component_id.isdigit() and feedback:
                    component_feedback[int(component_id)] = feedback
            if request.draft_id and self._confirmation_sessions is not None:
                partitioned, responses = self._confirmation_sessions.partition_answers_by_component(
                    request.draft_id, responses
                )
                for index, answers in partitioned.items():
                    if not (0 <= index < len(intent.services)):
                        continue
                    remaining = self._apply_component_answers(intent.services[index], answers)
                    if remaining:
                        component_feedback[index] = "\n".join(
                            f"问题：{question}\n客户回答：{answer}"
                            for question, answer in remaining.items()
                        )
            deleted_indices = {
                index
                for index, feedback in component_feedback.items()
                if feedback == CONFIGURATION_COMPONENT_DELETE
            }
            # Component ids belong to the configuration table the customer
            # saw. Apply edits before deletions so removing an earlier row can
            # never shift a later edit onto the wrong Azure component.
            for index, update in component_updates.items():
                if 0 <= index < len(intent.services):
                    previous = intent.services[index].model_copy(deep=True)
                    revised = apply_component_update(intent.services[index], update)
                    intent.services[index] = self._guard_vm_shape_revision(
                        previous,
                        revised,
                        revised.source_text,
                    )
            for index, feedback in component_feedback.items():
                if index in deleted_indices:
                    continue
                if 0 <= index < len(intent.services):
                    previous = intent.services[index].model_copy(deep=True)
                    revised = await self._parser.revise_component_from_feedback(
                        request.customer_request,
                        intent.services[index],
                        feedback,
                        reporter=reporter,
                    )
                    intent.services[index] = self._guard_vm_shape_revision(
                        previous,
                        revised,
                        feedback,
                    )
            for index in sorted(deleted_indices, reverse=True):
                if 0 <= index < len(intent.services):
                    del intent.services[index]

            addition_feedback = responses.pop(CONFIGURATION_FEEDBACK_QUESTION, "").strip()
            if addition_feedback:
                added = await self._parser.parse(addition_feedback, reporter=reporter)
                intent.services.extend(added.services)
                intent.ambiguities.extend(added.ambiguities)
                names = [
                    service.calculator_service_name or service.service
                    for service in intent.services
                ]
                intent.customer_summary = (
                    f"已识别 {len(intent.services)} 项 Azure 配置：" + "、".join(names)
                )
            self._apply_global_answers(intent, responses)
            intent.ambiguities = [
                ambiguity
                for ambiguity in intent.ambiguities
                if not self._answered(ambiguity, responses)
            ]
            return intent
        return await self._parser.parse(request.customer_request, reporter=reporter)

    @staticmethod
    def _region_mentions(
        value: str,
        options: list[tuple[str, str]],
    ) -> list[str]:
        """Return distinct official Azure regions literally present in text."""

        folded = value.casefold()
        alias_owners: dict[str, set[str]] = {}
        for code, label in [*options, *AZURE_REGION_FALLBACK]:
            normalized_code = str(code).strip().casefold()
            if not normalized_code:
                continue
            for alias in (normalized_code, str(label).strip().casefold()):
                if alias:
                    alias_owners.setdefault(alias, set()).add(normalized_code)
        for code, aliases in _AZURE_COMMON_REGION_ALIASES.items():
            for alias in aliases:
                alias_owners.setdefault(alias.casefold(), set()).add(code)
        for code, (chinese, english) in AZURE_REGION_BILINGUAL_NAMES.items():
            alias_owners.setdefault(chinese.casefold(), set()).add(code)
            alias_owners.setdefault(english.casefold(), set()).add(code)

        positions: list[tuple[int, str]] = []
        for alias, owners in alias_owners.items():
            if len(owners) != 1:
                continue
            code = next(iter(owners))
            if code not in {item[0] for item in options}:
                continue
            if re.fullmatch(r"[a-z0-9 -]+", alias):
                pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
                positions.extend((match.start(), code) for match in re.finditer(pattern, folded))
            else:
                start = 0
                while True:
                    position = folded.find(alias, start)
                    if position < 0:
                        break
                    positions.append((position, code))
                    start = position + len(alias)

        regions: list[str] = []
        for _, region in sorted(positions, key=lambda item: item[0]):
            if region not in regions:
                regions.append(region)
        return regions

    @classmethod
    def _explicit_sales_region(
        cls,
        text: str,
        options: list[tuple[str, str]],
    ) -> tuple[str | None, bool]:
        """Read only a workload-wide declaration, never a component-local one."""

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        candidates: list[str] = []
        declaration_found = False
        for line in lines:
            region_source: str | None = None
            prefix = _AZURE_GLOBAL_REGION_LINE.search(line)
            location_first = _AZURE_LOCATION_FIRST_REGION_LINE.fullmatch(line)
            workload = _AZURE_WORKLOAD_REGION_LINE.search(line)
            if prefix:
                declaration_found = True
                region_source = line[prefix.end():]
            elif location_first:
                declaration_found = True
                region_source = location_first.group("label")
            elif workload:
                declaration_found = True
                region_source = line[workload.end():]
            if region_source is None:
                continue
            mentions = cls._region_mentions(region_source, options)
            if len(mentions) != 1:
                return None, True
            if mentions[0] not in candidates:
                candidates.append(mentions[0])

        # Sales commonly places one short region heading before the numbered
        # component list. Treat only that first-line form as global context.
        if not declaration_found and len(lines) >= 2:
            first = lines[0]
            numbered_rows = sum(
                bool(re.match(r"^\s*\d{1,3}\s*[、.．):：-]", line))
                for line in lines[1:]
            )
            if numbered_rows and len(first) <= 48:
                if re.match(r"^\s*\d{1,3}\s*[、.．):：-]", first) is None:
                    mentions = cls._region_mentions(first, options)
                    if len(mentions) == 1:
                        declaration_found = True
                        candidates.append(mentions[0])

        unique = list(dict.fromkeys(candidates))
        return (unique[0], True) if len(unique) == 1 else (None, declaration_found)

    @staticmethod
    def _apply_sales_region(intent: ParsedIntent, sales_region: str | None) -> None:
        """Share only the quote-wide region; keep every service payload isolated."""

        if not sales_region:
            return
        regional = [service for service in intent.services if service.service != "front_door"]
        for service in regional:
            if service.region is not None:
                continue
            service.region = sales_region
            service.field_sources["region"] = "sales_confirmation"
            if service.service == "bandwidth":
                service.requirements["source_region"] = sales_region
                service.field_sources["requirements.source_region"] = "sales_confirmation"
        if regional and all(service.region for service in regional):
            intent.ambiguities = [
                ambiguity
                for ambiguity in intent.ambiguities
                if "这些区域型服务" not in ambiguity
            ]

    @staticmethod
    def _single_shared_region(intent: ParsedIntent) -> str | None:
        """Recover one already-confirmed region for older Azure links."""

        regions = list(
            dict.fromkeys(
                str(service.region).casefold()
                for service in intent.services
                if service.service != "front_door"
                and service.region
                and str(service.region).casefold() != "global"
            )
        )
        return regions[0] if len(regions) == 1 else None

    @staticmethod
    def _guard_vm_shape_revision(
        previous: ServiceRequirement,
        revised: ServiceRequirement,
        feedback: str,
    ) -> ServiceRequirement:
        """Never attach a newly requested VM shape to an unchanged old SKU."""

        if revised.service != "azure_vm":
            return revised
        explicit_sku = re.search(r"\bStandard_[A-Za-z0-9_-]+\b", feedback, re.I)
        if explicit_sku:
            revised.requirements["requested_sku"] = explicit_sku.group(0)
            revised.requirements.pop("vcpu", None)
            revised.requirements.pop("memory_gib", None)
            revised.requirements.pop("_review_selected_model", None)
            revised.requirements.pop("_review_selected_specifications", None)
            return revised
        old_shape = (
            previous.requirements.get("vcpu"),
            previous.requirements.get("memory_gib"),
        )
        new_shape = (
            revised.requirements.get("vcpu"),
            revised.requirements.get("memory_gib"),
        )
        if new_shape == old_shape or new_shape == (None, None):
            return revised
        revised.requirements.pop("requested_sku", None)
        revised.requirements.pop("sku_name", None)
        revised.requirements.pop("_review_selected_model", None)
        revised.requirements.pop("_review_selected_specifications", None)
        revised.requirements["_customer_select_official_sku"] = True
        return revised

    @staticmethod
    def _repair_legacy_vm_shape_conflicts(intent: ParsedIntent) -> None:
        """Upgrade drafts saved before VM edits were checked transactionally."""

        shape_pattern = re.compile(
            r"(\d+(?:\.\d+)?)\s*(?:核|vcpus?|vcpu)\s*[,，/\s·]*"
            r"(\d+(?:\.\d+)?)\s*(?:gib|gb|g)",
            re.I,
        )
        for component in intent.services:
            if component.service != "azure_vm":
                continue
            requirements = component.requirements
            if not requirements.get("requested_sku"):
                continue
            if "客户最新修改" not in component.source_text:
                continue
            source_shapes = shape_pattern.findall(component.source_text)
            if not source_shapes:
                continue
            original_vcpu, original_memory = map(float, source_shapes[-1])
            current_vcpu = requirements.get("vcpu")
            current_memory = requirements.get("memory_gib")
            if not isinstance(current_vcpu, (int, float)) or not isinstance(
                current_memory, (int, float)
            ):
                continue
            if float(current_vcpu) == original_vcpu and float(current_memory) == original_memory:
                continue
            requirements.pop("requested_sku", None)
            requirements.pop("sku_name", None)
            requirements.pop("_review_selected_model", None)
            requirements.pop("_review_selected_specifications", None)
            requirements["_customer_select_official_sku"] = True

    @staticmethod
    def _apply_component_answers(
        component: ServiceRequirement,
        answers: dict[str, str],
    ) -> dict[str, str]:
        """Apply structured catalog choices without asking AI to reinterpret them."""

        remaining: dict[str, str] = {}
        for question, answer in answers.items():
            if any(
                marker in question.casefold()
                for marker in ("区域", "地区", "地域", "region")
            ):
                compact = answer.strip().casefold()
                if compact.startswith("选择 "):
                    compact = compact.removeprefix("选择 ").strip()
                code_match = re.search(r"\(([a-z][a-z0-9-]{2,30})\)\s*$", compact)
                region = code_match.group(1) if code_match else compact
                if re.fullmatch(r"[a-z][a-z0-9-]{2,30}", region):
                    component.region = region
                    component.field_sources["region"] = "customer_confirmation"
                    if component.service == "bandwidth":
                        component.requirements["source_region"] = region
                        component.field_sources["requirements.source_region"] = (
                            "customer_confirmation"
                        )
                    component.locked_fields = sorted(set(component.locked_fields) | {"region"})
                    continue
            model_match = re.fullmatch(r"选择\s+(.+)", answer.strip())
            if model_match and any(
                marker in question.casefold() for marker in ("型号", "sku", "配置", "规格")
            ):
                model = model_match.group(1).strip()
                if model:
                    component.requirements["requested_sku"] = model
                    component.requirements.pop("_customer_select_official_sku", None)
                    component.requirements.pop("_review_selected_model", None)
                    component.requirements.pop("_review_selected_specifications", None)
                    if component.service == "azure_vm":
                        component.requirements.pop("vcpu", None)
                        component.requirements.pop("memory_gib", None)
                    component.field_sources["requirements.requested_sku"] = "customer_confirmation"
                    component.locked_fields = sorted(
                        set(component.locked_fields) | {"requirements.requested_sku"}
                    )
                    continue
            remaining[question] = answer
        return remaining

    @staticmethod
    def _apply_global_answers(intent: ParsedIntent, responses: dict[str, str]) -> None:
        aliases = {
            "新加坡": "southeastasia",
            "东京": "japaneast",
            "香港": "eastasia",
            "伦敦": "uksouth",
            "美国东部": "eastus",
            "美国西部": "westus",
            "西欧": "westeurope",
        }
        for question, answer in responses.items():
            if not any(
                marker in question.casefold()
                for marker in ("区域", "地区", "地域", "region")
            ):
                continue
            compact = answer.strip().casefold()
            region = next((code for label, code in aliases.items() if label in answer), None)
            if region is None and re.fullmatch(r"[a-z][a-z0-9-]{2,30}", compact):
                region = compact
            if not region:
                continue
            for service in intent.services:
                if not service.region:
                    service.region = region
                    service.field_sources["region"] = "customer_confirmation"
                    service.locked_fields = sorted(set(service.locked_fields) | {"region"})

    @staticmethod
    def _plain_customer_question(
        question: str,
        selection: PreviewSelection | None = None,
        requirement: ServiceRequirement | None = None,
    ) -> str:
        """Turn catalog language into one short question a customer can answer."""

        text = question.strip()
        folded = text.casefold()
        service = (selection.service if selection is not None else "") or (
            requirement.service if requirement is not None else ""
        )
        service_names = {
            "azure_vm": "Azure 云服务器",
            "managed_disks": "Azure 云硬盘",
            "azure_sql": "Azure SQL 数据库",
            "azure_postgresql": "Azure PostgreSQL 数据库",
            "azure_mysql": "Azure MySQL 数据库",
            "azure_cache": "Azure Redis 缓存",
            "blob_storage": "Azure 对象存储",
            "load_balancer": "Azure 负载均衡",
            "application_gateway": "Azure 应用网关",
            "front_door": "Azure Front Door",
            "bandwidth": "Azure 公网流量",
            "aks": "Azure 容器服务",
            "monitor": "Azure 日志监控",
            "api_management": "Azure API 管理",
            "azure_functions": "Azure 函数服务",
            "azure_event_hubs": "Azure Event Hubs 消息服务",
        }
        display_name = service_names.get(
            service,
            str(
                (selection.display_name if selection is not None else None)
                or (requirement.calculator_service_name if requirement is not None else None)
                or "这项 Azure 服务"
            ),
        )

        if any(marker in folded for marker in ("区域", "地区", "地域", "region")):
            if any(marker in folded for marker in ("不可用", "不支持", "没有", "未找到")):
                return f"{display_name} 在当前区域不可用，请选择支持该服务的 Azure 区域。"
            if selection is not None:
                return f"请选择 {display_name} 的部署区域。"
            return "请选择本次方案的 Azure 部署区域。"

        requirements = requirement.requirements if requirement is not None else {}
        if service == "azure_vm":
            vcpu = requirements.get("vcpu")
            memory = requirements.get("memory_gib")
            if isinstance(vcpu, (int, float)) and isinstance(memory, (int, float)):
                return (
                    f"请选择符合 {float(vcpu):g} vCPU、{float(memory):g} GiB 内存需求的 "
                    "Azure 官方实例规格（SKU）。"
                )
            return "请选择 Azure 虚拟机的官方实例规格（SKU）。"
        if service == "managed_disks":
            return "请选择 Azure 托管磁盘的容量及性能层级（SKU）。"
        if service == "monitor":
            return "请选择 Azure Monitor Logs 的日志数据层级。"
        if service == "azure_functions":
            return "请选择 Azure Functions 的托管方案与计费 SKU。"
        if service == "azure_event_hubs":
            return "请选择 Azure Event Hubs 的服务层级（SKU）。"
        if selection is not None:
            return f"请选择 {display_name} 的官方 SKU 或计费配置。"

        cleaned = text.replace("Microsoft 官方", "").replace("SKU", "型号")
        cleaned = cleaned.replace("计费项", "配置").replace("请从下方", "请从下面")
        cleaned = cleaned.replace("请确认", "请告诉我们")
        return cleaned

    @staticmethod
    def _confirmation_answer_key(component_id: str | None, question: str) -> str:
        scope = f"component-{component_id}" if component_id is not None else "global"
        digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]
        return f"azure-{scope}:{digest}"

    @staticmethod
    def _answered(question: str, responses: dict[str, str]) -> bool:
        normalized = "".join(question.split()).casefold()
        return any("".join(candidate.split()).casefold() == normalized for candidate in responses)

    @staticmethod
    def _region_options(
        question: str,
        regions: list[tuple[str, str]],
    ) -> list[ConfirmationOption]:
        if not AzureQuoteService._is_region_question(question):
            return []
        return [
            ConfirmationOption(label=f"{label}（{code}）", value=code) for code, label in regions
        ]

    @staticmethod
    def _is_region_question(question: str) -> bool:
        text = question.strip().casefold()
        if not any(marker in text for marker in ("区域", "地区", "地域", "region")):
            return False
        if any(marker in text for marker in ("不可用", "不支持", "没有", "未找到")):
            return False
        return bool(
            re.search(
                r"(?:请确认|缺少|未指定)[^。；？?]{0,48}(?:部署[^。；？?]{0,16})?"
                r"(?:区域|地区|地域|region)"
                r"|部署在(?:哪|哪个|哪一个)[^。；？?]{0,20}(?:区域|地区|地域|region)"
                r"|(?:区域|地区|地域|region)[^。；？?]{0,16}(?:缺少|未指定)",
                text,
            )
        )

    @staticmethod
    def _pricing_label(request: QuoteRequest) -> str:
        if request.azure_pricing_mode == "reservation":
            payment = "一次性支付" if request.azure_payment_option == "upfront" else "月付"
            return f"Azure {request.azure_term_years or 1} 年预留 · {payment}"
        if request.azure_pricing_mode == "savings_plan":
            return f"Azure {request.azure_term_years or 1} 年 Savings Plan"
        if request.azure_pricing_mode == "spot":
            return "Azure Spot"
        return "Azure Pay-as-you-go"
