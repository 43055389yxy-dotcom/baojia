from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.services.gpt_quote_batches import parse_numbered_component_lines
from app.services.gpt_quote_relay import GptQuoteRelayStore


def codex_chat(index: int) -> str:
    return f"codex-chat://conversations/00000000-0000-4000-8000-{index:012d}"


def local_codex_chat(index: int) -> str:
    return f"codex-chat://conversations/local-chatgpt:00000000-0000-4000-8000-{index:012d}"


@pytest.fixture
def worker(monkeypatch):
    """Load the real worker without needing a browser driver in backend tests."""
    modules = {
        "selenium": {"webdriver": Mock()},
        "selenium.common": {},
        "selenium.common.exceptions": {"WebDriverException": RuntimeError},
        "selenium.webdriver": {},
        "selenium.webdriver.common": {},
        "selenium.webdriver.common.by": {"By": Mock()},
        "selenium.webdriver.common.keys": {"Keys": Mock()},
        "selenium.webdriver.firefox": {},
        "selenium.webdriver.firefox.options": {"Options": Mock()},
        "selenium.webdriver.remote": {},
        "selenium.webdriver.remote.webelement": {"WebElement": Mock()},
        "selenium.webdriver.support": {},
        "selenium.webdriver.support.ui": {"WebDriverWait": Mock()},
    }
    for name, attributes in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    path = Path(__file__).resolve().parents[2] / "tools/gpt_quote_relay_worker.py"
    spec = importlib.util.spec_from_file_location("relay_worker_behavior", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def running_job(tmp_path):
    checkpoints = tmp_path / "checkpoints"
    store = GptQuoteRelayStore(tmp_path / "relay", checkpoint_directory=checkpoints)
    job_id = store.create("测试组件。", {})["job_id"]
    store.claim_next("test-worker")
    checkpoints.mkdir()
    checkpoint_path = checkpoints / f"relay-{job_id}.json"
    return store, job_id, checkpoint_path


@pytest.mark.parametrize("marker", ["displayed_on_page", "delivered"])
def test_text_delivery_claim_cannot_complete_without_machine_receipt(worker, running_job, marker):
    store, job_id, _ = running_job
    outcome = worker.complete_job(
        store, job_id, f"ASTRAQUOTE_STATUS: {marker}\nASTRAQUOTE_SUMMARY: 已完成"
    )
    assert outcome == "continue"
    assert store.get(job_id)["status"] == "processing"


def test_known_unrecoverable_failure_stops_without_nudging(worker, running_job):
    store, job_id, checkpoint_path = running_job
    checkpoint_path.write_text(json.dumps({
        "relay_job_id": job_id, "stage": "failed",
        "error": {"code": "permission_denied", "retryable": False},
    }), encoding="utf-8")
    outcome = worker.complete_job(
        store, job_id, "ASTRAQUOTE_STOP_CODE: AQ-QUOTE-BLOCKED\nASTRAQUOTE_SUMMARY: 权限不足"
    )
    assert outcome == "failed"
    assert store.get(job_id)["status"] == "failed"


def test_known_blocker_preserves_available_successes_for_one_partial_delivery(worker, running_job):
    store, job_id, checkpoint_path = running_job
    checkpoint_path.write_text(json.dumps({
        "relay_job_id": job_id, "stage": "pricing_partial", "quote_terminal": True,
        "total_component_count": 40, "completed_component_count": 38,
    }), encoding="utf-8")
    assert worker.complete_job(
        store, job_id, "ASTRAQUOTE_STATUS: blocked\nASTRAQUOTE_SUMMARY: 两项不可继续"
    ) == "partial_finalize"
    browser = Mock()
    active = worker.ActiveQuote(job_id, "https://chatgpt.com/c/example", 100)
    checkpoint_path.write_text(json.dumps({
        "relay_job_id": job_id, "stage": "artifacts_generated",
        "total_component_count": 40, "completed_component_count": 38,
    }), encoding="utf-8")
    assert not worker.continue_from_saved_stage(store, browser, active, message="续跑")
    browser.continue_quote.assert_not_called()


def test_partial_retry_reuses_only_coordinator_after_queue_claim(worker, running_job):
    store, job_id, _ = running_job
    for index in range(3):
        store.record_chat_session(
            job_id, batch_index=index, batch_count=3,
            chat_url=codex_chat(index),
            role="coordinator" if index == 0 else "component_batch",
            component_keys=[f"cmp-{index}"],
        )
    store.purge_source(job_id)
    store.update(job_id, {
        "status": "partial",
        "quick_quote_result": {
            "unpriced_components": [{"service_name": "未完成", "retryable": True}],
        },
    })
    assert store.retry_partial(job_id)["status"] == "queued"
    record = store.claim_next("worker-retry")
    assert record is not None and record["status"] == "processing"
    assert record["customer_request"] == ""
    browser = Mock()
    browser.resume_quote.side_effect = (
        lambda job, url, **kw: worker.ActiveQuote(job, url, 100, **kw)
    )
    active = {}
    worker.reattach_job_chats(store, browser, record, active)
    assert list(active) == [f"{job_id}:0"]
    assert browser.resume_quote.call_count == 1
    assert browser.continue_quote.call_count == 1


def test_batch_failure_state_churn_is_not_real_progress(worker, running_job, monkeypatch):
    store, job_id, _ = running_job
    store.record_chat_session(
        job_id, batch_index=1, batch_count=2,
        chat_url=codex_chat(1), role="component_batch", component_keys=["a", "b"],
    )
    active = worker.ActiveQuote(job_id, "https://chatgpt.com/c/child", 100, batch_index=1)
    batch = {
        "component_states": {"a": "completed", "b": "pending"},
        "price_batch_id": "aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "batch_count": 2, "component_keys": ["a", "b"],
    }
    monkeypatch.setattr(worker, "active_batch", lambda *_: batch)
    browser = Mock()
    assert worker.continue_component_batch(store, browser, active)
    prompt = browser.continue_quote.call_args.args[1]
    assert '"b"' in prompt
    assert '"a"' not in prompt
    assert not worker.continue_component_batch(store, browser, active)
    batch["component_states"]["a"] = "pending"
    assert not worker.continue_component_batch(store, browser, active)
    assert browser.continue_quote.call_count == 1


def test_single_chat_retries_only_incomplete_components_once_then_forces_official_page(
    worker, running_job, monkeypatch,
):
    store, job_id, checkpoint_path = running_job
    batch = {
        "batch_index": 0,
        "batch_count": 1,
        "price_batch_id": "aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "component_keys": ["done", "failed", "pending"],
        "component_states": {
            "done": "completed", "failed": "failed", "pending": "pending",
        },
    }
    checkpoint_path.write_text(json.dumps({
        "relay_job_id": job_id,
        "stage": "pricing_partial",
        "quote_terminal": True,
        "total_component_count": 3,
        "completed_component_count": 1,
        "completed_component_keys": ["done"],
    }), encoding="utf-8")
    monkeypatch.setattr(store, "quote_chat_batches", lambda *_: [batch])
    browser = Mock()
    active = worker.ActiveQuote(job_id, codex_chat(0), 100)

    assert worker.complete_job(
        store, job_id, "ASTRAQUOTE_STATUS: blocked\nASTRAQUOTE_SUMMARY: 两项失败",
    ) == "continue"
    assert worker.continue_from_saved_stage(store, browser, active, message="续跑")
    retry_prompt = browser.continue_quote.call_args.args[1]
    assert '"failed"' in retry_prompt
    assert '"pending"' in retry_prompt
    assert '"done"' not in retry_prompt

    assert worker.continue_from_saved_stage(store, browser, active, message="续跑")
    official_page_prompt = browser.continue_quote.call_args.args[1]
    assert "官方价格页" in official_page_prompt
    assert "不得标注销售手填" in official_page_prompt
    assert browser.continue_quote.call_count == 2


def test_failed_component_is_not_treated_as_completed_before_its_one_retry(worker):
    assert not worker.batch_is_finished({
        "component_keys": ["a"], "component_states": {"a": "failed"},
    })
    assert not worker.batch_is_finished({
        "component_keys": ["a"], "component_states": {"a": "completed"},
        "component_result_required": True,
    })
    assert worker.batch_is_finished({
        "component_keys": ["a"], "component_states": {"a": "completed"},
        "component_result_required": True, "component_result_saved": True,
    })
    assert not worker.batch_is_finished({
        "component_keys": ["a", "b"], "component_states": {"a": "completed"},
        "component_result_required": False, "component_result_saved": True,
    })


def test_unconfirmed_ui_send_does_not_consume_component_retry(worker, running_job, monkeypatch):
    store, job_id, _ = running_job
    store.record_chat_session(
        job_id, batch_index=1, batch_count=2,
        chat_url=codex_chat(1), role="component_batch", component_keys=["a"],
    )
    active = worker.ActiveQuote(job_id, "https://chatgpt.com/c/child", 100, batch_index=1)
    batch = {"component_states": {"a": "pending"}, "price_batch_id": "aqpb_id",
             "batch_count": 2, "component_keys": ["a"]}
    monkeypatch.setattr(worker, "active_batch", lambda *_: batch)
    browser = Mock()
    browser.continue_quote.side_effect = RuntimeError("delivery was uncertain")
    with pytest.raises(RuntimeError):
        worker.continue_component_batch(store, browser, active)
    assert store.get(job_id)["chat_sessions"][0].get("stalled_attempts", 0) == 0


def test_completed_prices_trigger_one_small_batch_result_save_not_a_retry(
    worker, running_job, monkeypatch,
):
    store, job_id, _ = running_job
    store.record_chat_session(
        job_id, batch_index=0, batch_count=2,
        chat_url=codex_chat(0), role="coordinator", component_keys=["a"],
    )
    batch = {
        "batch_index": 0, "batch_count": 2,
        "price_batch_id": "aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "component_keys": ["a"], "component_states": {"a": "completed"},
        "component_result_required": True, "component_result_saved": False,
    }
    monkeypatch.setattr(worker, "active_batch", lambda *_: batch)
    active = worker.ActiveQuote(job_id, codex_chat(0), 100, batch_count=2)
    browser = Mock()
    assert worker.continue_component_batch(store, browser, active)
    prompt = browser.continue_quote.call_args.args[1]
    assert "delivery_mode=save_component_batch" in prompt
    assert "不要重新查价" in prompt
    assert active.stalled_attempts == 0
    assert active.finalize_attempts == 1
    assert not worker.continue_component_batch(store, browser, active)
    assert browser.continue_quote.call_count == 1
    assert store.get(job_id)["chat_sessions"][0]["status"] == "stalled"


def test_saved_batch_advances_without_reopening_a_final_merge_chat(
    worker, running_job, monkeypatch,
):
    store, job_id, _ = running_job
    store.record_chat_session(
        job_id, batch_index=0, batch_count=1,
        chat_url=codex_chat(0), role="coordinator", component_keys=["a"],
    )
    batch = {
        "batch_index": 0, "batch_count": 1,
        "price_batch_id": "aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "component_keys": ["a"], "component_states": {"a": "completed"},
        "component_result_saved": True, "automatic_delivery_pending": False,
    }
    monkeypatch.setattr(worker, "active_batch", lambda *_: batch)
    active = worker.ActiveQuote(job_id, codex_chat(0), 100)
    browser = Mock()

    assert not worker.continue_component_batch(store, browser, active)
    browser.continue_quote.assert_not_called()
    assert store.get(job_id)["chat_sessions"][0]["status"] == "saved"


@pytest.mark.parametrize("status", ["partial", "completed", "cancelled"])
def test_late_worker_failure_cannot_overwrite_delivery_or_cancellation(worker, running_job, status):
    store, job_id, _ = running_job
    store.update(job_id, {"status": status})
    worker.fail_continuation_limit(store, job_id)
    assert store.get(job_id)["status"] == status


def test_new_visible_prose_cannot_extend_expired_no_progress_deadline(worker, monkeypatch):
    browser = worker.CodexChatDesktop(
        active_quote_factory=worker.ActiveQuote,
        quote_timeout_seconds=600,
    )
    browser._switch_to_quote = Mock()
    browser._scroll_to_latest = Mock()
    browser._approve_tool_if_needed = lambda: False
    browser._text_control_visible = lambda _: False
    browser._assistant_messages = lambda: ["仍在处理，准备继续查询"]
    browser._generation_active = lambda: False
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100)
    active = worker.ActiveQuote(
        "job", codex_chat(99), 90, last_text="之前的状态", generation_grace_used=True,
    )
    with pytest.raises(TimeoutError):
        browser.poll_quote(active)
    assert active.deadline == 90


