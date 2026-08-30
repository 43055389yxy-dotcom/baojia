from __future__ import annotations

import re
from typing import Literal

from app.domain.models import ServiceRequirement

MatchPolicy = Literal["exact", "approximate", "minimum"]
FieldScope = Literal["component_total", "aggregate", "per_resource", "per_node"]

CUSTOMER_FACT_SOURCES = {
    "customer_text",
    "customer_confirmation",
    "customer_correction",
    "sales_confirmation",
}


# Python's ``\b`` uses Unicode word semantics, so it does not create a word
# boundary between Chinese text and an ASCII instance type.  Customer phrases
# such as ``实例类型m6i.2xlarge，Ubuntu`` were therefore invisible to the old
# literal guard.  These ASCII-only boundaries are shared by every extraction
# path so an explicitly written product model can never be lost merely because
# it touches Chinese punctuation or a Chinese label.
ASCII_MODEL_START = r"(?<![a-z0-9.])"
ASCII_MODEL_END = r"(?![a-z0-9.-])"
EC2_MODEL_PATTERN = re.compile(
    ASCII_MODEL_START
    + r"((?!db\.|cache\.|kafka\.|mq\.|dms\.|ml\.)(?=[a-z0-9-]*\d)"
    r"[a-z][a-z0-9-]*\.(?:nano|micro|small|medium|large|xlarge|metal|\d+xlarge))"
    + ASCII_MODEL_END,
    re.IGNORECASE,
)


def explicit_requested_model(service: str, source: str) -> tuple[str, str] | None:
    """Return a model literally owned by one isolated customer component.

    This function performs no recommendation or inference. It only recognizes
    an AWS-shaped identifier that the customer actually wrote in this
    component, making it suitable for both fresh AI output and persisted draft
    repair.
    """

    key = re.sub(r"[^a-z0-9]", "", str(service or "").casefold())
    aliases = {
        "amazonelasticcomputec2": "ec2",
        "amazonrds": "rds",
        "redis": "elasticache",
        "amazonelasticache": "elasticache",
        "amazonmsk": "msk",
        "amazonopensearchservice": "opensearch",
        "amazonsagemaker": "sagemaker",
        "amazonmq": "mq",
        "awsdatabasemigrationservicedms": "dms",
    }
    key = aliases.get(key, key)
    patterns: dict[str, re.Pattern[str]] = {
        "ec2": EC2_MODEL_PATTERN,
        "rds": re.compile(
            ASCII_MODEL_START + r"(db\.[a-z0-9][a-z0-9.-]*)" + ASCII_MODEL_END,
            re.IGNORECASE,
        ),
        "elasticache": re.compile(
            ASCII_MODEL_START + r"(cache\.[a-z0-9][a-z0-9.-]*)" + ASCII_MODEL_END,
            re.IGNORECASE,
        ),
        "msk": re.compile(
            ASCII_MODEL_START
            + r"((?:kafka\.)?(?:m|t|r)\d+[a-z]*\.[a-z0-9]+)"
            + ASCII_MODEL_END,
            re.IGNORECASE,
        ),
        "opensearch": re.compile(
            ASCII_MODEL_START + r"([a-z0-9][a-z0-9.-]*\.search)" + ASCII_MODEL_END,
            re.IGNORECASE,
        ),
        "sagemaker": re.compile(
            ASCII_MODEL_START + r"(ml\.[a-z0-9][a-z0-9.-]*)" + ASCII_MODEL_END,
            re.IGNORECASE,
        ),
        "mq": re.compile(
            ASCII_MODEL_START + r"(mq\.[a-z0-9][a-z0-9.-]*)" + ASCII_MODEL_END,
            re.IGNORECASE,
        ),
        "dms": re.compile(
            ASCII_MODEL_START + r"(dms\.[a-z0-9][a-z0-9.-]*)" + ASCII_MODEL_END,
            re.IGNORECASE,
        ),
    }
    pattern = patterns.get(key)
    text = str(source or "")
    if pattern is None:
        # Automatically discovered official services do not have a handwritten
        # model prefix table.  A labelled literal such as
        # ``实例规格 db.r6g.large`` is nevertheless unambiguous customer data
        # and must survive the generic extraction path.
        pattern = re.compile(
            r"(?:实例(?:类型|规格)|型号|机型|sku)\s*[:：]?\s*"
            + ASCII_MODEL_START
            + r"([a-z][a-z0-9-]*(?:\.[a-z0-9-]+)+)"
            + ASCII_MODEL_END,
            re.IGNORECASE,
        )
    match = pattern.search(text)
    if not match:
        return None
    return match.group(1).lower().rstrip("。；;,.，"), match.group(0)


def infer_match_policy(evidence: str) -> MatchPolicy:
    """Interpret customer wording without making service-specific assumptions."""

    compact = re.sub(r"\s+", "", str(evidence or "")).casefold()
    if re.search(r"至少|不低于|不少于|minimum|atleast|>=|≥", compact, re.I):
        return "minimum"
    if re.search(r"大约|大概|约|左右|附近|approx|about|roughly", compact, re.I):
        return "approximate"
    return "exact"


