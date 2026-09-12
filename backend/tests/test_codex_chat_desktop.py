from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def desktop_module():
    path = Path(__file__).resolve().parents[2] / "tools/codex_chat_desktop.py"
    spec = importlib.util.spec_from_file_location("codex_chat_desktop_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_sales_prompt_must_start_with_real_astraquote_mention(desktop_module):
    assert desktop_module.split_astraquote_prompt(
        "@AstraQuote 请完成正式报价。\n客户需求：一台云服务器"
    ) == "请完成正式报价。\n客户需求：一台云服务器"

    with pytest.raises(ValueError, match="@AstraQuote"):
        desktop_module.split_astraquote_prompt("请使用 AstraQuote 完成正式报价。")


def test_codex_chat_reference_is_stable_and_rejects_web_urls(desktop_module):
    thread_id = "6aa52a28-5510-83ee-b69a-42c10c9f1ddb"
    reference = desktop_module.codex_chat_reference(thread_id)

    assert reference == f"codex-chat://conversations/{thread_id}"
    assert desktop_module.conversation_id_from_reference(reference) == thread_id
    with pytest.raises(ValueError):
        desktop_module.conversation_id_from_reference(
            "https://chatgpt.com/c/6aa52a28-5510-83ee-b69a-42c10c9f1ddb"
        )


def test_rich_mention_selector_requires_plugin_markup(desktop_module):
    assert "plugin-mention-display-name" in desktop_module.RICH_MENTION_SELECTOR
    assert "AstraQuote" in desktop_module.RICH_MENTION_SELECTOR


def test_new_chat_clears_a_stale_unsent_draft(desktop_module, monkeypatch):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=60,
    )
    dispatched = []

    monkeypatch.setattr(desktop, "_sidebar_ids", lambda: [])
    monkeypatch.setattr(desktop, "_open_deep_link", lambda _link: None)
    monkeypatch.setattr(desktop, "_evaluate", lambda _expression: True)
    monkeypatch.setattr(desktop, "_chat_surface_ready", lambda: True)
    monkeypatch.setattr(
        desktop,
        "_wait_until",
        lambda check, **_kwargs: check(),
    )
    monkeypatch.setattr(
        desktop,
        "_dispatch_key",
        lambda key, code, **kwargs: dispatched.append((key, code, kwargs)),
    )

    desktop._open_new_chat()

    assert dispatched == [
        ("a", "KeyA", {"modifiers": 2}),
        ("Backspace", "Backspace", {}),
    ]
