from __future__ import annotations

import re
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

PROVIDER_LABELS = {
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
}

ASTRAQUOTE_MENTION = "@AstraQuote"


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
        item["key"]: item["label"]
        for item in active_market_profile(provider)["pricing_scenarios"]
    }
    parts = [labels.get(str(scenario), str(scenario)) for scenario in scenarios]
    parts.append(f"使用率 {utilization}%")
    return "；".join(parts)


def build_quote_context_prompt(options: dict[str, Any]) -> str:
    """Render only the per-order facts shared by coordinator and child chats."""

    provider = str(options.get("cloud_provider") or "aws")
    provider_label = PROVIDER_LABELS.get(provider, provider)
    market_profile = active_market_profile(provider)
    preferred_region = str(options.get("preferred_region") or "").strip()
    official_codes = {
        str(item.get("code") or "").strip()
        for item in market_profile.get("regions", [])
        if isinstance(item, dict)
        and str(item.get("code") or "").strip()
    }
    if preferred_region in official_codes:
        preferred_region_instruction = (
            f"销售首选地域：{preferred_region}（以销售页选择为准；若有产品不可购，"
            "仅在同一云厂商、同一账号站点内改用最近的整套可购地域并披露原因）。"
        )
    else:
        preferred_region_instruction = (
            f"销售提供的地域偏好：{preferred_region or '未指定'}。不得因此停止报价；"
            "请把它只当作地理位置偏好，"
            "在同一云厂商、同一账号站点内选择距离最近且能覆盖整套产品的官方地域。"
        )
    return (
        f"云厂商：{provider_label}（销售已选定，不得改换）。\n\n"
        f"账号站点：{market_profile['site_label']}（凭证范围 "
        f"{market_profile['credential_scope']}，不得与其他站点的文档、域名或价格混用）。\n\n"
        f"{preferred_region_instruction}\n\n"
        f"计价选项：{_pricing_summary(options)}。"
    )


def build_quote_prompt(
    customer_request: str,
    options: dict[str, Any],
    *,
    relay_job_id: str,
    submission_code: str,
) -> str:
    """Build a compact message containing only facts unique to this quote."""
    provider = str(options.get("cloud_provider") or "aws")
    provider_label = PROVIDER_LABELS.get(provider, provider)
    return (
        f"{ASTRAQUOTE_MENTION} 请使用 AstraQuote 完成正式 {provider_label} 报价并交付。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。\n\n"
        f"{build_quote_context_prompt(options)}\n\n"
        "交付：销售报价页与 Excel 下载链接。\n\n"
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
        f"{ASTRAQUOTE_MENTION} 这不是新报价，当前 AstraQuote 报价尚未产生最终结果。"
        "请在本对话中从已保存阶段继续完成，不要只汇报剩余待办，"
        "也不要重复已经成功的查价、文件或交付步骤。"
        "收到后立即继续实际执行，禁止再次只输出状态、计划或待办清单。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。\n\n"
        "只有报价与 Excel 下载链接已经在销售页面就绪，或者存在确实无法继续处理的"
        "单一阻塞原因时，才结束本次回复。"
    )


def build_quote_partial_finalization_prompt(
    *,
    relay_job_id: str,
    submission_code: str,
) -> str:
    """Stop retrying an unchanged stage and publish verified successes."""

    return (
        f"{ASTRAQUOTE_MENTION} 后台已确认同一处理阶段连续两次没有真实进展。"
        "现在停止重复查价，不要整单报错。"
        "请从 AstraQuote 已保存阶段读取成功组件及其官方证据，立即生成部分报价和 Excel；"
        "仍未取得价格的组件全部放入 unpriced_services，并设置 is_partial=true。"
        "未取得价格的组件不得按 0 元、不得计入任何合计，也不得丢失。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。\n\n"
        "完成后按正常最终状态协议结束；不得再输出计划、继续等待或重新查询已经成功的组件。"
    )


def build_quote_failed_components_retry_prompt(
    *,
    relay_job_id: str,
    submission_code: str,
) -> str:
    """Resume a partial quote without repeating successful component work."""

    return (
        f"{ASTRAQUOTE_MENTION} 销售已选择重试部分报价中的未完成组件。这不是新报价。"
        "请读取 AstraQuote 保存的组件计划、价格批次和部分报价，只处理 unpriced_services；"
        "已经核价成功的组件、官方证据和 Excel 数据必须直接复用，不得重新查询。"
        "完成后重新核对整单：全部成功则交付完整报价；仍有组件失败则再次交付部分报价。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。"
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
