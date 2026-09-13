from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.services.gpt_browser_navigation import (
    active_quote_poll_order,
    bounded_continuation_attempts,
    canonical_url_path,
    is_interrupted_response,
    is_new_project_chat,
    is_persistent_permission_action,
    is_project_landing_url,
    is_scroll_to_latest_action,
    is_single_use_permission_action,
    is_tool_permission_prompt,
    is_transient_browser_poll_exception,
    should_extend_quote_deadline,
)
from app.services.gpt_quote_batches import (
    build_component_batch_continuation_prompt,
    build_component_batch_prompt,
    build_numbered_intake_batch_prompt,
    build_quote_merge_prompt,
    parse_numbered_component_lines,
    split_component_plan,
    split_numbered_intake,
)
from app.services.gpt_quote_prompt import (
    build_quote_context_prompt,
    build_quote_continuation_prompt,
    build_quote_failed_components_retry_prompt,
    build_quote_partial_finalization_prompt,
    build_quote_prompt,
    parse_final_response,
)
from app.services.gpt_quote_relay import GptQuoteRelayStore, GptRelayError


def test_numbered_sales_intake_splits_forty_four_components_into_five_item_waves() -> None:
    components = parse_numbered_component_lines(
        "\n".join(f"{index}. 组件 {index}：配置。" for index in range(1, 45))
    )

    batches = split_numbered_intake(components)

    assert [len(batch) for batch in batches] == [5, 5, 5, 5, 5, 5, 5, 5, 4]
    assert components[0]["component_key"] == "cmp_intake_0001"
    assert components[-1]["component_key"] == "cmp_intake_0044"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("1. 云服务器\n数据库", "第 2 行必须以连续序号 2. 开头"),
        ("1. 云服务器\n3. 数据库", "第 2 行序号应为 2，当前为 3"),
    ],
)
def test_numbered_sales_intake_rejects_missing_or_skipped_numbers(
    text: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_numbered_component_lines(text)


def test_numbered_intake_prompt_contains_only_its_owned_lines() -> None:
    components = parse_numbered_component_lines(
        "1. 云服务器：2 台。\n2. 数据库：1 套。\n3. 对象存储：1 TiB。"
    )

    prompt = build_numbered_intake_batch_prompt(
        relay_job_id="gpt-" + "a" * 32,
        submission_code="2",
        price_batch_id="aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        batch_index=1,
        batch_count=2,
        components=components[2:],
        quote_context="云厂商：AWS。",
    )

    assert prompt.startswith("@AstraQuote ")
    assert "cmp_intake_0003" in prompt
    assert "对象存储：1 TiB" in prompt
    assert "云服务器：2 台" not in prompt
    assert "relay_batch_index：1" in prompt
    assert "relay_batch_count：2" in prompt


def test_component_plan_splits_five_top_level_waves_and_keeps_children_together() -> None:
    plan = [
        {
            "component_key": f"cmp_root_{index:04d}",
            "customer_owned_source": f"组件 {index}。",
            "billing_scopes": [{"billing_key": "base"}],
        }
        for index in range(41)
    ]
    plan.insert(
        22,
        {
            "component_key": "cmp_child_0001",
            "parent_component_key": "cmp_root_0019",
            "customer_owned_source": "组件 19 的独立磁盘。",
            "billing_scopes": [{"billing_key": "storage"}],
        },
    )

    batches = split_component_plan(plan)

    assert len(batches) == 9
    assert [batch[0]["component_key"] for batch in batches] == [
        "cmp_root_0000",
        "cmp_root_0005",
        "cmp_root_0010",
        "cmp_root_0015",
        "cmp_root_0020",
        "cmp_root_0025",
        "cmp_root_0030",
        "cmp_root_0035",
        "cmp_root_0040",
    ]
    owning_batch_keys = {item["component_key"] for item in batches[3]}
    assert "cmp_root_0019" in owning_batch_keys
    assert "cmp_child_0001" in owning_batch_keys
    assert "cmp_child_0001" not in {
        item["component_key"]
        for index, batch in enumerate(batches)
        if index != 3
        for item in batch
    }

    forty = split_component_plan(
        [
            {
                "component_key": f"cmp_forty_{index:04d}",
                "customer_owned_source": f"组件 {index}。",
                "billing_scopes": [{"billing_key": "base"}],
            }
            for index in range(40)
        ]
    )
    sixty = split_component_plan(
        [
            {
                "component_key": f"cmp_sixty_{index:04d}",
                "customer_owned_source": f"组件 {index}。",
                "billing_scopes": [{"billing_key": "base"}],
            }
            for index in range(60)
        ]
    )
    assert [len(batch) for batch in forty] == [5] * 8
    assert [len(batch) for batch in sixty] == [5] * 12


def test_component_batch_prompt_contains_only_that_batches_cleaned_sources() -> None:
    current = [{
        "component_key": "cmp_compute_0001",
        "customer_owned_source": "云服务器：2 台，4 核 16 GiB。",
        "billing_scopes": [{"billing_key": "compute"}],
    }]

    prompt = build_component_batch_prompt(
        relay_job_id="gpt-" + "a" * 32,
        submission_code="2",
        price_batch_id="aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        batch_index=1,
        batch_count=2,
        components=current,
        quote_context=(
            "云厂商：AWS（销售已选定，不得改换）。\n"
            "账号站点：AWS 全球站（凭证范围 global）。\n"
            "销售首选地域：ap-southeast-1。\n"
            "计价选项：按需付费；使用率 100%。"
        ),
    )

    assert prompt.startswith("@AstraQuote ")
    assert "云服务器：2 台，4 核 16 GiB。" in prompt
    assert "对象存储" not in prompt
    assert "客户原话" not in prompt
    assert "第 2/2 批" in prompt
    assert "relay_batch_index：1" in prompt
    assert "relay_batch_count：2" in prompt
    assert "不要生成最终整单" in prompt
    assert "AWS 全球站" in prompt
    assert "ap-southeast-1" in prompt


def test_host_relay_uses_python39_compatible_datetime_api() -> None:
    source_path = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "services"
        / "gpt_quote_relay.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    datetime_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "datetime"
        for alias in node.names
    }

    assert "UTC" not in datetime_imports


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
    assert internal["continuation_attempts"] == 0
    assert "sales_name" not in internal


