from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from app.domain.component_hierarchy import component_hierarchy
from app.domain.component_integrity import ensure_component_keys
from app.domain.models import (
    ConfigurationReviewItem,
    ConfirmationItem,
    ConfirmationSessionResponse,
    ParsedIntent,
    QuoteRequest,
)
from app.domain.pricing_issues import (
    PricingIssueCategory,
    classify_persisted_pricing_issue,
    legacy_pricing_issue_message,
    should_retry_persisted_pricing_issue,
)
from app.integrations.service_templates import billing_dimension_fields
from app.services.confirmation_presentation import customer_confirmation_item

CONFIGURATION_FEEDBACK_QUESTION = "【客户对最终配置表的修改意见】"
CONFIGURATION_COMPONENT_FEEDBACK_PREFIX = "【组件修改】"
CONFIGURATION_COMPONENT_UPDATE_PREFIX = "【组件字段修改】"
CONFIGURATION_COMPONENT_DELETE = "__DELETE_COMPONENT__"
PROCESSOR_ARCHITECTURE_ANSWER_KEY = "__processor_architecture__"
COMPONENT_PROCESSOR_ARCHITECTURE_PREFIX = "__component_processor_architecture__:"
# A large, fully independent component set can legitimately need 2-5 minutes.
# The job runner restores the table immediately on a known failure, so this is
# only a crash-recovery ceiling and must never interrupt a healthy quote.
CONFIGURATION_REPROCESSING_STALE_SECONDS = 8 * 60
# A live worker renews this value whenever the component pipeline reports
# progress.  A shorter lease lets another worker resume after an abrupt crash;
# it is intentionally longer than one AI/catalog attempt.
CONFIGURATION_PROCESSING_LEASE_SECONDS = 2 * 60


