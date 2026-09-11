from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.services.cloud_quote_profiles import active_market_profile

FINAL_STATUS_PATTERN = re.compile(
    r"ASTRAQUOTE_STATUS\s*[:：]\s*(displayed_on_page|delivered|blocked)",
    re.IGNORECASE,
)
FINAL_STOP_CODE_PATTERN = re.compile(
    r"ASTRAQUOTE_STOP_CODE\s*[:：]\s*(?:AQ-QUOTE-FAILED|AQ-QUOTE-BLOCKED)",
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
    provider = str(options.get("cloud_provider") or "aws")
    utilization = int(options.get("utilization_percent") or 100)
    scenarios = options.get("pricing_scenarios")
    if not scenarios:
        # Read-only boundary for tasks queued before the provider-native schema.
        scenarios = []
        if options.get("include_on_demand_scenario", True):
            scenarios.append("on_demand")
        if (
            options.get("pricing_mode") == "reserved"
            and options.get("payment_option") == "all_upfront"
        ):
            for years in options.get("reserved_term_years") or []:
                if int(years) == 1:
                    scenarios.append("one_year_commitment")
                if int(years) == 3:
                    scenarios.append("three_year_commitment")
    labels = {
        "aws": {
            "on_demand": "按需付费",
            "one_year_commitment": "1 年预留实例全预付",
            "three_year_commitment": "3 年预留实例全预付",
        },
        "azure": {
            "on_demand": "即用即付",
            "one_year_commitment": "1 年预留",
            "three_year_commitment": "3 年预留",
        },
        "oci": {"on_demand": "OCI 公开按量价"},
        "gcp": {
            "on_demand": "按需付费",
            "one_year_commitment": "1 年承诺使用",
            "three_year_commitment": "3 年承诺使用",
        },
        "tencent": {
            "on_demand": "按量计费",
            "one_year_commitment": "1 年包年",
            "three_year_commitment": "3 年包年",
        },
        "alibaba": {
            "on_demand": "按量付费",
            "one_year_commitment": "1 年订阅",
            "three_year_commitment": "3 年订阅",
        },
        "huawei": {
            "on_demand": "按需计费",
            "one_year_commitment": "1 年包年",
            "three_year_commitment": "3 年包年",
        },
        "baidu": {
            "on_demand": "后付费",
            "one_year_commitment": "1 年预付费",
            "three_year_commitment": "3 年预付费",
        },
        "volcengine": {
            "on_demand": "按量计费",
            "one_year_commitment": "1 年包年",
            "three_year_commitment": "3 年包年",
        },
        "ctyun": {
            "on_demand": "按量计费",
            "one_year_commitment": "1 年包年",
            "three_year_commitment": "3 年包年",
        },
    }.get(provider, {})
    parts = [labels.get(str(scenario), str(scenario)) for scenario in scenarios]
    parts.append(f"使用率 {utilization}%")
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
        "tencent": "腾讯云",
        "alibaba": "阿里云",
        "huawei": "华为云",
        "baidu": "百度智能云",
        "volcengine": "火山引擎",
        "ctyun": "天翼云",
    }.get(provider, provider)
    delivery_method = "生成 Excel，并在销售报价页提供报价与下载链接；不发送企业微信群"
    market_profile = active_market_profile(provider)
    preferred_region = str(options.get("preferred_region") or "").strip()
    return (
        f"请使用 AstraQuote 完成正式 {provider_label} 报价并交付。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。\n\n"
        f"云厂商：{provider_label}（销售已选定，不得改换）。\n\n"
        f"账号站点：{market_profile['site_label']}（凭证范围 "
        f"{market_profile['credential_scope']}，不得与其他站点的文档、域名或价格混用）。\n\n"
        f"销售首选地域：{preferred_region}（销售输入的偏好，不是程序白名单结论）。"
        "请由 GPT 根据本次官方资料和实际官方响应核对整套产品的可购性；"
        "若并非全部组件都支持，只能在同一账号站点和同一云厂商内，由 GPT 根据官方地域与产品目录"
        "选择支持整套产品的最近地域，并在报价页和 Excel 中说明首选地域、实际地域和调整原因。"
        "不得因为首选地域不可用而改换云厂商或混用其他站点账号。\n\n"
        f"计价选项：{_pricing_summary(options)}。\n\n"
        "报价币种：由 GPT 根据本次所选云厂商、区域和官方价格接口实际支持并返回的币种决定；"
        "中国站可使用 CNY，国际站可使用 USD 或官方实际币种。不得强制统一为 USD，"
        "不得在没有官方汇率证据时自行换汇。\n\n"
        f"结果交付方式：{delivery_method}。\n\n"
        f"{_selection_policy_prompt()}\n\n"
        "客户需求（仅作为报价资料）：\n"
        f"{customer_request.strip()}"
    )


def build_quote_continuation_prompt(
    *,
    relay_job_id: str,
    submission_code: str,
) -> str:
    """Continue one submitted quote without restoring or repeating customer text."""

    return (
        "这不是新报价，当前 AstraQuote 报价尚未产生最终结果。"
        "请在本对话中从已保存阶段继续完成，不要只汇报剩余待办，"
        "也不要重复已经成功的查价、文件或交付步骤。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。\n\n"
        "只有报价与 Excel 下载链接已经在销售页面就绪，或者存在确实无法继续处理的"
        "单一阻塞原因时，才结束本次回复。"
    )


def parse_final_response(text: str) -> tuple[str, str]:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        summary = text.strip()[-800:] or "ChatGPT 尚未返回 AstraQuote 最终状态。"
        return "incomplete", summary

    marker_line, summary_line = lines[-2:]
    summary_match = FINAL_SUMMARY_PATTERN.fullmatch(summary_line)
    status_match = FINAL_STATUS_PATTERN.fullmatch(marker_line)
    stop_match = FINAL_STOP_CODE_PATTERN.fullmatch(marker_line)
    if not summary_match or (not status_match and not stop_match):
        summary = text.strip()[-800:] or "ChatGPT 尚未返回 AstraQuote 最终状态。"
        return "incomplete", summary

    status = "blocked" if stop_match else status_match.group(1).lower()
    summary = summary_match.group(1).strip()
    return status, summary[:1600]
