from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from app.domain.models import (
    ConfirmationItem,
    ConfirmationOption,
    ParsedIntent,
    QuoteRequest,
    ServiceRequirement,
)
from app.services.confirmation_sessions import (
    CONFIGURATION_COMPONENT_DELETE,
    CONFIGURATION_COMPONENT_FEEDBACK_PREFIX,
    CONFIGURATION_COMPONENT_UPDATE_PREFIX,
    CONFIGURATION_FEEDBACK_QUESTION,
    ConfirmationSessionStore,
)


def test_confirmation_session_round_trip(tmp_path: Path) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    intent = ParsedIntent(
        customer_summary="东京 EC2",
        services=[ServiceRequirement(service="ec2", region="ap-northeast-1")],
    )
    item = ConfirmationItem(
        question="是否跨可用区部署？",
        options=[ConfirmationOption(label="同意", value="同意跨可用区")],
    )

    token = store.create_or_replace(
        draft_id="draft0000001",
        customer_request="东京 EC2",
        customer_summary="东京 EC2",
        intent=intent,
        confirmation_text="请确认",
        items=[item],
    )
    pending = store.get(token)
    assert pending is not None
    assert pending.status == "pending"
    assert pending.confirmation_items == [item]

    submitted = store.submit(token, {item.question: "同意跨可用区"})
    assert submitted is not None
    assert submitted.status == "reviewing"
    assert submitted.answers[item.question] == "同意跨可用区"

    restored = store.restore_draft("draft0000001")
    assert restored is not None
    assert restored[0] == "东京 EC2"
    assert restored[1] == intent

    store.complete_by_draft("draft0000001")
    completed = store.get(token)
    assert completed is not None
    assert completed.status == "completed"


def test_saved_eks_worker_quantity_is_reconciled_before_customer_display(
    tmp_path: Path,
) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    source = (
        "Amazon EKS：数量2，每个集群配置3个Worker节点，"
        "Worker节点单台4核8GB/100GB存储"
    )
    intent = ParsedIntent(
        customer_summary="EKS",
        services=[
            ServiceRequirement(service="eks", quantity=2, source_text=source),
            ServiceRequirement(
                service="ec2",
                derived_from_service="eks",
                calculator_service_name="Amazon EC2 (EKS Worker Nodes)",
                quantity=2,
                source_text=source,
            ),
        ],
    )
    token = store.create_or_replace(
        draft_id="eksworkers01",
        customer_request=source,
        customer_summary="EKS",
        intent=intent,
        confirmation_text="请确认",
        items=[],
    )

    session = store.get(token)

    assert session is not None
    worker = next(
        item for item in session.configuration_items if "Worker" in item.display_name
    )
    assert worker.quantity == 6
    assert worker.component_number == "1.1"
    assert worker.parent_component_number == "1"


def test_configuration_review_uses_stable_ids_for_parent_and_child(
    tmp_path: Path,
) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    source = "Amazon EKS：数量1，3个Worker节点，单台4核8GB/100GB存储"
    intent = ParsedIntent(
        customer_summary="EKS",
        services=[ServiceRequirement(service="eks", quantity=1, source_text=source)],
    )
    token = store.create_or_replace(
        draft_id="stable-review-ids",
        customer_request=source,
        customer_summary="EKS",
        intent=intent,
        confirmation_text="",
        items=[],
    )
    store.prepare_configuration_review(draft_id="stable-review-ids", intent=intent)

    review = store.get(token)

    assert review is not None
    parent = next(item for item in review.configuration_items if item.service == "eks")
    child = next(
        item
        for item in review.configuration_items
        if item.parent_component_number == parent.component_number
    )
    assert parent.component_id.startswith("cmp_")
    assert child.component_id.startswith("cmp_")
    assert child.parent_component_id == parent.component_id


