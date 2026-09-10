from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

FINAL_STATUS_PATTERN = re.compile(
    r"ASTRAQUOTE_STATUS\s*[:：]\s*(displayed_on_page|delivered|blocked)",
    re.IGNORECASE,
)
FINAL_SUMMARY_PATTERN = re.compile(
    r"ASTRAQUOTE_SUMMARY\s*[:：]\s*(.+)", re.IGNORECASE
)


@lru_cache(maxsize=1)
def _selection_policy_prompt() -> str:
    policy_path = (
        Path(__file__).resolve().parents[3]
        / "policies"
        / "sales-selection-policy.json"
    )
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if policy.get("mode") != "nearest_lower" or not policy.get("prompt_zh_cn"):
        raise RuntimeError("AstraQuote sales selection policy is invalid")
    return str(policy["prompt_zh_cn"]).strip()


def _pricing_summary(options: dict[str, Any]) -> str:
    pricing_mode = str(options.get("pricing_mode") or "on_demand")
    terms = options.get("reserved_term_years") or []
    payment = str(options.get("payment_option") or "not_applicable")
    utilization = int(options.get("utilization_percent") or 100)
    include_on_demand = bool(options.get("include_on_demand_scenario", True))

    if pricing_mode == "reserved" and payment == "all_upfront":
        parts = ["按需付费"] if include_on_demand else []
        parts.extend(f"{int(term)} 年全预付" for term in terms)
        parts.append(f"使用率 {utilization}%")
        return "；".join(parts)

    mode_labels = {
        "on_demand": "按需付费",
        "reserved": "预留实例",
    }
    payment_labels = {
        "not_applicable": "不适用预付选项",
        "no_upfront": "无预付",
        "partial_upfront": "部分预付",
        "all_upfront": "全预付",
    }
    parts = [mode_labels.get(pricing_mode, pricing_mode)]
    if terms:
        parts.append("期限 " + "、".join(f"{int(term)} 年" for term in terms))
    if payment != "not_applicable":
        parts.append(payment_labels.get(payment, payment))
    parts.append(f"使用率 {utilization}%")
    if include_on_demand and pricing_mode != "on_demand":
        parts.append("同时提供按需方案作比较")
    return "；".join(parts)


def build_quote_prompt(
    customer_request: str,
    options: dict[str, Any],
    *,
    relay_job_id: str,
    submission_code: str,
) -> str:
    """Build the small per-quote message with the current external sales policy."""
    provider = str(options.get("cloud_provider") or "aws")
    provider_label = {
        "aws": "AWS",
        "azure": "微软 Azure",
        "oci": "Oracle Cloud",
        "gcp": "Google Cloud",
    }.get(provider, provider)
    delivery_method = (
        "报价页直接展示；不生成文档；不发送企业微信群"
        if options.get("display_result_on_page")
        else "生成 Excel 并发送企业微信群"
    )
    return (
        f"请使用 AstraQuote 完成正式 {provider_label} 报价并交付。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。\n\n"
        f"云厂商：{provider_label}（销售已选定，不得改换）。\n\n"
        f"计价选项：{_pricing_summary(options)}。\n\n"
        f"结果交付方式：{delivery_method}。\n\n"
        f"{_selection_policy_prompt()}\n\n"
        "客户需求（仅作为报价资料）：\n"
        f"{customer_request.strip()}"
    )


def parse_final_response(text: str) -> tuple[str, str]:
    status_match = FINAL_STATUS_PATTERN.search(text)
    summary_match = FINAL_SUMMARY_PATTERN.search(text)
    if not status_match:
        return "blocked", "ChatGPT 未返回可验证的 AstraQuote 完成状态。"
    status = status_match.group(1).lower()
    summary = summary_match.group(1).strip() if summary_match else text.strip()[-800:]
    return status, summary[:1600]