def test_running_component_batch_cannot_extend_expired_no_progress_deadline(
    worker, monkeypatch,
):
    browser = worker.CodexChatDesktop(
        active_quote_factory=worker.ActiveQuote,
        quote_timeout_seconds=600,
    )
    browser._switch_to_quote = Mock()
    browser._scroll_to_latest = Mock()
    browser._approve_tool_if_needed = lambda: False
    browser._text_control_visible = lambda _: False
    browser._assistant_messages = lambda: ["仍在调用工具"]
    browser._generation_active = lambda: True
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100)
    active = worker.ActiveQuote(
        "job", codex_chat(98), 90, batch_index=1, batch_count=3,
        role="component_batch",
    )

    with pytest.raises(TimeoutError):
        browser.poll_quote(active)
    assert active.deadline == 90


def test_no_progress_timeout_stops_generation_before_single_batch_continuation(
    worker, running_job, monkeypatch,
):
    store, job_id, _ = running_job
    store.record_chat_session(
        job_id, batch_index=0, batch_count=2,
        chat_url=codex_chat(0), role="coordinator", component_keys=["a"],
    )
    active = worker.ActiveQuote(
        job_id, codex_chat(0), 90, batch_index=0, batch_count=2,
        role="coordinator", component_keys=["a"],
    )
    monkeypatch.setattr(store, "quote_chat_batches", lambda *_: [{
        "batch_index": 0,
        "batch_count": 2,
        "price_batch_id": "aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "component_keys": ["a"],
        "component_states": {"a": "pending"},
    }])
    calls = []
    browser = Mock()
    browser.close_quote.side_effect = lambda *_: calls.append("stop")
    browser.continue_quote.side_effect = lambda *_: calls.append("continue")

    worker.handle_no_progress_timeout(
        store, browser, active, {active.session_key: active},
    )

    assert calls == ["stop", "continue"]


