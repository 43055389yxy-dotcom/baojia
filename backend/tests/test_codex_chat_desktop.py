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


def test_codex_chat_reference_accepts_strict_local_conversation_id(
    desktop_module,
):
    local_id = "local-chatgpt:2995f22c-1bfa-414c-8f65-cebe43aa23cf"

    reference = desktop_module.codex_chat_reference(local_id)

    assert reference == f"codex-chat://conversations/{local_id}"
    assert desktop_module.conversation_id_from_reference(reference) == local_id
    with pytest.raises(ValueError):
        desktop_module.codex_chat_reference("local-chatgpt:not-a-uuid")


def test_sidebar_conversations_include_local_and_server_ids(
    desktop_module, monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=600,
    )
    local_id = "local-chatgpt:2995f22c-1bfa-414c-8f65-cebe43aa23cf"
    server_id = "6aa52a28-5510-83ee-b69a-42c10c9f1ddb"
    monkeypatch.setattr(
        desktop,
        "_evaluate",
        lambda _expression: [local_id, server_id, "local-chatgpt:invalid"],
    )

    assert desktop._sidebar_ids() == [local_id, server_id]


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


def test_switching_multi_batch_quote_requires_the_exact_batch_marker(
    desktop_module, monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=600,
    )
    quote = type(
        "Quote",
        (),
        {
            "job_id": "gpt-0123456789abcdef0123456789abcdef",
            "chat_url": desktop_module.codex_chat_reference(
                "6aa52a28-5510-83ee-b69a-42c10c9f1ddb"
            ),
            "previous_conversation_ids": (),
            "batch_index": 1,
            "batch_count": 3,
            "role": "component_batch",
        },
    )()
    expressions = []

    def evaluate(expression):
        expressions.append(expression)
        if "const jobId" in expression:
            return True
        return False

    monkeypatch.setattr(desktop, "_evaluate", evaluate)

    desktop._switch_to_quote(quote)

    identity_expression = next(item for item in expressions if "const jobId" in item)
    assert quote.job_id in identity_expression
    assert "relay_batch_index：1" in identity_expression
    assert "当前为第 2/3 批" in identity_expression


def test_rich_mention_selector_requires_plugin_markup(desktop_module):
    assert "plugin-mention-display-name" in desktop_module.RICH_MENTION_SELECTOR
    assert "AstraQuote" in desktop_module.RICH_MENTION_SELECTOR


