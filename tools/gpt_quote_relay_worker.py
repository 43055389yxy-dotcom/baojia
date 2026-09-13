"""Drive one logged-in quote engine across isolated sales quote conversations.

The worker keeps one stable engine-specific reference per active quote and
visits those conversations in a round-robin loop; generation continues remotely
while another conversation is shown. Each worker owns one assigned engine.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOLS_DIRECTORY = Path(__file__).resolve().parent
if str(TOOLS_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIRECTORY))

from app.services.gpt_browser_navigation import (
    active_quote_poll_order,
    bounded_continuation_attempts,
    is_transient_browser_poll_exception,
)
from app.services.gpt_quote_batches import (
    WAVES_PER_CHAT,
    build_component_batch_continuation_prompt,
    build_component_batch_prompt,
    build_numbered_intake_batch_prompt,
    build_quote_merge_prompt,
)
from app.services.gpt_quote_prompt import (
    build_quote_context_prompt,
    build_quote_continuation_prompt,
    build_quote_failed_components_retry_prompt,
    build_quote_partial_finalization_prompt,
    build_quote_prompt,
    parse_final_response,
)
from app.services.gpt_quote_relay import (
    MAX_ACTIVE_CHATS_PER_SALES_JOB,
    GptQuoteRelayStore,
    utc_now,
)

RELAY_ENGINE = os.environ.get("ASTRAQUOTE_RELAY_ENGINE", "chatgpt").strip().lower()
if RELAY_ENGINE not in {"chatgpt", "gemini"}:
    raise RuntimeError("ASTRAQUOTE_RELAY_ENGINE must be chatgpt or gemini")


class PendingPromptSubmissionError(RuntimeError):
    """Keep a browser-side draft pending without failing the quote."""


if RELAY_ENGINE == "gemini":
    from gemini_chat_browser import GeminiChatBrowser
else:
    from codex_chat_desktop import (
        CodexChatDesktop,
        is_pending_chat_reference,
    )
    from codex_chat_desktop import (
        PendingPromptSubmissionError as CodexPendingPromptSubmissionError,
    )

    PendingPromptSubmissionError = CodexPendingPromptSubmissionError

POLL_SECONDS = float(os.environ.get("ASTRAQUOTE_GPT_RELAY_POLL_SECONDS", "4"))
QUOTE_TIMEOUT_SECONDS = int(os.environ.get("ASTRAQUOTE_GPT_QUOTE_TIMEOUT", "600"))
BATCH_STATE_SETTLE_SECONDS = max(
    POLL_SECONDS * 2,
    float(os.environ.get("ASTRAQUOTE_GPT_BATCH_STATE_SETTLE_SECONDS", "12")),
)
MAX_CONTINUATION_ATTEMPTS = bounded_continuation_attempts(
    os.environ.get("ASTRAQUOTE_GPT_MAX_CONTINUATIONS")
)
WORKER_ID = f"{RELAY_ENGINE}-{socket.gethostname()}-{os.getpid()}"


def is_pending_browser_reference(reference: str) -> bool:
    return RELAY_ENGINE == "chatgpt" and is_pending_chat_reference(reference)


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def write_heartbeat(
    store: GptQuoteRelayStore,
    *,
    logged_in: bool,
    message: str,
    browser: str,
) -> None:
    atomic_json(
        store.heartbeat_path_for(RELAY_ENGINE),
        {
            "updated_at": utc_now(),
            "worker_id": WORKER_ID,
            "browser": browser,
            "logged_in": logged_in,
            "project_name": None,
            "message": message,
        },
    )


@dataclass
class ActiveQuote:
    job_id: str
    chat_url: str
    deadline: float
    last_text: str = ""
    stable_since: float = 0.0
    saw_assistant: bool = False
    retry_visible_since: float | None = None
    retry_clicked: bool = False
    minimum_assistant_messages: int = 0
    generation_grace_used: bool = False
    batch_index: int = 0
    batch_count: int = 1
    conversation_index: int | None = None
    wave_index: int = 0
    wave_count: int = 1
    role: str = "coordinator"
    component_keys: tuple[str, ...] = ()
    batch_progress_fingerprint: str = ""
    machine_progress_fingerprint: str = ""
    stalled_attempts: int = 0
    previous_conversation_ids: tuple[str, ...] = ()
    backend_settle_fingerprint: str = ""
    backend_settle_started_at: float = 0.0
    last_polled_at: float = 0.0

    @property
    def session_key(self) -> str:
        conversation_index = (
            self.batch_index
            if self.conversation_index is None
            else self.conversation_index
        )
        return f"{self.job_id}:{conversation_index}"


def mark_needs_login(store: GptQuoteRelayStore, record: dict[str, Any]) -> None:
    has_submitted_chat = bool(record.get("chat_url") or record.get("chat_sessions"))
    store.update_if_not_cancelled(
        record["job_id"],
        {
            "status": "needs_login" if has_submitted_chat else "queued",
            "assigned_engine": record.get("assigned_engine") if has_submitted_chat else None,
            "worker_id": None,
            "lease_expires_at": None,
        },
        stage="login",
        message=f"{engine_display_name()} 登录已失效，等待管理员在运维桌面重新登录",
    )


def engine_display_name() -> str:
    return "Gemini" if RELAY_ENGINE == "gemini" else "Codex Chat"


def submit_job(
    store: GptQuoteRelayStore,
    browser: Any,
    record: dict[str, Any],
) -> ActiveQuote | None:
    job_id = record["job_id"]
    if not browser.logged_in():
        mark_needs_login(store, record)
        return None
    intake_batches = store.intake_chat_batches(job_id)
    if intake_batches:
        first_batch = intake_batches[0]
        source_lines = list(first_batch.get("source_lines") or [])
        if not source_lines:
            store.update_if_not_cancelled(
                job_id,
                {
                    "status": "failed",
                    "error": {
                        "code": "source_missing",
                        "message": "首批待清洗客户需求已不存在。",
                    },
                },
                stage="failed",
                message="客户需求缺失，任务已安全停止",
            )
            return None
        prompt = build_numbered_intake_batch_prompt(
            relay_job_id=job_id,
            submission_code=str(record.get("submission_code") or "").strip(),
            price_batch_id=str(record.get("reserved_price_batch_id") or ""),
            batch_index=0,
            batch_count=int(record.get("intake_batch_count") or len(intake_batches)),
            components=source_lines,
            quote_context=build_quote_context_prompt(record.get("quote_options") or {}),
        )
        active = browser.start_quote(job_id, prompt)
        active.batch_count = int(record.get("intake_batch_count") or len(intake_batches))
        active.conversation_index = int(first_batch.get("conversation_index") or 0)
        active.wave_index = int(first_batch.get("wave_index") or 0)
        active.wave_count = int(first_batch.get("wave_count") or 1)
        active.component_keys = tuple(first_batch.get("component_keys") or [])
        store.record_chat_session(
            job_id,
            batch_index=0,
            batch_count=active.batch_count,
            chat_url=active.chat_url,
            role="coordinator",
            component_keys=list(active.component_keys),
            previous_conversation_ids=list(active.previous_conversation_ids),
            conversation_index=active.conversation_index,
            wave_index=active.wave_index,
            wave_count=active.wave_count,
        )
        store.update_if_not_cancelled(
            job_id,
            {"project_name": None},
            stage="submitted",
            message="已创建首个组件批次报价",
        )
        store.purge_intake_batch(job_id, 0)
        return active
    customer_request = str(record.get("customer_request") or "").strip()
    if not customer_request:
        store.update_if_not_cancelled(
            job_id,
            {
                "status": "failed",
                "error": {
                    "code": "source_missing",
                    "message": "待清洗客户需求已不存在。",
                },
            },
            stage="failed",
            message="客户需求缺失，任务已安全停止",
        )
        return None

    latest = store.get(job_id)
    if latest.get("status") == "cancelled":
        return None
    prompt = build_quote_prompt(
        customer_request,
        record.get("quote_options") or {},
        relay_job_id=job_id,
        submission_code=str(record.get("submission_code") or "").strip(),
    )
    active = browser.start_quote(job_id, prompt)
    store.record_chat_session(
        job_id,
        batch_index=0,
        batch_count=1,
        chat_url=active.chat_url,
        role="coordinator",
        component_keys=[],
        previous_conversation_ids=list(active.previous_conversation_ids),
    )
    store.update_if_not_cancelled(
        job_id, {"project_name": None}, stage="submitted",
        message=f"已在 {engine_display_name()} 独立任务中创建总控报价并提交需求清洗",
    )
    store.purge_source(job_id)
    return active


def batch_progress_fingerprint(batch: dict[str, Any], previous: str = "") -> str:
    try:
        prior = json.loads(previous or "[]")
    except ValueError:
        prior = []
    if isinstance(prior, dict):
        prior = [key for key, state in prior.items() if state == "completed"]
    completed = {
        key for key, state in (batch.get("component_states") or {}).items()
        if state == "completed"
    }
    return json.dumps(
        sorted(set(prior) | completed),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def batch_is_finished(batch: dict[str, Any]) -> bool:
    component_keys = list(batch.get("component_keys") or [])
    states = batch.get("component_states") or {}
    return bool(component_keys) and all(
        states.get(component_key) == "completed" for component_key in component_keys
    )


def incomplete_component_keys(batch: dict[str, Any]) -> list[str]:
    """Return backend-owned component keys that still need one attempt."""

    states = batch.get("component_states") or {}
    return [
        key for key in batch.get("component_keys") or []
        if states.get(key) != "completed"
    ]


def component_batch_state_fingerprint(
    batch: dict[str, Any],
    previous_progress: str = "",
) -> str:
    """Describe only durable success and the remaining owned component keys."""

    progress = batch_progress_fingerprint(batch, previous_progress)
    completed = set(json.loads(progress))
    return json.dumps(
        {
            "completed": sorted(completed),
            "remaining": [
                key for key in batch.get("component_keys") or []
                if key not in completed
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def active_batch(
    store: GptQuoteRelayStore,
    active: ActiveQuote,
) -> dict[str, Any] | None:
    return next(
        (
            batch
            for batch in store.quote_chat_batches(active.job_id)
            if int(batch["batch_index"]) == active.batch_index
        ),
        None,
    )


def refresh_progress_deadline(store: GptQuoteRelayStore, active: ActiveQuote) -> None:
    """Only saved successful work extends the no-progress deadline."""

    batch = active_batch(store, active) if active.batch_count > 1 else None
    if batch is not None and active.role != "merge":
        fingerprint = batch_progress_fingerprint(batch, active.machine_progress_fingerprint)
    else:
        fingerprint = store._merge_progress(
            active.machine_progress_fingerprint,
            store.progress_fingerprint(active.job_id),
        )
    if fingerprint != active.machine_progress_fingerprint:
        active.machine_progress_fingerprint = fingerprint
        active.deadline = time.monotonic() + QUOTE_TIMEOUT_SECONDS
        active.generation_grace_used = False


def component_batch_state_settled(
    store: GptQuoteRelayStore,
    active: ActiveQuote,
    *,
    now: float | None = None,
) -> bool:
    """Wait for delayed backend writes before spending the batch's sole retry."""

    batch = active_batch(store, active)
    if batch is None or batch_is_finished(batch):
        return True
    if active.stalled_attempts >= MAX_CONTINUATION_ATTEMPTS:
        return True
    observed_at = time.monotonic() if now is None else now
    progress = batch_progress_fingerprint(batch, active.batch_progress_fingerprint)
    active.batch_progress_fingerprint = progress
    fingerprint = component_batch_state_fingerprint(batch, progress)
    if fingerprint != active.backend_settle_fingerprint:
        active.backend_settle_fingerprint = fingerprint
        active.backend_settle_started_at = observed_at
        store.update_chat_session(
            active.job_id,
            active.batch_index,
            progress_fingerprint=progress,
            backend_settle_fingerprint=fingerprint,
            backend_settle_started_at_epoch=time.time(),
        )
        return False
    return observed_at - active.backend_settle_started_at >= BATCH_STATE_SETTLE_SECONDS


