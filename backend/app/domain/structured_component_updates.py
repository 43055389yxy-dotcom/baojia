from __future__ import annotations

import json
from typing import Any

from app.domain.models import ServiceRequirement


SHAPE_FIELDS = {
    "vcpu",
    "memory_gib",
    "master_vcpu",
    "master_memory_gib",
    "core_vcpu",
    "core_memory_gib",
    "task_vcpu",
    "task_memory_gib",
}

# Any of these fields can change which official SKU/model is valid.  A model
# selected before the edit is only a cache of the old configuration; keeping
# it after (for example) switching Linux to Windows or changing a database
# engine makes the final pricing pass query an impossible combination.
CATALOG_COMPATIBILITY_FIELDS = {
    *SHAPE_FIELDS,
    "requested_model",
    "operating_system",
    "architecture",
    "tenancy",
    "engine",
    "deployment",
    "storage_class",
    "storage_type",
    "cluster_type",
    "volume_type",
}


def decode_component_update(value: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def apply_component_update(
    component: ServiceRequirement, update: dict[str, Any]
) -> ServiceRequirement:
    """Apply customer-edited form fields without asking AI to reinterpret them."""

    revised = component.model_copy(deep=True)
    changed_paths: list[str] = []

    if "region" in update:
        region = str(update["region"] or "").strip()
        if region:
            revised.region = region
            changed_paths.append("region")
    if "quantity" in update:
        quantity = int(update["quantity"])
        if quantity < 1 or quantity > 10000:
            raise ValueError("数量必须在 1 到 10000 之间")
        revised.quantity = quantity
        changed_paths.append("quantity")

    requirements = update.get("requirements", {})
    if not isinstance(requirements, dict):
        raise ValueError("组件参数格式不正确")
    explicitly_selected_model = "requested_model" in requirements
    catalog_compatibility_changed = (
        "region" in update
        or any(field in requirements for field in CATALOG_COMPATIBILITY_FIELDS)
    )
    for field, value in requirements.items():
        if not isinstance(field, str) or not field or field.startswith("_"):
            raise ValueError("组件参数名称不正确")
        path = f"requirements.{field}"
        if value is None or value == "":
            revised.requirements.pop(field, None)
            revised.field_sources.pop(path, None)
            revised.field_evidence.pop(path, None)
            revised.locked_fields = [entry for entry in revised.locked_fields if entry != path]
            continue
        revised.requirements[field] = value
        changed_paths.append(path)

    # Any catalog-compatibility edit must be matched again. Keeping the old
    # model would make the official catalog restore stale CPU/memory/OS/engine
    # information or reach the final quote with no billable product.
    if catalog_compatibility_changed and not explicitly_selected_model:
        revised.requirements.pop("requested_model", None)
        revised.requirements.pop("_review_selected_model", None)
        revised.requirements.pop("_review_selected_specifications", None)
        for path in (
            "requirements.requested_model",
            "requirements._review_selected_model",
            "requirements._review_selected_specifications",
        ):
            revised.field_sources.pop(path, None)
            revised.field_evidence.pop(path, None)
            revised.locked_fields = [entry for entry in revised.locked_fields if entry != path]

    # These values are derived defaults from the old revision. They must be
    # recalculated after any explicit customer edit.
    for field in (
        "reference_unit_only",
        "reference_lcu_unit_only",
        "system_default_assumption",
        "_quote_skip_reason",
    ):
        revised.requirements.pop(field, None)

    locked = set(revised.locked_fields)
    for path in changed_paths:
        revised.field_sources[path] = "customer_confirmation"
        revised.field_evidence[path] = "客户在配置表中直接编辑"
        locked.add(path)
    revised.locked_fields = sorted(locked)
    if changed_paths:
        summary = "、".join(path.removeprefix("requirements.") for path in changed_paths)
        revised.source_text = (
            f"客户通过配置表直接修改：{summary}\n{component.source_text}"
        ).strip()
    return revised
