from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class QuoteError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    http_status: int = 422

    def __str__(self) -> str:
        return self.message


class ManualConfirmationRequired(QuoteError):
    def __init__(self, message: str, *, code: str = "manual_confirmation_required", **details: Any):
        super().__init__(code=code, message=message, details=details, http_status=422)


class ConfigurationError(QuoteError):
    def __init__(self, message: str, **details: Any):
        super().__init__(
            code="configuration_error",
            message=message,
            details=details,
            http_status=503,
        )