def continue_component_batch(
    store: GptQuoteRelayStore,
    browser: Any,
    active: ActiveQuote,
) -> bool:
    """Continue a child batch once only while its machine state is unchanged."""

    batch = active_batch(store, active)
    if batch is None:
        return False
    fingerprint = batch_progress_fingerprint(batch, active.batch_progress_fingerprint)
    if fingerprint != active.batch_progress_fingerprint:
        active.batch_progress_fingerprint = fingerprint
    if batch_is_finished(batch):
        return advance_to_next_wave(
            store,
            browser,
            active,
            completed_status="saved",
        )
    if active.stalled_attempts >= MAX_CONTINUATION_ATTEMPTS:
        return advance_to_next_wave(
            store,
            browser,
            active,
            completed_status="stalled",
        )
    latest = store.get(active.job_id)
    completed_component_keys = set(json.loads(fingerprint))
    remaining_component_keys = [
        key for key in batch.get("component_keys") or []
        if key not in completed_component_keys
    ]
    if not remaining_component_keys:
        return advance_to_next_wave(
            store,
            browser,
            active,
            completed_status="saved",
        )
    browser.continue_quote(
        active,
        build_component_batch_continuation_prompt(
            relay_job_id=active.job_id,
            submission_code=str(latest.get("submission_code") or ""),
            price_batch_id=str(batch["price_batch_id"]),
            batch_index=active.batch_index,
            batch_count=int(batch["batch_count"]),
            component_keys=remaining_component_keys,
        ),
    )
    # The desktop adapter returns only after it sees the new user turn. A UI
    # draft that was not submitted therefore never consumes the one content
    # retry allowed for this five-component wave.
    active.stalled_attempts += 1
    store.update_chat_session(
        active.job_id,
        active.batch_index,
        status="running",
        stalled_attempts=active.stalled_attempts,
        progress_fingerprint=fingerprint,
        backend_settle_fingerprint=None,
        backend_settle_started_at_epoch=None,
    )
    active.backend_settle_fingerprint = ""
    active.backend_settle_started_at = 0.0
    return True


