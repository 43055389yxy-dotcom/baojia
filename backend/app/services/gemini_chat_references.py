"""Validated opaque references for Gemini Spark quote tasks."""

from __future__ import annotations

import re

GEMINI_CHAT_REFERENCE_PREFIX = "gemini-chat://tasks/"
GEMINI_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
GEMINI_SPARK_TASK_URL_PATTERN = re.compile(
    r"^https://gemini\.google\.com/spark/chat/[A-Za-z0-9_-]+(?:[/?#]|$)",
    re.IGNORECASE,
)


def is_gemini_task_id(value: object) -> bool:
    return bool(GEMINI_TASK_ID_PATTERN.fullmatch(str(value or "").strip()))


def is_gemini_chat_reference(value: object) -> bool:
    text = str(value or "").strip()
    return text.startswith(GEMINI_CHAT_REFERENCE_PREFIX) and is_gemini_task_id(
        text.removeprefix(GEMINI_CHAT_REFERENCE_PREFIX)
    )


def is_authenticated_gemini_workspace_url(value: object) -> bool:
    """Recognize the private Spark task route created after app sign-in."""

    return bool(GEMINI_SPARK_TASK_URL_PATTERN.match(str(value or "").strip()))


def gemini_chat_reference(task_id: str) -> str:
    normalized = str(task_id).strip()
    if not is_gemini_task_id(normalized):
        raise ValueError("Gemini task id is invalid")
    return f"{GEMINI_CHAT_REFERENCE_PREFIX}{normalized}"


def task_id_from_gemini_reference(reference: str) -> str:
    if not is_gemini_chat_reference(reference):
        raise ValueError("Only Gemini task references are allowed")
    return str(reference).strip().removeprefix(GEMINI_CHAT_REFERENCE_PREFIX)
