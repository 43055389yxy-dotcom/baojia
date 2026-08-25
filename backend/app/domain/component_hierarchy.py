from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.component_integrity import canonical_component_source
from app.domain.models import ServiceRequirement


@dataclass(frozen=True, slots=True)
class ComponentHierarchy:
    component_number: str
    parent_component_id: str | None = None
    parent_component_number: str | None = None
    parent_display_name: str | None = None


_RELATION_ONLY = re.compile(
    r"^(?:用于|基于|依赖|关联|连接|挂载|保护|提供给|承载|作为)", re.I
)


def component_hierarchy(
    services: list[ServiceRequirement],
) -> list[ComponentHierarchy]:
    """Build stable customer-facing numbers for derived billing components."""

    parent_by_child: dict[int, int] = {}
    key_to_index = {
        item.component_key: index
        for index, item in enumerate(services)
        if item.component_key
    }
    for index, item in enumerate(services):
        if item.parent_component_key in key_to_index:
            parent_index = key_to_index[item.parent_component_key]
            if parent_index != index:
                parent_by_child[index] = parent_index
                continue
        source = (item.source_text or "").strip()
        display = (item.calculator_service_name or "").casefold()
        explicit_parent_service = str(item.derived_from_service or "").strip().casefold()
        if explicit_parent_service:
            explicit_parent = next(
                (
                    parent_index
                    for parent_index, candidate in enumerate(services)
                    if parent_index != index
                    and candidate.service.casefold() == explicit_parent_service
                    and (
                        not source
                        or not (candidate.source_text or "").strip()
                        or source == (candidate.source_text or "").strip()
                        or source in (candidate.source_text or "")
                        or (candidate.source_text or "") in source
                        or canonical_component_source(source)
                        == canonical_component_source(candidate.source_text)
                    )
                ),
                None,
            )
            if explicit_parent is not None:
                parent_by_child[index] = explicit_parent
                continue

        # The legacy inference below is intentionally narrow.  Only generated
        # EC2 workers/self-hosted machines can be inferred from prose; all
        # other products need explicit lineage metadata so an ordinary nearby
        # component is never accidentally made a child.
        if item.service.casefold() != "ec2":
            continue
        relation_only = bool(source and _RELATION_ONLY.match(source))
        likely_worker = any(
            marker in display for marker in ("worker", "工作节点", "自建")
        )
        if not relation_only and not likely_worker:
            continue

        contained_parent = next(
            (
                parent_index
                for parent_index, candidate in enumerate(services)
                if parent_index != index
                and candidate.service.casefold() != "ec2"
                and bool((candidate.source_text or "").strip())
                and source
                and source in (candidate.source_text or "")
            ),
            None,
        )
        previous_index = index - 1
        previous_is_parent = previous_index >= 0 and services[
            previous_index
        ].service.casefold() in {"eks", "vpc"}
        nearby_parent = next(
            (
                candidate_index
                for candidate_index in range(index - 1, max(-1, index - 4), -1)
                if services[candidate_index].service.casefold() in {"eks", "vpc"}
            ),
            None,
        )
        if contained_parent is not None:
            parent_by_child[index] = contained_parent
        elif previous_is_parent:
            parent_by_child[index] = previous_index
        elif relation_only and nearby_parent is not None:
            parent_by_child[index] = nearby_parent

    root_numbers: dict[int, str] = {}
    child_counts: dict[int, int] = {}
    next_root = 0
    result: list[ComponentHierarchy] = []
    for index in range(len(services)):
        parent_index = parent_by_child.get(index)
        if parent_index is None:
            next_root += 1
            root_numbers[index] = str(next_root)
            result.append(ComponentHierarchy(component_number=str(next_root)))
            continue

        parent_number = root_numbers.get(parent_index)
        if parent_number is None:
            next_root += 1
            parent_number = str(next_root)
            root_numbers[parent_index] = parent_number
        child_counts[parent_index] = child_counts.get(parent_index, 0) + 1
        parent = services[parent_index]
        result.append(
            ComponentHierarchy(
                component_number=f"{parent_number}.{child_counts[parent_index]}",
                parent_component_id=str(parent_index),
                parent_component_number=parent_number,
                parent_display_name=(
                    parent.calculator_service_name or parent.service
                ),
            )
        )
    return result