def test_aws_and_azure_confirmation_storage_are_physically_isolated(
    tmp_path: Path,
) -> None:
    aws_store = ConfirmationSessionStore(tmp_path / "aws.sqlite3", "aws")
    azure_store = ConfirmationSessionStore(tmp_path / "azure.sqlite3", "azure")
    azure_intent = ParsedIntent(
        customer_summary="Azure Redis",
        services=[
            ServiceRequirement(
                service="azure_cache",
                calculator_service_name="Azure Cache for Redis",
                product_identity="azure_cache",
                source_text="Azure Cache for Redis Standard C1",
            )
        ],
    )
    token = azure_store.create_or_replace(
        draft_id="az0000000001",
        customer_request="Azure Cache for Redis",
        customer_summary="Azure Redis",
        intent=azure_intent,
        confirmation_text="请确认",
        items=[],
    )

    assert token.startswith("azure_")
    assert aws_store.get(token) is None
    assert aws_store.restore_draft("az0000000001") is None
    restored = azure_store.get(token)
    assert restored is not None
    assert restored.cloud_provider == "azure"
    assert restored.configuration_items[0].service == "azure_cache"
    assert restored.configuration_items[0].display_name == "Azure Cache for Redis"


def test_confirmation_session_requires_every_answer(tmp_path: Path) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    intent = ParsedIntent(
        customer_summary="test",
        services=[ServiceRequirement(service="ec2")],
    )
    items = [
        ConfirmationItem(question="问题一？"),
        ConfirmationItem(question="问题二？"),
    ]
    token = store.create_or_replace(
        draft_id="draft0000002",
        customer_request="test",
        customer_summary="test",
        intent=intent,
        confirmation_text="请确认",
        items=items,
    )

    try:
        store.submit(token, {"问题一？": "同意"})
    except ValueError as exc:
        assert "1 项未填写" in str(exc)
    else:
        raise AssertionError("missing answers must be rejected")


def test_same_draft_reuses_link_and_moves_to_configuration_review(tmp_path: Path) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    intent = ParsedIntent(
        customer_summary="法兰克福应用",
        services=[
            ServiceRequirement(
                service="ec2",
                calculator_service_name="Amazon EC2",
                region="eu-central-1",
                quantity=2,
                requirements={
                    "vcpu": 8,
                    "memory_gib": 16,
                    "_review_selected_model": "c7g.2xlarge",
                    "_review_selected_specifications": {
                        "vCPU": 8,
                        "memoryGiB": 16,
                    },
                    "_review_available_shapes": [
                        {"vcpu": 2, "memory_gib": 4},
                        {"vcpu": 2, "memory_gib": 8},
                        {"vcpu": 8, "memory_gib": 16},
                    ],
                    "_review_field_options": {
                        "operating_system": ["linux", "windows"],
                    },
                },
                source_text="EC2 8核16G 2台",
            )
        ],
    )
    first = store.create_or_replace(
        draft_id="draft0000003",
        customer_request="法兰克福 EC2",
        customer_summary=intent.customer_summary,
        intent=intent,
        confirmation_text="请确认区域",
        items=[ConfirmationItem(question="确认区域？")],
    )
    store.submit(first, {"确认区域？": "法兰克福"})

    second = store.create_or_replace(
        draft_id="draft0000003",
        customer_request="法兰克福 EC2",
        customer_summary=intent.customer_summary,
        intent=intent,
        confirmation_text="最终配置",
        items=[],
    )
    assert second == first
    token = store.prepare_configuration_review(draft_id="draft0000003", intent=intent)
    assert token == first
    review = store.get(first)
    assert review is not None
    assert review.status == "configuration_review"
    assert review.configuration_items[0].quantity == 2
    assert review.configuration_items[0].requirements["vcpu"] == 8
    assert review.configuration_items[0].selected_model == "c7g.2xlarge"
    assert review.configuration_items[0].official_specifications == {
        "vCPU": 8,
        "memoryGiB": 16,
    }
    assert review.configuration_items[0].available_shapes == [
        {"vcpu": 2.0, "memory_gib": 4.0},
        {"vcpu": 2.0, "memory_gib": 8.0},
        {"vcpu": 8.0, "memory_gib": 16.0},
    ]
    assert review.configuration_items[0].available_options == {
        "operating_system": ["linux", "windows"],
    }

    approved = store.approve_configuration(first)
    assert approved is not None
    assert approved.status == "approved"


