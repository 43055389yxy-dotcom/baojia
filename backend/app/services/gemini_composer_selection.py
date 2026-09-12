"""Select the correct Gemini Spark composer without fixed screen coordinates."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

PROMPT_LABEL_PATTERN = re.compile(
    r"为\s*Gemini\s*输入提示|Enter a prompt|Ask Gemini|描述任务|Describe a task|接下来要做些什么",
    re.IGNORECASE,
)


def choose_composer_candidate(
    candidates: Sequence[Mapping[str, Any]],
    *,
    new_task: bool,
    viewport_width: float,
    viewport_height: float,
) -> Mapping[str, Any] | None:
    """Choose the left task creator or the main conversation composer.

    Gemini gives both editors the same accessible label.  Their stable
    distinction is layout: the task creator is in the upper-left workspace,
    while the active-conversation composer is in the lower main pane.
    """

    usable = [
        item
        for item in candidates
        if float(item.get("width") or 0) > 20
        and float(item.get("height") or 0) > 10
        and float(item.get("left") or 0) >= 0
        and float(item.get("top") or 0) >= 0
        and PROMPT_LABEL_PATTERN.search(str(item.get("label") or ""))
    ]
    if new_task:
        pool = [
            item
            for item in usable
            if float(item.get("left") or 0) < viewport_width * 0.55
            and float(item.get("top") or 0) < viewport_height * 0.5
        ]
        return min(
            pool,
            key=lambda item: (
                float(item.get("top") or 0),
                float(item.get("left") or 0),
            ),
            default=None,
        )

    pool = [
        item
        for item in usable
        if float(item.get("left") or 0) >= viewport_width * 0.3
        and float(item.get("top") or 0) >= viewport_height * 0.35
    ]
    return max(
        pool,
        key=lambda item: (
            float(item.get("top") or 0),
            float(item.get("left") or 0),
        ),
        default=None,
    )
