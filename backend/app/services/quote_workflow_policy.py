from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = "astraquote-quote-workflow-policy/1"


def _validate_policy(payload: dict[str, Any]) -> None:
    positive_integer_paths = (
        ("batching", "components_per_wave"),
        ("batching", "waves_per_chat"),
        ("batching", "max_active_chats_per_sales_job"),
        ("batching", "global_active_chat_limit"),
        ("batching", "max_numbered_components"),
        ("batching", "max_deferred_components_per_retry"),
        ("timing", "default_quote_seconds"),
        ("timing", "max_continuations_without_progress"),
        ("pricing", "official_api_network_attempt_limit_per_scope"),
        ("pricing", "corrected_api_attempt_limit_per_scope"),
        ("pricing", "official_page_min_api_attempts"),
        ("recovery", "deferred_retry_rounds"),
    )
    for section, key in positive_integer_paths:
        value = payload[section].get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise RuntimeError(
                f"AstraQuote workflow policy integer is invalid: {section}.{key}"
            )

    if payload["pricing"]["official_api_network_attempt_limit_per_scope"] != (
        1 + payload["pricing"]["corrected_api_attempt_limit_per_scope"]
    ):
        raise RuntimeError("AstraQuote workflow policy API attempt budget is inconsistent")
    if payload["batching"]["max_deferred_components_per_retry"] > payload[
        "batching"
    ]["components_per_wave"]:
        raise RuntimeError("AstraQuote workflow policy deferred batch exceeds one wave")
    if payload["recovery"]["deferred_retry_rounds"] != 1:
        raise RuntimeError("AstraQuote worker supports exactly one deferred retry round")
    if payload["recovery"].get("stalled_component_strategy") != "defer_then_retry_once":
        raise RuntimeError("AstraQuote workflow policy recovery strategy is unsupported")
    if payload["completion"].get("authority") != "sealed_component_fragment":
        raise RuntimeError("AstraQuote workflow policy completion authority is unsupported")
    if payload["delivery"].get("owner") != "program":
        raise RuntimeError("AstraQuote workflow policy delivery owner is unsupported")

    boolean_paths = (
        ("pricing", "official_page_evidence_completes_scope"),
        ("pricing", "reuse_successful_evidence_within_quote"),
        ("pricing", "reuse_historical_prices_across_quotes"),
        ("pricing", "use_on_demand_fallback_for_missing_long_term_price"),
        ("recovery", "retry_in_original_conversation"),
        ("recovery", "continue_other_components"),
        ("completion", "ai_text_is_authoritative"),
        ("completion", "official_page_evidence_is_authoritative"),
        ("delivery", "allow_partial_sales_quote"),
        ("delivery", "allow_sales_manual_price"),
        ("delivery", "excel_after_all_component_batches"),
    )
    for section, key in boolean_paths:
        if not isinstance(payload[section].get(key), bool):
            raise RuntimeError(
                f"AstraQuote workflow policy boolean is invalid: {section}.{key}"
            )

    directives = payload["prompt_directives"]
    for slice_name, directive_keys in payload["consumer_slices"].items():
        if not isinstance(directive_keys, list) or not directive_keys:
            raise RuntimeError(f"AstraQuote workflow policy slice is invalid: {slice_name}")
        if any(
            not isinstance(key, str)
            or not isinstance(directives.get(key), str)
            or not directives[key].strip()
            for key in directive_keys
        ):
            raise RuntimeError(
                f"AstraQuote workflow policy slice has an invalid directive: {slice_name}"
            )


def _policy_path() -> Path:
    configured = os.environ.get("ASTRAQUOTE_QUOTE_WORKFLOW_POLICY_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3] / "policies" / "quote-workflow-policy.json"


@lru_cache(maxsize=1)
def quote_workflow_policy() -> dict[str, Any]:
    """Load the one process-cached source of quote workflow decisions."""

    payload = json.loads(_policy_path().read_text(encoding="utf-8"))
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise RuntimeError("AstraQuote workflow policy schema is invalid")
    if not str(payload.get("policy_version") or "").strip():
        raise RuntimeError("AstraQuote workflow policy version is missing")
    for section in (
        "batching",
        "timing",
        "pricing",
        "recovery",
        "completion",
        "delivery",
        "prompt_directives",
        "consumer_slices",
    ):
        if not isinstance(payload.get(section), dict):
            raise RuntimeError(f"AstraQuote workflow policy section is invalid: {section}")
    _validate_policy(payload)
    return payload


def workflow_policy_version() -> str:
    return str(quote_workflow_policy()["policy_version"])


def workflow_policy_value(*path: str) -> Any:
    value: Any = quote_workflow_policy()
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise RuntimeError(f"AstraQuote workflow policy value is missing: {'.'.join(path)}")
        value = value[key]
    return value


def workflow_policy_int(*path: str) -> int:
    value = workflow_policy_value(*path)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"AstraQuote workflow policy integer is invalid: {'.'.join(path)}")
    return value


def render_workflow_policy_slice(slice_name: str) -> str:
    """Project only the directives required by one GPT execution stage."""

    policy = quote_workflow_policy()
    directive_keys = policy["consumer_slices"].get(slice_name)
    if not isinstance(directive_keys, list) or not directive_keys:
        raise RuntimeError(f"AstraQuote workflow policy slice is invalid: {slice_name}")
    directives = policy["prompt_directives"]
    rendered: list[str] = []
    for key in directive_keys:
        text = directives.get(key)
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"AstraQuote workflow directive is invalid: {key}")
        rendered.append(text.strip())
    return "\n".join(rendered)


def workflow_policy_snapshot() -> dict[str, Any]:
    """Return the compact machine snapshot stored with a quote, without GPT prose."""

    policy = quote_workflow_policy()
    return {
        "schema_version": policy["schema_version"],
        "policy_version": policy["policy_version"],
        "batching": dict(policy["batching"]),
        "timing": dict(policy["timing"]),
        "pricing": dict(policy["pricing"]),
        "recovery": dict(policy["recovery"]),
        "completion": dict(policy["completion"]),
        "delivery": dict(policy["delivery"]),
    }