def test_relay_persists_only_pre_split_numbered_batches_and_reserves_three_slots(
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(tmp_path, max_concurrent_quotes=4)
    text = "\n".join(f"{index}. 组件 {index}：配置。" for index in range(1, 45))
    components = parse_numbered_component_lines(text)

    public = store.create(text, {"cloud_provider": "aws"}, numbered_components=components)

    internal = store.get(public["job_id"])
    assert internal["customer_request"] == ""
    assert internal["intake_component_count"] == 44
    assert internal["intake_batch_count"] == 9
    assert internal["intake_chat_count"] == 5
    assert internal["reserved_price_batch_id"].startswith("aqpb_")
    assert [len(batch["source_lines"]) for batch in internal["intake_batches"]] == [
        5, 5, 5, 5, 5, 5, 5, 5, 4,
    ]
    assert store._slot_count(internal) == 3


def test_purging_one_numbered_batch_does_not_delete_unsent_batches(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    text = "\n".join(f"{index}. 组件 {index}。" for index in range(1, 22))
    job = store.create(
        text,
        {},
        numbered_components=parse_numbered_component_lines(text),
    )

    purged = store.purge_intake_batch(job["job_id"], 0)

    assert purged["intake_batches"][0]["source_lines"] == []
    assert purged["intake_batches"][0]["status"] == "submitted"
    assert len(purged["intake_batches"][1]["source_lines"]) == 5
    assert purged["source_purged_at"] is None

    fully_purged = purged
    for batch_index in range(1, 5):
        fully_purged = store.purge_intake_batch(job["job_id"], batch_index)
    assert fully_purged["source_purged_at"]


def test_repeated_client_request_id_returns_the_original_job(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    client_request_id = "123e4567-e89b-42d3-a456-426614174000"
    first = store.create(
        "东京 EC2 两台，按需。",
        {"client_request_id": client_request_id, "cloud_provider": "aws"},
    )
    second = store.create(
        "这一段不会覆盖第一次提交。",
        {"client_request_id": client_request_id, "cloud_provider": "azure"},
    )

    assert second["job_id"] == first["job_id"]
    assert store.get(first["job_id"])["customer_request"] == "东京 EC2 两台，按需。"


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


def test_relay_runs_at_most_four_quotes_and_reports_the_waiting_queue(tmp_path: Path) -> None:
    checkpoints = tmp_path / "v2-quotes"
    store = GptQuoteRelayStore(
        tmp_path / "relay",
        checkpoint_directory=checkpoints,
        max_concurrent_quotes=4,
        default_quote_seconds=600,
    )
    jobs = [store.create(f"报价需求 {index}，两台云服务器。", {}) for index in range(6)]

    checkpoints.mkdir(parents=True)
    claimed = []
    for _ in range(4):
        record = store.claim_next("worker-1")
        claimed.append(record)
        assert record is not None
        (checkpoints / f"relay-{record['job_id']}.json").write_text(
            json.dumps(
                {
                    "relay_job_id": record["job_id"],
                    "stage": "requirements_cleaned",
                    "top_level_component_count": 1,
                    "total_component_count": 1,
                }
            ),
            encoding="utf-8",
        )

    assert [record["job_id"] for record in claimed if record] == [
        job["job_id"] for job in jobs[:4]
    ]
    assert store.claim_next("worker-1") is None

    fifth = store.public_get(jobs[4]["job_id"])
    sixth = store.public_get(jobs[5]["job_id"])
    assert fifth["status"] == "queued"
    assert fifth["max_concurrent_quotes"] == 4
    assert fifth["active_quote_count"] == 4
    assert fifth["queue_position"] == 1
    assert fifth["queued_ahead_count"] == 0
    assert fifth["jobs_ahead_count"] == 4
    assert fifth["estimated_wait_minutes"] == 10
    assert sixth["queue_position"] == 2
    assert sixth["queued_ahead_count"] == 1
    assert sixth["jobs_ahead_count"] == 5
    assert sixth["estimated_wait_minutes"] == 10

    store.update(jobs[0]["job_id"], {"status": "completed"})
    next_job = store.claim_next("worker-1")
    assert next_job is not None
    assert next_job["job_id"] == jobs[4]["job_id"]


def _numbered_components(count: int) -> list[dict[str, str]]:
    return [
        {
            "component_key": f"cmp_intake_{index:04d}",
            "source_line": f"{index}. 云资源组件 {index}",
        }
        for index in range(1, count + 1)
    ]


def test_quote_engine_defaults_to_chatgpt_and_is_public(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)

    public = store.create("1. 东京 EC2 两台。", {})

    assert public["preferred_engine"] == "chatgpt"
    assert public["assigned_engine"] is None
    assert store.get(public["job_id"])["preferred_engine"] == "chatgpt"


def test_disabled_gemini_preference_is_routed_to_chatgpt(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)

    public = store.create("1. 东京 EC2 两台。", {"preferred_engine": "gemini"})

    assert public["preferred_engine"] == "chatgpt"
    assert store.get(public["job_id"])["quote_options"]["preferred_engine"] == "chatgpt"
    assert store.claim_next("gemini-worker", engine="gemini") is None
    assert store.claim_next("chatgpt-worker", engine="chatgpt")["job_id"] == public["job_id"]


def test_each_engine_has_four_independent_slots_and_overflow_falls_back_atomically(
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(
        tmp_path, max_concurrent_quotes=4, enabled_engines=("chatgpt", "gemini")
    )
    chatgpt_job = store.create(
        "1. ChatGPT 大型报价。",
        {"preferred_engine": "chatgpt"},
        numbered_components=_numbered_components(80),
    )
    overflow = store.create(
        "1. 新报价。",
        {"preferred_engine": "chatgpt"},
        numbered_components=_numbered_components(40),
    )

    claimed_chatgpt = store.claim_next("chatgpt-worker", engine="chatgpt")
    assert claimed_chatgpt is not None
    assert claimed_chatgpt["job_id"] == chatgpt_job["job_id"]
    assert claimed_chatgpt["assigned_engine"] == "chatgpt"
    assert store.claim_next("chatgpt-worker", engine="chatgpt") is None

    claimed_gemini = store.claim_next("gemini-worker", engine="gemini")
    assert claimed_gemini is not None
    assert claimed_gemini["job_id"] == overflow["job_id"]
    assert claimed_gemini["assigned_engine"] == "gemini"
    assert store._slot_count(claimed_gemini) == 3


def test_gemini_preference_falls_back_to_chatgpt_when_all_gemini_slots_are_used(
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(
        tmp_path, max_concurrent_quotes=4, enabled_engines=("chatgpt", "gemini")
    )
    gemini_job = store.create(
        "1. Gemini 大型报价。",
        {"preferred_engine": "gemini"},
        numbered_components=_numbered_components(80),
    )
    overflow = store.create(
        "1. Gemini 新报价。",
        {"preferred_engine": "gemini"},
        numbered_components=_numbered_components(20),
    )

    claimed_gemini = store.claim_next("gemini-worker", engine="gemini")
    assert claimed_gemini is not None
    assert claimed_gemini["job_id"] == gemini_job["job_id"]
    claimed = store.claim_next("chatgpt-worker", engine="chatgpt")

    assert claimed is not None
    assert claimed["job_id"] == overflow["job_id"]
    assert claimed["assigned_engine"] == "chatgpt"


def test_job_waits_when_neither_engine_has_enough_slots(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(
        tmp_path, max_concurrent_quotes=4, enabled_engines=("chatgpt", "gemini")
    )
    for engine in ("chatgpt", "gemini"):
        store.create(
            f"1. {engine} 满载任务。",
            {"preferred_engine": engine},
            numbered_components=_numbered_components(80),
        )
        assert store.claim_next(f"{engine}-worker", engine=engine) is not None
    waiting = store.create(
        "1. 等待中的报价。",
        {"preferred_engine": "chatgpt"},
        numbered_components=_numbered_components(20),
    )

    assert store.claim_next("chatgpt-worker", engine="chatgpt") is None
    assert store.claim_next("gemini-worker", engine="gemini") is None
    assert store.public_get(waiting["job_id"])["status"] == "queued"


def test_submitted_quote_is_reattached_only_by_its_assigned_engine(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path, enabled_engines=("chatgpt", "gemini"))
    public = store.create(
        "1. Gemini 报价。",
        {"preferred_engine": "gemini"},
        numbered_components=_numbered_components(1),
    )
    claimed = store.claim_next("gemini-old", engine="gemini")
    assert claimed is not None
    store.update(
        public["job_id"],
        {"chat_url": "gemini-chat://tasks/046174d2550d9f1c"},
    )

    assert store.claim_submitted_for_monitoring(
        "chatgpt-worker", engine="chatgpt", limit=None,
    ) == []
    resumed = store.claim_submitted_for_monitoring(
        "gemini-worker", engine="gemini", limit=None,
    )
    assert [item["job_id"] for item in resumed] == [public["job_id"]]


def test_relay_admits_only_one_unplanned_intake_until_its_component_count_is_sealed(
    tmp_path: Path,
) -> None:
    checkpoints = tmp_path / "v2-quotes"
    store = GptQuoteRelayStore(
        tmp_path / "relay",
        checkpoint_directory=checkpoints,
        max_concurrent_quotes=4,
    )
    first = store.create("第一份未知规模报价。", {})
    second = store.create("第二份未知规模报价。", {})

    assert store.claim_next("worker-a")["job_id"] == first["job_id"]
    assert store.claim_next("worker-a") is None

    checkpoints.mkdir(parents=True)
    (checkpoints / f"relay-{first['job_id']}.json").write_text(
        json.dumps(
            {
                "relay_job_id": first["job_id"],
                "stage": "requirements_cleaned",
                "top_level_component_count": 40,
                "total_component_count": 40,
            }
        ),
        encoding="utf-8",
    )

    assert store.claim_next("worker-a")["job_id"] == second["job_id"]


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
                "spreadsheet_url": "https://baojia.tontiancloud.com/api/backend/api/quote-artifacts/aqdl_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "spreadsheet_filename": "quote.xlsx",
                "page_result": {
                    "schema_version": "astraquote-page-result/1",
                    "currency": "USD",
                    "region": "ap-northeast-1",
                    "components": [{
                        "service_name": "Amazon EC2",
                        "scenario_costs": [{
                            "scenario_key": "on_demand", "label": "按需付费",
                            "monthly_cost": "100", "upfront_cost": "0",
                        }],
                    }],
                    "scenarios": [{
                        "scenario_key": "on_demand", "label": "按需付费",
                        "monthly_total": "100", "upfront_total": "0",
                    }],
                },
            }
        ),
        encoding="utf-8",
    )

    reconciled = store.public_get(public["job_id"])

    assert reconciled["status"] == "completed"
    internal = store.get(public["job_id"])
    assert internal["result_status"] == "delivered"
    assert internal["error"] is None
    assert reconciled["quote_download_url"].endswith("a" * 48)


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
                "spreadsheet_url": "https://baojia.tontiancloud.com/api/backend/api/quote-artifacts/aqdl_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "spreadsheet_filename": "quote.xlsx",
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
    assert result["quote_download_url"].endswith("b" * 48)
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


def test_partial_receipt_returns_successful_rows_and_unpriced_components(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 和对象存储，按需。", {})
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
                "status": "partial_page_result_ready",
                "delivered_at": "2026-09-12T01:02:03.000Z",
                "spreadsheet_url": "https://baojia.tontiancloud.com/api/backend/api/quote-artifacts/aqdl_"
                + "c" * 48,
                "page_result": {
                    "schema_version": "astraquote-page-result/1",
                    "is_partial": True,
                    "currency": "USD",
                    "region": "ap-northeast-1",
                    "components": [
                        {
                            "service_name": "Amazon EC2",
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
                    "unpriced_components": [
                        {
                            "service_name": "Amazon S3",
                            "quantity": "2 TiB",
                            "configuration_summary": "S3 Standard 2 TiB，价格尚未取得。",
                            "failure_code": "official_price_unavailable",
                            "failure_category": "rate_limit",
                            "provider_code": "TooManyRequestsException",
                            "retryable": True,
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

    sales = store.public_get(public["job_id"])

    assert sales["status"] == "partial"
    assert sales["quick_quote_result"]["is_partial"] is True
    assert sales["quick_quote_result"]["unpriced_components"][0]["service_name"] == "Amazon S3"
    assert sales["quick_quote_result"]["unpriced_components"][0]["failure_category"] == "rate_limit"
    assert (
        sales["quick_quote_result"]["unpriced_components"][0]["provider_code"]
        == "TooManyRequestsException"
    )
    assert sales["quick_quote_result"]["scenarios"][0]["monthly_total"] == "100"

    retried = store.retry_partial(public["job_id"])
    assert retried["status"] == "queued"
    internal = store.get(public["job_id"])
    assert internal["customer_request"] == ""
    assert internal["partial_retry_generation"] == 1
    # The old partial receipt must not immediately complete the retried job.
    assert store.public_get(public["job_id"])["status"] == "queued"


def test_sales_page_notice_is_fixed_and_never_forwards_internal_error_text() -> None:
    public_result = GptQuoteRelayStore._page_result_public(
        {
            "schema_version": "astraquote-page-result/1",
            "is_partial": False,
            "currency": "USD",
            "region": "ap-southeast-1",
            "pricing_notice": "internal-query-id=secret; TooManyRequests raw trace",
            "components": [
                {
                    "service_name": "Amazon EC2",
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
            "unpriced_components": [],
            "scenarios": [
                {
                    "scenario_key": "on_demand",
                    "label": "按需付费",
                    "monthly_total": "100",
                    "upfront_total": "0",
                }
            ],
        }
    )

    assert public_result is not None
    assert public_result["pricing_notice"].startswith("销售提示：")
    assert "internal-query-id" not in public_result["pricing_notice"]
    assert "TooManyRequests" not in public_result["pricing_notice"]


def test_retry_partial_rejects_non_partial_jobs(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 一台。", {})

    with pytest.raises(GptRelayError) as raised:
        store.retry_partial(public["job_id"])

    assert raised.value.code == "gpt_relay_partial_retry_unavailable"


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


def test_per_quote_prompt_contains_only_per_order_context() -> None:
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

    assert prompt.startswith("@AstraQuote 请使用 AstraQuote 完成正式 AWS 报价并交付")
    assert "云厂商：AWS（销售已选定，不得改换）" in prompt
    assert "计价选项：按需付费；使用率 100%" in prompt
    assert "官方报价链接" not in prompt
    assert "customer_input_json" not in prompt
    assert "Fact Ledger" not in prompt
    assert "get_prices" not in prompt
    assert "build_estimate" not in prompt
    assert "ASTRAQUOTE_STATUS" not in prompt
    assert "当前官方地域清单" not in prompt
    assert "quote_components" not in prompt
    assert "每 20 个组件" not in prompt
    assert "禁止为了满足目标而向上选择" not in prompt

    plugin_instructions = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "astraquote-mcp"
        / "instructions.zh-CN.md"
    ).read_text(encoding="utf-8")
    assert "每 5 个组件一轮、每个对话两轮" in plugin_instructions
    assert "销售选择的是首选地域" in plugin_instructions
    assert "没有完全匹配时选最接近的小一档" in plugin_instructions


def test_unknown_sales_region_is_recoverable_and_ai_must_choose_nearest_official_region() -> None:
    prompt = build_quote_prompt(
        "ECS 两台，Linux。",
        {
            "pricing_scenarios": ["on_demand"],
            "utilization_percent": 100,
            "cloud_provider": "alibaba",
            "preferred_region": "not-a-real-region",
        },
        relay_job_id="gpt-cccccccccccccccccccccccccccccccc",
        submission_code="4",
    )

    assert "销售提供的地域偏好：not-a-real-region" in prompt
    assert "不得因此停止报价" in prompt
    assert "同一云厂商、同一账号站点内选择距离最近" in prompt
    assert "当前官方地域清单" not in prompt
    assert "杭州（cn-hangzhou）" not in prompt


def test_selected_region_does_not_expand_to_every_provider_region() -> None:
    prompt = build_quote_prompt(
        "Redis 3 个节点。",
        {
            "pricing_scenarios": ["on_demand"],
            "utilization_percent": 100,
            "cloud_provider": "tencent",
            "preferred_region": "ap-chengdu",
        },
        relay_job_id="gpt-dddddddddddddddddddddddddddddddd",
        submission_code="6",
    )

    assert "销售首选地域：ap-chengdu" in prompt
    assert "ap-shanghai" not in prompt
    assert "ap-singapore" not in prompt
    assert "eu-frankfurt" not in prompt


def test_default_comparison_prompt_lists_all_three_selected_scenarios() -> None:
    prompt = build_quote_prompt(
        "东京 EC2 两台，Linux。",
        {
            "pricing_scenarios": [
                "on_demand",
                "one_year_commitment",
                "three_year_commitment",
            ],
            "utilization_percent": 100,
            "cloud_provider": "azure",
        },
        relay_job_id="gpt-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        submission_code="3",
    )

    assert "即用即付" in prompt
    assert "1 年预留" in prompt
    assert "3 年预留" in prompt
    assert "云厂商：微软 Azure（销售已选定，不得改换）" in prompt
    assert "交付：销售报价页与 Excel 下载链接" in prompt


@pytest.mark.parametrize(
    ("provider", "scenarios", "expected", "forbidden"),
    [
        ("aws", ["on_demand"], "按需付费", "即用即付"),
        ("azure", ["on_demand", "one_year_commitment"], "1 年预留", "预留实例全预付"),
        ("oci", ["on_demand"], "OCI 公开按量价", "承诺使用"),
        ("gcp", ["on_demand", "three_year_commitment"], "3 年承诺使用", "预留实例"),
    ],
)
def test_quote_prompt_keeps_each_provider_purchase_vocabulary(
    provider: str,
    scenarios: list[str],
    expected: str,
    forbidden: str,
) -> None:
    prompt = build_quote_prompt(
        "2 核 4 GiB，一台。",
        {
            "pricing_scenarios": scenarios,
            "utilization_percent": 100,
            "cloud_provider": provider,
        },
        relay_job_id="gpt-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        submission_code="4",
    )

    assert expected in prompt
    assert forbidden not in prompt


def test_quote_prompt_uses_provider_profile_subscription_terms() -> None:
    prompt = build_quote_prompt(
        "CVM 2 核 4 GiB，一台。",
        {
            "pricing_scenarios": [
                "on_demand", "one_month_subscription", "one_year_subscription",
            ],
            "utilization_percent": 100,
            "cloud_provider": "tencent",
            "preferred_region": "ap-singapore",
        },
        relay_job_id="gpt-ffffffffffffffffffffffffffffffff",
        submission_code="4",
    )

    assert "按量计费；包月；包年（1 年）" in prompt
    assert "3 年包年" not in prompt
    assert "销售已选中的全部计价方式，任何一种都不得遗漏" in prompt
    assert "查价、汇总、报价页和 Excel" in prompt


def test_every_quote_prompt_requires_excel_and_sales_page_delivery_only() -> None:
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
    assert "交付：销售报价页与 Excel 下载链接" in prompt
    assert "不发送企业微信群" not in prompt


def test_completion_markers_are_still_parsed_outside_the_browser_driver() -> None:
    status, summary = parse_final_response(
        "报价已交付。\nASTRAQUOTE_STATUS: displayed_on_page\n"
        "ASTRAQUOTE_SUMMARY: 月费 12.34 USD，Excel 和下载链接已在报价页生成。"
    )

    assert status == "displayed_on_page"
    assert summary == "月费 12.34 USD，Excel 和下载链接已在报价页生成。"


def test_explicit_quote_stop_code_marks_an_unrecoverable_quote_blocked() -> None:
    status, summary = parse_final_response(
        "已确认当前连接没有该云厂商的正式报价能力。\n"
        "ASTRAQUOTE_STOP_CODE: AQ-QUOTE-BLOCKED\n"
        "ASTRAQUOTE_SUMMARY: 当前云厂商的官方报价连接不可用。"
    )

    assert status == "blocked"
    assert summary == "当前云厂商的官方报价连接不可用。"


def test_legacy_blocked_status_remains_compatible() -> None:
    status, summary = parse_final_response(
        "ASTRAQUOTE_STATUS: blocked\n"
        "ASTRAQUOTE_SUMMARY: 当前任务缺少不可恢复的官方授权。"
    )

    assert status == "blocked"
    assert summary == "当前任务缺少不可恢复的官方授权。"


def test_terminal_code_mentioned_before_the_final_lines_is_not_accepted() -> None:
    status, _summary = parse_final_response(
        "不要在处理中输出 ASTRAQUOTE_STOP_CODE: AQ-QUOTE-BLOCKED。\n"
        "当前价格查询仍在继续。"
    )

    assert status == "incomplete"


def test_failure_prose_without_stop_code_is_not_a_terminal_signal() -> None:
    status, summary = parse_final_response(
        "这次暂时无法正式报价，当前连接尚未提供该云厂商的价格查询能力。"
    )

    assert status == "incomplete"
    assert "暂时无法正式报价" in summary


def test_stage_summary_without_final_marker_requires_continuation() -> None:
    status, summary = parse_final_response(
        "当前已确认 EC2 和 RDS 官方价格。Redis 与 S3 仍需继续收窄，"
        "下一步完成剩余查询后再生成 Excel。"
    )

    assert status == "incomplete"
    assert "Redis 与 S3" in summary


def test_continuation_prompt_reuses_identity_without_restoring_customer_text() -> None:
    prompt = build_quote_continuation_prompt(
        relay_job_id="gpt-dddddddddddddddddddddddddddddddd",
        submission_code="6",
    )

    assert prompt.startswith("@AstraQuote ")
    assert "gpt-dddddddddddddddddddddddddddddddd" in prompt
    assert "提交码 6" in prompt
    assert "从已保存阶段继续" in prompt
    assert "立即继续实际执行" in prompt
    assert "只输出状态、计划或待办清单" in prompt
    assert "客户需求" not in prompt
    assert "get_prices" not in prompt
    assert "build_estimate" not in prompt


def test_partial_finalization_prompt_returns_saved_successes_without_customer_text() -> None:
    prompt = build_quote_partial_finalization_prompt(
        relay_job_id="gpt-dddddddddddddddddddddddddddddddd",
        submission_code="6",
    )

    assert prompt.startswith("@AstraQuote ")
    assert "补发一次" in prompt
    assert "部分报价" in prompt
    assert "未取得价格的组件不得按 0 元" in prompt
    assert "gpt-dddddddddddddddddddddddddddddddd" in prompt
    assert "客户需求" not in prompt


def test_every_automated_followup_explicitly_mentions_astraquote() -> None:
    identity = {
        "relay_job_id": "gpt-dddddddddddddddddddddddddddddddd",
        "submission_code": "6",
    }
    prompts = [
        build_quote_failed_components_retry_prompt(**identity),
        build_quote_merge_prompt(
            **identity,
            price_batch_id="aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        ),
        build_component_batch_continuation_prompt(
            **identity,
            price_batch_id="aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            batch_index=1,
            batch_count=2,
            component_keys=["cmp_compute_0001"],
        ),
    ]

    assert all(prompt.startswith("@AstraQuote ") for prompt in prompts)
    assert "relay_batch_index：1" in prompts[-1]
    assert "relay_batch_count：2" in prompts[-1]


def test_codex_worker_keeps_continuation_and_receipt_integration() -> None:
    root = Path(__file__).resolve().parents[2]
    worker = (
        root / "tools/gpt_quote_relay_worker.py"
    ).read_text(encoding="utf-8")
    desktop = (root / "tools/codex_chat_desktop.py").read_text(encoding="utf-8")

    assert "store.reconcile_delivery_receipt(job_id)" in worker
    assert 'if outcome == "continue":' in worker
    assert 'if outcome == "partial_finalize":' in worker
    assert "store.request_partial_finalization(job_id)" in worker
    assert "build_quote_partial_finalization_prompt(" in worker
    assert "browser.continue_quote(active, continuation_prompt)" in worker
    assert "is_persistent_permission_action(" not in worker
    assert "'允许一次', 'Allow once'" in desktop
    assert "'始终允许'" not in desktop
    assert "belongsToAstraQuote" in desktop
    assert "stop_terminal_job_chats(browser, active_quotes, job_id)" in worker
    assert "quote.deadline = quote.stable_since + self.quote_timeout_seconds" in desktop
    assert "except TimeoutError:" in worker
    assert "报价等待超时，已在原对话从保存阶段自动继续" in worker
    assert "delivered_without_calculator_link" not in worker


def test_permission_detection_accepts_only_astraquote_tool_cards() -> None:
    assert is_tool_permission_prompt("允许 ChatGPT 使用 AstraQuote？")
    assert is_tool_permission_prompt(
        "AstraQuote V2 允许 ChatGPT 使用 AstraQuote V2? 始终允许 拒绝 允许一次"
    )
    assert not is_tool_permission_prompt("允许 ChatGPT 使用 Gmail？")
    assert not is_tool_permission_prompt("Allow ChatGPT to use a pricing tool?")
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


def test_browser_worker_uses_one_desktop_window_instead_of_browser_tabs() -> None:
    worker = (
        Path(__file__).resolve().parents[2] / "tools/gpt_quote_relay_worker.py"
    ).read_text(encoding="utf-8")

    assert 'new_window("tab")' not in worker
    assert "window_handle" not in worker


def test_automatic_continuation_attempts_are_bounded_and_config_safe() -> None:
    assert bounded_continuation_attempts(None) == 1
    assert bounded_continuation_attempts("2") == 1
    assert bounded_continuation_attempts("0") == 1
    assert bounded_continuation_attempts("99") == 1
    assert bounded_continuation_attempts("invalid") == 1


def test_quote_context_requires_ai_to_fill_only_minimum_required_official_values() -> None:
    prompt = build_quote_context_prompt({
        "cloud_provider": "alibaba",
        "preferred_region": "cn-hangzhou",
        "pricing_scenarios": ["on_demand"],
    })

    assert "可省略" in prompt
    assert "最小刚需" in prompt
    assert "不得因为缺少参数停止" in prompt


def test_active_quote_polling_is_fair_without_switching_every_chat_per_tick() -> None:
    active_quotes = {
        "gpt-first": type("Quote", (), {"last_polled_at": 30.0})(),
        "gpt-second": type("Quote", (), {"last_polled_at": 10.0})(),
        "gpt-third": type("Quote", (), {"last_polled_at": 20.0})(),
    }

    assert active_quote_poll_order(active_quotes) == (
        "gpt-second",
        "gpt-third",
        "gpt-first",
    )
    assert active_quote_poll_order(active_quotes, limit=1) == ("gpt-second",)


def test_stale_dom_reference_is_retryable_without_failing_the_quote() -> None:
    stale_error = type("StaleElementReferenceException", (Exception,), {})()
    transport_timeout = type("ReadTimeoutError", (Exception,), {})(
        "HTTPConnectionPool(host='localhost', port=46059): Read timed out."
    )
    permanent_error = RuntimeError("the quote tab was closed")

    assert is_transient_browser_poll_exception(stale_error)
    assert is_transient_browser_poll_exception(transport_timeout)
    assert not is_transient_browser_poll_exception(permanent_error)


@pytest.mark.parametrize(
    "text",
    [
        "连接已中断。正在等待完整回复",
        "Connection interrupted. Waiting for the full response.",
    ],
)
def test_interrupted_chat_response_is_resumed_in_the_same_quote(text: str) -> None:
    assert is_interrupted_response(text)


def test_normal_stage_progress_is_not_mislabeled_as_an_interruption() -> None:
    assert not is_interrupted_response("正在调用官方价格接口，报价仍在继续。")


def test_pending_codex_sidebar_identity_is_a_transient_renderer_state() -> None:
    pending = type("PendingConversationReferenceError", (RuntimeError,), {})
    assert is_transient_browser_poll_exception(pending("sidebar title pending"))


def test_failed_job_exposes_only_the_generic_sales_failure_code(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 两台，按需。", {"preferred_region": "ap-northeast-1"})
    store.update(
        public["job_id"],
        {
            "status": "failed",
            "error": {"code": "internal_sensitive_detail", "message": "technical detail"},
        },
    )

    sales = store.public_get(public["job_id"])

    assert sales["failure_code"] == "AQ-QUOTE-FAILED"
    assert sales["preferred_region"] == "ap-northeast-1"
    assert "error" not in sales


def test_pending_codex_chat_reference_can_be_atomically_promoted(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 两台，按需。", {})
    job_id = public["job_id"]
    store.claim_next("worker-a")
    pending = f"codex-chat://pending/{job_id}"
    stable = "codex-chat://conversations/6aa52a28-5510-83ee-b69a-42c10c9f1ddb"

    store.record_chat_session(
        job_id,
        batch_index=0,
        batch_count=1,
        chat_url=pending,
        role="coordinator",
        component_keys=[],
        previous_conversation_ids=[
            "6aa5306f-4ed8-83e9-9c0d-466e73829f51"
        ],
    )
    store.promote_chat_session_reference(job_id, 0, pending, stable)

    record = store.get(job_id)
    assert record["chat_url"] == stable
    assert record["chat_sessions"][0]["chat_url"] == stable
    assert record["chat_sessions"][0]["previous_conversation_ids"] == []


def test_local_codex_chat_reference_can_be_saved_and_remapped(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 两台，按需。", {})
    job_id = public["job_id"]
    store.claim_next("worker-a")
    local_id = "local-chatgpt:2995f22c-1bfa-414c-8f65-cebe43aa23cf"
    local = f"codex-chat://conversations/{local_id}"
    stable = "codex-chat://conversations/6aa52a28-5510-83ee-b69a-42c10c9f1ddb"

    store.record_chat_session(
        job_id,
        batch_index=0,
        batch_count=1,
        chat_url=local,
        role="coordinator",
        component_keys=[],
        previous_conversation_ids=[local_id],
    )
    store.promote_chat_session_reference(job_id, 0, local, stable)

    record = store.get(job_id)
    assert record["chat_url"] == stable
    assert record["chat_sessions"][0]["chat_url"] == stable


def test_pending_codex_reference_must_match_its_relay_job(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 两台，按需。", {})
    store.claim_next("worker-a")

    with pytest.raises(Exception, match="无效的报价对话地址"):
        store.record_chat_session(
            public["job_id"],
            batch_index=0,
            batch_count=1,
            chat_url="codex-chat://pending/gpt-ffffffffffffffffffffffffffffffff",
            role="coordinator",
            component_keys=[],
        )


def test_stale_worker_does_not_terminally_fail_submitted_quote(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 两台，按需。", {})
    store.claim_next("worker-a")
    record = store.get(public["job_id"])
    record["updated_at"] = "2000-01-01T00:00:00+00:00"
    store._write_atomic(store._path(public["job_id"]), record)
    store._write_atomic(
        store.heartbeat_path,
        {"updated_at": "2000-01-01T00:00:00+00:00", "logged_in": False},
    )

    sales = store.public_get(public["job_id"])

    assert sales["status"] == "processing"
    assert sales["failure_code"] is None
    assert store.get(public["job_id"])["status"] == "processing"


def test_active_worker_heartbeat_prevents_false_quote_failure(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("东京 EC2 两台，按需。", {})
    store.claim_next("worker-a")
    record = store.get(public["job_id"])
    record["updated_at"] = "2000-01-01T00:00:00+00:00"
    store._write_atomic(store._path(public["job_id"]), record)
    store._write_atomic(
        store.heartbeat_path,
        {"updated_at": store.get(public["job_id"])["created_at"], "logged_in": True},
    )
    heartbeat = store._read(store.heartbeat_path)
    heartbeat["updated_at"] = store._event("heartbeat", "alive")["time"]
    store._write_atomic(store.heartbeat_path, heartbeat)

    sales = store.public_get(public["job_id"])

    assert sales["status"] == "processing"
    assert sales["failure_code"] is None


def test_only_real_generation_can_receive_one_bounded_grace_period() -> None:
    assert should_extend_quote_deadline(
        deadline_reached=True,
        generation_active=True,
        retry_visible=False,
    )
    assert not should_extend_quote_deadline(
        deadline_reached=True,
        generation_active=False,
        retry_visible=True,
    )
    assert not should_extend_quote_deadline(
        deadline_reached=True,
        generation_active=False,
        retry_visible=False,
    )
    assert not should_extend_quote_deadline(
        deadline_reached=False,
        generation_active=True,
        retry_visible=False,
    )


def test_sales_progress_comes_from_backend_checkpoint_not_chat_text(tmp_path: Path) -> None:
    checkpoints = tmp_path / "v2-quotes"
    store = GptQuoteRelayStore(tmp_path / "relay", checkpoint_directory=checkpoints)
    public = store.create("东京 EC2 两台，按需。", {})
    store.claim_next("worker-a")
    checkpoints.mkdir(parents=True)
    (checkpoints / f"relay-{public['job_id']}.json").write_text(
        json.dumps(
            {
                "schema_version": "astraquote-relay-checkpoint/1",
                "relay_job_id": public["job_id"],
                "stage": "pricing_partial",
                "total_component_count": 40,
                "completed_component_count": 38,
                "failed_component_count": 2,
                "updated_at": "2026-09-12T01:02:03.000Z",
                "incomplete_query_ids": ["must-not-leak"],
                "error": {"message": "must-not-leak"},
            }
        ),
        encoding="utf-8",
    )

    sales = store.public_get(public["job_id"])

    assert sales["progress"] == {
        "stage": "pricing_partial",
        "total_component_count": 40,
        "completed_component_count": 38,
        "failed_component_count": 2,
        "updated_at": "2026-09-12T01:02:03.000Z",
    }
    assert "incomplete_query_ids" not in json.dumps(sales)
    assert "must-not-leak" not in json.dumps(sales)


def test_same_stalled_checkpoint_allows_only_one_continuation(tmp_path: Path) -> None:
    checkpoints = tmp_path / "v2-quotes"
    store = GptQuoteRelayStore(tmp_path / "relay", checkpoint_directory=checkpoints)
    public = store.create("东京 EC2 两台，按需。", {})
    store.claim_next("worker-a")
    checkpoints.mkdir(parents=True)
    checkpoint_path = checkpoints / f"relay-{public['job_id']}.json"
    checkpoint_path.write_text(
        json.dumps(
            {
                "relay_job_id": public["job_id"],
                "stage": "pricing_partial",
                "total_component_count": 40,
                "completed_component_count": 38,
                "failed_component_count": 2,
                "updated_at": "2026-09-12T01:02:03.000Z",
            }
        ),
        encoding="utf-8",
    )

    assert store.reserve_continuation(public["job_id"], maximum=1) is True
    assert store.reserve_continuation(public["job_id"], maximum=1) is False
    record = store.get(public["job_id"])
    assert record["continuation_attempts"] == 1
    assert record["stalled_continuation_attempts"] == 1
    assert store.request_partial_finalization(public["job_id"]) is True
    assert store.request_partial_finalization(public["job_id"]) is False


def test_real_checkpoint_progress_does_not_create_a_second_automatic_retry(tmp_path: Path) -> None:
    checkpoints = tmp_path / "v2-quotes"
    store = GptQuoteRelayStore(tmp_path / "relay", checkpoint_directory=checkpoints)
    public = store.create("东京 EC2 两台，按需。", {})
    store.claim_next("worker-a")
    checkpoints.mkdir(parents=True)
    checkpoint_path = checkpoints / f"relay-{public['job_id']}.json"
    checkpoint = {
        "relay_job_id": public["job_id"],
        "stage": "pricing_partial",
        "total_component_count": 40,
        "completed_component_count": 20,
        "failed_component_count": 20,
    }
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    assert store.reserve_continuation(public["job_id"], maximum=1) is True

    checkpoint.update({"completed_component_count": 38, "failed_component_count": 2})
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

    assert store.reserve_continuation(public["job_id"], maximum=1) is False
    record = store.get(public["job_id"])
    assert record["continuation_attempts"] == 1
    assert record["stalled_continuation_attempts"] == 1


def test_failed_attempt_churn_cannot_reset_retry_budget(tmp_path: Path) -> None:
    checkpoints = tmp_path / "v2-quotes"
    store = GptQuoteRelayStore(tmp_path / "relay", checkpoint_directory=checkpoints)
    job_id = store.create("独立组件。", {})["job_id"]
    store.claim_next("worker-a")
    checkpoints.mkdir(parents=True)
    checkpoint_path = checkpoints / f"relay-{job_id}.json"
    checkpoint = {
        "relay_job_id": job_id,
        "stage": "pricing_partial",
        "total_component_count": 40,
        "completed_component_count": 38,
        "completed_component_keys": [f"cmp-{i}" for i in range(38)],
        "failed_component_count": 0,
        "batch_query_count": 40,
        "incomplete_query_count": 2,
    }
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    assert store.reserve_continuation(job_id)
    checkpoint.update(batch_query_count=55, incomplete_query_count=17, failed_component_count=2)
    checkpoint["completed_component_keys"].reverse()
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    assert not store.reserve_continuation(job_id)
    checkpoint.update(stage="pricing_request_rejected", batch_query_count=80)
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    assert not store.reserve_continuation(job_id)
    checkpoint.update(stage="pricing_partial", failed_component_count=0)
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    assert not store.reserve_continuation(job_id)


def test_stage_progress_never_creates_more_than_one_automatic_retry(tmp_path: Path) -> None:
    checkpoints = tmp_path / "v2-quotes"
    store = GptQuoteRelayStore(tmp_path / "relay", checkpoint_directory=checkpoints)
    job_id = store.create("独立组件。", {})["job_id"]
    store.claim_next("worker-a")
    checkpoints.mkdir(parents=True)
    checkpoint_path = checkpoints / f"relay-{job_id}.json"
    checkpoint = {
        "relay_job_id": job_id, "stage": "pricing_partial",
        "total_component_count": 40, "completed_component_count": 38,
    }
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    assert store.reserve_continuation(job_id)
    assert not store.reserve_continuation(job_id)
    checkpoint.update(stage="estimate_validated")
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    assert not store.reserve_continuation(job_id)
    checkpoint.update(stage="pricing_partial")
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    assert not store.reserve_continuation(job_id)
    assert store.request_partial_finalization(job_id)
    checkpoint.update(stage="artifacts_generated")
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    assert not store.reserve_continuation(job_id)


def test_component_batches_consume_the_shared_four_chat_slots(tmp_path: Path) -> None:
    checkpoints = tmp_path / "v2-quotes"
    store = GptQuoteRelayStore(
        tmp_path / "relay",
        checkpoint_directory=checkpoints,
        max_concurrent_quotes=4,
    )
    first = store.create("第一份大型报价。", {})
    second = store.create("第二份大型报价。", {})
    third = store.create("第三份普通报价。", {})
    first_record = store.claim_next("worker-a")
    assert first_record is not None
    checkpoints.mkdir(parents=True)
    (checkpoints / f"relay-{first['job_id']}.json").write_text(
        json.dumps(
            {
                "relay_job_id": first["job_id"],
                "stage": "requirements_cleaned",
                "total_component_count": 40,
            }
        ),
        encoding="utf-8",
    )
    second_record = store.claim_next("worker-a")
    assert second_record is not None
    (checkpoints / f"relay-{second['job_id']}.json").write_text(
        json.dumps(
            {
                "relay_job_id": second["job_id"],
                "stage": "requirements_cleaned",
                "total_component_count": 40,
            }
        ),
        encoding="utf-8",
    )

    assert store.claim_next("worker-a") is None
    queued = store.public_get(third["job_id"])
    assert queued["active_quote_count"] == 4
    assert queued["max_concurrent_quotes"] == 4


def test_relay_builds_private_chat_batches_from_the_sealed_price_plan(tmp_path: Path) -> None:
    checkpoints = tmp_path / "v2-quotes"
    store = GptQuoteRelayStore(tmp_path / "relay", checkpoint_directory=checkpoints)
    public = store.create("整单原始报价资料。", {})
    store.claim_next("worker-a")
    store.purge_source(public["job_id"])
    checkpoints.mkdir(parents=True)
    batch_id = "aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    (checkpoints / f"relay-{public['job_id']}.json").write_text(
        json.dumps({"relay_job_id": public["job_id"], "price_batch_id": batch_id}),
        encoding="utf-8",
    )
    component_plan = [
        {
            "component_key": f"cmp_root_{index:04d}",
            "customer_owned_source": f"清洗组件 {index}。",
            "billing_scopes": [{"billing_key": "base"}],
        }
        for index in range(21)
    ]
    (checkpoints / f"{batch_id}.json").write_text(
        json.dumps(
            {
                "price_batch_id": batch_id,
                "relay_job_id": public["job_id"],
                "quote_components": component_plan,
                "component_lifecycle": [],
            }
        ),
        encoding="utf-8",
    )

    batches = store.quote_chat_batches(public["job_id"])

    assert [len(batch["components"]) for batch in batches] == [5, 5, 5, 5, 1]
    assert batches[4]["component_keys"] == ["cmp_root_0020"]
    sales = store.public_get(public["job_id"])
    assert "customer_owned_source" not in json.dumps(sales)
    assert "整单原始报价资料" not in json.dumps(sales)


def test_relay_persists_multiple_codex_chat_references_for_one_sales_quote(
    tmp_path: Path,
) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("两批报价。", {})
    store.claim_next("worker-a")

    store.record_chat_session(
        public["job_id"], batch_index=0, batch_count=2,
        chat_url="codex-chat://conversations/6aa52a28-5510-83ee-b69a-42c10c9f1ddb",
        role="coordinator", component_keys=["cmp_root_0001"],
    )
    store.record_chat_session(
        public["job_id"], batch_index=1, batch_count=2,
        chat_url="codex-chat://conversations/6aa5306f-4ed8-83e9-9c0d-466e73829f51",
        role="component_batch", component_keys=["cmp_root_0021"],
    )

    record = store.get(public["job_id"])
    assert record["chat_url"].endswith("/6aa52a28-5510-83ee-b69a-42c10c9f1ddb")
    assert [item["batch_index"] for item in record["chat_sessions"]] == [0, 1]
    assert "chat_sessions" not in store.public_get(public["job_id"])


def test_relay_rejects_web_chat_reference_for_new_sales_quote(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    public = store.create("独立报价。", {})
    store.claim_next("worker-a")

    with pytest.raises(GptRelayError, match="报价对话地址"):
        store.record_chat_session(
            public["job_id"], batch_index=0, batch_count=1,
            chat_url="https://chatgpt.com/c/legacy-web-chat",
            role="coordinator", component_keys=[],
        )


def test_sales_receipt_accepts_sixty_successful_components(tmp_path: Path) -> None:
    store = GptQuoteRelayStore(tmp_path)
    components = [
        {
            "service_name": f"云服务 {index + 1}",
            "model_or_plan": "标准规格",
            "quantity": "1",
            "configuration_summary": "已核价配置",
            "scenario_costs": [
                {
                    "scenario_key": "on_demand",
                    "label": "按需付费",
                    "monthly_cost": "1.00",
                    "upfront_cost": "0",
                }
            ],
        }
        for index in range(60)
    ]

    public_result = store._page_result_public(
        {
            "schema_version": "astraquote-page-result/1",
            "is_partial": False,
            "currency": "USD",
            "region": "ap-southeast-1",
            "components": components,
            "unpriced_components": [],
            "scenarios": [
                {
                    "scenario_key": "on_demand",
                    "label": "按需付费",
                    "monthly_total": "60.00",
                    "upfront_total": "0",
                }
            ],
        }
    )

    assert public_result is not None
    assert len(public_result["components"]) == 60


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
        limit=4,
        lease_minutes=35,
    )

    assert [record["job_id"] for record in resumed] == [public["job_id"]]
    assert resumed[0]["status"] == "processing"
    assert resumed[0]["customer_request"] == ""
    assert resumed[0]["worker_id"] == "worker-new"


def test_existing_submitted_jobs_are_reattached_after_the_new_limit_is_enabled(
    tmp_path: Path,
) -> None:
    checkpoints = tmp_path / "v2-quotes"
    checkpoints.mkdir(parents=True)
    legacy_store = GptQuoteRelayStore(
        tmp_path / "relay",
        checkpoint_directory=checkpoints,
        max_concurrent_quotes=6,
    )
    jobs = [legacy_store.create(f"报价需求 {index}", {}) for index in range(6)]
    for job in jobs:
        claimed = legacy_store.claim_next("worker-old")
        assert claimed is not None
        (checkpoints / f"relay-{job['job_id']}.json").write_text(
            json.dumps(
                {
                    "relay_job_id": job["job_id"],
                    "stage": "requirements_cleaned",
                    "top_level_component_count": 1,
                    "total_component_count": 1,
                }
            ),
            encoding="utf-8",
        )
        legacy_store.update(
            job["job_id"],
            {"chat_url": f"https://chatgpt.com/g/g-p-abc123/c/quote-{job['job_id']}"},
        )
        legacy_store.purge_source(job["job_id"])

    store = GptQuoteRelayStore(
        tmp_path / "relay",
        checkpoint_directory=checkpoints,
        max_concurrent_quotes=4,
    )
    resumed = store.claim_submitted_for_monitoring(
        "worker-new",
        limit=None,
        lease_minutes=35,
    )

    assert [record["job_id"] for record in resumed] == [job["job_id"] for job in jobs]
