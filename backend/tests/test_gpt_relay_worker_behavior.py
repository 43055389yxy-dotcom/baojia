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
    assert worker.continue_component_batch(store, browser, active)
    batch["component_states"]["a"] = "pending"
    assert not worker.continue_component_batch(store, browser, active)
    assert browser.continue_quote.call_count == 2


def test_child_budget_is_durable_before_send_failure(worker, running_job, monkeypatch):
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
    assert store.get(job_id)["chat_sessions"][0]["stalled_attempts"] == 1


def test_final_merge_waits_until_running_chats_have_stopped(worker, running_job, monkeypatch):
    store, job_id, _ = running_job
    for index in range(2):
        store.record_chat_session(
            job_id, batch_index=index, batch_count=2,
            chat_url=codex_chat(index),
            role="coordinator" if index == 0 else "component_batch", component_keys=[str(index)],
        )
    batches = [{"batch_index": index, "price_batch_id": "aqpb_id", "component_keys": [str(index)],
                "component_states": {str(index): "completed"}} for index in range(2)]
    monkeypatch.setattr(store, "quote_chat_batches", lambda *_: batches)
    coordinator = worker.ActiveQuote(job_id, "https://chatgpt.com/c/batch-0", 100)
    active = {coordinator.session_key: coordinator}
    browser = Mock()
    worker.maybe_start_final_merge(store, browser, active, job_id)
    browser.continue_quote.assert_not_called()
    assert coordinator.role == "coordinator"


def test_merge_cannot_take_a_fifth_active_chat_slot(worker, running_job, monkeypatch):
    store, job_id, _ = running_job
    for index in range(2):
        store.record_chat_session(
            job_id, batch_index=index, batch_count=2,
            chat_url=codex_chat(index),
            role="coordinator" if index == 0 else "component_batch", component_keys=[str(index)],
        )
        store.update_chat_session(job_id, index, status="saved")
    batches = [{"batch_index": index, "price_batch_id": "aqpb_id", "component_keys": [str(index)],
                "component_states": {str(index): "completed"}} for index in range(2)]
    monkeypatch.setattr(store, "quote_chat_batches", lambda *_: batches)
    active = {f"other-{i}:0": worker.ActiveQuote(f"other-{i}", "https://chatgpt.com/c/other", 100)
              for i in range(4)}
    browser = Mock()
    worker.maybe_start_final_merge(store, browser, active, job_id)
    browser.resume_quote.assert_not_called()
    browser.continue_quote.assert_not_called()


def test_final_merge_is_authorized_only_when_every_batch_chat_has_stopped(
    worker, running_job, monkeypatch,
):
    store, job_id, _ = running_job
    for index in range(2):
        store.record_chat_session(
            job_id, batch_index=index, batch_count=2,
            chat_url=codex_chat(index),
            role="coordinator" if index == 0 else "component_batch",
            component_keys=[str(index)],
        )
        store.update_chat_session(job_id, index, status="saved")
    batches = [
        {
            "batch_index": index,
            "batch_count": 2,
            "price_batch_id": "aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "component_keys": [str(index)],
            "component_states": {str(index): "completed"},
        }
        for index in range(2)
    ]
    monkeypatch.setattr(store, "quote_chat_batches", lambda *_: batches)
    browser = Mock()
    browser.resume_quote.side_effect = lambda job, url, **kwargs: worker.ActiveQuote(
        job, url, 100, **kwargs,
    )

    worker.maybe_start_final_merge(store, browser, {}, job_id)

    assert store.get(job_id)["merge_authorized"] is True
    browser.continue_quote.assert_called_once()


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


def test_production_worker_constructs_codex_chat_adapter_not_firefox(worker):
    source = Path(worker.__file__).read_text(encoding="utf-8")
    main_source = source[source.index("def main()") :]

    assert "CodexChatDesktop(" in main_source
    assert "browser = ChatGptBrowser()" not in main_source


def test_numbered_intake_is_sent_as_three_isolated_chats_before_ai_cleanup(
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
    assert "cmp_intake_0021" not in first_prompt
    assert "cmp_intake_0021" in child_prompts[0]
    assert "cmp_intake_0041" not in child_prompts[0]
    assert "cmp_intake_0041" in child_prompts[1]
    internal = store.get(job["job_id"])
    assert internal["source_purged_at"]
    assert all(not batch["source_lines"] for batch in internal["intake_batches"])


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
