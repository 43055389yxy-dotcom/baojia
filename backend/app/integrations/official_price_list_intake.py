"""Provider-derived intake contract when Calculator has no form for a service."""

import hashlib
import json
from typing import Any

from app.domain.models import ServiceRequirement
from app.integrations.aws_regions import official_catalog_region_scope

PRICE_LIST_READY = "price_list_ready"
PRICE_LIST_CONTRACT = "_official_price_list_intake_contract"


def _verified_price_list_payload(
    component: ServiceRequirement, profile: dict[str, Any] | None,
) -> dict[str, Any]:
    """Require scoped official meters and bindings, not merely a success flag.

    This validates a profile returned by AutoServiceDiscovery, never AI output.
    Values/prices are deliberately excluded; actual usage and money must still
    pass through RequirementIR, ResourceIR, BillingUsageIR and PriceIR.
    """
    if not profile or profile.get("status") != "verified":
        raise ValueError("官方计费目录尚未验证")
    if profile.get("service_key") != component.service:
        raise ValueError("官方计费目录的产品身份与组件不一致")
    scope = official_catalog_region_scope(component.region)
    if "region" not in profile or official_catalog_region_scope(profile["region"]) != scope:
        raise ValueError("官方计费目录的查询区域与组件不一致")
    code = profile.get("service_code")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("缺少官方计费产品代码")
    dimensions = profile.get("dimensions") or []
    bindings = profile.get("field_bindings") or []
    if not dimensions or not bindings:
        raise ValueError("官方计费目录没有可验证的字段和计费项")
    meters = []
    for dimension in dimensions:
        if not isinstance(dimension, dict) or not dimension.get("usage_type") or not dimension.get("unit"):
            raise ValueError("官方计费项身份不完整")
        meters.append({key: dimension.get(key) or "" for key in (
            "usage_type", "operation", "unit", "description", "instance_type",
        )})
    identities = {(row["usage_type"], row["operation"], row["unit"]) for row in meters}
    for binding in bindings:
        if not isinstance(binding, dict) or not binding.get("field") or (
            binding.get("usage_type"), binding.get("operation") or "", binding.get("unit")
        ) not in identities:
            raise ValueError("字段映射未指向当前官方计费项")
    payload = {
        "source": "aws_price_list", "service_code": code,
        "service_key": component.service, "region": scope,
        "profile_schema_version": profile.get("profile_schema_version"),
        "meters": sorted(meters, key=lambda row: json.dumps(row, sort_keys=True)),
        "field_bindings": sorted(bindings, key=lambda row: json.dumps(row, sort_keys=True)),
    }
    return payload


def _contract_reference(payload: dict[str, Any]) -> dict[str, Any]:
    billing_schema_hash = hashlib.sha256(json.dumps(
        {key: value for key, value in payload.items() if key != "region"},
        ensure_ascii=False, sort_keys=True,
    ).encode()).hexdigest()
    schema_hash = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    # The full official profile remains in its provider cache. Drafts and UI
    # traces need only the scoped identity and fingerprints, not hundreds of
    # duplicated meter descriptions for each extraction event.
    return {
        key: payload[key] for key in (
            "source", "service_code", "service_key", "region", "profile_schema_version",
        )
    } | {"schema_hash": schema_hash, "billing_schema_hash": billing_schema_hash, "contract_version": 2}


def verified_price_list_contract(
    component: ServiceRequirement, profile: dict[str, Any] | None,
) -> dict[str, Any]:
    return _contract_reference(_verified_price_list_payload(component, profile))


def revalidated_price_list_contract(
    component: ServiceRequirement, profile: dict[str, Any] | None, stored: dict[str, Any],
) -> dict[str, Any]:
    """Upgrade v1 region spelling only after reproducing its full fingerprint.

    This never reinterprets source, edits facts, or accepts a changed meter.
    New contracts cannot invoke the legacy path by changing their region.
    """
    payload = _verified_price_list_payload(component, profile)
    current = _contract_reference(payload)
    if not isinstance(stored, dict) or stored.get("contract_version") not in (None, 2):
        raise ValueError("官方计费目录记录版本无效，请重新核验配置")
    identity_keys = ("source", "service_code", "service_key", "profile_schema_version")
    if (any(stored.get(key) != current[key] for key in identity_keys)
            or "region" not in stored
            or official_catalog_region_scope(stored["region"]) != current["region"]):
        raise ValueError("官方计费目录记录的产品或区域已变化，请重新核验配置")
    if current["schema_hash"] == stored.get("schema_hash"):
        return current
    if stored.get("contract_version") is None:
        legacy = _contract_reference(dict(payload, region=stored["region"]))
        if legacy["schema_hash"] == stored.get("schema_hash"):
            return current
    raise ValueError("官方计费字段已变化，请重新核验配置")
