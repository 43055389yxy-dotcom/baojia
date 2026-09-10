#!/usr/bin/env python3
"""Run the fixed AWS regional scenarios through preflight and preview only.

The runner is intentionally sequential.  It never starts ``/api/quote-jobs``
and never calls the formal quote endpoint.  Each case performs:

1. ``POST /api/quotes/region-preflight``;
2. ``POST /api/preview-jobs`` with the verified region;
3. ``GET /api/quote-jobs/{job_id}`` until the preview reaches a terminal state.

On timeout it may call the cancellation endpoint for the preview job.  JSON
keeps the complete failed-job event trail and error payload; Markdown provides
a compact operator summary with diagnostic IDs and the latest failure events.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
REPOSITORY_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_SCENARIO_FILE = SCRIPT_DIR / "aws_regional_preview_scenarios.json"
DEFAULT_OUTPUT_DIR = REPOSITORY_ROOT / "outputs"
REGION_PREFLIGHT_PATH = "/api/quotes/region-preflight"
PREVIEW_START_PATH = "/api/preview-jobs"
JOB_STATUS_PATH = "/api/quote-jobs/{job_id}"
JOB_CANCEL_PATH = "/api/quote-jobs/{job_id}/cancel"
TERMINAL_JOB_STATUSES = {"completed", "failed"}
AWS_REGION_PATTERN = re.compile(r"^(?:af|ap|ca|cn|eu|il|me|mx|sa|us)(?:-gov)?-[a-z0-9-]+-\d$")
SCENARIO_NUMBERED_PATTERN = re.compile(
    r"^\s*(?:需求\s*)?(\d{1,3})\s*[、,，.．。)）:：;；\-—]\s*(.*)$",
    re.I,
)
SCENARIO_PAREN_NUMBERED_PATTERN = re.compile(
    r"^\s*(?:需求\s*)?[（(]\s*(\d{1,3})\s*[)）]"
    r"\s*[、,，.．。:：;；\-—]?\s*(.*)$",
    re.I,
)
SCENARIO_SPACE_NUMBERED_PATTERN = re.compile(
    r"^\s*(?:需求\s*)?(\d{1,3})\s+(\S.*)$",
    re.I,
)


def scenario_numbered_components(lines: tuple[str, ...]) -> list[dict[str, Any]]:
    """Return explicit 1..N customer blocks without interpreting their values."""

    components: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in lines:
        line = raw_line.strip()
        marker = (
            SCENARIO_PAREN_NUMBERED_PATTERN.match(line)
            or SCENARIO_NUMBERED_PATTERN.match(line)
            or SCENARIO_SPACE_NUMBERED_PATTERN.match(line)
        )
        if marker is not None:
            if current is not None:
                components.append(current)
            current = {
                "component_number": int(marker.group(1)),
                "source_text": marker.group(2).strip(),
            }
            continue
        if current is not None:
            current["source_text"] = (
                f"{current['source_text']}\n{line}".strip()
            )
    if current is not None:
        components.append(current)
    for component in components:
        source = str(component["source_text"])
        component["heading"] = re.split(r"[：:]", source, maxsplit=1)[0].strip()
    return components


@dataclass(frozen=True, slots=True)
class Scenario:
    case_id: int
    name: str
    expected_region: str
    lines: tuple[str, ...]

    @property
    def customer_request(self) -> str:
        return "\n".join(self.lines)

    @property
    def numbered_components(self) -> list[dict[str, Any]]:
        return scenario_numbered_components(self.lines)

    @property
    def component_count(self) -> int:
        return len(self.numbered_components)


class ApiRequestError(RuntimeError):
    def __init__(
        self,
        method: str,
        path: str,
        status_code: int | None,
        payload: object,
        message: str,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.path = path
        self.status_code = status_code
        self.payload = payload


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def load_scenarios(path: Path) -> list[Scenario]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"场景文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"场景 JSON 无效：{exc}") from exc
    if not isinstance(raw, list) or not raw:
        raise ValueError("场景文件必须是非空 JSON 数组")

    scenarios: list[Scenario] = []
    seen_ids: set[int] = set()
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"第 {position} 个场景必须是 JSON 对象")
        case_id = item.get("id")
        name = item.get("name")
        expected_region = item.get("expected_region")
        lines = item.get("lines")
        if not isinstance(case_id, int) or case_id <= 0:
            raise ValueError(f"第 {position} 个场景的 id 必须是正整数")
        if case_id in seen_ids:
            raise ValueError(f"场景 id 重复：{case_id}")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"场景 {case_id} 缺少 name")
        if not isinstance(expected_region, str) or not AWS_REGION_PATTERN.fullmatch(
            expected_region
        ):
            raise ValueError(f"场景 {case_id} 的 expected_region 无效")
        if (
            not isinstance(lines, list)
            or len(lines) < 2
            or any(not isinstance(line, str) or not line.strip() for line in lines)
        ):
            raise ValueError(f"场景 {case_id} 的 lines 必须至少包含两行非空文本")
        customer_request = "\n".join(lines)
        if len(customer_request) > 12000:
            raise ValueError(f"场景 {case_id} 超过 QuoteRequest 12000 字符上限")
        scenario = Scenario(
            case_id=case_id,
            name=name.strip(),
            expected_region=expected_region,
            lines=tuple(line.strip() for line in lines),
        )
        components = scenario.numbered_components
        if not components:
            raise ValueError(f"场景 {case_id} 没有可校验的编号组件")
        component_numbers = [
            int(component["component_number"]) for component in components
        ]
        expected_numbers = list(range(1, len(components) + 1))
        if component_numbers != expected_numbers:
            raise ValueError(
                f"场景 {case_id} 的组件编号必须从1开始连续；"
                f"实际为 {component_numbers}"
            )
        if any(
            not str(component.get("source_text") or "").strip()
            for component in components
        ):
            raise ValueError(f"场景 {case_id} 存在空的编号组件")
        scenarios.append(scenario)
        seen_ids.add(case_id)
    return sorted(scenarios, key=lambda item: item.case_id)


def parse_case_selection(value: str, available_ids: set[int]) -> list[int]:
    if value.strip().casefold() in {"", "all", "*"}:
        return sorted(available_ids)
    selected: list[int] = []
    for raw_token in value.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if "-" in token:
            parts = token.split("-", 1)
            if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
                raise ValueError(f"无效编号范围：{token}")
            start, end = (int(part.strip()) for part in parts)
            if start > end:
                raise ValueError(f"编号范围起点大于终点：{token}")
            candidates = range(start, end + 1)
        elif token.isdigit():
            candidates = [int(token)]
        else:
            raise ValueError(f"无效场景编号：{token}")
        for case_id in candidates:
            if case_id not in available_ids:
                raise ValueError(f"场景编号不存在：{case_id}")
            if case_id not in selected:
                selected.append(case_id)
    if not selected:
        raise ValueError("至少选择一个场景")
    return selected


def response_payload(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return {"raw_text": response.text[:8000]}


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float,
) -> dict[str, Any]:
    try:
        response = client.request(method, path, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        raise ApiRequestError(
            method,
            path,
            None,
            {"error_type": type(exc).__name__},
            f"{method} {path} 请求失败：{exc}",
        ) from exc
    parsed = response_payload(response)
    if not response.is_success:
        message = "API 请求失败"
        if isinstance(parsed, dict):
            message = str(parsed.get("message") or parsed.get("detail") or message)
        raise ApiRequestError(method, path, response.status_code, parsed, message)
    if not isinstance(parsed, dict):
        raise ApiRequestError(
            method,
            path,
            response.status_code,
            parsed,
            "API 成功响应不是 JSON 对象",
        )
    return parsed


def collect_diagnostic_ids(value: object) -> list[str]:
    found: list[str] = []

    def append(candidate: object) -> None:
        if isinstance(candidate, str) and candidate.strip() and candidate not in found:
            found.append(candidate.strip())
        elif isinstance(candidate, list):
            for item in candidate:
                append(item)

    def visit(current: object) -> None:
        if isinstance(current, dict):
            for key, nested in current.items():
                normalized = str(key).casefold().replace("-", "_")
                if normalized in {"diagnostic_id", "diagnostic_ids"}:
                    append(nested)
                visit(nested)
        elif isinstance(current, list):
            for nested in current:
                visit(nested)

    visit(value)
    return found


# This is a regression-report vocabulary, not a pricing route table.  It only
# compares the customer-visible product family in a numbered heading with the
# returned preview identity. Unknown products remain explicitly "unverified"
# instead of being guessed or rejected.
IDENTITY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("memorydb", re.compile(r"memory\s*db", re.I)),
    ("firehose", re.compile(r"firehose", re.I)),
    ("mediaconvert", re.compile(r"media\s*convert", re.I)),
    ("transfer_family", re.compile(r"transfer\s*family", re.I)),
    ("step_functions", re.compile(r"step[_\s-]*functions?", re.I)),
    ("apigateway", re.compile(r"api[_\s-]*gateway|api\s*网关", re.I)),
    ("nat_gateway", re.compile(r"nat[_\s-]*gateway|nat\s*网关", re.I)),
    (
        "elb",
        re.compile(
            r"elastic[_\s-]*load[_\s-]*balanc|application[_\s-]*load[_\s-]*balanc|"
            r"network[_\s-]*load[_\s-]*balanc|(?<![a-z0-9])(?:alb|nlb|elb)(?![a-z0-9])|负载均衡",
            re.I,
        ),
    ),
    ("cloudfront", re.compile(r"cloud\s*front|(?<![a-z0-9])cdn(?![a-z0-9])", re.I)),
    ("cloudwatch", re.compile(r"cloud\s*watch|日志监控", re.I)),
    (
        "ebs",
        re.compile(
            r"elastic[_\s-]*block[_\s-]*store|(?<![a-z0-9])ebs(?![a-z0-9])|云硬盘",
            re.I,
        ),
    ),
    ("route53", re.compile(r"route\s*53|route53", re.I)),
    ("opensearch", re.compile(r"open\s*search|elasticsearch", re.I)),
    (
        "documentdb",
        re.compile(r"document\s*db|(?<![a-z0-9])docdb(?![a-z0-9])|mongodb", re.I),
    ),
    ("elasticache", re.compile(r"elasti\s*cache|(?<![a-z0-9])redis(?![a-z0-9])", re.I)),
    ("aurora_rds", re.compile(r"aurora|amazon\s*rds|(?<![a-z0-9])rds(?![a-z0-9])", re.I)),
    ("eks", re.compile(r"amazon\s*eks|(?<![a-z0-9])eks(?![a-z0-9])|kubernetes", re.I)),
    ("ecs", re.compile(r"amazon\s*ecs|(?<![a-z0-9])ecs(?![a-z0-9])|fargate", re.I)),
    (
        "ec2",
        re.compile(
            r"amazon\s*ec2|elastic[_\s-]*compute[_\s-]*cloud|"
            r"(?<![a-z0-9])ec2(?![a-z0-9])",
            re.I,
        ),
    ),
    ("msk", re.compile(r"amazon\s*msk|(?<![a-z0-9])msk(?![a-z0-9])|kafka", re.I)),
    (
        "s3",
        re.compile(
            r"amazon\s*s3|(?<![a-z0-9])s3(?![a-z0-9])|"
            r"(?<![a-z0-9])glacier(?![a-z0-9])",
            re.I,
        ),
    ),
    ("waf", re.compile(r"aws\s*waf|(?<![a-z0-9])waf(?![a-z0-9])|应用防火墙", re.I)),
    ("backup", re.compile(r"aws\s*backup|备份服务", re.I)),
    ("lambda", re.compile(r"(?<![a-z0-9])lambda(?![a-z0-9])", re.I)),
    ("dynamodb", re.compile(r"dynamo\s*db", re.I)),
    ("sqs", re.compile(r"(?<![a-z0-9])sqs(?![a-z0-9])", re.I)),
    ("cognito", re.compile(r"cognito", re.I)),
    ("kms", re.compile(r"key\s*management|(?<![a-z0-9])kms(?![a-z0-9])", re.I)),
    (
        "iot",
        re.compile(
            r"io[_\s-]*t(?:[_\s-]*(?:core|device[_\s-]*(?:management|defender)))?",
            re.I,
        ),
    ),
    ("kinesis", re.compile(r"kinesis", re.I)),
    ("timestream", re.compile(r"timestream", re.I)),
    ("mq", re.compile(r"amazon\s*mq|rabbit\s*mq|active\s*mq", re.I)),
    ("emr", re.compile(r"amazon\s*emr|(?<![a-z0-9])emr(?![a-z0-9])", re.I)),
    ("glue", re.compile(r"aws\s*glue|(?<![a-z0-9])glue(?![a-z0-9])", re.I)),
    ("athena", re.compile(r"athena", re.I)),
    ("redshift", re.compile(r"redshift", re.I)),
    ("ecr", re.compile(r"amazon\s*ecr|(?<![a-z0-9])ecr(?![a-z0-9])", re.I)),
    ("fsx", re.compile(r"(?<![a-z0-9])fsx(?![a-z0-9])", re.I)),
    ("dms", re.compile(r"database\s*migration|(?<![a-z0-9])dms(?![a-z0-9])", re.I)),
    ("efs", re.compile(r"amazon\s*efs|(?<![a-z0-9])efs(?![a-z0-9])", re.I)),
    ("vpn", re.compile(r"site[_\s-]*to[_\s-]*site\s*vpn|(?<![a-z0-9])vpn(?![a-z0-9])", re.I)),
)


def component_identity(*values: object) -> str | None:
    evidence = " ".join(str(value or "") for value in values).casefold()
    for identity, pattern in IDENTITY_RULES:
        if pattern.search(evidence):
            return identity
    return None


def duplicate_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def validate_preview_semantics(
    scenario: Scenario,
    result_summary: dict[str, Any],
) -> dict[str, Any]:
    """Check that transport success preserved numbered component ownership."""

    expected = scenario.numbered_components
    selections = result_summary.get("selections")
    selections = selections if isinstance(selections, list) else []
    issues: list[dict[str, Any]] = []

    def issue(code: str, message: str, **context: object) -> None:
        issues.append({"code": code, "message": message, **context})

    component_ids = [
        str(item.get("component_id") or "").strip()
        for item in selections
        if isinstance(item, dict)
    ]
    component_numbers = [
        str(item.get("component_number") or "").strip()
        for item in selections
        if isinstance(item, dict)
    ]
    missing_ids = [index + 1 for index, value in enumerate(component_ids) if not value]
    missing_numbers = [
        index + 1 for index, value in enumerate(component_numbers) if not value
    ]
    duplicate_ids = duplicate_values([value for value in component_ids if value])
    duplicate_numbers = duplicate_values(
        [value for value in component_numbers if value]
    )
    if missing_ids:
        issue("missing_component_id", "返回组件缺少 component_id", positions=missing_ids)
    if missing_numbers:
        issue(
            "missing_component_number",
            "返回组件缺少 component_number",
            positions=missing_numbers,
        )
    if duplicate_ids:
        issue(
            "duplicate_component_id",
            "返回结果含重复 component_id",
            values=duplicate_ids,
        )
    if duplicate_numbers:
        issue(
            "duplicate_component_number",
            "返回结果含重复 component_number",
            values=duplicate_numbers,
        )

    all_ids = {value for value in component_ids if value}
    roots: list[dict[str, Any]] = []
    orphan_children: list[dict[str, Any]] = []
    for selection in selections:
        if not isinstance(selection, dict):
            continue
        parent_id = str(selection.get("parent_component_id") or "").strip()
        if not parent_id:
            roots.append(selection)
            continue
        if parent_id not in all_ids:
            orphan_children.append(
                {
                    "component_id": selection.get("component_id"),
                    "component_number": selection.get("component_number"),
                    "parent_component_id": parent_id,
                }
            )
            continue
        parent = next(
            (
                candidate
                for candidate in selections
                if isinstance(candidate, dict)
                and str(candidate.get("component_id") or "").strip() == parent_id
            ),
            None,
        )
        declared_parent_number = str(
            selection.get("parent_component_number") or ""
        ).strip()
        actual_parent_number = (
            str(parent.get("component_number") or "").strip()
            if isinstance(parent, dict)
            else ""
        )
        if declared_parent_number and declared_parent_number != actual_parent_number:
            orphan_children.append(
                {
                    "component_id": selection.get("component_id"),
                    "component_number": selection.get("component_number"),
                    "parent_component_id": parent_id,
                    "declared_parent_number": declared_parent_number,
                    "actual_parent_number": actual_parent_number,
                }
            )
    if orphan_children:
        issue(
            "orphan_component",
            "派生组件找不到一致的父组件",
            components=orphan_children,
        )

    technical_components = [
        {
            "component_id": item.get("component_id"),
            "component_number": item.get("component_number"),
            "service": item.get("service"),
            "display_name": item.get("display_name"),
            "status": item.get("status"),
            "next_action": item.get("next_action"),
            "issue_code": item.get("issue_code"),
        }
        for item in selections
        if str(item.get("status") or "").casefold()
        in {"technical_issue", "failed", "error"}
        or str(item.get("next_action") or "").casefold() == "internal_block"
    ]
    if technical_components:
        issue(
            "component_technical_failure",
            "至少一个组件仍有内部错误，预览不能算通过",
            components=technical_components,
        )

    duplicate_parent_identity: list[dict[str, Any]] = []
    for child in [item for item in selections if item not in roots]:
        parent_id = str(child.get("parent_component_id") or "").strip()
        parent = next(
            (
                candidate
                for candidate in selections
                if str(candidate.get("component_id") or "").strip() == parent_id
            ),
            None,
        )
        if not isinstance(parent, dict):
            continue
        # Structured service identity is authoritative.  Display names may
        # intentionally mention the parent (for example "EC2 (EKS Worker
        # Nodes)") and must not relabel a correctly split child as EKS.
        child_identity = component_identity(child.get("service")) or component_identity(
            child.get("display_name")
        )
        parent_identity = component_identity(parent.get("service")) or component_identity(
            parent.get("display_name")
        )
        if child_identity is not None and child_identity == parent_identity:
            duplicate_parent_identity.append(
                {
                    "component_id": child.get("component_id"),
                    "component_number": child.get("component_number"),
                    "identity": child_identity,
                    "parent_component_id": parent_id,
                    "parent_component_number": parent.get("component_number"),
                }
            )
    if duplicate_parent_identity:
        issue(
            "derived_component_identity_matches_parent",
            "派生组件被错误识别成了父组件的第二份副本",
            components=duplicate_parent_identity,
        )

    expected_numbers = [str(item["component_number"]) for item in expected]
    actual_root_numbers = [
        str(item.get("component_number") or "").strip() for item in roots
    ]
    if len(roots) != len(expected):
        issue(
            "root_component_count_mismatch",
            "返回的根组件数量与输入编号组件数不一致",
            expected=len(expected),
            actual=len(roots),
        )
    if actual_root_numbers != expected_numbers:
        issue(
            "root_component_order_mismatch",
            "返回根组件未保持客户编号顺序",
            expected=expected_numbers,
            actual=actual_root_numbers,
        )

    roots_by_number = {
        str(item.get("component_number") or "").strip(): item for item in roots
    }
    identity_comparisons: list[dict[str, Any]] = []
    for expected_component in expected:
        number = str(expected_component["component_number"])
        expected_heading = str(expected_component.get("heading") or "")
        expected_identity = component_identity(expected_heading)
        actual = roots_by_number.get(number)
        if actual is None:
            identity_comparisons.append(
                {
                    "component_number": number,
                    "expected_heading": expected_heading,
                    "expected_identity": expected_identity,
                    "actual_identity": None,
                    "status": "missing",
                }
            )
            continue
        actual_identity = component_identity(actual.get("service")) or component_identity(
            actual.get("display_name")
        )
        source_identity = component_identity(actual.get("source_text"))
        status = "matched"
        if expected_identity is None:
            status = "unverified"
        elif actual_identity != expected_identity:
            status = "mismatched"
            issue(
                "component_identity_mismatch",
                "返回组件身份与客户编号标题不一致",
                component_number=number,
                expected_heading=expected_heading,
                expected_identity=expected_identity,
                actual_service=actual.get("service"),
                actual_display_name=actual.get("display_name"),
                actual_identity=actual_identity,
            )
        if (
            expected_identity is not None
            and source_identity is not None
            and source_identity != expected_identity
        ):
            status = "source_mismatched"
            issue(
                "component_source_identity_mismatch",
                "返回组件携带的客户原文属于另一产品身份",
                component_number=number,
                expected_heading=expected_heading,
                expected_identity=expected_identity,
                actual_source_text=actual.get("source_text"),
                source_identity=source_identity,
            )
        elif (
            actual_identity is not None
            and source_identity is not None
            and source_identity != actual_identity
        ):
            status = "source_mismatched"
            issue(
                "component_source_identity_mismatch",
                "返回组件服务身份与其客户原文身份不一致",
                component_number=number,
                actual_identity=actual_identity,
                actual_source_text=actual.get("source_text"),
                source_identity=source_identity,
            )
        identity_comparisons.append(
            {
                "component_number": number,
                "expected_heading": expected_heading,
                "expected_identity": expected_identity,
                "actual_service": actual.get("service"),
                "actual_display_name": actual.get("display_name"),
                "actual_identity": actual_identity,
                "source_identity": source_identity,
                "status": status,
            }
        )

    checks = {
        "root_component_count": len(roots) == len(expected),
        "unique_component_ids": not missing_ids and not duplicate_ids,
        "unique_component_numbers": not missing_numbers and not duplicate_numbers,
        "no_orphan_children": not orphan_children,
        "no_component_technical_failures": not technical_components,
        "derived_identity_distinct_from_parent": not duplicate_parent_identity,
        "root_component_order": actual_root_numbers == expected_numbers,
        "root_component_identity": not any(
            item["code"]
            in {
                "component_identity_mismatch",
                "component_source_identity_mismatch",
            }
            for item in issues
        ),
    }
    return {
        "passed": not issues,
        "expected_root_component_count": len(expected),
        "actual_root_component_count": len(roots),
        "derived_component_count": len(selections) - len(roots),
        "checks": checks,
        "identity_comparisons": identity_comparisons,
        "issues": issues,
    }


def preview_result_summary(result: object) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    selections = result.get("selections")
    compact_selections: list[dict[str, Any]] = []
    if isinstance(selections, list):
        for selection in selections:
            if not isinstance(selection, dict):
                continue
            compact_selections.append(
                {
                    key: selection.get(key)
                    for key in (
                        "component_id",
                        "component_number",
                        "parent_component_id",
                        "parent_component_number",
                        "parent_display_name",
                        "service",
                        "display_name",
                        "region",
                        "quantity",
                        "source_text",
                        "status",
                        "next_action",
                        "requires_confirmation",
                        "issue_code",
                    )
                }
            )
    confirmation_items = result.get("confirmation_items")
    return {
        "draft_id": result.get("draft_id"),
        "selection_count": len(compact_selections),
        "selections": compact_selections,
        "configuration_review_required": bool(result.get("configuration_review_required")),
        "sales_validation_required": bool(result.get("sales_validation_required")),
        "confirmation_item_count": (
            len(confirmation_items) if isinstance(confirmation_items, list) else 0
        ),
        "expert_review": result.get("expert_review"),
    }


def api_failure(stage: str, exc: ApiRequestError) -> dict[str, Any]:
    return {
        "stage": stage,
        "code": "http_request_failed",
        "message": str(exc),
        "method": exc.method,
        "path": exc.path,
        "http_status": exc.status_code,
        "payload": exc.payload,
    }


def finish_case(record: dict[str, Any], started: float) -> dict[str, Any]:
    record["finished_at"] = utc_now()
    record["duration_seconds"] = round(time.monotonic() - started, 3)
    record["diagnostic_ids"] = collect_diagnostic_ids(record)
    return record


def cancel_preview_job(
    client: httpx.Client,
    job_id: str,
    *,
    request_timeout: float,
) -> dict[str, Any]:
    try:
        return request_json(
            client,
            "POST",
            JOB_CANCEL_PATH.format(job_id=job_id),
            timeout=request_timeout,
        )
    except ApiRequestError as exc:
        return {"cancelled": False, "cancel_error": api_failure("preview_cancel", exc)}


def run_scenario(
    client: httpx.Client,
    scenario: Scenario,
    *,
    case_timeout: float,
    request_timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    started = time.monotonic()
    deadline = started + case_timeout
    record: dict[str, Any] = {
        "case_id": scenario.case_id,
        "name": scenario.name,
        "expected_region": scenario.expected_region,
        "expected_root_component_count": scenario.component_count,
        "expected_components": [
            {
                "component_number": item["component_number"],
                "heading": item["heading"],
                "identity": component_identity(item["heading"]),
            }
            for item in scenario.numbered_components
        ],
        "started_at": utc_now(),
        "outcome": "failed",
        "stage": "region_preflight",
        "transport_status": "not_started",
    }
    print(
        f"[{scenario.case_id}] {scenario.name}: 地区预检（期望 {scenario.expected_region}）",
        flush=True,
    )

    try:
        preflight = request_json(
            client,
            "POST",
            REGION_PREFLIGHT_PATH,
            payload={"customer_request": scenario.customer_request},
            timeout=min(request_timeout, max(deadline - time.monotonic(), 0.1)),
        )
    except ApiRequestError as exc:
        record["failure"] = api_failure("region_preflight", exc)
        return finish_case(record, started)

    record["region_preflight"] = preflight
    detected_regions = [
        str(region) for region in preflight.get("detected_regions", []) if isinstance(region, str)
    ]
    selected_region = preflight.get("selected_region")
    if not isinstance(selected_region, str) and len(detected_regions) == 1:
        selected_region = detected_regions[0]
    record["detected_regions"] = detected_regions
    record["selected_region"] = selected_region

    if bool(preflight.get("requires_confirmation")) or not isinstance(selected_region, str):
        record["failure"] = {
            "stage": "region_preflight",
            "code": "region_confirmation_required",
            "message": "地区预检未得到唯一可用的整单默认地区",
            "payload": preflight,
        }
        return finish_case(record, started)
    if selected_region != scenario.expected_region:
        record["failure"] = {
            "stage": "region_preflight",
            "code": "region_mismatch",
            "message": (f"地区识别为 {selected_region}，但场景期望 {scenario.expected_region}"),
            "payload": preflight,
        }
        return finish_case(record, started)

    print(
        f"[{scenario.case_id}] {scenario.name}: 启动预览任务（顺序执行）",
        flush=True,
    )
    record["stage"] = "preview_start"
    try:
        started_job = request_json(
            client,
            "POST",
            PREVIEW_START_PATH,
            payload={
                "cloud_provider": "aws",
                "customer_request": scenario.customer_request,
                "sales_region": selected_region,
                "pricing_mode": "on_demand",
                "include_on_demand_scenario": True,
            },
            timeout=min(request_timeout, max(deadline - time.monotonic(), 0.1)),
        )
    except ApiRequestError as exc:
        record["failure"] = api_failure("preview_start", exc)
        return finish_case(record, started)

    job_id = started_job.get("job_id")
    if not isinstance(job_id, str) or not job_id.startswith("aws-"):
        record["failure"] = {
            "stage": "preview_start",
            "code": "invalid_preview_job_id",
            "message": "预览任务未返回 AWS job_id",
            "payload": started_job,
        }
        return finish_case(record, started)
    record["job_id"] = job_id
    record["preview_start"] = started_job
    record["stage"] = "preview_poll"
    last_job: dict[str, Any] = {}
    reported_event_count = 0

    while time.monotonic() < deadline:
        remaining = max(deadline - time.monotonic(), 0.1)
        try:
            last_job = request_json(
                client,
                "GET",
                JOB_STATUS_PATH.format(job_id=job_id),
                timeout=min(request_timeout, remaining),
            )
        except ApiRequestError as exc:
            record["preview_job"] = last_job
            record["failure"] = api_failure("preview_poll", exc)
            return finish_case(record, started)

        events = last_job.get("events")
        if isinstance(events, list) and len(events) > reported_event_count:
            for event in events[reported_event_count:]:
                if isinstance(event, dict):
                    print(
                        f"  {event.get('time', '--:--:--')} "
                        f"{event.get('stage', 'event')}: {event.get('message', '')}",
                        flush=True,
                    )
            reported_event_count = len(events)

        status = str(last_job.get("status") or "")
        record["transport_status"] = status or "unknown"
        if status in TERMINAL_JOB_STATUSES:
            result_summary = preview_result_summary(last_job.get("result"))
            record["preview_job"] = {
                "job_id": job_id,
                "status": status,
                "updated_at": last_job.get("updated_at"),
                "events": events if isinstance(events, list) else [],
                "error": last_job.get("error"),
                "result_summary": result_summary,
            }
            if status == "completed":
                semantic_validation = validate_preview_semantics(
                    scenario,
                    result_summary,
                )
                record["semantic_validation"] = semantic_validation
                if semantic_validation["passed"]:
                    record["outcome"] = "passed"
                    record["stage"] = "completed"
                else:
                    record["stage"] = "semantic_validation"
                    record["failure"] = {
                        "stage": "semantic_validation",
                        "code": "preview_semantic_mismatch",
                        "message": (
                            "预览任务虽已完成，但返回组件未保持输入编号清单的"
                            "数量、顺序、归属或产品身份"
                        ),
                        "payload": semantic_validation,
                    }
            else:
                error = last_job.get("error")
                record["stage"] = "preview_failed"
                record["failure"] = {
                    "stage": "preview_job",
                    "code": (
                        error.get("code", "preview_job_failed")
                        if isinstance(error, dict)
                        else "preview_job_failed"
                    ),
                    "message": (
                        error.get("message", "预览任务失败")
                        if isinstance(error, dict)
                        else "预览任务失败"
                    ),
                    "payload": error,
                }
            return finish_case(record, started)
        time.sleep(min(poll_interval, max(deadline - time.monotonic(), 0)))

    cancellation = cancel_preview_job(
        client,
        job_id,
        request_timeout=request_timeout,
    )
    record["outcome"] = "timed_out"
    record["stage"] = "preview_timeout"
    record["preview_job"] = last_job
    record["cancellation"] = cancellation
    record["failure"] = {
        "stage": "preview_poll",
        "code": "preview_timeout",
        "message": f"预览任务超过 {case_timeout:g} 秒未完成",
        "payload": last_job.get("error") if isinstance(last_job, dict) else None,
    }
    return finish_case(record, started)


def markdown_escape(value: object) -> str:
    return str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any], *, event_limit: int) -> str:
    results = report.get("results", [])
    passed = sum(
        1 for result in results if isinstance(result, dict) and result.get("outcome") == "passed"
    )
    failed = len(results) - passed
    lines = [
        "# AWS 区域方案预览回归报告",
        "",
        f"- 运行 ID：`{report.get('run_id')}`",
        f"- 开始时间：`{report.get('started_at')}`",
        f"- 结束时间：`{report.get('finished_at')}`",
        f"- API：`{report.get('base_url')}`",
        f"- 结果：{passed} 通过 / {failed} 失败 / {len(results)} 总数",
        "- 范围：仅地区预检和配置预览，未启动正式报价。",
        "",
        "| 编号 | 方案 | 期望地区 | 识别地区 | 传输 | 根组件(返回/输入) | "
        "语义 | 结果 | 耗时(s) | Job ID | 错误码 | 诊断 ID |",
        "|---:|---|---|---|---|---|---|---|---:|---|---|---|",
    ]
    for result in results:
        if not isinstance(result, dict):
            continue
        failure = result.get("failure")
        failure_code = failure.get("code") if isinstance(failure, dict) else "-"
        diagnostic_ids = result.get("diagnostic_ids") or []
        outcome = "通过" if result.get("outcome") == "passed" else str(result.get("outcome"))
        semantic = result.get("semantic_validation")
        semantic = semantic if isinstance(semantic, dict) else {}
        component_count = (
            f"{semantic.get('actual_root_component_count', '-')}/"
            f"{result.get('expected_root_component_count', '-')}"
        )
        semantic_status = (
            "通过"
            if semantic.get("passed") is True
            else "失败"
            if semantic.get("passed") is False
            else "未运行"
        )
        lines.append(
            "| "
            + " | ".join(
                markdown_escape(value)
                for value in (
                    result.get("case_id"),
                    result.get("name"),
                    result.get("expected_region"),
                    result.get("selected_region"),
                    result.get("transport_status"),
                    component_count,
                    semantic_status,
                    outcome,
                    result.get("duration_seconds"),
                    result.get("job_id"),
                    failure_code,
                    ", ".join(diagnostic_ids) if diagnostic_ids else "-",
                )
            )
            + " |"
        )

    failed_results = [
        result
        for result in results
        if isinstance(result, dict) and result.get("outcome") != "passed"
    ]
    if failed_results:
        lines.extend(["", "## 失败详情", ""])
    for result in failed_results:
        failure = result.get("failure")
        failure = failure if isinstance(failure, dict) else {}
        lines.extend(
            [
                f"### 方案 {result.get('case_id')}｜{result.get('name')}",
                "",
                f"- 失败阶段：`{failure.get('stage') or result.get('stage')}`",
                f"- 错误码：`{failure.get('code', 'unknown')}`",
                f"- 错误信息：{failure.get('message', '-')}",
                f"- 诊断 ID：{', '.join(result.get('diagnostic_ids') or []) or '-'}",
                "",
            ]
        )
        semantic = result.get("semantic_validation")
        semantic_issues = semantic.get("issues", []) if isinstance(semantic, dict) else []
        if isinstance(semantic_issues, list) and semantic_issues:
            lines.extend(["语义校验问题：", ""])
            for semantic_issue in semantic_issues:
                if not isinstance(semantic_issue, dict):
                    continue
                lines.append(
                    f"- `{semantic_issue.get('code', 'unknown')}`："
                    f"{semantic_issue.get('message', '-')}"
                )
            lines.append("")
        preview_job = result.get("preview_job")
        events = preview_job.get("events", []) if isinstance(preview_job, dict) else []
        if isinstance(events, list) and events:
            selected_events = events[-event_limit:] if event_limit > 0 else events
            lines.extend([f"最后 {len(selected_events)} 条任务事件：", "", "```text"])
            for event in selected_events:
                if isinstance(event, dict):
                    lines.append(
                        f"{event.get('time', '--:--:--')} "
                        f"[{event.get('stage', 'event')}] {event.get('message', '')}"
                    )
            lines.extend(["```", ""])
    return "\n".join(lines).rstrip() + "\n"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("顺序运行 AWS 场景文件的地区预检与配置预览，不启动正式报价。"),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  运行全部：  python scripts/aws_regional_preview_runner.py\n"
            "  重跑 2/5/8：python scripts/aws_regional_preview_runner.py --cases 2,5,8\n"
            "  重跑范围：python scripts/aws_regional_preview_runner.py --cases 3-6\n"
            "  自定义场景：python scripts/aws_regional_preview_runner.py "
            "--scenario-file scripts/aws_generated_preview_scenarios.json\n"
            "  只验证输入：python scripts/aws_regional_preview_runner.py --validate-only"
        ),
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="AWS 后端地址（默认：%(default)s）",
    )
    parser.add_argument(
        "--trust-env",
        action="store_true",
        help="允许 httpx 读取 HTTP_PROXY/NO_PROXY；访问本地后端时默认关闭",
    )
    parser.add_argument(
        "--scenario-file",
        type=Path,
        default=DEFAULT_SCENARIO_FILE,
        help="场景 JSON 文件（默认：%(default)s）",
    )
    parser.add_argument(
        "--cases",
        default="all",
        help="按编号选择顺序重跑，如 2,5,8 或 3-6（默认：all）",
    )
    parser.add_argument(
        "--case-timeout",
        type=float,
        default=420.0,
        help="每个场景总超时秒数（默认：%(default)s）",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=45.0,
        help="单次 HTTP 请求超时秒数（默认：%(default)s）",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="预览任务轮询间隔秒数（默认：%(default)s）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="JSON/Markdown 报告目录（默认：%(default)s）",
    )
    parser.add_argument(
        "--markdown-event-limit",
        type=int,
        default=30,
        help="Markdown 中每个失败场景保留的最后事件数（默认：%(default)s）",
    )
    parser.add_argument(
        "--stop-on-failure",
        action="store_true",
        help="首个失败后停止；默认继续顺序运行后续场景",
    )
    parser.add_argument(
        "--list-cases",
        action="store_true",
        help="列出场景编号并退出，不请求 API",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="只验证场景文件和编号选择，不请求 API",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        scenarios = load_scenarios(args.scenario_file.resolve())
        selected_ids = parse_case_selection(
            args.cases,
            {scenario.case_id for scenario in scenarios},
        )
    except ValueError as exc:
        parser.error(str(exc))

    if args.case_timeout <= 0:
        parser.error("--case-timeout 必须大于 0")
    if args.request_timeout <= 0:
        parser.error("--request-timeout 必须大于 0")
    if args.poll_interval < 0.1:
        parser.error("--poll-interval 不能小于 0.1")
    if args.markdown_event_limit < 0:
        parser.error("--markdown-event-limit 不能小于 0")

    scenarios_by_id = {scenario.case_id: scenario for scenario in scenarios}
    selected = [scenarios_by_id[case_id] for case_id in selected_ids]
    if args.list_cases:
        for scenario in scenarios:
            print(
                f"{scenario.case_id:>2}  {scenario.name:<12}  "
                f"{scenario.expected_region}  {scenario.component_count} 个组件"
            )
        return 0
    if args.validate_only:
        print(
            f"场景文件有效：{len(scenarios)} 个场景；"
            f"已选择：{','.join(str(item.case_id) for item in selected)}"
        )
        return 0

    run_started = utc_now()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    results: list[dict[str, Any]] = []
    base_url = args.base_url.rstrip("/")
    print(
        f"开始顺序预览回归：{len(selected)} 个场景，API={base_url}",
        flush=True,
    )
    with httpx.Client(
        base_url=base_url,
        follow_redirects=True,
        trust_env=args.trust_env,
    ) as client:
        for scenario in selected:
            result = run_scenario(
                client,
                scenario,
                case_timeout=args.case_timeout,
                request_timeout=args.request_timeout,
                poll_interval=args.poll_interval,
            )
            results.append(result)
            print(
                f"[{scenario.case_id}] {scenario.name}: "
                f"{result.get('outcome')} ({result.get('duration_seconds')}s)",
                flush=True,
            )
            if args.stop_on_failure and result.get("outcome") != "passed":
                break

    report = {
        "schema_version": 2,
        "application": "AstraQuote",
        "kind": "aws_regional_preview_regression",
        "run_id": run_id,
        "started_at": run_started,
        "finished_at": utc_now(),
        "base_url": base_url,
        "scenario_file": str(args.scenario_file.resolve()),
        "selected_case_ids": selected_ids,
        "configuration": {
            "case_timeout_seconds": args.case_timeout,
            "request_timeout_seconds": args.request_timeout,
            "poll_interval_seconds": args.poll_interval,
            "stop_on_failure": args.stop_on_failure,
            "trust_environment_proxy": args.trust_env,
            "formal_quote_started": False,
        },
        "summary": {
            "total": len(results),
            "passed": sum(1 for item in results if item.get("outcome") == "passed"),
            "failed": sum(1 for item in results if item.get("outcome") == "failed"),
            "timed_out": sum(1 for item in results if item.get("outcome") == "timed_out"),
            "transport_completed": sum(
                1 for item in results if item.get("transport_status") == "completed"
            ),
            "semantic_failed": sum(
                1
                for item in results
                if isinstance(item.get("semantic_validation"), dict)
                and item["semantic_validation"].get("passed") is False
            ),
            "diagnostic_ids": collect_diagnostic_ids(results),
        },
        "results": results,
    }
    output_stem = f"aws-regional-preview-{run_id}"
    json_path = args.output_dir.resolve() / f"{output_stem}.json"
    markdown_path = args.output_dir.resolve() / f"{output_stem}.md"
    atomic_write(json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    atomic_write(
        markdown_path,
        render_markdown(report, event_limit=args.markdown_event_limit),
    )
    print(f"JSON 报告：{json_path}")
    print(f"Markdown 报告：{markdown_path}")
    return 0 if report["summary"]["passed"] == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
