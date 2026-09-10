from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.gpt_browser_navigation import (
    active_quote_poll_order,
    bounded_parallel_tabs,
    canonical_url_path,
    is_new_project_chat,
    is_persistent_permission_action,
    is_project_landing_url,
    is_scroll_to_latest_action,
    is_single_use_permission_action,
    is_tool_permission_prompt,
    is_transient_browser_poll_exception,
)
from app.services.gpt_quote_prompt import build_quote_prompt, parse_final_response
from app.services.gpt_quote_relay import GptQuoteRelayStore, GptRelayError


def test_relay_queues_and_hides_raw_customer_text(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create(
        "东京 EC2 两台，按需。",
        {"pricing_mode": "on_demand", "cloud_provider": "aws"},
    )

    assert public["status"] == "queued"
    assert public["submission_code"] in {str(value) for value in range(1, 10)}
    assert "customer_request" not in public
    assert public["cloud_provider"] == "aws"
    internal = store.get(public["job_id"])
    assert internal["customer_request"] == "东京 EC2 两台，按需。"
    assert "sales_name" not in internal


def test_worker_claims_one_job_and_purges_source_after_submission(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    first = store.create("东京 EC2 两台，按需。", {})
    second = store.create("新加坡 S3 1TB。", {})

    assert first["submission_code"] != second["submission_code"]

    claimed = store.claim_next("worker-1")
    assert claimed is not None
    assert claimed["job_id"] == first["job_id"]
    assert claimed["status"] == "processing"

    purged = store.purge_source(claimed["job_id"])
    assert purged["customer_request"] == ""
    assert purged["source_purged_at"]


def test_cancelled_job_is_purged_and_never_claimed_again(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    job = store.create("东京 EC2 两台，按需。", {})

    cancelled = store.cancel(job["job_id"])

    assert cancelled["status"] == "cancelled"
    assert store.get(job["job_id"])["customer_request"] == ""
    assert store.claim_next("worker-1") is None

    final = store.update_if_not_cancelled(
        job["job_id"],
        {"status": "completed", "result_summary": "must not be applied"},
    )
    assert final["status"] == "cancelled"
    assert final.get("result_summary") is None


def test_delivery_receipt_corrects_a_false_failed_browser_result(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 两台，按需。", {})
    store.claim_next("worker-1")
    store.purge_source(public["job_id"])
    store.update(public["job_id"], {"status": "failed", "error": {"code": "missing_marker"}})
    store.completions_directory.mkdir(parents=True, exist_ok=True)
    store._completion_path(public["job_id"]).write_text(
        json.dumps(
            {
                "schema_version": "astraquote-relay-completion/1",
                "job_id": public["job_id"],
                "submission_code": public["submission_code"],
                "quote_id": "aqv2_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "status": "delivered",
                "delivered_at": "2026-09-09T17:44:15.870Z",
                "webhook_event_id": "aqevt_receipt",
            }
        ),
        encoding="utf-8",
    )

    reconciled = store.public_get(public["job_id"])

    assert reconciled["status"] == "completed"
    internal = store.get(public["job_id"])
    assert internal["result_status"] == "delivered"
    assert internal["error"] is None


def test_page_result_receipt_completes_job_and_exposes_only_display_data(
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create(
        "东京 EC2 一台，按需。",
        {"display_result_on_page": True, "cloud_provider": "aws"},
    )
    store.claim_next("worker-1")
    store.purge_source(public["job_id"])
    store.completions_directory.mkdir(parents=True, exist_ok=True)
    store._completion_path(public["job_id"]).write_text(
        json.dumps(
            {
                "schema_version": "astraquote-relay-completion/1",
                "job_id": public["job_id"],
                "submission_code": public["submission_code"],
                "quote_id": "aqv2_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "status": "page_result_ready",
                "delivered_at": "2026-09-09T17:44:15.870Z",
                "page_result": {
                    "schema_version": "astraquote-page-result/1",
                    "currency": "USD",
                    "region": "ap-northeast-1",
                    "components": [
                        {
                            "service_name": "Amazon EC2",
                            "model_or_plan": "m7g.large",
                            "quantity": "1 台",
                            "configuration_summary": "2 核 8 GiB。",
                            "scenario_costs": [
                                {
                                    "scenario_key": "on_demand",
                                    "label": "按需付费",
                                    "monthly_cost": "100",
                                    "upfront_cost": "0",
                                }
                            ],
                        }
                    ],
                    "scenarios": [
                        {
                            "scenario_key": "on_demand",
                            "label": "按需付费",
                            "monthly_total": "100",
                            "upfront_total": "0",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    result = store.public_get(public["job_id"])

    assert result["status"] == "completed"
    assert result["display_result_on_page"] is True
    assert result["quick_quote_result"]["components"][0]["service_name"] == "Amazon EC2"
    assert "customer_request" not in result


def test_delivery_receipt_never_revives_a_cancelled_job(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 两台，按需。", {})
    store.claim_next("worker-1")
    store.purge_source(public["job_id"])
    store.cancel(public["job_id"])
    store.completions_directory.mkdir(parents=True, exist_ok=True)
    store._completion_path(public["job_id"]).write_text(
        json.dumps(
            {
                "schema_version": "astraquote-relay-completion/1",
                "job_id": public["job_id"],
                "submission_code": public["submission_code"],
                "quote_id": "aqv2_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "status": "delivered",
                "delivered_at": "2026-09-09T17:44:15.870Z",
            }
        ),
        encoding="utf-8",
    )

    assert store.public_get(public["job_id"])["status"] == "cancelled"


def test_mismatched_delivery_receipt_is_not_trusted(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 两台，按需。", {})
    store.claim_next("worker-1")
    store.purge_source(public["job_id"])
    store.update(public["job_id"], {"status": "failed"})
    store.completions_directory.mkdir(parents=True, exist_ok=True)
    store._completion_path(public["job_id"]).write_text(
        json.dumps(
            {
                "schema_version": "astraquote-relay-completion/1",
                "job_id": public["job_id"],
                "submission_code": "9" if public["submission_code"] != "9" else "8",
                "quote_id": "aqv2_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "status": "delivered",
                "delivered_at": "2026-09-09T17:44:15.870Z",
            }
        ),
        encoding="utf-8",
    )

    assert store.public_get(public["job_id"])["status"] == "failed"


def test_invalid_job_id_is_rejected(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    with pytest.raises(GptRelayError) as raised:
        store.get("../../etc/passwd")
    assert raised.value.code == "gpt_relay_job_id_invalid"


def test_health_reports_fresh_worker_heartbeat(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    store.heartbeat_path.write_text(
        json.dumps(
            {
                "updated_at": "2999-01-01T00:00:00+00:00",
                "browser": "Firefox",
                "logged_in": True,
                "project_name": "AstraQuote 报价",
            }
        ),
        encoding="utf-8",
    )
    health = store.health()
    assert health["status"] == "ready"
    assert health["message"] == "服务正常"
    assert "logged_in" not in health
    assert "browser" not in health
    assert "project_name" not in health


def test_login_waiting_job_resumes_without_losing_source(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 两台，按需。", {})
    store.update(public["job_id"], {"status": "needs_login"})

    assert store.resume_login_waiting() == 1
    resumed = store.get(public["job_id"])
    assert resumed["status"] == "queued"
    assert resumed["customer_request"] == "东京 EC2 两台，按需。"


def test_queued_job_waits_for_login_without_losing_source(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 两台，按需。", {})

    assert store.mark_queued_needs_login() == 1
    waiting = store.get(public["job_id"])
    assert waiting["status"] == "needs_login"
    assert waiting["customer_request"] == "东京 EC2 两台，按需。"


def test_per_quote_prompt_carries_the_current_nearest_lower_policy() -> None:
    prompt = build_quote_prompt(
        "东京 EC2 两台，Linux。",
        {
            "pricing_mode": "on_demand",
            "reserved_term_years": [],
            "payment_option": "not_applicable",
            "include_on_demand_scenario": True,
            "utilization_percent": 100,
            "cloud_provider": "aws",
        },
        relay_job_id="gpt-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        submission_code="7",
    )

    assert "请使用 AstraQuote 完成正式 AWS 报价并交付" in prompt
    assert "云厂商：AWS（销售已选定，不得改换）" in prompt
    assert "计价选项：按需付费；使用率 100%" in prompt
    assert "官方报价链接" not in prompt
    assert "customer_input_json" not in prompt
    assert "Fact Ledger" not in prompt
    assert "get_prices" not in prompt
    assert "build_estimate" not in prompt
    assert "ASTRAQUOTE_STATUS" not in prompt
    assert "禁止为了满足目标而向上选择" in prompt


def test_default_comparison_prompt_lists_all_three_selected_scenarios() -> None:
    prompt = build_quote_prompt(
        "东京 EC2 两台，Linux。",
        {
            "pricing_mode": "reserved",
            "reserved_term_years": [1, 3],
            "payment_option": "all_upfront",
            "include_on_demand_scenario": True,
            "utilization_percent": 100,
            "cloud_provider": "azure",
        },
        relay_job_id="gpt-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        submission_code="3",
    )

    assert "按需付费" in prompt
    assert "1 年全预付" in prompt
    assert "3 年全预付" in prompt
    assert "云厂商：微软 Azure（销售已选定，不得改换）" in prompt
    assert "结果交付方式：生成 Excel 并发送企业微信群" in prompt
    assert "同时提供按需方案作比较" not in prompt


def test_page_result_prompt_forbids_document_and_group_delivery() -> None:
    prompt = build_quote_prompt(
        "东京 EC2 一台。",
        {
            "pricing_mode": "on_demand",
            "include_on_demand_scenario": True,
            "utilization_percent": 100,
            "cloud_provider": "aws",
            "display_result_on_page": True,
        },
        relay_job_id="gpt-cccccccccccccccccccccccccccccccc",
        submission_code="5",
    )

    assert "官方报价链接" not in prompt
    assert "报价页直接展示；不生成文档；不发送企业微信群" in prompt


def test_completion_markers_are_still_parsed_outside_the_browser_driver() -> None:
    status, summary = parse_final_response(
        "报价已交付。\nASTRAQUOTE_STATUS: delivered\n"
        "ASTRAQUOTE_SUMMARY: 月费 12.34 USD，Excel 已发送，官方链接已生成。"
    )

    assert status == "delivered"
    assert summary == "月费 12.34 USD，Excel 已发送，官方链接已生成。"


def test_permission_detection_accepts_any_explicit_tool_card() -> None:
    assert is_tool_permission_prompt("允许 ChatGPT 使用 AstraQuote？")
    assert is_tool_permission_prompt(
        "AstraQuote V2 允许 ChatGPT 使用 AstraQuote V2? 始终允许 拒绝 允许一次"
    )
    assert is_tool_permission_prompt("允许 ChatGPT 使用 Gmail？")
    assert is_tool_permission_prompt("Allow ChatGPT to use a pricing tool?")
    assert not is_tool_permission_prompt("AstraQuote 已调用工具")


def test_permission_action_detection_covers_current_split_button_ui() -> None:
    assert is_persistent_permission_action("始终允许")
    assert is_persistent_permission_action("Always allow")
    assert is_single_use_permission_action("允许一次")
    assert is_single_use_permission_action("Allow once")
    assert not is_persistent_permission_action("允许一次")
    assert not is_single_use_permission_action("拒绝")


def test_latest_message_control_detection_covers_both_locales() -> None:
    assert is_scroll_to_latest_action("滚动到底部")
    assert is_scroll_to_latest_action("转到最新消息")
    assert is_scroll_to_latest_action("Scroll to bottom")
    assert not is_scroll_to_latest_action("返回顶部")


def test_project_navigation_requires_a_fresh_chat_in_the_same_project() -> None:
    project_url = "https://chatgpt.com/g/g-p-abc123/project"
    old_chat_url = "https://chatgpt.com/g/g-p-abc123/c/old-chat?messageId=final"
    new_chat_url = "https://chatgpt.com/g/g-p-abc123/c/new-chat"

    assert is_project_landing_url(project_url)
    assert is_new_project_chat(project_url, new_chat_url, old_chat_url)
    assert not is_new_project_chat(project_url, old_chat_url, old_chat_url)
    assert not is_new_project_chat(
        project_url,
        "https://chatgpt.com/g/g-p-other/c/new-chat",
        old_chat_url,
    )
    assert canonical_url_path(old_chat_url) == "/g/g-p-abc123/c/old-chat"


def test_parallel_browser_work_is_capped_at_three_tabs() -> None:
    assert bounded_parallel_tabs(None) == 3
    assert bounded_parallel_tabs("2") == 2
    assert bounded_parallel_tabs("0") == 1
    assert bounded_parallel_tabs("99") == 3
    assert bounded_parallel_tabs("invalid") == 3


def test_every_active_quote_tab_is_visited_in_each_polling_round() -> None:
    active_quotes = {
        "gpt-first": object(),
        "gpt-second": object(),
        "gpt-third": object(),
    }

    assert active_quote_poll_order(active_quotes) == (
        "gpt-first",
        "gpt-second",
        "gpt-third",
    )


def test_stale_dom_reference_is_retryable_without_failing_the_quote() -> None:
    stale_error = type("StaleElementReferenceException", (Exception,), {})()
    permanent_error = RuntimeError("the quote tab was closed")

    assert is_transient_browser_poll_exception(stale_error)
    assert not is_transient_browser_poll_exception(permanent_error)


def test_submitted_processing_job_can_be_reattached_after_worker_restart(
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 两台，按需。", {})
    claimed = store.claim_next("worker-old")
    assert claimed is not None
    store.update(
        public["job_id"],
        {"chat_url": "https://chatgpt.com/g/g-p-abc123/c/current-quote"},
    )
    store.purge_source(public["job_id"])

    resumed = store.claim_submitted_for_monitoring(
        "worker-new",
        limit=3,
        lease_minutes=35,
    )

    assert [record["job_id"] for record in resumed] == [public["job_id"]]
    assert resumed[0]["status"] == "processing"
    assert resumed[0]["customer_request"] == ""
    assert resumed[0]["worker_id"] == "worker-new"
