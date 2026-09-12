from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

_PROJECT_ID_PATTERN = re.compile(
    r"/g/(g-p-[0-9a-z]+)(?:-[^/]+)?/",
    re.IGNORECASE,
)


def bounded_continuation_attempts(value: str | None) -> int:
    """Retry one unchanged backend stage at most twice.

    A large quote may legitimately make progress many times.  That progress is
    tracked separately by the relay store; this bound applies only while the
    machine checkpoint has not changed.
    """

    try:
        requested = int(value or "2")
    except (TypeError, ValueError):
        requested = 2
    return min(2, max(1, requested))


def active_quote_poll_order(active_quotes: Mapping[str, Any]) -> tuple[str, ...]:
    """Snapshot every active tab so one polling round cannot omit background work."""

    return tuple(active_quotes.keys())


def should_extend_quote_deadline(
    *,
    deadline_reached: bool,
    generation_active: bool,
    retry_visible: bool,
) -> bool:
    """Allow a bounded grace period only for active generation.

    A visible Retry button is an error state rather than proof of work.  The
    caller is responsible for granting this generation grace only once until
    machine-observed progress changes.
    """

    return deadline_reached and generation_active


def is_transient_browser_poll_exception(exc: BaseException) -> bool:
    """Classify a DOM replacement during polling as retryable.

    ChatGPT replaces message and permission-card nodes while it is generating.
    Selenium then raises StaleElementReferenceException for the old node even
    though the conversation and browser tab are still healthy.
    """

    error_name = type(exc).__name__
    if error_name in {
        "StaleElementReferenceException",
        "ReadTimeoutError",
        "NewConnectionError",
        "ProtocolError",
    }:
        return True
    message = str(exc).casefold()
    return "httpconnectionpool" in message and "read timed out" in message


def is_interrupted_response(text: str) -> bool:
    """Recognize ChatGPT's recoverable stream interruption banner."""

    normalized = " ".join(str(text or "").split()).casefold()
    return (
        "连接已中断" in normalized
        or "正在等待完整回复" in normalized
        or "connection interrupted" in normalized
        or "waiting for the full response" in normalized
    )


def canonical_url_path(url: str) -> str:
    """Compare ChatGPT locations without transient query parameters."""

    return urlsplit(url).path.rstrip("/") or "/"


def project_id_from_url(url: str) -> str | None:
    match = _PROJECT_ID_PATTERN.search(canonical_url_path(url) + "/")
    return match.group(1).lower() if match else None


def is_project_landing_url(url: str) -> bool:
    path = canonical_url_path(url)
    return project_id_from_url(url) is not None and path.endswith("/project")


def is_project_chat_url(url: str) -> bool:
    path = canonical_url_path(url)
    return project_id_from_url(url) is not None and "/c/" in path


def is_new_project_chat(
    project_url: str,
    chat_url: str,
    previous_chat_url: str | None,
) -> bool:
    """Require a new conversation owned by the project just opened."""

    project_id = project_id_from_url(project_url)
    if project_id is None or project_id_from_url(chat_url) != project_id:
        return False
    if not is_project_chat_url(chat_url):
        return False
    if previous_chat_url and canonical_url_path(chat_url) == canonical_url_path(previous_chat_url):
        return False
    return True


def is_tool_permission_prompt(text: str) -> bool:
    """Recognize an explicit AstraQuote permission card in either locale.

    The relay is a quote-only desktop worker.  A generic ChatGPT permission
    card (for example Gmail) must never be approved merely because it happens
    to be visible in the same account.
    """

    normalized = " ".join(str(text or "").split()).casefold()
    asks_for_tool = (
        "允许 chatgpt 使用" in normalized
        or "allow chatgpt to use" in normalized
    )
    return asks_for_tool and "astraquote" in normalized


def is_persistent_permission_action(text: str) -> bool:
    """Recognize the explicit persistent approval action in either UI locale."""

    normalized = " ".join(str(text or "").split()).casefold()
    return normalized in {"始终允许", "always allow"}


def is_single_use_permission_action(text: str) -> bool:
    """Recognize ChatGPT's split-button label used to expose approval choices."""

    normalized = " ".join(str(text or "").split()).casefold()
    return normalized in {"允许一次", "allow once"}


def is_scroll_to_latest_action(text: str) -> bool:
    """Recognize ChatGPT's locale-specific control for following new output."""

    normalized = " ".join(str(text or "").split()).casefold()
    return normalized in {
        "滚动到底部",
        "转到最新消息",
        "前往最新消息",
        "scroll to bottom",
        "go to latest message",
    }
