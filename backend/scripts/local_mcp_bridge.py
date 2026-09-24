#!/usr/bin/env python3
"""Process-local JSON bridge used by the standalone AstraQuote MCP.

The MCP owns the public transport.  This helper imports the existing official
catalog clients as a library so local users do not have to run a second HTTP
backend process.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from typing import Any

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)


def _load_runtime() -> tuple[Any, dict[str, Any]]:
    # Keep stdout reserved for one JSON response per input line.  A third-party
    # dependency that prints while importing must not corrupt the bridge.
    with contextlib.redirect_stdout(sys.stderr):
        from app.core.config import get_settings
        from app.integrations.aws import AwsClients
        from app.services.aws_query_executor import ReadOnlyAwsQueryExecutor
        from app.services.mcp_v2_pricing import (
            AttributeValuesRequest,
            DescribeServiceRequest,
            GetPricesRequest,
            OfficialPricingService,
            ProductSearchRequest,
        )

        settings = get_settings()
        service = OfficialPricingService(
            ReadOnlyAwsQueryExecutor(AwsClients.from_settings(settings))
        )

    models: dict[str, Any] = {
        "describe_service": DescribeServiceRequest,
        "get_attribute_values": AttributeValuesRequest,
        "search_products": ProductSearchRequest,
        "get_prices": GetPricesRequest,
    }
    return service, models


SERVICE, MODELS = _load_runtime()


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _error_payload(exc: Exception) -> dict[str, Any]:
    details = getattr(exc, "details", {})
    validation_error = hasattr(exc, "errors")
    if hasattr(exc, "errors"):
        try:
            details = {"violations": exc.errors()}
        except Exception:  # pragma: no cover - defensive serialization only
            pass
    return {
        "code": str(
            getattr(
                exc,
                "code",
                "local_pricing_request_schema_invalid"
                if validation_error
                else "local_pricing_bridge_failed",
            )
        ),
        "message": str(exc)[:1200] or "Local pricing bridge failed.",
        "details": _json_safe(details if isinstance(details, dict) else {}),
        "retryable": bool(getattr(exc, "retryable", validation_error)),
        "status": int(
            getattr(
                exc,
                "status_code",
                getattr(exc, "http_status", 422 if validation_error else 500),
            )
            or 500
        ),
    }


def dispatch(method: str, params: dict[str, Any]) -> dict[str, Any]:
    if method == "health":
        return {
            "status": "ready",
            "role": "process-local official cloud catalog client",
            "provider_catalogs": SERVICE.catalog_availability(),
        }
    model = MODELS.get(method)
    if model is None:
        raise ValueError(f"Unsupported local pricing bridge method: {method}")
    request = model.model_validate(params)
    handler = getattr(SERVICE, method)
    return _json_safe(handler(request))


def main() -> int:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        request_id: Any = None
        try:
            payload = json.loads(line)
            request_id = payload.get("id")
            method = str(payload.get("method") or "")
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("Bridge params must be an object")
            response = {
                "id": request_id,
                "ok": True,
                "result": dispatch(method, params),
            }
        except Exception as exc:  # Keep the long-lived bridge available.
            response = {"id": request_id, "ok": False, "error": _error_payload(exc)}
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
        sys.stdout.write("\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
