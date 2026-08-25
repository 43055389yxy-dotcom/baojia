from __future__ import annotations

from typing import Any, Literal

PricingIssueCategory = Literal[
    "retryable",
    "compatibility",
    "catalog_mapping",
    "system_configuration",
    "unsupported",
]


def classify_persisted_pricing_issue(
    *,
    reason: str,
    code: str = "",
    service: str = "",
    requirements: dict[str, Any] | None = None,
) -> PricingIssueCategory:
    """Classify both current and legacy persisted component failures.

    Older confirmation sessions stored only a Chinese display sentence.  Keep
    those links usable without treating every catalog miss as a network
    timeout.  New sessions also pass the structured error code, which always
    takes priority over wording.
    """

    folded_code = code.casefold()
    folded_reason = reason.casefold()
    folded_service = service.casefold()
    values = requirements or {}

    # Old drafts lost the original error code and replaced it with one generic
    # "temporarily unavailable" sentence. Product facts that still survive in
    # the draft are more reliable than that sentence: an RDS engine version is
    # a compatibility decision, while QuickSight and CodeDeploy both have a
    # deterministic component-local recovery path.
    if not folded_code and folded_service in {"rds", "aurora"} and values.get(
        "engine_version"
    ):
        return "compatibility"
    if not folded_code and folded_service in {"quicksight", "codedeploy"}:
        return "catalog_mapping"

    if any(
        marker in folded_code
        for marker in (
            "lookup_timeout",
            "catalog_temporarily_unavailable",
            "backend_unavailable",
            "pricing_catalog_unavailable",
            "pricing_attribute_values_unavailable",
            "pricing_service_registry_unavailable",
            "bcm_",
        )
    ) or any(
        marker in folded_reason
        for marker in (
            "超时",
            "暂时无响应",
            "暂时未响应",
            "暂时不可用",
            "接口暂时未返回",
            "服务暂时不可用",
        )
    ):
        return "retryable"

    if folded_service in {"rds", "aurora"} and values.get("engine_version"):
        if any(
            marker in folded_code
            for marker in ("rds_", "pricing_candidates_not_found")
        ) or any(
            marker in folded_reason
            for marker in ("版本", "engine version", "不再提供维护", "不受支持")
        ):
            return "compatibility"

    if any(
        marker in folded_code
        for marker in (
            "credentials_invalid",
            "region_not_enabled",
            "unsupported_or_unknown_region",
            "adapter_not_ready",
            "discovery_failed",
            "auto_discovery_",
        )
    ) or any(
        marker in folded_reason
        for marker in ("凭证", "iam role", "区域未启用", "系统配置")
    ):
        return "system_configuration"

    if any(
        marker in folded_code
        for marker in (
            "unsupported_service",
            "generic_service_code_not_found",
            "service_region_not_supported",
        )
    ) or any(marker in folded_reason for marker in ("尚未接入", "暂不支持")):
        return "unsupported"

    return "catalog_mapping"


def legacy_pricing_issue_message(
    *,
    reason: str,
    category: PricingIssueCategory,
    service: str,
    display_name: str,
    requirements: dict[str, Any] | None = None,
) -> str:
    """Replace obsolete catch-all wording when an old session is opened."""

    values = requirements or {}
    folded_service = service.casefold()
    if folded_service in {"rds", "aurora"} and category == "compatibility":
        version = str(values.get("engine_version") or "当前").strip()
        return (
            f"{display_name} 的 {version} 版本将按同一主版本下最新受维护的小版本核价，"
            "无需修改实例配置。"
        )
    if folded_service == "quicksight" and category == "catalog_mapping":
        return (
            "旧目录状态已失效；系统会补齐 QuickSight 最小用户用量并重新匹配官方计费项，"
            "无需修改配置。"
        )
    if folded_service == "codedeploy" and category == "catalog_mapping":
        return (
            "旧目录状态已失效；部署到 Amazon EC2 的 CodeDeploy 无额外服务费，"
            "系统会按免费项正常完成。"
        )
    if category == "retryable":
        return "上次官方查询未完成，系统会自动重试，当前配置无需修改。"
    if category == "catalog_mapping":
        return "旧目录状态已失效，系统会重新匹配官方计费维度，当前配置无需修改。"
    return reason


def should_retry_persisted_pricing_issue(
    *,
    reason: str,
    category: str = "",
    code: str = "",
    service: str = "",
    requirements: dict[str, Any] | None = None,
) -> bool:
    resolved = category or classify_persisted_pricing_issue(
        reason=reason,
        code=code,
        service=service,
        requirements=requirements,
    )
    return resolved in {"retryable", "compatibility", "catalog_mapping"}
