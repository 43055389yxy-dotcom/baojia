"""Build isolated component batches for one-window quote chat automation.

Numbered sales lines are mechanically isolated before first-pass AI cleaning;
later continuation prompts contain only cleaned component sources. A parent
and all descendants stay in the same chat, preserving the irreversible
ownership boundary while the worker visits chats through one logged-in window.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any

COMPONENTS_PER_WAVE = 5
WAVES_PER_CHAT = 2
COMPONENTS_PER_CHAT = COMPONENTS_PER_WAVE * WAVES_PER_CHAT
ASTRAQUOTE_MENTION = "@AstraQuote"
MAX_NUMBERED_COMPONENTS = 200
_NUMBERED_COMPONENT_LINE = re.compile(
    r"^\s*(?:需求\s*)?(?:[（(]\s*)?(?P<number>\d{1,3})(?:\s*[)）])?"
    r"\s*[、,，.．。:：;；\-—]\s*(?P<body>\S.*)$"
)


def parse_numbered_component_lines(customer_request: str) -> list[dict[str, Any]]:
    """Mechanically validate one numbered top-level component per real line.

    This boundary deliberately does not interpret product names, quantities or
    specifications.  It only establishes stable ownership slices before each
    slice is handed to the first-pass AI cleaner.
    """

    nonempty_lines = [
        (line_number, line.strip())
        for line_number, line in enumerate(str(customer_request).splitlines(), start=1)
        if line.strip()
    ]
    if not nonempty_lines:
        raise ValueError("客户需求不能为空。")
    if len(nonempty_lines) > MAX_NUMBERED_COMPONENTS:
        raise ValueError(f"客户需求最多支持 {MAX_NUMBERED_COMPONENTS} 个组件。")

    components: list[dict[str, Any]] = []
    for expected, (line_number, line) in enumerate(nonempty_lines, start=1):
        match = _NUMBERED_COMPONENT_LINE.fullmatch(line)
        if match is None:
            raise ValueError(
                f"第 {line_number} 行必须以连续序号 {expected}. 开头，并且一行只写一个组件。"
            )
        actual = int(match.group("number"))
        if actual != expected:
            raise ValueError(
                f"第 {line_number} 行序号应为 {expected}，当前为 {actual}。"
            )
        components.append(
            {
                "component_number": expected,
                "component_key": f"cmp_intake_{expected:04d}",
                "source_line": line,
            }
        )
    return components


def split_numbered_intake(
    components: list[dict[str, Any]],
    *,
    maximum_components: int = COMPONENTS_PER_WAVE,
) -> list[list[dict[str, Any]]]:
    """Split the mechanically numbered sales intake without reading its meaning."""

    if maximum_components < 1:
        raise ValueError("maximum_components must be positive")
    return [
        components[offset : offset + maximum_components]
        for offset in range(0, len(components), maximum_components)
    ]


def build_numbered_intake_batch_prompt(
    *,
    relay_job_id: str,
    submission_code: str,
    price_batch_id: str,
    batch_index: int,
    batch_count: int,
    components: list[dict[str, Any]],
    quote_context: str,
) -> str:
    """Create the first-pass prompt for exactly one pre-split intake batch."""

    conversation_index = batch_index // WAVES_PER_CHAT
    conversation_count = (batch_count + WAVES_PER_CHAT - 1) // WAVES_PER_CHAT
    conversation_start = conversation_index * WAVES_PER_CHAT
    conversation_wave_count = min(
        WAVES_PER_CHAT,
        max(1, batch_count - conversation_start),
    )
    wave_index = batch_index - conversation_start
    owned_lines = "\n".join(
        f"[component_key={item['component_key']}] {item['source_line']}"
        for item in components
    )
    return (
        f"{ASTRAQUOTE_MENTION} 请使用 AstraQuote 完成正式报价。"
        f"这是同一张报价的第 {conversation_index + 1}/{conversation_count} 个对话，"
        f"当前为本对话第 {wave_index + 1}/{conversation_wave_count} 轮，"
        f"也是整单第 {batch_index + 1}/{batch_count} 个执行小批，"
        "只处理并保存本批；全部批次完成后由后台统一合并交付。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。\n"
        f"price_batch_id：{price_batch_id}\n"
        f"relay_batch_index：{batch_index}\n"
        f"relay_batch_count：{batch_count}\n\n"
        "本批沿用以下整单报价条件：\n"
        f"{quote_context.strip()}\n\n"
        "本批客户需求（每个实际换行是一项独立顶层组件）：\n"
        f"{owned_lines}\n\n"
        "本批全部组件完成选型、核价和金额计算后，必须调用 build_estimate，"
        "设置 delivery_mode=save_component_batch，并原样传入本提示中的 "
        "relay_batch_index、relay_batch_count 和 price_batch_id；只提交本批结果。"
        "工具返回 component_batch_saved 时立即结束本轮；最后一批会由程序自动合并、"
        "校验并生成销售页和 Excel，不要再由 AI 整理整单。"
    )


def split_component_plan(
    components: list[dict[str, Any]],
    *,
    maximum_top_level_components: int = COMPONENTS_PER_WAVE,
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
        "本批全部组件完成选型、核价和金额计算后，必须调用 build_estimate，"
        "设置 delivery_mode=save_component_batch，并原样传入本提示中的批次编号；"
        "只提交本批 Fact Ledger、组件结果、方案费用和本批小计。"
        "工具返回 component_batch_saved 时结束本轮；最后一批由程序自动合并、"
        "核对并生成 Excel。不要生成最终整单，也不要自行整理跨批次内容。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。\n"
        f"price_batch_id：{price_batch_id}\n"
        f"relay_batch_index：{batch_index}\n"
        f"relay_batch_count：{batch_count}\n\n"
        "本批必须沿用以下整单报价条件：\n"
        f"{quote_context.strip()}\n\n"
        "本批已清洗组件（JSON）：\n"
        f"{json.dumps(safe_components, ensure_ascii=False, separators=(',', ':'))}"
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
        "请先读取后台组件状态，只把本批尚未完成的组件重新组织后补查一次，"
        "已经成功的查询必须复用，不得处理其他批次，也不要生成最终整单。"
        "补查结束后必须调用 build_estimate，设置 delivery_mode=save_component_batch，"
        "只保存本批最终组件结果；最后一批由程序自动合并并生成 Excel。"
        f"当前为第 {batch_index + 1}/{batch_count} 批；"
        f"允许处理的 component_key：{json.dumps(component_keys, ensure_ascii=False)}。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。\n"
        f"price_batch_id：{price_batch_id}\n"
        f"relay_batch_index：{batch_index}\n"
        f"relay_batch_count：{batch_count}。"
    )


def build_component_batch_finalize_prompt(
    *,
    relay_job_id: str,
    submission_code: str,
    price_batch_id: str,
    batch_index: int,
    batch_count: int,
    component_keys: list[str],
) -> str:
    """Ask for the missing structured save after all prices are already durable."""

    return (
        f"{ASTRAQUOTE_MENTION} 本批价格证据已经全部保存，不要重新查价。"
        "现在只完成本批结构化收口：调用 build_estimate，"
        "设置 delivery_mode=save_component_batch，只提交本批 Fact Ledger、组件结果、"
        "销售所选全部方案费用和本批小计。不要读取其他批次，不要由 AI 合并整单。"
        f"允许提交的 component_key：{json.dumps(component_keys, ensure_ascii=False)}。\n\n"
        f"交付信息：提交码 {submission_code}；内部任务编号 {relay_job_id}。\n"
        f"price_batch_id：{price_batch_id}\n"
        f"relay_batch_index：{batch_index}\n"
        f"relay_batch_count：{batch_count}。"
    )
