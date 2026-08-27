from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.errors import ManualConfirmationRequired
from app.domain.component_hierarchy import component_hierarchy
from app.domain.component_integrity import (
    ComponentLedger,
    capture_customer_ledger,
    enforce_component_integrity,
    restore_customer_ledger,
)
from app.domain.customer_configuration import (
    customer_product_identity,
    preserve_customer_configuration,
    preserve_service_configuration,
    restore_customer_authority,
)
from app.domain.customer_facts import customer_match_policy, record_customer_fact_metadata
from app.domain.models import (
    CandidateOption,
    ConfirmationItem,
    ConfirmationOption,
    ConfirmationSessionResponse,
    ExecutionEvent,
    ExpertReview,
    ParsedIntent,
    PreviewSelection,
    PricedLine,
    PricingScenario,
    QuotePreviewResponse,
    QuoteRequest,
    QuoteResponse,
    QuoteStatus,
    SelectedResource,
    ServiceKind,
    ServiceRequirement,
    UsageLine,
)
from app.domain.pricing_issues import should_retry_persisted_pricing_issue
from app.domain.requirement_fields import (
    canonicalize_requirement_fields,
    sanitize_requirement_values,
)
from app.domain.structured_component_updates import (
    apply_component_update,
    decode_component_update,
)
from app.integrations.calculator_web import (
    AwsCalculatorWebAutomator,
    GenericCalculatorInput,
)
from app.integrations.deepseek import DeepSeekIntentParser
from app.integrations.service_templates import (
    safe_requirement_defaults,
    strip_non_pricing_context_fields,
)
from app.services.bcm_estimator import BcmQuoteResult, BcmWorkloadEstimator
from app.services.confirmation_sessions import (
    CONFIGURATION_COMPONENT_DELETE,
    CONFIGURATION_COMPONENT_FEEDBACK_PREFIX,
    CONFIGURATION_COMPONENT_UPDATE_PREFIX,
    CONFIGURATION_FEEDBACK_QUESTION,
    ConfirmationSessionStore,
)
from app.services.plugins.base import PluginRegistry
from app.services.plugins.generic_official import GenericOfficialPlugin

ProgressReporter = Callable[[str, str], Awaitable[None]]
logger = logging.getLogger(__name__)