def advance_to_next_wave(
    store: GptQuoteRelayStore,
    browser: Any,
    active: ActiveQuote,
    *,
    completed_status: str,
) -> bool:
    """Send the second five items in the same conversation, if present."""

    intake_batches = store.intake_chat_batches(active.job_id)
    next_batch_index = active.batch_index + 1
    next_batch = next(
        (
            item
            for item in intake_batches
            if int(item.get("batch_index", -1)) == next_batch_index
            and int(item.get("conversation_index", -1))
            == int(active.conversation_index or 0)
        ),
        None,
    )
    source_lines = list(next_batch.get("source_lines") or []) if next_batch else []
    if not source_lines:
        store.update_chat_session(
            active.job_id,
            active.batch_index,
            status=completed_status,
            stalled_attempts=active.stalled_attempts,
            backend_settle_fingerprint=None,
            backend_settle_started_at_epoch=None,
        )
        return False

    record = store.get(active.job_id)
    prompt = build_numbered_intake_batch_prompt(
        relay_job_id=active.job_id,
        submission_code=str(record.get("submission_code") or ""),
        price_batch_id=str(record.get("reserved_price_batch_id") or ""),
        batch_index=next_batch_index,
        batch_count=int(record.get("intake_batch_count") or len(intake_batches)),
        components=source_lines,
        quote_context=build_quote_context_prompt(record.get("quote_options") or {}),
    )
    browser.continue_quote(active, prompt)
    store.update_chat_session(
        active.job_id,
        active.batch_index,
        status=completed_status,
        stalled_attempts=active.stalled_attempts,
        backend_settle_fingerprint=None,
        backend_settle_started_at_epoch=None,
    )
    store.record_chat_session(
        active.job_id,
        batch_index=next_batch_index,
        batch_count=int(record.get("intake_batch_count") or len(intake_batches)),
        chat_url=active.chat_url,
        role="component_batch",
        component_keys=list(next_batch.get("component_keys") or []),
        previous_conversation_ids=list(active.previous_conversation_ids),
        conversation_index=active.conversation_index,
        wave_index=int(next_batch.get("wave_index") or 0),
        wave_count=int(next_batch.get("wave_count") or 1),
    )
    store.purge_intake_batch(active.job_id, next_batch_index)
    active.batch_index = next_batch_index
    active.wave_index = int(next_batch.get("wave_index") or 0)
    active.wave_count = int(next_batch.get("wave_count") or 1)
    active.role = "component_batch"
    active.component_keys = tuple(next_batch.get("component_keys") or [])
    active.stalled_attempts = 0
    active.batch_progress_fingerprint = ""
    active.machine_progress_fingerprint = ""
    active.backend_settle_fingerprint = ""
    active.backend_settle_started_at = 0.0
    return True


