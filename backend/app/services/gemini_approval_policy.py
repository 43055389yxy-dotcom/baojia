"""Narrow allowlist for unattended Gemini AstraQuote confirmations."""

from __future__ import annotations

ASTRAQUOTE_TOOL_NAMES = {
    "describe_service",
    "get_attribute_values",
    "get_prices",
    "get_price_results",
    "get_quote_job_status",
    "resume_quote_job",
    "build_estimate",
}
APPROVE_LABELS = {
    "allow",
    "allow once",
    "allow this time",
    "允许",
    "允许一次",
    "这次允许",
    "确认",
    "确认授权",
}


def approval_card_is_safe(text: str, button_label: str) -> bool:
    """Approve only a known public operation on an AstraQuote tool card."""

    normalized_label = str(button_label or "").strip().casefold()
    card_text = str(text or "")
    if normalized_label not in APPROVE_LABELS or "astraquote" not in card_text.casefold():
        return False
    return any(tool in card_text for tool in ASTRAQUOTE_TOOL_NAMES)