def test_configuration_feedback_reuses_link_and_returns_to_reviewing(tmp_path: Path) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    intent = ParsedIntent(
        customer_summary="test",
        services=[ServiceRequirement(service="cloudfront")],
    )
    token = store.create_or_replace(
        draft_id="draft-feedback",
        customer_request="CDN 5TB",
        customer_summary="test",
        intent=intent,
        confirmation_text="最终配置",
        items=[],
    )
    store.prepare_configuration_review(draft_id="draft-feedback", intent=intent)

    submitted = store.submit_configuration_feedback(
        token, "CloudFront 重复了，只保留一个。"
    )

    assert submitted is not None
    assert submitted.status == "reviewing"
    assert submitted.answers == {
        CONFIGURATION_FEEDBACK_QUESTION: "CloudFront 重复了，只保留一个。"
    }


def test_stale_configuration_update_returns_to_editable_table(tmp_path: Path) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    intent = ParsedIntent(
        customer_summary="test",
        services=[ServiceRequirement(service="ec2", source_text="EC2 2台")],
    )
    token = store.create_or_replace(
        draft_id="draft-stale-feedback",
        customer_request="EC2 2台",
        customer_summary="test",
        intent=intent,
        confirmation_text="最终配置",
        items=[],
    )
    store.prepare_configuration_review(draft_id="draft-stale-feedback", intent=intent)
    store.submit_configuration_feedback(token, component_feedback={"0": "改成4核8G"})
    stale_time = (datetime.now(UTC) - timedelta(minutes=9)).isoformat()
    with store._connect() as connection:
        connection.execute(
            "UPDATE confirmation_sessions SET submitted_at = ? WHERE token = ?",
            (stale_time, token),
        )

    recovered = store.get(token)

    assert recovered is not None
    assert recovered.status == "configuration_review"
    assert "原配置已保留" in recovered.confirmation_text


def test_normal_five_minute_configuration_update_is_not_treated_as_stale(
    tmp_path: Path,
) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    intent = ParsedIntent(
        customer_summary="test",
        services=[ServiceRequirement(service="future_product", source_text="20项配置")],
    )
    token = store.create_or_replace(
        draft_id="long-valid-job",
        customer_request="20项独立配置",
        customer_summary="test",
        intent=intent,
        confirmation_text="最终配置",
        items=[],
    )
    store.prepare_configuration_review(draft_id="long-valid-job", intent=intent)
    store.submit_configuration_feedback(token, component_feedback={"0": "修改用量"})
    active_time = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    with store._connect() as connection:
        connection.execute(
            "UPDATE confirmation_sessions SET submitted_at = ? WHERE token = ?",
            (active_time, token),
        )

    active = store.get(token)

    assert active is not None
    assert active.status == "reviewing"


def test_legacy_product_identity_is_normalized_when_session_is_loaded(
    tmp_path: Path,
) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    intent = ParsedIntent(
        customer_summary="Prometheus",
        services=[
            ServiceRequirement(
                service="amp",
                product_identity="prometheus",
                source_text="Prometheus 指标监控",
            )
        ],
    )
    token = store.create_or_replace(
        draft_id="draft-legacy-identity",
        customer_request="Prometheus 指标监控",
        customer_summary="Prometheus",
        intent=intent,
        confirmation_text="请确认",
        items=[],
    )
    legacy = json.loads(intent.model_dump_json())
    legacy["services"][0]["product_identity"] = "Prometheus"
    with store._connect() as connection:
        connection.execute(
            "UPDATE confirmation_sessions SET intent_json = ? WHERE token = ?",
            (json.dumps(legacy, ensure_ascii=False), token),
        )

    restored = store.get(token)

    assert restored is not None
    assert restored.configuration_items[0].service == "amp"