def advance_machine_completed_wave(
    store: GptQuoteRelayStore,
    browser: Any,
    active_quotes: dict[str, ActiveQuote],
) -> bool:
    """Advance one backend-complete wave without waiting for assistant prose.

    The function deliberately inspects at most one visible conversation.  This
    both prioritizes a ready second wave and prevents one desktop window from
    bouncing through every active conversation in a single relay tick.
    """

    # Only the overall least-recently inspected conversation may take this
    # fast path.  An unusually long final response in one completed wave must
    # not starve permission checks and progress reads in its sibling chats.
    for session_key in active_quote_poll_order(active_quotes, limit=1):
        active = active_quotes.get(session_key)
        if (
            active is None
            or active.batch_count <= 1
            or active.role == "merge"
        ):
            continue
        batch = active_batch(store, active)
        if batch is None or not batch_is_finished(batch):
            continue
        active.last_polled_at = time.monotonic()
        current = store.reconcile_delivery_receipt(active.job_id)
        if current.get("status") in {"completed", "partial", "failed"}:
            stop_terminal_job_chats(browser, active_quotes, active.job_id)
            return True
        if current.get("status") == "cancelled":
            stop_terminal_job_chats(
                browser, active_quotes, active.job_id, cancelled=True,
            )
            return True
        try:
            store.renew_lease(active.job_id, WORKER_ID, lease_minutes=35)
            previous_reference = active.chat_url
            ready = getattr(browser, "ready_for_next_turn", None)
            can_continue = bool(ready(active)) if callable(ready) else True
            persist_promoted_chat_reference(store, active, previous_reference)
            if not can_continue:
                return True
            if not continue_component_batch(store, browser, active):
                active_quotes.pop(session_key, None)
            maybe_start_final_merge(
                store, browser, active_quotes, active.job_id,
            )
        except PendingPromptSubmissionError:
            active.stable_since = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - isolate this visible quote
            if is_transient_browser_poll_exception(exc):
                reconnect = getattr(browser, "reconnect", None)
                if callable(reconnect):
                    try:
                        reconnect()
                    except Exception:  # noqa: BLE001,S110 - retry next tick
                        pass
            else:
                fail_job(store, active.job_id, exc)
                try:
                    browser.close_quote(active)
                except Exception:  # noqa: BLE001,S110 - terminal cleanup is best effort
                    pass
                active_quotes.pop(session_key, None)
        return True
    return False


