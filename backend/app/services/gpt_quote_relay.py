"""File-backed queue shared with the host-side quote-engine workers.

The backend container only accepts sales requests and exposes progress.  The
worker on the desktop host owns the logged-in browser.  Raw customer text is
kept only while queued and is removed as soon as the worker has submitted the
first-pass cleaning prompt.
"""

from __future__ import annotations

import fcntl
import heapq
import json
import math
import os
import re
import secrets
import statistics
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.services.codex_chat_references import (
    is_codex_chat_reference,
    is_codex_conversation_id,
)
from app.services.gemini_chat_references import is_gemini_chat_reference
from app.services.gpt_quote_batches import (
    COMPONENTS_PER_CHAT,
    WAVES_PER_CHAT,
    split_component_plan,
    split_numbered_intake,
)

UTC = timezone.utc  # noqa: UP017 - the host-side worker still supports Python 3.9

TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled"}
DELIVERY_RECEIPT_STATUSES = {
    "delivered",
    "page_result_ready",
    "partial_page_result_ready",
}
PUBLIC_FIELDS = {
    "job_id",
    "status",
    "created_at",
    "updated_at",
    "submission_code",
    "cloud_provider",
    "preferred_region",
    "preferred_engine",
    "assigned_engine",
    "failure_code",
    "display_result_on_page",
    "quick_quote_result",
    "quote_download_url",
    "quote_download_filename",
    "progress",
}

DEFAULT_MAX_CONCURRENT_QUOTES = 4
DEFAULT_QUOTE_SECONDS = 600
MAX_ACTIVE_CHATS_PER_SALES_JOB = 3
QUOTE_ENGINES = ("chatgpt", "gemini")
DEFAULT_ENABLED_QUOTE_ENGINES = ("chatgpt",)