def test_configuration_feedback_targets_only_selected_components(tmp_path: Path) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    intent = ParsedIntent(
        customer_summary="test",
        services=[
            ServiceRequirement(service="ec2", source_text="EC2 2 台"),
            ServiceRequirement(service="s3", source_text="S3 20TB"),
        ],
    )
    token = store.create_or_replace(
        draft_id="draft-targeted-feedback",
        customer_request="EC2 2 台，S3 20TB",
        customer_summary="test",
        intent=intent,
        confirmation_text="最终配置",
        items=[],
    )
    store.prepare_configuration_review(
        draft_id="draft-targeted-feedback", intent=intent
    )

    submitted = store.submit_configuration_feedback(
        token, component_feedback={"1": "S3 改为 30TB", "0": "   "}
    )

    assert submitted is not None
    assert submitted.answers == {
        f"{CONFIGURATION_COMPONENT_FEEDBACK_PREFIX}1": "S3 改为 30TB"
    }
    assert [item.component_id for item in submitted.configuration_items] == ["0", "1"]

    store.prepare_configuration_review(
        draft_id="draft-targeted-feedback", intent=intent
    )
    with pytest.raises(ValueError, match="刷新页面"):
        store.submit_configuration_feedback(
            token, component_feedback={"9": "修改容量"}
        )


def test_configuration_feedback_accepts_staged_component_deletion(tmp_path: Path) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    intent = ParsedIntent(
        customer_summary="test",
        services=[
            ServiceRequirement(service="ec2"),
            ServiceRequirement(service="s3"),
        ],
    )
    token = store.create_or_replace(
        draft_id="draft-delete-component",
        customer_request="EC2 和 S3",
        customer_summary="test",
        intent=intent,
        confirmation_text="最终配置",
        items=[],
    )
    store.prepare_configuration_review(
        draft_id="draft-delete-component", intent=intent
    )

    submitted = store.submit_configuration_feedback(
        token,
        feedback="请新增一项 CloudFront",
        component_feedback={"1": CONFIGURATION_COMPONENT_DELETE},
    )

    assert submitted is not None
    assert submitted.status == "reviewing"
    assert submitted.answers == {
        CONFIGURATION_FEEDBACK_QUESTION: "请新增一项 CloudFront",
        f"{CONFIGURATION_COMPONENT_FEEDBACK_PREFIX}1": CONFIGURATION_COMPONENT_DELETE,
    }

    store.prepare_configuration_review(
        draft_id="draft-delete-component", intent=intent
    )
    with pytest.raises(ValueError, match="至少需要保留一项"):
        store.submit_configuration_feedback(
            token,
            component_feedback={
                "0": CONFIGURATION_COMPONENT_DELETE,
                "1": CONFIGURATION_COMPONENT_DELETE,
            },
        )


def test_customer_answers_are_partitioned_by_component(tmp_path: Path) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    intent = ParsedIntent(
        customer_summary="test",
        services=[
            ServiceRequirement(service="rds"),
            ServiceRequirement(service="s3"),
        ],
    )
    rds_question = "【组件1·Amazon RDS】请选择单可用区还是主备。"
    region_question = "请确认所有服务的部署区域。"
    store.create_or_replace(
        draft_id="draft-partition",
        customer_request="RDS 和 S3",
        customer_summary="test",
        intent=intent,
        confirmation_text="确认",
        items=[
            ConfirmationItem(
                question=rds_question,
                component_id="0",
                service="rds",
            ),
            ConfirmationItem(question=region_question),
        ],
    )

    component, global_answers = store.partition_answers_by_component(
        "draft-partition",
        {rds_question: "主备", region_question: "法兰克福"},
    )

    assert component == {0: {rds_question: "主备"}}
    assert global_answers == {region_question: "法兰克福"}