def test_send_prompt_clicks_visible_send_control_and_confirms_new_turn(
    desktop_module, monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=60,
    )
    expressions = []
    counts = iter([2, 3])

    monkeypatch.setattr(desktop, "_user_message_count", lambda: next(counts))
    monkeypatch.setattr(desktop, "_call", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(desktop, "_dispatch_key", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(desktop, "_wait_until", lambda check, **_kwargs: check())

    def evaluate(expression):
        expressions.append(expression)
        if "return 'ready'" in expression:
            return "ready"
        if "data-list-navigation-item" in expression:
            return True
        if "已发送消息" in expression:
            return True
        return True

    monkeypatch.setattr(desktop, "_evaluate", evaluate)

    desktop._send_prompt("@AstraQuote 正式报价")

    assert any(
        "send-button" in expression and ".click()" in expression
        for expression in expressions
    )


def test_switch_refuses_to_leave_a_conversation_with_an_unsent_draft(
    desktop_module, monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=60,
    )
    quote = type("Quote", (), {
        "job_id": "gpt-0123456789abcdef0123456789abcdef",
        "chat_url": desktop_module.codex_chat_reference(
            "6aa52a28-5510-83ee-b69a-42c10c9f1ddb"
        ),
        "previous_conversation_ids": (),
        "batch_index": 0,
        "batch_count": 1,
        "role": "coordinator",
    })()
    sidebar_clicked = []

    def evaluate(expression):
        if "const jobId" in expression:
            return False
        if "pending_astraquote" in expression:
            return "pending_other"
        if "SIDEBAR" in expression:
            sidebar_clicked.append(True)
        return False

    monkeypatch.setattr(desktop, "_evaluate", evaluate)

    with pytest.raises(desktop_module.PendingPromptSubmissionError):
        desktop._switch_to_quote(quote)
    assert not sidebar_clicked


def test_switch_submits_pending_astraquote_draft_before_navigation(
    desktop_module,
    monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=60,
    )
    quote = type("Quote", (), {
        "job_id": "gpt-0123456789abcdef0123456789abcdef",
        "chat_url": desktop_module.codex_chat_reference(
            "6aa52a28-5510-83ee-b69a-42c10c9f1ddb"
        ),
        "previous_conversation_ids": (),
        "batch_index": 0,
        "batch_count": 1,
        "role": "coordinator",
    })()
    submitted = []

    def evaluate(expression):
        if "const jobId" in expression:
            return False
        if "pending_astraquote" in expression:
            return "pending_astraquote"
        return False

    monkeypatch.setattr(desktop, "_evaluate", evaluate)
    monkeypatch.setattr(desktop, "_user_message_count", lambda: 3)
    monkeypatch.setattr(
        desktop,
        "_submit_composer_and_confirm",
        lambda count: submitted.append(count),
    )

    with pytest.raises(desktop_module.PendingPromptSubmissionError):
        desktop._switch_to_quote(quote)
    assert submitted == [3]


def test_switch_submits_pending_draft_even_when_quote_is_already_current(
    desktop_module,
    monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=60,
    )
    quote = type("Quote", (), {
        "job_id": "gpt-0123456789abcdef0123456789abcdef",
        "chat_url": desktop_module.codex_chat_reference(
            "6aa52a28-5510-83ee-b69a-42c10c9f1ddb"
        ),
        "previous_conversation_ids": (),
        "batch_index": 0,
        "batch_count": 1,
        "role": "coordinator",
    })()
    submitted = []

    def evaluate(expression):
        if "const jobId" in expression:
            return True
        if "pending_astraquote" in expression:
            return "pending_astraquote"
        return False

    monkeypatch.setattr(desktop, "_evaluate", evaluate)
    monkeypatch.setattr(desktop, "_user_message_count", lambda: 4)
    monkeypatch.setattr(
        desktop,
        "_submit_composer_and_confirm",
        lambda count: submitted.append(count),
    )

    with pytest.raises(desktop_module.PendingPromptSubmissionError):
        desktop._switch_to_quote(quote)
    assert submitted == [4]


def test_ready_for_next_turn_requires_idle_generation_and_ready_composer(
    desktop_module,
    monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=60,
    )
    quote = object()
    monkeypatch.setattr(desktop, "_switch_to_quote", lambda _quote: None)
    monkeypatch.setattr(desktop, "_scroll_to_latest", lambda: None)
    monkeypatch.setattr(desktop, "_approve_tool_if_needed", lambda: False)
    monkeypatch.setattr(desktop, "_generation_active", lambda: False)
    monkeypatch.setattr(desktop, "_composer_ready_for_new_turn", lambda: True)

    assert desktop.ready_for_next_turn(quote)

    monkeypatch.setattr(desktop, "_generation_active", lambda: True)
    assert not desktop.ready_for_next_turn(quote)


def test_ready_for_next_turn_waits_after_approving_a_tool_card(
    desktop_module,
    monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=60,
    )
    monkeypatch.setattr(desktop, "_switch_to_quote", lambda _quote: None)
    monkeypatch.setattr(desktop, "_scroll_to_latest", lambda: None)
    monkeypatch.setattr(desktop, "_approve_tool_if_needed", lambda: True)
    monkeypatch.setattr(desktop, "_generation_active", lambda: False)
    monkeypatch.setattr(desktop, "_composer_ready_for_new_turn", lambda: True)

    assert not desktop.ready_for_next_turn(object())


def test_continue_quote_arms_new_response_boundary_before_uncertain_send(
    desktop_module, monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=60,
    )
    quote = type("Quote", (), {})()
    quote.minimum_assistant_messages = 1
    quote.last_text = "上一轮已完成"
    quote.stable_since = 0.0
    quote.deadline = 0.0
    quote.saw_assistant = True
    quote.retry_visible_since = 1.0
    quote.retry_clicked = True
    quote.generation_grace_used = True
    monkeypatch.setattr(desktop, "_switch_to_quote", lambda _quote: None)
    monkeypatch.setattr(desktop, "_assistant_messages", lambda: ["旧回答一", "旧回答二"])

    def uncertain_send(_prompt):
        raise desktop_module.PendingPromptSubmissionError("send confirmation timed out")

    monkeypatch.setattr(desktop, "_send_prompt", uncertain_send)

    with pytest.raises(desktop_module.PendingPromptSubmissionError):
        desktop.continue_quote(quote, "第二轮")

    assert quote.minimum_assistant_messages == 3
    assert quote.last_text == ""
    assert quote.saw_assistant is False
    assert quote.retry_visible_since is None
    assert quote.retry_clicked is False
    assert quote.generation_grace_used is False


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


def test_start_revives_codex_when_renderer_opens_but_chat_surface_is_stuck(
    desktop_module, monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=60,
    )
    attempts = []
    recovered = []

    monkeypatch.setattr(desktop, "_connect", lambda: attempts.append("connect"))

    def prepare_surface():
        attempts.append("surface")
        if attempts.count("surface") == 1:
            raise TimeoutError("renderer stuck")

    monkeypatch.setattr(desktop, "_prepare_chat_surface", prepare_surface)
    monkeypatch.setattr(
        desktop,
        "_recover_desktop",
        lambda _error: recovered.append("restarted"),
    )

    desktop.start()

    assert attempts == ["connect", "surface", "connect", "surface"]
    assert recovered == ["restarted"]
    assert desktop.driver is desktop


def test_unavailable_session_recovers_but_real_login_screen_is_left_for_admin(
    desktop_module, monkeypatch,
):
    desktop = desktop_module.CodexChatDesktop(
        active_quote_factory=dict,
        quote_timeout_seconds=60,
    )
    restarted = []
    monkeypatch.setattr(desktop, "_login_screen_visible", lambda: False)
    monkeypatch.setattr(desktop, "close", lambda: restarted.append("closed"))
    monkeypatch.setattr(desktop, "start", lambda: restarted.append("started"))
    monkeypatch.setattr(desktop, "logged_in", lambda: True)

    assert desktop.recover_if_unavailable()
    assert restarted == ["closed", "started"]

    restarted.clear()
    monkeypatch.setattr(desktop, "_login_screen_visible", lambda: True)
    assert not desktop.recover_if_unavailable()
    assert restarted == []


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
