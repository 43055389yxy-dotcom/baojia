from __future__ import annotations

import contextvars
import os
import re
import threading
import traceback
import uuid
from collections import deque
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "diagnostic_request_id",
    default=None,
)

_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "session_token",
    "access_key",
    "api_key",
    "apikey",
)
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_AWS_ACCESS_KEY_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_CONFIRMATION_TOKEN_PATTERN = re.compile(r"\b(?:aws|azure)_[A-Za-z0-9_-]{12,}\b")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(authorization|api[_-]?key|access[_-]?key|session[_-]?token|"
    r"secret(?:[_-]?access)?[_-]?key|password|credential|token)"
    r"(\s*[:=]\s*[\"']?)([^\"'\s&,;}]+)"
)
_OPENAI_STYLE_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def current_request_id() -> str | None:
    return _request_id.get()


def bind_request_id(value: str):
    return _request_id.set(value)


def reset_request_id(token: contextvars.Token[str | None]) -> None:
    _request_id.reset(token)


def _redact_text(value: str) -> str:
    redacted = _BEARER_PATTERN.sub(r"\1 [REDACTED]", value)
    redacted = _AWS_ACCESS_KEY_PATTERN.sub("[REDACTED_AWS_ACCESS_KEY]", redacted)
    redacted = _CONFIRMATION_TOKEN_PATTERN.sub("[REDACTED_CONFIRMATION_TOKEN]", redacted)
    redacted = _JWT_PATTERN.sub("[REDACTED_JWT]", redacted)
    redacted = _OPENAI_STYLE_KEY_PATTERN.sub("[REDACTED_API_KEY]", redacted)
    return _SECRET_ASSIGNMENT_PATTERN.sub(r"\1\2[REDACTED]", redacted)


def redact_diagnostic_value(value: Any, *, _depth: int = 0) -> Any:
    """Return JSON-safe diagnostic data without credentials or customer links."""

    if _depth > 12:
        return "[MAX_DEPTH_REACHED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return redact_diagnostic_value(value.value, _depth=_depth + 1)
    if isinstance(value, BaseModel):
        return redact_diagnostic_value(value.model_dump(mode="json"), _depth=_depth + 1)
    if isinstance(value, BaseException):
        return {
            "error_type": type(value).__name__,
            "message": _redact_text(str(value)),
        }
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_item in value.items():
            key = str(raw_key)
            lowered = key.lower().replace("-", "_")
            if any(part in lowered for part in _SENSITIVE_KEY_PARTS) or lowered == "token":
                result[key] = "[REDACTED]"
            else:
                result[key] = redact_diagnostic_value(raw_item, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset, deque)):
        return [redact_diagnostic_value(item, _depth=_depth + 1) for item in value]
    try:
        return redact_diagnostic_value(vars(value), _depth=_depth + 1)
    except TypeError:
        return _redact_text(repr(value))


class DiagnosticLog:
    """Bounded in-memory development log exposed to the local diagnostics UI."""

    def __init__(self, max_entries: int = 2000) -> None:
        self._entries: deque[dict[str, Any]] = deque(maxlen=max_entries)
        self._lock = threading.Lock()
        self._enabled = os.getenv("APP_ENV", "development").lower() not in {
            "production",
            "prod",
        }

    @property
    def enabled(self) -> bool:
        return self._enabled

    def configure(self, *, enabled: bool) -> None:
        self._enabled = enabled
        if not enabled:
            self.clear()

    def record(
        self,
        event: str,
        *,
        level: str = "info",
        message: str | None = None,
        context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> str | None:
        if not self._enabled:
            return None
        diagnostic_id = f"diag_{uuid.uuid4().hex}"
        entry = {
            "diagnostic_id": diagnostic_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level,
            "event": event,
            "message": _redact_text(message) if message is not None else None,
            "request_id": request_id or current_request_id(),
            "context": redact_diagnostic_value(context or {}),
        }
        with self._lock:
            self._entries.append(entry)
        return diagnostic_id

    def record_exception(
        self,
        event: str,
        error: BaseException,
        *,
        level: str = "error",
        context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> str | None:
        raw_traceback = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
        error_context = dict(context or {})
        error_context.update(
            {
                "error_type": type(error).__name__,
                "raw_error": repr(error),
                "raw_message": str(error),
                "traceback": raw_traceback,
            }
        )
        cause = error.__cause__ or error.__context__
        if cause is not None:
            error_context["cause"] = {
                "error_type": type(cause).__name__,
                "raw_error": repr(cause),
                "raw_message": str(cause),
            }
        return self.record(
            event,
            level=level,
            message=str(error),
            context=error_context,
            request_id=request_id,
        )

    def snapshot(self, *, limit: int = 500, since: str | None = None) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 2000))
        with self._lock:
            entries = list(self._entries)
        if since:
            entries = [entry for entry in entries if str(entry.get("timestamp", "")) > since]
        return entries[-bounded_limit:]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


diagnostic_log = DiagnosticLog()