def test_identical_visible_questions_keep_independent_component_answers(
    tmp_path: Path,
) -> None:
    store = ConfirmationSessionStore(tmp_path / "independent-answers.sqlite3")
    intent = ParsedIntent(
        customer_summary="two independent EC2 components",
        services=[
            ServiceRequirement(service="ec2"),
            ServiceRequirement(service="ec2"),
        ],
    )
    question = "EC2 还没有指定型号，请选择需要的型号。"
    first = ConfirmationItem(
        question=question,
        answer_key="component-0:first",
        component_id="0",
        service="ec2",
    )
    second = ConfirmationItem(
        question=question,
        answer_key="component-1:second",
        component_id="1",
        service="ec2",
    )
    token = store.create_or_replace(
        draft_id="draft-independent-answers",
        customer_request="two EC2 components",
        customer_summary="test",
        intent=intent,
        confirmation_text="confirm all",
        items=[first, second],
    )

    submitted = store.submit(
        token,
        {
            first.answer_key or "": "选择 t3.small",
            second.answer_key or "": "选择 c7g.xlarge",
        },
    )
    assert submitted is not None
    assert submitted.answers == {
        "component-0:first": "选择 t3.small",
        "component-1:second": "选择 c7g.xlarge",
    }

    component, global_answers = store.partition_answers_by_component(
        "draft-independent-answers", submitted.answers
    )

    assert component == {
        0: {question: "选择 t3.small"},
        1: {question: "选择 c7g.xlarge"},
    }
    assert global_answers == {}


def test_confirmation_round_survives_replacing_the_same_draft(tmp_path: Path) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    intent = ParsedIntent(
        customer_summary="test",
        services=[ServiceRequirement(service="ec2")],
    )
    question = "请选择处理器"
    token = store.create_or_replace(
        draft_id="draft-one-page",
        customer_request="EC2",
        customer_summary="test",
        intent=intent,
        confirmation_text="确认",
        items=[ConfirmationItem(question=question)],
    )
    store.submit(token, {question: "4 vCPU"})

    store.create_or_replace(
        draft_id="draft-one-page",
        customer_request="EC2",
        customer_summary="test",
        intent=intent,
        confirmation_text="不应再显示第二页",
        items=[ConfirmationItem(question="请选择型号")],
    )

    assert store.confirmation_round_by_draft("draft-one-page") == 1
    assert store.asked_questions_by_draft("draft-one-page") == [
        "请选择处理器",
        "请选择型号",
    ]


def test_confirmation_question_deduplication_keeps_complete_question() -> None:
    short = ConfirmationItem(
        question="请确认当前组件在所选区域支持的处理器和内存配置？",
        component_id="component-a",
        service="Amazon EC2",
    )
    complete = ConfirmationItem(
        question="请确认当前组件在所选区域支持的处理器和内存配置，并选择最终型号。",
        component_id="component-a",
        service="Amazon EC2",
    )

    result = ConfirmationSessionStore._deduplicate_confirmation_items(
        [short, complete]
    )

    assert result == [complete]


def test_confirmation_question_deduplication_does_not_cross_components() -> None:
    first = ConfirmationItem(
        question="请确认当前组件在所选区域支持的处理器和内存配置？",
        component_id="component-a",
        service="Amazon EC2",
    )
    second = ConfirmationItem(
        question="请确认当前组件在所选区域支持的处理器和内存配置，并选择最终型号。",
        component_id="component-b",
        service="Amazon RDS",
    )

    result = ConfirmationSessionStore._deduplicate_confirmation_items(
        [first, second]
    )

    assert result == [first, second]


