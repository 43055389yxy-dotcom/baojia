from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

_SAFE_REGION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,79}$")


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[3] / "policies" / "official-api-base-routes.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "astraquote-official-api-base-routes/1":
        raise RuntimeError("AstraQuote official API base-route catalog is invalid")
    return payload


def _service_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().casefold())


def official_api_base_route(
    provider: str, service: str, region: str
) -> dict[str, str] | None:
    """Return only a verified official hostname for a provider service.

    Paths, actions, parameters, response shapes and rates deliberately remain
    live-query inputs.  This prevents the old detailed-route cache from growing
    back while still stopping callers from inventing provider hostnames.
    """

    provider_key = str(provider).strip().casefold()
    service_key = _service_key(service)
    region_value = str(region).strip()
    if not service_key or not _SAFE_REGION.fullmatch(region_value):
        return None
    provider_routes = _catalog().get("providers", {}).get(provider_key)
    if not isinstance(provider_routes, dict):
        return None
    selected: dict[str, Any] | None = None
    for candidate in provider_routes.get("routes") or []:
        if not isinstance(candidate, dict):
            continue
        aliases = {_service_key(alias) for alias in candidate.get("services") or []}
        if service_key in aliases:
            selected = candidate
            break
    if selected is None and provider_routes.get("default_endpoint"):
        selected = provider_routes
    if selected is None:
        return None
    endpoint = str(
        selected.get("endpoint") or selected.get("default_endpoint") or ""
    ).strip().casefold()
    endpoint_template = str(selected.get("endpoint_template") or "").strip().casefold()
    if not endpoint and endpoint_template:
        endpoint = endpoint_template.replace("{region}", region_value.casefold())
    if not endpoint:
        return None
    source_url = str(
        selected.get("official_source_url")
        or provider_routes.get("official_source_url")
        or ""
    ).strip()
    return {
        "provider": provider_key,
        "service": service_key,
        "endpoint": endpoint,
        "official_source_url": source_url,
        "catalog_checked_at": str(_catalog().get("catalog_checked_at") or ""),
    }