def infer_field_scope(evidence: str) -> FieldScope:
    """Bind a quantity to the resource level explicitly stated by the customer."""

    compact = re.sub(r"\s+", "", str(evidence or "")).casefold()
    if re.search(r"总(?:计|共|量|数|调用|请求)?|合计|overall|total", compact, re.I):
        return "aggregate"
    if re.search(
        r"每(?:个)?(?:数据|工作|worker)?节点|单(?:个)?(?:数据|工作|worker)?节点|"
        r"每broker|单broker|per(?:node|broker)",
        compact,
        re.I,
    ):
        return "per_node"
    if re.search(
        r"(?:每|单)(?:个|台|套|函数|实例|资源)|per(?:resource|instance|function)",
        compact,
        re.I,
    ):
        return "per_resource"
    return "component_total"


def record_customer_fact_metadata(
    requirement: ServiceRequirement,
    field: str,
    evidence: str,
    *,
    policy: MatchPolicy | None = None,
    scope: FieldScope | None = None,
) -> None:
    """Persist pricing semantics beside a customer-owned requirement field."""

    requirement.field_match_policies[field] = policy or infer_match_policy(evidence)
    inferred_scope = scope or infer_field_scope(evidence)
    if scope is None and inferred_scope == "component_total":
        # Models often return the shortest evidence token (``磁盘200G``),
        # dropping the immediately preceding scope clause
        # (``单实例4核16G，磁盘200G``). Recover only that adjacent clause from
        # the immutable component source. This is grammar-level scope
        # preservation, not service vocabulary, so it works for any present
        # or future product without turning the whole sentence into a guess.
        source = str(
            requirement.original_source_text or requirement.source_text or ""
        )
        token = str(evidence or "").strip()
        position = source.casefold().find(token.casefold()) if token else -1
        if position >= 0:
            separators = "，,；;。\n"
            immediate_left = max(
                (source.rfind(separator, 0, position) for separator in separators),
                default=-1,
            )
            previous_left = (
                max(
                    (
                        source.rfind(separator, 0, immediate_left)
                        for separator in separators
                    ),
                    default=-1,
                )
                if immediate_left >= 0
                else -1
            )
            context = source[previous_left + 1 : position + len(token)]
            contextual_scope = infer_field_scope(context)
            if contextual_scope != "component_total":
                inferred_scope = contextual_scope
    requirement.field_scopes[field] = inferred_scope


def customer_match_policy(
    requirement: ServiceRequirement,
    field: str,
) -> MatchPolicy | None:
    """Return a constraint only for a field genuinely owned by the customer."""

    path = f"requirements.{field}"
    source = requirement.field_sources.get(path)
    if source not in CUSTOMER_FACT_SOURCES and path not in requirement.locked_fields:
        return None
    saved_policy = requirement.field_match_policies.get(field)
    # Component extractors sometimes return only the numeric token as field
    # evidence (``13GB``) while the immutable source says ``约13GB``.  The
    # source-owned clause is the authority for exact/approximate/minimum
    # semantics; otherwise a harmless official value such as 13.07 GiB becomes
    # a bogus exact-shape confirmation question.  Later customer edits remain
    # exact because their source type is handled above/below independently.
    if source == "customer_text":
        evidence = str(requirement.field_evidence.get(path) or "").strip()
        source_text = str(
            requirement.original_source_text or requirement.source_text or ""
        )
        if evidence and source_text:
            position = source_text.casefold().find(evidence.casefold())
            if position >= 0:
                left = max(
                    (source_text.rfind(separator, 0, position) for separator in "，,；;。\n"),
                    default=-1,
                )
                right_candidates = [
                    index
                    for separator in "，,；;。\n"
                    if (index := source_text.find(separator, position + len(evidence))) >= 0
                ]
                right = min(right_candidates) if right_candidates else len(source_text)
                contextual_policy = infer_match_policy(source_text[left + 1 : right])
                if contextual_policy in {"approximate", "minimum"}:
                    return contextual_policy
    if saved_policy in {
        "exact",
        "approximate",
        "minimum",
    }:
        return saved_policy
    if source in {"customer_confirmation", "customer_correction", "sales_confirmation"}:
        return "exact"
    return infer_match_policy(requirement.field_evidence.get(path, ""))


def field_scope(requirement: ServiceRequirement, field: str) -> FieldScope:
    if requirement.field_scopes.get(field) in {
        "component_total",
        "aggregate",
        "per_resource",
        "per_node",
    }:
        return requirement.field_scopes[field]
    return "component_total"


def scoped_amount(
    requirement: ServiceRequirement,
    field: str,
    value: float,
    *,
    resource_count: float | None = None,
) -> float:
    """Multiply only when the customer explicitly described a per-resource value."""

    if field_scope(requirement, field) != "per_resource":
        return value
    count = requirement.quantity if resource_count is None else resource_count
    return value * count