def test_second_batch_timeout_stops_only_that_batch_and_leaves_other_batches_running(
    worker, running_job, monkeypatch,
):
    store, job_id, _ = running_job
    for index in range(2):
        store.record_chat_session(
            job_id, batch_index=index, batch_count=2,
            chat_url=codex_chat(index),
            role="coordinator" if index == 0 else "component_batch",
            component_keys=[f"cmp-{index}"],
        )
    timed_out = worker.ActiveQuote(
        job_id, codex_chat(0), 90, batch_index=0, batch_count=2,
        role="coordinator", component_keys=["cmp-0"], stalled_attempts=1,
    )
    still_running = worker.ActiveQuote(
        job_id, codex_chat(1), 90, batch_index=1, batch_count=2,
        role="component_batch", component_keys=["cmp-1"],
    )
    batches = [{
        "batch_index": index,
        "batch_count": 2,
        "price_batch_id": "aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "component_keys": [f"cmp-{index}"],
        "component_states": {f"cmp-{index}": "pending"},
    } for index in range(2)]
    monkeypatch.setattr(store, "quote_chat_batches", lambda *_: batches)
    active = {
        timed_out.session_key: timed_out,
        still_running.session_key: still_running,
    }
    browser = Mock()

    worker.handle_no_progress_timeout(store, browser, timed_out, active)

    assert timed_out.session_key not in active
    assert still_running.session_key in active
    browser.continue_quote.assert_not_called()


