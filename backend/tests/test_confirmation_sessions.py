from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from app.domain.models import ConfirmationItem, ConfirmationOption, ParsedIntent, ServiceRequirement
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
    stale_time = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
    with store._connect() as connection:
        connection.execute(
            "UPDATE confirmation_sessions SET submitted_at = ? WHERE token = ?",
            (stale_time, token),
        )

    recovered = store.get(token)

    assert recovered is not None
    assert recovered.status == "configuration_review"
    assert "原配置已保留" in recovered.confirmation_text


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