class QuoteService:
    def __init__(
        self,
        parser: DeepSeekIntentParser,
        plugins: PluginRegistry,
        estimator: BcmWorkloadEstimator,
        calculator: AwsCalculatorWebAutomator | None = None,
        confirmation_sessions: ConfirmationSessionStore | None = None,
        ai_provider: str = "Configured AI",
        generic_plugin: GenericOfficialPlugin | None = None,
    ):
        self._parser = parser
        self._plugins = plugins
        self._estimator = estimator
        self._calculator = calculator
        self._confirmation_sessions = confirmation_sessions
        if confirmation_sessions is not None and confirmation_sessions.cloud_provider != "aws":
            raise ValueError("AWS 报价系统只能连接 AWS 专用确认存储")
        self._ai_provider = ai_provider
        self._generic_plugin = generic_plugin
        self._drafts: dict[str, tuple[str, ParsedIntent]] = {}
        # Customer confirmation is intentionally progressive: preview should
        # discover almost everything in one pass, while an official pricing
        # adapter may still reveal one genuinely new business decision later.
        # Keep a per-draft history so that a late issue can be asked once
        # without creating a confirmation loop.
        self._asked_confirmation_questions: dict[str, set[str]] = {}
        self._confirmation_rounds: dict[str, int] = {}
        self._configuration_candidate_cache: dict[tuple[str, str, str], list[CandidateOption]] = {}

    async def identify_sales_region(self, text: str) -> dict[str, object]:
        identifier = getattr(self._parser, "identify_sales_region", None)
        if not callable(identifier):
            return {"regions": [], "requires_confirmation": True}
        return await identifier(text)

    def recover_configuration_review_after_failure(self, draft_id: str | None) -> None:
        """Return a failed customer edit to the editable table immediately."""

        if not draft_id or self._confirmation_sessions is None:
            return
        if self._confirmation_sessions.status_by_draft(draft_id) not in {
            "reviewing",
            "submitted",
            "processing",
        }:
            return
        restored = self._confirmation_sessions.restore_draft(draft_id)
        if restored is None:
            return
        customer_request, intent = restored
        self._drafts[draft_id] = (customer_request, intent.model_copy(deep=True))
        self._confirmation_sessions.prepare_configuration_review(
            draft_id=draft_id,
            intent=intent,
            confirmation_text="这次修改没有完成，原配置已保留，请重新提交。",
        )

    async def preview(
        self,
        request: QuoteRequest,
        reporter: ProgressReporter | None = None,
    ) -> QuotePreviewResponse:
        if request.draft_id and request.draft_id.startswith("az"):
            raise ManualConfirmationRequired(
                "检测到 Azure 草稿被提交到 AWS 报价系统，已阻止处理",
                code="cloud_provider_boundary_violation",
                provider="aws",
            )
        official_regions = set(DeepSeekIntentParser.official_aws_region_labels())
        if request.sales_region and request.sales_region not in official_regions:
            raise ManualConfirmationRequired(
                "所选地区不是当前可用的 AWS 官方区域，请销售重新选择",
                code="sales_region_confirmation_required",
                options=sorted(official_regions),
            )
        # The browser performs this preflight before starting a job, but the
        # quote API is also a trust boundary.  A direct caller must not bypass
        # region validation and let the parser invent a pricing region.
        if (
            isinstance(self._parser, DeepSeekIntentParser)
            and not request.sales_region
            and not request.draft_id
        ):
            region_result = await self.identify_sales_region(request.customer_request)
            detected_regions = [
                str(region)
                for region in region_result.get("regions", [])
                if isinstance(region, str) and str(region) in official_regions
            ]
            if bool(region_result.get("requires_confirmation")) or not detected_regions:
                raise ManualConfirmationRequired(
                    "客户地区缺失或不是可用的 AWS 官方区域，请销售先在内部页面确认地区",
                    code="sales_region_confirmation_required",
                    options=sorted(official_regions),
                )
            if len(detected_regions) == 1:
                request = request.model_copy(update={"sales_region": detected_regions[0]})
        started_at = time.perf_counter()
        ai_trace: list[ExecutionEvent] = []

        async def collect_ai_trace(stage: str, message: str) -> None:
            ai_trace.append(ExecutionEvent(stage=stage, message=message))
            # Raw prompts/responses remain in the final audit trace. The live
            # sales screen receives only compact workflow events, never prompt
            # bodies or customer payload dumps.
            if reporter is not None and stage in {
                "intake_start",
                "intake_done",
                "component_plan",
                "component_start",
                "component_done",
                "ai_repair",
                "catalog",
            }:
                await reporter(stage, message)

        accepted_current_page = False
        configuration_revision_requested = False
        configuration_revision_component_ids: set[int] = set()
        configuration_revision_scope_ids: set[int] | None = None
        configuration_revision_original_intent: ParsedIntent | None = None
        cached = self._drafts.get(request.draft_id) if request.draft_id else None
        if cached is None and request.draft_id and self._confirmation_sessions is not None:
            cached = self._confirmation_sessions.restore_draft(request.draft_id)
        if cached and cached[0] == request.customer_request:
            intent = cached[1].model_copy(deep=True)
            confirmation_responses = dict(request.confirmation_responses)
            submitted_confirmation_responses = dict(confirmation_responses)
            structured_response_components: dict[str, int] = {}
            configuration_revision_requested = any(
                question.startswith(CONFIGURATION_COMPONENT_FEEDBACK_PREFIX)
                or question.startswith(CONFIGURATION_COMPONENT_UPDATE_PREFIX)
                or question == CONFIGURATION_FEEDBACK_QUESTION
                for question in confirmation_responses
            )
            if configuration_revision_requested:
                # Customer edits are transactional. AI always works on this
                # temporary copy; a result that cannot pass the official
                # catalog must never replace or partially erase the last
                # confirmed configuration.
                configuration_revision_original_intent = intent.model_copy(deep=True)
            component_feedback: dict[int, str] = {}
            component_updates: dict[int, dict[str, Any]] = {}
            structured_catalog_component_ids: set[int] = set()
            answered_component_questions: dict[int, set[str]] = {}
            for question in list(confirmation_responses):
                if question.startswith(CONFIGURATION_COMPONENT_UPDATE_PREFIX):
                    component_id = question.removeprefix(CONFIGURATION_COMPONENT_UPDATE_PREFIX)
                    update = decode_component_update(confirmation_responses.pop(question, ""))
                    if component_id.isdigit() and update:
                        parsed_component_id = int(component_id)
                        component_updates[parsed_component_id] = update
                        configuration_revision_component_ids.add(parsed_component_id)
                        requirements_update = update.get("requirements", {})
                        catalog_fields = {
                            "requested_model",
                            "vcpu",
                            "memory_gib",
                            "master_requested_model",
                            "master_vcpu",
                            "master_memory_gib",
                            "core_requested_model",
                            "core_vcpu",
                            "core_memory_gib",
                            "task_requested_model",
                            "task_vcpu",
                            "task_memory_gib",
                            "operating_system",
                            "architecture",
                            "tenancy",
                            "engine",
                            "deployment",
                            "storage_class",
                            "storage_type",
                        }
                        if "region" in update or (
                            isinstance(requirements_update, dict)
                            and catalog_fields.intersection(requirements_update)
                        ):
                            structured_catalog_component_ids.add(parsed_component_id)
                    continue
                if not question.startswith(CONFIGURATION_COMPONENT_FEEDBACK_PREFIX):
                    continue
                component_id = question.removeprefix(CONFIGURATION_COMPONENT_FEEDBACK_PREFIX)
                feedback = confirmation_responses.pop(question, "").strip()
                if component_id.isdigit() and feedback:
                    component_feedback[int(component_id)] = feedback
                    configuration_revision_component_ids.add(int(component_id))

            for index, update in component_updates.items():
                if 0 <= index < len(intent.services):
                    intent.services[index] = apply_component_update(intent.services[index], update)

            deleted_component_indices = {
                index
                for index, feedback in component_feedback.items()
                if feedback == CONFIGURATION_COMPONENT_DELETE
            }
            deleted_component_descriptions = [
                (intent.services[index].calculator_service_name or intent.services[index].service)
                for index in sorted(deleted_component_indices)
                if 0 <= index < len(intent.services)
            ]
            component_feedback = {
                index: feedback
                for index, feedback in component_feedback.items()
                if index not in deleted_component_indices
            }

            if request.draft_id and self._confirmation_sessions is not None:
                # Workflow controls are structured customer decisions, not
                # free-form component edits. Keep them out of the AI revision
                # lane. Partition first so the component id remains attached;
                # a quote may contain several EC2 rows and question wording is
                # not a safe way to guess which one the customer selected.
                component_answers, confirmation_responses = (
                    self._confirmation_sessions.partition_answers_by_component(
                        request.draft_id, confirmation_responses
                    )
                )
                for index, answers in component_answers.items():
                    structured_answers = {
                        question: answer
                        for question, answer in answers.items()
                        if self._is_structured_workflow_answer(answer)
                    }
                    for question, answer in structured_answers.items():
                        response_key = self._scoped_confirmation_response_key(
                            index, question
                        )
                        confirmation_responses[response_key] = answer
                        structured_response_components[response_key] = index
                        answers.pop(question, None)
                    answered_component_questions[index] = set(answers)
                    answer_text = "\n".join(
                        f"问题：{question}\n客户回答：{answer}"
                        for question, answer in answers.items()
                    )
                    if answer_text:
                        component_feedback[index] = "\n".join(
                            part for part in (component_feedback.get(index), answer_text) if part
                        )

            component_reviser = getattr(self._parser, "revise_component_from_feedback", None)
            if component_feedback and callable(component_reviser):
                revision_semaphore = asyncio.Semaphore(max(1, len(component_feedback)))

                async def revise_one(index: int, feedback: str):
                    if index < 0 or index >= len(intent.services):
                        return index, None, None
                    try:
                        reviser_arguments = (
                            {"reporter": collect_ai_trace}
                            if "reporter" in inspect.signature(component_reviser).parameters
                            else {}
                        )
                        async with revision_semaphore:
                            original_component = intent.services[index].model_copy(deep=True)
                            revised = await component_reviser(
                                request.customer_request,
                                original_component,
                                feedback,
                                **reviser_arguments,
                            )
                        if revised is not None:
                            revised = restore_customer_authority(
                                original_component,
                                revised,
                            )
                        return index, revised, None
                    except Exception:
                        logger.exception(
                            "Component-only configuration revision failed for index %d",
                            index,
                        )
                        display_name = (
                            intent.services[index].calculator_service_name
                            or intent.services[index].service
                        )
                        return (
                            index,
                            None,
                            (f"{display_name}的修改未能通过格式校验，请只说明要修改的字段和新值。"),
                        )

                revised_results = await asyncio.gather(
                    *(revise_one(index, feedback) for index, feedback in component_feedback.items())
                )
                for index, revised, issue in revised_results:
                    if revised is not None:
                        intent.services[index] = revised
                        for question in getattr(revised, "_revision_questions", []):
                            compact_question = str(question).strip()
                            if compact_question and compact_question not in intent.ambiguities:
                                intent.ambiguities.append(compact_question)
                        answered_keys = {
                            self._confirmation_question_key(question)
                            for question in answered_component_questions.get(index, set())
                        }
                        if answered_keys:
                            intent.ambiguities = [
                                ambiguity
                                for ambiguity in intent.ambiguities
                                if self._confirmation_question_key(ambiguity) not in answered_keys
                            ]
                    if issue:
                        intent.ambiguities.append(issue)

            # Deletions are applied only after component-specific revisions so
            # every correction remains bound to the component id shown to the
            # customer. Removing rows earlier would shift those indexes and
            # could modify the wrong service.
            for index in sorted(deleted_component_indices, reverse=True):
                if 0 <= index < len(intent.services):
                    del intent.services[index]

            configuration_feedback = confirmation_responses.pop(
                CONFIGURATION_FEEDBACK_QUESTION, ""
            ).strip()
            if configuration_feedback:
                addition_only = bool(
                    re.fullmatch(
                        r"\s*请新增以下配置\s*[：:]\s*[\s\S]+?\s*",
                        configuration_feedback,
                    )
                )
                previous_component_count = len(intent.services)
                if deleted_component_descriptions:
                    configuration_feedback = (
                        f"{configuration_feedback}\n同时保持以下删除结果，不得重新加入："
                        + "、".join(deleted_component_descriptions)
                    )
                    addition_only = False
                reviser = getattr(self._parser, "revise_configuration_from_feedback", None)
                if callable(reviser):
                    reviser_arguments = (
                        {"reporter": collect_ai_trace}
                        if "reporter" in inspect.signature(reviser).parameters
                        else {}
                    )
                    intent = await reviser(
                        request.customer_request,
                        intent,
                        configuration_feedback,
                        **reviser_arguments,
                    )
                if addition_only and len(intent.services) > previous_component_count:
                    new_component_ids = set(
                        range(previous_component_count, len(intent.services))
                    )
                    configuration_revision_component_ids.update(new_component_ids)
                    # The existing rows have already passed customer review and
                    # official validation.  Only the newly appended rows may
                    # call catalogue adapters in this preview.
                    configuration_revision_scope_ids = new_component_ids
            # A final-table edit is enclosed within its own component.  Do not
            # send every untouched component back through AWS catalog adapters:
            # that made a one-field save as slow and fragile as rebuilding the
            # entire quote.  Whole-table prose and deletions can change indexes
            # or add/remove services, so only those exceptional operations keep
            # the full validation path.
            if (
                configuration_revision_component_ids
                and not deleted_component_indices
                and not configuration_feedback
            ):
                # Dropdown values already came from the persisted official
                # catalog. Quantity, capacity and usage changes need no second
                # catalog round-trip; region/model/shape compatibility still
                # revalidates only its own component.
                configuration_revision_scope_ids = {
                    *component_feedback.keys(),
                    *structured_catalog_component_ids,
                }
            await self._apply_confirmation_responses(
                intent,
                confirmation_responses,
                response_components=structured_response_components,
            )
            # Catalog buttons and other structured controls have already been
            # applied deterministically above. Sending those machine values to
            # the AI finalizer adds latency and can turn a valid answer into a
            # false "could not review" loop. Only genuinely free-form answers
            # need semantic review.
            needs_semantic_answer_review = any(
                not self._is_structured_workflow_answer(answer)
                for answer in submitted_confirmation_responses.values()
            )
            if (
                submitted_confirmation_responses
                and needs_semantic_answer_review
                and not configuration_revision_requested
            ):
                finalizer = getattr(self._parser, "finalize_confirmed_intent", None)
                if callable(finalizer):
                    intent_before_finalizer = intent.model_copy(deep=True)
                    finalizer_arguments = (
                        {"reporter": collect_ai_trace}
                        if "reporter" in inspect.signature(finalizer).parameters
                        else {}
                    )
                    intent = await finalizer(
                        request.customer_request,
                        intent,
                        submitted_confirmation_responses,
                        **finalizer_arguments,
                    )
                    for index, original_component in enumerate(
                        intent_before_finalizer.services
                    ):
                        if index >= len(intent.services):
                            break
                        intent.services[index] = restore_customer_authority(
                            original_component,
                            intent.services[index],
                        )
            if request.retry_component_ids:
                retry_scope = {
                    component_id
                    for component_id in request.retry_component_ids
                    if 0 <= component_id < len(intent.services)
                }
                configuration_revision_scope_ids = (
                    retry_scope
                    if configuration_revision_scope_ids is None
                    else configuration_revision_scope_ids | retry_scope
                )
            logger.info("Quote preview reused structured draft %s", request.draft_id)
        else:
            accepted_current_page = self._has_plain_affirmative_confirmation(
                request.customer_request
            )
            try:
                parser_arguments = (
                    {"reporter": collect_ai_trace}
                    if "reporter" in inspect.signature(self._parser.parse).parameters
                    else {}
                )
                intent = await self._parser.parse(request.customer_request, **parser_arguments)
            except ManualConfirmationRequired as exc:
                exc.details["execution_trace"] = [event.model_dump() for event in ai_trace]
                raise
        # Restore customer-facing product identity before any pricing defaults
        # or adapter conversion touches the working draft. This also upgrades
        # older saved drafts created before customer/pricing separation.
        numbered_blocks = DeepSeekIntentParser._numbered_requirement_blocks(
            request.customer_request
        )
        top_level_components = [
            item for item in intent.services if not item.derived_from_service
        ]
        if len(numbered_blocks) > len(top_level_components):
            DeepSeekIntentParser._reconcile_explicit_component_inventory(
                request.customer_request, intent
            )
        preserve_customer_configuration(intent)
        DeepSeekIntentParser.reconcile_customer_pricing_facts(intent)
        # Reconcile derived children on every draft boundary, not only during
        # the first AI parse. Older/saved drafts may contain an EKS Worker row
        # whose quantity copied the cluster count instead of
        # ``clusters × workers_per_cluster``.
        DeepSeekIntentParser._split_eks_worker_nodes(intent)
        enforce_component_integrity(intent)
        DeepSeekIntentParser._normalize_database_group_quantity(intent)
        DeepSeekIntentParser._normalize_redis_topology(intent)
        DeepSeekIntentParser._normalize_cluster_group_quantities(intent)
        # Apply the region boundary on every preview, including restored drafts
        # and customer-edited components.  Regional services such as S3 must
        # never reach an AWS adapter with the human label ``global/全球``.
        DeepSeekIntentParser._normalize_invalid_global_regions(intent)
        # Only an explicit quote-wide ``区域：...`` may fill missing component
        # regions.  A region written inside one numbered component must never
        # overwrite another component's explicit region.
        DeepSeekIntentParser._reconcile_explicit_regions(request.customer_request, intent)
        self._apply_sales_region(intent, request.sales_region)
        # Regional components without their own region inherit a deterministic
        # quote region.  With several customer regions, use the first one in
        # the original request; explicit component regions still win.
        DeepSeekIntentParser._inherit_single_workload_region(intent, request.customer_request)
        DeepSeekIntentParser._ensure_missing_region_ambiguity(intent)
        self._apply_sales_pricing_choice(intent, request)
        parse_elapsed = time.perf_counter() - started_at
        logger.info("Quote preview AI parse completed in %.2fs", parse_elapsed)
        self._merge_transfer_only_ec2_services(intent)
        self._strip_non_numeric_placeholders(intent)
        self._strip_non_pricing_context(intent)
        confirmed_before_defaults = self._customer_confirmed_snapshot(intent)
        self._apply_calculator_minimum_defaults(intent)
        self._restore_customer_confirmed_snapshot(intent, confirmed_before_defaults)
        preflight_notices: list[str] = []
        preflight_sizing_service_indexes: set[int] = set()
        confirmation_options: dict[str, list[ConfirmationOption]] = {}
        confirmation_components: dict[str, tuple[str, str]] = {}
        technical_errors: list[ManualConfirmationRequired] = []
        preflight_trace: list[ExecutionEvent] = []
        try:
            await self._require_official_spec_confirmation(
                intent,
                component_ids=configuration_revision_scope_ids,
            )
        except ManualConfirmationRequired as exc:
            if exc.code == "official_spec_confirmation_required":
                preflight_trace.append(
                    ExecutionEvent(
                        stage="aws",
                        message="AWS 官方规格预检发现需要客户确认的配置",
                        status="warning",
                    )
                )
                if not accepted_current_page:
                    preflight_notices.extend(
                        self._questions_from_confirmation_text(
                            str(exc.details["confirmation_text"])
                        )
                    )
                    for item in exc.details.get("sizing_options", []):
                        if not isinstance(item, dict):
                            continue
                        service_index = item.get("service_index")
                        if isinstance(service_index, int):
                            preflight_sizing_service_indexes.add(service_index)
                        question = self._sizing_confirmation_question(item)
                        if isinstance(service_index, int) and 1 <= service_index <= len(
                            intent.services
                        ):
                            component = intent.services[service_index - 1]
                            confirmation_components[question] = (
                                str(service_index - 1),
                                component.service,
                            )
                        options: list[ConfirmationOption] = []
                        for option in item.get("options", []):
                            if not isinstance(option, dict):
                                continue
                            model = str(option.get("example_model") or "").strip()
                            if not model:
                                continue
                            vcpu = float(option["vcpu"])
                            memory = float(option["memory_gib"])
                            options.append(
                                ConfirmationOption(
                                    label=(f"{model} · {vcpu:g} vCPU · {memory:g} GiB"),
                                    value=f"选择 {model}",
                                    model=model,
                                    specifications={"vCPU": vcpu, "memoryGiB": memory},
                                )
                            )
                        confirmation_options[question] = options
                    for notice in exc.details.get("design_notices", []):
                        if not isinstance(notice, str):
                            continue
                        component_index = self._service_index_for_notice(intent, notice)
                        if component_index is not None:
                            component = intent.services[component_index]
                            compact = self._compact_customer_question(notice)
                            confirmation_components[compact] = (
                                str(component_index),
                                component.service,
                            )
            elif self._is_technical_catalog_error(exc):
                technical_errors.append(exc)
                preflight_trace.append(
                    ExecutionEvent(
                        stage="aws",
                        message="AWS 官方规格接口暂时不可用；该技术问题不会发送给客户",
                        status="warning",
                    )
                )
            else:
                raise

        notices = await self._enriched_confirmation_notices(intent)
        if preflight_notices:
            already_in_preflight = set(self._missing_spec_confirmation_notices(intent))
            notices = [
                notice
                for notice in notices
                if not self._is_blocking_design_notice(notice)
                and notice not in already_in_preflight
            ]
        notices.extend(preflight_notices)
        for notice in notices:
            if notice in confirmation_components:
                continue
            component_index = self._service_index_for_notice(intent, notice)
            if component_index is not None:
                component = intent.services[component_index]
                confirmation_components[notice] = (
                    str(component_index),
                    component.service,
                )
        selections: list[PreviewSelection] = []
        trace = [
            *ai_trace,
            ExecutionEvent(
                stage="ai",
                message=f"系统已把客户原话拆成逐条报价任务（{parse_elapsed:.1f} 秒）",
            ),
        ]
        trace.extend(preflight_trace)

        async def preview_one(index: int, service: ServiceRequirement):
            item_started_at = time.perf_counter()
            kind = self._service_kind(service.service)
            display_name = self._calculator_service_name(
                service.service, service.calculator_service_name
            )
            pending_architecture = bool(service.field_sources.get("_pending_architecture_decision"))
            if kind is None and self._generic_plugin is None:
                question = (
                    f"AWS 当前没有可直接核价的 {display_name} 托管方案。"
                    "请选择按原需求在 EC2 自建，或移出本次报价。"
                )
                return (
                    index,
                    PreviewSelection(
                        component_id=str(index),
                        service=service.service,
                        display_name=display_name,
                        region=service.region or "未指定区域",
                        quantity=service.quantity,
                        requirements=service.requirements,
                        source_text=service.source_text,
                        selection_reason="需要客户决定替代方式",
                        candidates=[
                            CandidateOption(
                                model=f"在 EC2 自建 {display_name}",
                                family="service_replacement",
                                specifications={"decision": "replace_service:ec2:self_hosted"},
                                rationale="保留原需求并改用 EC2 自建。",
                            ),
                            CandidateOption(
                                model="暂不纳入本次报价",
                                family="service_replacement",
                                specifications={"decision": "exclude_component"},
                                rationale="由客户决定移出本次报价。",
                            ),
                        ],
                        requires_confirmation=True,
                        confirmation_reason=question,
                        status="customer_issue",
                        issue_message=question,
                    ),
                    question,
                    ExecutionEvent(stage="aws", message=question, status="warning"),
                    None,
                )
            plugin = self._plugins.get(kind) if kind is not None else self._generic_plugin
            assert plugin is not None
            current = service.model_copy(deep=True)
            if pending_architecture:
                # Fetch the official EC2 catalog during the first page load so
                # choosing self-hosted can expand its machine controls
                # immediately, without a second submit/wait/page cycle.
                current.field_sources.pop("_pending_architecture_decision", None)
                current.field_sources["_customer_select_configuration"] = "customer_confirmation"
            repair_count = 0
            catalog_retry_count = 0
            while True:
                failure: ManualConfirmationRequired | None = None
                service_key = kind.value if kind is not None else current.service
                normalized = self._calculator_requirements(
                    current.requirements, current.quantity, service_key
                )
                requirement = self._pricing_requirement_copy(
                    current, service_key=service_key, requirements=normalized
                )
                self._align_pricing_product_identity(current, requirement)
                try:
                    selection = await asyncio.to_thread(
                        plugin.preview, requirement, "ap-southeast-1"
                    )
                    selection = self._enforce_catalog_sizing_invariant(
                        current,
                        selection,
                    )
                    if selection.requires_confirmation and len(selection.candidates) <= 1:
                        # A one-option question is not a customer decision. If
                        # the initially selected model is incompatible, expand
                        # the same regional catalog and re-run the shared rule.
                        expanded = await self._configuration_candidates(selection, current)
                        if expanded:
                            selection = selection.model_copy(update={"candidates": expanded})
                            selection = self._enforce_catalog_sizing_invariant(
                                current,
                                selection,
                            )
                    break
                except ManualConfirmationRequired as exc:
                    failure = exc
                    if repair_count < 3 and self._is_ai_repairable_component_error(exc):
                        repair_count += 1
                        await collect_ai_trace(
                            "ai_repair",
                            f"组件 {index + 1}｜{display_name}｜本地映射未通过，正在执行第 {repair_count} 次定向修正",
                        )
                        repairer = getattr(self._parser, "repair_quote_component", None)
                        repaired = None
                        if callable(repairer):
                            try:
                                repaired = await repairer(
                                    request.customer_request,
                                    current,
                                    error_code=exc.code,
                                    error_message=exc.message,
                                    error_details=exc.details,
                                    attempt=repair_count,
                                    reporter=collect_ai_trace,
                                )
                            except ManualConfirmationRequired as customer_error:
                                failure = customer_error
                        if repaired is not None:
                            preserve_service_configuration(repaired)
                            repaired = restore_customer_authority(current, repaired)
                            preserve_service_configuration(repaired)
                            current = repaired
                            intent.services[index] = repaired.model_copy(deep=True)
                            continue
                    if (
                        catalog_retry_count < 2
                        and self._should_auto_retry_component_error(failure, current)
                    ):
                        catalog_retry_count += 1
                        await collect_ai_trace(
                            "catalog",
                            f"组件 {index + 1}｜{display_name}｜现有缓存不满足本次区域或计费字段，正在同步 AWS 官方目录",
                        )
                        refresher = getattr(plugin, "refresh_component", None)
                        if callable(refresher):
                            try:
                                await asyncio.to_thread(refresher, current)
                                await collect_ai_trace(
                                    "catalog",
                                    f"组件 {index + 1}｜{display_name}｜官方目录已返回，正在校验后写入组件缓存",
                                )
                            except Exception:
                                # The next preview call still performs its own
                                # official lookup. A refresh failure belongs to
                                # this component and must not stop its siblings.
                                logger.exception(
                                    "Component-only catalog refresh failed for %s",
                                    display_name,
                                )
                                await collect_ai_trace(
                                    "catalog",
                                    f"组件 {index + 1}｜{display_name}｜官方目录本次未返回有效数据，旧缓存保持不变并继续重试",
                                )
                        await asyncio.sleep(0.15 * (2 ** (catalog_retry_count - 1)))
                        continue
                assert failure is not None
                elapsed = time.perf_counter() - item_started_at
                if self._is_third_party_architecture_catalog_miss(current, display_name, failure):
                    # A literal third-party product (for example ClickHouse)
                    # is a customer architecture decision, not an AWS outage.
                    # Recover the customer's full numbered block because some
                    # model responses keep only the product heading here.
                    self._recover_third_party_deployment(
                        current,
                        request.customer_request,
                        display_name,
                    )
                    product_name = self._third_party_product_name(current, display_name)
                    current.field_sources["_pending_architecture_decision"] = "system_policy"
                    current.field_sources["_third_party_product"] = product_name
                    current.service = "ec2"
                    current.calculator_service_name = f"Amazon EC2（自建 {product_name}）"
                    current.requirements.setdefault("operating_system", "linux")
                    intent.services[index] = current.model_copy(deep=True)
                    candidates: list[CandidateOption] = []
                    try:
                        ec2_plugin = self._plugins.get(ServiceKind.EC2)
                        ec2_working = current.model_copy(deep=True)
                        ec2_working.field_sources.pop("_pending_architecture_decision", None)
                        ec2_working.field_sources["_customer_select_configuration"] = (
                            "customer_confirmation"
                        )
                        ec2_normalized = self._calculator_requirements(
                            ec2_working.requirements,
                            ec2_working.quantity,
                            "ec2",
                        )
                        ec2_requirement = self._pricing_requirement_copy(
                            ec2_working,
                            service_key="ec2",
                            requirements=ec2_normalized,
                        )
                        ec2_selection = await asyncio.to_thread(
                            ec2_plugin.preview,
                            ec2_requirement,
                            "ap-southeast-1",
                        )
                        candidates = list(ec2_selection.candidates)
                    except ManualConfirmationRequired as ec2_failure:
                        candidates = self._candidate_options_from_error(ec2_failure)
                    question = (
                        f"AWS 没有与 {product_name} 完全等价的托管服务。"
                        "您要采用 AWS 托管替代方案（功能可能不同），"
                        f"还是按原配置在 EC2 上自建 {product_name}？"
                    )
                    logger.info(
                        "Quote preview converted third-party catalog miss to "
                        "architecture confirmation for %s",
                        product_name,
                    )
                    return (
                        index,
                        PreviewSelection(
                            component_id=str(index),
                            service=current.service,
                            display_name=product_name,
                            region=current.region or "未指定区域",
                            quantity=current.quantity,
                            requirements=dict(current.requirements),
                            source_text=current.source_text,
                            selection_reason="需要客户选择托管替代方案或自建方案",
                            candidates=candidates,
                            requires_confirmation=True,
                            confirmation_reason=question,
                            status="customer_issue",
                            issue_message=question,
                        ),
                        question,
                        ExecutionEvent(
                            stage="aws",
                            message=(f"第 {index + 1} 项需要客户选择托管或自建：{product_name}"),
                            status="warning",
                        ),
                        None,
                    )
                if failure.code in {
                    "service_region_not_supported",
                    "service_retired",
                    "unsupported_service",
                }:
                    confirmation_candidates = (
                        await self._confirmation_candidates_for_failure(
                            plugin=plugin,
                            component=current,
                            failure=failure,
                            display_name=display_name,
                        )
                    )
                    if not confirmation_candidates and failure.code == "unsupported_service":
                        confirmation_candidates = [
                            CandidateOption(
                                model=f"在 EC2 自建 {display_name}",
                                family="service_replacement",
                                specifications={"decision": "replace_service:ec2:self_hosted"},
                                rationale="保留原需求并改用 EC2 自建。",
                            ),
                            CandidateOption(
                                model="暂不纳入本次报价",
                                family="service_replacement",
                                specifications={"decision": "exclude_component"},
                                rationale="由客户决定移出本次报价。",
                            ),
                        ]
                    if confirmation_candidates:
                        question = self._plugin_confirmation_question(
                            display_name, requirement, failure
                        )
                        return (
                            index,
                            PreviewSelection(
                                component_id=str(index),
                                service=service_key,
                                display_name=display_name,
                                region=service.region or "未指定区域",
                                quantity=service.quantity,
                                requirements=normalized,
                                source_text=current.source_text,
                                selection_reason="需要客户选择可用区域或替代方案",
                                requires_confirmation=True,
                                confirmation_reason=question,
                                candidates=confirmation_candidates,
                                status="customer_issue",
                                issue_message=question,
                            ),
                            question,
                            ExecutionEvent(
                                stage="aws",
                                message=f"第 {index + 1} 项需要客户选择：{display_name}",
                                status="warning",
                            ),
                            None,
                        )
                if self._is_technical_catalog_error(failure):
                    issue_category = self._catalog_issue_category(failure, current)
                    issue_message = self._catalog_issue_message(
                        failure,
                        current,
                        display_name,
                        issue_category,
                    )
                    logger.warning(
                        "Quote preview AWS check %s failed in %.2fs: %s (%s)",
                        display_name,
                        elapsed,
                        failure.code,
                        issue_category,
                    )
                    return (
                        index,
                        PreviewSelection(
                            component_id=str(index),
                            service=service_key,
                            display_name=display_name,
                            region=service.region or "未指定区域",
                            quantity=service.quantity,
                            requirements=normalized,
                            source_text=current.source_text,
                            selection_reason=issue_message,
                            status="technical_issue",
                            issue_message=issue_message,
                            issue_code=failure.code,
                            issue_category=issue_category,
                        ),
                        None,
                        ExecutionEvent(
                            stage="aws",
                            message=(
                                f"第 {index + 1} 项官方核验未完成：{display_name}；"
                                f"原因分类 {issue_category}"
                            ),
                            status="warning",
                        ),
                        failure,
                    )
                logger.info(
                    "Quote preview AWS check %s completed with confirmation in %.2fs",
                    display_name,
                    elapsed,
                )
                confirmation_candidates = await self._confirmation_candidates_for_failure(
                    plugin=plugin,
                    component=current,
                    failure=failure,
                    display_name=display_name,
                )
                return (
                    index,
                    PreviewSelection(
                        component_id=str(index),
                        service=service_key,
                        display_name=display_name,
                        region=service.region or "未指定区域",
                        quantity=service.quantity,
                        requirements=normalized,
                        source_text=current.source_text,
                        selection_reason="需要客户补充或确认配置",
                        requires_confirmation=True,
                        confirmation_reason=self._plugin_confirmation_question(
                            display_name, requirement, failure
                        ),
                        candidates=confirmation_candidates,
                        status="customer_issue",
                        issue_message=self._plugin_confirmation_question(
                            display_name, requirement, failure
                        ),
                    ),
                    self._plugin_confirmation_question(display_name, requirement, failure),
                    ExecutionEvent(
                        stage="aws",
                        message=(
                            f"第 {index + 1} 项需要客户补充信息：{display_name}（{elapsed:.1f} 秒）"
                        ),
                        status="warning",
                    ),
                    None,
                )
            confirmation_reason = None
            if selection.requires_confirmation:
                confirmation_reason = self._customer_confirmation_question(
                    display_name,
                    requirement,
                    selection.confirmation_reason or "当前配置需要您确认。",
                )
            selection = selection.model_copy(
                update={
                    "component_id": str(index),
                    "display_name": display_name,
                    "quantity": current.quantity,
                    "requirements": normalized,
                    "source_text": current.source_text,
                    "status": ("customer_issue" if selection.requires_confirmation else "ready"),
                    "confirmation_reason": confirmation_reason,
                    "issue_message": confirmation_reason,
                }
            )
            elapsed = time.perf_counter() - item_started_at
            if elapsed < 0.05 and catalog_retry_count == 0:
                await collect_ai_trace(
                    "catalog",
                    f"组件 {index + 1}｜{display_name}｜本地缓存完整且适用于当前区域，直接复用",
                )
            logger.info("Quote preview AWS check %s completed in %.2fs", display_name, elapsed)
            return (
                index,
                selection,
                (
                    selection.confirmation_reason
                    if selection.requires_confirmation and selection.confirmation_reason
                    else None
                ),
                ExecutionEvent(
                    stage="aws",
                    message=(
                        f"第 {index + 1} 项已通过 AWS 官方接口预检：{display_name}"
                        + (f"（系统自动修正 {repair_count} 次）" if repair_count else "")
                        + (
                            f"（组件独立重试 {catalog_retry_count} 次）"
                            if catalog_retry_count
                            else ""
                        )
                        + (
                            "（本地缓存命中，少于 0.1 秒）"
                            if elapsed < 0.05
                            else f"（{elapsed:.1f} 秒）"
                        )
                    ),
                ),
                None,
            )

        # Keep requests isolated but run a few components concurrently.  The
        # cap avoids provider throttling while removing the old N-times serial
        # wait for a quote containing many services.
        preview_semaphore = asyncio.Semaphore(max(1, len(intent.services)))

        async def preview_one_limited(index: int, service: ServiceRequirement):
            async with preview_semaphore:
                display_name = self._calculator_service_name(
                    service.service, service.calculator_service_name
                )
                if reporter is not None:
                    await reporter(
                        "aws_start",
                        f"组件 {index + 1}｜{display_name}｜正在查询 AWS 官方规格",
                    )
                result = await preview_one(index, service)
                if reporter is not None:
                    selection = result[1]
                    state = (
                        "需要客户确认"
                        if selection is not None and selection.requires_confirmation
                        else "官方规格核验完成"
                    )
                    await reporter(
                        "aws_done",
                        f"组件 {index + 1}｜{display_name}｜{state}",
                    )
                return result

        async def preview_saved_component(index: int, service: ServiceRequirement):
            """Reuse the last validated result for an untouched review row."""

            display_name = self._calculator_service_name(
                service.service, service.calculator_service_name
            )
            selected_model = service.requirements.get("_review_selected_model")
            selected_specifications = service.requirements.get("_review_selected_specifications")
            candidates: list[CandidateOption] = []
            raw_saved_candidates = service.requirements.get("_review_confirmation_candidates")
            if isinstance(raw_saved_candidates, list):
                for raw_candidate in raw_saved_candidates:
                    if not isinstance(raw_candidate, dict):
                        continue
                    try:
                        candidates.append(CandidateOption.model_validate(raw_candidate))
                    except ValueError:
                        continue
            if isinstance(selected_model, str) and selected_model.strip():
                selected_candidate = CandidateOption(
                    model=selected_model,
                    family=selected_model.split(".", 1)[0],
                    specifications=(
                        dict(selected_specifications)
                        if isinstance(selected_specifications, dict)
                        else {}
                    ),
                    rationale="沿用上次已通过官方校验的配置",
                    is_default=True,
                )
                if not any(candidate.model == selected_model for candidate in candidates):
                    candidates.append(selected_candidate)
            saved_status = str(service.requirements.get("_review_status") or "ready")
            saved_question = str(
                service.requirements.get("_review_confirmation_reason") or ""
            ).strip()
            is_saved_customer_issue = saved_status == "customer_issue" and bool(saved_question)
            selection = PreviewSelection(
                component_id=str(index),
                service=service.service,
                display_name=display_name,
                region=service.region or "未指定区域",
                quantity=service.quantity,
                requirements=dict(service.requirements),
                source_text=service.source_text,
                requested_model=(
                    str(service.requirements.get("requested_model"))
                    if service.requirements.get("requested_model")
                    else None
                ),
                selected_model=(
                    selected_model
                    if isinstance(selected_model, str) and selected_model.strip()
                    else None
                ),
                selection_reason=(
                    "等待客户完成已生成的组件选择"
                    if is_saved_customer_issue
                    else "未修改，沿用上次官方校验结果"
                ),
                candidates=candidates,
                requires_confirmation=is_saved_customer_issue,
                confirmation_reason=saved_question or None,
                status="customer_issue" if is_saved_customer_issue else "ready",
                issue_message=saved_question or None,
            )
            return (
                index,
                selection,
                saved_question or None,
                ExecutionEvent(
                    stage="aws",
                    message=f"第 {index + 1} 项未修改，沿用已校验配置：{display_name}",
                ),
                None,
            )

        results = await asyncio.gather(
            *(
                (
                    preview_saved_component(index, service)
                    if configuration_revision_scope_ids is not None
                    and index not in configuration_revision_scope_ids
                    else preview_one_limited(index, service)
                )
                for index, service in enumerate(intent.services)
            )
        )
        hierarchy = component_hierarchy(intent.services)
        for result_index, selection, notice, event, technical_error in sorted(
            results, key=lambda item: item[0]
        ):
            if selection is not None:
                relation = hierarchy[result_index]
                selection = selection.model_copy(
                    update={
                        "component_number": relation.component_number,
                        "parent_component_id": relation.parent_component_id,
                        "parent_component_number": relation.parent_component_number,
                        "parent_display_name": relation.parent_display_name,
                    }
                )
                selections.append(selection)
            if (
                notice
                and not accepted_current_page
                and (
                    (
                        "自建" in notice
                        and any(
                            marker in notice.casefold() for marker in ("托管", "managed", "aws")
                        )
                    )
                    or (
                        result_index + 1 not in preflight_sizing_service_indexes
                        and str(result_index)
                        not in {component[0] for component in confirmation_components.values()}
                    )
                )
            ):
                notices.append(notice)
                confirmation_components[notice] = (
                    str(result_index),
                    intent.services[result_index].service,
                )
                is_architecture_notice = "自建" in notice and any(
                    marker in notice.casefold() for marker in ("托管", "managed", "aws")
                )
                if selection is not None and not is_architecture_notice:
                    confirmation_options[notice] = self._compact_candidate_options(
                        selection.candidates,
                        intent.services[result_index],
                    )
            trace.append(event)
            if technical_error is not None:
                technical_errors.append(technical_error)

        total_elapsed = time.perf_counter() - started_at
        logger.info("Quote preview completed in %.2fs", total_elapsed)
        notices = self._deduplicate_confirmation_notices(notices, confirmation_components)
        prior_asked = (
            self._asked_confirmation_questions.get(request.draft_id, set())
            if request.draft_id
            else set()
        )
        if request.draft_id and self._confirmation_sessions is not None:
            prior_asked = set(prior_asked)
            prior_asked.update(
                self._confirmation_question_key(question)
                for question in self._confirmation_sessions.asked_questions_by_draft(
                    request.draft_id
                )
            )
        completed_confirmation_rounds = self._confirmation_rounds.get(request.draft_id or "", 0)
        if request.draft_id and self._confirmation_sessions is not None:
            completed_confirmation_rounds = max(
                completed_confirmation_rounds,
                self._confirmation_sessions.confirmation_round_by_draft(request.draft_id),
            )
        # If the customer has just answered a round, never ask an identical
        # normalized question again even when the AI phrases the unresolved
        # component in the same way during cleanup.
        if prior_asked and not configuration_revision_requested:
            notices = [
                notice
                for notice in notices
                if self._confirmation_question_key(notice) not in prior_asked
            ]
        # Region is the one quote-wide fallback: once every regional component
        # has a resolved region, a late parser or preflight question must not
        # ask the customer for it again. All other decisions remain enclosed
        # within their own component.
        notices = self._drop_resolved_region_questions(intent, notices)
        notices = self._ensure_selection_confirmation_notices(
            intent,
            selections,
            notices,
            confirmation_components,
            confirmation_options,
        )
        notices = self._simplify_component_confirmation_notices(
            intent,
            selections,
            notices,
            confirmation_components,
            confirmation_options,
        )
        notices = self._apply_customer_question_language_policy(
            notices,
            confirmation_components,
            confirmation_options,
        )
        # Component adapters may add or reword a region question after the
        # earlier cleanup pass.  Region resolution is quote-wide, so run the
        # guard again at the final presentation boundary and remove its stale
        # component metadata as well.
        notices_before_region_cleanup = set(notices)
        notices = self._drop_resolved_region_questions(intent, notices)
        for removed_notice in notices_before_region_cleanup.difference(notices):
            confirmation_components.pop(removed_notice, None)
            confirmation_options.pop(removed_notice, None)
        # Model availability is region-dependent.  When the request omitted
        # region, preview may use a private fallback only to warm the catalog;
        # that fallback must never produce a second customer question claiming
        # the requested model is unavailable "in the current region". Ask for
        # region first, then validate the exact locked model on the next pass.
        pre_region_notices = set(notices)
        notices = self._drop_pre_region_catalog_questions(
            intent,
            notices,
            confirmation_components,
            confirmation_options,
        )
        for removed_notice in pre_region_notices.difference(notices):
            confirmation_components.pop(removed_notice, None)
            confirmation_options.pop(removed_notice, None)
        # Managed-vs-self-hosted decisions are a staged workflow. Do not show
        # machine sizing or unrelated questions until architecture is chosen;
        # if self-hosting is chosen, show only its machine count/shape next.
        pending_architecture = {
            str(index)
            for index, component in enumerate(intent.services)
            if component.field_sources.get("_pending_architecture_decision")
        }
        if pending_architecture:
            for notice in notices:
                if notice in confirmation_components:
                    continue
                folded_notice = notice.casefold()
                if "自建" in notice and any(
                    marker in folded_notice for marker in ("托管", "managed", "aws")
                ):
                    component_id = self._architecture_notice_component_id(
                        intent, notice, pending_architecture
                    )
                    if component_id is not None:
                        component = intent.services[int(component_id)]
                        confirmation_components[notice] = (
                            component_id,
                            component.service,
                        )
        if pending_architecture:
            # The architecture question owns the dependent EC2 picker. Drop
            # only the duplicate standalone machine question; keep unrelated
            # component questions visible on this same page.
            notices = [
                notice
                for notice in notices
                if confirmation_components.get(notice, (None, None))[0] not in pending_architecture
                or (
                    "自建" in notice
                    and any(marker in notice.casefold() for marker in ("托管", "managed", "aws"))
                )
            ]
        # The customer must see one consolidated question page only.  After
        # they submit that page, any newly exposed catalog-only model choice
        # is deterministic: use the cheapest official model matching the
        # already confirmed CPU and memory, then continue to the final editable
        # configuration table.  Business decisions (for example managed vs
        # self-hosted) are never auto-selected here.
        if request.draft_id and completed_confirmation_rounds > 0:
            selection_positions = {
                selection.component_id: position for position, selection in enumerate(selections)
            }
            auto_resolved_notices: set[str] = set()
            for notice in notices:
                component = confirmation_components.get(notice)
                if component is None or component[0] in pending_architecture:
                    continue
                options = confirmation_options.get(notice, [])
                if not options or not any(option.model for option in options):
                    continue
                position = selection_positions.get(component[0])
                if position is None:
                    continue
                selection = selections[position]
                candidates = [candidate for candidate in selection.candidates if candidate.model]
                if not candidates:
                    continue
                requested_vcpu = intent.services[int(component[0])].requirements.get("vcpu")
                requested_memory = intent.services[int(component[0])].requirements.get("memory_gib")
                exact_shape = [
                    candidate
                    for candidate in candidates
                    if (
                        requested_vcpu is None
                        or candidate.specifications.get("vCPU") == requested_vcpu
                    )
                    and (
                        requested_memory is None
                        or candidate.specifications.get("memoryGiB") == requested_memory
                    )
                ]
                if exact_shape:
                    candidates = exact_shape
                cheapest = min(
                    candidates,
                    key=lambda candidate: (
                        candidate.monthly_catalog_cost is None,
                        candidate.monthly_catalog_cost
                        if candidate.monthly_catalog_cost is not None
                        else float("inf"),
                        candidate.model,
                    ),
                )
                component_index = int(component[0])
                requirement = intent.services[component_index]
                requirement.requirements["requested_model"] = cheapest.model
                requirement.field_sources["requirements.requested_model"] = (
                    "system_cheapest_official_match"
                )
                vcpu = cheapest.specifications.get("vCPU")
                memory = cheapest.specifications.get("memoryGiB")
                if "vcpu" not in requirement.requirements and isinstance(vcpu, (int, float)):
                    requirement.requirements["vcpu"] = vcpu
                if "memory_gib" not in requirement.requirements and isinstance(
                    memory, (int, float)
                ):
                    requirement.requirements["memory_gib"] = memory
                selections[position] = selection.model_copy(
                    update={
                        "requested_model": cheapest.model,
                        "selected_model": cheapest.model,
                        "selection_reason": "已自动选择满足配置的最低价官方型号",
                        "requirements": dict(requirement.requirements),
                        "requires_confirmation": False,
                        "confirmation_reason": None,
                        "status": "ready",
                        "issue_message": None,
                    }
                )
                auto_resolved_notices.add(notice)
            if auto_resolved_notices:
                notices = [notice for notice in notices if notice not in auto_resolved_notices]
                for notice in auto_resolved_notices:
                    confirmation_components.pop(notice, None)
                    confirmation_options.pop(notice, None)
        # A cross-field/design conflict can be discovered before an individual
        # adapter previews successfully (for example Windows on an ARM EC2
        # model). Reflect that conflict on the corresponding component card.
        customer_issues_by_component: dict[str, list[str]] = {}
        for notice in notices:
            component = confirmation_components.get(notice)
            if component is None:
                continue
            customer_issues_by_component.setdefault(component[0], []).append(notice)
        selections = [
            selection.model_copy(
                update={
                    "status": "customer_issue",
                    "requires_confirmation": True,
                    "confirmation_reason": "；".join(
                        customer_issues_by_component[selection.component_id]
                    ),
                    "issue_message": "；".join(
                        customer_issues_by_component[selection.component_id]
                    ),
                }
            )
            if selection.component_id in customer_issues_by_component
            else selection
            for selection in selections
        ]
        # A product adapter that is temporarily unavailable must stay isolated
        # to its own component. Persist the skip reason in the structured draft
        # so the quote stage can omit that component explicitly instead of
        # failing the complete workload or silently losing it.
        if reporter:
            await reporter(
                "review_options_start",
                "组件规格核验已完成，正在整理可编辑选项",
            )
        for selection in selections:
            try:
                selected_component_index = int(selection.component_id)
            except (TypeError, ValueError):
                selected_component_index = -1
            if 0 <= selected_component_index < len(intent.services):
                intent.services[selected_component_index].requirements[
                    "_review_product_identity"
                ] = customer_product_identity(intent.services[selected_component_index])
                intent.services[selected_component_index].requirements[
                    "_review_service"
                ] = intent.services[selected_component_index].service
                billing_fields, billing_labels = self._configuration_billing_metadata(
                    intent.services[selected_component_index]
                )
                if billing_fields:
                    intent.services[selected_component_index].requirements[
                        "_review_billing_fields"
                    ] = billing_fields
                if billing_labels:
                    intent.services[selected_component_index].requirements[
                        "_review_billing_labels"
                    ] = billing_labels
                component_requirements = intent.services[
                    selected_component_index
                ].requirements
                component_requirements["_review_status"] = selection.status
                if selection.status == "customer_issue" and selection.confirmation_reason:
                    component_requirements["_review_confirmation_reason"] = (
                        selection.confirmation_reason
                    )
                    component_requirements["_review_confirmation_candidates"] = [
                        candidate.model_dump(mode="json")
                        for candidate in selection.candidates
                    ]
                else:
                    component_requirements.pop("_review_confirmation_reason", None)
                    component_requirements.pop("_review_confirmation_candidates", None)
                if (
                    configuration_revision_scope_ids is not None
                    and selected_component_index not in configuration_revision_scope_ids
                ):
                    # Its last selected model, field options and broad shape
                    # cache are already persisted.  Preserve them byte-for-byte
                    # instead of repeating optional catalog discovery.
                    continue
                try:
                    metadata_candidates = await asyncio.wait_for(
                        self._configuration_candidates(
                            selection, intent.services[selected_component_index]
                        ),
                        timeout=8,
                    )
                except TimeoutError:
                    # Edit-control enrichment is optional. It must never keep
                    # a fully validated quote spinning for minutes when an AWS
                    # catalog endpoint is slow.
                    logger.warning(
                        "Timed out expanding optional edit choices for %s",
                        selection.display_name,
                    )
                    metadata_candidates = selection.candidates
                shape_pairs = {
                    (
                        float(candidate.specifications["vCPU"]),
                        float(candidate.specifications["memoryGiB"]),
                    )
                    for candidate in metadata_candidates
                    if isinstance(candidate.specifications.get("vCPU"), (int, float))
                    and isinstance(candidate.specifications.get("memoryGiB"), (int, float))
                }
                previous_shapes = intent.services[selected_component_index].requirements.get(
                    "_review_available_shapes", []
                )
                if isinstance(previous_shapes, list):
                    shape_pairs.update(
                        (
                            float(shape["vcpu"]),
                            float(shape["memory_gib"]),
                        )
                        for shape in previous_shapes
                        if isinstance(shape, dict)
                        and isinstance(shape.get("vcpu"), (int, float))
                        and isinstance(shape.get("memory_gib"), (int, float))
                    )
                if shape_pairs:
                    intent.services[selected_component_index].requirements[
                        "_review_available_shapes"
                    ] = [
                        {"vcpu": vcpu, "memory_gib": memory} for vcpu, memory in sorted(shape_pairs)
                    ]
                specification_fields = {
                    "vCPU": "vcpu",
                    "memoryGiB": "memory_gib",
                    "operatingSystem": "operating_system",
                    "storageGiB": "storage_gib",
                    "storageGiBPerNode": "storage_gib_per_node",
                    "storageGiBPerBroker": "storage_gib_per_broker",
                }
                field_options: dict[str, list[object]] = {}
                previous_field_options = intent.services[selected_component_index].requirements.get(
                    "_review_field_options", {}
                )
                if isinstance(previous_field_options, dict):
                    for field, values in previous_field_options.items():
                        if isinstance(field, str) and isinstance(values, list):
                            field_options[field] = list(values)
                for candidate in metadata_candidates:
                    for raw_field, value in candidate.specifications.items():
                        if not isinstance(value, (str, int, float, bool)) or value == "":
                            continue
                        field = (
                            specification_fields.get(raw_field)
                            or re.sub(r"(?<!^)(?=[A-Z])", "_", raw_field).casefold()
                        )
                        values = field_options.setdefault(field, [])
                        if value not in values:
                            values.append(value)
                component = intent.services[selected_component_index]
                kind = self._service_kind(component.service)
                option_plugin = self._plugins.get(kind) if kind is not None else None
                option_provider = getattr(
                    option_plugin, "configuration_field_options", None
                )
                if callable(option_provider) and not field_options.get("engine_version"):
                    try:
                        official_field_options = await asyncio.wait_for(
                            asyncio.to_thread(
                                option_provider,
                                component.model_copy(deep=True),
                                selection.region or "ap-southeast-1",
                            ),
                            timeout=8,
                        )
                    except Exception:  # Optional edit metadata must not block preview.
                        official_field_options = {}
                    if isinstance(official_field_options, dict):
                        for field, values in official_field_options.items():
                            if not isinstance(field, str) or not isinstance(values, list):
                                continue
                            target = field_options.setdefault(field, [])
                            for value in values:
                                if (
                                    isinstance(value, (str, int, float, bool))
                                    and value not in target
                                ):
                                    target.append(value)
                if field_options:
                    intent.services[selected_component_index].requirements[
                        "_review_field_options"
                    ] = {
                        field: sorted(values, key=lambda value: str(value))
                        for field, values in field_options.items()
                    }
            if (
                selection.status == "ready"
                and selection.selected_model
                and 0 <= selected_component_index < len(intent.services)
            ):
                intent.services[selected_component_index].requirements["_review_selected_model"] = (
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
                if selected_candidate is not None:
                    intent.services[selected_component_index].requirements[
                        "_review_selected_specifications"
                    ] = dict(selected_candidate.specifications)
            elif 0 <= selected_component_index < len(intent.services):
                intent.services[selected_component_index].requirements.pop(
                    "_review_selected_model", None
                )
                intent.services[selected_component_index].requirements.pop(
                    "_review_selected_specifications", None
                )
            if selection.status not in {"technical_issue", "unsupported"}:
                try:
                    component_index = int(selection.component_id)
                except (TypeError, ValueError):
                    continue
                if 0 <= component_index < len(intent.services):
                    for field in (
                        "_quote_skip_reason",
                        "_quote_skip_code",
                        "_quote_skip_category",
                    ):
                        intent.services[component_index].requirements.pop(field, None)
                continue
            try:
                component_index = int(selection.component_id)
            except (TypeError, ValueError):
                continue
            if not 0 <= component_index < len(intent.services):
                continue
            intent.services[component_index].requirements["_quote_skip_reason"] = (
                selection.issue_message
                or selection.selection_reason
                or "该组件的官方报价适配器暂不可用"
            )
            intent.services[component_index].requirements["_quote_skip_code"] = (
                selection.issue_code
                or ("unsupported_service" if selection.status == "unsupported" else "unknown")
            )
            intent.services[component_index].requirements["_quote_skip_category"] = (
                selection.issue_category
                or ("unsupported" if selection.status == "unsupported" else "system_configuration")
            )
        if reporter:
            await reporter(
                "review_options_done",
                "可编辑选项已整理完成，正在生成确认页面",
            )
        disabled_region = next(
            (error for error in technical_errors if error.code == "aws_region_not_enabled"),
            None,
        )
        if disabled_region is not None:
            region = str(disabled_region.details.get("region", "")).strip()
            raise ManualConfirmationRequired(
                f"AWS 账号尚未启用区域 {region}，请先在 AWS 控制台启用该区域，或改选已启用区域后重试",
                code="aws_region_not_enabled",
                region=region,
            )
        if any(error.code == "aws_credentials_invalid" for error in technical_errors):
            raise ManualConfirmationRequired(
                "后端 AWS 凭证已失效，无法实时查询官方规格。请更新后端环境变量凭证或配置 IAM Role 后重试",
                code="aws_credentials_invalid",
            )
        for notice in notices:
            if not confirmation_options.get(notice):
                generated = (
                    self._region_confirmation_options()
                    if self._is_region_confirmation_notice(notice)
                    else self._default_confirmation_options(notice)
                )
                if generated:
                    confirmation_options[notice] = generated
        confirmation_items = [
            ConfirmationItem(
                question=notice,
                answer_key=self._confirmation_answer_key(
                    confirmation_components.get(notice, (None, None))[0],
                    notice,
                ),
                options=confirmation_options.get(notice, []),
                dependent_options=(
                    self._compact_candidate_options(
                        next(
                            selection.candidates
                            for selection in selections
                            if selection.component_id
                            == confirmation_components.get(notice, (None, None))[0]
                        ),
                        intent.services[int(confirmation_components[notice][0])],
                    )
                    if (
                        "自建" in notice
                        and any(
                            marker in notice.casefold() for marker in ("托管", "managed", "aws")
                        )
                        and notice in confirmation_components
                        and any(
                            selection.component_id == confirmation_components[notice][0]
                            and selection.candidates
                            for selection in selections
                        )
                    )
                    else []
                ),
                dependent_on_values=(
                    ["nacos_self_hosted", "self_hosted"]
                    if "自建" in notice
                    and any(marker in notice.casefold() for marker in ("托管", "managed", "aws"))
                    else []
                ),
                component_id=confirmation_components.get(notice, (None, None))[0],
                service=confirmation_components.get(notice, (None, None))[1],
                selection_mode=self._confirmation_selection_mode(
                    notice, confirmation_options.get(notice, [])
                ),
            )
            for notice in notices
        ]
        unavailable_choices = [
            item.question
            for item in confirmation_items
            if item.selection_mode != "text" and not item.options
        ]
        if unavailable_choices:
            raise ManualConfirmationRequired(
                "官方可选项尚未准备完成，系统已阻止把选择题降级为手动填写",
                code="confirmation_options_unavailable",
                questions=unavailable_choices,
            )
        # A quote task owns one stable draft and therefore one stable customer
        # confirmation URL across every review round.
        draft_id = request.draft_id or f"aw{uuid.uuid4().hex[:10]}"
        if len(self._drafts) >= 100:
            self._drafts.pop(next(iter(self._drafts)))
        # Persist drafts even when confirmation is required.  The next answer
        # mutates this structured intent directly instead of appending prose to
        # the original request and asking the model to parse everything again.
        self._drafts[draft_id] = (request.customer_request, intent.model_copy(deep=True))
        if prior_asked:
            self._asked_confirmation_questions[draft_id] = set(prior_asked)
            self._confirmation_rounds[draft_id] = self._confirmation_rounds.get(
                request.draft_id or "", 0
            )
        # Internal catalog/system failures are handled on the sales side and
        # must not consume a customer question before a safe link exists.
        internal_validation_failed = any(
            selection.status in {"technical_issue", "unsupported"}
            for selection in selections
        )
        if confirmation_items and (
            self._confirmation_sessions is None or not internal_validation_failed
        ):
            asked = self._asked_confirmation_questions.setdefault(draft_id, set())
            asked.update(
                self._confirmation_question_key(item.question) for item in confirmation_items
            )
            self._confirmation_rounds[draft_id] = self._confirmation_rounds.get(draft_id, 0) + 1
        confirmation_text = self._confirmation_text(notices)
        confirmation_token = None
        unsupported_components = sum(selection.status == "unsupported" for selection in selections)
        technical_components = sum(
            selection.status == "technical_issue" for selection in selections
        )
        sales_validation_required = bool(unsupported_components or technical_components)
        sales_validation_message = (
            f"系统正在自动处理 {unsupported_components + technical_components} 项组件；"
            "已通过组件已锁定，不会重复运行。全部完成后会自动生成客户链接。"
            if sales_validation_required
            else None
        )
        configuration_review_required = False
        link_already_published = bool(
            self._confirmation_sessions is not None
            and self._confirmation_sessions.status_by_draft(draft_id) is not None
        )
        # Sales does not approve technical configuration. The system publishes
        # the customer link automatically only after its own official checks
        # pass; an already-issued link remains stable during later rechecks.
        customer_link_publication_allowed = bool(
            link_already_published or not sales_validation_required
        )
        if self._confirmation_sessions is not None and customer_link_publication_allowed:
            if confirmation_text:
                if configuration_revision_requested:
                    # A later edit belongs to the final configuration table.
                    # Never throw the customer back into the full-page initial
                    # questionnaire and never publish the AI's unvalidated
                    # working copy. Roll back the whole edit transaction so
                    # model, deployment, storage and every other old field stay
                    # intact together.
                    if configuration_revision_original_intent is not None:
                        intent = configuration_revision_original_intent.model_copy(deep=True)
                    rollback_notice = "当前区域没有完全相同的规格，已保留原配置，请重新修改。"
                    self._drafts[draft_id] = (
                        request.customer_request,
                        intent.model_copy(deep=True),
                    )
                    confirmation_token = self._confirmation_sessions.create_or_replace(
                        draft_id=draft_id,
                        customer_request=request.customer_request,
                        customer_summary=intent.customer_summary,
                        intent=intent,
                        confirmation_text=rollback_notice,
                        items=[],
                        quote_request=request,
                    )
                    confirmation_token = self._confirmation_sessions.prepare_configuration_review(
                        draft_id=draft_id,
                        intent=intent,
                        confirmation_text=rollback_notice,
                    )
                    configuration_review_required = confirmation_token is not None
                    confirmation_items = []
                    notices = []
                    confirmation_text = None
                else:
                    confirmation_token = self._confirmation_sessions.create_or_replace(
                        draft_id=draft_id,
                        customer_request=request.customer_request,
                        customer_summary=intent.customer_summary,
                        intent=intent,
                        confirmation_text=confirmation_text,
                        items=confirmation_items,
                        quote_request=request,
                    )
            else:
                # Once all customer questions are resolved, always return the
                # same confirmation link to the final configuration table.
                # A temporarily unavailable official lookup is displayed on
                # its component, but must never leave the customer link stuck
                # forever in ``reviewing``.
                confirmation_token = self._confirmation_sessions.create_or_replace(
                    draft_id=draft_id,
                    customer_request=request.customer_request,
                    customer_summary=intent.customer_summary,
                    intent=intent,
                    confirmation_text="请确认最终配置清单，确认后系统才会开始报价。",
                    items=[],
                    quote_request=request,
                )
                confirmation_token = self._confirmation_sessions.prepare_configuration_review(
                    draft_id=draft_id,
                    intent=intent,
                )
                configuration_review_required = confirmation_token is not None
        expert_status = (
            "awaiting_customer"
            if confirmation_items
            else "partial"
            if unsupported_components or technical_components
            else "ready"
        )
        expert_review = ExpertReview(
            run_id=f"expert-{uuid.uuid4().hex[:10]}",
            provider=self._ai_provider,
            status=expert_status,
            ai_calls=sum(event.stage == "ai_response" for event in ai_trace),
            components=len(selections),
            official_checks=sum(selection.status != "unsupported" for selection in selections),
            customer_questions=len(confirmation_items),
            unsupported_components=unsupported_components,
            safeguards=[
                "AWS 账号仅允许官方规格与价格只读查询",
                "配置处理引擎不生成单价，也不能购买、删除或修改 AWS 资源",
                "组件失败相互隔离，不生成空报价或猜测价格",
            ],
        )
        trace.append(
            ExecutionEvent(
                stage="agent",
                message=(
                    f"系统已整理 {len(selections)} 个组件，完成 "
                    f"{expert_review.official_checks} 项 AWS 官方核验"
                ),
            )
        )
        return QuotePreviewResponse(
            draft_id=draft_id,
            customer_summary=intent.customer_summary,
            selections=selections,
            notices=notices,
            confirmation_text=confirmation_text,
            confirmation_items=confirmation_items,
            confirmation_token=confirmation_token,
            configuration_review_required=configuration_review_required,
            sales_validation_required=sales_validation_required,
            sales_validation_message=sales_validation_message,
            execution_trace=trace,
            expert_review=expert_review,
        )

    @staticmethod
    def _apply_sales_region(intent: ParsedIntent, sales_region: str | None) -> None:
        """Fill unresolved regional components from the salesperson's choice."""

        if not sales_region:
            return
        global_services = {
            ServiceKind.CLOUDFRONT,
            ServiceKind.ROUTE53,
            ServiceKind.GLOBAL_ACCELERATOR,
        }
        for component in intent.services:
            if QuoteService._service_kind(component.service) in global_services:
                continue
            if component.region is None:
                component.region = sales_region
                component.field_sources["region"] = "sales_confirmation"
        regional = [
            component
            for component in intent.services
            if QuoteService._service_kind(component.service) not in global_services
        ]
        if regional and all(component.region for component in regional):
            intent.ambiguities = [
                ambiguity
                for ambiguity in intent.ambiguities
                if not QuoteService._is_region_confirmation_notice(ambiguity)
            ]

    @staticmethod
    def _sizing_confirmation_question(item: dict[str, object]) -> str:
        requested = item.get("requested")
        options = item.get("options")
        assert isinstance(requested, dict)
        assert isinstance(options, list)
        choices: list[str] = []
        for option in options:
            assert isinstance(option, dict)
            suffix = "（偏低）" if str(option["label"]).startswith("较低") else "（不低配）"
            choices.append(f"{float(option['vcpu']):g}核{float(option['memory_gib']):g}G{suffix}")
        return (
            f"服务器没有 {float(requested.get('vcpu')):g}核"
            f"{float(requested.get('memory_gib')):g}G，选{'，还是'.join(choices)}？"
        )

    @classmethod
    def _service_index_for_notice(cls, intent: ParsedIntent, notice: str) -> int | None:
        """Associate a customer-facing design notice with one workload card."""

        folded = notice.casefold()
        numbered = re.search(r"【?组件\s*(\d+)", notice, re.I)
        if numbered:
            index = int(numbered.group(1)) - 1
            if 0 <= index < len(intent.services):
                return index
        aliases = {
            "ec2": ("ec2", "服务器", "实例", "windows", "arm"),
            "rds": ("rds", "数据库", "mysql", "postgresql", "multi-az"),
            "elasticache": ("elasticache", "redis", "valkey", "缓存"),
            "elb": ("alb", "nlb", "负载均衡", "load balancer"),
            "s3": ("s3", "对象存储"),
            "cloudfront": ("cloudfront", "cdn"),
            "waf": ("waf",),
            "msk": ("msk", "kafka", "broker"),
            "apigateway": ("api gateway", "api 网关", "网关"),
            "scheduler": ("eventbridge", "scheduler", "定时"),
        }
        for index, service in enumerate(intent.services):
            requested_model = str(service.requirements.get("requested_model") or "")
            if requested_model and requested_model.casefold() in folded:
                return index
        for index, service in enumerate(intent.services):
            kind = cls._service_kind(service.service)
            key = kind.value if kind is not None else service.service.casefold()
            if any(alias in folded for alias in aliases.get(key, (key,))):
                return index
        return None

    @staticmethod
    def _compact_candidate_options(
        candidates: list[CandidateOption], requirement: object
    ) -> list[ConfirmationOption]:
        requirements = getattr(requirement, "requirements", {})
        requested_vcpu = requirements.get("vcpu") if isinstance(requirements, dict) else None
        requested_memory = (
            requirements.get("memory_gib") if isinstance(requirements, dict) else None
        )
        if not isinstance(requested_vcpu, (int, float)):
            requested_vcpu = None
        if not isinstance(requested_memory, (int, float)):
            requested_memory = None

        def distance(candidate: CandidateOption) -> float:
            vcpu = candidate.specifications.get("vCPU")
            memory = candidate.specifications.get("memoryGiB")
            return (
                abs(float(vcpu) - float(requested_vcpu)) / max(float(requested_vcpu), 1)
                if requested_vcpu is not None and isinstance(vcpu, (int, float))
                else 0
            ) + (
                abs(float(memory) - float(requested_memory)) / max(float(requested_memory), 1)
                if requested_memory is not None and isinstance(memory, (int, float))
                else 0
            )

        unique: dict[str, CandidateOption] = {}
        for candidate in candidates:
            unique.setdefault(candidate.model.casefold(), candidate)
        ordered = sorted(
            unique.values(),
            key=lambda candidate: (
                distance(candidate),
                candidate.monthly_catalog_cost is None,
                candidate.monthly_catalog_cost or float("inf"),
                candidate.model,
            ),
        )
        options: list[ConfirmationOption] = []
        for candidate in ordered:
            if candidate.family == "aws_region":
                label = str(candidate.specifications.get("label") or candidate.model)
                options.append(
                    ConfirmationOption(
                        label=label,
                        value=candidate.model,
                        specifications=candidate.specifications,
                    )
                )
                continue
            if candidate.family == "service_replacement":
                decision = str(candidate.specifications.get("decision") or "").strip()
                if decision:
                    options.append(
                        ConfirmationOption(
                            label=candidate.model,
                            value=decision,
                            specifications=candidate.specifications,
                        )
                    )
                continue
            if candidate.family == "billing_variant":
                decision = str(candidate.specifications.get("decision") or "").strip()
                if decision:
                    options.append(
                        ConfirmationOption(
                            label=candidate.model,
                            value=decision,
                            specifications=candidate.specifications,
                        )
                    )
                continue
            options.append(
                ConfirmationOption(
                    label=" · ".join(
                        part
                        for part in (
                            candidate.model,
                            (
                                f"{candidate.specifications.get('vCPU'):g} vCPU"
                                if isinstance(
                                    candidate.specifications.get("vCPU"), (int, float)
                                )
                                else None
                            ),
                            (
                                f"{candidate.specifications.get('memoryGiB'):g} GiB"
                                if isinstance(
                                    candidate.specifications.get("memoryGiB"), (int, float)
                                )
                                else None
                            ),
                        )
                        if part
                    ),
                    value=f"选择 {candidate.model}",
                    model=candidate.model,
                    specifications=candidate.specifications,
                    monthly_catalog_cost=candidate.monthly_catalog_cost,
                )
            )
        return options

    @staticmethod
    def _enforce_catalog_sizing_invariant(
        requirement: ServiceRequirement,
        selection: PreviewSelection,
    ) -> PreviewSelection:
        """Prevent every catalog adapter from silently changing a customer shape.

        Literal values are exact by default. Only wording such as ``约`` or
        ``至少`` authorizes approximate or upward matching. Keeping this rule
        above all plugins also protects services added in the future.
        """

        if requirement.field_sources.get("_customer_select_configuration"):
            return selection
        requested = canonicalize_requirement_fields(
            requirement.requirements,
            service=requirement.service,
        )
        requested_model = str(requested.get("requested_model") or "").strip().casefold()
        if requested_model and str(selection.selected_model or "").casefold() == requested_model:
            return selection

        def positive_number(value: object) -> float | None:
            if isinstance(value, bool):
                return None
            try:
                number = float(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None
            return number if number > 0 else None

        requested_vcpu = positive_number(requested.get("vcpu"))
        requested_memory = positive_number(requested.get("memory_gib"))
        policies = {
            "vcpu": customer_match_policy(requirement, "vcpu"),
            "memory_gib": customer_match_policy(requirement, "memory_gib"),
        }
        # Some fully managed products (notably WorkSpaces) publish bundle and
        # billing dimensions without exposing an EC2-style vCPU/memory shape
        # on the selected Price List row. Missing comparison metadata is not
        # evidence that the customer's requested shape is unavailable. Only
        # enforce a shape invariant for fields the official candidate catalog
        # actually exposes; otherwise retain the requirement for pricing and
        # avoid a false customer question with one empty "official dimension".
        comparable_fields = {
            "vcpu": any(
                positive_number(candidate.specifications.get("vCPU")) is not None
                for candidate in selection.candidates
            ),
            "memory_gib": any(
                positive_number(candidate.specifications.get("memoryGiB")) is not None
                for candidate in selection.candidates
            ),
        }
        policies = {
            field: policy if comparable_fields[field] else None
            for field, policy in policies.items()
        }
        if not any(policies.values()):
            return selection

        eligible: list[CandidateOption] = []
        for candidate in selection.candidates:
            candidate_vcpu = positive_number(candidate.specifications.get("vCPU"))
            candidate_memory = positive_number(candidate.specifications.get("memoryGiB"))
            matches = True
            for field, requested_value, candidate_value in (
                ("vcpu", requested_vcpu, candidate_vcpu),
                ("memory_gib", requested_memory, candidate_memory),
            ):
                policy = policies[field]
                if policy is None or requested_value is None:
                    continue
                if candidate_value is None:
                    matches = False
                    break
                if policy == "exact" and abs(candidate_value - requested_value) > 0.001:
                    matches = False
                    break
                if policy in {"minimum", "approximate"} and candidate_value < requested_value:
                    matches = False
                    break
            if not matches:
                continue
            eligible.append(candidate)
        if not eligible:
            exact_fields = [
                ("vCPU" if field == "vcpu" else "内存", requested.get(field))
                for field, policy in policies.items()
                if policy == "exact" and requested.get(field) is not None
            ]
            requested_shape = "、".join(
                f"{label} {float(value):g}" for label, value in exact_fields
            )
            return selection.model_copy(
                update={
                    "selected_model": None,
                    "candidates": [
                        candidate.model_copy(update={"is_default": False})
                        for candidate in selection.candidates
                    ],
                    "requires_confirmation": True,
                    "confirmation_reason": (
                        f"客户要求的精确规格（{requested_shape}）在当前区域没有完全一致的官方型号，"
                        "系统不会自动放大、缩小或替换。请从下方官方可用配置中选择。"
                    ),
                    "status": "customer_issue",
                    "issue_message": "客户精确规格与官方型号不一致，等待客户选择。",
                    "issue_code": "exact_customer_shape_not_available",
                }
            )

        chosen = min(
            eligible,
            key=lambda candidate: (
                candidate.monthly_catalog_cost is None,
                candidate.monthly_catalog_cost
                if candidate.monthly_catalog_cost is not None
                else float("inf"),
                not candidate.is_default,
                candidate.model,
            ),
        )
        candidates = [
            candidate.model_copy(update={"is_default": candidate.model == chosen.model})
            for candidate in selection.candidates
        ]
        return selection.model_copy(
            update={
                "selected_model": chosen.model,
                "selection_reason": (
                    "已选择与客户精确规格一致的最低价官方型号。"
                    if any(policy == "exact" for policy in policies.values())
                    else "已选择满足客户约值或规格下限的最低价官方型号。"
                ),
                "candidates": candidates,
                "requires_confirmation": False,
                "confirmation_reason": None,
                "status": "ready",
                "issue_message": None,
            }
        )

    async def _configuration_candidates(
        self,
        selection: PreviewSelection,
        component: ServiceRequirement,
    ) -> list[CandidateOption]:
        """Return broad official choices used to build generic edit controls.

        Normal quoting may intentionally narrow candidates to an already
        selected model.  Configuration editing needs the wider regional
        catalog so CPU can constrain memory and future plugins can expose
        their own finite option sets without frontend service-specific code.
        """

        if len(selection.candidates) > 1:
            return selection.candidates

        kind = self._service_kind(component.service)
        plugin = self._plugins.get(kind) if kind is not None else self._generic_plugin
        if plugin is None:
            return selection.candidates
        # Usage-based managed services (for example QuickSight and Kinesis)
        # have no CPU/model matrix to expand. Their editable billing fields
        # are loaded from the auto-discovered profile separately; rerunning a
        # broad product scan here only delays the confirmation page.
        has_requested_shape = any(
            component.requirements.get(field) not in (None, "")
            for field in ("requested_model", "vcpu", "memory_gib")
        )
        if kind is None and not has_requested_shape and selection.selected_model in {
            None,
            "",
            "AWS 官方计费维度",
            "官方单位参考价",
        }:
            return selection.candidates
        discovery = component.model_copy(deep=True)
        for field in (
            "requested_model",
            "vcpu",
            "memory_gib",
            "master_requested_model",
            "master_vcpu",
            "master_memory_gib",
            "core_requested_model",
            "core_vcpu",
            "core_memory_gib",
            "task_requested_model",
            "task_vcpu",
            "task_memory_gib",
            "_review_selected_model",
            "_review_selected_specifications",
        ):
            discovery.requirements.pop(field, None)
        service_key = kind.value if kind is not None else discovery.service
        context = {
            key: value
            for key, value in discovery.requirements.items()
            if not key.startswith("_")
            and key
            not in {
                "system_disk_gib",
                "storage_gib",
                "storage_gib_per_node",
                "storage_gib_per_broker",
                "data_transfer_out_gib",
                "data_transfer_in_gib",
                "data_processed_gib",
                "requests",
                "https_requests",
            }
        }
        cache_key = (
            service_key,
            discovery.region or selection.region or "ap-southeast-1",
            json.dumps(context, ensure_ascii=False, sort_keys=True, default=str),
        )
        cached = self._configuration_candidate_cache.get(cache_key)
        if cached is not None:
            return cached
        normalized = self._calculator_requirements(
            discovery.requirements, discovery.quantity, service_key
        )
        requirement = self._pricing_requirement_copy(
            discovery, service_key=service_key, requirements=normalized
        )
        self._align_pricing_product_identity(discovery, requirement)
        try:
            candidate_loader = getattr(plugin, "configuration_candidates", None)
            if callable(candidate_loader):
                candidates = await asyncio.to_thread(
                    candidate_loader, requirement, "ap-southeast-1"
                )
                if candidates:
                    self._configuration_candidate_cache[cache_key] = candidates
                    return candidates
            preview = await asyncio.to_thread(plugin.preview, requirement, "ap-southeast-1")
        except Exception:  # Optional metadata discovery must never block quoting.
            logger.debug(
                "Could not expand configuration choices for %s",
                service_key,
                exc_info=True,
            )
            return selection.candidates
        candidates = preview.candidates or selection.candidates
        self._configuration_candidate_cache[cache_key] = candidates
        return candidates

    def _configuration_billing_metadata(
        self, component: ServiceRequirement
    ) -> tuple[list[str], dict[str, str]]:
        """Expose exact cached billing bindings for first-use AWS services."""

        plugin = self._generic_plugin
        discovery = getattr(plugin, "auto_discovery", None) if plugin else None
        getter = getattr(discovery, "get_profile", None)
        if not callable(getter):
            return [], {}
        profile = getter(component.service, component.region)
        if not isinstance(profile, dict) or profile.get("status") != "verified":
            return [], {}
        fields: list[str] = []
        labels: dict[str, str] = {}
        bindings = profile.get("field_bindings")
        if not isinstance(bindings, list):
            return fields, labels
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            field = str(binding.get("field") or "").strip()
            label = str(binding.get("label") or "").strip()
            if not field or field in {"requested_model", "vcpu", "memory_gib"}:
                continue
            if field not in fields:
                fields.append(field)
            if label:
                labels[field] = label
        return fields, labels

    @staticmethod
    def _candidate_options_from_error(
        error: ManualConfirmationRequired,
    ) -> list[CandidateOption]:
        """Convert official adapter alternatives into the shared choice type."""

        raw_candidates = error.details.get("nearby_candidates")
        if not isinstance(raw_candidates, list):
            return []

        options: list[CandidateOption] = []
        seen_models: set[str] = set()
        for raw in raw_candidates:
            if not isinstance(raw, dict):
                continue
            model = str(raw.get("model") or "").strip()
            folded_model = model.casefold()
            if not model or folded_model in seen_models:
                continue
            seen_models.add(folded_model)
            raw_specs = raw.get("specifications")
            specifications = dict(raw_specs) if isinstance(raw_specs, dict) else {}
            if raw.get("vcpu") is not None:
                specifications.setdefault("vCPU", raw["vcpu"])
            if raw.get("memory_gib") is not None:
                specifications.setdefault("memoryGiB", raw["memory_gib"])
            official_product = raw.get("official_product")
            options.append(
                CandidateOption(
                    model=model,
                    family=str(raw.get("family") or model.split(".")[0]),
                    specifications=specifications,
                    monthly_catalog_cost=(
                        float(raw["monthly_catalog_cost"])
                        if isinstance(raw.get("monthly_catalog_cost"), (int, float))
                        else None
                    ),
                    rationale=str(raw.get("rationale") or "AWS 官方目录提供的可用相邻规格。"),
                    official_product=(
                        dict(official_product) if isinstance(official_product, dict) else {}
                    ),
                )
            )
        return options

    @staticmethod
    def _question_requires_finite_choice(question: str) -> bool:
        """Identify customer questions that promise a finite set of choices.

        This is deliberately based on the customer-facing contract rather than
        a product allow-list.  New AWS products therefore inherit the same
        rule: wording such as ``请从下方选择`` can never be rendered as a text
        box when an adapter forgot to attach its official options.
        """

        compact = re.sub(r"\s+", "", question).casefold()
        return any(
            marker in compact
            for marker in (
                "请从下方",
                "从下方选择",
                "请选择",
                "重新选择",
                "可选版本",
                "可用配置中选择",
                "选择您需要",
                "采用aws",
                "保留原",
            )
        )

    @classmethod
    def _confirmation_selection_mode(
        cls,
        question: str,
        options: list[ConfirmationOption],
    ) -> str:
        if options:
            return (
                "catalog"
                if (
                    any(option.model for option in options)
                    or len(options) > 6
                    or "数据库版本" in question
                    or "引擎版本" in question
                )
                else "buttons"
            )
        return "buttons" if cls._question_requires_finite_choice(question) else "text"

    async def _confirmation_candidates_for_failure(
        self,
        *,
        plugin: object,
        component: ServiceRequirement,
        failure: ManualConfirmationRequired,
        display_name: str,
    ) -> list[CandidateOption]:
        """Recover finite official choices for any adapter failure.

        Adapters may attach nearby choices directly.  If they do not, run the
        same product-neutral catalog discovery used by the configuration edit
        form after removing exact model/shape locks.  This keeps the policy in
        one place and covers future plugins without frontend product branches.
        """

        attached = self._candidate_options_from_error(failure)
        if attached:
            return attached
        kind = self._service_kind(component.service)
        service_key = kind.value if kind is not None else component.service
        seed = PreviewSelection(
            component_id="component",
            service=service_key,
            display_name=display_name,
            region=component.region or "ap-southeast-1",
            quantity=component.quantity,
            requirements=dict(component.requirements),
            source_text=component.source_text,
            candidates=[],
            requires_confirmation=True,
            confirmation_reason=failure.message,
            status="customer_issue",
            issue_message=failure.message,
        )
        return await self._configuration_candidates(seed, component)

    async def hydrate_confirmation_session_choices(
        self,
        session: ConfirmationSessionResponse,
    ) -> ConfirmationSessionResponse:
        """Repair older pending links that stored a selection with no options."""

        if session.status != "pending":
            return session
        configurations = {
            item.component_id: item for item in session.configuration_items
        }
        hydrated: list[ConfirmationItem] = []
        changed = False
        for item in session.confirmation_items:
            if item.options or not self._question_requires_finite_choice(item.question):
                hydrated.append(item)
                continue
            configuration = configurations.get(item.component_id or "")
            if configuration is None:
                hydrated.append(
                    item.model_copy(update={"selection_mode": "buttons"})
                )
                continue
            component = ServiceRequirement(
                service=configuration.service,
                calculator_service_name=configuration.display_name,
                region=configuration.region,
                quantity=configuration.quantity,
                requirements=dict(configuration.requirements),
                source_text=configuration.source_text,
            )
            kind = self._service_kind(component.service)
            plugin = self._plugins.get(kind) if kind is not None else self._generic_plugin
            if plugin is None:
                hydrated.append(item.model_copy(update={"selection_mode": "buttons"}))
                continue
            seed = PreviewSelection(
                component_id=configuration.component_id,
                service=(kind.value if kind is not None else component.service),
                display_name=configuration.display_name,
                region=configuration.region or "ap-southeast-1",
                quantity=configuration.quantity,
                requirements=dict(configuration.requirements),
                source_text=configuration.source_text,
                candidates=[],
            )
            candidates = await self._configuration_candidates(seed, component)
            options = self._compact_candidate_options(candidates, component)
            if not options:
                hydrated.append(item.model_copy(update={"selection_mode": "buttons"}))
                continue
            changed = True
            hydrated.append(
                item.model_copy(
                    update={
                        "options": options,
                        "selection_mode": self._confirmation_selection_mode(
                            item.question, options
                        ),
                    }
                )
            )
        if not changed:
            return session
        return session.model_copy(update={"confirmation_items": hydrated})

    @staticmethod
    def _default_confirmation_options(notice: str) -> list[ConfirmationOption]:
        """Return safe business choices for questions with a closed answer set.

        These values are intentionally data, not model names. AWS model
        selection still happens later from the official catalog.
        """

        folded = notice.casefold()
        if "cloudfront" in folded and any(
            marker in folded for marker in ("流量地区", "访问者", "traffic geography")
        ):
            return [
                ConfirmationOption(label="亚太地区（Asia Pacific）", value="traffic_geography:Asia Pacific"),
                ConfirmationOption(label="美国（United States）", value="traffic_geography:United States"),
                ConfirmationOption(label="欧洲（Europe）", value="traffic_geography:Europe"),
                ConfirmationOption(label="日本（Japan）", value="traffic_geography:Japan"),
                ConfirmationOption(label="澳大利亚（Australia）", value="traffic_geography:Australia"),
                ConfirmationOption(label="加拿大（Canada）", value="traffic_geography:Canada"),
            ]
        if any(marker in folded for marker in ("rds", "数据库", "mysql")) and any(
            marker in folded
            for marker in (
                "数据库版本",
                "引擎版本",
                "engine version",
                "mysql 版本",
                "可用版本",
            )
        ):
            version_text = (
                notice.split("可选版本：", 1)[1].split("。", 1)[0] if "可选版本：" in notice else ""
            )
            versions = [
                value.strip().rstrip("。；; ")
                for value in re.split(r"[、,，]", version_text)
                if value.strip()
            ]
            if not versions:
                supported = re.search(
                    r"RDS\s*支持的\s*MySQL\s*版本是\s*"
                    r"(\d+(?:\.\d+){1,2}(?:-rds\.\d+)?)",
                    notice,
                    re.IGNORECASE,
                )
                if supported:
                    versions = [supported.group(1)]
            versions = list(dict.fromkeys(versions))
            if "mysql" in folded and not any(
                version.casefold().startswith("8.4") for version in versions
            ):
                versions.insert(0, "8.4")
            else:
                versions.sort(key=lambda version: (not version.casefold().startswith("8.4"),))
            if versions:
                return [
                    ConfirmationOption(
                        label=(
                            f"MySQL {version}（推荐）"
                            if version.casefold().startswith("8.4")
                            else f"MySQL {version}（旧版本，会额外收费）"
                            if version.casefold().startswith(("5.7", "8.0"))
                            else f"MySQL {version}"
                        ),
                        value=f"engine_version:{version}",
                    )
                    for version in versions
                ]
            return [
                ConfirmationOption(
                    label="自动使用当前区域维护版本",
                    value="engine_version:auto",
                )
            ]
        if any(marker in folded for marker in ("redis", "elasticache", "缓存")) and any(
            marker in folded for marker in ("可选版本", "支持的版本", "引擎版本")
        ):
            version_text = (
                notice.split("可选版本：", 1)[1].split("。", 1)[0]
                if "可选版本：" in notice
                else ""
            )
            versions = list(
                dict.fromkeys(
                    value.strip().rstrip("。；; ")
                    for value in re.split(r"[、,，]", version_text)
                    if value.strip()
                )
            )
            return [
                ConfirmationOption(
                    label=f"Redis {version}",
                    value=f"cache_engine_version:{version}",
                )
                for version in versions
            ]
        if any(marker in folded for marker in ("rds", "数据库")) and any(
            marker in folded for marker in ("数据库类型", "数据库引擎", "engine")
        ):
            return [
                ConfirmationOption(label="MySQL", value="mysql"),
                ConfirmationOption(label="PostgreSQL", value="postgresql"),
                ConfirmationOption(label="MariaDB", value="mariadb"),
                ConfirmationOption(label="SQL Server Standard", value="sql_server_standard"),
                ConfirmationOption(label="Oracle", value="oracle"),
                ConfirmationOption(label="Db2", value="db2"),
            ]
        if "nacos" in folded and "cloud map" in folded and "appconfig" in folded:
            node_match = re.search(r"(\d+)\s*个节点", notice)
            node_count = node_match.group(1) if node_match else "原"
            return [
                ConfirmationOption(
                    label=f"继续使用 Nacos（自建 {node_count} 个节点）",
                    value="nacos_self_hosted",
                ),
                ConfirmationOption(
                    label="改用 AWS 托管（Cloud Map + AppConfig）",
                    value="aws_managed_cloudmap_appconfig",
                ),
            ]
        if "自建" in notice and any(marker in folded for marker in ("托管", "managed", "aws")):
            return [
                ConfirmationOption(label="采用 AWS 托管方案", value="aws_managed"),
                ConfirmationOption(label="保留原产品自建", value="self_hosted"),
            ]
        if any(marker in folded for marker in ("rds", "数据库")) and any(
            marker in folded for marker in ("部署方式", "单可用区", "主备", "multi-az", "multi_az")
        ):
            return [
                ConfirmationOption(label="单可用区", value="single_az"),
                ConfirmationOption(label="主备高可用（Multi-AZ）", value="multi_az"),
            ]
        if (
            any(marker in folded for marker in ("redis", "elasticache", "缓存"))
            and any(
                marker in notice
                for marker in (
                    "单节点容量",
                    "每节点",
                    "每个节点",
                    "节点内存",
                    "Redis 容量",
                    "缓存容量",
                    "内存",
                )
            )
            and any(marker in folded for marker in ("缺少", "需要", "大概", "补充"))
        ):
            return [
                ConfirmationOption(label="每节点 1 GiB", value="1G"),
                ConfirmationOption(label="每节点 4 GiB", value="4G"),
                ConfirmationOption(label="每节点 8 GiB", value="8G"),
            ]
        return []

    @staticmethod
    def _region_confirmation_options() -> list[ConfirmationOption]:
        """Offer the complete AWS commercial-region catalog.

        China and GovCloud use separate account partitions and credentials, so
        they intentionally do not appear in a commercial-account quote.
        """

        regions = (
            ("美国东部（弗吉尼亚北部）", "us-east-1"),
            ("美国东部（俄亥俄）", "us-east-2"),
            ("美国西部（加利福尼亚北部）", "us-west-1"),
            ("美国西部（俄勒冈）", "us-west-2"),
            ("非洲（开普敦）", "af-south-1"),
            ("香港", "ap-east-1"),
            ("台北", "ap-east-2"),
            ("孟买", "ap-south-1"),
            ("海得拉巴", "ap-south-2"),
            ("新加坡", "ap-southeast-1"),
            ("悉尼", "ap-southeast-2"),
            ("雅加达", "ap-southeast-3"),
            ("墨尔本", "ap-southeast-4"),
            ("马来西亚", "ap-southeast-5"),
            ("新西兰", "ap-southeast-6"),
            ("泰国", "ap-southeast-7"),
            ("东京", "ap-northeast-1"),
            ("首尔", "ap-northeast-2"),
            ("大阪", "ap-northeast-3"),
            ("加拿大（中部）", "ca-central-1"),
            ("加拿大西部（卡尔加里）", "ca-west-1"),
            ("法兰克福", "eu-central-1"),
            ("苏黎世", "eu-central-2"),
            ("爱尔兰", "eu-west-1"),
            ("伦敦", "eu-west-2"),
            ("巴黎", "eu-west-3"),
            ("斯德哥尔摩", "eu-north-1"),
            ("米兰", "eu-south-1"),
            ("西班牙", "eu-south-2"),
            ("以色列（特拉维夫）", "il-central-1"),
            ("墨西哥（中部）", "mx-central-1"),
            ("中东（巴林）", "me-south-1"),
            ("中东（阿联酋）", "me-central-1"),
            ("南美洲（圣保罗）", "sa-east-1"),
        )
        return [ConfirmationOption(label=f"{name}（{code}）", value=code) for name, code in regions]

    async def _apply_confirmation_responses(
        self,
        intent: ParsedIntent,
        responses: dict[str, str],
        *,
        response_components: dict[str, int] | None = None,
    ) -> None:
        """Apply customer decisions to the saved structured draft.

        Confirmation answers are decisions, not new customer requirements.  In
        particular, reparsing ``original text + answer`` leaves the old conflict
        in place and can ask the same question forever.  Resolve the supported
        decisions here and let the normal AWS preflight verify the updated draft.
        """

        if not responses:
            return

        before = {
            id(service): {
                "region": service.region,
                "quantity": service.quantity,
                "requirements": dict(service.requirements),
            }
            for service in intent.services
        }
        resolved_markers: list[tuple[str, ...]] = []
        excluded_component_ids: set[int] = set()
        response_components = response_components or {}
        for response_key, raw_answer in responses.items():
            question = self._confirmation_response_question(response_key)
            answer = raw_answer.strip()
            if not answer:
                continue
            question_folded = question.casefold()
            answer_folded = answer.casefold()

            def bound_service(
                kind: ServiceKind,
                *,
                current_response_key: str = response_key,
                current_question: str = question,
            ):
                """Prefer the component id carried by this exact form answer."""

                component_index = response_components.get(current_response_key)
                if component_index is not None and 0 <= component_index < len(intent.services):
                    component = intent.services[component_index]
                    if self._service_kind(component.service) == kind:
                        return component
                return self._service_for_confirmation(intent, kind, current_question)

            component_index = response_components.get(response_key)
            if answer_folded == "exclude_component":
                if component_index is not None and 0 <= component_index < len(intent.services):
                    excluded_component_ids.add(component_index)
                    resolved_markers.append((intent.services[component_index].service.casefold(),))
                continue

            if answer_folded.startswith("replace_service:"):
                parts = answer.split(":", 2)
                if (
                    component_index is not None
                    and 0 <= component_index < len(intent.services)
                    and len(parts) >= 2
                ):
                    component = intent.services[component_index]
                    previous_service = component.calculator_service_name or component.service
                    target_service = parts[1].strip().casefold()
                    target_variant = parts[2].strip().casefold() if len(parts) > 2 else ""
                    if target_service == "rds":
                        component.service = "rds"
                        component.calculator_service_name = (
                            "Amazon Aurora PostgreSQL"
                            if target_variant == "aurora_postgresql"
                            else "Amazon RDS"
                        )
                        storage_gib = component.requirements.get("storage_gib")
                        component.requirements = {
                            "engine": target_variant or "postgresql",
                            **(
                                {"storage_gib": storage_gib}
                                if isinstance(storage_gib, (int, float))
                                else {}
                            ),
                            "_replacement_source_service": previous_service,
                        }
                        component.field_sources["service"] = "customer_confirmation"
                        component.field_sources["requirements.engine"] = (
                            "customer_confirmation"
                        )
                        resolved_markers.append(("停止服务",))
                    elif target_service == "ec2":
                        retained_fields = {
                            field: value
                            for field, value in component.requirements.items()
                            if field
                            in {
                                "vcpu",
                                "memory_gib",
                                "storage_gib",
                                "operating_system",
                                "architecture",
                                "tenancy",
                            }
                        }
                        component.service = "ec2"
                        component.calculator_service_name = f"Amazon EC2（自建 {previous_service}）"
                        component.requirements = {
                            **retained_fields,
                            "_replacement_source_service": previous_service,
                        }
                        component.field_sources["service"] = "customer_confirmation"
                        resolved_markers.append(("托管",))
                continue

            if answer_folded.startswith("billing_variant:"):
                parts = answer.split(":", 2)
                component_index = response_components.get(response_key)
                if (
                    len(parts) == 3
                    and component_index is not None
                    and 0 <= component_index < len(intent.services)
                ):
                    field = re.sub(r"[^a-z0-9_]", "", parts[1].casefold())
                    usage_type = parts[2].strip()
                    if field and usage_type:
                        component = intent.services[component_index]
                        key = f"_billing_variant_{field}"
                        component.requirements[key] = usage_type
                        component.field_sources[f"requirements.{key}"] = (
                            "customer_confirmation"
                        )
                        component.field_evidence[f"requirements.{key}"] = (
                            "客户选择了实际收费方式"
                        )
                        component.locked_fields = sorted(
                            set(component.locked_fields) | {f"requirements.{key}"}
                        )
                        resolved_markers.append(("收费方式",))
                continue

            if answer_folded.startswith("traffic_geography:"):
                service = bound_service(ServiceKind.CLOUDFRONT)
                geography = answer.split(":", 1)[1].strip()
                if service is not None and geography:
                    service.requirements["traffic_geography"] = geography
                    service.field_sources["requirements.traffic_geography"] = (
                        "customer_confirmation"
                    )
                    service.field_evidence["requirements.traffic_geography"] = (
                        "客户从 CloudFront 官方流量地区中选择"
                    )
                    service.locked_fields = sorted(
                        set(service.locked_fields) | {"requirements.traffic_geography"}
                    )
                    record_customer_fact_metadata(
                        service,
                        "traffic_geography",
                        "客户从 CloudFront 官方流量地区中选择",
                        policy="exact",
                    )
                    resolved_markers.append(("cloudfront", "地区"))
                continue

            if answer_folded.startswith("cache_engine_version:"):
                service = bound_service(ServiceKind.REDIS)
                selected_version = answer.split(":", 1)[1].strip()
                if service is not None and selected_version:
                    service.requirements["engine_version"] = selected_version
                    service.field_sources["requirements.engine_version"] = (
                        "customer_confirmation"
                    )
                    service.field_evidence["requirements.engine_version"] = (
                        "客户从当前区域支持的 Redis 版本中选择"
                    )
                    service.locked_fields = sorted(
                        set(service.locked_fields) | {"requirements.engine_version"}
                    )
                    record_customer_fact_metadata(
                        service,
                        "engine_version",
                        "客户从当前区域支持的 Redis 版本中选择",
                        policy="exact",
                    )
                    existing_options = service.requirements.get("_review_field_options", {})
                    if not isinstance(existing_options, dict):
                        existing_options = {}
                    service.requirements["_review_field_options"] = {
                        **existing_options,
                        "engine_version": [selected_version],
                    }
                    service.query_action = None
                    resolved_markers.append(("redis", "版本"))
                continue

            if (
                any(marker in question_folded for marker in ("rds", "数据库"))
                and any(
                    marker in question_folded
                    for marker in (
                        "数据库版本",
                        "引擎版本",
                        "engine version",
                        "mysql 版本",
                    )
                )
                and answer_folded.startswith("engine_version:")
            ):
                service = bound_service(ServiceKind.RDS)
                if service is not None:
                    selected_version = answer.split(":", 1)[1].strip()
                    if selected_version.casefold() == "auto":
                        service.requirements.pop("engine_version", None)
                    elif selected_version:
                        service.requirements["engine_version"] = selected_version
                    version_choices_text = question
                    for marker in (
                        "可选版本：",
                        "可选版本:",
                        "支持的 MySQL 版本是",
                        "支持的 mysql 版本是",
                    ):
                        if marker in version_choices_text:
                            version_choices_text = version_choices_text.split(marker, 1)[1]
                            break
                    official_versions = list(
                        dict.fromkeys(
                            re.findall(
                                r"(?<!\d)(\d+\.\d+(?:\.\d+)?(?:-rds\.\d+)?)(?!\d)",
                                version_choices_text,
                                re.I,
                            )
                        )
                    )
                    if selected_version and selected_version.casefold() != "auto":
                        official_versions = list(
                            dict.fromkeys([selected_version, *official_versions])
                        )
                    if official_versions:
                        existing_options = service.requirements.get(
                            "_review_field_options", {}
                        )
                        if not isinstance(existing_options, dict):
                            existing_options = {}
                        service.requirements["_review_field_options"] = {
                            **existing_options,
                            "engine_version": official_versions,
                        }
                    service.query_action = None
                    resolved_marker = next(
                        marker
                        for marker in (
                            "数据库版本",
                            "引擎版本",
                            "engine version",
                            "mysql 版本",
                        )
                        if marker in question_folded
                    )
                    resolved_markers.append((resolved_marker,))
                    continue

            if any(marker in question_folded for marker in ("rds", "数据库")) and any(
                marker in question_folded for marker in ("数据库类型", "数据库引擎", "engine")
            ):
                engines = {
                    "mysql": "mysql",
                    "postgresql": "postgresql",
                    "postgres": "postgresql",
                    "mariadb": "mariadb",
                    "sql_server_standard": "sql_server_standard",
                    "sql server standard": "sql_server_standard",
                    "oracle": "oracle",
                    "db2": "db2",
                }
                engine = engines.get(answer_folded)
                service = bound_service(ServiceKind.RDS)
                if engine and service is not None:
                    service.requirements["engine"] = engine
                    service.requirements.pop("requested_model", None)
                    service.query_action = None
                    resolved_markers.append(("数据库类型",))
                    continue

            if any(
                marker in answer_folded
                for marker in (
                    "self_hosted",
                    "self-hosted",
                    "保留原产品自建",
                    "继续自建",
                )
            ):
                component_index = response_components.get(response_key)
                current = (
                    intent.services[component_index]
                    if component_index is not None and 0 <= component_index < len(intent.services)
                    else next(
                        (
                            service
                            for service in intent.services
                            if service.field_sources.get("_pending_architecture_decision")
                        ),
                        None,
                    )
                )
                if current is not None:
                    current.service = "ec2"
                    current.field_sources.pop("_pending_architecture_decision", None)
                    current.field_sources["_architecture_decision"] = "self_hosted"
                    current.requirements.setdefault("operating_system", "linux")
                    selected_model = self._model_from_confirmation_answer(answer)
                    if selected_model:
                        current.requirements["requested_model"] = selected_model
                        current.field_sources.pop("_customer_select_configuration", None)
                    else:
                        has_customer_shape = all(
                            isinstance(current.requirements.get(field), (int, float))
                            and not isinstance(current.requirements.get(field), bool)
                            for field in ("vcpu", "memory_gib")
                        )
                        if has_customer_shape:
                            current.field_sources.pop("_customer_select_configuration", None)
                        else:
                            current.field_sources["_customer_select_configuration"] = (
                                "customer_confirmation"
                            )
                    machine_count = re.search(r"机器(?:数量|台数)\s*[:：]?\s*(\d+)", answer, re.I)
                    if machine_count:
                        current.quantity = max(int(machine_count.group(1)), 1)
                    resolved_markers.append(("自建", "托管"))
                    continue

            if "nacos" in question_folded:
                component_index = next(
                    (
                        index
                        for index, service in enumerate(intent.services)
                        if "nacos" in (service.source_text or "").casefold()
                        or "nacos" in (service.calculator_service_name or "").casefold()
                    ),
                    None,
                )
                if component_index is not None:
                    current = intent.services[component_index]
                    if any(
                        marker in answer_folded
                        for marker in ("nacos_self_hosted", "继续", "自建", "保留 nacos")
                    ):
                        current.service = "ec2"
                        current.calculator_service_name = "Amazon EC2（自建 Nacos）"
                        current.field_sources.pop("_pending_architecture_decision", None)
                        current.field_sources["_architecture_decision"] = "self_hosted"
                        current.requirements.setdefault("operating_system", "linux")
                        count_match = re.search(r"(\d+)\s*个节点", question)
                        if count_match:
                            current.quantity = max(int(count_match.group(1)), 1)
                        if not any(
                            current.field_sources.get(f"requirements.{field}")
                            in {"customer_text", "customer_confirmation"}
                            for field in ("requested_model", "vcpu", "memory_gib")
                        ):
                            current.field_sources["_customer_select_configuration"] = (
                                "customer_confirmation"
                            )
                        resolved_markers.append(("nacos",))
                        continue
                    if any(
                        marker in answer_folded
                        for marker in (
                            "aws_managed_cloudmap_appconfig",
                            "cloud map",
                            "appconfig",
                            "aws 托管",
                        )
                    ):
                        shared = {
                            "region": current.region,
                            "hours_per_month": current.hours_per_month,
                            "source_text": current.source_text,
                        }
                        intent.services[component_index : component_index + 1] = [
                            ServiceRequirement(
                                service="cloud_map",
                                calculator_service_name="AWS Cloud Map",
                                quantity=1,
                                requirements={},
                                **shared,
                            ),
                            ServiceRequirement(
                                service="appconfig",
                                calculator_service_name="AWS AppConfig",
                                quantity=1,
                                requirements={},
                                **shared,
                            ),
                        ]
                        resolved_markers.append(("nacos",))
                        continue

            if any(marker in question_folded for marker in ("rds", "数据库")) and any(
                marker in question_folded
                for marker in ("部署方式", "单可用区", "主备", "multi-az", "multi_az")
            ):
                deployment = (
                    "multi_az"
                    if any(
                        marker in answer_folded
                        for marker in ("multi_az", "multi-az", "主备", "高可用")
                    )
                    else "single_az"
                    if any(
                        marker in answer_folded
                        for marker in ("single_az", "single-az", "单可用区", "单机")
                    )
                    else None
                )
                service = bound_service(ServiceKind.RDS)
                if deployment and service is not None:
                    service.requirements["deployment"] = deployment
                    service.requirements.pop("deployment_option", None)
                    service.requirements.pop("multi_az", None)
                    service.quantity = 1
                    resolved_markers.append(("部署方式",))
                    continue

            if "区域" in question_folded or "region" in question_folded:
                region = self._region_from_confirmation(answer)
                if region:
                    if component_index is not None and 0 <= component_index < len(intent.services):
                        intent.services[component_index].region = region
                    else:
                        # A service-availability answer carries a component id
                        # and changes that row only. Quote-wide region questions
                        # intentionally keep the existing all-component behavior.
                        for service in intent.services:
                            if self._service_kind(service.service) not in {
                                ServiceKind.CLOUDFRONT,
                                ServiceKind.ROUTE53,
                                ServiceKind.GLOBAL_ACCELERATOR,
                            }:
                                service.region = region
                    resolved_markers.append(("区域",))
                    continue

            if "具体数量" in question and (count_match := re.search(r"\d+", answer)):
                kind = (
                    ServiceKind.EC2
                    if any(marker in question_folded for marker in ("ec2", "服务器"))
                    else ServiceKind.EKS
                    if any(marker in question_folded for marker in ("eks", "k8s", "kubernetes"))
                    else None
                )
                if kind is not None:
                    service = bound_service(kind)
                    if service is not None:
                        service.quantity = max(int(count_match.group()), 1)
                        resolved_markers.append(("具体数量",))
                        continue

            if (
                "msk" in question_folded
                and "broker" in question_folded
                and (count_match := re.search(r"\d+", answer))
            ):
                service = bound_service(ServiceKind.MSK)
                if service is not None:
                    service.requirements["broker_count"] = max(int(count_match.group()), 1)
                    resolved_markers.append(("msk", "broker"))
                    continue

            if (
                "s3" in question_folded
                and "存储容量" in question
                and (
                    storage_match := re.search(
                        r"(\d+(?:\.\d+)?)\s*(tib|tb|t|gib|gb|g)?",
                        answer,
                        re.I,
                    )
                )
            ):
                value = float(storage_match.group(1))
                if (storage_match.group(2) or "gib").casefold() in {"tib", "tb", "t"}:
                    value *= 1024
                service = bound_service(ServiceKind.S3)
                if service is not None:
                    service.requirements["storage_gib"] = value
                    resolved_markers.append(("s3", "存储容量"))
                    continue

            # A missing Redis node size is collected as a plain business
            # answer (for example ``1G``).  Apply it to the saved structured
            # draft directly.  Sending the answer back through the intent
            # parser loses the association with this exact question and used
            # to produce the same confirmation page indefinitely.
            if any(
                marker in question_folded for marker in ("redis", "elasticache", "缓存")
            ) and any(
                marker in question for marker in ("单节点容量", "每节点", "每个节点", "节点内存")
            ):
                memory_match = re.search(
                    r"(\d+(?:\.\d+)?)\s*(?:gib|gb|g|吉)?\s*$",
                    answer,
                    re.IGNORECASE,
                )
                if memory_match:
                    service = bound_service(ServiceKind.REDIS)
                    if service is not None:
                        service.requirements["memory_gib"] = float(memory_match.group(1))
                        resolved_markers.append(("redis", "单节点"))
                    continue

            # Generic CPU/memory confirmation answers such as ``8c16G`` or
            # ``8核16G`` are applied before the cleanup model runs.  This keeps
            # the association with the exact component and prevents the model
            # from treating G as TB or asking the same question again.
            shape_match = re.search(
                r"(\d+(?:\.\d+)?)\s*(?:c|核|vcpu)\s*[,/， ]*"
                r"(\d+(?:\.\d+)?)\s*(?:gib|gb|g)",
                answer,
                re.IGNORECASE,
            )
            if shape_match and any(
                marker in question_folded for marker in ("核", "vcpu", "内存", "memory")
            ):
                kind = (
                    ServiceKind.RDS
                    if any(marker in question_folded for marker in ("rds", "数据库"))
                    else ServiceKind.EC2
                )
                service = bound_service(kind)
                if service is not None:
                    service.requirements["vcpu"] = float(shape_match.group(1))
                    service.requirements["memory_gib"] = float(shape_match.group(2))
                    service.field_sources.pop(
                        "_customer_shape_replaced_by_model", None
                    )
                    resolved_markers.append(("核", "内存"))
                continue

            if (
                "windows" in question_folded
                and "arm" in question_folded
                and any(marker in answer_folded for marker in ("x86", "保留 windows"))
            ):
                service = bound_service(ServiceKind.EC2)
                if service is not None:
                    plugin = self._plugins.get(ServiceKind.EC2)
                    resolver = getattr(plugin, "compatible_x86_model", None)
                    replacement = None
                    if resolver is not None:
                        replacement = await asyncio.to_thread(resolver, service, "ap-southeast-1")
                    if replacement:
                        service.requirements["requested_model"] = replacement
                        service.requirements["operating_system"] = "Windows"
                        resolved_markers.append(("windows", "arm"))
                continue

            if (
                "windows" in question_folded
                and "arm" in question_folded
                and "linux" in answer_folded
            ):
                service = bound_service(ServiceKind.EC2)
                if service is not None:
                    service.requirements["operating_system"] = "Linux"
                    resolved_markers.append(("windows", "arm"))
                continue

            selected_model = self._model_from_confirmation_answer(answer)
            if selected_model:
                kind = self._confirmation_service_kind(question, selected_model)
                component_index = response_components.get(response_key)
                service = (
                    intent.services[component_index]
                    if component_index is not None and 0 <= component_index < len(intent.services)
                    else (bound_service(kind) if kind else None)
                )
                staged_service = next(
                    (
                        component
                        for component in intent.services
                        if component.field_sources.get("_customer_select_configuration")
                    ),
                    None,
                )
                if component_index is None and staged_service is not None:
                    service = staged_service
                if service is not None:
                    service.requirements["requested_model"] = selected_model
                    service.field_sources["requirements.requested_model"] = (
                        "customer_confirmation"
                    )
                    service.field_evidence["requirements.requested_model"] = (
                        "客户从官方可用型号中选择"
                    )
                    service.locked_fields = sorted(
                        set(service.locked_fields) | {"requirements.requested_model"}
                    )
                    record_customer_fact_metadata(
                        service,
                        "requested_model",
                        "客户从官方可用型号中选择",
                        policy="exact",
                    )
                    service.field_sources.pop("_customer_select_configuration", None)
                    machine_count = re.search(r"机器(?:数量|台数)\s*[:：]?\s*(\d+)", answer, re.I)
                    if machine_count:
                        service.quantity = max(int(machine_count.group(1)), 1)
                        service.field_sources["quantity"] = "customer_confirmation"
                    service.requirements.pop("_review_selected_model", None)
                    service.requirements.pop("_review_selected_specifications", None)
                    # A replacement-model answer is the customer's decision
                    # about an unavailable/non-exact CPU or memory request.
                    # Keeping the old shape as a second hard constraint makes
                    # preflight reject the selected replacement and ask the
                    # same question again.  From this point the official model
                    # is the source of its CPU/memory specification.
                    if any(
                        marker in question_folded
                        for marker in (
                            "没有",
                            "最接近",
                            "相邻规格",
                            "不是完全匹配",
                            "不是同一套",
                            "请从下方",
                            "请选择",
                        )
                    ):
                        service.requirements.pop("vcpu", None)
                        service.requirements.pop("memory_gib", None)
                        # This is an explicit customer override of the old
                        # CPU/memory sentence. Keep the source text for audit,
                        # but do not let literal recovery restore the rejected
                        # shape on the next preview/quote pass.
                        service.field_sources[
                            "_customer_shape_replaced_by_model"
                        ] = "customer_confirmation"
                    if kind == ServiceKind.REDIS:
                        resolved_markers.append(("redis", "相邻"))
                    elif kind == ServiceKind.RDS:
                        resolved_markers.append(("数据库", "规格"))
                    elif kind == ServiceKind.EC2:
                        resolved_markers.append(("服务器", "规格"))
                continue

            affirmative = (
                bool(
                    re.fullmatch(
                        r"\s*(?:同意|可以|确认|是|接受|按建议)(?:跨可用区|改为\s*multi-az)?\s*[。.!！]?\s*",
                        answer,
                        re.IGNORECASE,
                    )
                )
                or "multi-az" in answer_folded
                or "跨可用区" in answer
            )

            if affirmative and any(
                marker in question_folded for marker in ("single-az", "single az")
            ):
                service = bound_service(ServiceKind.RDS)
                if service is not None:
                    service.requirements["deployment_option"] = "multi_az"
                    service.requirements["multi_az"] = True
                    resolved_markers.append(("single-az",))
                continue

            if affirmative and "redis" in question_folded and "可用区" in question:
                service = bound_service(ServiceKind.REDIS)
                if service is not None:
                    service.requirements["multi_az"] = True
                    service.requirements["cross_az"] = True
                    service.requirements.pop("same_availability_zone", None)
                    resolved_markers.append(("redis", "可用区"))
                continue

            if affirmative and "服务器" in question and "可用区" in question:
                service = bound_service(ServiceKind.EC2)
                if service is not None:
                    service.requirements["availability_zone_count"] = 2
                    service.requirements.pop("same_availability_zone", None)
                    resolved_markers.append(("ec2", "可用区"))

        if excluded_component_ids:
            intent.services = [
                service
                for index, service in enumerate(intent.services)
                if index not in excluded_component_ids
            ]
        if resolved_markers:
            intent.ambiguities = [
                notice
                for notice in intent.ambiguities
                if not any(
                    all(marker in notice.casefold() for marker in markers)
                    for markers in resolved_markers
                )
            ]
        for service in intent.services:
            previous = before.get(
                id(service),
                {"region": None, "quantity": None, "requirements": {}},
            )
            sources = dict(service.field_sources)
            locked = set(service.locked_fields)
            if service.region != previous["region"]:
                sources["region"] = "customer_confirmation"
                locked.add("region")
            if service.quantity != previous["quantity"]:
                sources["quantity"] = "customer_confirmation"
                locked.add("quantity")
            previous_requirements = previous["requirements"]
            assert isinstance(previous_requirements, dict)
            for field, value in service.requirements.items():
                if previous_requirements.get(field) != value:
                    path = f"requirements.{field}"
                    sources[path] = "customer_confirmation"
                    locked.add(path)
            removed = set(previous_requirements) - set(service.requirements)
            for field in removed:
                path = f"requirements.{field}"
                sources.pop(path, None)
                locked.discard(path)
            service.field_sources = sources
            service.locked_fields = sorted(locked)

    @classmethod
    def _is_structured_workflow_answer(cls, answer: str) -> bool:
        """Return whether an answer must bypass free-form AI revision."""

        folded = answer.strip().casefold()
        return bool(
            folded.startswith("engine_version:")
            or folded.startswith("cache_engine_version:")
            or folded.startswith("traffic_geography:")
            or folded.startswith("billing_variant:")
            or folded.startswith("replace_service:")
            or folded == "exclude_component"
            or cls._region_from_confirmation(answer) is not None
            or cls._model_from_confirmation_answer(answer)
            or folded
            in {
                "mysql",
                "postgresql",
                "postgres",
                "mariadb",
                "sql_server_standard",
                "sql server standard",
                "oracle",
                "db2",
            }
            or any(
                marker in folded
                for marker in (
                    "self_hosted",
                    "self-hosted",
                    "nacos_self_hosted",
                    "aws_managed_cloudmap_appconfig",
                    "保留原产品自建",
                    "继续自建",
                )
            )
        )

    @staticmethod
    def _region_from_confirmation(answer: str) -> str | None:
        folded = answer.strip().casefold()
        markers = {
            "新加坡": "ap-southeast-1",
            "singapore": "ap-southeast-1",
            "悉尼": "ap-southeast-2",
            "sydney": "ap-southeast-2",
            "雅加达": "ap-southeast-3",
            "jakarta": "ap-southeast-3",
            "东京": "ap-northeast-1",
            "tokyo": "ap-northeast-1",
            "首尔": "ap-northeast-2",
            "seoul": "ap-northeast-2",
            "大阪": "ap-northeast-3",
            "osaka": "ap-northeast-3",
            "香港": "ap-east-1",
            "hong kong": "ap-east-1",
            "孟买": "ap-south-1",
            "mumbai": "ap-south-1",
            "法兰克福": "eu-central-1",
            "frankfurt": "eu-central-1",
            "伦敦": "eu-west-2",
            "london": "eu-west-2",
            "巴黎": "eu-west-3",
            "paris": "eu-west-3",
            "弗吉尼亚北部": "us-east-1",
            "n. virginia": "us-east-1",
            "俄勒冈": "us-west-2",
            "oregon": "us-west-2",
        }
        region = next((code for marker, code in markers.items() if marker in folded), None)
        if region:
            return region
        match = re.search(
            r"\b(?:af|ap|ca|eu|il|me|mx|sa|us)(?:-gov)?-[a-z0-9-]+-\d\b",
            folded,
        )
        return match.group(0) if match else None

    @staticmethod
    def _model_from_confirmation_answer(answer: str) -> str | None:
        # Customer-selected official models are not limited to EC2/RDS/Redis.
        # Keep this generic so new AWS products can participate in the same
        # confirmation flow without adding another hard-coded prefix.
        match = re.search(
            r"(?<![A-Za-z0-9_-])"
            r"([A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+){1,3})"
            r"(?![A-Za-z0-9_.-])",
            answer,
            re.IGNORECASE,
        )
        return match.group(1).casefold() if match else None

    @staticmethod
    def _confirmation_service_kind(question: str, model: str) -> ServiceKind | None:
        folded = f"{question} {model}".casefold()
        if model.casefold().startswith("cache.") or any(
            marker in folded for marker in ("redis", "elasticache", "缓存")
        ):
            return ServiceKind.REDIS
        if model.casefold().startswith("db.") or any(
            marker in folded for marker in ("rds", "数据库")
        ):
            return ServiceKind.RDS
        if model.casefold().startswith("kafka.") or any(
            marker in folded for marker in ("msk", "kafka", "broker", "消息代理")
        ):
            return ServiceKind.MSK
        if model.casefold().endswith(".search") or any(
            marker in folded for marker in ("opensearch", "elasticsearch", "es 节点", "搜索")
        ):
            return ServiceKind.OPENSEARCH
        if any(marker in folded for marker in ("ec2", "服务器", "windows", "linux")):
            return ServiceKind.EC2
        return None

    @staticmethod
    def _first_service(intent: ParsedIntent, kind: ServiceKind):
        return next(
            (
                service
                for service in intent.services
                if QuoteService._service_kind(service.service) == kind
            ),
            None,
        )

    @staticmethod
    def _service_for_confirmation(intent: ParsedIntent, kind: ServiceKind, question: str):
        candidates = [
            service
            for service in intent.services
            if QuoteService._service_kind(service.service) == kind
        ]
        question_folded = question.casefold()
        model_match = next(
            (
                service
                for service in candidates
                if str(service.requirements.get("requested_model") or "").casefold()
                in question_folded
                and service.requirements.get("requested_model")
            ),
            None,
        )
        if model_match is not None:
            return model_match

        # A quote may contain several resources of the same service (for
        # example dev 4C16G and database-like 4C100G entries).  Confirmation
        # responses are keyed by their human-readable question, so bind the
        # answer to the resource whose original requested shape appears first
        # in that question.  Falling back to candidates[0] used to write a
        # choice for the second card onto the first card and caused an endless
        # confirmation loop.
        shape_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:核|vcpus?)\s*[、,，/\s]*"
            r"(\d+(?:\.\d+)?)\s*(?:gib|gb|g)(?![a-z])",
            question,
            re.IGNORECASE,
        )
        if shape_match:
            requested_vcpu = float(shape_match.group(1))
            requested_memory = float(shape_match.group(2))
            exact_matches = [
                service
                for service in candidates
                if QuoteService._numeric_requirement(service, "vcpu") == requested_vcpu
                and QuoteService._numeric_requirement(service, "memory_gib") == requested_memory
            ]
            if len(exact_matches) == 1:
                return exact_matches[0]

        return candidates[0] if candidates else None

    @staticmethod
    def _numeric_requirement(service: ServiceRequirement, key: str) -> float | None:
        value = service.requirements.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        try:
            return float(str(value)) if value is not None else None
        except ValueError:
            return None

    @staticmethod
    def _questions_from_confirmation_text(text: str) -> list[str]:
        return [
            match.group(1).strip()
            for match in re.finditer(r"^\s*\d+\.\s*(.+?)\s*$", text, re.MULTILINE)
        ]

    @staticmethod
    def _has_plain_affirmative_confirmation(text: str) -> bool:
        replies = re.findall(r"【客户确认回复】\s*([\s\S]*?)(?=【客户确认回复】|$)", text)
        affirmative = re.compile(
            r"\s*(?:同意|可以|确认|是|接受|按建议)\s*[。.!！]?\s*",
            re.IGNORECASE,
        )
        return any(affirmative.fullmatch(reply.strip()) for reply in replies)

    @staticmethod
    def _confirmation_notices(intent: ParsedIntent) -> list[str]:
        # AI may describe every omitted optional field as an ambiguity. Those are
        # system defaults, not customer decisions. Only explicit contradictions
        # or unsupported architecture choices belong on the confirmation page;
        # plugins separately ask for genuinely essential sizing information.
        global_services = {
            ServiceKind.CLOUDFRONT,
            ServiceKind.ROUTE53,
            ServiceKind.GLOBAL_ACCELERATOR,
        }
        regional = [
            item
            for item in intent.services
            if QuoteService._service_kind(item.service) not in global_services
        ]
        all_regional_services_resolved = bool(regional) and all(item.region for item in regional)
        notices = [
            item.strip()
            for item in intent.ambiguities
            if item.strip()
            and QuoteService._is_customer_decision_notice(item)
            and not (
                all_regional_services_resolved and QuoteService._is_region_confirmation_notice(item)
            )
            and not QuoteService._is_optional_opensearch_role_notice(item)
        ]
        return QuoteService._deduplicate_confirmation_notices(notices)

    @staticmethod
    def _is_region_confirmation_notice(notice: str) -> bool:
        text = notice.casefold()
        if "区域" not in text or any(marker in text for marker in ("不可用", "不支持")):
            return False
        # A configuration question often repeats source text such as
        # ``区域：新加坡`` and later says ``请确认推荐配置``.  Treating those two
        # unrelated fragments as a region question collapsed independent RDS,
        # Redis and OpenSearch choices into one bogus global-region prompt.
        return bool(
            re.search(
                r"(?:请确认|缺少|未指定)[^。；？?]{0,48}(?:部署[^。；？?]{0,16})?区域"
                r"|部署在(?:哪|哪个|哪一个)[^。；？?]{0,20}区域"
                r"|区域[^。；？?]{0,16}(?:缺少|未指定)",
                text,
            )
        )

    @classmethod
    def _drop_resolved_region_questions(cls, intent: ParsedIntent, notices: list[str]) -> list[str]:
        global_services = {
            ServiceKind.CLOUDFRONT,
            ServiceKind.ROUTE53,
            ServiceKind.GLOBAL_ACCELERATOR,
        }
        regional = [
            item
            for item in intent.services
            if cls._service_kind(item.service) not in global_services
        ]
        if not regional or not all(item.region for item in regional):
            return notices
        return [notice for notice in notices if not cls._is_region_confirmation_notice(notice)]

    @classmethod
    def _drop_pre_region_catalog_questions(
        cls,
        intent: ParsedIntent,
        notices: list[str],
        component_scopes: dict[str, tuple[str, str]],
        confirmation_options: dict[str, list[ConfirmationOption]] | None = None,
    ) -> list[str]:
        """Make region selection the only first-round customer decision.

        Preview adapters are allowed to use a private fallback region to warm
        their catalogs.  That fallback can populate ``component.region`` even
        though the customer-facing region question is still unresolved.  The
        presence of that question—not the temporary component value—is the
        source of truth here. All other decisions are deferred and regenerated
        after the selected region has been applied, so customers never choose
        a version, model or architecture against the wrong regional catalog.
        """

        region_notices = [
            notice for notice in notices if cls._is_region_confirmation_notice(notice)
        ]
        if not region_notices:
            return notices
        # Region notices are already consolidated by business meaning, but
        # retain a single item defensively in case an upstream parser emits
        # multiple phrasings in the same pass.
        return region_notices[:1]

    @classmethod
    def _ensure_selection_confirmation_notices(
        cls,
        intent: ParsedIntent,
        selections: list[PreviewSelection],
        notices: list[str],
        confirmation_components: dict[str, tuple[str, str]],
        confirmation_options: dict[str, list[ConfirmationOption]],
    ) -> list[str]:
        """Give every red component card one customer-answerable question."""

        result = list(notices)
        mapped_components = {
            confirmation_components[notice][0]
            for notice in result
            if notice in confirmation_components
        }
        for selection in selections:
            component_id = selection.component_id
            if not selection.requires_confirmation or component_id in mapped_components:
                continue
            question = str(
                selection.confirmation_reason
                or selection.issue_message
                or "请选择该组件当前区域支持的官方配置。"
            ).strip()
            if not question:
                continue
            existing_scope = confirmation_components.get(question)
            if existing_scope and existing_scope[0] != component_id:
                component_number = selection.component_number or str(int(component_id) + 1)
                question = f"【组件 {component_number} · {selection.display_name}】{question}"
            confirmation_components[question] = (component_id, selection.service)
            try:
                requirement = intent.services[int(component_id)]
            except (ValueError, IndexError):
                requirement = object()
            confirmation_options[question] = cls._compact_candidate_options(
                selection.candidates, requirement
            )
            result.append(question)
            mapped_components.add(component_id)
        return cls._deduplicate_confirmation_notices(result, confirmation_components)

    @staticmethod
    def _customer_service_name(selection: PreviewSelection, requirement: ServiceRequirement) -> str:
        identity = " ".join(
            filter(
                None,
                (
                    selection.display_name,
                    selection.service,
                    requirement.product_identity,
                    requirement.source_text,
                ),
            )
        ).casefold()
        if "eks worker" in identity or "worker nodes" in identity:
            return "EKS 工作节点"
        if "elasticache" in identity or "redis" in identity:
            return "Redis"
        if "postgres" in identity:
            return "RDS PostgreSQL"
        if "mysql" in identity:
            return "RDS MySQL"
        if "opensearch" in identity:
            return "OpenSearch"
        return selection.display_name.removeprefix("Amazon ").strip() or "该组件"

    @staticmethod
    def _plain_spec_value(value: object) -> str | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return f"{value:g}"
        try:
            return f"{float(str(value)):g}"
        except (TypeError, ValueError):
            return None

    @classmethod
    def _plain_model_selection_question(
        cls,
        selection: PreviewSelection,
        requirement: ServiceRequirement,
        intent: ParsedIntent | None = None,
    ) -> str:
        service_name = cls._customer_service_name(selection, requirement)
        vcpu = cls._plain_spec_value(requirement.requirements.get("vcpu"))
        memory = cls._plain_spec_value(requirement.requirements.get("memory_gib"))
        requested_model = str(
            requirement.requirements.get("requested_model")
            or selection.requested_model
            or cls._model_from_confirmation_answer(requirement.source_text or "")
            or ""
        ).strip()
        if requested_model:
            if vcpu or memory:
                supplied_shape = "、".join(
                    value
                    for value in (
                        f"{vcpu} 核" if vcpu else "",
                        f"{memory} GB 内存" if memory else "",
                    )
                    if value
                )
                return (
                    f"您同时填写了 {service_name} 型号 {requested_model} 和 {supplied_shape}，"
                    "但这两个配置对不上。请在下面确认要用哪一个。"
                )
            return (
                f"您填写的 {service_name} 型号 {requested_model} 在这个地区不能使用。"
                "请从下面选择一个可用型号。"
            )
        if service_name == "EKS 工作节点" and not vcpu and not memory:
            return "EKS 工作节点还没写需要几核、多少内存。请在下面选择。"
        subject = (
            f"{service_name} 每个节点"
            if service_name == "OpenSearch"
            else service_name
        )
        if vcpu and memory:
            return (
                f"您填写的 {subject} 是 {vcpu} 核、{memory} GB，但没有完全一样的型号。"
                "请从下面选择一个合适的配置。"
            )
        if memory:
            return (
                f"您填写的 {service_name} 是 {memory} GB 内存，但没有完全一样的型号。"
                "请从下面选择一个合适的配置。"
            )
        if vcpu:
            return (
                f"您填写的 {service_name} 是 {vcpu} 核，但没有完全一样的型号。"
                "请从下面选择一个合适的配置。"
            )
        source = (requirement.source_text or "").strip()
        relation_only = bool(
            re.match(r"^(?:用于|基于|依赖|关联|连接|挂载|保护|提供给|承载)", source)
        )
        parent_requirement: ServiceRequirement | None = None
        if relation_only and intent is not None:
            parent_requirement = next(
                (
                    item
                    for item in intent.services
                    if item is not requirement
                    and source
                    and source in (item.source_text or "")
                    and (
                        len((item.source_text or "").strip()) > len(source)
                        or cls._service_kind(item.service) != ServiceKind.EC2
                    )
                ),
                None,
            )
            if parent_requirement is None:
                try:
                    requirement_index = next(
                        index for index, item in enumerate(intent.services) if item is requirement
                    )
                except StopIteration:
                    requirement_index = -1
                if requirement_index > 0:
                    previous = intent.services[requirement_index - 1]
                    if previous.service.casefold() in {"vpc", "eks"}:
                        parent_requirement = previous
        if relation_only:
            parent_source = (
                (parent_requirement.source_text or "").strip()
                if parent_requirement is not None
                else ""
            )
            source_heading = (
                re.split(r"[：:]", parent_source, maxsplit=1)[0].strip() if parent_source else ""
            )
            parent_name = (
                source_heading
                if source_heading and source_heading != parent_source
                else str(
                    (parent_requirement.calculator_service_name if parent_requirement else "")
                    or (parent_requirement.service if parent_requirement else "")
                    or "客户的关联需求"
                )
            )
            return (
                f"客户在“{parent_name}”中提到了“{source}”，但没写这台 {service_name} "
                "需要几核、多少内存。请在下面选择。"
            )
        if source:
            source_name = re.split(r"[：:]", source, maxsplit=1)[0].strip()
            source_label = source_name if source_name and len(source_name) <= 48 else service_name
            return (
                f"客户提到了“{source_label}”，但没写这台 {service_name} 需要几核、"
                "多少内存。请在下面选择。"
            )
        return f"{service_name} 还没写具体配置，请在下面选择。"

    @staticmethod
    def _plain_customer_words(text: str) -> str:
        """Translate internal catalogue language at the customer boundary.

        Product and model names remain untouched, but implementation terms are
        replaced with the short words a buyer naturally uses.  This is a final
        safety net for questions produced by current and future plugins or AI
        prompts, so one new adapter cannot leak engineering language to the
        confirmation page.
        """

        clean = re.sub(r"\s+", " ", str(text or "")).strip()
        replacements = (
            ("AWS 官方可售配置", "AWS 可用配置"),
            ("AWS 官方规格", "AWS 实际配置"),
            ("Microsoft 官方 SKU", "可用型号"),
            ("官方 SKU", "可用型号"),
            ("当前区域可售配置", "下面的可用配置"),
            ("当前区域可用配置", "下面的可用配置"),
            ("当前区域支持的配置", "下面的可用配置"),
            ("官方型号", "可用型号"),
            ("计费维度", "价格信息"),
            ("核价", "计算价格"),
            ("部署区域", "地区"),
            ("vCPU", "核"),
            ("GiB", "GB"),
            ("下方", "下面"),
        )
        for technical, plain in replacements:
            clean = clean.replace(technical, plain)
        clean = clean.replace("客户填写的", "您填写的")
        return clean

    @classmethod
    def _simplify_component_confirmation_notices(
        cls,
        intent: ParsedIntent,
        selections: list[PreviewSelection],
        notices: list[str],
        confirmation_components: dict[str, tuple[str, str]],
        confirmation_options: dict[str, list[ConfirmationOption]],
    ) -> list[str]:
        """Apply one customer-language policy to every component question.

        Plugins and parsers may keep detailed technical reasons for logging, but
        the confirmation page has one presentation boundary. Model questions
        explain the requested shape and next action; every other component
        question loses internal prefixes and repeated source paragraphs here.
        This keeps the behavior consistent for existing and future plugins.
        """

        selections_by_id = {item.component_id: item for item in selections}
        result: list[str] = []
        for notice in notices:
            component = confirmation_components.get(notice)
            options = confirmation_options.get(notice, [])
            if not component:
                result.append(notice)
                continue
            selection = selections_by_id.get(component[0])
            try:
                requirement = intent.services[int(component[0])]
            except (ValueError, IndexError):
                requirement = None
            if selection is None or requirement is None:
                result.append(notice)
                continue
            plain = (
                cls._plain_model_selection_question(selection, requirement, intent)
                if any(option.model for option in options)
                else cls._customer_confirmation_question(
                    selection.display_name, requirement, notice
                )
            )
            plain_folded = plain.casefold()
            if (
                "mysql" in plain_folded
                and "版本" in plain
                and "extended support" not in plain_folded
            ):
                plain = (
                    f"{plain.rstrip('。？?')}。MySQL 5.7 和 8.0 属于旧版本，继续使用会额外收费。"
                    "建议改用当前可用的最新版本；如果业务必须使用旧版本，也可以继续选择。"
                )
            confirmation_components.pop(notice, None)
            confirmation_options.pop(notice, None)
            confirmation_components[plain] = component
            confirmation_options[plain] = options
            result.append(plain)
        return cls._deduplicate_confirmation_notices(result, confirmation_components)

    @classmethod
    def _apply_customer_question_language_policy(
        cls,
        notices: list[str],
        confirmation_components: dict[str, tuple[str, str]],
        confirmation_options: dict[str, list[ConfirmationOption]],
    ) -> list[str]:
        """Apply the same short, conversational wording to every question."""

        result: list[str] = []
        for notice in notices:
            plain = cls._compact_customer_question(notice)
            if plain != notice:
                component = confirmation_components.pop(notice, None)
                options = confirmation_options.pop(notice, None)
                if component is not None:
                    confirmation_components.setdefault(plain, component)
                if options is not None:
                    confirmation_options.setdefault(plain, options)
            if plain not in result:
                result.append(plain)
        return result

    @staticmethod
    def _is_optional_opensearch_role_notice(notice: str) -> bool:
        text = notice.casefold()
        return (
            "opensearch" in text
            and any(
                marker in text for marker in ("master", "data", "coordinating", "角色", "独立节点")
            )
            and any(marker in text for marker in ("节点", "架构", "请确认", "未明确"))
        )

    @staticmethod
    def _deduplicate_confirmation_notices(
        notices: list[str],
        component_scopes: dict[str, tuple[str, str]] | None = None,
    ) -> list[str]:
        """Merge customer questions by business meaning, not exact wording.

        The intake model may report the same missing region once per regional
        service and phrase each copy differently.  Exact-string deduplication
        therefore produced four region inputs for one workload.  Consolidate
        only globally shared decisions here; service-specific sizing and
        architecture questions remain separate.
        """

        result: list[str] = []
        seen: set[str] = set()
        for raw in notices:
            notice = raw.strip()
            if not notice:
                continue
            folded = notice.casefold()
            is_region_question = QuoteService._is_region_confirmation_notice(notice)
            is_component_region_question = is_region_question and any(
                marker in folded for marker in ("该组件", "不能使用“全球”", '不能使用"全球"')
            )
            if is_region_question and not is_component_region_question:
                key = "shared:deployment_region"
                notice = "请确认这些区域型服务部署在哪个 AWS 区域；如各服务区域不同，请分别说明。"
            else:
                normalized = re.sub(r"[\s，,。；;：:？?!！…]+", "", folded)
                current_scope = component_scopes.get(raw) if component_scopes is not None else None
                scope_key = current_scope[0] if current_scope else "global"
                business_key = QuoteService._confirmation_question_key(notice)
                # Separate validation layers can describe the same missing
                # value with unrelated sentences.  Collapse those by business
                # meaning, but retain the component scope so two independent
                # databases never share or suppress each other's question.
                if "|" in business_key:
                    key = f"business:{scope_key}:{business_key}"
                    if key in seen:
                        continue
                    seen.add(key)
                    result.append(notice)
                    continue
                prefix_duplicate = next(
                    (
                        index
                        for index, existing in enumerate(result)
                        if not (
                            current_scope
                            and component_scopes is not None
                            and component_scopes.get(existing)
                            and component_scopes.get(existing) != current_scope
                        )
                        and min(
                            len(normalized),
                            len(re.sub(r"[\s，,。；;：:？?!！…]+", "", existing.casefold())),
                        )
                        >= 24
                        and (
                            normalized.startswith(
                                re.sub(r"[\s，,。；;：:？?!！…]+", "", existing.casefold())
                            )
                            or re.sub(
                                r"[\s，,。；;：:？?!！…]+", "", existing.casefold()
                            ).startswith(normalized)
                        )
                    ),
                    None,
                )
                if prefix_duplicate is not None:
                    existing = result[prefix_duplicate]
                    if len(notice) > len(existing):
                        result[prefix_duplicate] = notice
                    continue
                key = f"semantic:{scope_key}:{normalized[:48]}"
            if key in seen:
                continue
            seen.add(key)
            result.append(notice)
        return result

    @staticmethod
    def _is_customer_decision_notice(notice: str) -> bool:
        """Keep real customer decisions and hide defaults/technical failures.

        Intake is expected to report every contradiction or missing essential
        business value in one pass.  Previously only a small hard-coded list of
        architecture conflicts survived, which silently discarded questions
        such as a missing deployment region or an ambiguous API Gateway amount.
        """

        text = notice.casefold()
        technical_markers = (
            "api 错误",
            "api 失败",
            "接口错误",
            "接口失败",
            "未返回",
            "没有返回",
            "超时",
            "timeout",
            "凭证",
            "adapter",
            "适配器",
            "程序报错",
            "后端报错",
            "目录无法",
            "官方目录",
            "计费项",
        )
        if any(marker in text for marker in technical_markers):
            return False

        optional_default_markers = (
            "未指定引擎版本",
            "未指定 iops",
            "未指定对象数量",
            "未指定是否开启",
            "未指定操作系统",
            "节点未指定操作系统",
            "监控，默认",
            "快照保留策略",
            "license model",
            "请求数按0",
            "lcu 业务量",
        )
        if any(marker in text for marker in optional_default_markers):
            return False

        # These are product defaults, not business decisions.  When omitted we
        # choose the lowest-priced standard billing path and disclose it in the
        # quote instead of asking the customer technical implementation details.
        optional_product_questions = (
            (("redis", "elasticache"), ("版本", "引擎版本", "集群模式")),
            (("msk",), ("集群类型", "standard", "serverless", "存储类型", "tiered storage")),
            (("api gateway", "api 网关"), ("类型", "rest api", "http api", "websocket")),
        )
        if any(
            any(service_marker in text for service_marker in service_markers)
            and any(field_marker in text for field_marker in field_markers)
            for service_markers, field_markers in optional_product_questions
        ):
            return False

        return bool(
            QuoteService._is_blocking_design_notice(notice)
            or any(
                marker in text
                for marker in (
                    "请确认部署区域",
                    "缺少部署区域",
                    "未指定区域",
                    "是指",
                    "还是",
                    "二选一",
                    "请选择",
                    "请确认",
                    "缺少",
                )
            )
        )

    async def _enriched_confirmation_notices(self, intent: ParsedIntent) -> list[str]:
        return self._confirmation_notices(intent)

    @staticmethod
    def _cache_requested_memory(service: object, notices: list[str]) -> float | None:
        requirements = getattr(service, "requirements", {})
        value = requirements.get("memory_gib") if isinstance(requirements, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        source = str(getattr(service, "source_text", "") or "")
        related_notices = " ".join(
            item
            for item in notices
            if any(marker in item.lower() for marker in ("缓存", "redis", "elasticache"))
        )
        text = f"{source} {related_notices}"
        patterns = (
            r"(?:每个?节点|节点|内存)[^\d]{0,18}(\d+(?:\.\d+)?)\s*(?:gib|gb|g)(?![a-z])",
            r"(\d+(?:\.\d+)?)\s*(?:gib|gb|g)(?![a-z])[^。；,，]{0,18}(?:内存|节点)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                return float(match.group(1))
        return None

    @staticmethod
    def _confirmation_text(notices: list[str]) -> str | None:
        if not notices:
            return None
        lines = [
            "您好，麻烦确认下面几项：",
            *(
                f"{index}. {QuoteService._compact_customer_question(notice)}"
                for index, notice in enumerate(notices, start=1)
            ),
            "直接回复选项即可，谢谢。",
        ]
        return "\n".join(lines)

    @staticmethod
    def _customer_confirmed_snapshot(
        intent: ParsedIntent,
    ) -> ComponentLedger:
        """Capture customer facts by identity, never by mutable row position."""

        return capture_customer_ledger(intent)

    @staticmethod
    def _restore_customer_confirmed_snapshot(
        intent: ParsedIntent,
        snapshots: ComponentLedger,
    ) -> None:
        """Prevent any later defaulting rule from erasing a customer edit."""

        restore_customer_ledger(intent, snapshots)

    @staticmethod
    def _apply_calculator_minimum_defaults(intent: ParsedIntent) -> list[str]:
        notices: list[str] = []
        lcu_fields = {
            "processed_bytes_gib",
            "processed_bytes_ec2_ip_gib_per_hour",
            "new_connections_per_second",
            "average_connection_duration_seconds",
            "active_connections_per_minute",
            "requests_per_second",
            "rule_evaluations_per_request",
            "rule_evaluations_per_second",
            "lcu_count",
        }
        for service in intent.services:
            service_name = (f"{service.service} {service.calculator_service_name or ''}").lower()
            requirements = service.requirements
            # One shared default layer covers every known component template.
            # Defaults are applied only when absent, so customer text and later
            # customer edits always remain authoritative.
            for field, value in safe_requirement_defaults(service.service).items():
                requirements.setdefault(field, value)
                path = f"requirements.{field}"
                if path not in service.field_sources:
                    service.field_sources[path] = "system_minimum"
            if service.service.lower() in {"elasticache", "redis"}:
                # Multiple shards necessarily use cluster mode.  Engine version
                # remains omitted so AWS's supported default version is used.
                shards = requirements.get("shards")
                if isinstance(shards, (int, float)) and not isinstance(shards, bool) and shards > 1:
                    requirements.setdefault("cluster_mode", True)
                requirements.setdefault("backup_retention_days", 0)
                requirements.setdefault("detailed_monitoring", False)
                requirements.setdefault("data_transfer_monitoring", False)
                continue

            if service.service.lower() in {"msk", "amazon_msk"}:
                # Missing broker count uses the smallest supported base layout
                # instead of becoming a question or leaking prose into a
                # numeric field.
                requirements.setdefault("broker_count", 2)
                requirements.setdefault("cluster_type", "provisioned")
                requirements.setdefault("storage_type", "ebs")
                continue

            if service.service.lower() in {"apigateway", "api_gateway"}:
                requirements.setdefault("api_type", "http")
                continue

            if service.service.lower() in {"quicksight", "amazon_quicksight"}:
                # QuickSight cannot be safely priced without a subscription
                # count.  When the customer only asks for the service, use the
                # smallest useful official subscription instead of mislabeling
                # the missing quantity as an AWS timeout.
                requirements.setdefault("edition", "enterprise")
                requirements.setdefault("users", max(1, int(service.quantity or 1)))
                requirements.setdefault(
                    "system_default_assumption",
                    "客户未说明 QuickSight 用户数；按 Enterprise 版 1 位用户估算",
                )
                notices.append(str(requirements["system_default_assumption"]))
                continue

            is_load_balancer = any(
                marker in service_name
                for marker in (
                    "elastic load balancing",
                    "application load balancer",
                    "alb",
                    "elb",
                )
            )
            if is_load_balancer:
                if not any(requirements.get(field) is not None for field in lcu_fields):
                    requirements["reference_lcu_unit_only"] = True
                    requirements["system_default_assumption"] = (
                        "客户未提供 ALB LCU 业务量；仅展示 LCU 官方单位价，不计入月费合计"
                    )
                if requirements.get("system_default_assumption"):
                    notices.append(str(requirements["system_default_assumption"]))
                continue

            if "cloudfront" in service_name:
                # Request count is optional. Models sometimes emit placeholders
                # even when the customer never mentioned requests; that parser
                # defect must never become a customer-facing question.
                request_keys = ("https_requests", "https_requests_per_month", "request_count")
                source = (service.source_text or "").casefold()
                customer_mentioned_requests = bool(
                    re.search(r"(?:https\s*)?(?:请求|requests?)", source, re.IGNORECASE)
                )
                if not customer_mentioned_requests:
                    for key in request_keys:
                        requirements.pop(key, None)
                customer_supplied_transfer = bool(
                    re.search(
                        r"\d+(?:\.\d+)?\s*(?:gib|gb|g|tib|tb|t)(?:\s*/?月)?",
                        service.source_text or "",
                        re.IGNORECASE,
                    )
                )
                # The component parser may already have recovered the usage
                # from a neighbouring line in the same numbered block.  Do
                # not erase that structured customer value merely because the
                # shortened display excerpt no longer contains the unit text.
                structured_transfer = any(
                    (QuoteService._numeric_requirement(service, key) or 0) > 0
                    for key in (
                        "data_transfer_out_gib",
                        "data_transfer_gib",
                        "transfer_gib",
                    )
                )
                if not customer_supplied_transfer and not structured_transfer:
                    for key in ("data_transfer_out_gib", "data_transfer_gib", "transfer_gib"):
                        requirements.pop(key, None)
                    requirements["reference_unit_only"] = True
                    requirements["system_default_assumption"] = (
                        "客户未提供 CloudFront 下行量；仅展示 1 GiB 对应的官方单位价，不计入月费合计"
                    )
                else:
                    requirements.pop("reference_unit_only", None)
                    default_note = requirements.get("system_default_assumption")
                    if isinstance(default_note, str) and "CloudFront 下行量" in default_note:
                        requirements.pop("system_default_assumption", None)
                if requirements.get("system_default_assumption"):
                    notices.append(str(requirements["system_default_assumption"]))
                continue

            if service.service.lower() in {"s3", "amazon_s3"}:
                customer_supplied_storage = bool(
                    re.search(
                        r"\d+(?:\.\d+)?\s*(?:个|块|条|份)?\s*(?:gib|gb|g|tib|tb|t)",
                        service.source_text or "",
                        re.IGNORECASE,
                    )
                )
                structured_storage = (
                    QuoteService._numeric_requirement(service, "storage_gib") or 0
                ) > 0
                if not customer_supplied_storage and not structured_storage:
                    requirements.pop("storage_gib", None)
                    requirements["reference_unit_only"] = True
                    requirements["system_default_assumption"] = (
                        "客户未提供 S3 容量；仅展示 1 GiB 对应的官方单位价，不计入月费合计"
                    )
                else:
                    requirements.pop("reference_unit_only", None)
                    default_note = requirements.get("system_default_assumption")
                    if isinstance(default_note, str) and "S3 容量" in default_note:
                        requirements.pop("system_default_assumption", None)
                if requirements.get("system_default_assumption"):
                    notices.append(str(requirements["system_default_assumption"]))
                continue

            if service.service.lower() in {"ec2", "fargate"}:
                requirements.setdefault("operating_system", "Linux")
            if service.service.lower() == "ec2":
                requirements.setdefault("detailed_monitoring", False)
            if any(marker in service_name for marker in ("elasticache", "redis", "valkey")):
                requirements.setdefault("backup_retention_days", 0)
                requirements.setdefault("detailed_monitoring", False)
                requirements.setdefault("data_transfer_monitoring", False)
            if service.service.lower() in {"rds", "aurora"}:
                requirements.setdefault("performance_insights", False)
                requirements.setdefault("enhanced_monitoring", False)
        return notices

    @staticmethod
    def _strip_non_numeric_placeholders(intent: ParsedIntent) -> None:
        """Remove model-authored placeholders from fields consumed as numbers.

        Natural language such as ``按实际使用量计费`` means that the customer
        has not supplied a quantity.  Smaller models occasionally copy that
        phrase into a numeric JSON field.  Treat it as missing so metered
        services can return an official unit rate and sizing services can ask
        a concise business question instead of exposing ``invalid_requirement``.
        """

        numeric_fields = {
            "vcpu",
            "memory_gib",
            "system_disk_gib",
            "storage_gib",
            "storage_iops",
            "storage_throughput_mbps",
            "ebs_iops",
            "ebs_throughput_mbps",
            "data_transfer_in_gib",
            "data_transfer_regional_gib",
            "data_transfer_out_gib",
            "data_transfer_gib",
            "transfer_gib",
            "processed_bytes_gib",
            "processed_bytes_ec2_ip_gib_per_hour",
            "new_connections_per_second",
            "average_connection_duration_seconds",
            "active_connections_per_minute",
            "requests_per_second",
            "rule_evaluations_per_request",
            "rule_evaluations_per_second",
            "lcu_count",
            "https_requests",
            "https_requests_per_month",
            "request_count",
            "requests",
            "dns_queries",
            "outbound_messages",
            "log_ingestion_gib",
            "custom_metrics",
            "accelerators",
            "messages",
            "connection_minutes",
            "throughput_mbps_per_tib",
        }
        for service in intent.services:
            requirements = service.requirements
            kind = QuoteService._service_kind(service.service)
            service_key = kind.value if kind is not None else service.service
            sanitized = sanitize_requirement_values(requirements, service=service_key)
            requirements.clear()
            requirements.update(sanitized)
            canonical = canonicalize_requirement_fields(
                requirements,
                service=service_key,
            )
            # Promote normalized numeric aliases while leaving customer-facing
            # enum spelling intact for parsers that already return a guarded
            # draft. Service adapters normalize enum spelling themselves.
            for key in numeric_fields:
                if key in canonical:
                    requirements[key] = canonical[key]
            for key in numeric_fields.intersection(requirements):
                value = requirements.get(key)
                if value is None:
                    requirements.pop(key, None)
                    continue
                if isinstance(value, bool):
                    requirements.pop(key, None)
                    continue
                if isinstance(value, (int, float)):
                    continue
                try:
                    float(str(value).strip())
                except (TypeError, ValueError):
                    requirements.pop(key, None)

    @staticmethod
    def _strip_non_pricing_context(intent: ParsedIntent) -> None:
        """Keep prose/context available in source_text but outside pricing.

        This also cleans restored drafts created before the price-only
        extraction contract was introduced, so an old customer link cannot
        reintroduce a non-price field as a blocking catalog constraint.
        """

        for component in intent.services:
            component.requirements = strip_non_pricing_context_fields(
                component.service, component.requirements
            )
            retained_paths = {
                f"requirements.{field}" for field in component.requirements
            }
            component.field_sources = {
                path: value
                for path, value in component.field_sources.items()
                if not path.startswith("requirements.") or path in retained_paths
            }
            component.field_evidence = {
                path: value
                for path, value in component.field_evidence.items()
                if not path.startswith("requirements.") or path in retained_paths
            }
            component.locked_fields = [
                path
                for path in component.locked_fields
                if not path.startswith("requirements.") or path in retained_paths
            ]
            component.field_match_policies = {
                field: value
                for field, value in component.field_match_policies.items()
                if field in component.requirements
            }
            component.field_scopes = {
                field: value
                for field, value in component.field_scopes.items()
                if field in component.requirements
            }

    async def create_quote(
        self,
        request: QuoteRequest,
        reporter: ProgressReporter | None = None,
    ) -> QuoteResponse:
        if request.draft_id and request.draft_id.startswith("az"):
            raise ManualConfirmationRequired(
                "检测到 Azure 草稿被提交到 AWS 报价系统，已阻止报价",
                code="cloud_provider_boundary_violation",
                provider="aws",
            )
        if request.draft_id and self._confirmation_sessions is not None:
            review_status = self._confirmation_sessions.status_by_draft(request.draft_id)
            if review_status is not None and review_status not in {"approved", "completed"}:
                raise ManualConfirmationRequired(
                    "客户尚未确认最终配置清单，系统不会提前开始报价",
                    code="configuration_review_required",
                    draft_id=request.draft_id,
                    confirmation_status=review_status,
                )
        # Do not consume the draft before official pricing succeeds. A pricing
        # adapter can discover a new customer decision that was impossible to
        # know during the initial catalog preview; that decision must continue
        # from the same structured draft instead of reparsing customer prose.
        cached = self._drafts.get(request.draft_id) if request.draft_id else None
        if cached is None and request.draft_id and self._confirmation_sessions is not None:
            # The customer may approve the review after the API worker has
            # restarted.  Restore the exact reviewed draft instead of parsing
            # the original prose again and losing the reviewed model locks.
            cached = self._confirmation_sessions.restore_draft(request.draft_id)
        if cached and cached[0] == request.customer_request:
            intent = cached[1].model_copy(deep=True)
            if request.draft_id:
                self._drafts[request.draft_id] = (
                    request.customer_request,
                    intent.model_copy(deep=True),
                )
            if reporter:
                await reporter("ai", "已使用通过 AWS 官方预检的需求，不再重复解析")
        else:
            if reporter:
                await reporter("ai", "系统正在拆分客户报价任务")
            parser_arguments = (
                {"reporter": reporter}
                if "reporter" in inspect.signature(self._parser.parse).parameters
                else {}
            )
            intent = await self._parser.parse(request.customer_request, **parser_arguments)
            merged_transfer_items = self._merge_transfer_only_ec2_services(intent)
            if reporter:
                await reporter("ai", f"已整理 {len(intent.services)} 项 AWS 配置")
                if merged_transfer_items:
                    await reporter(
                        "ai",
                        f"已将 {merged_transfer_items} 项独立公网流量合并到对应 EC2 配置",
                    )
            validation_intent = intent.model_copy(deep=True)
            self._apply_sales_pricing_choice(validation_intent, request)
            await self._require_official_spec_confirmation(validation_intent)

        # Saved drafts and fresh parser output use the same product-identity
        # guard. A pricing-family adapter must not relabel an approved product.
        preserve_customer_configuration(intent)
        DeepSeekIntentParser.reconcile_customer_pricing_facts(intent)
        DeepSeekIntentParser._split_eks_worker_nodes(intent)
        enforce_component_integrity(intent)
        DeepSeekIntentParser._normalize_database_group_quantity(intent)
        DeepSeekIntentParser._normalize_redis_topology(intent)
        DeepSeekIntentParser._normalize_cluster_group_quantities(intent)
        self._strip_non_numeric_placeholders(intent)
        self._strip_non_pricing_context(intent)

        scenario_requests: list[QuoteRequest] = []
        if request.include_on_demand_scenario or request.pricing_mode == "on_demand":
            scenario_requests.append(
                request.model_copy(
                    update={
                        "pricing_mode": "on_demand",
                        "reserved_term_years": None,
                        "reserved_term_options": None,
                        "payment_option": None,
                        "include_on_demand_scenario": False,
                    }
                )
            )
        if request.pricing_mode != "on_demand":
            for term in request.reserved_term_options or [request.reserved_term_years or 1]:
                scenario_requests.append(
                    request.model_copy(
                        update={
                            "reserved_term_years": term,
                            "reserved_term_options": [term],
                            "include_on_demand_scenario": False,
                        }
                    )
                )
        scenario_quotes: list[tuple[QuoteRequest, QuoteResponse]] = []
        unavailable_scenario_notices: list[str] = []
        # These caches live only for this final quote. Components remain fully
        # isolated, while identical official selection/BCM results can be
        # reused across the on-demand, 1-year and 3-year presentation columns.
        # No price is persisted beyond this request.
        component_selection_cache: dict[str, SelectedResource] = {}
        bcm_component_cache: dict[str, BcmQuoteResult] = {}
        for scenario_request in scenario_requests:
            scenario_intent = intent.model_copy(deep=True)
            self._apply_sales_pricing_choice(scenario_intent, scenario_request)
            scenario_customer_ledger = capture_customer_ledger(scenario_intent)
            default_notices = self._apply_calculator_minimum_defaults(scenario_intent)
            restore_customer_ledger(scenario_intent, scenario_customer_ledger)
            if reporter and len(scenario_requests) > 1:
                await reporter(
                    "calculator",
                    f"正在核算{self._pricing_scenario_label(scenario_request)}",
                )
            try:
                quote = await self._create_api_quote(
                    scenario_intent,
                    scenario_request,
                    reporter,
                    default_notices=default_notices,
                    component_selection_cache=component_selection_cache,
                    bcm_component_cache=bcm_component_cache,
                )
            except ManualConfirmationRequired as exc:
                # A confirmed workload can still have no matching Reserved
                # offer for one particular term/payment combination.  That is
                # an unavailable commercial option, not missing customer
                # configuration.  Keep the other comparison scenarios and do
                # not send a technical catalog condition back to the customer.
                if (
                    scenario_request.pricing_mode != "on_demand"
                    and self._is_unavailable_pricing_scenario(exc)
                ):
                    scenario_label = self._pricing_scenario_label(scenario_request)
                    display_name = str(exc.details.get("display_name") or "部分已确认组件")
                    unavailable_scenario_notices.append(
                        f"{scenario_label}：{display_name} 的当前型号没有对应的官方价格，"
                        "本方案暂不展示；其他可用方案已正常核价。"
                    )
                    if reporter:
                        await reporter(
                            "calculator",
                            f"{scenario_label}暂无完整官方价格，继续核算其他方案",
                        )
                    continue
                raise await self._late_customer_confirmation(
                    exc,
                    request=scenario_request,
                    intent=scenario_intent,
                ) from exc
            scenario_quotes.append((scenario_request, quote))

        if not scenario_quotes:
            raise ManualConfirmationRequired(
                "所选预留期限与付款方式没有完整的官方价格，请改用其他报价方案后重试",
                code="pricing_scenarios_unavailable",
            )

        primary = scenario_quotes[0][1]
        if unavailable_scenario_notices:
            primary = primary.model_copy(
                update={
                    "notices": [
                        *primary.notices,
                        *unavailable_scenario_notices,
                    ]
                }
            )
        scenarios = [
            PricingScenario(
                label=self._pricing_scenario_label(scenario_request),
                pricing_mode=scenario_request.pricing_mode,
                reserved_term_years=scenario_request.reserved_term_years,
                payment_option=scenario_request.payment_option,
                quote_id=quote.quote_id,
                total_cost=quote.total_cost,
                upfront_cost=quote.upfront_cost,
                currency=quote.currency,
                priced_lines=quote.priced_lines,
                component_costs=self._component_costs(
                    quote.selections,
                    quote.priced_lines,
                ),
                is_partial=quote.is_partial,
                incomplete_component_ids=quote.incomplete_component_ids,
            )
            for scenario_request, quote in scenario_quotes
        ]
        self._validate_component_scenarios(primary.selections, scenarios)
        all_incomplete_component_ids = list(
            dict.fromkeys(
                component_id
                for _, quote in scenario_quotes
                for component_id in quote.incomplete_component_ids
            )
        )
        result = primary.model_copy(
            update={
                "pricing_scenarios": scenarios,
                "is_partial": bool(all_incomplete_component_ids),
                "incomplete_component_ids": all_incomplete_component_ids,
            }
        )
        if request.draft_id and self._confirmation_sessions is not None:
            self._confirmation_sessions.complete_by_draft(request.draft_id)
        return result

    @staticmethod
    def _component_costs(
        selections: list[SelectedResource],
        priced_lines: list[PricedLine],
    ) -> dict[str, float]:
        """Bind exact priced-line keys to their stable component ids."""

        costs: dict[str, float] = {}
        for fallback_index, selection in enumerate(selections):
            component_id = selection.component_id or str(fallback_index)
            try:
                ordinal = int(component_id) + 1
            except ValueError:
                ordinal = fallback_index + 1
            pattern = re.compile(rf"^(?:s|az){ordinal}(?:l\d+|commit)$")
            costs[component_id] = sum(
                float(line.cost)
                for line in priced_lines
                if pattern.fullmatch(line.key)
            )
        return costs

    @staticmethod
    def _validate_component_scenarios(
        selections: list[SelectedResource],
        scenarios: list[PricingScenario],
    ) -> None:
        """Refuse a final table whose independent component ledger is incomplete."""

        component_ids = [
            selection.component_id or str(index)
            for index, selection in enumerate(selections)
        ]
        expected = set(component_ids)
        if len(expected) != len(component_ids):
            raise RuntimeError("duplicate component id in final quote")
        for scenario in scenarios:
            if set(scenario.component_costs) != expected:
                raise RuntimeError(
                    f"incomplete component ledger for pricing scenario {scenario.label}"
                )
            component_total = sum(scenario.component_costs.values())
            if abs(component_total - scenario.total_cost) > 0.000001:
                raise RuntimeError(
                    f"component ledger total mismatch for pricing scenario {scenario.label}"
                )

    @staticmethod
    def _confirmation_answer_key(component_id: str | None, question: str) -> str:
        """Create an identity that never uses visible wording as component scope."""

        scope = f"component-{component_id}" if component_id is not None else "global"
        digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]
        return f"{scope}:{digest}"

    @staticmethod
    def _scoped_confirmation_response_key(component_index: int, question: str) -> str:
        return f"__component_answer__{component_index}::{question}"

    @staticmethod
    def _confirmation_response_question(response_key: str) -> str:
        if response_key.startswith("__component_answer__") and "::" in response_key:
            return response_key.split("::", 1)[1]
        return response_key

    @staticmethod
    def _confirmation_question_key(question: str) -> str:
        compact = re.sub(r"[\s，。；;、：:？！?]+", "", question).casefold()
        product_aliases = (
            ("opensearch", ("opensearch", "开放搜索")),
            ("eks_worker", ("eks工作节点", "eksworker", "worker节点")),
            ("eks", ("eks", "kubernetes")),
            ("elasticache", ("elasticache", "redis")),
            ("rds", ("rds", "aurora", "数据库")),
            ("msk", ("msk", "kafka")),
            ("nacos", ("nacos",)),
            ("ec2", ("ec2", "云服务器")),
            ("s3", ("s3", "对象存储")),
            ("cloudfront", ("cloudfront", "cdn")),
            ("waf", ("waf",)),
        )
        product = next(
            (
                name
                for name, aliases in product_aliases
                if any(alias in compact for alias in aliases)
            ),
            "",
        )
        categories = (
            ("hosting", ("自建", "托管", "managed")),
            ("region", ("区域", "region")),
            # A selected RDS version is one closed decision even when the
            # adapter later phrases the official patch differently. Keep this
            # before the generic engine category so 5.7 -> 8.4 cannot create
            # a new apparent question identity on every validation pass.
            (
                "engine_version",
                ("数据库版本", "引擎版本", "engineversion", "engine version", "mysql版本"),
            ),
            # The intake AI and the RDS catalog preflight phrase this same
            # closed decision differently. Give it one stable identity so a
            # submitted answer cannot trigger another confirmation round.
            ("engine", ("数据库类型", "数据库引擎", "引擎类型", "engine")),
            ("shape_model", ("型号", "机型", "vcpu", "cpu", "处理器", "内存", "规格")),
            ("storage", ("存储", "硬盘", "磁盘", "容量")),
            ("deployment", ("主备", "高可用", "可用区", "部署方式")),
            ("quantity", ("数量", "节点数", "几台", "台数")),
            ("traffic", ("流量", "请求量", "请求数")),
        )
        category = next(
            (name for name, aliases in categories if any(alias in compact for alias in aliases)),
            "",
        )
        return f"{product}|{category}" if product and category else compact

    async def _late_customer_confirmation(
        self,
        error: ManualConfirmationRequired,
        *,
        request: QuoteRequest,
        intent: ParsedIntent,
    ) -> ManualConfirmationRequired:
        """Turn a new pricing-time business issue into another customer form.

        Technical/catalog failures are never presented as customer questions.
        Repeated questions and excessive rounds are stopped as system errors
        instead of sending the customer through an infinite loop.
        """

        draft_id = request.draft_id
        asked = self._asked_confirmation_questions.setdefault(draft_id or "", set())
        rounds = self._confirmation_rounds.get(draft_id or "", 0)

        if not isinstance(
            error.details.get("component_errors"), list
        ) and self._is_technical_catalog_error(error):
            return error

        if not draft_id or self._confirmation_sessions is None:
            return error
        # Pricing runs asynchronously and the process may have restarted since
        # the customer answered.  The database is therefore authoritative for
        # both asked questions and round count; relying only on this process's
        # memory caused the same decision to be presented again after submit.
        asked.update(
            self._confirmation_question_key(item)
            for item in self._confirmation_sessions.asked_questions_by_draft(draft_id)
        )
        rounds = max(
            rounds,
            self._confirmation_sessions.confirmation_round_by_draft(draft_id),
        )
        if rounds >= 3:
            return ManualConfirmationRequired(
                "配置已经过多轮确认，系统已停止继续询问，避免形成确认循环",
                code="confirmation_round_limit_reached",
                original_error_code=error.code,
            )

        raw_errors = error.details.get("component_errors")
        errors = (
            [item for item in raw_errors if isinstance(item, ManualConfirmationRequired)]
            if isinstance(raw_errors, list)
            else [error]
        )
        items: list[ConfirmationItem] = []
        questions: list[str] = []
        question_keys: set[str] = set()
        original_codes: list[str] = []
        for current_error in errors:
            if self._is_technical_catalog_error(current_error):
                continue
            service_index = current_error.details.get("service_index")
            try:
                component_index = int(service_index)
            except (TypeError, ValueError):
                component_index = -1
            service = (
                intent.services[component_index]
                if 0 <= component_index < len(intent.services)
                else None
            )
            display_name = str(current_error.details.get("display_name") or "该服务")
            question = self._plugin_confirmation_question(display_name, service, current_error)
            question_key = self._confirmation_question_key(question)
            if question_key in asked or question_key in question_keys:
                continue
            candidates = self._candidate_options_from_error(current_error)
            if not candidates and service is not None:
                kind = self._service_kind(service.service)
                plugin = (
                    self._plugins.get(kind)
                    if kind is not None
                    else self._generic_plugin
                )
                if plugin is not None:
                    candidates = await self._confirmation_candidates_for_failure(
                        plugin=plugin,
                        component=service,
                        failure=current_error,
                        display_name=display_name,
                    )
            options = self._compact_candidate_options(candidates, service) if candidates else []
            if not options:
                options = self._default_confirmation_options(question)
            items.append(
                ConfirmationItem(
                    question=question,
                    options=options,
                    component_id=(str(component_index) if component_index >= 0 else None),
                    service=service.service if service is not None else None,
                    selection_mode=self._confirmation_selection_mode(question, options),
                )
            )
            questions.append(question)
            question_keys.add(question_key)
            original_codes.append(current_error.code)

        if not items:
            return ManualConfirmationRequired(
                "客户已经回答过这些问题，但答案未能形成可报价配置；系统已停止重复询问，请检查答案应用逻辑",
                code="confirmation_answer_not_applied",
                original_error_code=error.code,
            )

        unavailable_choices = [
            item.question for item in items if item.selection_mode != "text" and not item.options
        ]
        if unavailable_choices:
            return ManualConfirmationRequired(
                "官方可选项尚未准备完成，系统已阻止把选择题降级为手动填写",
                code="confirmation_options_unavailable",
                questions=unavailable_choices,
                original_error_code=error.code,
            )

        self._drafts[draft_id] = (request.customer_request, intent.model_copy(deep=True))
        token = self._confirmation_sessions.create_or_replace(
            draft_id=draft_id,
            customer_request=request.customer_request,
            customer_summary=intent.customer_summary,
            intent=intent,
            confirmation_text=self._confirmation_text(questions),
            items=items,
            quote_request=request,
        )
        asked.update(question_keys)
        self._confirmation_rounds[draft_id] = rounds + 1
        return ManualConfirmationRequired(
            f"官方核价发现 {len(items)} 项配置需要客户确认，请在同一页回答后继续报价",
            code="late_customer_confirmation_required",
            confirmation_text=self._confirmation_text(questions),
            confirmation_items=[item.model_dump(mode="json") for item in items],
            confirmation_token=token,
            draft_id=draft_id,
            confirmation_round=rounds + 1,
            original_error_code=original_codes[0] if len(original_codes) == 1 else None,
            original_error_codes=original_codes,
        )

    @staticmethod
    def _pricing_scenario_label(request: QuoteRequest) -> str:
        if request.pricing_mode == "on_demand":
            return "按需"
        payment = {
            "no_upfront": "无预付",
            "partial_upfront": "部分预付",
            "all_upfront": "全预付",
        }.get(request.payment_option or "no_upfront", "")
        label = f"{request.reserved_term_years}年{payment}"
        if request.pricing_mode == "convertible_reserved":
            return f"{label}（可转换）"
        return label

    @staticmethod
    def _is_unavailable_pricing_scenario(error: ManualConfirmationRequired) -> bool:
        """Return whether only the requested commercial offer is unavailable."""

        return error.code in {
            "reserved_term_not_found",
            "reserved_price_dimensions_missing",
        }

    @staticmethod
    def _apply_sales_pricing_choice(intent: ParsedIntent, request: QuoteRequest) -> None:
        """Apply sales defaults without overwriting final-review corrections.

        Purchase wording in the original request remains subordinate to the
        salesperson's form.  A later, explicit component correction on the
        final configuration page is authoritative for that component.
        """

        for service in intent.services:
            requirements = service.requirements
            pricing_fields = (
                "purchase_option",
                "reserved_term_years",
                "payment_option",
            )
            has_customer_override = any(
                service.field_sources.get(f"requirements.{key}")
                in {
                    "customer_text",
                    "customer_confirmation",
                    "customer_confirmation_removed",
                    "customer_correction",
                }
                for key in pricing_fields
            )
            if has_customer_override:
                # The correction path writes a coherent purchase option, term
                # and payment tuple. Never let a subsequent preview/create
                # request silently reset it to the original sales default.
                if requirements.get("purchase_option") == "on_demand":
                    requirements.pop("reserved_term_years", None)
                    requirements.pop("payment_option", None)
                    if QuoteService._service_kind(service.service) == ServiceKind.EC2:
                        requirements["utilization_percent"] = request.utilization_percent
                continue
            for key in (
                "purchase_option",
                "reserved_term_years",
                "payment_option",
                "utilization_percent",
            ):
                requirements.pop(key, None)

            kind = QuoteService._service_kind(service.service)
            is_memorydb = re.sub(
                r"[^a-z0-9]", "", service.service.casefold()
            ) in {"memorydb", "amazonmemorydb"}
            if kind not in {ServiceKind.EC2, ServiceKind.RDS, ServiceKind.REDIS} and not is_memorydb:
                continue

            if request.pricing_mode == "on_demand":
                requirements["purchase_option"] = "on_demand"
                if kind == ServiceKind.EC2:
                    requirements["utilization_percent"] = request.utilization_percent
                continue

            requirements["purchase_option"] = (
                request.pricing_mode if kind == ServiceKind.EC2 else "reserved"
            )
            requirements["reserved_term_years"] = request.reserved_term_years or 1
            requirements["payment_option"] = request.payment_option or "no_upfront"

    async def _require_official_spec_confirmation(
        self,
        intent: ParsedIntent,
        *,
        component_ids: set[int] | None = None,
    ) -> None:
        sizing_options: list[dict[str, object]] = []
        model_compatibility_notices: list[str] = []
        for index, service in enumerate(intent.services):
            if component_ids is not None and index not in component_ids:
                continue
            kind = self._service_kind(service.service)
            if kind != ServiceKind.EC2:
                continue
            plugin = self._plugins.get(kind)
            compatibility_resolver = getattr(plugin, "specified_model_compatibility_notice", None)
            if compatibility_resolver is not None:
                try:
                    compatibility_notice = await asyncio.wait_for(
                        asyncio.to_thread(compatibility_resolver, service, "ap-southeast-1"),
                        timeout=30,
                    )
                except TimeoutError as exc:
                    raise ManualConfirmationRequired(
                        "AWS 官方规格查询超时，请稍后重试",
                        code="official_spec_lookup_timeout",
                        service=service.service,
                    ) from exc
                if compatibility_notice:
                    model_compatibility_notices.append(str(compatibility_notice))
            # A customer-provided CPU/memory shape is sufficient. Individual
            # adapters select the cheapest official non-underprovisioned model.
            # Do not turn the absence of an exact shape into a customer question.

        design_notices = self._confirmation_notices(intent)
        design_notices.extend(model_compatibility_notices)
        design_notices.extend(self._missing_spec_confirmation_notices(intent))
        design_notices = self._deduplicate_confirmation_notices(design_notices)
        if not sizing_options and not design_notices:
            return

        lines = ["您好，麻烦确认下面几项："]
        number = 1
        for item in sizing_options:
            lines.append(f"{number}. {self._sizing_confirmation_question(item)}")
            number += 1
        for notice in design_notices:
            lines.append(f"{number}. {self._compact_customer_question(notice)}")
            number += 1
        lines.append("直接回复选项即可，谢谢。")
        confirmation_text = "\n".join(lines)
        raise ManualConfirmationRequired(
            "客户需求包含非标准规格或架构冲突，请先把确认话术发给客户",
            code="official_spec_confirmation_required",
            confirmation_text=confirmation_text,
            sizing_options=sizing_options,
            design_notices=design_notices,
        )

    @staticmethod
    def _missing_spec_confirmation_notices(intent: ParsedIntent) -> list[str]:
        # Missing model/specification is not a customer question. Every plugin
        # applies the official lowest-cost baseline and discloses that choice in
        # the quote. Only explicit conflicts remain confirmation items.
        return []

    @staticmethod
    def _is_blocking_design_notice(notice: str) -> bool:
        text = notice.casefold()
        return any(
            marker in text
            for marker in (
                "冲突",
                "矛盾",
                "无法同时",
                "不支持",
                "固定公网 ip",
                "固定 ip",
                "single-az",
                "single az",
                "备用库不能",
                "同可用区",
                "url 路径",
                "express one zone",
                "anycast static ip",
            )
        )

    @staticmethod
    def _compact_customer_question(notice: str) -> str:
        notice = QuoteService._plain_customer_words(notice)
        if "?" in notice or "？" in notice:
            return notice.strip()
        text = notice.casefold()
        if "相邻规格" in notice and "请选择" in notice:
            return f"{notice.strip().rstrip('。；; ')}？"
        if ("single-az" in text or "single az" in text) and any(
            marker in text for marker in ("故障", "主备", "高可用", "切换")
        ):
            return "您原选 Single-AZ，但它不提供主备自动切换；要自动切换需改为 Multi-AZ，是否同意？"
        if any(marker in text for marker in ("固定公网 ip", "固定 ip")):
            if "cloudfront" in text:
                return (
                    "您要求 CloudFront 使用固定公网 IP，这需要启用额外收费的 "
                    "Anycast Static IP，是否启用？"
                )
            return (
                "您要求 ALB 使用固定公网 IP，但 ALB 的 IP 会变化；"
                "是否改用支持固定 IP 的 NLB 或 Global Accelerator？"
            )
        if "ec2" in text and "单可用区" in text and "跨可用区" in text:
            return (
                "您要求服务器全部放在一个可用区，但又要求该区故障后切到另一区；"
                "要自动切换需跨可用区部署，是否同意？"
            )
        if "rds" in text and "备用库" in text and "只读" in text:
            return (
                "您要求 RDS Multi-AZ 备用库跑只读查询，但主备模式备用库不可读；"
                "请选择 Multi-AZ DB cluster，或另建只读副本。"
            )
        if "redis" in text and "同可用区" in text and "故障" in text:
            return (
                "您要求 Redis 两节点放在同一可用区，但该区故障时两台都会不可用；"
                "要自动切换需跨可用区部署，是否同意？"
            )
        if "nlb" in text and "url" in text and "路径" in text:
            return "您要求 NLB 按 /api、/static 路径转发，但 NLB 不支持路径规则；是否改用 ALB？"
        if "s3 standard" in text and "express one zone" in text:
            return (
                "您要求 S3 Standard 在 7 天后转为 Express One Zone，但两者不能生命周期转换；"
                "请确认保留 Standard，还是单独使用 Express One Zone？"
            )
        if "redis" in text or "缓存" in text:
            if any(marker in text for marker in ("冲突", "矛盾", "无法同时")):
                values = re.search(
                    r"整套\s*(\d+(?:\.\d+)?)g.*?每(?:个)?节点(?:至少)?\s*(\d+(?:\.\d+)?)g",
                    text,
                )
                if values:
                    return (
                        f"您原填写 Redis 整套 {values.group(1)}G、每节点 {values.group(2)}G，"
                        "两者不一致；请确认以哪个为准？"
                    )
                return (
                    "您同时填写了 Redis 整套容量和每节点容量，两者不一致；"
                    "请确认以整套容量还是每节点容量为准？"
                )
        compact = notice.strip().rstrip("。；; ")
        return f"{compact}？"

    @staticmethod
    def _compact_customer_source(requirement: object) -> str:
        source = str(getattr(requirement, "source_text", "") or "")
        source = re.sub(r"\s+", " ", source).strip()
        if len(source) > 140:
            return source[:137].rstrip() + "…"
        return source

    @classmethod
    def _customer_confirmation_question(
        cls,
        display_name: str,
        requirement: object,
        notice: str,
    ) -> str:
        clean = cls._plain_customer_words(notice)
        clean = re.sub(r"(?:请|由)?销售(?:人员)?确认", "请您确认", clean)
        clean = clean.replace("找销售确认", "请您确认")
        clean = clean.strip().lstrip("。；; ")
        clean = re.sub(r"^当前配置尚不能直接核价[：:]\s*", "", clean)
        if not clean:
            clean = "还缺少必要信息，请在下方补充。"
        # A well-formed causal sentence already identifies the request and the
        # action. Do not wrap it in another product prefix.
        if clean.startswith(("您", "请")):
            return clean

        identity = display_name.casefold()
        if "eks worker" in identity or "worker nodes" in identity:
            customer_name = "EKS 工作节点"
        elif "elasticache" in identity or "redis" in identity:
            customer_name = "Redis"
        elif "postgres" in identity:
            customer_name = "RDS PostgreSQL"
        elif "mysql" in identity:
            customer_name = "RDS MySQL"
        elif "opensearch" in identity:
            customer_name = "OpenSearch"
        else:
            customer_name = display_name.removeprefix("Amazon ").strip() or "该组件"
        if customer_name.casefold() in clean.casefold():
            return clean
        return f"{customer_name}：{clean}"

    @classmethod
    def _plugin_confirmation_question(
        cls,
        display_name: str,
        requirement: object,
        error: ManualConfirmationRequired,
    ) -> str:
        code = error.code
        requirements = getattr(requirement, "requirements", {})
        if code == "service_region_not_supported":
            region = str(error.details.get("region") or "当前区域")
            question = f"{display_name} 在 {region} 不能使用。您想改到哪个地区？"
            return cls._customer_confirmation_question(display_name, requirement, question)
        if code == "service_retired":
            question = f"{display_name} 已经停止提供。您想换成其他服务，还是不报这一项？"
            return cls._customer_confirmation_question(display_name, requirement, question)
        if code == "unsupported_service":
            question = (
                "AWS 没有直接对应的服务。您想改用 AWS 上的自建方案，"
                "还是不报这一项？"
            )
            return cls._customer_confirmation_question(display_name, requirement, question)
        if code == "unsupported_rds_engine_or_region":
            engine = str(
                error.details.get("engine")
                or (requirements.get("engine") if isinstance(requirements, dict) else "")
                or "数据库"
            ).strip()
            requested_version = str(
                error.details.get("requested_version")
                or (requirements.get("engine_version") if isinstance(requirements, dict) else "")
                or ""
            ).strip()
            region = str(error.details.get("region") or "当前区域").strip()
            raw_versions = error.details.get("supported_engine_versions")
            supported_versions = (
                [str(version).strip() for version in raw_versions if str(version).strip()]
                if isinstance(raw_versions, list)
                else []
            )
            version_choices = (
                f"可选版本：{'、'.join(supported_versions)}。"
                if supported_versions
                else "可选择由系统自动使用当前区域仍受维护的版本。"
            )
            if requested_version:
                question = (
                    f"您填写的 {engine} {requested_version} 在 {region} 已不能新购。"
                    f"您想改用哪个可用版本？{version_choices}"
                )
            else:
                question = (
                    f"{engine} 在 {region} 暂时没有可购买的配置。"
                    f"您想改用哪个可用版本？{version_choices}"
                )
            return cls._customer_confirmation_question(display_name, requirement, question)
        if code == "insufficient_redis_requirements":
            topology = ""
            if isinstance(requirements, dict):
                shards = requirements.get("shards")
                replicas = requirements.get("replicas_per_shard")
                if shards == 1 and replicas is not None:
                    topology = f"您已选 Redis 1 主 {int(replicas)} 从，"
            question = (
                f"{topology}但还缺少单节点容量。每节点大概需要 1G、4G 还是 8G 内存？"
                "型号由系统自动选择。"
            )
            return cls._customer_confirmation_question(display_name, requirement, question)
        if code == "unsupported_cache_engine_or_region":
            engine = str(
                error.details.get("engine")
                or (requirements.get("engine") if isinstance(requirements, dict) else "")
                or "redis"
            ).strip()
            requested_version = str(
                error.details.get("requested_version")
                or (requirements.get("engine_version") if isinstance(requirements, dict) else "")
                or ""
            ).strip()
            region = str(error.details.get("region") or "当前区域").strip()
            raw_versions = error.details.get("supported_engine_versions")
            supported_versions = (
                [str(version).strip() for version in raw_versions if str(version).strip()]
                if isinstance(raw_versions, list)
                else []
            )
            if supported_versions:
                question = (
                    f"您填写的 {engine.title()} {requested_version or '版本'} 在 {region} "
                    f"不能使用。您想改用哪个版本？可选：{'、'.join(supported_versions)}。"
                )
            else:
                question = (
                    f"{engine.title()} 在 {region} 不能购买。您想改到哪个地区？"
                )
            return cls._customer_confirmation_question(display_name, requirement, question)
        if code == "insufficient_ec2_requirements":
            question = "还缺少处理器或内存要求，请补充每台需要几核、多少内存。"
            return cls._customer_confirmation_question(display_name, requirement, question)
        if code == "ec2_specification_not_found":
            question = "没有和您填写的核数、内存完全一样的型号。请从下面选择一个合适的配置。"
            return cls._customer_confirmation_question(display_name, requirement, question)
        if code == "generic_official_specification_not_found":
            requested_model = str(error.details.get("requested_model") or "").strip()
            requested_vcpu = error.details.get("requested_vcpu")
            requested_memory = error.details.get("requested_memory_gib")
            supplied = "、".join(
                part
                for part in (
                    f"型号 {requested_model}" if requested_model else "",
                    f"{float(requested_vcpu):g} 核"
                    if isinstance(requested_vcpu, (int, float))
                    else "",
                    f"{float(requested_memory):g} GB 内存"
                    if isinstance(requested_memory, (int, float))
                    else "",
                )
                if part
            )
            question = (
                f"您填写的{supplied or '几项配置'}对不上。"
                "请从下面选择这次要使用的配置。"
            )
            return cls._customer_confirmation_question(display_name, requirement, question)
        if code == "billing_variant_required":
            # This adapter already supplies a short, customer-facing question
            # and official choices.  Do not wrap it in the generic failure
            # wording, which makes a normal price choice sound like an error.
            return cls._customer_confirmation_question(
                display_name,
                requirement,
                error.message,
            )
        if code in {"insufficient_rds_requirements", "rds_specification_not_found"}:
            if isinstance(requirements, dict):
                engine = requirements.get("engine")
                vcpu = requirements.get("vcpu")
                memory = requirements.get("memory_gib")
                missing: list[str] = []
                if engine in (None, ""):
                    missing.append("数据库类型")
                if vcpu in (None, ""):
                    missing.append("CPU")
                if memory in (None, ""):
                    missing.append("内存")
                if missing:
                    question = (
                        f"数据库还缺少{'、'.join(missing)}。请确认大概需要几核、多少内存，"
                        "型号由系统自动选择。"
                        if engine not in (None, "")
                        else "请确认数据库类型，以及大概需要几核、多少内存；型号由系统自动选择。"
                    )
                    return cls._customer_confirmation_question(display_name, requirement, question)
            question = (
                "没有和您填写的数据库核数、内存完全一样的型号，"
                "请从下面选择一个合适的配置。"
                if code == "rds_specification_not_found"
                else "请确认数据库大概需要几核、多少内存。"
            )
            return cls._customer_confirmation_question(display_name, requirement, question)
        message = error.message.strip().rstrip("。；; ")
        question = f"这项信息还不能计算价格：{message}。请从下面选择，或补充需要的配置。"
        return cls._customer_confirmation_question(display_name, requirement, question)

    @staticmethod
    def _is_technical_catalog_error(error: ManualConfirmationRequired) -> bool:
        code = error.code.casefold()
        return any(
            marker in code
            for marker in (
                "discovery_failed",
                "credentials_invalid",
                "lookup_timeout",
                "catalog_temporarily_unavailable",
                "pricing_candidates_not_found",
                "ambiguous_billing",
                "backend_unavailable",
                "invalid_requirement",
                "incomplete_billing_dimensions",
                "billing_product_not_found",
                "billing_dimension_not_found",
                "pricing_catalog_unavailable",
                "pricing_attribute_values_unavailable",
                "unsupported_or_unknown_region",
                "reference_unit_rate_not_found",
                "adapter_not_ready",
                "unparseable_official_specification",
                "empty_cloudwatch_requirement",
                "unsupported_service",
                "generic_service_code_not_found",
                "generic_unit_rate_not_found",
                "generic_semantic_rate_not_found",
                "generic_official_shape_not_exposed",
                "service_region_not_supported",
                "auto_discovery_",
                "pricing_service_registry_unavailable",
                "msk_specification_not_found",
                "product_identity_invariant_failed",
                # BCM Pricing Calculator failures are AWS/API execution
                # failures. Customers cannot resolve them by changing a
                # workload specification, so they must never become a
                # customer confirmation question.
                "bcm_",
                "too_many_usage_lines",
                "reserved_term_not_found",
                "reserved_price_dimensions_missing",
                "pricing_scenarios_unavailable",
                "confirmation_options_unavailable",
            )
        )

    @staticmethod
    def _catalog_issue_category(
        error: ManualConfirmationRequired,
        component: ServiceRequirement,
    ) -> str:
        """Classify an official-catalog miss before it reaches the customer.

        Historically every failure in this broad bucket was rendered as an AWS
        timeout.  That hid actionable compatibility and mapping defects behind
        a misleading retry banner.  Keep the broad isolation boundary, but
        preserve the real reason all the way to the review page.
        """

        code = error.code.casefold()
        service = component.service.casefold()
        requirements = component.requirements

        if service in {"rds", "aurora"} and requirements.get("engine_version"):
            if code.startswith("rds_") or "pricing_candidates_not_found" in code:
                return "compatibility"
        if any(
            marker in code
            for marker in (
                "lookup_timeout",
                "catalog_temporarily_unavailable",
                "backend_unavailable",
                "pricing_catalog_unavailable",
                "pricing_attribute_values_unavailable",
                "pricing_service_registry_unavailable",
                "bcm_",
            )
        ):
            return "retryable"
        if any(
            marker in code
            for marker in (
                "credentials_invalid",
                "region_not_enabled",
                "unsupported_or_unknown_region",
                "adapter_not_ready",
                "discovery_failed",
                "auto_discovery_",
            )
        ):
            return "system_configuration"
        if any(
            marker in code
            for marker in (
                "unsupported_service",
                "generic_service_code_not_found",
                "service_region_not_supported",
            )
        ):
            return "unsupported"
        return "catalog_mapping"

    @classmethod
    def _should_auto_retry_component_error(
        cls,
        error: ManualConfirmationRequired,
        component: ServiceRequirement,
    ) -> bool:
        """Retry only failures that a component-local refresh can repair."""

        if error.code.casefold() in {
            "service_region_not_supported",
            "service_retired",
            "credentials_invalid",
            "aws_region_not_enabled",
            "unsupported_or_unknown_region",
            "unsupported_service",
        }:
            return False
        return cls._catalog_issue_category(error, component) in {
            "retryable",
            "catalog_mapping",
        }

    @staticmethod
    def _catalog_issue_message(
        error: ManualConfirmationRequired,
        component: ServiceRequirement,
        display_name: str,
        category: str,
    ) -> str:
        if error.code.casefold() == "service_region_not_supported":
            region = str(error.details.get("region") or component.region or "当前区域")
            return (
                f"已识别 {display_name} 为 AWS 官方托管服务，但该服务在 {region} "
                "没有可用的官方区域计费目录；系统不会改成 EC2 自建，也不会猜价。"
            )
        if category == "retryable":
            return "AWS 官方查询本次超时，系统会自动重试，当前配置无需修改。"
        if category == "compatibility":
            version = str(component.requirements.get("engine_version") or "").strip()
            return (
                f"{display_name} 的 {version or '当前'}版本无法直接匹配官方在售版本，"
                "系统将按同一主版本下最新受维护的小版本核价。"
            )
        if category == "catalog_mapping":
            return (
                f"已识别 {display_name}，但尚未安全匹配到完整计费维度；"
                "系统将重新同步官方目录，未确认前不会猜价。"
            )
        if category == "unsupported":
            return f"{display_name} 尚未建立安全的官方报价映射，本次暂不计入总价。"
        return (
            f"{display_name} 的官方核验尚未完成（{error.code}），"
            "配置已保留，系统配置恢复后可重新核验。"
        )

    @staticmethod
    def _third_party_product_name(requirement: ServiceRequirement, display_name: str) -> str:
        stored = str(requirement.field_sources.get("_third_party_product") or "").strip()
        if stored:
            return stored
        for candidate in (
            display_name,
            requirement.calculator_service_name,
            requirement.service,
        ):
            value = str(candidate or "").strip()
            if not value:
                continue
            self_hosted = re.search(r"自建\s*([^）)]+)", value, re.I)
            if self_hosted:
                return self_hosted.group(1).strip()
            value = re.sub(r"^(?:Amazon|AWS)\s+", "", value, flags=re.I)
            if value:
                return value
        return "该产品"

    @classmethod
    def _is_third_party_architecture_catalog_miss(
        cls,
        requirement: ServiceRequirement,
        display_name: str,
        error: ManualConfirmationRequired,
    ) -> bool:
        """Turn an unknown literal third-party product into a real decision.

        AWS/Amazon products with a temporarily missing catalog code remain
        technical failures.  A named non-AWS product must instead ask whether
        the customer wants an AWS managed alternative or an EC2 deployment.
        """

        if error.code.casefold() != "generic_service_code_not_found":
            return False
        identity = " ".join(
            str(value or "").strip()
            for value in (
                display_name,
                requirement.calculator_service_name,
                requirement.service,
            )
        ).strip()
        if not identity:
            return False
        return not bool(re.search(r"(?:^|\s)(?:aws|amazon)(?:\s|$)", identity, re.I))

    @classmethod
    def _recover_third_party_deployment(
        cls,
        requirement: ServiceRequirement,
        customer_request: str,
        display_name: str,
    ) -> None:
        """Restore explicit node/shape/storage values from one customer block.

        This is deliberately literal extraction only.  It never borrows data
        from another component and therefore preserves the per-component data
        boundary when the intake model returned just ``ClickHouse：``.
        """

        product_name = cls._third_party_product_name(requirement, display_name)
        blocks = re.split(
            r"(?m)(?=^\s*(?:\d+\s*[、.)）]|[-*]\s+))",
            customer_request or "",
        )
        block = next(
            (
                item.strip()
                for item in blocks
                if item.strip() and product_name.casefold() in item.casefold()
            ),
            "",
        )
        if not block:
            block = str(requirement.source_text or "").strip()
        if block:
            requirement.source_text = block

        node_match = re.search(
            r"(?:部署数量|节点数量|机器数量|机器台数|数量)\s*[:：]?\s*"
            r"(\d+)\s*(?:个|台)?\s*(?:节点|机器|实例|台)",
            block,
            re.I,
        )
        if node_match:
            requirement.quantity = max(int(node_match.group(1)), 1)
            requirement.field_sources["quantity"] = "customer_text"

        shape_match = re.search(
            r"(?:每\s*节点配置|每节点配置|每台配置|配置)\s*[:：]?\s*"
            r"(\d+(?:\.\d+)?)\s*(?:核|c|vcpu)\s*"
            r"(\d+(?:\.\d+)?)\s*(?:gib|gb|g)",
            block,
            re.I,
        )
        if shape_match:
            requirement.requirements["vcpu"] = float(shape_match.group(1))
            requirement.requirements["memory_gib"] = float(shape_match.group(2))
            requirement.field_sources["requirements.vcpu"] = "customer_text"
            requirement.field_sources["requirements.memory_gib"] = "customer_text"

        storage_match = re.search(
            r"(?:存储容量|存储)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*"
            r"(tib|tb|gib|gb|g)\s*(?:/|每)?\s*(?:节点|台)?",
            block,
            re.I,
        )
        if storage_match:
            value = float(storage_match.group(1))
            unit = storage_match.group(2).casefold()
            if unit in {"tb", "tib"}:
                value *= 1024
            requirement.requirements["system_disk_gib"] = value
            requirement.field_sources["requirements.system_disk_gib"] = "customer_text"

    @classmethod
    def _architecture_notice_component_id(
        cls,
        intent: ParsedIntent,
        notice: str,
        pending_component_ids: set[str],
    ) -> str | None:
        """Bind each architecture question to its own component.

        With several pending third-party products, falling back to the first
        component hid later questions and applied answers to the wrong row.
        Match by the isolated product identity; only use a positional fallback
        when exactly one pending component exists.
        """

        folded_notice = notice.casefold()
        matches: list[str] = []
        for component_id in sorted(pending_component_ids, key=int):
            component = intent.services[int(component_id)]
            names = {
                cls._third_party_product_name(
                    component,
                    component.calculator_service_name or component.service,
                ).casefold(),
                str(component.service or "").strip().casefold(),
            }
            names.discard("")
            if any(name in folded_notice for name in names):
                matches.append(component_id)
        if len(matches) == 1:
            return matches[0]
        if len(pending_component_ids) == 1:
            return next(iter(pending_component_ids))
        return None

    @staticmethod
    def _is_ai_repairable_component_error(error: ManualConfirmationRequired) -> bool:
        """Errors caused by structured query shape may be repaired by AI.

        Credentials, network, region activation and adapter availability cannot
        be fixed by changing customer data, so they must never consume AI turns.
        """

        code = error.code.casefold()
        blocked = (
            "credential",
            "timeout",
            "backend_unavailable",
            "catalog_temporarily_unavailable",
            "pricing_catalog_unavailable",
            "region_not_enabled",
            "adapter_not_ready",
            "unsupported_service",
            "generic_service_code_not_found",
            "generic_unit_rate_not_found",
            "generic_semantic_rate_not_found",
            "auto_discovery_",
            "pricing_service_registry_unavailable",
            "product_identity_invariant_failed",
            "bcm_",
            "too_many_usage_lines",
            # The RDS adapter already returns the requested version and the
            # official supported versions. This is a closed customer choice,
            # not a malformed query for AI to reinterpret. Sending it through
            # AI can turn the structured error into a repeated free-text
            # question and prevent the selected version from being applied.
            "unsupported_rds_engine_or_region",
        )
        if any(marker in code for marker in blocked):
            return False
        return any(
            marker in code
            for marker in (
                "invalid_requirement",
                "incomplete_billing_dimensions",
                "billing_product_not_found",
                "billing_dimension_not_found",
                "pricing_candidates_not_found",
                "ambiguous_billing",
                "unparseable_official_specification",
                "unsupported_purchase_option",
                "unsupported_cache_engine",
                "unsupported_rds_engine",
                "unsupported_s3_storage_class",
            )
        )

    @classmethod
    def _merge_transfer_only_ec2_services(cls, intent: ParsedIntent) -> int:
        """Merge an AI-created transfer-only EC2 item into its compute workload.

        Customers often put public egress on a separate line.  The parser can
        interpret that line as another EC2 service even though it contains no
        instance request.  A transfer-only line is a child cost of an existing
        EC2 workload, not a second server group.
        """

        transfer_fields = {
            "data_transfer_in_gib",
            "data_transfer_regional_gib",
            "data_transfer_out_gib",
            "data_transfer_in_gib_per_instance",
            "data_transfer_regional_gib_per_instance",
            "data_transfer_out_gib_per_instance",
        }
        compute_fields = {"requested_model", "vcpu", "memory_gib"}
        # Some models copy the preceding EC2 shape onto a later line such as
        # “公网流量：应用服务器额外 1TB/月”.  The source line itself is the
        # authority: when it only describes transfer, remove those inherited
        # shape fields before deciding whether this is another server group.
        for item in intent.services:
            if cls._service_kind(item.service) != ServiceKind.EC2:
                continue
            source = (item.source_text or "").casefold()
            has_transfer = bool(re.search(r"(?:公网|出站|下行|流量|transfer|egress)", source, re.I))
            has_shape = bool(
                re.search(
                    r"(?:\d+\s*(?:核|vcpu)|\d+(?:\.\d+)?\s*(?:gib|gb|g)\s*(?:内存)?|"
                    r"(?:型号|实例型号|instance\s*type)|\d+\s*台\s*(?:linux|windows|ec2|服务器))",
                    source,
                    re.I,
                )
            )
            has_transfer_value = any(
                item.requirements.get(key) is not None for key in transfer_fields
            )
            if has_transfer and has_transfer_value and not has_shape:
                for key in compute_fields:
                    item.requirements.pop(key, None)
        ec2_indexes = [
            index
            for index, item in enumerate(intent.services)
            if cls._service_kind(item.service) == ServiceKind.EC2
        ]
        compute_indexes = [
            index
            for index in ec2_indexes
            if any(
                intent.services[index].requirements.get(key) is not None for key in compute_fields
            )
        ]
        remove_indexes: set[int] = set()

        for index in ec2_indexes:
            item = intent.services[index]
            present_transfer_fields = {
                key for key in transfer_fields if item.requirements.get(key) is not None
            }
            if not present_transfer_fields or any(
                item.requirements.get(key) is not None for key in compute_fields
            ):
                continue

            candidates = [candidate for candidate in compute_indexes if candidate != index]
            if item.region:
                same_region = [
                    candidate
                    for candidate in candidates
                    if intent.services[candidate].region == item.region
                ]
                if same_region:
                    candidates = same_region
            if len(candidates) != 1:
                has_explicit_transfer = any(
                    cls._service_kind(candidate.service) == ServiceKind.DATA_TRANSFER
                    for candidate in intent.services
                )
                if has_explicit_transfer:
                    remove_indexes.add(index)
                else:
                    # Aggregate egress across several workload regions cannot
                    # be attached to an arbitrary EC2 group.  Quote it through
                    # the dedicated Data Transfer adapter, which applies the
                    # disclosed lowest-rate assumption when no split is given.
                    intent.services[index] = item.model_copy(
                        update={
                            "service": ServiceKind.DATA_TRANSFER.value,
                            "calculator_service_name": "AWS Data Transfer",
                        }
                    )
                continue

            target = intent.services[candidates[0]]
            merged = dict(target.requirements)
            for key in present_transfer_fields:
                incoming = item.requirements[key]
                existing = merged.get(key)
                if (
                    isinstance(existing, (int, float))
                    and not isinstance(existing, bool)
                    and isinstance(incoming, (int, float))
                    and not isinstance(incoming, bool)
                ):
                    merged[key] = float(existing) + float(incoming)
                elif existing is None:
                    merged[key] = incoming
            intent.services[candidates[0]] = target.model_copy(
                update={
                    "requirements": merged,
                    "source_text": "\n".join(
                        part for part in (target.source_text, item.source_text) if part
                    ),
                }
            )
            remove_indexes.add(index)

        if remove_indexes:
            intent.services = [
                item for index, item in enumerate(intent.services) if index not in remove_indexes
            ]
        return len(remove_indexes)

    async def _create_api_quote(
        self,
        intent: ParsedIntent,
        request: QuoteRequest,
        reporter: ProgressReporter | None,
        default_notices: list[str] | None = None,
        component_selection_cache: dict[str, SelectedResource] | None = None,
        bcm_component_cache: dict[str, BcmQuoteResult] | None = None,
    ) -> QuoteResponse:
        selections: list[SelectedResource] = []
        notices: list[str] = list(default_notices or [])
        trace: list[ExecutionEvent] = []
        usage_lines: list[UsageLine] = []
        usage_group_names: dict[str, str] = {}
        direct_priced_lines: list[PricedLine] = []
        upfront_cost = 0.0
        late_confirmation_errors: list[ManualConfirmationRequired] = []
        hierarchy = component_hierarchy(intent.services)

        def keep_unpriced_component(
            service: ServiceRequirement,
            index: int,
            display_name: str,
            message: str,
            code: str,
        ) -> SelectedResource:
            item_hierarchy = hierarchy[index]
            requested_model = str(
                service.requirements.get("requested_model")
                or service.requirements.get("_review_selected_model")
                or ""
            ).strip()
            return SelectedResource(
                component_id=str(index),
                component_number=item_hierarchy.component_number,
                parent_component_id=item_hierarchy.parent_component_id,
                parent_component_number=item_hierarchy.parent_component_number,
                parent_display_name=item_hierarchy.parent_display_name,
                service=service.service,
                display_name=display_name,
                region=service.region or "Global",
                model=requested_model or "暂未取得官方计费项",
                quantity=service.quantity,
                architecture="组件已独立保留；当前金额未计入合计",
                specifications=self._complete_selection_specifications(service, {}),
                official_product={"source": "AWS official catalog", "status": "unpriced"},
                rationale=message,
                pricing_status="unpriced",
                pricing_issue_code=code,
                pricing_notice=message,
                remarks=[message, *self._dependency_remarks(service, intent.services)],
            )

        quote_component_labels = [
            self._calculator_service_name(item.service, item.calculator_service_name)
            for item in intent.services
        ]
        priced_component_labels: list[tuple[int, str]] = []
        if reporter:
            await reporter(
                "component_plan",
                f"已建立 {len(intent.services)} 个组件报价通道",
            )
            # Create every visual channel before the first serial AWS adapter
            # call.  The user can therefore see the complete workload and the
            # exact component currently waiting, running or finished.
            for component_index, component_name in enumerate(quote_component_labels, start=1):
                await reporter(
                    "quote_component_waiting",
                    f"组件 {component_index}｜{component_name}｜等待进入官方报价队列",
                )

        for index, service in enumerate(intent.services):
            kind = self._service_kind(service.service)
            display_name = self._calculator_service_name(
                service.service, service.calculator_service_name
            )
            skip_reason = str(service.requirements.get("_quote_skip_reason") or "").strip()
            skip_category = str(service.requirements.get("_quote_skip_category") or "").strip()
            # Recoverable current and legacy failures must be attempted again
            # at final quote time. Older drafts have only a display sentence,
            # so resolve that sentence through the same category rules used by
            # the confirmation page instead of skipping the component forever.
            if skip_reason and should_retry_persisted_pricing_issue(
                reason=skip_reason,
                category=skip_category,
                code=str(service.requirements.get("_quote_skip_code") or ""),
                service=service.service,
                requirements=service.requirements,
            ):
                for field in (
                    "_quote_skip_reason",
                    "_quote_skip_code",
                    "_quote_skip_category",
                ):
                    service.requirements.pop(field, None)
                skip_reason = ""
            if skip_reason:
                notices.append(
                    f"{display_name}：{skip_reason}；本次未计入总价，其他已支持组件已正常核价。"
                )
                trace.append(
                    ExecutionEvent(
                        stage="aws",
                        message=f"已跳过暂不可报价组件：{display_name}",
                        status="warning",
                    )
                )
                if reporter:
                    await reporter(
                        "quote_done",
                        f"组件 {index + 1}｜{display_name}｜已完成处理，本次不计入总价",
                    )
                selections.append(
                    keep_unpriced_component(
                        service,
                        index,
                        display_name,
                        skip_reason,
                        str(service.requirements.get("_quote_skip_code") or "unpriced"),
                    )
                )
                continue
            if kind is None and self._generic_plugin is None:
                issue_message = "该服务尚未接入官方报价适配器"
                notices.append(
                    f"{display_name} {issue_message}，本次未计入总价；"
                    "其他已支持组件已正常核价。"
                )
                trace.append(
                    ExecutionEvent(
                        stage="aws",
                        message=f"已跳过待适配组件：{display_name}",
                        status="warning",
                    )
                )
                if reporter:
                    await reporter(
                        "quote_done",
                        f"组件 {index + 1}｜{display_name}｜已完成处理，本次不计入总价",
                    )
                selections.append(
                    keep_unpriced_component(
                        service,
                        index,
                        display_name,
                        issue_message,
                        "adapter_not_available",
                    )
                )
                continue
            service_key = kind.value if kind is not None else service.service
            normalized = self._calculator_requirements(
                service.requirements, service.quantity, service_key
            )
            confirmed_model = self._confirmed_pricing_model(
                service,
                request.selected_models.get(str(index)),
            )
            if confirmed_model:
                normalized["requested_model"] = confirmed_model
            requirement = self._pricing_requirement_copy(
                service, service_key=service_key, requirements=normalized
            )
            self._align_pricing_product_identity(service, requirement)
            if reporter:
                await reporter(
                    "aws_start",
                    f"组件 {index + 1}｜{display_name}｜正在查询 AWS 官方产品与计费维度",
                )
            plugin = self._plugins.get(kind) if kind is not None else self._generic_plugin
            assert plugin is not None
            repair_count = 0
            selection: SelectedResource | None = None
            while True:
                try:
                    selection_cache_key = self._selection_cache_key(requirement)
                    cached_selection = (
                        component_selection_cache.get(selection_cache_key)
                        if component_selection_cache is not None
                        else None
                    )
                    if cached_selection is not None:
                        selection = cached_selection.model_copy(deep=True)
                        if reporter:
                            await reporter(
                                "catalog_cache",
                                f"组件 {index + 1}｜{display_name}｜复用本次报价已核验的官方计费项",
                            )
                    else:
                        selection = await asyncio.to_thread(
                            plugin.select, requirement, "ap-southeast-1"
                        )
                        if component_selection_cache is not None:
                            component_selection_cache[selection_cache_key] = (
                                selection.model_copy(deep=True)
                            )
                    break
                except ManualConfirmationRequired as exc:
                    # A customer-confirmed/reviewed model is authoritative.
                    # If it no longer has an official billing product, do not
                    # remove it and silently substitute another model. Return
                    # this component to the customer with official choices.
                    if (
                        confirmed_model
                        and self._is_stale_model_pricing_error(exc)
                    ):
                        exc.details.setdefault("service_index", index)
                        exc.details.setdefault("component_id", str(index))
                        exc.details.setdefault("service", service_key)
                        exc.details.setdefault("display_name", display_name)
                        exc.details.setdefault("requested_model", confirmed_model)
                        late_confirmation_errors.append(exc)
                        selection = None
                        break
                    if self._is_component_isolatable_pricing_error(exc):
                        category = self._catalog_issue_category(exc, service)
                        issue_message = self._catalog_issue_message(
                            exc, service, display_name, category
                        )
                        notices.append(
                            f"{display_name}：{issue_message}；"
                            "本次暂不计入总价，其他组件已继续报价。"
                        )
                        trace.append(
                            ExecutionEvent(
                                stage="aws",
                                message=f"已隔离暂不可报价组件：{display_name}",
                                status="warning",
                            )
                        )
                        selection = keep_unpriced_component(
                            service,
                            index,
                            display_name,
                            issue_message,
                            exc.code,
                        )
                        break
                    if repair_count >= 3 or not self._is_ai_repairable_component_error(exc):
                        # One component's official catalog/BCM dimension must
                        # not cancel the other independent components.  Keep
                        # the reviewed row, omit only its unverified amount and
                        # finish the rest of the quote with an explicit note.
                        if self._is_component_isolatable_pricing_error(exc):
                            notices.append(
                                f"{display_name}：AWS 官方目录暂未返回可提交的计费项；"
                                "本次暂不计入总价，其他组件已继续报价。"
                            )
                            trace.append(
                                ExecutionEvent(
                                    stage="aws",
                                    message=f"已隔离暂不可报价组件：{display_name}",
                                    status="warning",
                                )
                            )
                            selection = None
                            break
                        exc.details.setdefault("service_index", index)
                        exc.details.setdefault("component_id", str(index))
                        exc.details.setdefault("service", service_key)
                        exc.details.setdefault("display_name", display_name)
                        if self._is_technical_catalog_error(exc):
                            raise
                        late_confirmation_errors.append(exc)
                        selection = None
                        break
                    repair_count += 1
                    repairer = getattr(self._parser, "repair_quote_component", None)
                    if not callable(repairer):
                        exc.details.setdefault("service_index", index)
                        exc.details.setdefault("component_id", str(index))
                        exc.details.setdefault("service", service_key)
                        exc.details.setdefault("display_name", display_name)
                        if self._is_technical_catalog_error(exc):
                            raise
                        late_confirmation_errors.append(exc)
                        selection = None
                        break
                    if reporter:
                        await reporter(
                            "ai_repair",
                            f"组件 {index + 1}｜{display_name}｜官方查询未通过，"
                            f"正在定向修正参数（{repair_count}/3）",
                        )
                    repaired = await repairer(
                        request.customer_request,
                        service,
                        error_code=exc.code,
                        error_message=exc.message,
                        error_details=exc.details,
                        attempt=repair_count,
                        reporter=reporter,
                    )
                    if repaired is None:
                        exc.details.setdefault("service_index", index)
                        exc.details.setdefault("component_id", str(index))
                        exc.details.setdefault("service", service_key)
                        exc.details.setdefault("display_name", display_name)
                        raise
                    preserve_service_configuration(repaired)
                    repaired = restore_customer_authority(service, repaired)
                    preserve_service_configuration(repaired)
                    service = repaired
                    normalized = self._calculator_requirements(
                        service.requirements, service.quantity, service_key
                    )
                    if confirmed_model:
                        normalized["requested_model"] = confirmed_model
                    requirement = self._pricing_requirement_copy(
                        service, service_key=service_key, requirements=normalized
                    )
                    self._align_pricing_product_identity(service, requirement)
            if selection is None:
                if reporter:
                    await reporter(
                        "quote_done",
                        f"组件 {index + 1}｜{display_name}｜官方月费暂未生成，已隔离并继续其他组件",
                    )
                continue
            if selection.pricing_status != "unpriced":
                self._require_confirmed_model_match(
                    confirmed_model,
                    selection.model,
                    component_id=str(index),
                    service=service_key,
                    display_name=display_name,
                    allow_system_substitution=False,
                )
            derived_pricing_status = selection.pricing_status
            if derived_pricing_status == "priced" and not selection.usage_lines and not (
                selection.monthly_commitment_cost or selection.upfront_commitment_cost
            ):
                derived_pricing_status = (
                    "reference_only" if selection.reference_rates else "free"
                )
            selection = selection.model_copy(
                update={
                    "component_id": str(index),
                    "component_number": hierarchy[index].component_number,
                    "parent_component_id": hierarchy[index].parent_component_id,
                    "parent_component_number": hierarchy[index].parent_component_number,
                    "parent_display_name": hierarchy[index].parent_display_name,
                    "display_name": display_name,
                    "quantity": service.quantity,
                    "pricing_status": derived_pricing_status,
                    "specifications": self._complete_selection_specifications(
                        service,
                        selection.specifications,
                    ),
                    "remarks": list(
                        dict.fromkeys(
                            [
                                *selection.remarks,
                                *(
                                    [str(service.requirements["system_default_assumption"])]
                                    if service.requirements.get("system_default_assumption")
                                    else []
                                ),
                                *self._dependency_remarks(service, intent.services),
                            ]
                        )
                    ),
                }
            )
            selections.append(selection)
            if selection.pricing_status != "unpriced":
                priced_component_labels.append((index, display_name))
            if reporter:
                await reporter(
                    "aws_match_done",
                    (
                        f"组件 {index + 1}｜{display_name}｜官方产品与计费项匹配完成"
                        if selection.pricing_status != "unpriced"
                        else f"组件 {index + 1}｜{display_name}｜已独立保留，当前未计入金额"
                    ),
                )
            if selection.substitution_notice:
                notices.append(selection.substitution_notice)
            trace.append(
                ExecutionEvent(
                    stage="aws",
                    message=(
                        f"已确认 {selection.display_name}：{selection.model}"
                        if selection.pricing_status != "unpriced"
                        else f"已保留 {selection.display_name}；当前金额未计入"
                    ),
                    status=(
                        "completed"
                        if selection.pricing_status != "unpriced"
                        else "warning"
                    ),
                )
            )
            if selection.usage_lines:
                usage_group_names[f"service-{index + 1}"] = display_name
            for line_index, line in enumerate(selection.usage_lines, start=1):
                usage_lines.append(
                    line.model_copy(
                        update={
                            "key": f"s{index + 1}l{line_index}",
                            "group": f"service-{index + 1}",
                        }
                    )
                )
            if selection.monthly_commitment_cost or selection.upfront_commitment_cost:
                direct_priced_lines.append(
                    PricedLine(
                        key=f"s{index + 1}commit",
                        service_code=service_key,
                        usage_type="AWS Price List Reserved commitment",
                        operation="Reserved",
                        amount=service.quantity,
                        unit="month",
                        cost=selection.monthly_commitment_cost,
                    )
                )
                upfront_cost += selection.upfront_commitment_cost

        if late_confirmation_errors:
            raise ManualConfirmationRequired(
                "官方核价发现多项需要客户确认的配置",
                code="batched_component_confirmation_required",
                component_errors=late_confirmation_errors,
            )

        if not selections:
            raise ManualConfirmationRequired(
                "没有可报价的 AWS 服务，系统已阻止生成空报价",
                code="empty_quote_blocked",
            )

        has_reference_rates = any(selection.reference_rates for selection in selections)
        if usage_lines:
            if reporter:
                await reporter(
                    "calculator", "正在提交已知用量到 AWS BCM Pricing Calculator 官方核价"
                )
            result = await self._quote_bcm_with_component_fallback(
                usage_lines,
                usage_group_names=usage_group_names,
                notices=notices,
                trace=trace,
                reporter=reporter,
                component_cache=bcm_component_cache,
            )
            if result.failed_groups:
                failed_component_ids = {
                    str(int(group.removeprefix("service-")) - 1)
                    for group in result.failed_groups
                    if group.startswith("service-")
                    and group.removeprefix("service-").isdigit()
                }
                selections = [
                    (
                        selection.model_copy(
                            update={
                                "pricing_status": "unpriced",
                                "pricing_issue_code": "bcm_component_failed",
                                "pricing_notice": (
                                    "AWS BCM 本次未返回该组件的完整月费，已从合计中独立排除。"
                                ),
                            }
                        )
                        if (selection.component_id or "") in failed_component_ids
                        else selection
                    )
                    for selection in selections
                ]
            if reporter:
                await reporter("result", "已读取 BCM 官方费用；未提供用量的项目仅展示单位参考价")
            trace.append(
                ExecutionEvent(
                    stage="calculator",
                    message="BCM 官方核价已完成，临时报价数据已清理",
                )
            )
            quote_id = f"quote-{uuid.uuid4().hex[:12]}"
            priced_lines = [*result.priced_lines, *direct_priced_lines]
            total_cost = result.total_cost + sum(line.cost for line in direct_priced_lines)
            currency = result.currency
            rate_type = result.rate_type
            rate_timestamp = result.rate_timestamp
            source_url = None
        elif direct_priced_lines:
            quote_id = f"reserved-{uuid.uuid4().hex[:12]}"
            priced_lines = direct_priced_lines
            total_cost = sum(line.cost for line in direct_priced_lines)
            currency = "USD"
            rate_type = "AWS_PRICE_LIST_RESERVED"
            rate_timestamp = None
            source_url = "https://aws.amazon.com/ec2/pricing/reserved-instances/pricing/"
            if reporter:
                await reporter("result", "已读取 AWS Price List 官方预留价格")
            trace.append(
                ExecutionEvent(
                    stage="calculator",
                    message="已按官方预留条款计算月均成本与预付金额",
                )
            )
        else:
            quote_id = f"reference-{uuid.uuid4().hex[:12]}"
            priced_lines = []
            total_cost = 0.0
            currency = "USD"
            rate_type = "REFERENCE_RATES_ONLY"
            rate_timestamp = None
            source_url = None
            if reporter:
                await reporter("result", "客户未提供可累计用量；已读取 AWS 官方单位参考价")
            trace.append(
                ExecutionEvent(
                    stage="calculator",
                    message="未提交虚构用量到 BCM；仅生成 AWS 官方单位参考价",
                )
            )
        if reporter:
            for component_index, component_name in priced_component_labels:
                await reporter(
                    "quote_done",
                    f"组件 {component_index + 1}｜{component_name}｜官方报价计算完成",
                )
        if upfront_cost > 0:
            notices.append(
                "预留方案的月均成本已包含预付费用按合同期摊销；预付金额单独列示，请勿再次相加。"
            )
        incomplete_component_ids = [
            selection.component_id or str(index)
            for index, selection in enumerate(selections)
            if selection.pricing_status == "unpriced"
        ]
        return QuoteResponse(
            quote_id=quote_id,
            status=QuoteStatus.QUOTED,
            customer_summary=intent.customer_summary,
            selections=selections,
            priced_lines=priced_lines,
            total_cost=total_cost,
            upfront_cost=upfront_cost,
            currency=currency,
            rate_type=rate_type,
            rate_timestamp=rate_timestamp,
            notices=list(dict.fromkeys(notices)),
            execution_trace=trace,
            pricing_source=(
                "AWS BCM Pricing Calculator API + AWS Price List unit reference rates"
                if has_reference_rates and usage_lines
                else "AWS Price List unit reference rates"
                if has_reference_rates
                else "AWS BCM Pricing Calculator API"
            ),
            source_url=source_url,
            share_url=None,
            calculator_details=[
                f"{line.service_code} · {line.usage_type} · {line.cost:.2f} {line.currency}"
                for line in priced_lines
            ],
            is_partial=bool(incomplete_component_ids),
            incomplete_component_ids=incomplete_component_ids,
        )

    async def _quote_bcm_with_component_fallback(
        self,
        usage_lines: list[UsageLine],
        *,
        usage_group_names: dict[str, str],
        notices: list[str],
        trace: list[ExecutionEvent],
        reporter: ProgressReporter | None,
        component_cache: dict[str, BcmQuoteResult] | None = None,
    ) -> BcmQuoteResult:
        """Quote every component as an independent BCM workload.

        The table is only a presentation aggregate. A component is never sent
        to BCM in the same workload as another component, so deletion,
        rejection, timeout, or a missing dimension cannot shift or invalidate
        any neighbouring price. Lines belonging to one component remain an
        atomic group: either the whole component succeeds or none of it is
        included in the monthly total.
        """

        groups: dict[str, list[UsageLine]] = {}
        for line in usage_lines:
            group = line.group or line.key
            groups.setdefault(group, []).append(line)

        # A small concurrency window keeps a twenty-component quote responsive
        # without turning one throttled AWS call into a shared batch failure.
        semaphore = asyncio.Semaphore(3)

        async def quote_component(
            group: str,
            lines: list[UsageLine],
        ) -> tuple[str, BcmQuoteResult | None, ManualConfirmationRequired | None]:
            component_name = usage_group_names.get(group, group)
            cache_key = self._bcm_component_cache_key(lines)
            cached = component_cache.get(cache_key) if component_cache is not None else None
            if cached is not None:
                if reporter:
                    await reporter(
                        "calculator_component_done",
                        f"{component_name}｜复用本次报价已完成的独立官方核价",
                    )
                return group, self._copy_bcm_result(cached), None
            async with semaphore:
                if reporter:
                    await reporter(
                        "calculator_component",
                        f"{component_name}｜正在独立提交 AWS BCM 官方核价",
                    )
                try:
                    result = await asyncio.to_thread(self._estimator.quote, lines)
                except ManualConfirmationRequired as error:
                    return group, None, error
                if component_cache is not None:
                    component_cache[cache_key] = self._copy_bcm_result(result)
                if reporter:
                    await reporter(
                        "calculator_component_done",
                        f"{component_name}｜独立官方核价完成",
                    )
                return group, result, None

        outcomes = await asyncio.gather(
            *(quote_component(group, lines) for group, lines in groups.items())
        )
        successful: list[BcmQuoteResult] = []
        failed_groups: set[str] = set()
        failures: list[ManualConfirmationRequired] = []
        for group, result, error in outcomes:
            if result is not None:
                successful.append(result)
                continue
            failed_groups.add(group)
            if error is not None:
                failures.append(error)

        if failed_groups:
            await self._record_bcm_component_fallback(
                failed_groups,
                usage_group_names=usage_group_names,
                notices=notices,
                trace=trace,
                reporter=reporter,
            )

        if not successful:
            # A global credential/configuration failure is not a component
            # dimension problem. Preserve the precise system error when every
            # independent workload fails for that reason.
            non_recoverable = next(
                (
                    error
                    for error in failures
                    if not self._is_recoverable_bcm_dimension_error(error)
                ),
                None,
            )
            if non_recoverable is not None:
                raise non_recoverable
            return BcmQuoteResult(
                priced_lines=[],
                total_cost=0.0,
                currency="USD",
                rate_type="REFERENCE_RATES_ONLY",
                rate_timestamp=None,
                estimate_id="",
                failed_groups=failed_groups,
            )

        # Some independent components may fail with a system/catalog error
        # while others succeed. Keep the successful rows; the failed rows are
        # already marked and excluded, never allowed to cancel their peers.
        return BcmQuoteResult(
            priced_lines=[line for result in successful for line in result.priced_lines],
            total_cost=sum(result.total_cost for result in successful),
            currency=successful[0].currency,
            rate_type=successful[0].rate_type,
            rate_timestamp=max(
                (
                    result.rate_timestamp
                    for result in successful
                    if result.rate_timestamp is not None
                ),
                default=None,
            ),
            estimate_id=successful[0].estimate_id,
            failed_groups=failed_groups,
        )

    @staticmethod
    def _selection_cache_key(requirement: ServiceRequirement) -> str:
        payload = json.dumps(
            requirement.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _bcm_component_cache_key(lines: list[UsageLine]) -> str:
        payload = json.dumps(
            [line.model_dump(mode="json") for line in lines],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _copy_bcm_result(result: BcmQuoteResult) -> BcmQuoteResult:
        return BcmQuoteResult(
            priced_lines=[line.model_copy(deep=True) for line in result.priced_lines],
            total_cost=result.total_cost,
            currency=result.currency,
            rate_type=result.rate_type,
            rate_timestamp=result.rate_timestamp,
            estimate_id=result.estimate_id,
            failed_groups=set(result.failed_groups),
        )

    @staticmethod
    def _is_recoverable_bcm_dimension_error(
        error: ManualConfirmationRequired,
    ) -> bool:
        return error.code in {
            "bcm_usage_rejected",
            "bcm_estimate_invalid",
            "bcm_incomplete_result",
            "bcm_incomplete_line_result",
            "too_many_usage_lines",
        }

    @staticmethod
    def _bcm_rejected_groups(
        error: ManualConfirmationRequired,
        usage_lines: list[UsageLine],
    ) -> set[str]:
        rejected_keys = {
            str(item.get("key"))
            for item in error.details.get("errors", [])
            if isinstance(item, dict) and item.get("key")
        }
        return {line.group or line.key for line in usage_lines if line.key in rejected_keys}

    @staticmethod
    async def _record_bcm_component_fallback(
        failed_groups: set[str],
        *,
        usage_group_names: dict[str, str],
        notices: list[str],
        trace: list[ExecutionEvent],
        reporter: ProgressReporter | None,
    ) -> None:
        for group in sorted(failed_groups):
            display_name = usage_group_names.get(group, "该组件")
            notices.append(
                f"{display_name} 本次未取得可累计的官方月费；"
                "已按最小计费单位展示官方参考单价，不计入月费合计。"
            )
            trace.append(
                ExecutionEvent(
                    stage="calculator",
                    message=f"{display_name} 已切换为官方单位参考价，其他组件继续核价",
                    status="warning",
                )
            )
        if reporter and failed_groups:
            await reporter(
                "calculator",
                "部分服务已切换为官方单位参考价，其余服务继续完成核价",
            )

    @staticmethod
    def _service_kind(service: str) -> ServiceKind | None:
        normalized = service.strip().lower().replace("-", "_")
        canonical = re.sub(r"[^a-z0-9]", "", normalized)
        aliases = {
            "ec2": ServiceKind.EC2,
            "rds": ServiceKind.RDS,
            "aurora": ServiceKind.RDS,
            "redis": ServiceKind.REDIS,
            "elasticache": ServiceKind.REDIS,
            "valkey": ServiceKind.REDIS,
            "s3": ServiceKind.S3,
            "amazon_s3": ServiceKind.S3,
            "elb": ServiceKind.ELB,
            "elbv2": ServiceKind.ELB,
            "alb": ServiceKind.ELB,
            "elastic_load_balancing": ServiceKind.ELB,
            "cloudfront": ServiceKind.CLOUDFRONT,
            "cloud_front": ServiceKind.CLOUDFRONT,
            "route53": ServiceKind.ROUTE53,
            "route_53": ServiceKind.ROUTE53,
            "waf": ServiceKind.WAF,
            "wafv2": ServiceKind.WAF,
            "aws_waf": ServiceKind.WAF,
            "sqs": ServiceKind.SQS,
            "ses": ServiceKind.SES,
            "cloudwatch": ServiceKind.CLOUDWATCH,
            "ebs": ServiceKind.EBS,
            "amazon_ebs": ServiceKind.EBS,
            "data_transfer": ServiceKind.DATA_TRANSFER,
            "aws_data_transfer": ServiceKind.DATA_TRANSFER,
            "global_accelerator": ServiceKind.GLOBAL_ACCELERATOR,
            "aws_global_accelerator": ServiceKind.GLOBAL_ACCELERATOR,
            "msk": ServiceKind.MSK,
            "amazon_msk": ServiceKind.MSK,
            "apigateway": ServiceKind.API_GATEWAY,
            "api_gateway": ServiceKind.API_GATEWAY,
            "scheduler": ServiceKind.SCHEDULER,
            "eventbridge_scheduler": ServiceKind.SCHEDULER,
            "opensearch": ServiceKind.OPENSEARCH,
            "amazon_opensearch": ServiceKind.OPENSEARCH,
            "nat_gateway": ServiceKind.NAT_GATEWAY,
            "aws_nat_gateway": ServiceKind.NAT_GATEWAY,
        }
        if exact := aliases.get(normalized):
            return exact
        if canonical in {"ec2", "amazonec2", "amazonelasticcomputecloud", "compute"}:
            return ServiceKind.EC2
        if (
            canonical in {"rds", "amazonrds", "aurora", "amazonaurora"}
            or "amazonrdsfor" in canonical
        ):
            return ServiceKind.RDS
        if (
            canonical in {"redis", "elasticache", "amazonelasticache", "valkey"}
            or "elasticache" in canonical
        ):
            return ServiceKind.REDIS
        if canonical in {"s3", "amazons3", "amazonsimplestorageservice", "objectstorage"} or (
            "simplestorageservice" in canonical and "s3" in canonical
        ):
            return ServiceKind.S3
        if (
            "load_balanc" in normalized
            or "loadbalanc" in canonical
            or canonical in {"alb", "elb", "applicationloadbalancer"}
            or normalized.endswith(("_alb", "_elb"))
        ):
            return ServiceKind.ELB
        if "cloudfront" in canonical:
            return ServiceKind.CLOUDFRONT
        if canonical in {"route53", "amazonroute53", "dns"}:
            return ServiceKind.ROUTE53
        if canonical in {"waf", "wafv2", "awswaf", "awswafv2"}:
            return ServiceKind.WAF
        if canonical in {"sqs", "amazonsqs", "amazonqueueservice"}:
            return ServiceKind.SQS
        if canonical in {"ses", "amazonses", "amazonsimpleemailservice"}:
            return ServiceKind.SES
        if canonical in {"cloudwatch", "amazoncloudwatch"}:
            return ServiceKind.CLOUDWATCH
        if canonical in {"ebs", "amazonebs", "elasticblockstore"}:
            return ServiceKind.EBS
        if canonical in {"datatransfer", "awsdatatransfer", "internetegress"}:
            return ServiceKind.DATA_TRANSFER
        if canonical in {"globalaccelerator", "awsglobalaccelerator"}:
            return ServiceKind.GLOBAL_ACCELERATOR
        if canonical in {"msk", "amazonmsk", "managedstreamingforkafka"}:
            return ServiceKind.MSK
        if canonical in {"apigateway", "amazonapigateway"}:
            return ServiceKind.API_GATEWAY
        if canonical in {"scheduler", "eventbridgescheduler", "amazoneventbridgescheduler"}:
            return ServiceKind.SCHEDULER
        if canonical in {"opensearch", "amazonopensearch", "amazonopensearchservice"}:
            return ServiceKind.OPENSEARCH
        if canonical in {"natgateway", "awsnatgateway"}:
            return ServiceKind.NAT_GATEWAY
        return None

    async def _create_calculator_quote(
        self,
        intent: ParsedIntent,
        request: QuoteRequest,
        reporter: ProgressReporter | None,
        default_notices: list[str] | None = None,
    ) -> QuoteResponse:
        calculator = self._require_calculator()
        quote_inputs: list[GenericCalculatorInput] = []
        non_pricing_notices: list[str] = []
        for index, service in enumerate(intent.services):
            requirements = self._calculator_requirements(
                service.requirements, service.quantity, service.service
            )
            if service.service == "ec2" and requirements.get("ebs_storage_breakdown") is not None:
                non_pricing_notices.append(str(requirements["ebs_storage_breakdown"]))
            if (
                service.service == "rds"
                and service.requirements.get("backup_retention_days") is not None
            ):
                days = service.requirements["backup_retention_days"]
                non_pricing_notices.append(
                    f"RDS 自动备份保留 {days} 天已保留为部署要求；Calculator 按实际备份"
                    "存储量（GB-month）计费，客户未提供额外备份存储量，因此未添加猜测费用"
                )
            confirmed_model = self._confirmed_pricing_model(
                service,
                request.selected_models.get(str(index)),
            )
            if confirmed_model:
                requirements["requested_model"] = confirmed_model
            quote_inputs.append(
                GenericCalculatorInput(
                    service=service.service,
                    calculator_service_name=self._calculator_service_name(
                        service.service, service.calculator_service_name
                    ),
                    region=service.region,
                    quantity=service.quantity,
                    requirements=requirements,
                    source_text=service.source_text,
                )
            )

        web_result = await calculator.quote_ai_groups(quote_inputs, reporter)
        if len(web_result.generic_groups) != len(intent.services):
            raise ManualConfirmationRequired(
                "Calculator 保存的项目数量与客户需求不一致",
                code="calculator_result_group_mismatch",
            )

        for quote_input in quote_inputs:
            adjustments = quote_input.requirements.get("calculator_adjustment_notices")
            if isinstance(adjustments, list):
                non_pricing_notices.extend(str(item) for item in adjustments if item)

        selections: list[SelectedResource] = []
        for index, (service, group, quote_input) in enumerate(
            zip(intent.services, web_result.generic_groups, quote_inputs, strict=True),
            start=1,
        ):
            requested_model = quote_input.requirements.get("requested_model")
            self._require_confirmed_model_match(
                str(requested_model) if requested_model else None,
                group.selected_model,
                component_id=str(index - 1),
                service=service.service,
                display_name=quote_input.calculator_service_name,
            )
            specifications = {
                "quantity": quote_input.quantity,
                **service.requirements,
            }
            for key in (
                "storage_iops",
                "storage_throughput_mbps",
                "requested_storage_iops",
                "requested_storage_throughput_mbps",
            ):
                if key in quote_input.requirements:
                    specifications[key] = quote_input.requirements[key]
            selections.append(
                SelectedResource(
                    service=service.service,
                    display_name=quote_input.calculator_service_name,
                    region=quote_input.region or "Calculator 默认区域",
                    model=group.selected_model,
                    architecture="已按客户需求填写并保存到 Calculator",
                    specifications=specifications,
                    official_product={"source": "AWS Pricing Calculator Web"},
                    rationale=(
                        "使用客户指定型号。"
                        if requested_model
                        else "由 Calculator 当前页面完成配置。"
                    ),
                    usage_lines=[
                        UsageLine(
                            key=f"g{index}",
                            service_code=quote_input.calculator_service_name,
                            usage_type="CalculatorPageResult",
                            operation="BrowserEstimate",
                            amount=1,
                            group=f"g{index}",
                        )
                    ],
                )
            )

        return QuoteResponse(
            quote_id=uuid.uuid4().hex[:12],
            status=QuoteStatus.QUOTED,
            customer_summary=intent.customer_summary,
            selections=selections,
            priced_lines=[
                PricedLine(
                    key="webtotal",
                    service_code="AWSCalculator",
                    usage_type="AWS-Calculator-Web-Total",
                    operation="BrowserEstimate",
                    amount=1,
                    unit="MonthlyEstimate",
                    cost=web_result.monthly_total,
                )
            ],
            total_cost=web_result.monthly_total,
            upfront_cost=web_result.upfront_total,
            currency=web_result.currency,
            rate_type="AWS_CALCULATOR_WEB",
            execution_trace=[
                ExecutionEvent(stage="calculator", message=step) for step in web_result.steps
            ],
            pricing_source="AWS Pricing Calculator Web",
            source_url=web_result.source_url,
            share_url=web_result.share_url,
            calculator_details=web_result.details,
            notices=list(dict.fromkeys((default_notices or []) + non_pricing_notices)),
        )

    @staticmethod
    def _dependency_remarks(
        service: ServiceRequirement, all_services: list[ServiceRequirement]
    ) -> list[str]:
        """Explain separately billed dependencies without inventing resources."""

        key = str(service.service).casefold()
        present = {str(item.service).casefold() for item in all_services}
        source = service.source_text or ""
        notes: list[str] = []
        calculator_name = str(service.calculator_service_name or "").casefold()
        is_eks_worker = key == "ec2" and (
            "eks worker" in calculator_name
            or "eks 工作节点" in calculator_name
            or (
                bool(re.search(r"\beks\b|kubernetes|k8s|k8s", source, re.I))
                and bool(re.search(r"worker|工作节点", source, re.I))
            )
        )
        if is_eks_worker:
            notes.append(
                "本项为 EKS 集群的工作节点计算资源，由 EC2 提供并与对应 EKS 集群配套使用。"
            )
        elif key == "eks":
            has_explicit_workers = bool(
                re.search(r"(?:工作|worker)?节点(?:数量|规格)|node\s*group", source, re.I)
            )
            if not has_explicit_workers:
                notes.append(
                    "本项仅含 EKS 集群控制平面；实际运行容器还需 EC2 或 Fargate 工作节点，客户未提供节点配置，本次未计入。"
                )
        elif key == "cloudfront":
            if "s3" in present:
                notes.append("CloudFront 源站 S3 的存储与请求费用在 S3 项中单独计费。")
            else:
                notes.append(
                    "CloudFront 需配置源站；客户未提供源站服务，本次未新增或计入源站费用。"
                )
        elif key in {"elb", "alb", "nlb"}:
            notes.append("负载均衡后端计算资源独立计费；本项不重复包含 EC2、ECS 或 Lambda 费用。")
        elif key == "waf":
            notes.append(
                "WAF 需关联 CloudFront、ALB 或 API Gateway 等受保护资源；关联资源费用独立计算。"
            )
        elif key == "apigateway":
            notes.append("API Gateway 后端计算服务独立计费；客户未明确时不自动增加 Lambda 或 EC2。")
        elif key == "route53":
            notes.append("本项不自动包含域名注册、健康检查等客户未指定的附加费用。")
        elif key == "nat_gateway":
            notes.append("NAT Gateway 后端工作负载及其公网数据传输费用独立计算，不在本项重复计费。")
        return notes

    @staticmethod
    def _complete_selection_specifications(
        service: ServiceRequirement,
        official_specifications: dict[str, object],
    ) -> dict[str, object]:
        """Keep every confirmed customer field in the final quote display.

        Plugins own AWS validation and pricing, but they must not also become
        the only source for presentation fields. A plugin may expose a compact
        official shape and accidentally omit storage, traffic, node counts, or
        another confirmed value. Build one complete display object here for
        every service, then let authoritative AWS values override aliases such
        as CPU and memory.
        """

        aliases = {
            "vcpu": "vCPU",
            "memory_gib": "memoryGiB",
            "operating_system": "operatingSystem",
            "engine": "engine",
            "deployment": "deploymentOption",
            "storage_gib": "storageGiB",
            "storage_type": "storageType",
            "data_nodes": "dataNodes",
            "storage_gib_per_node": "storageGiBPerNode",
            "broker_count": "brokerCount",
            "storage_gib_per_broker": "storageGiBPerBroker",
            "storage_class": "storageClass",
            "data_transfer_out_gib": "dataTransferOutGiB",
            "messages": "messages",
            "connection_minutes": "connectionMinutes",
            "throughput_mbps_per_tib": "throughputMbpsPerTiB",
            "processed_bytes_gib": "processedBytesGiB",
            "system_disk_gib": "systemDiskGiB",
            "volume_type": "volumeType",
            "web_acls": "webACLs",
        }
        presentation_only_exclusions = {
            "requested_model",
            "system_default_assumption",
            "calculator_adjustment_notices",
            "ebs_storage_breakdown",
            # Pricing controls belong to the scenario columns, not the
            # customer-facing configuration cell.
            "purchase_option",
            "reserved_term_years",
            "payment_option",
            "utilization_percent",
            "tenancy",
            # This is derived from per-machine disk × quantity.  Showing both
            # values made the configuration look duplicated.
            "total_system_disk_gib",
        }
        complete: dict[str, object] = {}

        def add_visible(key: str, value: object) -> None:
            if (
                key.startswith("_")
                or key in presentation_only_exclusions
                or value is None
                or value is False
            ):
                return
            visible_key = aliases.get(key, key)
            if visible_key in presentation_only_exclusions:
                return
            complete[visible_key] = value

        for key, value in service.requirements.items():
            add_visible(key, value)
        # Generic official adapters may return the original requirement map.
        # Run it through the same filter instead of blindly reintroducing
        # internal, derived and false-valued fields after they were removed.
        for key, value in official_specifications.items():
            if not isinstance(key, str):
                continue
            add_visible(key, value)
        return complete

    @staticmethod
    def _calculator_requirements(
        requirements: dict[str, object], quantity: int, service: str | None = None
    ) -> dict[str, object]:
        """Convert per-resource transfer values to Calculator workload totals."""

        normalized = canonicalize_requirement_fields(requirements, service=service)
        # Review and workflow metadata is never a pricing input.  The reviewed
        # model is promoted explicitly by ``_confirmed_pricing_model`` before
        # the adapter is called; all other private fields stay internal.
        normalized = {
            key: value
            for key, value in normalized.items()
            if not key.startswith("_") or key.startswith("_billing_variant_")
        }
        if service == "ec2":
            volumes = normalized.get("additional_ebs_volumes")
            system_disk = normalized.get("system_disk_gib")
            system_type = str(normalized.get("volume_type") or "gp3").lower()
            if (
                isinstance(system_disk, (int, float))
                and not isinstance(system_disk, bool)
                and isinstance(volumes, list)
                and volumes
            ):
                compatible = all(
                    isinstance(volume, dict)
                    and isinstance(volume.get("size_gib"), (int, float))
                    and not isinstance(volume.get("size_gib"), bool)
                    and str(volume.get("volume_type") or system_type).lower() == system_type
                    for volume in volumes
                )
                if compatible:
                    extra_total = sum(
                        float(volume["size_gib"]) * int(volume.get("count_per_instance") or 1)
                        for volume in volumes
                    )
                    total = float(system_disk) + extra_total
                    normalized["system_disk_gib"] = total
                    normalized.pop("additional_ebs_volumes", None)
                    normalized["ebs_storage_breakdown"] = (
                        "EC2 Calculator 按每实例 EBS 总容量计费；本次每台按 "
                        f"{total:g} GiB {system_type} 填写，其中系统盘 "
                        f"{float(system_disk):g} GiB，额外数据盘合计 {extra_total:g} GiB"
                    )
        if service == "rds":
            # Retention is an RDS deployment policy. Calculator prices backup
            # storage in GB-month and does not expose retention days as a cost input.
            normalized.pop("backup_retention_days", None)
        for total_key, per_instance_key in (
            ("data_transfer_in_gib", "data_transfer_in_gib_per_instance"),
            ("data_transfer_regional_gib", "data_transfer_regional_gib_per_instance"),
            ("data_transfer_out_gib", "data_transfer_out_gib_per_instance"),
        ):
            per_instance = normalized.pop(per_instance_key, None)
            if total_key in normalized or per_instance is None:
                continue
            if isinstance(per_instance, (int, float)) and not isinstance(per_instance, bool):
                normalized[total_key] = float(per_instance) * quantity
        return normalized

    @staticmethod
    def _pricing_requirement_copy(
        service: ServiceRequirement,
        *,
        service_key: str,
        requirements: dict[str, object],
    ) -> ServiceRequirement:
        """Build an adapter-only copy without exposing customer configuration.

        Every plugin receives a deep copy. It may translate customer fields to
        AWS catalog dimensions, but it cannot mutate the reviewed component,
        its evidence, or its locked values.
        """

        pricing_copy = service.model_copy(deep=True)
        pricing_copy.service = service_key
        pricing_copy.requirements = dict(requirements)
        return pricing_copy

    @staticmethod
    def _align_pricing_product_identity(
        reviewed: ServiceRequirement,
        pricing_copy: ServiceRequirement,
    ) -> None:
        """Force an adapter-family conversion to keep the reviewed product.

        Sharing an implementation is allowed; sharing or changing a product
        identity is not.  The reviewed customer choice is authoritative, so a
        stale or incorrectly translated adapter field is repaired on the
        adapter-only copy instead of being exposed as a customer question.
        This runs immediately before every preview and final pricing call,
        including automatic repair retries.
        """

        identity = str(reviewed.product_identity or "").strip().casefold()
        if not identity:
            return
        expected_fields: dict[str, tuple[str, str]] = {
            "elasticache_redis": ("engine", "redis"),
            "elasticache_valkey": ("engine", "valkey"),
            "elasticache_memcached": ("engine", "memcached"),
            "application_load_balancer": ("load_balancer_type", "application"),
            "network_load_balancer": ("load_balancer_type", "network"),
            "gateway_load_balancer": ("load_balancer_type", "gateway"),
            "amazon_mq_rabbitmq": ("engine_type", "rabbitmq"),
            "amazon_mq_activemq": ("engine_type", "activemq"),
            "api_gateway_http": ("api_type", "http"),
            "api_gateway_rest": ("api_type", "rest"),
            "api_gateway_websocket": ("api_type", "websocket"),
            "amazon_msk_serverless": ("cluster_type", "serverless"),
            "amazon_msk_provisioned": ("cluster_type", "provisioned"),
            "amazon_fsx_windows": ("file_system_type", "windows"),
            "amazon_fsx_lustre": ("file_system_type", "lustre"),
            "amazon_fsx_ontap": ("file_system_type", "ontap"),
            "amazon_fsx_openzfs": ("file_system_type", "openzfs"),
            "aurora_mysql": ("engine", "aurora_mysql"),
            "aurora_postgresql": ("engine", "aurora_postgresql"),
        }
        if identity.startswith("rds_"):
            expected_fields[identity] = ("engine", identity.removeprefix("rds_"))
        expected = expected_fields.get(identity)
        if expected is None:
            return
        field, expected_value = expected

        def normalized(value: object) -> str:
            return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())

        actual_value = pricing_copy.requirements.get(field)
        if normalized(actual_value) == normalized(expected_value):
            return
        logger.warning(
            "Correcting adapter product identity field: identity=%s field=%s actual=%r expected=%r",
            identity,
            field,
            actual_value,
            expected_value,
        )
        pricing_copy.requirements[field] = expected_value

    @staticmethod
    def _confirmed_pricing_model(
        service: ServiceRequirement,
        request_selected_model: str | None = None,
    ) -> str | None:
        """Return the model that the pricing stage is required to preserve.

        A model explicitly supplied with the quote request wins.  Otherwise,
        the exact model shown during the final configuration review is the
        authoritative pricing input.  Only drafts created before review-model
        persistence fall back to the original structured requirement.
        """

        requested_model = service.requirements.get("requested_model")
        customer_confirmed_model = (
            requested_model
            if service.field_sources.get("requirements.requested_model") == "customer_confirmation"
            else None
        )
        for value in (
            request_selected_model,
            customer_confirmed_model,
            service.requirements.get("_review_selected_model"),
            requested_model,
        ):
            model = str(value or "").strip()
            if model:
                return model
        return None

    @staticmethod
    def _is_stale_model_pricing_error(error: ManualConfirmationRequired) -> bool:
        code = error.code.casefold()
        return "billing_product_not_found" in code or "pricing_candidates_not_found" in code

    @staticmethod
    def _is_component_isolatable_pricing_error(
        error: ManualConfirmationRequired,
    ) -> bool:
        code = error.code.casefold()
        return any(
            marker in code
            for marker in (
                "billing_product_not_found",
                "pricing_candidates_not_found",
                "generic_semantic_rate_not_found",
                "generic_unit_rate_not_found",
                "generic_service_code_not_found",
                "service_region_not_supported",
                "reference_unit_rate_not_found",
                "auto_discovery_",
            )
        )

    @staticmethod
    def _requirement_without_stale_model(
        requirement: ServiceRequirement,
        original: ServiceRequirement,
    ) -> ServiceRequirement:
        """Rebuild one pricing request after its cached model became invalid."""

        requirements = dict(requirement.requirements)
        for field in (
            "requested_model",
            "_review_selected_model",
            "_review_selected_specifications",
        ):
            requirements.pop(field, None)
        specifications = original.requirements.get("_review_selected_specifications")
        if isinstance(specifications, dict):
            for official_field, requirement_field in {
                "vCPU": "vcpu",
                "memoryGiB": "memory_gib",
                "storageGiB": "storage_gib",
                "storageGiBPerNode": "storage_gib_per_node",
                "storageGiBPerBroker": "storage_gib_per_broker",
            }.items():
                value = specifications.get(official_field)
                if value not in (None, ""):
                    requirements.setdefault(requirement_field, value)
        return requirement.model_copy(update={"requirements": requirements})

    @classmethod
    def _require_confirmed_model_match(
        cls,
        confirmed_model: str | None,
        priced_model: str,
        *,
        component_id: str,
        service: str,
        display_name: str,
        allow_system_substitution: bool = False,
    ) -> None:
        """Block any silent model substitution after configuration approval."""

        if (
            not confirmed_model
            or cls._models_equivalent(confirmed_model, priced_model)
            or allow_system_substitution
        ):
            return
        raise ManualConfirmationRequired(
            f"{display_name} 的正式计价型号与已确认型号不一致，系统已停止生成报价",
            code="confirmed_model_mismatch",
            component_id=component_id,
            service=service,
            display_name=display_name,
            confirmed_model=confirmed_model,
            priced_model=priced_model,
        )

    @staticmethod
    def _models_equivalent(left: str, right: str) -> bool:
        def normalized(value: str) -> str:
            model = re.sub(r"\s+", "", value).casefold()
            # MSK customer-facing model names may include the AWS catalog's
            # ``kafka.`` prefix while the adapter displays the instance class.
            return model.removeprefix("kafka.")

        return normalized(left) == normalized(right)

    def _require_calculator(self) -> AwsCalculatorWebAutomator:
        if self._calculator is None:
            raise ManualConfirmationRequired(
                "AWS Pricing Calculator 浏览器服务尚未启动",
                code="calculator_unavailable",
            )
        return self._calculator

    @staticmethod
    def _calculator_service_name(service: str, explicit_name: str | None) -> str:
        if explicit_name:
            return explicit_name
        return {
            "ec2": "Amazon EC2",
            "rds": "Amazon RDS",
            "redis": "Amazon ElastiCache",
            "elasticache": "Amazon ElastiCache",
            "route53": "Amazon Route 53",
            "waf": "AWS WAF",
            "ebs": "Amazon Elastic Block Store (EBS)",
            "data_transfer": "AWS Data Transfer",
            "global_accelerator": "AWS Global Accelerator",
            "sqs": "Amazon SQS",
            "ses": "Amazon SES",
            "cloudwatch": "Amazon CloudWatch",
            "msk": "Amazon MSK",
            "apigateway": "Amazon API Gateway",
            "scheduler": "Amazon EventBridge Scheduler",
            "opensearch": "Amazon OpenSearch Service",
            "nat_gateway": "AWS NAT Gateway",
        }.get(service, service.replace("_", " ").strip())