def test_production_worker_constructs_codex_chat_adapter_not_firefox(worker):
    source = Path(worker.__file__).read_text(encoding="utf-8")
    main_source = source[source.index("def main()") :]

    assert "CodexChatDesktop(" in main_source
    assert "browser = ChatGptBrowser()" not in main_source


def test_worker_recovers_a_stale_codex_control_session_with_backoff(worker):
    browser = Mock()
    browser.logged_in.return_value = False
    browser.recover_if_unavailable.return_value = True

    logged_in, last_recovery, attempted = worker.browser_login_with_recovery(
        browser,
        last_recovery_at=0,
        now=100,
    )

    assert logged_in
    assert last_recovery == 100
    assert attempted
    browser.recover_if_unavailable.assert_called_once_with()

    browser.reset_mock()
    browser.logged_in.return_value = False
    logged_in, last_recovery, attempted = worker.browser_login_with_recovery(
        browser,
        last_recovery_at=95,
        now=100,
    )

    assert not logged_in
    assert last_recovery == 95
    assert not attempted
    browser.recover_if_unavailable.assert_not_called()


def test_numbered_intake_starts_only_three_conversations_with_five_items_each(
    worker,
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(tmp_path / "relay", max_concurrent_quotes=4)
    text = "\n".join(f"{index}. 组件 {index}：配置。" for index in range(1, 45))
    job = store.create(
        text,
        {"cloud_provider": "aws", "preferred_region": "ap-southeast-1"},
        numbered_components=parse_numbered_component_lines(text),
    )
    record = store.claim_next("test-worker")
    assert record is not None
    browser = Mock()
    browser.logged_in.return_value = True
    browser.start_quote.return_value = worker.ActiveQuote(job["job_id"], codex_chat(0), 100)
    browser.start_component_batch.side_effect = lambda job_id, _prompt, **kwargs: (
        worker.ActiveQuote(job_id, codex_chat(kwargs["batch_index"]), 100, **kwargs)
    )

    first = worker.submit_job(store, browser, record)
    assert first is not None
    active = {first.session_key: first}
    worker.create_missing_component_chats(store, browser, active, job["job_id"])

    first_prompt = browser.start_quote.call_args.args[1]
    child_prompts = [call.args[1] for call in browser.start_component_batch.call_args_list]
    assert set(active) == {f"{job['job_id']}:{index}" for index in range(3)}
    assert "cmp_intake_0001" in first_prompt
    assert "cmp_intake_0006" not in first_prompt
    assert "cmp_intake_0011" in child_prompts[0]
    assert "cmp_intake_0016" not in child_prompts[0]
    assert "cmp_intake_0021" in child_prompts[1]
    assert "cmp_intake_0026" not in child_prompts[1]
    internal = store.get(job["job_id"])
    assert internal["source_purged_at"] is None
    assert not internal["intake_batches"][0]["source_lines"]
    assert not internal["intake_batches"][2]["source_lines"]
    assert not internal["intake_batches"][4]["source_lines"]
    assert len(internal["intake_batches"][1]["source_lines"]) == 5


def test_completed_first_wave_sends_second_five_in_same_conversation(
    worker,
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = GptQuoteRelayStore(tmp_path / "relay", max_concurrent_quotes=4)
    text = "\n".join(f"{index}. 组件 {index}：配置。" for index in range(1, 11))
    job = store.create(
        text,
        {"cloud_provider": "aws", "preferred_region": "ap-southeast-1"},
        numbered_components=parse_numbered_component_lines(text),
    )
    record = store.claim_next("test-worker")
    browser = Mock()
    browser.logged_in.return_value = True
    browser.start_quote.return_value = worker.ActiveQuote(job["job_id"], codex_chat(0), 100)
    active = worker.submit_job(store, browser, record)
    assert active is not None
    first_batch = {
        "batch_index": 0,
        "batch_count": 2,
        "price_batch_id": store.get(job["job_id"])["reserved_price_batch_id"],
        "component_keys": [f"cmp_intake_{index:04d}" for index in range(1, 6)],
        "component_states": {
            f"cmp_intake_{index:04d}": "completed" for index in range(1, 6)
        },
    }
    monkeypatch.setattr(worker, "active_batch", lambda *_: first_batch)

    assert worker.continue_component_batch(store, browser, active)

    assert active.batch_index == 1
    assert active.conversation_index == 0
    assert active.component_keys == tuple(
        f"cmp_intake_{index:04d}" for index in range(6, 11)
    )
    assert browser.continue_quote.call_args.args[0] is active
    second_prompt = browser.continue_quote.call_args.args[1]
    assert "cmp_intake_0006" in second_prompt
    assert "cmp_intake_0001" not in second_prompt
    sessions = store.get(job["job_id"])["chat_sessions"]
    assert sessions[0]["chat_url"] == sessions[1]["chat_url"] == codex_chat(0)
    assert sessions[0]["status"] == "saved"
    assert sessions[1]["status"] == "running"


def test_backend_completed_wave_advances_before_assistant_prose_is_polled(
    worker,
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = GptQuoteRelayStore(tmp_path / "relay", max_concurrent_quotes=4)
    text = "\n".join(f"{index}. 组件 {index}：配置。" for index in range(1, 11))
    job = store.create(text, {}, numbered_components=parse_numbered_component_lines(text))
    record = store.claim_next("test-worker")
    browser = Mock()
    browser.logged_in.return_value = True
    browser.start_quote.return_value = worker.ActiveQuote(job["job_id"], codex_chat(0), 100)
    active = worker.submit_job(store, browser, record)
    assert active is not None
    completed = {
        "batch_index": 0,
        "batch_count": 2,
        "price_batch_id": store.get(job["job_id"])["reserved_price_batch_id"],
        "component_keys": [f"cmp_intake_{index:04d}" for index in range(1, 6)],
        "component_states": {
            f"cmp_intake_{index:04d}": "completed" for index in range(1, 6)
        },
    }
    monkeypatch.setattr(worker, "active_batch", lambda *_: completed)
    browser.ready_for_next_turn.return_value = True
    active_quotes = {active.session_key: active}

    assert worker.advance_machine_completed_wave(store, browser, active_quotes)

    assert active.batch_index == 1
    assert "cmp_intake_0006" in browser.continue_quote.call_args.args[1]
    browser.poll_quote.assert_not_called()


def test_backend_completion_does_not_send_while_the_conversation_is_still_busy(
    worker,
    running_job,
    monkeypatch,
) -> None:
    store, job_id, _ = running_job
    store.record_chat_session(
        job_id, batch_index=0, batch_count=2,
        chat_url=codex_chat(0), role="coordinator", component_keys=["a"],
    )
    active = worker.ActiveQuote(
        job_id, codex_chat(0), 100, batch_count=2, component_keys=("a",),
    )
    monkeypatch.setattr(worker, "active_batch", lambda *_: {
        "batch_index": 0,
        "batch_count": 2,
        "price_batch_id": "aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "component_keys": ["a"],
        "component_states": {"a": "completed"},
    })
    browser = Mock()
    browser.ready_for_next_turn.return_value = False

    assert worker.advance_machine_completed_wave(
        store, browser, {active.session_key: active},
    )
    browser.continue_quote.assert_not_called()


def test_completed_wave_fast_path_does_not_starve_an_older_sibling_chat(
    worker,
    running_job,
    monkeypatch,
) -> None:
    store, job_id, _ = running_job
    older = worker.ActiveQuote(
        job_id, codex_chat(0), 100, batch_count=2,
        conversation_index=0, component_keys=("a",), last_polled_at=10.0,
    )
    completed = worker.ActiveQuote(
        job_id, codex_chat(1), 100, batch_index=1, batch_count=2,
        conversation_index=1, component_keys=("b",), last_polled_at=20.0,
    )
    batches = {
        0: {
            "component_keys": ["a"], "component_states": {"a": "pending"},
        },
        1: {
            "component_keys": ["b"], "component_states": {"b": "completed"},
        },
    }
    monkeypatch.setattr(worker, "active_batch", lambda _store, quote: batches[quote.batch_index])
    browser = Mock()

    assert not worker.advance_machine_completed_wave(
        store,
        browser,
        {older.session_key: older, completed.session_key: completed},
    )
    browser.ready_for_next_turn.assert_not_called()


def test_incomplete_backend_state_must_settle_before_the_one_retry(
    worker,
    running_job,
    monkeypatch,
) -> None:
    store, job_id, _ = running_job
    store.record_chat_session(
        job_id, batch_index=0, batch_count=2,
        chat_url=codex_chat(0), role="coordinator", component_keys=["a", "b"],
    )
    active = worker.ActiveQuote(
        job_id, codex_chat(0), 100, batch_count=2, component_keys=("a", "b"),
    )
    batch = {
        "batch_index": 0,
        "batch_count": 2,
        "price_batch_id": "aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "component_keys": ["a", "b"],
        "component_states": {"a": "completed", "b": "pending"},
    }
    monkeypatch.setattr(worker, "active_batch", lambda *_: batch)
    monkeypatch.setattr(worker.time, "time", lambda: 1_000.0)

    assert not worker.component_batch_state_settled(store, active, now=100.0)
    assert not worker.component_batch_state_settled(
        store,
        active,
        now=100.0 + worker.BATCH_STATE_SETTLE_SECONDS - 0.1,
    )
    assert worker.component_batch_state_settled(
        store,
        active,
        now=100.0 + worker.BATCH_STATE_SETTLE_SECONDS,
    )
    session = store.get(job_id)["chat_sessions"][0]
    assert session["backend_settle_fingerprint"]
    assert session["backend_settle_started_at_epoch"] == 1_000.0


def test_new_backend_success_resets_settle_window_instead_of_retrying_early(
    worker,
    running_job,
    monkeypatch,
) -> None:
    store, job_id, _ = running_job
    store.record_chat_session(
        job_id, batch_index=0, batch_count=2,
        chat_url=codex_chat(0), role="coordinator", component_keys=["a", "b"],
    )
    active = worker.ActiveQuote(
        job_id, codex_chat(0), 100, batch_count=2, component_keys=("a", "b"),
    )
    batch = {
        "batch_index": 0,
        "batch_count": 2,
        "price_batch_id": "aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "component_keys": ["a", "b"],
        "component_states": {"a": "pending", "b": "pending"},
    }
    monkeypatch.setattr(worker, "active_batch", lambda *_: batch)

    assert not worker.component_batch_state_settled(store, active, now=100.0)
    batch["component_states"]["a"] = "completed"
    assert not worker.component_batch_state_settled(
        store,
        active,
        now=100.0 + worker.BATCH_STATE_SETTLE_SECONDS,
    )
    assert active.backend_settle_started_at == 100.0 + worker.BATCH_STATE_SETTLE_SECONDS


def test_backend_state_regression_cannot_reopen_a_saved_component(worker) -> None:
    batch = {
        "component_keys": ["a", "b"],
        "component_states": {"a": "completed", "b": "pending"},
    }
    progress = worker.batch_progress_fingerprint(batch)
    first = worker.component_batch_state_fingerprint(batch, progress)
    batch["component_states"]["a"] = "pending"

    assert worker.component_batch_state_fingerprint(batch, progress) == first


def test_worker_restart_restores_the_backend_settle_window(
    worker,
    running_job,
    monkeypatch,
) -> None:
    store, job_id, _ = running_job
    store.record_chat_session(
        job_id, batch_index=0, batch_count=2,
        chat_url=codex_chat(0), role="coordinator", component_keys=["a"],
    )
    store.update_chat_session(
        job_id,
        0,
        backend_settle_fingerprint='{"completed":[],"remaining":["a"]}',
        backend_settle_started_at_epoch=995.0,
    )
    record = store.get(job_id)
    browser = Mock()
    browser.resume_quote.side_effect = lambda current_job, url, **kwargs: worker.ActiveQuote(
        current_job, url, 100, **kwargs,
    )
    monkeypatch.setattr(worker.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(worker.time, "monotonic", lambda: 200.0)
    active_quotes: dict[str, worker.ActiveQuote] = {}

    worker.reattach_job_chats(store, browser, record, active_quotes)

    active = active_quotes[f"{job_id}:0"]
    assert active.backend_settle_fingerprint
    assert active.backend_settle_started_at == 195.0


def test_worker_restart_resumes_unsent_second_wave_in_the_same_conversation(
    worker,
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(tmp_path / "relay", max_concurrent_quotes=4)
    text = "\n".join(f"{index}. 组件 {index}：配置。" for index in range(1, 11))
    job = store.create(text, {}, numbered_components=parse_numbered_component_lines(text))
    record = store.claim_next("old-worker")
    assert record is not None
    store.record_chat_session(
        job["job_id"], batch_index=0, batch_count=2,
        chat_url=codex_chat(0), role="coordinator",
        component_keys=[f"cmp_intake_{index:04d}" for index in range(1, 6)],
        conversation_index=0, wave_index=0, wave_count=2,
    )
    store.update_chat_session(job["job_id"], 0, status="saved")
    store.purge_intake_batch(job["job_id"], 0)
    browser = Mock()
    browser.resume_quote.side_effect = lambda job_id, url, **kwargs: worker.ActiveQuote(
        job_id, url, 100, **kwargs,
    )
    active: dict[str, worker.ActiveQuote] = {}

    worker.create_missing_component_chats(store, browser, active, job["job_id"])

    assert list(active) == [f"{job['job_id']}:0"]
    assert active[f"{job['job_id']}:0"].batch_index == 1
    assert "cmp_intake_0006" in browser.continue_quote.call_args.args[1]
    latest = store.get(job["job_id"])
    assert latest["intake_batches"][1]["source_lines"] == []
    assert latest["chat_sessions"][1]["chat_url"] == codex_chat(0)


def test_same_sales_job_never_opens_more_than_three_active_conversations(
    worker,
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(tmp_path / "relay", max_concurrent_quotes=4)
    text = "\n".join(f"{index}. 组件 {index}：配置。" for index in range(1, 61))
    job = store.create(text, {}, numbered_components=parse_numbered_component_lines(text))
    record = store.claim_next("test-worker")
    browser = Mock()
    browser.logged_in.return_value = True
    browser.start_quote.return_value = worker.ActiveQuote(job["job_id"], codex_chat(0), 100)
    browser.start_component_batch.side_effect = lambda job_id, _prompt, **kwargs: (
        worker.ActiveQuote(job_id, codex_chat(kwargs["batch_index"]), 100, **kwargs)
    )
    first = worker.submit_job(store, browser, record)
    active = {first.session_key: first}

    worker.create_missing_component_chats(store, browser, active, job["job_id"])

    assert len([quote for quote in active.values() if quote.job_id == job["job_id"]]) == 3
    assert browser.start_component_batch.call_count == 2


def test_local_codex_conversation_ids_do_not_serialize_component_chat_creation(
    worker,
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(tmp_path / "relay", max_concurrent_quotes=4)
    text = "\n".join(f"{index}. 组件 {index}：配置。" for index in range(1, 45))
    job = store.create(
        text,
        {"cloud_provider": "alibaba", "preferred_region": "cn-hangzhou"},
        numbered_components=parse_numbered_component_lines(text),
    )
    record = store.claim_next("test-worker")
    assert record is not None
    browser = Mock()
    browser.logged_in.return_value = True
    browser.start_quote.return_value = worker.ActiveQuote(
        job["job_id"], local_codex_chat(0), 100,
    )
    browser.start_component_batch.side_effect = lambda job_id, _prompt, **kwargs: (
        worker.ActiveQuote(
            job_id,
            local_codex_chat(kwargs["batch_index"]),
            100,
            **kwargs,
        )
    )

    first = worker.submit_job(store, browser, record)
    assert first is not None
    active = {first.session_key: first}
    worker.create_missing_component_chats(store, browser, active, job["job_id"])

    assert set(active) == {f"{job['job_id']}:{index}" for index in range(3)}
    assert browser.start_component_batch.call_count == 2


def test_terminal_cleanup_keeps_slot_when_stop_is_uncertain(worker):
    failed = worker.ActiveQuote("job", "https://chatgpt.com/c/failed", 90, batch_index=0)
    stopped = worker.ActiveQuote("job", "https://chatgpt.com/c/stopped", 90, batch_index=1)
    active_quotes = {failed.session_key: failed, stopped.session_key: stopped}
    browser = Mock()

    def close_quote(quote):
        if quote is failed:
            raise RuntimeError("desktop control unavailable")

    browser.close_quote.side_effect = close_quote
    worker.stop_terminal_job_chats(browser, active_quotes, "job")

    assert failed.session_key in active_quotes
    assert stopped.session_key not in active_quotes


def test_promoted_pending_chat_reference_is_saved_for_restart(
    worker, running_job,
):
    store, job_id, _ = running_job
    pending = f"codex-chat://pending/{job_id}"
    stable = codex_chat(91)
    store.record_chat_session(
        job_id,
        batch_index=0,
        batch_count=1,
        chat_url=pending,
        role="coordinator",
        component_keys=[],
    )
    active = worker.ActiveQuote(job_id, stable, 100)

    worker.persist_promoted_chat_reference(store, active, pending)

    record = store.get(job_id)
    assert record["chat_url"] == stable
    assert record["chat_sessions"][0]["chat_url"] == stable


def test_real_component_progress_extends_deadline_but_query_churn_does_not(
    worker, running_job, monkeypatch,
):
    store, job_id, checkpoint_path = running_job
    checkpoint = {"relay_job_id": job_id, "stage": "pricing_partial",
                  "completed_component_count": 1, "batch_query_count": 10}
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100)
    active = worker.ActiveQuote(job_id, "https://chatgpt.com/c/example", 90)
    worker.refresh_progress_deadline(store, active)
    extended = active.deadline
    assert extended > 100
    monkeypatch.setattr(worker.time, "monotonic", lambda: 150)
    checkpoint.update(batch_query_count=30, failed_component_count=2)
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    worker.refresh_progress_deadline(store, active)
    assert active.deadline == extended
    checkpoint.update(completed_component_count=2)
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    worker.refresh_progress_deadline(store, active)
    assert active.deadline > extended
