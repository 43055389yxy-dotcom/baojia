from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from app.domain.customer_configuration import preserve_customer_configuration
from app.domain.models import (
    ConfirmationItem,
    ConfirmationSessionResponse,
    ConfigurationReviewItem,
    ParsedIntent,
)


CONFIGURATION_FEEDBACK_QUESTION = "【客户对最终配置表的修改意见】"
CONFIGURATION_COMPONENT_FEEDBACK_PREFIX = "【组件修改】"
CONFIGURATION_COMPONENT_DELETE = "__DELETE_COMPONENT__"


class ConfirmationSessionStore:
    """Persistent customer-confirmation forms tied to structured quote drafts."""

    def __init__(self, database_path: Path):
        self._database_path = database_path
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection

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
                    submitted_at TEXT
                )
                """
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
    ) -> str:
        now = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT token FROM confirmation_sessions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
            token = str(existing["token"]) if existing else secrets.token_urlsafe(18)
            connection.execute(
                """
                INSERT INTO confirmation_sessions (
                    token, draft_id, customer_request, customer_summary, intent_json,
                    confirmation_text, items_json, answers_json, status, created_at, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', 'pending', ?, NULL)
                ON CONFLICT(draft_id) DO UPDATE SET
                    customer_request=excluded.customer_request,
                    customer_summary=excluded.customer_summary,
                    intent_json=excluded.intent_json,
                    confirmation_text=excluded.confirmation_text,
                    items_json=excluded.items_json,
                    answers_json='{}', status='pending', submitted_at=NULL
                """,
                (
                    token,
                    draft_id,
                    customer_request,
                    customer_summary,
                    intent.model_dump_json(),
                    confirmation_text,
                    json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False),
                    now,
                ),
            )
        return token

    def get(self, token: str) -> ConfirmationSessionResponse | None:
        row = self._row(token)
        if row is None:
            return None
        intent = ParsedIntent.model_validate_json(str(row["intent_json"]))
        preserve_customer_configuration(intent)
        self._normalize_review_group_quantities(intent)
        configuration_items = [
            ConfigurationReviewItem(
                component_id=str(index),
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
                pricing_status=(
                    "unpriced"
                    if item.requirements.get("_quote_skip_reason")
                    else "ready"
                ),
                pricing_notice=(
                    str(item.requirements.get("_quote_skip_reason"))
                    if item.requirements.get("_quote_skip_reason")
                    else None
                ),
                requirements={
                    key: value
                    for key, value in item.requirements.items()
                    if not key.startswith("_") and key != "system_default_assumption"
                },
                source_text=item.source_text,
            )
            for index, item in enumerate(intent.services)
        ]
        confirmation_items = self._deduplicate_confirmation_items(
            [
                ConfirmationItem.model_validate(item)
                for item in json.loads(str(row["items_json"]))
            ]
        )
        return ConfirmationSessionResponse(
            token=str(row["token"]),
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

    def submit(self, token: str, answers: dict[str, str]) -> ConfirmationSessionResponse | None:
        row = self._row(token)
        if row is None:
            return None
        questions = {
            str(item.get("question"))
            for item in json.loads(str(row["items_json"]))
        }
        cleaned = {
            question: answer.strip()
            for question, answer in answers.items()
            if question in questions and answer.strip()
        }
        if set(cleaned) != questions:
            missing = questions - cleaned.keys()
            raise ValueError(f"尚有 {len(missing)} 项未填写")
        submitted_at = datetime.now(UTC).isoformat()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE confirmation_sessions
                SET answers_json = ?, status = 'reviewing', submitted_at = ?
                WHERE token = ?
                """,
                (json.dumps(cleaned, ensure_ascii=False), submitted_at, token),
            )
        return self.get(token)

    def complete_by_draft(self, draft_id: str) -> None:
        """Mark the stable customer link complete after the recheck passes."""

        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE confirmation_sessions
                SET status = 'completed'
                WHERE draft_id = ? AND status IN ('submitted', 'reviewing', 'approved')
                """,
                (draft_id,),
            )

    def prepare_configuration_review(
        self,
        *,
        draft_id: str,
        intent: ParsedIntent,
    ) -> str | None:
        """Reuse the same link for the final, price-free configuration review."""

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
                SET intent_json = ?, confirmation_text = ?, items_json = '[]',
                    status = 'configuration_review', submitted_at = ?
                WHERE draft_id = ?
                """,
                (
                    intent.model_dump_json(),
                    "请确认最终配置清单，确认后系统才会开始报价。",
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
                "UPDATE confirmation_sessions SET status = 'approved' WHERE token = ?",
                (token,),
            )
        return self.get(token)

    def submit_configuration_feedback(
        self,
        token: str,
        feedback: str | None = None,
        component_feedback: dict[str, str] | None = None,
    ) -> ConfirmationSessionResponse | None:
        """Queue only the configuration components the customer corrected."""

        row = self._row(token)
        if row is None:
            return None
        if str(row["status"]) != "configuration_review":
            raise ValueError("当前确认单还没有进入最终配置确认阶段")
        intent = ParsedIntent.model_validate_json(str(row["intent_json"]))
        valid_component_ids = {str(index) for index in range(len(intent.services))}
        cleaned_components = {
            str(component_id): value.strip()
            for component_id, value in (component_feedback or {}).items()
            if value.strip()
        }
        invalid_ids = set(cleaned_components) - valid_component_ids
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
        if not cleaned_feedback and not cleaned_components:
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
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE confirmation_sessions
                SET answers_json = ?, status = 'reviewing', submitted_at = ?
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
                "SELECT customer_request, intent_json FROM confirmation_sessions WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        if row is None:
            return None
        intent = ParsedIntent.model_validate_json(str(row["intent_json"]))
        preserve_customer_configuration(intent)
        return str(row["customer_request"]), intent

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
        question_components = {
            str(item.get("question")): str(item.get("component_id"))
            for item in json.loads(str(row["items_json"]))
            if item.get("question") and item.get("component_id") is not None
        }
        component_answers: dict[int, dict[str, str]] = {}
        global_answers: dict[str, str] = {}
        for question, answer in answers.items():
            component_id = question_components.get(question)
            if component_id is not None and component_id.isdigit():
                component_answers.setdefault(int(component_id), {})[question] = answer
            else:
                global_answers[question] = answer
        return component_answers, global_answers

    def _row(self, token: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM confirmation_sessions WHERE token = ?",
                (token,),
            ).fetchone()

    @staticmethod
    def _normalize_review_group_quantities(intent: ParsedIntent) -> None:
        for item in intent.services:
            if item.service.casefold() not in {"rds", "aurora"}:
                continue
            if item.requirements.get("aurora_cluster"):
                continue
            deployment = str(item.requirements.get("deployment") or "").casefold()
            source = item.source_text or ""
            if deployment not in {"multi_az", "multi-az"} and not re.search(
                r"主备|高可用|multi[ -]?az", source, re.I
            ):
                continue
            if not re.search(
                r"(?:数据库|实例|集群)?数量\s*[:：]?\s*\d+|"
                r"\d+\s*(?:套|个数据库|个集群)",
                source,
                re.I,
            ):
                item.quantity = 1

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