def create_missing_component_chats(
    store: GptQuoteRelayStore,
    browser: Any,
    active_quotes: dict[str, ActiveQuote],
    job_id: str,
) -> None:
    """Open pending component chats through the one shared desktop window."""

    record = store.get(job_id)
    if record.get("status") != "processing":
        return
    intake_batches = store.intake_chat_batches(job_id)
    if len(intake_batches) > 1:
        sessions = {
            int(item.get("batch_index", -1)): item
            for item in (record.get("chat_sessions") or [])
        }
        # If the worker restarted after finishing the first five but before it
        # could send the second five, recover the saved conversation and send
        # the pending second wave there. Never open a replacement chat.
        for intake_batch in intake_batches:
            batch_index = int(intake_batch["batch_index"])
            conversation_index = int(
                intake_batch.get("conversation_index", batch_index // WAVES_PER_CHAT)
            )
            wave_index = int(
                intake_batch.get("wave_index", batch_index % WAVES_PER_CHAT)
            )
            if wave_index != 1 or batch_index in sessions:
                continue
            if not (intake_batch.get("source_lines") or []):
                continue
            previous = sessions.get(batch_index - 1)
            if not previous or previous.get("status") not in {"saved", "stalled"}:
                continue
            if any(
                quote.job_id == job_id
                and int(quote.conversation_index or 0) == conversation_index
                for quote in active_quotes.values()
            ):
                continue
            if len(active_quotes) >= store.max_concurrent_quotes:
                break
            if sum(
                quote.job_id == job_id for quote in active_quotes.values()
            ) >= MAX_ACTIVE_CHATS_PER_SALES_JOB:
                break
            active = browser.resume_quote(
                job_id,
                str(previous["chat_url"]),
                batch_index=batch_index - 1,
                batch_count=int(record.get("intake_batch_count") or len(intake_batches)),
                role=str(previous.get("role") or "component_batch"),
                component_keys=list(previous.get("component_keys") or []),
                previous_conversation_ids=list(
                    previous.get("previous_conversation_ids") or []
                ),
                conversation_index=conversation_index,
                wave_index=0,
                wave_count=int(intake_batch.get("wave_count") or WAVES_PER_CHAT),
            )
            if advance_to_next_wave(
                store,
                browser,
                active,
                completed_status=str(previous.get("status") or "saved"),
            ):
                active_quotes[active.session_key] = active
            if is_pending_browser_reference(active.chat_url):
                break

        for intake_batch in intake_batches[1:]:
            batch_index = int(intake_batch["batch_index"])
            conversation_index = int(
                intake_batch.get("conversation_index", batch_index // WAVES_PER_CHAT)
            )
            if int(intake_batch.get("wave_index", batch_index % WAVES_PER_CHAT)) != 0:
                continue
            if batch_index in sessions or not (intake_batch.get("source_lines") or []):
                continue
            if len(active_quotes) >= store.max_concurrent_quotes:
                break
            if sum(
                quote.job_id == job_id for quote in active_quotes.values()
            ) >= MAX_ACTIVE_CHATS_PER_SALES_JOB:
                break
            prompt = build_numbered_intake_batch_prompt(
                relay_job_id=job_id,
                submission_code=str(record.get("submission_code") or ""),
                price_batch_id=str(record.get("reserved_price_batch_id") or ""),
                batch_index=batch_index,
                batch_count=int(record.get("intake_batch_count") or len(intake_batches)),
                components=list(intake_batch.get("source_lines") or []),
                quote_context=build_quote_context_prompt(record.get("quote_options") or {}),
            )
            active = browser.start_component_batch(
                job_id,
                prompt,
                batch_index=batch_index,
                batch_count=int(record.get("intake_batch_count") or len(intake_batches)),
                component_keys=list(intake_batch.get("component_keys") or []),
            )
            active.conversation_index = conversation_index
            active.wave_index = int(intake_batch.get("wave_index") or 0)
            active.wave_count = int(intake_batch.get("wave_count") or 1)
            store.record_chat_session(
                job_id,
                batch_index=batch_index,
                batch_count=int(record.get("intake_batch_count") or len(intake_batches)),
                chat_url=active.chat_url,
                role="component_batch",
                component_keys=list(intake_batch.get("component_keys") or []),
                previous_conversation_ids=list(active.previous_conversation_ids),
                conversation_index=active.conversation_index,
                wave_index=active.wave_index,
                wave_count=active.wave_count,
            )
            store.purge_intake_batch(job_id, batch_index)
            active_quotes[active.session_key] = active
            if is_pending_browser_reference(active.chat_url):
                break
        return

    batches = store.quote_chat_batches(job_id)
    if len(batches) <= 1:
        return
    sessions = {
        int(item.get("batch_index", -1)): item
        for item in (record.get("chat_sessions") or [])
    }
    coordinator = sessions.get(0)
    if coordinator:
        store.update_chat_session(
            job_id, 0,
            batch_count=len(batches),
            component_keys=list(batches[0]["component_keys"]),
        )
        current = active_quotes.get(f"{job_id}:0")
        if current is not None:
            current.batch_count = len(batches)
            current.component_keys = tuple(batches[0]["component_keys"])

    for batch in batches[1:]:
        batch_index = int(batch["batch_index"])
        if batch_is_finished(batch) or batch_index in sessions:
            continue
        if len(active_quotes) >= store.max_concurrent_quotes:
            break
        if sum(
            quote.job_id == job_id for quote in active_quotes.values()
        ) >= MAX_ACTIVE_CHATS_PER_SALES_JOB:
            break
        prompt = build_component_batch_prompt(
            relay_job_id=job_id,
            submission_code=str(record.get("submission_code") or ""),
            price_batch_id=str(batch["price_batch_id"]),
            batch_index=batch_index,
            batch_count=int(batch["batch_count"]),
            components=list(batch["components"]),
            quote_context=build_quote_context_prompt(record.get("quote_options") or {}),
        )
        active = browser.start_component_batch(
            job_id,
            prompt,
            batch_index=batch_index,
            batch_count=int(batch["batch_count"]),
            component_keys=list(batch["component_keys"]),
        )
        active.conversation_index = batch_index // WAVES_PER_CHAT
        active.wave_index = batch_index % WAVES_PER_CHAT
        active.wave_count = WAVES_PER_CHAT
        store.record_chat_session(
            job_id,
            batch_index=batch_index,
            batch_count=int(batch["batch_count"]),
            chat_url=active.chat_url,
            role="component_batch",
            component_keys=list(batch["component_keys"]),
            previous_conversation_ids=list(active.previous_conversation_ids),
            conversation_index=active.conversation_index,
            wave_index=active.wave_index,
            wave_count=active.wave_count,
        )
        active.batch_progress_fingerprint = batch_progress_fingerprint(batch)
        active_quotes[active.session_key] = active
        if is_pending_browser_reference(active.chat_url):
            # Do not leave a just-sent conversation before Codex exposes its
            # stable sidebar id; the model continues running in this chat.
            break


def maybe_start_final_merge(
    store: GptQuoteRelayStore,
    browser: Any,
    active_quotes: dict[str, ActiveQuote],
    job_id: str,
) -> None:
    """Return to the coordinator only after every component chat has stopped."""

    batches = store.quote_chat_batches(job_id)
    if len(batches) <= 1:
        return
    record = store.get(job_id)
    if record.get("status") != "processing":
        return
    if any(active.job_id == job_id for active in active_quotes.values()):
        return
    if len(active_quotes) >= store.max_concurrent_quotes:
        return
    sessions = {
        int(item.get("batch_index", -1)): item
        for item in (record.get("chat_sessions") or [])
    }
    for batch in batches:
        if batch_is_finished(batch):
            continue
        session = sessions.get(int(batch["batch_index"]))
        if not session or session.get("status") != "stalled":
            return
    if any(item.get("status") == "merging" for item in sessions.values()):
        return
    if any(item.get("status") == "running" for item in sessions.values()):
        return
    coordinator = sessions.get(0)
    if not coordinator:
        return
    active = active_quotes.get(f"{job_id}:0")
    if active is None:
        active = browser.resume_quote(
            job_id,
            str(coordinator["chat_url"]),
            batch_index=0,
            batch_count=len(batches),
            role="merge",
            component_keys=list(batches[0]["component_keys"]),
        )
    else:
        active.role = "merge"
    # Persist the merge reservation and keep the resumed coordinator visible
    # before touching the renderer.  A send-button race may leave a valid
    # draft in the composer; without this ordering the next loop re-authorizes
    # and recreates the same merge over and over.
    active.role = "merge"
    active_quotes[active.session_key] = active
    store.update_chat_session(job_id, 0, status="merging")
    store.authorize_merge(job_id)
    browser.continue_quote(
        active,
        build_quote_merge_prompt(
            relay_job_id=job_id,
            submission_code=str(record.get("submission_code") or ""),
            price_batch_id=str(batches[0]["price_batch_id"]),
        ),
    )


def complete_job(store: GptQuoteRelayStore, job_id: str, response: str) -> str:
    current = store.reconcile_delivery_receipt(job_id)
    if current.get("status") in {"cancelled", "completed", "partial", "failed"}:
        return str(current["status"])
    status, summary = parse_final_response(response)
    # A completion sentence is not delivery evidence. Only the receipt above
    # may complete the job. A confirmed blocker skips futile retry messages.
    if current.get("partial_finalization_requested"):
        summary = "部分报价收口仍未产生可用交付回执。"
    elif store.has_unrecoverable_failure(job_id):
        batches = store.quote_chat_batches(job_id)
        can_retry_single_batch = (
            len(batches) == 1
            and bool(incomplete_component_keys(batches[0]))
            and int(current.get("stalled_continuation_attempts") or 0)
            < MAX_CONTINUATION_ATTEMPTS
        )
        if can_retry_single_batch:
            return "continue"
        if store.request_partial_finalization(job_id):
            return "partial_finalize"
        summary = summary or "报价遇到当前无法继续的阻塞。"
    elif status in {"incomplete", "displayed_on_page", "delivered", "blocked"}:
        return "continue"
    store.update_if_not_cancelled(
        job_id,
        {
            "status": "failed",
            "result_summary": summary,
            "error": {"code": "gpt_quote_blocked", "message": summary},
            "lease_expires_at": None,
        },
        stage="failed",
        message="报价无法继续，已保存当前处理结果",
    )
    return "failed"


def fail_continuation_limit(store: GptQuoteRelayStore, job_id: str) -> None:
    store.update_if_not_cancelled(
        job_id,
        {
            "status": "failed",
            "result_summary": "报价引擎补发一次后仍未给出最终完成或明确阻塞状态。",
            "error": {
                "code": "gpt_quote_continuation_limit",
                "message": "报价补发一次后仍未产生最终状态。",
            },
            "lease_expires_at": None,
        },
        stage="failed",
        message="报价补发一次后仍未产生最终状态",
    )


def continue_from_saved_stage(
    store: GptQuoteRelayStore,
    browser: Any,
    active: ActiveQuote,
    *,
    message: str,
) -> bool:
    """Resume one unfinished quote in the same conversation without source text."""

    latest = store.reconcile_delivery_receipt(active.job_id)
    if latest.get("status") != "processing":
        return False
    if latest.get("partial_finalization_requested"):
        fail_continuation_limit(store, active.job_id)
        return False
    batches = store.quote_chat_batches(active.job_id)
    single_batch_remaining = (
        incomplete_component_keys(batches[0]) if len(batches) == 1 else []
    )
    if store.has_unrecoverable_failure(active.job_id) and not single_batch_remaining:
        if store.request_partial_finalization(active.job_id):
            browser.continue_quote(
                active,
                build_quote_partial_finalization_prompt(
                    relay_job_id=active.job_id,
                    submission_code=str(latest.get("submission_code") or ""),
                ),
            )
            return True
        complete_job(store, active.job_id, "")
        return False
    if not store.reserve_continuation(
        active.job_id,
        maximum=MAX_CONTINUATION_ATTEMPTS,
    ):
        if store.request_partial_finalization(active.job_id):
            partial_prompt = build_quote_partial_finalization_prompt(
                relay_job_id=active.job_id,
                submission_code=str(latest.get("submission_code") or ""),
            )
            browser.continue_quote(active, partial_prompt)
            return True
        fail_continuation_limit(store, active.job_id)
        return False
    continuation_prompt = build_quote_continuation_prompt(
        relay_job_id=active.job_id,
        submission_code=str(latest.get("submission_code") or ""),
        component_keys=single_batch_remaining or None,
    )
    browser.continue_quote(active, continuation_prompt)
    store.update_if_not_cancelled(
        active.job_id,
        {},
        stage="continuing",
        message=message,
    )
    return True


def fail_job(store: GptQuoteRelayStore, job_id: str, exc: Exception) -> None:
    current = store.get(job_id)
    if current.get("status") == "cancelled":
        return
    store.purge_all_sources(job_id)
    store.update_if_not_cancelled(
        job_id,
        {
            "status": "failed",
            "customer_request": "",
            "error": {
                "code": f"{RELAY_ENGINE}_automation_failed",
                "message": str(exc)[:1200],
            },
            "lease_expires_at": None,
        },
        stage="failed",
        message=f"{engine_display_name()} 自动化失败，任务已安全停止",
    )


def handle_no_progress_timeout(
    store: GptQuoteRelayStore,
    browser: Any,
    active: ActiveQuote,
    active_quotes: dict[str, ActiveQuote],
) -> None:
    """Use the same durable budget for timeouts and repeated DOM failures."""

    current = store.reconcile_delivery_receipt(active.job_id)
    if current.get("status") in {"completed", "partial", "cancelled", "failed"}:
        browser.close_quote(active)
        active_quotes.pop(active.session_key, None)
        return
    batches = store.quote_chat_batches(active.job_id)
    if len(batches) > 1 and active.role != "merge":
        browser.close_quote(active)
        if not continue_component_batch(store, browser, active):
            active_quotes.pop(active.session_key, None)
        maybe_start_final_merge(store, browser, active_quotes, active.job_id)
        return
    browser.close_quote(active)
    if not continue_from_saved_stage(
        store,
        browser,
        active,
        message="报价等待超时，已在原对话从保存阶段自动继续",
    ):
        browser.close_quote(active)
        active_quotes.pop(active.session_key, None)


def reattach_job_chats(
    store: GptQuoteRelayStore,
    browser: Any,
    record: dict[str, Any],
    active_quotes: dict[str, ActiveQuote],
) -> None:
    """Restore every running logical chat after the desktop worker restarts."""

    sessions = list(record.get("chat_sessions") or [])
    if not sessions and record.get("chat_url"):
        sessions = [{
            "batch_index": 0,
            "batch_count": 1,
            "chat_url": record["chat_url"],
            "role": "coordinator",
            "component_keys": [],
            "status": "running",
        }]
    for session in sessions:
        if record.get("partial_retry_pending"):
            continue
        if session.get("status") not in {"running", "merging"}:
            continue
        batch_index = int(session.get("batch_index") or 0)
        active = browser.resume_quote(
            record["job_id"],
            str(session["chat_url"]),
            batch_index=batch_index,
            batch_count=int(session.get("batch_count") or 1),
            role=("merge" if session.get("status") == "merging" else str(
                session.get("role") or "coordinator"
            )),
            component_keys=list(session.get("component_keys") or []),
            previous_conversation_ids=list(
                session.get("previous_conversation_ids") or []
            ),
            conversation_index=int(
                session.get("conversation_index", batch_index // WAVES_PER_CHAT)
            ),
            wave_index=int(session.get("wave_index", batch_index % WAVES_PER_CHAT)),
            wave_count=int(session.get("wave_count") or 1),
        )
        active.stalled_attempts = int(session.get("stalled_attempts") or 0)
        active.batch_progress_fingerprint = str(
            session.get("progress_fingerprint") or ""
        )
        active.backend_settle_fingerprint = str(
            session.get("backend_settle_fingerprint") or ""
        )
        if active.backend_settle_fingerprint:
            try:
                settle_epoch = float(session.get("backend_settle_started_at_epoch"))
                settle_age = max(0.0, time.time() - settle_epoch)
            except (TypeError, ValueError):
                settle_age = 0.0
            active.backend_settle_started_at = max(
                0.0, time.monotonic() - settle_age,
            )
        active_quotes[active.session_key] = active

    if record.get("partial_retry_pending"):
        coordinator = next(
            (item for item in sessions if int(item.get("batch_index") or 0) == 0),
            None,
        )
        if coordinator:
            active = active_quotes.get(f"{record['job_id']}:0")
            if active is None:
                active = browser.resume_quote(
                    record["job_id"],
                    str(coordinator["chat_url"]),
                    batch_index=0,
                    batch_count=int(coordinator.get("batch_count") or 1),
                    role="merge",
                    component_keys=list(coordinator.get("component_keys") or []),
                )
            browser.continue_quote(
                active,
                build_quote_failed_components_retry_prompt(
                    relay_job_id=record["job_id"],
                    submission_code=str(record.get("submission_code") or ""),
                ),
            )
            active.role = "merge"
            active_quotes[active.session_key] = active
            store.update_chat_session(record["job_id"], 0, status="merging")
            store.update_if_not_cancelled(
                record["job_id"],
                {"partial_retry_pending": False},
                stage="retry",
                message="已在总控对话中仅重试未完成组件",
            )


def stop_terminal_job_chats(
    browser: Any,
    active_quotes: dict[str, ActiveQuote],
    job_id: str,
    *,
    cancelled: bool = False,
) -> None:
    """Stop every still-running chat before releasing its shared slot."""

    for session_key, quote in list(active_quotes.items()):
        if quote.job_id != job_id:
            continue
        try:
            if cancelled:
                browser.cancel_quote(quote)
            else:
                browser.close_quote(quote)
        except Exception:  # noqa: BLE001,S112 - uncertain stop keeps the shared slot
            # Keep the slot and retry on the next poll.  Forgetting a chat on
            # an uncertain stop is what allowed old batches to run overnight.
            continue
        active_quotes.pop(session_key, None)


def pending_active_quote(
    active_quotes: dict[str, ActiveQuote],
) -> ActiveQuote | None:
    return next(
        (
            quote
            for quote in active_quotes.values()
            if is_pending_browser_reference(quote.chat_url)
        ),
        None,
    )


def persist_promoted_chat_reference(
    store: GptQuoteRelayStore,
    active: ActiveQuote,
    previous_reference: str,
) -> None:
    if RELAY_ENGINE != "chatgpt":
        return
    if active.chat_url == previous_reference:
        return
    store.promote_chat_session_reference(
        active.job_id,
        active.batch_index,
        previous_reference,
        active.chat_url,
    )


def main() -> int:
    store = GptQuoteRelayStore()
    browser = (
        GeminiChatBrowser(
            active_quote_factory=ActiveQuote,
            quote_timeout_seconds=QUOTE_TIMEOUT_SECONDS,
        )
        if RELAY_ENGINE == "gemini"
        else CodexChatDesktop(
            active_quote_factory=ActiveQuote,
            quote_timeout_seconds=QUOTE_TIMEOUT_SECONDS,
        )
    )
    active_quotes: dict[str, ActiveQuote] = {}
    while True:
        try:
            if browser.driver is None:
                browser.start()
            logged_in = browser.logged_in()
            if logged_in and not active_quotes:
                for record in store.claim_submitted_for_monitoring(
                    WORKER_ID,
                    limit=None,
                    lease_minutes=35,
                    engine=RELAY_ENGINE,
                ):
                    reattach_job_chats(store, browser, record, active_quotes)
                    create_missing_component_chats(
                        store, browser, active_quotes, record["job_id"],
                    )
                    maybe_start_final_merge(
                        store, browser, active_quotes, record["job_id"],
                    )
            if logged_in:
                store.resume_login_waiting(RELAY_ENGINE)
            write_heartbeat(
                store,
                logged_in=logged_in,
                message=(
                    f"正在并行处理 {len(active_quotes)} 个报价"
                    if active_quotes
                    else "等待销售报价任务"
                ) if logged_in else f"等待管理员登录 {engine_display_name()}",
                browser=engine_display_name(),
            )
            if not logged_in:
                for job_id in {quote.job_id for quote in active_quotes.values()}:
                    mark_needs_login(store, store.get(job_id))
                active_quotes.clear()
                time.sleep(POLL_SECONDS)
                continue
            if logged_in:
                for record in store.claim_submitted_for_monitoring(
                    WORKER_ID,
                    limit=None,
                    lease_minutes=35,
                    exclude_job_ids={quote.job_id for quote in active_quotes.values()},
                    engine=RELAY_ENGINE,
                ):
                    reattach_job_chats(store, browser, record, active_quotes)
                    create_missing_component_chats(
                        store, browser, active_quotes, record["job_id"],
                    )
                    maybe_start_final_merge(
                        store, browser, active_quotes, record["job_id"],
                    )
                    if pending_active_quote(active_quotes) is not None:
                        break
            while logged_in and pending_active_quote(active_quotes) is None:
                record = store.claim_next(
                    WORKER_ID,
                    lease_minutes=35,
                    engine=RELAY_ENGINE,
                )
                if record is None:
                    break
                try:
                    if record.get("partial_retry_pending") and record.get("chat_url"):
                        reattach_job_chats(store, browser, record, active_quotes)
                    else:
                        active = submit_job(store, browser, record)
                        if active is not None:
                            active_quotes[active.session_key] = active
                except Exception as exc:  # noqa: BLE001 - isolate one sales job
                    try:
                        browser.capture_debug(record["job_id"])
                    except Exception:  # noqa: BLE001,S110 - debug capture is best effort
                        pass
                    fail_job(store, record["job_id"], exc)

            pending_quote = pending_active_quote(active_quotes)
            if pending_quote is None:
                processing_job_ids = {
                    quote.job_id for quote in active_quotes.values()
                }
                for job_id in processing_job_ids:
                    create_missing_component_chats(
                        store, browser, active_quotes, job_id,
                    )
                    if pending_active_quote(active_quotes) is not None:
                        break
                    maybe_start_final_merge(store, browser, active_quotes, job_id)

            pending_quote = pending_active_quote(active_quotes)
            if pending_quote is None and advance_machine_completed_wave(
                store, browser, active_quotes,
            ):
                time.sleep(POLL_SECONDS)
                continue

            pending_quote = pending_active_quote(active_quotes)
            poll_order = (
                [pending_quote.session_key]
                if pending_quote is not None
                else active_quote_poll_order(active_quotes, limit=1)
            )
            for session_key in poll_order:
                active = active_quotes.get(session_key)
                if active is None:
                    continue
                active.last_polled_at = time.monotonic()
                job_id = active.job_id
                current = store.reconcile_delivery_receipt(job_id)
                if current.get("status") in {"completed", "partial", "failed"}:
                    stop_terminal_job_chats(browser, active_quotes, job_id)
                    continue
                if current.get("status") == "cancelled":
                    stop_terminal_job_chats(
                        browser,
                        active_quotes,
                        job_id,
                        cancelled=True,
                    )
                    continue
                try:
                    store.renew_lease(job_id, WORKER_ID, lease_minutes=35)
                    refresh_progress_deadline(store, active)
                    previous_reference = active.chat_url
                    try:
                        response = browser.poll_quote(
                            active,
                            completion_check=lambda job_id=job_id: (
                                store.reconcile_delivery_receipt(job_id).get("status")
                                in {"completed", "partial"}
                            ),
                        )
                    finally:
                        persist_promoted_chat_reference(
                            store, active, previous_reference,
                        )
                    if response is None:
                        continue
                    batches = store.quote_chat_batches(job_id)
                    if len(batches) > 1 and active.role != "merge":
                        if not component_batch_state_settled(store, active):
                            continue
                        if not continue_component_batch(store, browser, active):
                            active_quotes.pop(session_key, None)
                        maybe_start_final_merge(
                            store, browser, active_quotes, job_id,
                        )
                        continue
                    outcome = complete_job(store, job_id, response)
                    if outcome == "partial_finalize":
                        latest = store.get(job_id)
                        browser.continue_quote(
                            active,
                            build_quote_partial_finalization_prompt(
                                relay_job_id=job_id,
                                submission_code=str(
                                    latest.get("submission_code") or ""
                                ),
                            ),
                        )
                        continue
                    if outcome == "continue":
                        if not continue_from_saved_stage(
                            store,
                            browser,
                            active,
                            message="报价尚未完成，已在原对话从保存阶段自动继续",
                        ):
                            browser.close_quote(active)
                            active_quotes.pop(session_key, None)
                        continue
                    browser.close_quote(active)
                    active_quotes.pop(session_key, None)
                except TimeoutError:
                    handle_no_progress_timeout(store, browser, active, active_quotes)
                except PendingPromptSubmissionError:
                    # A UI-level non-send is not a pricing attempt and must not
                    # fail the quote or consume the one content retry. Keep the
                    # current conversation active and try its send control on
                    # the next round instead of switching away.
                    active.stable_since = time.monotonic()
                    break
                except Exception as exc:  # noqa: BLE001 - classify live renderer failures
                    if is_transient_browser_poll_exception(exc):
                        # Quote clients replace live DOM nodes while generating. The
                        # next polling round must locate fresh elements; the
                        # quote itself is still running and must remain active.
                        active.stable_since = time.monotonic()
                        reconnect = getattr(browser, "reconnect", None)
                        if callable(reconnect):
                            try:
                                reconnect()
                            except Exception:  # noqa: BLE001,S110 - retry next poll
                                pass
                        if active.stable_since >= active.deadline:
                            handle_no_progress_timeout(store, browser, active, active_quotes)
                        continue
                    fail_job(store, job_id, exc)
                    try:
                        browser.close_quote(active)
                    except Exception:  # noqa: BLE001,S110 - terminal cleanup is best effort
                        pass
                    active_quotes.pop(session_key, None)
            time.sleep(POLL_SECONDS)
        except KeyboardInterrupt:
            browser.close()
            return 0
        except Exception as exc:  # noqa: BLE001 - keep the supervisor alive
            write_heartbeat(
                store,
                logged_in=False,
                message=f"{engine_display_name()} 工作进程异常：{str(exc)[:500]}",
                browser=engine_display_name(),
            )
            # Submitted conversations continue server-side while the desktop
            # renderer restarts. Leave them processing for reference reattachment.
            active_quotes.clear()
            browser.close()
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