PUBLIC_FAILURE_CATEGORIES = {
    "credentials",
    "authorization",
    "provider_unavailable",
    "transport",
    "rate_limit",
    "invalid_request",
    "response_schema",
    "official_api_error",
}
SALES_CACHE_FALLBACK_NOTICE = (
    "销售提示：官方价格接口临时不可用，部分价格采用带时间戳的最近官方价格快照；"
    "建议发送客户前再次确认。"
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class GptRelayError(RuntimeError):
    def __init__(self, message: str, *, code: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class GptQuoteRelayStore:
    def __init__(
        self,
        directory: Path | str | None = None,
        *,
        max_concurrent_quotes: int | None = None,
        default_quote_seconds: int | None = None,
        checkpoint_directory: Path | str | None = None,
        enabled_engines: tuple[str, ...] | None = None,
    ) -> None:
        configured_limit = max_concurrent_quotes or DEFAULT_MAX_CONCURRENT_QUOTES
        configured_duration = default_quote_seconds or DEFAULT_QUOTE_SECONDS
        self.max_concurrent_quotes = max(1, configured_limit)
        self.default_quote_seconds = max(60, configured_duration)
        configured_engines = enabled_engines or DEFAULT_ENABLED_QUOTE_ENGINES
        self.enabled_engines = tuple(
            dict.fromkeys(
                self._normalize_engine(engine)
                for engine in configured_engines
                if str(engine or "").strip().lower() in QUOTE_ENGINES
            )
        ) or DEFAULT_ENABLED_QUOTE_ENGINES
        owner_uid = os.environ.get("ASTRAQUOTE_GPT_RELAY_UID")
        owner_gid = os.environ.get("ASTRAQUOTE_GPT_RELAY_GID")
        self.owner_uid = int(owner_uid) if owner_uid and owner_uid.isdigit() else None
        self.owner_gid = int(owner_gid) if owner_gid and owner_gid.isdigit() else None
        configured_directory = directory or os.environ.get("ASTRAQUOTE_GPT_RELAY_DIR")
        if configured_directory:
            self.directory = Path(configured_directory)
        else:
            data_root = Path("/data")
            self.directory = (
                data_root / "gpt-relay"
                if data_root.is_dir() and os.access(data_root, os.W_OK)
                else Path(__file__).resolve().parents[3] / ".data" / "gpt-relay"
            )
        self.jobs_directory = self.directory / "jobs"
        self.completions_directory = self.directory / "completions"
        self.requests_directory = self.directory / "requests"
        self.jobs_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.completions_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.requests_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._set_owner(self.directory)
        self._set_owner(self.jobs_directory)
        self._set_owner(self.completions_directory)
        self._set_owner(self.requests_directory)
        self.lock_path = self.directory / ".queue.lock"
        self.heartbeat_path = self.directory / "worker-heartbeat.json"
        configured_checkpoints = checkpoint_directory or os.environ.get(
            "ASTRAQUOTE_V2_STATE_DIR"
        )
        self.checkpoint_directory = Path(
            configured_checkpoints
            or (
                "/data/v2-quotes"
                if Path("/data").is_dir()
                else Path(__file__).resolve().parents[3] / ".data" / "v2-quotes"
            )
        )

    def _set_owner(self, path: Path | str) -> None:
        if self.owner_uid is None and self.owner_gid is None:
            return
        os.chown(
            path,
            self.owner_uid if self.owner_uid is not None else -1,
            self.owner_gid if self.owner_gid is not None else -1,
        )

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.lock_path.touch(mode=0o600, exist_ok=True)
        self._set_owner(self.lock_path)
        with self.lock_path.open("r+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _path(self, job_id: str) -> Path:
        if not job_id.startswith("gpt-") or not job_id[4:].isalnum():
            raise GptRelayError("无效的 GPT 报价任务编号。", code="gpt_relay_job_id_invalid")
        return self.jobs_directory / f"{job_id}.json"

    def _completion_path(self, job_id: str) -> Path:
        self._path(job_id)
        return self.completions_directory / f"{job_id}.json"

    def heartbeat_path_for(self, engine: str) -> Path:
        normalized = self._normalize_engine(engine)
        if normalized == "chatgpt":
            return self.heartbeat_path
        return self.directory / f"worker-heartbeat-{normalized}.json"

    @staticmethod
    def _normalize_engine(value: object) -> str:
        normalized = str(value or "chatgpt").strip().lower()
        return normalized if normalized in QUOTE_ENGINES else "chatgpt"

    def _request_path(self, client_request_id: str) -> Path:
        try:
            normalized = str(uuid.UUID(client_request_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise GptRelayError(
                "无效的客户端提交编号。", code="gpt_relay_client_request_id_invalid"
            ) from exc
        return self.requests_directory / f"{normalized}.json"

    def _write_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            self._set_owner(temporary)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _event(stage: str, message: str) -> dict[str, str]:
        return {"stage": stage, "message": message, "time": utc_now()}

    def create(
        self,
        customer_request: str,
        options: dict[str, Any],
        *,
        numbered_components: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        text = customer_request.strip()
        if len(text) < 3 or len(text) > 12000:
            raise GptRelayError(
                "客户需求长度必须在 3 到 12000 个字符之间。",
                code="gpt_relay_request_invalid",
            )
        job_id = f"gpt-{uuid.uuid4().hex}"
        now = utc_now()
        with self._lock():
            client_request_id = str(options.get("client_request_id") or uuid.uuid4())
            request_path = self._request_path(client_request_id)
            if request_path.exists():
                try:
                    request_index = self._read(request_path)
                    existing_path = self._path(str(request_index.get("job_id") or ""))
                    if existing_path.exists():
                        return self.public(self._read(existing_path))
                except (OSError, ValueError, TypeError, GptRelayError):
                    pass
            active_codes: set[str] = set()
            for path in self.jobs_directory.glob("gpt-*.json"):
                try:
                    existing = self._read(path)
                except (OSError, ValueError):
                    continue
                if existing.get("status") not in TERMINAL_STATUSES:
                    code = str(existing.get("submission_code") or "")
                    if code in {str(value) for value in range(1, 10)}:
                        active_codes.add(code)
            available = [str(value) for value in range(1, 10) if str(value) not in active_codes]
            submission_code = secrets.choice(available or [str(value) for value in range(1, 10)])
            intake_groups = (
                split_numbered_intake(numbered_components)
                if numbered_components is not None
                else []
            )
            intake_batches = [
                {
                    "batch_index": index,
                    "batch_count": len(intake_groups),
                    "conversation_index": index // WAVES_PER_CHAT,
                    "wave_index": index % WAVES_PER_CHAT,
                    "wave_count": min(
                        WAVES_PER_CHAT,
                        len(intake_groups) - (index // WAVES_PER_CHAT) * WAVES_PER_CHAT,
                    ),
                    "component_keys": [str(item["component_key"]) for item in group],
                    "source_lines": [
                        {
                            "component_key": str(item["component_key"]),
                            "source_line": str(item["source_line"]),
                        }
                        for item in group
                    ],
                    "status": "pending",
                }
                for index, group in enumerate(intake_groups)
            ]
            preferred_engine = self._normalize_engine(options.get("preferred_engine"))
            if preferred_engine not in self.enabled_engines:
                preferred_engine = self.enabled_engines[0]
            normalized_options = {**options, "preferred_engine": preferred_engine}
            record = {
                "schema_version": "astraquote-gpt-relay/3",
                "job_id": job_id,
                "submission_code": submission_code,
                "cloud_provider": str(options.get("cloud_provider") or "aws"),
                "preferred_region": str(options.get("preferred_region") or ""),
                "preferred_engine": preferred_engine,
                "assigned_engine": None,
                "display_result_on_page": True,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "customer_request": "" if intake_batches else text,
                "quote_options": normalized_options,
                "source_purged_at": None,
                "intake_component_count": (
                    len(numbered_components or []) if intake_batches else None
                ),
                "intake_batch_count": len(intake_batches) if intake_batches else None,
                "intake_chat_count": (
                    math.ceil(len(intake_batches) / WAVES_PER_CHAT)
                    if intake_batches
                    else None
                ),
                "intake_batches": intake_batches,
                "reserved_price_batch_id": (
                    f"aqpb_{uuid.uuid4()}" if intake_batches else None
                ),
                "merge_authorized": False,
                "events": [self._event("queue", "报价申请已进入队列")],
                "chat_url": None,
                "project_name": None,
                "result_summary": None,
                "error": None,
                "continuation_attempts": 0,
                "stalled_continuation_attempts": 0,
                "last_progress_fingerprint": None,
            }
            self._write_atomic(self._path(job_id), record)
            self._write_atomic(
                request_path,
                {"client_request_id": client_request_id, "job_id": job_id, "created_at": now},
            )
        return self.public(record)

    def intake_chat_batches(self, job_id: str) -> list[dict[str, Any]]:
        """Return the private, mechanically split first-pass intake batches."""

        record = self.get(job_id)
        batches = record.get("intake_batches")
        if not isinstance(batches, list):
            return []
        return [dict(item) for item in batches if isinstance(item, dict)]

    def get(self, job_id: str) -> dict[str, Any]:
        path = self._path(job_id)
        if not path.exists():
            raise GptRelayError("GPT 报价任务不存在。", code="gpt_relay_job_not_found")
        return self._read(path)

    def public(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = {key: record.get(key) for key in PUBLIC_FIELDS}
        progress = self._public_progress(record)
        if progress is not None:
            payload["progress"] = progress
        else:
            payload.pop("progress", None)
        payload["failure_code"] = (
            "AQ-QUOTE-FAILED" if record.get("status") == "failed" else None
        )
        if record.get("status") in {"queued", "needs_login"}:
            payload.update(self._queue_metadata(record))
        return payload

    def _checkpoint_path(self, job_id: str) -> Path:
        self._path(job_id)
        return self.checkpoint_directory / f"relay-{job_id}.json"

    def _checkpoint(self, job_id: str) -> dict[str, Any] | None:
        path = self._checkpoint_path(job_id)
        if not path.exists():
            return None
        try:
            checkpoint = self._read(path)
        except (OSError, ValueError, TypeError):
            return None
        if checkpoint.get("relay_job_id") not in {None, job_id}:
            return None
        return checkpoint

    def quote_chat_batches(self, job_id: str) -> list[dict[str, Any]]:
        """Read private, sealed component batches for the desktop worker.

        New numbered jobs expose their preassigned ownership keys immediately,
        before AI cleaning. Raw sales lines are never returned here. Legacy jobs
        continue to split only after their cleaned plan is sealed.
        """

        record = self.get(job_id)
        checkpoint = self._checkpoint(job_id) or {}
        batch_id = str(
            checkpoint.get("price_batch_id")
            or record.get("reserved_price_batch_id")
            or ""
        )
        if not re.fullmatch(r"aqpb_[a-f0-9-]{36}", batch_id):
            return []
        batch_path = self.checkpoint_directory / f"{batch_id}.json"
        try:
            batch = self._read(batch_path)
        except (OSError, ValueError, TypeError):
            batch = {}
        if batch and str(batch.get("relay_job_id") or "") != job_id:
            return []
        components = batch.get("quote_components") or []
        if not isinstance(components, list):
            components = []
        lifecycle_by_key = {
            str(item.get("component_key") or ""): str(item.get("state") or "pending")
            for item in (batch.get("component_lifecycle") or [])
            if isinstance(item, dict)
        }

        intake_batches = self.intake_chat_batches(job_id)
        if intake_batches:
            root_batch_index = {
                str(key): int(item["batch_index"])
                for item in intake_batches
                for key in (item.get("component_keys") or [])
            }
            by_key = {
                str(item.get("component_key") or ""): item
                for item in components
                if isinstance(item, dict) and item.get("component_key")
            }

            def component_root(component_key: str) -> str | None:
                current = component_key
                seen: set[str] = set()
                while current in by_key and current not in seen:
                    seen.add(current)
                    parent = str(by_key[current].get("parent_component_key") or "")
                    if not parent:
                        return current
                    current = parent
                return None

            result = []
            for item in intake_batches:
                batch_index = int(item["batch_index"])
                expected_roots = [str(key) for key in (item.get("component_keys") or [])]
                owned = [
                    component
                    for key, component in by_key.items()
                    if root_batch_index.get(component_root(key) or "") == batch_index
                ]
                actual_keys = [str(component["component_key"]) for component in owned]
                state_keys = list(dict.fromkeys([*expected_roots, *actual_keys]))
                result.append(
                    {
                        "batch_index": batch_index,
                        "batch_count": int(item.get("batch_count") or len(intake_batches)),
                        "price_batch_id": batch_id,
                        "component_keys": state_keys,
                        "components": owned,
                        "component_states": {
                            key: lifecycle_by_key.get(key, "pending") for key in state_keys
                        },
                    }
                )
            return result

        if not record.get("source_purged_at") or not components:
            return []
        try:
            grouped = split_component_plan(components)
        except (KeyError, TypeError, ValueError):
            return []
        return [
            {
                "batch_index": index,
                "batch_count": len(grouped),
                "price_batch_id": batch_id,
                "component_keys": [str(item["component_key"]) for item in group],
                "components": group,
                "component_states": {
                    str(item["component_key"]): lifecycle_by_key.get(
                        str(item["component_key"]), "pending"
                    )
                    for item in group
                },
            }
            for index, group in enumerate(grouped)
        ]

    def record_chat_session(
        self,
        job_id: str,
        *,
        batch_index: int,
        batch_count: int,
        chat_url: str,
        role: str,
        component_keys: list[str],
        previous_conversation_ids: list[str] | None = None,
        conversation_index: int | None = None,
        wave_index: int = 0,
        wave_count: int = 1,
    ) -> dict[str, Any]:
        """Persist one logical chat while the robot keeps a single window."""

        if role not in {"coordinator", "component_batch"}:
            raise GptRelayError("无效的报价对话角色。", code="gpt_relay_chat_role_invalid")
        if not 0 <= batch_index < batch_count <= 200:
            raise GptRelayError("无效的报价对话批次。", code="gpt_relay_chat_batch_invalid")
        stable_reference = is_codex_chat_reference(chat_url) or is_gemini_chat_reference(
            chat_url
        )
        pending_reference = str(chat_url) == f"codex-chat://pending/{job_id}"
        if not stable_reference and not pending_reference:
            raise GptRelayError("无效的报价对话地址。", code="gpt_relay_chat_url_invalid")
        previous_ids = list(previous_conversation_ids or [])
        if len(previous_ids) > 200 or any(
            not is_codex_conversation_id(value)
            for value in previous_ids
        ):
            raise GptRelayError(
                "无效的历史报价对话地址。", code="gpt_relay_chat_url_invalid"
            )
        with self._lock():
            path = self._path(job_id)
            record = self._read(path)
            sessions = [
                item
                for item in (record.get("chat_sessions") or [])
                if int(item.get("batch_index", -1)) != batch_index
            ]
            sessions.append(
                {
                    "batch_index": batch_index,
                    "batch_count": batch_count,
                    "conversation_index": (
                        batch_index if conversation_index is None else conversation_index
                    ),
                    "wave_index": wave_index,
                    "wave_count": wave_count,
                    "chat_url": str(chat_url),
                    "role": role,
                    "component_keys": list(component_keys),
                    "previous_conversation_ids": previous_ids,
                    "status": "running",
                    "updated_at": utc_now(),
                }
            )
            sessions.sort(key=lambda item: int(item["batch_index"]))
            record["chat_sessions"] = sessions
            if role == "coordinator":
                record["chat_url"] = str(chat_url)
            record["updated_at"] = utc_now()
            self._write_atomic(path, record)
            return record

    def promote_chat_session_reference(
        self,
        job_id: str,
        batch_index: int,
        previous_reference: str,
        stable_reference: str,
    ) -> dict[str, Any]:
        """Atomically replace a pending or remapped Codex conversation handle."""

        previous_is_valid = (
            previous_reference == f"codex-chat://pending/{job_id}"
            or is_codex_chat_reference(previous_reference)
        )
        if not previous_is_valid or not is_codex_chat_reference(stable_reference):
            raise GptRelayError(
                "无效的报价对话地址。", code="gpt_relay_chat_url_invalid"
            )
        with self._lock():
            path = self._path(job_id)
            record = self._read(path)
            sessions = list(record.get("chat_sessions") or [])
            target = next(
                (
                    item
                    for item in sessions
                    if int(item.get("batch_index", -1)) == batch_index
                ),
                None,
            )
            if target is None or target.get("chat_url") != previous_reference:
                raise GptRelayError(
                    "报价对话批次不存在。", code="gpt_relay_chat_batch_not_found"
                )
            target["chat_url"] = stable_reference
            target["previous_conversation_ids"] = []
            target["updated_at"] = utc_now()
            if target.get("role") == "coordinator":
                record["chat_url"] = stable_reference
            record["chat_sessions"] = sessions
            record["updated_at"] = utc_now()
            self._write_atomic(path, record)
            return record

    def update_chat_session(
        self,
        job_id: str,
        batch_index: int,
        **changes: Any,
    ) -> dict[str, Any]:
        """Update only worker bookkeeping; component results stay in V2 state."""

        with self._lock():
            path = self._path(job_id)
            record = self._read(path)
            sessions = list(record.get("chat_sessions") or [])
            found = False
            for item in sessions:
                if int(item.get("batch_index", -1)) == batch_index:
                    item.update(changes)
                    item["updated_at"] = utc_now()
                    found = True
                    break
            if not found:
                raise GptRelayError(
                    "报价对话批次不存在。", code="gpt_relay_chat_batch_not_found"
                )
            record["chat_sessions"] = sessions
            record["updated_at"] = utc_now()
            self._write_atomic(path, record)
            return record

    @staticmethod
    def _safe_count(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        try:
            count = int(value)
        except (TypeError, ValueError):
            return None
        return count if count >= 0 else None

    def _public_progress(self, record: dict[str, Any]) -> dict[str, Any] | None:
        checkpoint = self._checkpoint(str(record.get("job_id") or ""))
        intake_total = self._safe_count(record.get("intake_component_count"))
        intake_chat_count = self._safe_count(record.get("intake_chat_count"))
        if intake_chat_count is None:
            intake_batch_count = self._safe_count(record.get("intake_batch_count"))
            if intake_batch_count is not None:
                intake_chat_count = math.ceil(intake_batch_count / WAVES_PER_CHAT)
        if checkpoint is None and intake_total is None:
            return None
        checkpoint = checkpoint or {}
        progress: dict[str, Any] = {
            "stage": str(checkpoint.get("stage") or record.get("status") or "processing")[:80],
        }
        for key in (
            "total_component_count",
            "top_level_component_count",
            "component_chat_count",
            "completed_component_count",
            "failed_component_count",
            "pending_component_count",
        ):
            count = self._safe_count(checkpoint.get(key))
            if count is not None:
                progress[key] = count
        if intake_total is not None:
            progress.setdefault("total_component_count", intake_total)
            progress.setdefault("top_level_component_count", intake_total)
            progress.setdefault("pending_component_count", intake_total)
        if intake_chat_count is not None:
            progress.setdefault("component_chat_count", intake_chat_count)
        progress.setdefault("completed_component_count", 0)
        progress.setdefault("failed_component_count", 0)
        updated_at = str(checkpoint.get("updated_at") or "").strip()
        if updated_at:
            progress["updated_at"] = updated_at[:80]
        return progress

    def progress_fingerprint(self, job_id: str) -> str:
        """Return a stable machine-progress identity, excluding timestamps/text."""

        checkpoint = self._checkpoint(job_id) or {}
        stable = self._progress_snapshot(checkpoint)
        return json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _progress_snapshot(cls, checkpoint: dict[str, Any]) -> dict[str, Any]:
        # Attempts, failures, query IDs and discovery results are activity, not
        # progress. Pricing status can oscillate as the next query starts.
        stage_ranks = {
            "requirements_cleaned": 1,
            "pricing_request_rejected": 1,
            "pricing_partial": 2,
            "pricing_completed": 2,
            "estimate_validated": 3,
            "artifacts_generated": 4,
            "delivery_completed": 5,
        }
        return {
            "stage_rank": stage_ranks.get(str(checkpoint.get("stage") or ""), 0),
            "completed_component_count": cls._safe_count(
                checkpoint.get("completed_component_count")
            ) or 0,
            "completed_component_keys": sorted({
                key for key in (checkpoint.get("completed_component_keys") or [])
                if isinstance(key, str) and key
            }),
        }

    @classmethod
    def _merge_progress(cls, previous: str | None, current: str) -> str:
        """Remember high-water marks so state regressions cannot buy retries."""

        try:
            prior = json.loads(previous or "{}")
        except (TypeError, ValueError):
            prior = {}
        if not isinstance(prior, dict):
            prior = {}
        if "stage_rank" not in prior:
            prior = cls._progress_snapshot(prior)
        latest = json.loads(current)
        merged = {
            "stage_rank": max(prior.get("stage_rank") or 0, latest["stage_rank"]),
            "completed_component_count": max(
                prior.get("completed_component_count") or 0,
                latest["completed_component_count"],
            ),
            "completed_component_keys": sorted(
                set(prior.get("completed_component_keys") or [])
                | set(latest["completed_component_keys"])
            ),
        }
        return json.dumps(merged, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def has_unrecoverable_failure(self, job_id: str) -> bool:
        checkpoint = self._checkpoint(job_id) or {}
        record = self.get(job_id)
        if int(checkpoint.get("partial_retry_generation") or 0) < int(
            record.get("partial_retry_generation") or 0
        ):
            return False
        if checkpoint.get("quote_terminal") is True:
            return True
        error = checkpoint.get("error") or {}
        return checkpoint.get("stage") == "failed" and error.get("retryable") is not True

    def reserve_continuation(self, job_id: str, *, maximum: int = 1) -> bool:
        """Atomically reserve one retry for the current unchanged checkpoint."""

        maximum = 1
        with self._lock():
            path = self._path(job_id)
            if not path.exists():
                raise GptRelayError("GPT 报价任务不存在。", code="gpt_relay_job_not_found")
            record = self._read(path)
            if record.get("status") != "processing" or record.get(
                "partial_finalization_requested"
            ):
                return False
            previous = record.get("last_progress_fingerprint")
            fingerprint = self._merge_progress(previous, self.progress_fingerprint(job_id))
            stalled = int(record.get("stalled_continuation_attempts") or 0)
            if stalled >= maximum:
                return False
            record.update(
                {
                    "continuation_attempts": int(record.get("continuation_attempts") or 0) + 1,
                    "stalled_continuation_attempts": stalled + 1,
                    "last_progress_fingerprint": fingerprint,
                    "updated_at": utc_now(),
                }
            )
            self._write_atomic(path, record)
            return True

    def request_partial_finalization(self, job_id: str) -> bool:
        """Reserve one final partial-delivery instruction after stalled retries."""

        checkpoint = self._checkpoint(job_id) or {}
        completed = self._safe_count(checkpoint.get("completed_component_count")) or 0
        total = self._safe_count(checkpoint.get("total_component_count")) or 0
        if completed < 1 or total <= completed:
            return False
        with self._lock():
            path = self._path(job_id)
            record = self._read(path)
            if record.get("status") != "processing" or record.get(
                "partial_finalization_requested"
            ):
                return False
            record.update(
                {
                    "partial_finalization_requested": True,
                    "updated_at": utc_now(),
                    "events": [
                        *(record.get("events") or []),
                        self._event(
                            "partial_finalization",
                            "停滞重试已结束，正在交付已完成组件",
                        ),
                    ][-100:],
                }
            )
            self._write_atomic(path, record)
            return True

    def _slot_count(self, record: dict[str, Any]) -> int:
        intake_total = self._safe_count(record.get("intake_component_count")) or 0
        checkpoint = self._checkpoint(str(record.get("job_id") or "")) or {}
        total = (
            intake_total
            or self._safe_count(checkpoint.get("top_level_component_count"))
            or self._safe_count(checkpoint.get("total_component_count"))
            or 0
        )
        return min(
            self.max_concurrent_quotes,
            MAX_ACTIVE_CHATS_PER_SALES_JOB,
            max(1, math.ceil(total / COMPONENTS_PER_CHAT)),
        )

    def _has_sealed_component_count(self, record: dict[str, Any]) -> bool:
        if (self._safe_count(record.get("intake_component_count")) or 0) > 0:
            return True
        checkpoint = self._checkpoint(str(record.get("job_id") or "")) or {}
        return (
            self._safe_count(checkpoint.get("top_level_component_count")) or 0
        ) > 0 or (
            self._safe_count(checkpoint.get("total_component_count")) or 0
        ) > 0

    def _record_engine(self, record: dict[str, Any]) -> str:
        return self._normalize_engine(record.get("assigned_engine") or "chatgpt")

    def _active_slots_by_engine(
        self,
        records: list[dict[str, Any]],
        now: datetime,
    ) -> tuple[dict[str, int], dict[str, bool]]:
        slots = {engine: 0 for engine in self.enabled_engines}
        has_unplanned = {engine: False for engine in self.enabled_engines}
        for record in records:
            if record.get("status") != "processing":
                continue
            lease_text = record.get("lease_expires_at")
            expired = False
            if lease_text:
                try:
                    expired = datetime.fromisoformat(str(lease_text)) <= now
                except ValueError:
                    expired = True
            if expired:
                continue
            engine = self._record_engine(record)
            if engine not in slots:
                continue
            slots[engine] += self._slot_count(record)
            has_unplanned[engine] = (
                has_unplanned[engine]
                or not self._has_sealed_component_count(record)
            )
        return slots, has_unplanned

    def _engine_heartbeat_ready(self, engine: str) -> bool | None:
        """Return readiness, or None before an engine has ever reported."""

        path = self.heartbeat_path_for(engine)
        if not path.exists():
            return None
        try:
            heartbeat = self._read(path)
            updated = datetime.fromisoformat(str(heartbeat["updated_at"]))
            fresh = datetime.now(UTC) - updated < timedelta(seconds=45)
        except (OSError, ValueError, KeyError, TypeError):
            return False
        return fresh and bool(heartbeat.get("logged_in"))

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        try:
            text = str(value or "")
            if text.endswith("Z"):
                text = f"{text[:-1]}+00:00"
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    def _queue_metadata(self, target: dict[str, Any]) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        for path in self.jobs_directory.glob("gpt-*.json"):
            try:
                records.append(self._read(path))
            except (OSError, ValueError):
                continue

        durations: list[tuple[datetime, float]] = []
        for record in records:
            if record.get("status") != "completed":
                continue
            started_at = self._timestamp(record.get("processing_started_at"))
            completed_at = self._timestamp(record.get("updated_at"))
            if started_at and completed_at and completed_at > started_at:
                durations.append(
                    (completed_at, (completed_at - started_at).total_seconds())
                )
        recent_durations = [
            seconds for _completed_at, seconds in sorted(durations, reverse=True)[:20]
        ]
        typical_seconds = (
            max(60, int(statistics.median(recent_durations)))
            if recent_durations
            else self.default_quote_seconds
        )

        now = datetime.now(UTC)
        target_engine = self._normalize_engine(
            target.get("assigned_engine") or target.get("preferred_engine")
        )
        active = [
            record
            for record in records
            if record.get("status") == "processing"
            and self._record_engine(record) == target_engine
        ]
        active_slot_count = min(
            self.max_concurrent_quotes,
            sum(self._slot_count(record) for record in active),
        )
        waiting = sorted(
            (
                record
                for record in records
                if record.get("status") in {"queued", "needs_login"}
            ),
            key=lambda record: (
                str(record.get("created_at") or ""),
                str(record.get("job_id") or ""),
            ),
        )
        target_id = str(target.get("job_id") or "")
        target_index = next(
            (
                index
                for index, record in enumerate(waiting)
                if str(record.get("job_id") or "") == target_id
            ),
            0,
        )

        slot_availability: list[float] = []
        for record in active:
            started_at = self._timestamp(record.get("processing_started_at"))
            elapsed = max(0.0, (now - started_at).total_seconds()) if started_at else 0.0
            slot_availability.extend(
                [max(60.0, typical_seconds - elapsed)] * self._slot_count(record)
            )
        while len(slot_availability) < self.max_concurrent_quotes:
            slot_availability.append(0.0)
        heapq.heapify(slot_availability)

        target_wait_seconds = 0.0
        for index, _record in enumerate(waiting):
            starts_after = heapq.heappop(slot_availability)
            if index == target_index:
                target_wait_seconds = starts_after
                break
            heapq.heappush(slot_availability, starts_after + typical_seconds)

        return {
            "max_concurrent_quotes": self.max_concurrent_quotes,
            "active_quote_count": active_slot_count,
            "queue_position": target_index + 1,
            "queued_ahead_count": target_index,
            "jobs_ahead_count": active_slot_count + target_index,
            "estimated_wait_minutes": math.ceil(target_wait_seconds / 60),
            "engine_capacity": {
                engine: {
                    "active_quote_count": min(
                        self.max_concurrent_quotes,
                        sum(
                            self._slot_count(record)
                            for record in records
                            if record.get("status") == "processing"
                            and self._record_engine(record) == engine
                        ),
                    ),
                    "max_concurrent_quotes": self.max_concurrent_quotes,
                }
                for engine in self.enabled_engines
            },
        }

    def public_get(self, job_id: str) -> dict[str, Any]:
        # A browser-worker heartbeat is transport health, not quote outcome.
        # Keeping the job non-terminal lets a restarted worker reattach to the
        # saved engine conversation and continue from its persisted checkpoint.
        record = self.reconcile_delivery_receipt(job_id)
        return self.public(record)

    def reconcile_delivery_receipt(self, job_id: str) -> dict[str, Any]:
        """Prefer a verified MCP delivery receipt over brittle chat prose.

        The receipt is written only after the private workbook and stable sales
        page link exist. It is bound to the relay job id and submission code,
        and it repairs an earlier false browser failure. Cancellation wins.
        """

        with self._lock():
            job_path = self._path(job_id)
            if not job_path.exists():
                raise GptRelayError("GPT 报价任务不存在。", code="gpt_relay_job_not_found")
            record = self._read(job_path)
            if record.get("status") == "cancelled":
                return record
            if (
                record.get("status") in {"completed", "partial"}
                and record.get("quick_quote_result")
                and record.get("quote_download_url")
            ):
                return record
            receipt_path = self._completion_path(job_id)
            if not receipt_path.exists() or not record.get("source_purged_at"):
                return record
            try:
                receipt = self._read(receipt_path)
            except (OSError, ValueError, TypeError):
                return record
            retry_requested_at = self._timestamp(record.get("partial_retry_requested_at"))
            receipt_delivered_at = self._timestamp(receipt.get("delivered_at"))
            if retry_requested_at and (
                receipt_delivered_at is None or receipt_delivered_at <= retry_requested_at
            ):
                return record
            if (
                receipt.get("schema_version") != "astraquote-relay-completion/1"
                or receipt.get("job_id") != job_id
                or str(receipt.get("submission_code") or "")
                != str(record.get("submission_code") or "")
                or receipt.get("status") not in DELIVERY_RECEIPT_STATUSES
                or not str(receipt.get("quote_id") or "").startswith("aqv2_")
                or not str(receipt.get("delivered_at") or "").strip()
            ):
                return record
            page_result = self._page_result_public(receipt.get("page_result"))
            download_url = self._download_url_public(receipt.get("spreadsheet_url"))
            if page_result is None or download_url is None:
                return record
            record.update(
                {
                    "status": (
                        "partial" if page_result.get("is_partial") is True else "completed"
                    ),
                    "result_status": receipt["status"],
                    "result_summary": "报价结果和 Excel 已生成。",
                    "quick_quote_result": page_result,
                    "quote_download_url": download_url,
                    "quote_download_filename": str(
                        receipt.get("spreadsheet_filename") or "报价单.xlsx"
                    )[:220],
                    "error": None,
                    "lease_expires_at": None,
                    "updated_at": utc_now(),
                    "events": [
                        *(record.get("events") or []),
                        self._event(
                            "partial" if page_result.get("is_partial") is True else "completed",
                            (
                                "部分报价和 Excel 已生成"
                                if page_result.get("is_partial") is True
                                else "报价结果和 Excel 已生成"
                            ),
                        ),
                    ][-100:],
                }
            )
            self._write_atomic(job_path, record)
            return record

    @staticmethod
    def _scenario_term_months(value: dict[str, Any]) -> int | None:
        configured = value.get("term_months")
        if isinstance(configured, int) and configured > 0:
            return configured
        return {
            "one_month_subscription": 1,
            "one_year_subscription": 12,
            "one_year_commitment": 12,
            "one_year_all_upfront": 12,
            "three_year_commitment": 36,
            "three_year_all_upfront": 36,
        }.get(str(value.get("scenario_key") or ""))

    @staticmethod
    def _page_result_public(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or value.get("schema_version") != "astraquote-page-result/1":
            return None
        currency = str(value.get("currency") or "")
        if len(currency) != 3 or not currency.isalpha() or currency.upper() != currency:
            return None
        components = value.get("components")
        scenarios = value.get("scenarios")
        if not isinstance(components, list) or not 1 <= len(components) <= 200:
            return None
        if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 3:
            return None
        allowed_scenarios = {
            "on_demand",
            "one_month_subscription",
            "one_year_subscription",
            "one_year_commitment",
            "three_year_commitment",
            "one_year_all_upfront",
            "three_year_all_upfront",
        }
        public_components = []
        for component in components:
            if not isinstance(component, dict):
                return None
            service_name = str(component.get("service_name") or "").strip()
            if not service_name:
                return None
            costs = component.get("scenario_costs")
            if not isinstance(costs, list) or len(costs) > 3:
                return None
            public_costs = []
            for cost in costs:
                if not isinstance(cost, dict) or cost.get("scenario_key") not in allowed_scenarios:
                    return None
                try:
                    monthly = float(cost["monthly_cost"])
                    upfront = float(cost.get("upfront_cost", 0))
                except (KeyError, TypeError, ValueError):
                    return None
                if monthly < 0 or upfront < 0:
                    return None
                public_costs.append(
                    {
                        "scenario_key": cost["scenario_key"],
                        "label": str(cost.get("label") or "")[:40],
                        "term_months": GptQuoteRelayStore._scenario_term_months(cost),
                        "monthly_cost": str(cost["monthly_cost"]),
                        "upfront_cost": str(cost.get("upfront_cost", "0")),
                    }
                )
            public_components.append(
                {
                    "service_name": service_name[:120],
                    "model_or_plan": str(component.get("model_or_plan") or "")[:160],
                    "quantity": str(component.get("quantity") or "")[:80],
                    "configuration_summary": str(
                        component.get("configuration_summary") or ""
                    )[:1200],
                    "scenario_costs": public_costs,
                }
            )
        public_scenarios = []
        for scenario in scenarios:
            if (
                not isinstance(scenario, dict)
                or scenario.get("scenario_key") not in allowed_scenarios
            ):
                return None
            try:
                monthly = float(scenario["monthly_total"])
                upfront = float(scenario["upfront_total"])
            except (KeyError, TypeError, ValueError):
                return None
            if monthly < 0 or upfront < 0:
                return None
            public_scenarios.append(
                {
                    "scenario_key": scenario["scenario_key"],
                    "label": str(scenario.get("label") or "")[:40],
                    "term_months": GptQuoteRelayStore._scenario_term_months(scenario),
                    "monthly_total": str(scenario["monthly_total"]),
                    "upfront_total": str(scenario["upfront_total"]),
                }
            )
        unpriced_components = value.get("unpriced_components") or []
        if not isinstance(unpriced_components, list) or len(unpriced_components) > 200:
            return None
        public_unpriced = []
        allowed_failure_codes = {
            "official_price_unavailable",
            "official_query_failed",
            "unsupported_in_region",
            "retry_limit_reached",
        }
        for component in unpriced_components:
            if not isinstance(component, dict):
                return None
            service_name = str(component.get("service_name") or "").strip()
            failure_code = str(component.get("failure_code") or "")
            if not service_name or failure_code not in allowed_failure_codes:
                return None
            public_component = {
                "service_name": service_name[:120],
                "model_or_plan": str(component.get("model_or_plan") or "")[:160],
                "quantity": str(component.get("quantity") or "")[:80],
                "configuration_summary": str(
                    component.get("configuration_summary") or ""
                )[:1200],
                "failure_code": failure_code,
                "retryable": component.get("retryable") is not False,
            }
            failure_category = str(component.get("failure_category") or "")
            if failure_category in PUBLIC_FAILURE_CATEGORIES:
                public_component["failure_category"] = failure_category
            provider_code = str(component.get("provider_code") or "")
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", provider_code):
                public_component["provider_code"] = provider_code
            public_unpriced.append(public_component)
        is_partial = value.get("is_partial") is True
        if is_partial != bool(public_unpriced):
            return None
        public_result = {
            "schema_version": "astraquote-page-result/1",
            "is_partial": is_partial,
            "currency": currency,
            "region": str(value.get("region") or "")[:32],
            "preferred_region": str(value.get("preferred_region") or "")[:80],
            "region_adjustment_reason": str(
                value.get("region_adjustment_reason") or ""
            )[:500],
            "components": public_components,
            "unpriced_components": public_unpriced,
            "scenarios": public_scenarios,
        }
        if value.get("pricing_notice"):
            public_result["pricing_notice"] = SALES_CACHE_FALLBACK_NOTICE
        return public_result

    @staticmethod
    def _download_url_public(value: Any) -> str | None:
        from urllib.parse import urlsplit

        text = str(value or "").strip()
        try:
            parsed = urlsplit(text)
        except ValueError:
            return None
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            return None
        if not re.fullmatch(
            r"/api/backend/api/quote-artifacts/aqdl_[a-f0-9]{48}", parsed.path
        ):
            return None
        return text

    def update(
        self,
        job_id: str,
        changes: dict[str, Any],
        *,
        stage: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        with self._lock():
            path = self._path(job_id)
            if not path.exists():
                raise GptRelayError("GPT 报价任务不存在。", code="gpt_relay_job_not_found")
            record = self._read(path)
            record.update(changes)
            record["updated_at"] = utc_now()
            if stage and message:
                record["events"] = [
                    *(record.get("events") or []),
                    self._event(stage, message),
                ][-100:]
            self._write_atomic(path, record)
        return record

    def update_if_not_cancelled(
        self,
        job_id: str,
        changes: dict[str, Any],
        *,
        stage: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        """Apply a worker transition without reviving cancellation or delivery."""

        with self._lock():
            path = self._path(job_id)
            if not path.exists():
                raise GptRelayError("GPT 报价任务不存在。", code="gpt_relay_job_not_found")
            record = self._read(path)
            if record.get("status") in {"cancelled", "completed", "partial"}:
                return record
            record.update(changes)
            record["updated_at"] = utc_now()
            if stage and message:
                record["events"] = [
                    *(record.get("events") or []),
                    self._event(stage, message),
                ][-100:]
            self._write_atomic(path, record)
            return record

    def claim_next(
        self,
        worker_id: str,
        lease_minutes: int = 30,
        *,
        engine: str = "chatgpt",
    ) -> dict[str, Any] | None:
        requesting_engine = self._normalize_engine(engine)
        if requesting_engine not in self.enabled_engines:
            return None
        with self._lock():
            now = datetime.now(UTC)
            candidates: list[tuple[str, Path, dict[str, Any]]] = []
            records: list[dict[str, Any]] = []
            for path in self.jobs_directory.glob("gpt-*.json"):
                try:
                    record = self._read(path)
                except (OSError, ValueError):
                    continue
                records.append(record)
                status = record.get("status")
                lease_text = record.get("lease_expires_at")
                expired = False
                if status == "processing" and lease_text:
                    try:
                        expired = datetime.fromisoformat(lease_text) <= now
                    except ValueError:
                        expired = True
                if status == "queued" or expired:
                    candidates.append((str(record.get("created_at") or ""), path, record))
            if not candidates:
                return None
            active_slots, active_has_unplanned = self._active_slots_by_engine(
                records, now
            )
            eligible: list[tuple[str, Path, dict[str, Any]]] = []
            for item in candidates:
                record = item[2]
                required = self._slot_count(record)
                assigned = record.get("assigned_engine")
                preferred = self._normalize_engine(record.get("preferred_engine"))
                if preferred not in self.enabled_engines:
                    preferred = self.enabled_engines[0]
                if assigned:
                    routed_engine = self._normalize_engine(assigned)
                else:
                    engine_order = (
                        preferred,
                        *(candidate for candidate in self.enabled_engines if candidate != preferred),
                    )
                    routed_engine = ""
                    for candidate in engine_order:
                        heartbeat_ready = self._engine_heartbeat_ready(candidate)
                        can_run = (
                            not active_has_unplanned[candidate]
                            and active_slots[candidate] + required
                            <= self.max_concurrent_quotes
                            and (
                                candidate == requesting_engine
                                or heartbeat_ready is True
                            )
                            and heartbeat_ready is not False
                        )
                        if can_run:
                            routed_engine = candidate
                            break
                if routed_engine != requesting_engine:
                    continue
                if active_has_unplanned[requesting_engine]:
                    continue
                if (
                    active_slots[requesting_engine] + required
                    > self.max_concurrent_quotes
                ):
                    continue
                eligible.append(item)
            if not eligible:
                return None
            _, path, record = min(eligible, key=lambda item: item[0])
            record.update(
                {
                    "status": "processing",
                    "assigned_engine": requesting_engine,
                    "worker_id": worker_id,
                    "processing_started_at": record.get("processing_started_at") or utc_now(),
                    "lease_expires_at": (now + timedelta(minutes=lease_minutes)).isoformat(),
                    "updated_at": utc_now(),
                    "events": [
                        *(record.get("events") or []),
                        self._event("browser", "报价任务已开始处理"),
                    ][-100:],
                }
            )
            self._write_atomic(path, record)
            return record

    def renew_lease(
        self, job_id: str, worker_id: str, *, lease_minutes: int = 35
    ) -> dict[str, Any]:
        """Keep a genuinely active browser tab from being reclaimed as abandoned."""

        with self._lock():
            path = self._path(job_id)
            record = self._read(path)
            if record.get("status") != "processing" or record.get("worker_id") != worker_id:
                return record
            now = datetime.now(UTC)
            try:
                expires = datetime.fromisoformat(str(record.get("lease_expires_at") or ""))
            except ValueError:
                expires = now
            if expires - now > timedelta(minutes=max(1, lease_minutes // 2)):
                return record
            record["lease_expires_at"] = (now + timedelta(minutes=lease_minutes)).isoformat()
            record["updated_at"] = utc_now()
            self._write_atomic(path, record)
            return record

    def claim_submitted_for_monitoring(
        self,
        worker_id: str,
        *,
        limit: int | None,
        lease_minutes: int = 30,
        exclude_job_ids: set[str] | None = None,
        engine: str = "chatgpt",
    ) -> list[dict[str, Any]]:
        """Reattach submitted conversations after the browser worker restarts.

        At least one conversation has already been submitted, so it must be
        monitored by its saved reference instead of submitting the first batch
        again. A very large intake may still have later unsent private batches.
        """

        if limit is not None and limit < 1:
            return []
        claimed: list[dict[str, Any]] = []
        requesting_engine = self._normalize_engine(engine)
        excluded = exclude_job_ids or set()
        with self._lock():
            candidates: list[tuple[str, Path, dict[str, Any]]] = []
            for path in self.jobs_directory.glob("gpt-*.json"):
                try:
                    record = self._read(path)
                except (OSError, ValueError):
                    continue
                if (
                    record.get("status") == "processing"
                    and self._record_engine(record) == requesting_engine
                    and record.get("job_id") not in excluded
                    and (
                        str(record.get("chat_url") or "").strip()
                        or bool(record.get("chat_sessions"))
                    )
                ):
                    candidates.append((str(record.get("created_at") or ""), path, record))
            now = datetime.now(UTC)
            ordered = sorted(candidates, key=lambda item: item[0])
            selected = ordered if limit is None else ordered[:limit]
            for _, path, record in selected:
                record.update(
                    {
                        "worker_id": worker_id,
                        "lease_expires_at": (now + timedelta(minutes=lease_minutes)).isoformat(),
                        "updated_at": utc_now(),
                        "events": [
                            *(record.get("events") or []),
                            self._event("browser", "报价监控已自动恢复"),
                        ][-100:],
                    }
                )
                self._write_atomic(path, record)
                claimed.append(record)
        return claimed

    def purge_source(self, job_id: str) -> dict[str, Any]:
        return self.update(
            job_id,
            {"customer_request": "", "source_purged_at": utc_now()},
            stage="intake",
            message="报价需求已进入安全处理阶段",
        )

    def purge_intake_batch(self, job_id: str, batch_index: int) -> dict[str, Any]:
        """Remove one batch's raw sales lines immediately after successful send."""

        with self._lock():
            path = self._path(job_id)
            record = self._read(path)
            batches = list(record.get("intake_batches") or [])
            target = next(
                (
                    item
                    for item in batches
                    if int(item.get("batch_index", -1)) == int(batch_index)
                ),
                None,
            )
            if target is None:
                raise GptRelayError(
                    "报价输入批次不存在。", code="gpt_relay_intake_batch_not_found"
                )
            target["source_lines"] = []
            target["status"] = "submitted"
            target["submitted_at"] = target.get("submitted_at") or utc_now()
            all_purged = all(not (item.get("source_lines") or []) for item in batches)
            record.update(
                {
                    "customer_request": "",
                    "intake_batches": batches,
                    "source_purged_at": (
                        record.get("source_purged_at") or utc_now()
                        if all_purged
                        else None
                    ),
                    "updated_at": utc_now(),
                }
            )
            self._write_atomic(path, record)
            return record

    def authorize_merge(self, job_id: str) -> dict[str, Any]:
        """Allow one final build only after the desktop worker stops every batch."""

        return self.update_if_not_cancelled(
            job_id,
            {"merge_authorized": True},
            stage="merge",
            message="所有组件批次已停止，正在统一合并报价",
        )

    @staticmethod
    def _purged_intake_batches(record: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                **item,
                "source_lines": [],
                "status": "cancelled",
            }
            for item in (record.get("intake_batches") or [])
            if isinstance(item, dict)
        ]

    def purge_all_sources(self, job_id: str) -> dict[str, Any]:
        record = self.get(job_id)
        return self.update_if_not_cancelled(
            job_id,
            {
                "customer_request": "",
                "intake_batches": self._purged_intake_batches(record),
                "source_purged_at": utc_now(),
            },
        )

    def cancel(self, job_id: str) -> dict[str, Any]:
        record = self.get(job_id)
        if record.get("status") in TERMINAL_STATUSES:
            return self.public(record)
        updated = self.update(
            job_id,
            {
                "status": "cancelled",
                "customer_request": "",
                "intake_batches": self._purged_intake_batches(record),
                "source_purged_at": utc_now(),
            },
            stage="cancelled",
            message="报价任务已取消",
        )
        return self.public(updated)

    def retry_partial(self, job_id: str) -> dict[str, Any]:
        """Resume only the unpriced part of a previously delivered partial quote."""

        with self._lock():
            path = self._path(job_id)
            if not path.exists():
                raise GptRelayError("GPT 报价任务不存在。", code="gpt_relay_job_not_found")
            record = self._read(path)
            result = record.get("quick_quote_result") or {}
            retryable = [
                item
                for item in (result.get("unpriced_components") or [])
                if isinstance(item, dict) and item.get("retryable") is not False
            ]
            if record.get("status") != "partial" or not retryable:
                raise GptRelayError(
                    "当前报价没有可重试的未完成组件。",
                    code="gpt_relay_partial_retry_unavailable",
                )
            now = utc_now()
            record.update(
                {
                    "status": "queued",
                    "worker_id": None,
                    "lease_expires_at": None,
                    "partial_retry_requested_at": now,
                    "partial_retry_pending": True,
                    "partial_retry_generation": int(
                        record.get("partial_retry_generation") or 0
                    )
                    + 1,
                    "partial_finalization_requested": False,
                    "continuation_attempts": 0,
                    "stalled_continuation_attempts": 0,
                    "last_progress_fingerprint": None,
                    "updated_at": now,
                    "events": [
                        *(record.get("events") or []),
                        self._event("retry", "仅重试未完成组件"),
                    ][-100:],
                }
            )
            self._write_atomic(path, record)
            return self.public(record)

    def resume_login_waiting(self, engine: str | None = None) -> int:
        """Return login-blocked jobs to the queue after the browser signs in."""
        resumed = 0
        normalized_engine = self._normalize_engine(engine) if engine else None
        with self._lock():
            for path in self.jobs_directory.glob("gpt-*.json"):
                try:
                    record = self._read(path)
                except (OSError, ValueError):
                    continue
                if record.get("status") != "needs_login":
                    continue
                if normalized_engine and self._normalize_engine(
                    record.get("assigned_engine") or record.get("preferred_engine")
                ) != normalized_engine:
                    continue
                has_submitted_chat = bool(
                    record.get("chat_url") or record.get("chat_sessions")
                )
                record.update(
                    {
                        "status": "processing" if has_submitted_chat else "queued",
                        "worker_id": None,
                        "lease_expires_at": None,
                        "updated_at": utc_now(),
                        "events": [
                            *(record.get("events") or []),
                            self._event("login", "报价服务已恢复，任务自动继续"),
                        ][-100:],
                    }
                )
                self._write_atomic(path, record)
                resumed += 1
        return resumed

    def mark_queued_needs_login(self, engine: str | None = None) -> int:
        """Expose an expired browser login without dropping queued source text."""
        marked = 0
        normalized_engine = self._normalize_engine(engine) if engine else None
        with self._lock():
            for path in self.jobs_directory.glob("gpt-*.json"):
                try:
                    record = self._read(path)
                except (OSError, ValueError):
                    continue
                if record.get("status") != "queued":
                    continue
                if normalized_engine and self._normalize_engine(
                    record.get("assigned_engine") or record.get("preferred_engine")
                ) != normalized_engine:
                    continue
                record.update(
                    {
                        "status": "needs_login",
                        "worker_id": None,
                        "lease_expires_at": None,
                        "updated_at": utc_now(),
                        "events": [
                            *(record.get("events") or []),
                            self._event("login", "报价服务暂时不可用，等待管理员处理"),
                        ][-100:],
                    }
                )
                self._write_atomic(path, record)
                marked += 1
        return marked

    def health(self) -> dict[str, Any]:
        engines: dict[str, dict[str, Any]] = {}
        latest_updated_at: str | None = None
        for engine in self.enabled_engines:
            path = self.heartbeat_path_for(engine)
            heartbeat: dict[str, Any] = {}
            ready = False
            if path.exists():
                try:
                    heartbeat = self._read(path)
                    updated = datetime.fromisoformat(str(heartbeat["updated_at"]))
                    ready = (
                        datetime.now(UTC) - updated < timedelta(seconds=45)
                        and bool(heartbeat.get("logged_in"))
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    ready = False
            updated_text = str(heartbeat.get("updated_at") or "") or None
            if updated_text and (latest_updated_at is None or updated_text > latest_updated_at):
                latest_updated_at = updated_text
            engines[engine] = {
                "status": "ready" if ready else "offline",
                "max_concurrent_quotes": self.max_concurrent_quotes,
            }
        ready = any(item["status"] == "ready" for item in engines.values())
        return {
            "status": "ready" if ready else "offline",
            "message": "服务正常" if ready else "报价服务暂时不可用",
            "updated_at": latest_updated_at,
            "engines": engines,
        }
