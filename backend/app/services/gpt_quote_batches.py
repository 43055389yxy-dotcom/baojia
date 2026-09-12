"""Build isolated component batches for one-window quote chat automation.

Only first-pass cleaned component sources enter these prompts.  A parent and
all of its descendants always stay in the same chat, so the irreversible
component ownership boundary is preserved while the worker visits chat URLs
through one logged-in desktop window.
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

COMPONENTS_PER_CHAT = 20
ASTRAQUOTE_MENTION = "@AstraQuote"


def split_component_plan(
    components: list[dict[str, Any]],
    *,
    maximum_top_level_components: int = COMPONENTS_PER_CHAT,
) -> list[list[dict[str, Any]]]:
    """Split by top-level ownership and keep every descendant with its root."""

    if maximum_top_level_components < 1:
        raise ValueError("maximum_top_level_components must be positive")
    if not components:
        return []

    by_key = {str(item.get("component_key") or ""): item for item in components}
    if "" in by_key or len(by_key) != len(components):
        raise ValueError("component keys must be non-empty and unique")

    def root_key(component_key: str) -> str:
        seen: set[str] = set()
        current = component_key
        while True:
            if current in seen:
                raise ValueError("component parent relationship contains a cycle")
            seen.add(current)
            parent = str(by_key[current].get("parent_component_key") or "")
            if not parent:
                return current
            if parent not in by_key:
                raise ValueError("component parent does not exist")
            current = parent

    roots: list[str] = []
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in components:
        key = str(item["component_key"])
        root = root_key(key)
        if root not in families:
            roots.append(root)
        families[root].append(item)

    batches: list[list[dict[str, Any]]] = []
    for offset in range(0, len(roots), maximum_top_level_components):
        batch: list[dict[str, Any]] = []
        for root in roots[offset : offset + maximum_top_level_components]:
            batch.extend(families[root])
        batches.append(batch)
    return batches


def build_component_batch_prompt(
    *,
    relay_job_id: str,
    submission_code: str,
    price_batch_id: str,
    batch_index: int,
    batch_count: int,
    components: list[dict[str, Any]],
    quote_context: str,
) -> str:
    """Create a child-chat prompt from one batch's sealed cleaned sources."""

    safe_components = [
        {
            "component_key": str(component["component_key"]),
            **(
                {"parent_component_key": str(component["parent_component_key"])}
                if component.get("parent_component_key")
                else {}
            ),
            "customer_owned_source": str(component["customer_owned_source"]),
            "billing_scopes": component.get("billing_scopes") or [],
        }
        for component in components
    ]
    return (
        f"{ASTRAQUOTE_MENTION} 这是同一张 AstraQuote 报价的组件子批次，不是新报价。"
        f"当前为第 {batch_index + 1}/{batch_count} 批。只处理下面列出的组件，"
        "不得读取、猜测或修改其他批次。使用已经封存的 price_batch_id，"
        "逐组件查询并保存官方价格证据；已经成功的计费项直接复用，只补未完成项。"
        "本批结束后不要生成最终整单，也不要自行合并；总控对话会在所有批次结束后"
        "由后台统一合并、核对并生成 Excel。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。\n"
        f"price_batch_id：{price_batch_id}\n\n"
        "本批必须沿用以下整单报价条件：\n"
        f"{quote_context.strip()}\n\n"
        "本批已清洗组件（JSON）：\n"
        f"{json.dumps(safe_components, ensure_ascii=False, separators=(',', ':'))}"
    )


def build_quote_merge_prompt(
    *,
    relay_job_id: str,
    submission_code: str,
    price_batch_id: str,
) -> str:
    """Return the coordinator to the saved state for one final merge."""

    return (
        f"{ASTRAQUOTE_MENTION} 所有组件子批次已经停止运行。"
        "请在总控对话中读取 AstraQuote 后台保存的"
        "组件计划、price_batch 和真实组件状态，立即做最终机械合并与编译器校验。"
        "已有官方价格的组件必须全部进入报价；永久失败或两次无进展后仍未完成的组件"
        "进入 unpriced_services，不得按 0 元，不得计入合计。若存在未核价组件，"
        "生成部分报价和 Excel；否则生成完整报价和 Excel。不得重复查询已经成功的组件。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。\n"
        f"price_batch_id：{price_batch_id}。"
    )


def build_component_batch_continuation_prompt(
    *,
    relay_job_id: str,
    submission_code: str,
    price_batch_id: str,
    batch_index: int,
    batch_count: int,
    component_keys: list[str],
) -> str:
    """Continue only one saved child batch without restoring any source text."""

    return (
        f"{ASTRAQUOTE_MENTION} 这不是新报价。"
        "请继续当前组件子批次，只补本批尚未完成的官方价格，"
        "已经成功的查询必须复用，不得处理其他批次，也不要生成最终整单。"
        f"当前为第 {batch_index + 1}/{batch_count} 批；"
        f"允许处理的 component_key：{json.dumps(component_keys, ensure_ascii=False)}。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。\n"
        f"price_batch_id：{price_batch_id}。"
    )