class ConfirmationSessionStore:
    """Persistent customer-confirmation forms tied to structured quote drafts."""

    def __init__(
        self,
        database_path: Path,
        cloud_provider: Literal["aws", "azure"] = "aws",
    ):
        self._database_path = database_path
        self.cloud_provider = cloud_provider
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _parse_persisted_intent(value: str) -> ParsedIntent:
        """Load old drafts after normalizing internal identity slugs.

        Customer-visible product names may contain capitals and spaces, but
        ``product_identity`` is an internal stable key.  Normalizing at the
        persistence boundary keeps links created by an older release usable.
        """

        payload = json.loads(value)
        services = payload.get("services") if isinstance(payload, dict) else None
        if isinstance(services, list):
            for service in services:
                if not isinstance(service, dict):
                    continue
                identity = service.get("product_identity")
                if not isinstance(identity, str) or not identity.strip():
                    continue
                normalized = re.sub(
                    r"[^a-z0-9_-]+", "_", identity.strip().casefold()
                ).strip("_-")
                service["product_identity"] = normalized or None
        return ParsedIntent.model_validate(payload)

    @staticmethod
    def _review_requirements(item: ServiceRequirement) -> dict[str, object]:
        """Return customer-visible facts without a legacy model/shape hybrid."""

        requirements = {
            key: value
            for key, value in item.requirements.items()
            if not key.startswith("_") and key != "system_default_assumption"
        }
        if (
            item.field_sources.get("_customer_shape_replaced_by_model")
            and requirements.get("requested_model")
            and not isinstance(
                item.requirements.get("_review_selected_specifications"), dict
            )
        ):
            # Older links may contain the replacement model together with the
            # rejected CPU/memory sentence. Until a fresh official lookup has
            # populated the selected specifications, omit those superseded
            # values instead of displaying an impossible combination.
            requirements.pop("vcpu", None)
            requirements.pop("memory_gib", None)
        return requirements

    @staticmethod
    def _pricing_issue_category(item: object) -> PricingIssueCategory | None:
        requirements = getattr(item, "requirements", {})
        reason = str(requirements.get("_quote_skip_reason") or "")
        if not reason:
            return None
        stored = str(requirements.get("_quote_skip_category") or "")
        if stored in {
            "retryable",
            "compatibility",
            "catalog_mapping",
            "system_configuration",
            "unsupported",
        }:
            return cast(PricingIssueCategory, stored)
        return classify_persisted_pricing_issue(
            reason=reason,
            code=str(requirements.get("_quote_skip_code") or ""),
            service=str(getattr(item, "service", "")),
            requirements=requirements,
        )

    @classmethod
    def _pricing_notice(cls, item: object) -> str | None:
        requirements = getattr(item, "requirements", {})
        reason = str(requirements.get("_quote_skip_reason") or "")
        if not reason:
            field_sources = getattr(item, "field_sources", {})
            if (
                "_official_calculator_status" in field_sources
                and field_sources.get("_semantic_fact_mapping") != "ai_cleaning"
            ):
                return "该组件尚未完成 AWS 官方字段核验，不能进入报价。"
            return None
        category = cls._pricing_issue_category(item)
        retryable = category is not None and should_retry_persisted_pricing_issue(
            reason=reason,
            category=category,
            code=str(requirements.get("_quote_skip_code") or ""),
            service=str(getattr(item, "service", "")),
            requirements=requirements,
        )
        # Catalog refreshes, transient lookups and system configuration are
        # sales-side operational details. They must never leak into a customer
        # confirmation page. A new link is publication-gated before reaching
        # this point; returning None also protects already-issued legacy links.
        if retryable or category in {
            "catalog_mapping",
            "system_configuration",
            "unsupported",
        }:
            return None
        if requirements.get("_quote_skip_category") or requirements.get(
            "_quote_skip_code"
        ):
            return reason
        if category is None:
            return reason
        return legacy_pricing_issue_message(
            reason=reason,
            category=category,
            service=str(getattr(item, "service", "")),
            display_name=str(
                getattr(item, "calculator_service_name", None)
                or getattr(item, "service", "AWS 服务")
            ),
            requirements=requirements,
        )

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS confirmation_sessions (
                    token TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL UNIQUE,
                    customer_request TEXT NOT NULL,
                    customer_summary TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    confirmation_text TEXT NOT NULL,
                    items_json TEXT NOT NULL,
                    answers_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    submitted_at TEXT,
                    confirmation_round INTEGER NOT NULL DEFAULT 0,
                    asked_questions_json TEXT NOT NULL DEFAULT '[]',
                    request_json TEXT NOT NULL DEFAULT '{}'
                    , processing_owner TEXT
                    , processing_heartbeat_at TEXT
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(confirmation_sessions)"
                ).fetchall()
            }
            if "confirmation_round" not in columns:
                connection.execute(
                    "ALTER TABLE confirmation_sessions "
                    "ADD COLUMN confirmation_round INTEGER NOT NULL DEFAULT 0"
                )
            if "asked_questions_json" not in columns:
                connection.execute(
                    "ALTER TABLE confirmation_sessions "
                    "ADD COLUMN asked_questions_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "request_json" not in columns:
                connection.execute(
                    "ALTER TABLE confirmation_sessions "
                    "ADD COLUMN request_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "processing_owner" not in columns:
                connection.execute(
                    "ALTER TABLE confirmation_sessions ADD COLUMN processing_owner TEXT"
                )
            if "processing_heartbeat_at" not in columns:
                connection.execute(
                    "ALTER TABLE confirmation_sessions ADD COLUMN processing_heartbeat_at TEXT"
                )

    def create_or_replace(
        self,
        *,
        draft_id: str,
        customer_request: str,
        customer_summary: str,
        intent: ParsedIntent,
        confirmation_text: str,
        items: list[ConfirmationItem],
        quote_request: QuoteRequest | None = None,
    ) -> str:
        ensure_component_keys(intent)
        if self.cloud_provider == "aws":
            # Persist the same finalized fact ledger that restore/submit will
            # later consume.  Saving a pre-ledger object and upgrading only on
            # read made an otherwise lossless round trip change meaning and
            # reopened prose interpretation after a customer link was created.
            from app.integrations.deepseek import DeepSeekIntentParser

            DeepSeekIntentParser.reconcile_customer_pricing_facts(intent)
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT token, asked_questions_json, answers_json FROM confirmation_sessions "
                "WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            token = (
                str(existing["token"])
                if existing
                else f"{self.cloud_provider}_{secrets.token_urlsafe(18)}"
            )
            asked_questions = (
                json.loads(str(existing["asked_questions_json"]))
                if existing and existing["asked_questions_json"]
                else []
            )
            asked_questions = list(
                dict.fromkeys(
                    [
                        *(str(question) for question in asked_questions),
                        *(item.question for item in items),
                    ]
                )
            )
            preserved_answers: dict[str, str] = {}
            if existing and existing["answers_json"]:
                try:
                    previous_answers = json.loads(str(existing["answers_json"]))
                except json.JSONDecodeError:
                    previous_answers = {}
                architecture = (
                    previous_answers.get(PROCESSOR_ARCHITECTURE_ANSWER_KEY)
                    if isinstance(previous_answers, dict)
                    else None
                )
                if architecture in {"arm64", "x86_64"}:
                    preserved_answers[PROCESSOR_ARCHITECTURE_ANSWER_KEY] = architecture
                current_answer_keys = {
                    str(item.answer_key or item.question)
                    for item in items
                }
                if isinstance(previous_answers, dict):
                    for key, value in previous_answers.items():
                        if not str(key).startswith(
                            COMPONENT_PROCESSOR_ARCHITECTURE_PREFIX
                        ):
                            continue
                        answer_key = str(key).removeprefix(
                            COMPONENT_PROCESSOR_ARCHITECTURE_PREFIX
                        )
                        if (
                            answer_key in current_answer_keys
                            and value in {"arm64", "x86_64"}
                        ):
                            preserved_answers[str(key)] = str(value)
            preserved_answers_json = json.dumps(preserved_answers, ensure_ascii=False)
            connection.execute(
                """
                INSERT INTO confirmation_sessions (
                    token, draft_id, customer_request, customer_summary, intent_json,
                    confirmation_text, items_json, answers_json, status, created_at,
                    submitted_at, asked_questions_json
                    , request_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL, ?, ?)
                ON CONFLICT(draft_id) DO UPDATE SET
                    customer_request=excluded.customer_request,
                    customer_summary=excluded.customer_summary,
                    intent_json=excluded.intent_json,
                    confirmation_text=excluded.confirmation_text,
                    items_json=CASE
                        WHEN excluded.items_json = '[]'
                        THEN confirmation_sessions.items_json
                        ELSE excluded.items_json
                    END,
                    answers_json=CASE
                        WHEN excluded.items_json = '[]'
                        THEN confirmation_sessions.answers_json
                        ELSE excluded.answers_json
                    END,
                    status='pending', submitted_at=NULL,
                    processing_owner=NULL, processing_heartbeat_at=NULL,
                    asked_questions_json=excluded.asked_questions_json,
                    request_json=CASE
                        WHEN excluded.request_json = '{}' THEN confirmation_sessions.request_json
                        ELSE excluded.request_json
                    END
                """,
                (
                    token,
                    draft_id,
                    customer_request,
                    customer_summary,
                    intent.model_dump_json(),
                    confirmation_text,
                    json.dumps(
                        [item.model_dump(mode="json") for item in items],
                        ensure_ascii=False,
                    ),
                    preserved_answers_json,
                    now,
                    json.dumps(asked_questions, ensure_ascii=False),
                    quote_request.model_dump_json() if quote_request is not None else "{}",
                ),
            )
        return token

    def get(self, token: str) -> ConfirmationSessionResponse | None:
        row = self._row(token)
        if row is None:
            return None
        # A dead worker must not leave the browser on an endless spinner.  Live
        # workers renew their lease through progress events; an expired lease
        # is returned to ``reviewing`` so the API can atomically claim and
        # resume the same saved answers on another worker.
        if str(row["status"]) == "processing":
            lease_value = row["processing_heartbeat_at"] or row["submitted_at"]
            lease_time = (
                datetime.fromisoformat(str(lease_value)) if lease_value else None
            )
            if lease_time is None or (
                datetime.now(UTC) - lease_time
            ).total_seconds() > CONFIGURATION_PROCESSING_LEASE_SECONDS:
                with self._lock, self._connect() as connection:
                    connection.execute(
                        """
                        UPDATE confirmation_sessions
                        SET status = 'reviewing', processing_owner = NULL,
                            processing_heartbeat_at = NULL
                        WHERE token = ? AND status = 'processing'
                          AND COALESCE(processing_heartbeat_at, submitted_at, '') = ?
                        """,
                        (token, str(lease_value or "")),
                    )
                row = self._row(token)
                if row is None:
                    return None
        # The older review/submission boundary has no active worker lease. If
        # it was abandoned, restore the editable configuration table.
        if str(row["status"]) in {"reviewing", "submitted"} and row["submitted_at"]:
            submitted_at = datetime.fromisoformat(str(row["submitted_at"]))
            if (
                datetime.now(UTC) - submitted_at
            ).total_seconds() > CONFIGURATION_REPROCESSING_STALE_SECONDS:
                with self._lock, self._connect() as connection:
                    connection.execute(
                        """
                        UPDATE confirmation_sessions
                        SET status = 'configuration_review',
                            confirmation_text = ?, processing_owner = NULL,
                            processing_heartbeat_at = NULL
                        WHERE token = ? AND status IN ('reviewing', 'submitted')
                        """,
                        ("这次修改没有完成，原配置已保留，请重新提交。", token),
                    )
                row = self._row(token)
                if row is None:
                    return None
        intent = self._parse_persisted_intent(str(row["intent_json"]))
        # Reading a link is not another extraction pass. Old drafts are
        # upgraded transactionally by official intake when submitted, never
        # by source-scanning or changing component indexes on a GET request.
        hierarchy = component_hierarchy(intent.services)
        session_status = str(row["status"])
        stable_review_ids = session_status == "configuration_review"
        review_parent_ids = {
            str(index): item.component_key
            for index, item in enumerate(intent.services)
            if item.component_key
        }
        configuration_items = [
            ConfigurationReviewItem(
                component_id=(
                    item.component_key
                    if stable_review_ids and item.component_key
                    else str(index)
                ),
                component_number=hierarchy[index].component_number,
                parent_component_id=(
                    review_parent_ids.get(hierarchy[index].parent_component_id or "")
                    if stable_review_ids
                    else hierarchy[index].parent_component_id
                ),
                parent_component_number=hierarchy[index].parent_component_number,
                parent_display_name=hierarchy[index].parent_display_name,
                service=item.service,
                display_name=item.calculator_service_name or item.service,
                region=item.region,
                quantity=item.quantity,
                selected_model=(
                    str(item.requirements.get("_review_selected_model"))
                    if item.requirements.get("_review_selected_model")
                    else None
                ),
                official_specifications=(
                    dict(item.requirements.get("_review_selected_specifications"))
                    if isinstance(
                        item.requirements.get("_review_selected_specifications"), dict
                    )
                    else {}
                ),
                available_shapes=(
                    [
                        {
                            "vcpu": float(shape["vcpu"]),
                            "memory_gib": float(shape["memory_gib"]),
                        }
                        for shape in item.requirements.get(
                            "_review_available_shapes", []
                        )
                        if isinstance(shape, dict)
                        and isinstance(shape.get("vcpu"), (int, float))
                        and isinstance(shape.get("memory_gib"), (int, float))
                    ]
                    if isinstance(
                        item.requirements.get("_review_available_shapes"), list
                    )
                    else []
                ),
                available_options=(
                    {
                        str(field): list(values)
                        for field, values in item.requirements.get(
                            "_review_field_options", {}
                        ).items()
                        if isinstance(values, list)
                    }
                    if isinstance(
                        item.requirements.get("_review_field_options"), dict
                    )
                    else {}
                ),
                available_billing_fields=list(
                    dict.fromkeys(
                        [
                            *billing_dimension_fields(item.service),
                            *(
                                str(field)
                                for field in item.requirements.get(
                                    "_review_billing_fields", []
                                )
                                if isinstance(field, str) and field
                            ),
                        ]
                    )
                ),
                available_billing_labels=(
                    {
                        str(field): str(label)
                        for field, label in item.requirements.get(
                            "_review_billing_labels", {}
                        ).items()
                        if isinstance(field, str)
                        and field
                        and isinstance(label, str)
                        and label
                    }
                    if isinstance(
                        item.requirements.get("_review_billing_labels"), dict
                    )
                    else {}
                ),
                pricing_status=(
                    "unpriced"
                    if item.requirements.get("_quote_skip_reason")
                    or (
                        "_official_calculator_status" in item.field_sources
                        and item.field_sources.get("_semantic_fact_mapping")
                        != "ai_cleaning"
                    )
                    else "ready"
                ),
                pricing_notice=(
                    self._pricing_notice(item)
                ),
                pricing_issue_code=(
                    str(item.requirements.get("_quote_skip_code"))
                    if item.requirements.get("_quote_skip_code")
                    else None
                ),
                pricing_issue_category=(
                    self._pricing_issue_category(item)
                ),
                requirements=self._review_requirements(item),
                source_text=item.original_source_text or item.source_text,
            )
            for index, item in enumerate(intent.services)
        ]
        confirmation_items = self._deduplicate_confirmation_items(
            [
                ConfirmationItem.model_validate(item)
                for item in json.loads(str(row["items_json"]))
            ]
        )
        if self.cloud_provider == "aws":
            confirmation_items = [customer_confirmation_item(item) for item in confirmation_items]
        return ConfirmationSessionResponse(
            token=str(row["token"]),
            cloud_provider=self.cloud_provider,
            status=str(row["status"]),
            customer_summary=(
                self._configuration_summary(intent)
                if str(row["status"]) == "configuration_review"
                else str(row["customer_summary"])
            ),
            confirmation_text=str(row["confirmation_text"]),
            confirmation_items=confirmation_items,
            answers=json.loads(str(row["answers_json"])),
            configuration_items=configuration_items,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            submitted_at=(
                datetime.fromisoformat(str(row["submitted_at"]))
                if row["submitted_at"]
                else None
            ),
        )

    def replace_pending_confirmation_items(
        self,
        token: str,
        items: list[ConfirmationItem],
    ) -> None:
        """Persist hydrated choices for an older, still unanswered link."""

        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE confirmation_sessions
                SET items_json = ?
                WHERE token = ? AND status = 'pending'
                """,
                (
                    json.dumps(
                        [item.model_dump(mode="json") for item in items],
                        ensure_ascii=False,
                    ),
                    token,
                ),
            )

    @staticmethod
    def _option_processor_architecture(option: dict[str, object]) -> str | None:
        specifications = option.get("specifications")
        specs = specifications if isinstance(specifications, dict) else {}
        declared = specs.get("processorArchitecture") or specs.get(
            "processor_architecture"
        )
        if isinstance(declared, str):
            normalized = declared.strip().casefold()
            if normalized in {"arm", "arm64", "aarch64", "graviton"}:
                return "arm64"
            if normalized in {"x86", "x86_64", "amd64", "i386"}:
                return "x86_64"
        model = str(option.get("model") or "").strip().casefold()
        if not model or "." not in model:
            return None
        segments = [segment for segment in model.split(".") if segment]
        if any(
            segment == "a1"
            or segment == "mac2"
            or segment.startswith("mac2-")
            or re.search(r"\d+g(?:[a-z]*)$", segment)
            for segment in segments
        ):
            return "arm64"
        return "x86_64"

    @classmethod
    def _validate_processor_architecture(
        cls,
        items: list[dict[str, object]],
        answers: dict[str, str],
        architecture: str,
        component_architectures: dict[str, str] | None = None,
    ) -> None:
        """Reject an unscoped mixed model answer before it mutates the draft."""

        component_architectures = component_architectures or {}
        valid_answer_keys = {
            str(item.get("answer_key") or item.get("question") or "")
            for item in items
        }
        unknown_keys = set(component_architectures).difference(valid_answer_keys)
        if unknown_keys:
            raise ValueError("单组件处理器架构绑定到了不存在的确认项")

        for item in items:
            question = str(item.get("question") or "")
            answer_key = str(item.get("answer_key") or question)
            effective_architecture = component_architectures.get(
                answer_key, architecture
            )
            answer = answers.get(answer_key)
            if answer is None:
                answer = answers.get(question)
            if not answer:
                continue
            options = [
                option
                for option in [
                    *(item.get("options") or []),
                    *(item.get("dependent_options") or []),
                ]
                if isinstance(option, dict) and option.get("model")
            ]
            selected_segments = {part.strip() for part in str(answer).split("；")}
            selected = next(
                (
                    option
                    for option in options
                    if str(option.get("value") or "").strip() in selected_segments
                ),
                None,
            )
            if selected is None:
                continue
            selected_architecture = cls._option_processor_architecture(selected)
            if (
                selected_architecture
                and selected_architecture != effective_architecture
            ):
                if answer_key in component_architectures:
                    raise ValueError(
                        "所选型号与该组件指定的处理器架构不一致，请重新选择"
                    )
                raise ValueError(
                    "所选型号与整份报价的处理器架构不一致，请重新选择同一架构的型号"
                )

    def submit(
        self,
        token: str,
        answers: dict[str, str],
        *,
        processor_architecture: str | None = None,
        component_processor_architectures: dict[str, str] | None = None,
    ) -> ConfirmationSessionResponse | None:
        row = self._row(token)
        if row is None:
            return None
        items = [
            item
            for item in json.loads(str(row["items_json"]))
            if isinstance(item, dict) and item.get("question")
        ]
        visible_question_counts: dict[str, int] = {}
        for item in items:
            question = str(item["question"])
            visible_question_counts[question] = visible_question_counts.get(question, 0) + 1
        # New pages submit the opaque answer_key. Continue accepting the old
        # question-text key when that visible question is unique. Duplicate
        # visible questions must use their per-component key to avoid answers
        # leaking from one component into another.
        cleaned: dict[str, str] = {}
        missing = 0
        for item in items:
            question = str(item["question"])
            answer_key = str(item.get("answer_key") or question)
            raw_answer = answers.get(answer_key)
            if raw_answer is None and visible_question_counts.get(question) == 1:
                raw_answer = answers.get(question)
            answer = str(raw_answer or "").strip()
            if not answer:
                missing += 1
                continue
            cleaned[answer_key] = answer
        if missing:
            raise ValueError(f"尚有 {missing} 项未填写")
        component_processor_architectures = {
            str(answer_key): str(architecture)
            for answer_key, architecture in (
                component_processor_architectures or {}
            ).items()
            if architecture in {"arm64", "x86_64"}
        }
        if processor_architecture in {"arm64", "x86_64"}:
            self._validate_processor_architecture(
                items,
                cleaned,
                processor_architecture,
                component_processor_architectures,
            )
            cleaned[PROCESSOR_ARCHITECTURE_ANSWER_KEY] = processor_architecture
            for answer_key, architecture in component_processor_architectures.items():
                cleaned[
                    f"{COMPONENT_PROCESSOR_ARCHITECTURE_PREFIX}{answer_key}"
                ] = architecture
        submitted_at = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE confirmation_sessions
                SET answers_json = ?, status = 'reviewing', submitted_at = ?,
                    confirmation_round = confirmation_round + 1,
                    processing_owner = NULL, processing_heartbeat_at = NULL
                WHERE token = ?
                """,
                (json.dumps(cleaned, ensure_ascii=False), submitted_at, token),
            )
        return self.get(token)

    def confirmation_round_by_draft(self, draft_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT confirmation_round FROM confirmation_sessions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        return int(row["confirmation_round"] or 0) if row is not None else 0

    def asked_questions_by_draft(self, draft_id: str) -> list[str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT asked_questions_json FROM confirmation_sessions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        if row is None or not row["asked_questions_json"]:
            return []
        parsed = json.loads(str(row["asked_questions_json"]))
        return [str(question) for question in parsed if str(question).strip()]

    def complete_by_draft(self, draft_id: str) -> None:
        """Mark the stable customer link complete after the recheck passes."""

        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE confirmation_sessions
                SET status = 'completed', processing_owner = NULL,
                    processing_heartbeat_at = NULL
                WHERE draft_id = ?
                  AND status IN ('submitted', 'reviewing', 'processing', 'approved')
                """,
                (draft_id,),
            )

    def prepare_configuration_review(
        self,
        *,
        draft_id: str,
        intent: ParsedIntent,
        confirmation_text: str | None = None,
    ) -> str | None:
        """Reuse the same link for the final, price-free configuration review."""

        ensure_component_keys(intent)
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT token FROM confirmation_sessions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            if row is None:
                return None
            token = str(row["token"])
            connection.execute(
                """
                UPDATE confirmation_sessions
                SET intent_json = ?, confirmation_text = ?,
                    status = 'configuration_review', submitted_at = ?,
                    processing_owner = NULL, processing_heartbeat_at = NULL
                WHERE draft_id = ?
                """,
                (
                    intent.model_dump_json(),
                    confirmation_text
                    or "请确认最终配置清单，确认后系统才会开始报价。",
                    now,
                    draft_id,
                ),
            )
        return token

    def approve_configuration(self, token: str) -> ConfirmationSessionResponse | None:
        row = self._row(token)
        if row is None:
            return None
        if str(row["status"]) != "configuration_review":
            raise ValueError("当前确认单还没有进入最终配置确认阶段")
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE confirmation_sessions
                   SET status = 'approved', processing_owner = NULL,
                       processing_heartbeat_at = NULL
                   WHERE token = ?""",
                (token,),
            )
        return self.get(token)

    def begin_configuration_reprocessing(
        self, token: str, *, owner_id: str | None = None,
    ) -> QuoteRequest | None:
        """Claim a submitted AWS edit and build its self-contained preview request.

        Customer saves must not depend on a salesperson keeping another browser
        tab alive.  The atomic status transition also prevents that legacy poller
        from launching a duplicate full-preview job.
        """

        row = self._row(token)
        if row is None or str(row["status"]) not in {"reviewing", "submitted"}:
            return None
        raw_request = str(row["request_json"] or "{}")
        try:
            payload = json.loads(raw_request)
            if not isinstance(payload, dict):
                payload = {}
        except json.JSONDecodeError:
            payload = {}
        payload.update(
            {
                "cloud_provider": self.cloud_provider,
                "customer_request": str(row["customer_request"]),
                "draft_id": str(row["draft_id"]),
                "confirmation_responses": json.loads(str(row["answers_json"])),
            }
        )
        request = QuoteRequest.model_validate(payload)
        with self._lock, self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE confirmation_sessions
                SET status = 'processing', processing_owner = ?, processing_heartbeat_at = ?
                WHERE token = ? AND status IN ('reviewing', 'submitted')
                """,
                (owner_id, datetime.now(UTC).isoformat(), token),
            ).rowcount
        return request if changed else None

    def touch_configuration_reprocessing(self, draft_id: str, owner_id: str) -> bool:
        """Renew only the lease owned by the currently running worker."""
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            changed = connection.execute(
                """
                UPDATE confirmation_sessions SET processing_heartbeat_at = ?
                WHERE draft_id = ? AND status = 'processing' AND processing_owner = ?
                """,
                (now, draft_id, owner_id),
            ).rowcount
        return bool(changed)

    def release_configuration_reprocessing(self, owner_id: str) -> int:
        """Make this process's unfinished work claimable after shutdown/reload."""
        with self._lock, self._connect() as connection:
            return connection.execute(
                """
                UPDATE confirmation_sessions
                SET status='reviewing', processing_owner=NULL, processing_heartbeat_at=NULL
                WHERE status='processing' AND processing_owner=?
                """,
                (owner_id,),
            ).rowcount

    def recover_unowned_configuration_reprocessing(self) -> int:
        """Upgrade sessions claimed before processing leases were introduced."""
        with self._lock, self._connect() as connection:
            return connection.execute(
                """
                UPDATE confirmation_sessions
                SET status='reviewing', processing_owner=NULL, processing_heartbeat_at=NULL
                WHERE status='processing' AND (processing_owner IS NULL OR processing_owner='')
                """
            ).rowcount

    def submit_configuration_feedback(
        self,
        token: str,
        feedback: str | None = None,
        component_feedback: dict[str, str] | None = None,
        component_updates: dict[str, dict[str, object]] | None = None,
    ) -> ConfirmationSessionResponse | None:
        """Queue only the configuration components the customer corrected."""

        row = self._row(token)
        if row is None:
            return None
        if str(row["status"]) != "configuration_review":
            raise ValueError("当前确认单还没有进入最终配置确认阶段")
        intent = self._parse_persisted_intent(str(row["intent_json"]))
        if self.cloud_provider == "aws":
            from app.integrations.deepseek import DeepSeekIntentParser

            DeepSeekIntentParser.reconcile_customer_pricing_facts(intent)
            with self._lock, self._connect() as connection:
                connection.execute(
                    "UPDATE confirmation_sessions SET intent_json = ? WHERE token = ?",
                    (intent.model_dump_json(), token),
                )
        stable_to_index = {
            item.component_key: str(index)
            for index, item in enumerate(intent.services)
            if item.component_key
        }

        def resolved_component_id(component_id: object) -> str | None:
            value = str(component_id)
            if value in stable_to_index:
                return stable_to_index[value]
            if value.isdigit() and 0 <= int(value) < len(intent.services):
                return value
            return None

        invalid_ids: set[str] = set()
        cleaned_components: dict[str, str] = {}
        for component_id, value in (component_feedback or {}).items():
            resolved = resolved_component_id(component_id)
            if resolved is None:
                invalid_ids.add(str(component_id))
            elif value.strip():
                cleaned_components[resolved] = value.strip()
        cleaned_updates: dict[str, dict[str, object]] = {}
        for component_id, update in (component_updates or {}).items():
            resolved = resolved_component_id(component_id)
            if resolved is None:
                invalid_ids.add(str(component_id))
            elif update:
                cleaned_updates[resolved] = update
        if invalid_ids:
            raise ValueError("配置项已变更，请刷新页面后重新填写")
        cleaned_feedback = (feedback or "").strip()
        delete_ids = {
            component_id
            for component_id, value in cleaned_components.items()
            if value == CONFIGURATION_COMPONENT_DELETE
        }
        if delete_ids and len(delete_ids) >= len(intent.services) and not cleaned_feedback:
            raise ValueError("报价至少需要保留一项配置")
        if not cleaned_feedback and not cleaned_components and not cleaned_updates:
            raise ValueError("请填写需要修改的内容")
        submitted_at = datetime.now(UTC).isoformat()
        answers: dict[str, str] = {}
        if cleaned_feedback:
            # Kept for compatibility with links created before per-component
            # correction was introduced.
            answers[CONFIGURATION_FEEDBACK_QUESTION] = cleaned_feedback
        answers.update(
            {
                f"{CONFIGURATION_COMPONENT_FEEDBACK_PREFIX}{component_id}": value
                for component_id, value in cleaned_components.items()
            }
        )
        answers.update(
            {
                f"{CONFIGURATION_COMPONENT_UPDATE_PREFIX}{component_id}": json.dumps(
                    update, ensure_ascii=False, separators=(",", ":")
                )
                for component_id, update in cleaned_updates.items()
            }
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE confirmation_sessions
                SET answers_json = ?, status = 'reviewing', submitted_at = ?,
                    processing_owner = NULL, processing_heartbeat_at = NULL
                WHERE token = ?
                """,
                (json.dumps(answers, ensure_ascii=False), submitted_at, token),
            )
        return self.get(token)

    def status_by_draft(self, draft_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM confirmation_sessions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        return str(row["status"]) if row is not None else None

    def restore_draft(self, draft_id: str) -> tuple[str, ParsedIntent] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT customer_request, intent_json "
                "FROM confirmation_sessions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        if row is None:
            return None
        intent = self._parse_persisted_intent(str(row["intent_json"]))
        return str(row["customer_request"]), intent

    def historical_answers_by_component(
        self,
        draft_id: str,
    ) -> tuple[dict[int, dict[str, str]], dict[str, str]]:
        """Restore exact question/component bindings after final review.

        Final configuration review must not forget the decisions that produced
        that configuration. New sessions retain their questions and answers;
        older sessions are recovered from the self-contained request snapshot.
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT items_json, answers_json, asked_questions_json, request_json "
                "FROM confirmation_sessions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        if row is None:
            return {}, {}

        answers: dict[str, str] = {}
        try:
            request_payload = json.loads(str(row["request_json"] or "{}"))
        except json.JSONDecodeError:
            request_payload = {}
        if isinstance(request_payload, dict):
            request_answers = request_payload.get("confirmation_responses")
            if isinstance(request_answers, dict):
                answers.update(
                    {
                        str(key): str(value)
                        for key, value in request_answers.items()
                        if str(value).strip()
                    }
                )
        try:
            stored_answers = json.loads(str(row["answers_json"] or "{}"))
        except json.JSONDecodeError:
            stored_answers = {}
        if isinstance(stored_answers, dict):
            answers.update(
                {
                    str(key): str(value)
                    for key, value in stored_answers.items()
                    if str(value).strip()
                }
            )
        if not answers:
            return {}, {}

        answer_bindings: dict[str, tuple[str, str | None]] = {}
        try:
            raw_items = json.loads(str(row["items_json"] or "[]"))
        except json.JSONDecodeError:
            raw_items = []
        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict) or not item.get("question"):
                    continue
                question = str(item["question"])
                component_id = (
                    str(item["component_id"])
                    if item.get("component_id") is not None
                    else None
                )
                answer_key = str(item.get("answer_key") or question)
                answer_bindings[answer_key] = (question, component_id)
                answer_bindings.setdefault(question, (question, component_id))

        try:
            asked_questions = json.loads(str(row["asked_questions_json"] or "[]"))
        except json.JSONDecodeError:
            asked_questions = []
        questions_by_digest = {
            hashlib.sha256(str(question).encode("utf-8")).hexdigest()[:16]: str(question)
            for question in asked_questions
            if str(question).strip()
        }

        component_answers: dict[int, dict[str, str]] = {}
        global_answers: dict[str, str] = {}
        opaque_key = re.compile(r"^component-(\d+):([0-9a-f]{16})$")
        scoped_key = re.compile(r"^__component_answer__(\d+)::([\s\S]+)$")
        for answer_key, answer in answers.items():
            question, component_id = answer_bindings.get(answer_key, (answer_key, None))
            opaque_match = opaque_key.fullmatch(answer_key)
            if opaque_match:
                component_id = opaque_match.group(1)
                question = questions_by_digest.get(opaque_match.group(2), question)
            scoped_match = scoped_key.fullmatch(answer_key)
            if scoped_match:
                component_id = scoped_match.group(1)
                question = scoped_match.group(2)
            if component_id is not None and component_id.isdigit():
                component_answers.setdefault(int(component_id), {})[question] = answer
            else:
                global_answers[question] = answer
        return component_answers, global_answers

    def partition_answers_by_component(
        self,
        draft_id: str,
        answers: dict[str, str],
    ) -> tuple[dict[int, dict[str, str]], dict[str, str]]:
        """Bind every submitted answer back to the component that asked it."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT items_json FROM confirmation_sessions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        if row is None:
            return {}, dict(answers)
        answer_bindings: dict[str, tuple[str, str | None]] = {}
        ambiguous_legacy_questions: set[str] = set()
        for item in json.loads(str(row["items_json"])):
            if not isinstance(item, dict) or not item.get("question"):
                continue
            question = str(item["question"])
            component_id = (
                str(item["component_id"])
                if item.get("component_id") is not None
                else None
            )
            answer_key = str(item.get("answer_key") or question)
            answer_bindings[answer_key] = (question, component_id)
            existing = answer_bindings.get(question)
            if existing is None:
                answer_bindings[question] = (question, component_id)
            elif existing[1] != component_id:
                ambiguous_legacy_questions.add(question)
        for question in ambiguous_legacy_questions:
            answer_bindings.pop(question, None)
        component_answers: dict[int, dict[str, str]] = {}
        global_answers: dict[str, str] = {}
        for answer_key, answer in answers.items():
            question, component_id = answer_bindings.get(answer_key, (answer_key, None))
            if component_id is not None and component_id.isdigit():
                component_answers.setdefault(int(component_id), {})[question] = answer
            else:
                global_answers[question] = answer
        return component_answers, global_answers

    def extract_component_processor_architectures(
        self,
        draft_id: str,
        answers: dict[str, str],
    ) -> tuple[dict[int, str], dict[str, str]]:
        """Remove architecture metadata and bind each exception to one component."""

        with self._connect() as connection:
            row = connection.execute(
                "SELECT items_json FROM confirmation_sessions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        if row is None:
            return {}, {
                key: value
                for key, value in answers.items()
                if not key.startswith(COMPONENT_PROCESSOR_ARCHITECTURE_PREFIX)
            }
        component_by_answer_key = {
            str(item.get("answer_key") or item.get("question") or ""): str(
                item.get("component_id")
            )
            for item in json.loads(str(row["items_json"]))
            if isinstance(item, dict)
            and item.get("question")
            and item.get("component_id") is not None
        }
        preferences: dict[int, str] = {}
        remaining: dict[str, str] = {}
        for key, value in answers.items():
            if not key.startswith(COMPONENT_PROCESSOR_ARCHITECTURE_PREFIX):
                remaining[key] = value
                continue
            answer_key = key.removeprefix(COMPONENT_PROCESSOR_ARCHITECTURE_PREFIX)
            component_id = component_by_answer_key.get(answer_key)
            if component_id and component_id.isdigit() and value in {"arm64", "x86_64"}:
                preferences[int(component_id)] = value
        return preferences, remaining

    def _row(self, token: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM confirmation_sessions WHERE token = ?",
                (token,),
            ).fetchone()

    @staticmethod
    def _configuration_summary(intent: ParsedIntent) -> str:
        region = next((item.region for item in intent.services if item.region), None)
        services = "、".join(
            f"{item.calculator_service_name or item.service} × {item.quantity}"
            for item in intent.services
        )
        return (
            f"已识别 {len(intent.services)} 项 AWS 配置；"
            f"区域：{region or '待确认'}；{services}。"
        )

    @staticmethod
    def _deduplicate_confirmation_items(
        items: list[ConfirmationItem],
    ) -> list[ConfirmationItem]:
        """Prefer the complete question over a truncated duplicate in one component."""

        result: list[ConfirmationItem] = []

        def normalized(question: str) -> str:
            return re.sub(
                r"[\s，,。；;、：:？?!！…]+", "", question.casefold()
            )

        def same_scope(left: ConfirmationItem, right: ConfirmationItem) -> bool:
            if left.component_id or right.component_id:
                return bool(
                    left.component_id
                    and right.component_id
                    and left.component_id == right.component_id
                )
            if left.service or right.service:
                return bool(
                    left.service
                    and right.service
                    and left.service == right.service
                )
            return True

        for item in items:
            current = normalized(item.question)
            duplicate_index = next(
                (
                    index
                    for index, existing in enumerate(result)
                    if same_scope(item, existing)
                    and min(len(current), len(normalized(existing.question))) >= 20
                    and (
                        current.startswith(normalized(existing.question))
                        or normalized(existing.question).startswith(current)
                    )
                ),
                None,
            )
            if duplicate_index is None:
                result.append(item)
            elif len(item.question) > len(result[duplicate_index].question):
                result[duplicate_index] = item
        return result