def test_structured_component_update_is_stored_without_free_form_ai_text(
    tmp_path: Path,
) -> None:
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    intent = ParsedIntent(
        customer_summary="test",
        services=[ServiceRequirement(service="s3", requirements={"storage_class": "standard"})],
    )
    token = store.create_or_replace(
        draft_id="draft-structured-edit",
        customer_request="S3",
        customer_summary="test",
        intent=intent,
        confirmation_text="确认",
        items=[],
    )
    store.prepare_configuration_review(
        draft_id="draft-structured-edit", intent=intent
    )

    submitted = store.submit_configuration_feedback(
        token,
        component_updates={"0": {"requirements": {"storage_gib": 20480}}},
    )

    assert submitted is not None
    assert submitted.status == "reviewing"
    assert submitted.answers == {
        f"{CONFIGURATION_COMPONENT_UPDATE_PREFIX}0": '{"requirements":{"storage_gib":20480}}'
    }


def test_submitted_aws_edit_can_be_claimed_without_sales_browser(tmp_path: Path) -> None:
    store = ConfirmationSessionStore(tmp_path / "self-processing.sqlite3")
    intent = ParsedIntent(
        customer_summary="test",
        services=[ServiceRequirement(service="ec2", region="ap-southeast-1")],
    )
    original_request = QuoteRequest(
        customer_request="EC2 新加坡",
        draft_id="selfprocess1",
        pricing_mode="standard_reserved",
        reserved_term_years=3,
        payment_option="all_upfront",
    )
    token = store.create_or_replace(
        draft_id="selfprocess1",
        customer_request=original_request.customer_request,
        customer_summary="test",
        intent=intent,
        confirmation_text="确认",
        items=[],
        quote_request=original_request,
    )
    store.prepare_configuration_review(draft_id="selfprocess1", intent=intent)
    store.submit_configuration_feedback(
        token,
        component_updates={"0": {"quantity": 3}},
    )

    claimed = store.begin_configuration_reprocessing(token)

    assert claimed is not None
    assert claimed.draft_id == "selfprocess1"
    assert claimed.pricing_mode == "standard_reserved"
    assert claimed.reserved_term_options == [3]
    assert claimed.confirmation_responses == {
        f"{CONFIGURATION_COMPONENT_UPDATE_PREFIX}0": '{"quantity":3}'
    }
    processing = store.get(token)
    assert processing is not None
    assert processing.status == "processing"
    assert store.begin_configuration_reprocessing(token) is None


def test_configuration_items_expose_service_specific_billing_fields(tmp_path: Path) -> None:
    store = ConfirmationSessionStore(tmp_path / "billing-fields.sqlite3")
    intent = ParsedIntent(
        customer_summary="Cloud Map 和 RDS",
        services=[
            ServiceRequirement(service="cloud_map", region="ap-southeast-1"),
            ServiceRequirement(service="rds", region="ap-southeast-1"),
            ServiceRequirement(service="appconfig", region="ap-southeast-1"),
        ],
    )
    token = store.create_or_replace(
        draft_id="billingfield1",
        customer_request="Cloud Map 和 RDS",
        customer_summary=intent.customer_summary,
        intent=intent,
        confirmation_text="确认",
        items=[],
    )
    store.prepare_configuration_review(draft_id="billingfield1", intent=intent)

    session = store.get(token)

    assert session is not None
    assert session.configuration_items[0].available_billing_fields == [
        "namespaces",
        "service_instances",
        "api_calls",
        "dns_queries",
    ]
    assert "log_ingestion_gib" not in session.configuration_items[0].available_billing_fields
    assert "data_scanned_gib" not in session.configuration_items[0].available_billing_fields
    assert "storage_gib" in session.configuration_items[1].available_billing_fields
    assert "dns_queries" not in session.configuration_items[1].available_billing_fields
    assert session.configuration_items[2].available_billing_fields == [
        "configuration_requests",
        "configuration_retrievals",
        "experiment_hours",
    ]
