from __future__ import annotations

from fastapi.testclient import TestClient

from app.aws_main import app
from app.core.diagnostics import diagnostic_log, redact_diagnostic_value


def test_diagnostic_redaction_keeps_structure_but_removes_credentials() -> None:
    value = redact_diagnostic_value(
        {
            "aws_access_key_id": "AKIA1234567890123456",
            "Authorization": "Bearer original-private-token",
            "raw_error": "request failed: api_key=sk-this-is-a-private-key-value",
            "nested": {
                "confirmation_url": "http://localhost:3000/confirm/aws_abcdefghijklmnop",
                "aws_error_code": "ValidationException",
            },
        }
    )

    assert value == {
        "aws_access_key_id": "[REDACTED]",
        "Authorization": "[REDACTED]",
        "raw_error": "request failed: api_key=[REDACTED]",
        "nested": {
            "confirmation_url": (
                "http://localhost:3000/confirm/[REDACTED_CONFIRMATION_TOKEN]"
            ),
            "aws_error_code": "ValidationException",
        },
    }


def test_diagnostic_exception_preserves_original_type_message_and_traceback() -> None:
    diagnostic_log.configure(enabled=True)
    diagnostic_log.clear()
    try:
        raise RuntimeError("BCM original failure")
    except RuntimeError as error:
        diagnostic_id = diagnostic_log.record_exception(
            "test_original_exception",
            error,
            context={"component": "EC2", "aws_error_code": "ValidationException"},
        )

    entries = diagnostic_log.snapshot(limit=10)
    entry = next(item for item in entries if item["diagnostic_id"] == diagnostic_id)
    assert entry["context"]["error_type"] == "RuntimeError"
    assert entry["context"]["raw_error"] == "RuntimeError('BCM original failure')"
    assert "raise RuntimeError" in entry["context"]["traceback"]
    assert entry["context"]["aws_error_code"] == "ValidationException"


def test_diagnostic_message_redacts_confirmation_links() -> None:
    diagnostic_log.configure(enabled=True)
    diagnostic_log.clear()
    diagnostic_log.record(
        "request",
        message="GET /api/confirmation-sessions/aws_abcdefghijklmnop -> 404",
    )

    entry = diagnostic_log.snapshot(limit=1)[0]
    assert "aws_abcdefghijklmnop" not in entry["message"]
    assert "[REDACTED_CONFIRMATION_TOKEN]" in entry["message"]


def test_clear_diagnostic_logs_does_not_log_the_clear_request() -> None:
    diagnostic_log.configure(enabled=True)
    diagnostic_log.clear()
    diagnostic_log.record("old_quote_failure", level="error", message="old run")

    with TestClient(app) as client:
        clear_response = client.post("/api/debug/logs/clear")
        logs_response = client.get("/api/debug/logs?limit=20")

    assert clear_response.status_code == 200
    assert logs_response.status_code == 200
    assert logs_response.json()["entries"] == []


def test_local_api_error_returns_traceable_diagnostic_id() -> None:
    diagnostic_log.configure(enabled=True)
    diagnostic_log.clear()

    with TestClient(app) as client:
        response = client.post(
            "/api/quotes/preview",
            json={
                "cloud_provider": "azure",
                "customer_request": "1、Azure Virtual Machines",
            },
        )
        logs_response = client.get("/api/debug/logs?limit=20")

    assert response.status_code == 403
    assert response.headers["x-diagnostic-request-id"].startswith("req_")
    details = response.json()["details"]
    assert details["diagnostic_id"].startswith("diag_")
    assert details["request_id"] == response.headers["x-diagnostic-request-id"]
    assert logs_response.status_code == 200
    entries = logs_response.json()["entries"]
    error = next(item for item in entries if item["diagnostic_id"] == details["diagnostic_id"])
    assert error["event"] == "quote_api_error"
    assert error["context"]["error_code"] == "provider_boundary_violation"
    assert "QuoteError" in error["context"]["traceback"]
