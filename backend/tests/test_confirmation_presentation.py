from copy import deepcopy
import pytest

from app.domain.models import ConfirmationItem, ConfirmationOption, ParsedIntent, ServiceRequirement
from app.services.confirmation_sessions import ConfirmationSessionStore
from app.services.confirmation_presentation import customer_confirmation_item


def backup_question():
    return ConfirmationItem(
        question="AWS Backup 在 AWS 官方报价器里分为多种类型，请选择这项实际使用的服务。",
        answer_key="component-0:stable-key", component_id="0", service="backup",
        options=[ConfirmationOption(label="EFS Backup", value="official_template:amazonEfsBackup",
                                    description="AWS 官方报价器子模板"),
                 ConfirmationOption(label="S3 Backup", value="official_template:s3Backup")],
    )


def test_presentation_changes_copy_only_and_is_idempotent():
    original = backup_question()
    before = deepcopy(original.model_dump())
    presented = customer_confirmation_item(original)
    assert "备份哪种资源" in presented.question
    assert presented.options[0].label == "共享文件系统（EFS）"
    assert "存储桶" in presented.options[1].description
    assert "子模板" not in presented.model_dump_json()
    assert presented.answer_key == original.answer_key
    assert presented.component_id == original.component_id
    assert [o.value for o in presented.options] == [o.value for o in original.options]
    assert original.model_dump() == before
    assert customer_confirmation_item(presented) == presented


def test_unknown_official_option_is_not_hidden_or_mapped_to_another_product():
    item = backup_question()
    item.options.append(ConfirmationOption(label="New Product Backup", value="official_template:newProduct"))
    presented = customer_confirmation_item(item)
    assert presented.options[-1].label == "New Product Backup"
    assert presented.options[-1].value == "official_template:newProduct"
    assert len(presented.options) == 3


def test_non_template_questions_are_unchanged():
    item = ConfirmationItem(question="选择备份保留天数", service="backup",
                            options=[ConfirmationOption(label="7 天", value="7")])
    assert customer_confirmation_item(item) == item


def test_legacy_question_without_answer_key_keeps_its_submission_identity():
    item = backup_question().model_copy(update={"answer_key": None})
    assert customer_confirmation_item(item).answer_key == item.question


@pytest.mark.parametrize("code", ["amazonEfsBackup", "vMwareBackup", "timestreamBackup",
    "storageGatewayBackup", "fsxBackup", "s3Backup", "redshiftBackup", "rdsBackup",
    "ebsBackup", "backupDynamoDb", "auroraBackup", "neptuneBackup", "docDbBackup",
    "sapHanaBackup", "auroraDsqlBackup"])
def test_each_current_backup_choice_has_chinese_explanation_and_exact_value(code):
    item = backup_question().model_copy(update={"options": [ConfirmationOption(
        label="Official name", value=f"official_template:{code}")]})
    option = customer_confirmation_item(item).options[0]
    assert "（" in option.label
    assert option.description.startswith("备份")
    assert option.value == f"official_template:{code}"


def test_existing_session_gets_readable_copy_without_changing_saved_answer_keys(tmp_path):
    store = ConfirmationSessionStore(tmp_path / "sessions.sqlite3")
    item = backup_question()
    token = store.create_or_replace(draft_id="draft-copy", customer_request="AWS Backup",
        customer_summary="AWS Backup", intent=ParsedIntent(customer_summary="Backup",
        services=[ServiceRequirement(service="backup")]), confirmation_text=item.question, items=[item])
    response = store.get(token)
    assert "备份哪种资源" in response.confirmation_items[0].question
    assert response.confirmation_items[0].answer_key == item.answer_key
    submitted = store.submit(token, {item.answer_key: "official_template:s3Backup"})
    assert submitted.answers[item.answer_key] == "official_template:s3Backup"
