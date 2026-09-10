from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

_PROJECT_ID_PATTERN = re.compile(
    r"/g/(g-p-[0-9a-z]+)(?:-[^/]+)?/",
    re.IGNORECASE,
)


def bounded_parallel_tabs(value: str | None) -> int:
    """Keep one browser profile within the supported one-to-four work tabs."""

    try:
        requested = int(value or "4")
    except (TypeError, ValueError):
        requested = 4
    return min(4, max(1, requested))


def active_quote_poll_order(active_quotes: Mapping[str, Any]) -> tuple[str, ...]:
    """Snapshot every active tab so one polling round cannot omit background work."""

    return tuple(active_quotes.keys())


def is_transient_browser_poll_exception(exc: BaseException) -> bool:
    """Classify a DOM replacement during polling as retryable.

    ChatGPT replaces message and permission-card nodes while it is generating.
    Selenium then raises StaleElementReferenceException for the old node even
    though the conversation and browser tab are still healthy.
    """

    return type(exc).__name__ == "StaleElementReferenceException"


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
    """Recognize ChatGPT's explicit tool-permission card in either locale."""

    normalized = " ".join(str(text or "").split()).casefold()
    return "允许 chatgpt 使用" in normalized or "allow chatgpt to use" in normalized


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
