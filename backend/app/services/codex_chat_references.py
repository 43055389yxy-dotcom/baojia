"""Validate opaque Codex Chat conversation references.

Codex exposes a local conversation id immediately after a Chat conversation is
created and may replace it with a server id after synchronization.  Both ids
are opaque navigation handles; neither is a web URL.
"""

from __future__ import annotations

import re

_UUID_SOURCE = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
CODEX_CONVERSATION_ID_PATTERN = re.compile(
    rf"^(?:{_UUID_SOURCE}|local-chatgpt:{_UUID_SOURCE})$",
    re.IGNORECASE,
)
CODEX_CHAT_REFERENCE_PREFIX = "codex-chat://conversations/"


def is_codex_conversation_id(value: object) -> bool:
    return bool(CODEX_CONVERSATION_ID_PATTERN.fullmatch(str(value).strip()))


def is_codex_chat_reference(value: object) -> bool:
    reference = str(value).strip()
    if not reference.startswith(CODEX_CHAT_REFERENCE_PREFIX):
        return False
    return is_codex_conversation_id(
        reference.removeprefix(CODEX_CHAT_REFERENCE_PREFIX)
    )

