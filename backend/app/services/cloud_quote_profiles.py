from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[3] / "policies" / "cloud-market-profiles.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "astraquote-cloud-market-profiles/1":
        raise RuntimeError("AstraQuote cloud market profile catalog is invalid")
    return payload


def active_market_profile(provider: str) -> dict[str, Any]:
    provider_key = str(provider).strip().casefold()
    provider_config = _catalog().get("providers", {}).get(provider_key)
    if not isinstance(provider_config, dict):
        raise ValueError("unsupported cloud provider")
    environment_key = f"ASTRAQUOTE_{provider_key.upper()}_MARKET_PROFILE"
    profile_id = os.environ.get(environment_key, provider_config.get("default_profile"))
    profile = provider_config.get("profiles", {}).get(profile_id)
    if not isinstance(profile, dict):
        raise RuntimeError(f"Configured market profile does not exist: {profile_id}")
    regions = profile.get("regions") or []
    normalized_regions = [
        {"code": str(code), "label": str(label)}
        for code, label in regions
        if str(code).strip() and str(label).strip()
    ]
    normalized_scenarios = []
    for scenario in profile.get("pricing_scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        key = str(scenario.get("key") or "").strip()
        label = str(scenario.get("label") or "").strip()
        term_months = scenario.get("term_months")
        if not key or not label or (
            term_months is not None
            and (not isinstance(term_months, int) or term_months < 1)
        ):
            continue
        normalized_scenarios.append(
            {"key": key, "label": label, "term_months": term_months}
        )
    if not normalized_scenarios or normalized_scenarios[0]["key"] != "on_demand":
        raise RuntimeError(f"Configured pricing scenarios are invalid: {profile_id}")
    scenario_keys = [item["key"] for item in normalized_scenarios]
    if len(scenario_keys) != len(set(scenario_keys)) or len(scenario_keys) > 3:
        raise RuntimeError(f"Configured pricing scenarios are invalid: {profile_id}")
    return {
        "provider": provider_key,
        "market_profile": str(profile_id),
        "site_label": str(profile.get("site_label") or profile_id),
        "market_scope": str(profile.get("market_scope") or "unknown"),
        "credential_scope": str(profile.get("credential_scope") or profile_id),
        "currency_policy": str(profile.get("currency_policy") or "official_response"),
        "official_source_url": str(profile.get("official_source_url") or ""),
        "regions": normalized_regions,
        "pricing_scenarios": normalized_scenarios,
    }


def provider_region_catalog(provider: str) -> dict[str, Any]:
    profile = active_market_profile(provider)
    regions = profile["regions"]
    if profile["provider"] == "aws":
        # Only the backend region-catalog endpoint needs botocore's current AWS
        # directory. The desktop GPT relay only needs the market profile and
        # must remain able to start in its intentionally small host venv.
        from app.integrations.aws_regions import commercial_aws_region_options

        regions = [
            {"code": str(code), "label": str(label)}
            for code, label in commercial_aws_region_options()
        ]
    return {
        **profile,
        "regions": regions,
        "region_count": len(regions),
        "catalog_checked_at": str(_catalog().get("catalog_checked_at") or ""),
        "catalog_role": "official_provider_regions_only",
    }


def is_provider_region(provider: str, region: str) -> bool:
    code = str(region).strip()
    return any(
        item["code"] == code
        for item in provider_region_catalog(provider)["regions"]
    )


def provider_pricing_scenarios(provider: str) -> list[dict[str, Any]]:
    return list(active_market_profile(provider)["pricing_scenarios"])


def is_provider_pricing_scenario(provider: str, scenario_key: str) -> bool:
    key = str(scenario_key).strip()
    return any(
        item["key"] == key
        for item in provider_pricing_scenarios(provider)
    )
