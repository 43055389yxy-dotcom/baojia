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


def test_pending_reference_is_bound_to_the_relay_job(desktop_module):
    job_id = "gpt-0123456789abcdef0123456789abcdef"

    reference = desktop_module.pending_chat_reference(job_id)

    assert reference == f"codex-chat://pending/{job_id}"
    assert desktop_module.is_pending_chat_reference(reference)
    assert not desktop_module.is_pending_chat_reference(
        "codex-chat://conversations/6aa52a28-5510-83ee-b69a-42c10c9f1ddb"
    )


def test_start_quote_keeps_sent_prompt_running_before_sidebar_title_exists(
    desktop_module, monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=600,
    )
    job_id = "gpt-0123456789abcdef0123456789abcdef"
    previous = ["6aa52a28-5510-83ee-b69a-42c10c9f1ddb"]

    monkeypatch.setattr(desktop, "_open_new_chat", lambda: previous)
    monkeypatch.setattr(desktop, "_send_prompt", lambda _prompt: None)
    monkeypatch.setattr(desktop, "_new_conversation_reference", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(desktop_module, "_atomic_json", lambda *_args, **_kwargs: None)

    active = desktop.start_quote(job_id, "@AstraQuote 正式报价")

    assert active["chat_url"] == desktop_module.pending_chat_reference(job_id)
    assert active["previous_conversation_ids"] == tuple(previous)


def test_pending_reference_promotes_when_sidebar_row_appears(
    desktop_module, monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=600,
    )
    job_id = "gpt-0123456789abcdef0123456789abcdef"
    old_id = "6aa52a28-5510-83ee-b69a-42c10c9f1ddb"
    new_id = "6aa5306f-4ed8-83e9-9c0d-466e73829f51"
    active = {
        "job_id": job_id,
        "chat_url": desktop_module.pending_chat_reference(job_id),
        "previous_conversation_ids": (old_id,),
    }
    quote = type("Quote", (), active)()
    monkeypatch.setattr(desktop, "_sidebar_ids", lambda: [new_id, old_id])
    monkeypatch.setattr(desktop_module, "_atomic_json", lambda *_args, **_kwargs: None)

    assert desktop.promote_pending_reference(quote)
    assert quote.chat_url == desktop_module.codex_chat_reference(new_id)
    assert quote.previous_conversation_ids == ()


def test_resume_pending_quote_waits_for_sidebar_without_failing(
    desktop_module, monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=600,
    )
    job_id = "gpt-0123456789abcdef0123456789abcdef"
    monkeypatch.setattr(
        desktop,
        "_switch_to_quote",
        lambda _quote: (_ for _ in ()).throw(
            desktop_module.PendingConversationReferenceError("still pending")
        ),
    )

    active = desktop.resume_quote(
        job_id,
        desktop_module.pending_chat_reference(job_id),
    )

    assert active["chat_url"] == desktop_module.pending_chat_reference(job_id)


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


def test_start_revives_codex_after_the_desktop_window_is_closed(
    desktop_module, monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=60,
    )
    attempts = []
    recovered = []

    def connect():
        attempts.append("connect")
        if len(attempts) == 1:
            raise RuntimeError("Codex desktop control is unavailable")

    monkeypatch.setattr(desktop, "_connect", connect)
    monkeypatch.setattr(
        desktop,
        "_recover_desktop",
        lambda _error: recovered.append("restarted"),
    )
    monkeypatch.setattr(desktop, "_chat_surface_ready", lambda: True)

    desktop.start()

    assert attempts == ["connect", "connect"]
    assert recovered == ["restarted"]
    assert desktop.driver is desktop


@pytest.mark.parametrize(
    ("running", "expected_action"),
    [("true", "restart"), ("false", "start")],
)
def test_desktop_recovery_checks_the_real_container_state(
    desktop_module, monkeypatch, running, expected_action,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=60,
    )
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[1] == "inspect":
            return type("Result", (), {"stdout": f"{running}\n"})()
        return type("Result", (), {"stdout": ""})()

    monkeypatch.setattr(desktop_module.subprocess, "run", run)
    monkeypatch.setattr(desktop, "_wait_until", lambda check, **_kwargs: check())
    monkeypatch.setattr(desktop, "_cdp_target_available", lambda: True)

    desktop._recover_desktop(RuntimeError("window closed"))

    assert commands[0][1:3] == ["inspect", "--format"]
    assert commands[1] == ["docker", expected_action, desktop.container_name]
