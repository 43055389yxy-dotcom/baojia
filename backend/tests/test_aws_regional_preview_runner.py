import json
from pathlib import Path

import pytest

from scripts import aws_regional_preview_runner as runner


def scenario(*lines: str) -> runner.Scenario:
    return runner.Scenario(
        case_id=1,
        name="semantic regression",
        expected_region="ap-southeast-1",
        lines=tuple(lines),
    )


def selection(
    component_id: str,
    component_number: str,
    service: str,
    display_name: str,
    source_text: str,
    **extra: object,
) -> dict[str, object]:
    return {
        "component_id": component_id,
        "component_number": component_number,
        "service": service,
        "display_name": display_name,
        "source_text": source_text,
        **extra,
    }


def test_numbered_scenario_parser_keeps_blocks_and_continuations() -> None:
    parsed = runner.scenario_numbered_components(
        (
            "方案 2026｜新加坡",
            "1、Amazon EC2：3台，m6a.2xlarge",
            "  每台磁盘500G",
            "2) Amazon S3：Standard存储10TB",
        )
    )

    assert [item["component_number"] for item in parsed] == [1, 2]
    assert parsed[0]["heading"] == "Amazon EC2"
    assert parsed[0]["source_text"].endswith("每台磁盘500G")
    assert parsed[1]["heading"] == "Amazon S3"


def test_glacier_official_offer_identity_matches_its_s3_customer_heading() -> None:
    assert runner.component_identity("S3 Glacier Flexible Retrieval") == "s3"
    assert runner.component_identity("glacier") == "s3"


def test_load_scenarios_rejects_non_contiguous_component_numbers(
    tmp_path: Path,
) -> None:
    scenario_file = tmp_path / "scenarios.json"
    scenario_file.write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "name": "gap",
                    "expected_region": "ap-southeast-1",
                    "lines": ["方案", "1、Amazon EC2：1台", "3、Amazon S3：1TB"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="组件编号必须从1开始连续"):
        runner.load_scenarios(scenario_file)


def test_semantic_validation_accepts_owned_child_and_preserves_root_order() -> None:
    current = scenario(
        "方案｜新加坡",
        "1、Amazon EC2：3台，磁盘500G",
        "2、Amazon S3：Standard存储10TB",
    )
    result = {
        "selections": [
            selection("0", "1", "ec2", "Amazon EC2", "Amazon EC2：3台"),
            selection(
                "2",
                "1.1",
                "ebs",
                "Amazon EBS",
                "Amazon EBS：数据盘500G",
                parent_component_id="0",
                parent_component_number="1",
            ),
            selection("1", "2", "s3", "Amazon S3", "Amazon S3：10TB"),
        ]
    }

    validation = runner.validate_preview_semantics(current, result)

    assert validation["passed"] is True
    assert validation["actual_root_component_count"] == 2
    assert validation["derived_component_count"] == 1
    assert all(validation["checks"].values())


def test_semantic_validation_detects_cross_owned_source_and_orphan() -> None:
    current = scenario(
        "方案｜新加坡",
        "1、Application Load Balancer：2个",
        "2、AWS NAT Gateway：2个",
    )
    result = {
        "selections": [
            selection(
                "0",
                "1",
                "elb",
                "Application Load Balancer",
                "AWS NAT Gateway：2个",
            ),
            selection(
                "1",
                "2.1",
                "nat_gateway",
                "AWS NAT Gateway",
                "AWS NAT Gateway：2个",
                parent_component_id="missing-parent",
                parent_component_number="2",
            ),
        ]
    }

    validation = runner.validate_preview_semantics(current, result)
    codes = {item["code"] for item in validation["issues"]}

    assert validation["passed"] is False
    assert "component_source_identity_mismatch" in codes
    assert "orphan_component" in codes
    assert "root_component_count_mismatch" in codes


def test_semantic_validation_keeps_two_numbered_rows_of_same_service() -> None:
    current = scenario(
        "方案｜新加坡",
        "1、Amazon S3：生产数据10TB",
        "2、Amazon S3：归档数据20TB",
    )
    result = {
        "selections": [
            selection("0", "1", "s3", "Amazon S3", "Amazon S3：生产数据10TB"),
            selection("1", "2", "s3", "Amazon S3", "Amazon S3：归档数据20TB"),
        ]
    }

    validation = runner.validate_preview_semantics(current, result)

    assert validation["passed"] is True
    assert validation["actual_root_component_count"] == 2


def test_iot_subservice_keys_share_the_iot_identity_family() -> None:
    assert runner.component_identity("IoT Device Management") == "iot"
    assert runner.component_identity("io_t_device_management") == "iot"
    assert runner.component_identity("io_t_device_defender") == "iot"


def test_common_product_aliases_share_their_native_identity_family() -> None:
    assert runner.component_identity("DocDB兼容Mongo") == "documentdb"
    assert runner.component_identity("Amazon DocumentDB") == "documentdb"
    assert runner.component_identity("Elastic Compute Cloud VM") == "ec2"
    assert runner.component_identity("AWS Fargate") == "ecs"
    assert runner.component_identity("Elastic Block Store / EBS") == "ebs"


def test_semantic_validation_rejects_failed_child_relabelled_as_parent() -> None:
    current = scenario(
        "方案｜圣保罗",
        "1、Amazon EKS：1套，Worker节点6台",
    )
    result = {
        "selections": [
            selection("0", "1", "eks", "Amazon EKS", "Amazon EKS：1套"),
            selection(
                "1",
                "1.1",
                "eks",
                "Amazon EKS",
                "Worker节点6台",
                parent_component_id="0",
                parent_component_number="1",
                status="technical_issue",
                next_action="internal_block",
                issue_code="unconsumed_customer_pricing_facts",
            ),
        ]
    }

    validation = runner.validate_preview_semantics(current, result)
    codes = {item["code"] for item in validation["issues"]}

    assert validation["passed"] is False
    assert "component_technical_failure" in codes
    assert "derived_component_identity_matches_parent" in codes


def test_completed_transport_fails_case_when_semantics_do_not_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = scenario(
        "方案｜新加坡",
        "1、Application Load Balancer：2个",
    )
    responses = iter(
        [
            {
                "selected_region": "ap-southeast-1",
                "detected_regions": ["ap-southeast-1"],
                "requires_confirmation": False,
            },
            {"job_id": "aws-test", "status": "queued"},
            {
                "job_id": "aws-test",
                "status": "completed",
                "events": [],
                "result": {
                    "draft_id": "draft-test",
                    "selections": [
                        selection(
                            "0",
                            "1",
                            "elb",
                            "Application Load Balancer",
                            "AWS NAT Gateway：2个",
                        )
                    ],
                },
            },
        ]
    )

    def fake_request_json(*args: object, **kwargs: object) -> dict[str, object]:
        return next(responses)

    monkeypatch.setattr(runner, "request_json", fake_request_json)

    result = runner.run_scenario(
        object(),
        current,
        case_timeout=5,
        request_timeout=1,
        poll_interval=0.1,
    )

    assert result["transport_status"] == "completed"
    assert result["outcome"] == "failed"
    assert result["stage"] == "semantic_validation"
    assert result["failure"]["code"] == "preview_semantic_mismatch"
    assert result["semantic_validation"]["passed"] is False
