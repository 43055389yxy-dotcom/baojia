from __future__ import annotations

import json
from typing import Any

from app.domain.customer_facts import record_customer_fact_metadata
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
    "requested_sku",
    "operating_system",
    "architecture",
    "tenancy",
    "engine",
    "engine_version",
    "deployment",
    "storage_class",
    "storage_type",
    "cluster_type",
    "volume_type",
}


def _normalized_catalog_model(value: object) -> str:
    """Normalize presentation-only prefixes without guessing a product."""

    model = "".join(str(value or "").split()).casefold()
    # MSK's pricing catalogue uses ``kafka.m7g.xlarge`` in some responses,
    # while the customer picker and EC2 specification API use ``m7g.xlarge``.
    return model.removeprefix("kafka.")


def bind_selected_model_specifications(
    component: ServiceRequirement,
    selected_model: str,
) -> bool:
    """Atomically bind an official model choice to its official CPU/memory.

    A selected model and the previous descriptive shape are not independent
    customer facts.  If the customer chooses one of the official candidates,
    that candidate's specifications must replace the old CPU/memory values in
    the same update.  Otherwise later pages can display impossible hybrids
    such as ``m7g.xlarge`` together with ``8 vCPU / 16 GiB``.
    """

    normalized_selected = _normalized_catalog_model(selected_model)
    if not normalized_selected:
        return False

    candidates = component.requirements.get("_review_confirmation_candidates")
    if not isinstance(candidates, list):
        candidates = []
    matched: dict[str, Any] | None = next(
        (
            candidate
            for candidate in candidates
            if isinstance(candidate, dict)
            and _normalized_catalog_model(candidate.get("model")) == normalized_selected
        ),
        None,
    )
    specifications = (
        matched.get("specifications") if isinstance(matched, dict) else None
    )
    if not isinstance(specifications, dict):
        reviewed_model = component.requirements.get("_review_selected_model")
        reviewed_specifications = component.requirements.get(
            "_review_selected_specifications"
        )
        if (
            _normalized_catalog_model(reviewed_model) == normalized_selected
            and isinstance(reviewed_specifications, dict)
        ):
            specifications = reviewed_specifications
    if not isinstance(specifications, dict):
        return False

    shape = {
        "vcpu": specifications.get("vCPU"),
        "memory_gib": specifications.get("memoryGiB"),
    }
    usable_shape = {
        field: value
        for field, value in shape.items()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    }
    if not usable_shape:
        return False

    component.requirements["requested_model"] = selected_model
    component.requirements["_review_selected_model"] = selected_model
    component.requirements["_review_selected_specifications"] = dict(specifications)
    component.field_sources["requirements.requested_model"] = "customer_confirmation"
    component.field_evidence["requirements.requested_model"] = (
        "客户从官方可用型号中选择"
    )
    locked = set(component.locked_fields) | {"requirements.requested_model"}
    for field, value in usable_shape.items():
        path = f"requirements.{field}"
        component.requirements[field] = value
        component.field_sources[path] = "customer_confirmation"
        component.field_evidence[path] = f"由客户选择的官方型号 {selected_model} 确定"
        record_customer_fact_metadata(
            component,
            field,
            component.field_evidence[path],
            policy="exact",
        )
        locked.add(path)
    component.field_sources["_customer_shape_replaced_by_model"] = (
        "customer_confirmation"
    )
    component.locked_fields = sorted(locked)
    return True


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
    explicitly_selected_model = any(
        requirements.get(field) not in {None, ""}
        for field in ("requested_model", "requested_sku")
    )
    if SHAPE_FIELDS.intersection(requirements):
        # A later direct shape edit supersedes an earlier choice that replaced
        # the original CPU/memory sentence with a catalog model.
        revised.field_sources.pop("_customer_shape_replaced_by_model", None)
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
            revised.field_sources[path] = "customer_confirmation_removed"
            revised.field_evidence[path] = "客户在配置表中明确删除"
            revised.locked_fields = sorted(set(revised.locked_fields) | {path})
            changed_paths.append(path)
            continue
        revised.requirements[field] = value
        record_customer_fact_metadata(
            revised,
            field,
            "客户在配置表中直接编辑",
            policy="exact",
        )
        changed_paths.append(path)

    selected_model = requirements.get("requested_model")
    model_shape_bound = False
    if isinstance(selected_model, str) and selected_model.strip():
        # The browser may submit the whole edit form, including the old shape,
        # in the same payload as the newly chosen model.  The official option
        # is one atomic choice, so its catalogue specifications win over those
        # stale form values for every instance-sized AWS service.
        model_shape_bound = bind_selected_model_specifications(
            revised, selected_model.strip()
        )

    # Any catalog-compatibility edit must be matched again. Keeping the old
    # model would make the official catalog restore stale CPU/memory/OS/engine
    # information or reach the final quote with no billable product.
    if catalog_compatibility_changed and not explicitly_selected_model:
        revised.requirements.pop("requested_model", None)
        revised.requirements.pop("requested_sku", None)
        revised.requirements.pop("_review_selected_model", None)
        revised.requirements.pop("_review_selected_specifications", None)
        for path in (
            "requirements.requested_model",
            "requirements.requested_sku",
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
        "_quote_skip_code",
        "_quote_skip_category",
    ):
        revised.requirements.pop(field, None)

    locked = set(revised.locked_fields)
    for path in changed_paths:
        if model_shape_bound and path in {
            "requirements.vcpu",
            "requirements.memory_gib",
        }:
            # Keep the more precise evidence written by the atomic model
            # binding instead of relabelling an official specification as a
            # free-form value typed by the customer.
            locked.add(path)
            continue
        if revised.field_sources.get(path) != "customer_confirmation_removed":
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
