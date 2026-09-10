from __future__ import annotations

from app.domain.fact_ledger import (
    OWNED_SOURCE_SLICE_EVIDENCE_FIELD,
    OWNED_SOURCE_SLICE_FIELD,
)
from app.domain.models import ParsedIntent

CLEANED_INPUT_POLICY_VERSION = "cleaned-only-v1"


def canonical_cleaned_request(intent: ParsedIntent) -> str:
    """Return the only prose representation allowed after initial cleaning."""

    return "\n".join(
        component.source_text.strip()
        for component in intent.services
        if component.source_text.strip()
    )


def intent_is_cleaned_only(intent: ParsedIntent) -> bool:
    return bool(intent.services) and all(
        component.original_source_text is None
        and not component.intake_source_fragments
        and component.field_sources.get("_source_retention_policy")
        == CLEANED_INPUT_POLICY_VERSION
        for component in intent.services
    )


def discard_original_input(intent: ParsedIntent) -> str:
    """Irreversibly replace raw customer prose with validated cleaned data.

    The caller must run the raw-input completeness and ownership checks first.
    After this boundary every evidence lookup resolves to the cleaned component
    sentence, and no raw fragment survives in a draft, cache or later AI call.
    """

    for component in intent.services:
        cleaned = component.source_text.strip()
        if not cleaned:
            raise ValueError("第一步清洗结果缺少标准化配置，不能删除原始输入")

        component.source_text = cleaned
        component.original_source_text = None
        component.intake_source_fragments = []
        component.customer_pricing_facts = []
        component.locked_fields = []
        component.query_action = None

        # First-pass facts are still candidates for the official component
        # template. Their raw snippets are not needed after losslessness has
        # been proved; the component pass recreates precise evidence against
        # this cleaned sentence.
        component.field_evidence = {
            OWNED_SOURCE_SLICE_EVIDENCE_FIELD: cleaned,
        }
        component.field_sources = {
            key: value
            for key, value in component.field_sources.items()
            if key.startswith("_")
            and key not in {"_semantic_fact_mapping", "_intake_pipeline_version"}
        }
        component.field_sources[OWNED_SOURCE_SLICE_FIELD] = "system_policy"
        component.field_sources["_source_retention_policy"] = CLEANED_INPUT_POLICY_VERSION

        # Preserve unknown price-changing values, but replace their raw quote
        # with the cleaned component sentence. The next fixed-schema pass can
        # map them or keep them as an explicit publication blocker.
        component.unmapped_pricing_facts = [
            fact.model_copy(update={"evidence": cleaned})
            for fact in component.unmapped_pricing_facts
        ]

    intent.customer_summary = f"已清洗 {len(intent.services)} 项配置。"
    if intent.ambiguities:
        intent.ambiguities = [
            f"清洗阶段发现第 {index} 项配置存在冲突，请在组件确认中核对。"
            for index, _ in enumerate(intent.ambiguities, start=1)
        ]
    return canonical_cleaned_request(intent)
