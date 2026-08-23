from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime

from app.core.errors import ManualConfirmationRequired, QuoteError
from app.domain.models import (
    ConfirmationItem,
    ConfirmationOption,
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
from app.integrations.azure_intent import AzureIntentParser
from app.services.azure_plugins import AzurePluginRegistry
from app.services.confirmation_sessions import (
    CONFIGURATION_COMPONENT_DELETE,
    CONFIGURATION_COMPONENT_FEEDBACK_PREFIX,
    CONFIGURATION_FEEDBACK_QUESTION,
    ConfirmationSessionStore,
)


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
        if (
            confirmation_sessions is not None
            and confirmation_sessions.cloud_provider != "azure"
        ):
            raise ValueError("Azure 报价系统只能连接 Azure 专用确认存储")
        self._ai_provider = ai_provider
        self._drafts: dict[str, tuple[str, ParsedIntent]] = {}

    async def preview(self, request: QuoteRequest, reporter=None) -> QuotePreviewResponse:
        if request.draft_id and request.draft_id.startswith("aw"):
            raise QuoteError(
                "cloud_provider_boundary_violation",
                "检测到 AWS 草稿被提交到 Azure 报价系统，已阻止处理。",
                {"provider": "azure"},
                409,
            )
        trace: list[ExecutionEvent] = []

        async def report(stage: str, message: str) -> None:
            trace.append(ExecutionEvent(stage=stage, message=message))
            if reporter is not None:
                await reporter(stage, message)

        intent = await self._intent_for_request(request, report)
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
                selection = plugin._confirmation_preview(service, str(index), exc.message)
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
            question_text = str(
                selection.confirmation_reason or selection.issue_message or ""
            )
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
            ambiguity.strip()
            and self._is_region_question(ambiguity)
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
        for ambiguity in intent.ambiguities:
            if ambiguity.strip():
                questions.append(
                    (
                        ambiguity.strip(),
                        None,
                        None,
                        self._region_options(ambiguity, region_options),
                    )
                )
        for selection in selections:
            if not selection.requires_confirmation:
                continue
            question = str(
                selection.confirmation_reason or selection.issue_message or "请确认配置。"
            )
            is_region_question = self._is_region_question(question)
            if has_global_region_question and is_region_question:
                continue
            options = (
                self._region_options(question, region_options)
                if is_region_question
                else [
                    ConfirmationOption(
                        label=candidate.model,
                        value=f"选择 {candidate.model}",
                        model=candidate.model,
                        specifications=candidate.specifications,
                        monthly_catalog_cost=candidate.monthly_catalog_cost,
                    )
                    for candidate in selection.candidates
                ]
            )
            questions.append((question, selection.component_id, selection.service, options))
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
            self._is_region_question(item.question)
            and not item.options
            for item in confirmation_items
        ):
            raise QuoteError(
                "azure_region_options_empty",
                "Azure 官方区域选项暂未准备完成，系统不会向客户显示空白输入框。",
                {"provider": "azure"},
                503,
            )
        confirmation_text = (
            "您好，请确认：\n"
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
        if self._confirmation_sessions is not None:
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
            unsupported_components=sum(
                selection.status == "unsupported" for selection in selections
            ),
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
            execution_trace=trace,
            expert_review=expert_review,
        )

    async def create_quote(self, request: QuoteRequest, reporter=None) -> QuoteResponse:
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
            for question in list(responses):
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
                    remaining = self._apply_component_answers(
                        intent.services[index], answers
                    )
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
            if (
                float(current_vcpu) == original_vcpu
                and float(current_memory) == original_memory
            ):
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
                for marker in ("区域", "地域", "region")
            ):
                compact = answer.strip().casefold()
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
                    component.locked_fields = sorted(
                        set(component.locked_fields) | {"region"}
                    )
                    continue
            model_match = re.fullmatch(r"选择\s+(.+)", answer.strip())
            if model_match and any(
                marker in question.casefold()
                for marker in ("型号", "sku", "配置", "规格")
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
                    component.field_sources["requirements.requested_sku"] = (
                        "customer_confirmation"
                    )
                    component.locked_fields = sorted(
                        set(component.locked_fields)
                        | {"requirements.requested_sku"}
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
            if not any(marker in question.casefold() for marker in ("区域", "地域", "region")):
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
            ConfirmationOption(label=f"{label}（{code}）", value=code)
            for code, label in regions
        ]

    @staticmethod
    def _is_region_question(question: str) -> bool:
        text = question.strip().casefold()
        if not any(marker in text for marker in ("区域", "地域", "region")):
            return False
        if any(marker in text for marker in ("不可用", "不支持", "没有", "未找到")):
            return False
        return bool(
            re.search(
                r"(?:请确认|缺少|未指定)[^。；？?]{0,48}(?:部署[^。；？?]{0,16})?"
                r"(?:区域|地域|region)"
                r"|部署在(?:哪|哪个|哪一个)[^。；？?]{0,20}(?:区域|地域|region)"
                r"|(?:区域|地域|region)[^。；？?]{0,16}(?:缺少|未指定)",
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
