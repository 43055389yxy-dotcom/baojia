from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.domain.models import ParsedIntent, ServiceRequirement


CUSTOMER_FIELD_SOURCES = {
    "customer_text",
    "customer_confirmation",
    "customer_confirmation_removed",
    "customer_correction",
    "sales_confirmation",
}

CUSTOMER_OVERRIDE_SOURCES = {
    "customer_confirmation",
    "customer_confirmation_removed",
    "customer_correction",
    "sales_confirmation",
}

_EDIT_PREFIX = re.compile(
    r"^(?:客户通过配置表直接修改|客户最新修改|处理规则|客户原始配置)\s*[:：].*$",
    re.I,
)


def original_component_source(source: str) -> str:
    """Remove edit annotations while retaining the customer's exact wording."""

    lines: list[str] = []
    for raw_line in str(source or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _EDIT_PREFIX.match(line):
            # ``客户原始配置：...`` can carry the original text on the same
            # line. Keep that payload while removing only the edit annotation.
            if re.match(r"^客户原始配置\s*[:：]", line, re.I):
                payload = re.sub(r"^客户原始配置\s*[:：]\s*", "", line, count=1, flags=re.I)
                if payload:
                    lines.append(payload)
            continue
        lines.append(line)
    return "\n".join(lines)


def canonical_component_source(source: str) -> str:
    """Return the normalized immutable source used only for identity matching."""

    return re.sub(r"\s+", "", original_component_source(source)).casefold()


def _digest(*parts: str) -> str:
    payload = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def ensure_component_keys(intent: ParsedIntent) -> None:
    """Backfill stable identities for new and persisted component drafts."""

    used: set[str] = set()
    collisions: dict[str, int] = {}
    for item in intent.services:
        if not item.original_source_text:
            item.original_source_text = original_component_source(item.source_text)
        if item.component_key and item.component_key not in used:
            used.add(item.component_key)
            continue
        base = _digest(
            str(item.service or "").casefold(),
            str(item.product_identity or "").casefold(),
            canonical_component_source(item.source_text),
        )
        collisions[base] = collisions.get(base, 0) + 1
        suffix = collisions[base]
        key = f"cmp_{base}" if suffix == 1 else f"cmp_{base}_{suffix}"
        while key in used:
            suffix += 1
            key = f"cmp_{base}_{suffix}"
        item.component_key = key
        used.add(key)

    # Bind every explicitly derived resource to its parent using the same
    # generic rule.  Service-specific code may set the relationship earlier,
    # but persisted drafts and future dynamic products are upgraded here.
    for child in intent.services:
        if child.parent_component_key or not child.derived_from_service:
            continue
        parent_service = str(child.derived_from_service).strip().casefold()
        candidates = [
            candidate
            for candidate in intent.services
            if candidate is not child and candidate.service.casefold() == parent_service
        ]
        if not candidates:
            continue
        child_source = canonical_component_source(child.source_text)
        source_matches = [
            candidate
            for candidate in candidates
            if child_source
            and (
                canonical_component_source(candidate.source_text) == child_source
                or child_source in canonical_component_source(candidate.source_text)
                or canonical_component_source(candidate.source_text) in child_source
            )
        ]
        parent = source_matches[0] if len(source_matches) == 1 else (
            candidates[0] if len(candidates) == 1 else None
        )
        if parent is not None:
            child.parent_component_key = parent.component_key


def customer_source_priority(source: str | None) -> int:
    if source in CUSTOMER_OVERRIDE_SOURCES:
        return 4
    if source == "customer_text":
        return 3
    if source == "system_derived":
        return 2
    if source:
        return 1
    return 0


def overlay_customer_fields(
    target: ServiceRequirement,
    source: ServiceRequirement,
) -> None:
    """Merge one component without allowing weaker data to erase customer facts."""

    for field in ("region", "quantity", "hours_per_month"):
        incoming_source = source.field_sources.get(field)
        if incoming_source not in CUSTOMER_FIELD_SOURCES:
            continue
        current_source = target.field_sources.get(field)
        if customer_source_priority(incoming_source) < customer_source_priority(current_source):
            continue
        setattr(target, field, getattr(source, field))
        target.field_sources[field] = incoming_source
        if field in source.field_evidence:
            target.field_evidence[field] = source.field_evidence[field]

    for path, incoming_source in source.field_sources.items():
        if not path.startswith("requirements.") or incoming_source not in CUSTOMER_FIELD_SOURCES:
            continue
        current_source = target.field_sources.get(path)
        if customer_source_priority(incoming_source) < customer_source_priority(current_source):
            continue
        field = path.split(".", 1)[1]
        if incoming_source == "customer_confirmation_removed":
            target.requirements.pop(field, None)
        elif field in source.requirements:
            target.requirements[field] = source.requirements[field]
        target.field_sources[path] = incoming_source
        if path in source.field_evidence:
            target.field_evidence[path] = source.field_evidence[path]

    target.locked_fields = sorted(
        set(target.locked_fields)
        | {
            path
            for path, kind in target.field_sources.items()
            if kind in CUSTOMER_FIELD_SOURCES
            and (
                path in {"region", "quantity", "hours_per_month"}
                or path.startswith("requirements.")
            )
        }
    )


@dataclass(slots=True)
class ComponentLedger:
    components: dict[str, ServiceRequirement]


def capture_customer_ledger(intent: ParsedIntent) -> ComponentLedger:
    """Capture all customer-owned facts by stable component identity."""

    ensure_component_keys(intent)
    return ComponentLedger(
        components={
            item.component_key: item.model_copy(deep=True)
            for item in intent.services
            if item.component_key
        }
    )


def restore_customer_ledger(
    intent: ParsedIntent,
    ledger: ComponentLedger,
    *,
    restore_missing_components: bool = True,
) -> None:
    """Restore customer facts after any automated normalization/pricing pass."""

    ensure_component_keys(intent)
    current = {item.component_key: item for item in intent.services if item.component_key}
    for key, original in ledger.components.items():
        target = current.get(key)
        if target is None:
            if restore_missing_components:
                intent.services.append(original.model_copy(deep=True))
            continue
        overlay_customer_fields(target, original)
        target.component_key = original.component_key
        target.parent_component_key = original.parent_component_key
        target.derived_from_service = original.derived_from_service


def deduplicate_derived_components(intent: ParsedIntent) -> None:
    """Merge exact duplicate children for every service family.

    The key is parent + product + immutable source. Distinct node groups or
    disks under the same parent remain separate because their source or
    product identity differs. Customer-confirmed values always choose the
    survivor and are overlaid before the duplicate row is removed.
    """

    ensure_component_keys(intent)
    grouped: dict[tuple[str, str, str, str], list[ServiceRequirement]] = {}
    for item in intent.services:
        if not item.parent_component_key:
            continue
        identity = str(
            item.product_identity or item.calculator_service_name or item.service
        ).strip().casefold()
        immutable_source = canonical_component_source(item.source_text)
        if not immutable_source:
            continue
        key = (
            item.parent_component_key,
            item.service.casefold(),
            identity,
            immutable_source,
        )
        grouped.setdefault(key, []).append(item)

    removed: set[int] = set()
    for items in grouped.values():
        if len(items) < 2:
            continue
        items.sort(
            key=lambda item: sum(
                customer_source_priority(source)
                for source in item.field_sources.values()
            ),
            reverse=True,
        )
        survivor = items[0]
        for duplicate in items[1:]:
            overlay_customer_fields(survivor, duplicate)
            for field, value in duplicate.requirements.items():
                survivor.requirements.setdefault(field, value)
            if survivor.region is None:
                survivor.region = duplicate.region
            if survivor.quantity == 1 and duplicate.quantity != 1:
                survivor.quantity = duplicate.quantity
            removed.add(id(duplicate))
    if removed:
        intent.services = [item for item in intent.services if id(item) not in removed]


def enforce_component_integrity(intent: ParsedIntent) -> None:
    """Apply the universal isolation contract at every persisted boundary."""

    ensure_component_keys(intent)
    deduplicate_derived_components(intent)
    ensure_component_keys(intent)
    for item in intent.services:
        locked = set(item.locked_fields)
        for path, source in tuple(item.field_sources.items()):
            if source not in CUSTOMER_FIELD_SOURCES:
                continue
            if path.startswith("requirements."):
                field = path.split(".", 1)[1]
                if source != "customer_confirmation_removed" and field not in item.requirements:
                    # Do not keep a false provenance record. Missing fields are
                    # restored from the ledger before this invariant runs.
                    item.field_sources.pop(path, None)
                    item.field_evidence.pop(path, None)
                    continue
            locked.add(path)
        item.locked_fields = sorted(locked)
