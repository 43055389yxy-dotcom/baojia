from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_SCHEMA_VERSION = "astraquote-aws-pricing-route/1"
_PRICING_HOST = "api.pricing.us-east-1.amazonaws.com"
_MISSING = object()
_SAFE_ROUTE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,159}$")
_SAFE_ROUTE_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,119}$")


class AwsPricingRouteError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


@dataclass(frozen=True)
class PreparedAwsPricingRoute:
    route: dict[str, Any]
    component_id: str
    service_code: str
    region: str
    pricing_model: str
    route_inputs: dict[str, Any]
    filters: dict[str, str]


class AwsPricingRouteCatalog:
    """Process-local registry of verified AWS pricing request contracts.

    Route files contain no prices and never choose product values.  They only
    turn caller-selected runtime values into a read-only Price List request and
    mechanically narrow the official response.
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        self._directory = Path(directory) if directory else _default_route_directory()
        self._loaded = False
        self._routes: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self._components: dict[str, dict[str, Any]] = {}
        self._rejected_files: dict[str, str] = {}

    @property
    def route_count(self) -> int:
        self._load()
        return len(self._routes)

    @property
    def component_ids(self) -> set[str]:
        self._load()
        return set(self._components)

    @property
    def rejected_file_count(self) -> int:
        self._load()
        return len(self._rejected_files)

    def describe(
        self,
        *,
        service_code: str,
        component_id: str | None,
        pricing_model: str | None,
        route_search: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        self._load()
        normalized_component = str(component_id or "").strip().casefold()
        normalized_service = str(service_code).strip()
        search_terms = [
            token for token in re.split(r"[^a-z0-9]+", str(route_search or "").casefold()) if token
        ]
        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for document, route in self._routes.values():
            if normalized_component and document["component_id"].casefold() != normalized_component:
                continue
            if normalized_service and document["service_code"] != normalized_service:
                continue
            models = [str(item) for item in route.get("supported_pricing_models") or []]
            if pricing_model and pricing_model not in models:
                continue
            searchable = " ".join(
                (
                    str(route.get("route_id") or ""),
                    str(route.get("billing_dimension") or ""),
                    str(document.get("component_id") or ""),
                    " ".join(str(alias) for alias in document.get("aliases") or []),
                )
            ).casefold()
            if search_terms and not all(term in searchable for term in search_terms):
                continue
            matches.append((document, route))
        matches.sort(key=lambda item: str(item[1]["route_id"]))
        page = matches[offset : offset + limit]
        return {
            "status": "found" if matches else "not_found",
            "provider": "aws",
            "account_site": "aws-commercial",
            "service_code": normalized_service,
            "component_id": normalized_component or None,
            "matched_count": len(matches),
            "offset": offset,
            "next_offset": offset + limit if offset + limit < len(matches) else None,
            "routes": [self._summary(document, route) for document, route in page],
            "source": "AstraQuote verified local AWS pricing routes",
        }

    def prepare_query(
        self,
        *,
        route_id: str,
        service_code: str,
        region: str,
        pricing_model: str,
        route_inputs: dict[str, Any],
        caller_filters: dict[str, str],
    ) -> PreparedAwsPricingRoute:
        self._load()
        selected = self._routes.get(route_id)
        if selected is None:
            raise AwsPricingRouteError(
                "The requested verified local AWS pricing route is not installed.",
                code="aws_local_route_not_found",
                details={"route_id": route_id},
            )
        document, route = selected
        api_service_code = str(route["api_1"].get("service_code") or "").strip()
        if service_code not in {document["service_code"], api_service_code}:
            raise AwsPricingRouteError(
                "The local AWS pricing route belongs to a different service code.",
                code="aws_local_route_service_mismatch",
                details={
                    "route_id": route_id,
                    "expected_service_codes": [
                        document["service_code"],
                        api_service_code,
                    ],
                    "received_service_code": service_code,
                },
            )
        supported_models = set(route.get("supported_pricing_models") or [])
        if pricing_model not in supported_models:
            raise AwsPricingRouteError(
                "The local AWS pricing route does not support this purchase model.",
                code="aws_local_route_pricing_model_mismatch",
                details={
                    "route_id": route_id,
                    "pricing_model": pricing_model,
                    "supported_pricing_models": sorted(supported_models),
                },
            )
        normalized_inputs = self._validate_runtime_inputs(route, route_inputs)
        filters = dict(caller_filters)
        for rule in route["api_1"].get("filters") or []:
            if rule.get("send_to_api") is False:
                continue
            field = str(rule.get("field") or "").strip()
            if not field:
                continue
            value = _expected_value(rule, normalized_inputs, region)
            if value is _MISSING:
                if rule.get("omit_if_input_absent") is True:
                    continue
                raise AwsPricingRouteError(
                    "A required local AWS pricing route value is missing.",
                    code="aws_local_route_input_missing",
                    retryable=True,
                    details={"route_id": route_id, "field": field},
                )
            rendered = _aws_filter_value(value)
            if field in filters and str(filters[field]) != rendered:
                raise AwsPricingRouteError(
                    "A caller filter conflicts with the verified local AWS pricing route.",
                    code="aws_local_route_filter_conflict",
                    details={
                        "route_id": route_id,
                        "field": field,
                        "route_value": rendered,
                        "caller_value": str(filters[field]),
                    },
                )
            filters[field] = rendered
        return PreparedAwsPricingRoute(
            route=route,
            component_id=document["component_id"],
            service_code=api_service_code,
            region=region,
            pricing_model=pricing_model,
            route_inputs=normalized_inputs,
            filters=filters,
        )

    def filter_products(
        self,
        products: list[dict[str, Any]],
        prepared: PreparedAwsPricingRoute,
    ) -> list[dict[str, Any]]:
        rules = list(prepared.route["api_1"].get("post_filters") or [])
        product_rules = [rule for rule in rules if _rule_scope(rule) == "product"]
        term_rules = [rule for rule in rules if _rule_scope(rule) == "term"]
        dimension_rules = [rule for rule in rules if _rule_scope(rule) == "price_dimension"]
        term_namespace = "Reserved" if prepared.pricing_model == "reserved" else "OnDemand"
        filtered: list[dict[str, Any]] = []
        for payload in products:
            if not all(
                _rule_matches(
                    rule,
                    payload,
                    prepared.route_inputs,
                    prepared.region,
                    scope="product",
                )
                for rule in product_rules
            ):
                continue
            copied = copy.deepcopy(payload)
            namespace = copied.get("terms", {}).get(term_namespace, {})
            if not isinstance(namespace, dict) or not namespace:
                continue
            selected_terms: dict[str, Any] = {}
            for term_code, term in namespace.items():
                if not isinstance(term, dict):
                    continue
                if not all(
                    _rule_matches(
                        rule,
                        term,
                        prepared.route_inputs,
                        prepared.region,
                        scope="term",
                    )
                    for rule in term_rules
                ):
                    continue
                selected_term = copy.deepcopy(term)
                dimensions = selected_term.get("priceDimensions", {})
                if not isinstance(dimensions, dict):
                    continue
                if dimension_rules:
                    dimensions = {
                        dimension_code: dimension
                        for dimension_code, dimension in dimensions.items()
                        if isinstance(dimension, dict)
                        and all(
                            _rule_matches(
                                rule,
                                dimension,
                                prepared.route_inputs,
                                prepared.region,
                                scope="price_dimension",
                            )
                            for rule in dimension_rules
                        )
                    }
                    if not dimensions:
                        continue
                    selected_term["priceDimensions"] = dimensions
                selected_terms[str(term_code)] = selected_term
            if namespace and not selected_terms:
                continue
            copied["terms"] = {term_namespace: selected_terms}
            filtered.append(copied)
        return filtered

    def _load(self) -> None:
        if self._loaded:
            return
        if not self._directory.is_dir():
            self._loaded = True
            return
        for path in sorted(self._directory.glob("aws-commercial-*-route.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self._validate_document(path, payload)
                component_id = str(payload["component_id"])
                route_ids = [str(route["route_id"]) for route in payload["routes"]]
                if component_id in self._components:
                    raise RuntimeError(f"Duplicate AWS pricing route component: {component_id}")
                duplicates = sorted(set(route_ids) & set(self._routes))
                if duplicates:
                    raise RuntimeError(f"Duplicate AWS pricing route id: {duplicates[0]}")
            except (OSError, json.JSONDecodeError, RuntimeError, re.error) as exc:
                self._rejected_files[path.name] = str(exc)[:300]
                continue
            self._components[component_id] = payload
            for route_id, route in zip(route_ids, payload["routes"], strict=True):
                self._routes[route_id] = (payload, route)
        self._loaded = True

    @staticmethod
    def _validate_document(path: Path, payload: Any) -> None:
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise RuntimeError(f"AWS pricing route schema is invalid: {path}")
        if (
            payload.get("provider") != "aws"
            or payload.get("account_site") != "aws-commercial"
            or payload.get("partition") != "aws"
        ):
            raise RuntimeError(f"AWS pricing route account boundary is invalid: {path}")
        if not str(payload.get("component_id") or "").strip():
            raise RuntimeError(f"AWS pricing route component is missing: {path}")
        service_code = str(payload.get("service_code") or "").strip()
        routes = payload.get("routes")
        if not service_code or not isinstance(routes, list) or not routes or len(routes) > 1000:
            raise RuntimeError(f"AWS pricing route document is incomplete: {path}")
        for route in routes:
            if not isinstance(route, dict) or route.get("status") != "ready":
                raise RuntimeError(f"AWS pricing route is not ready: {path}")
            route_id = str(route.get("route_id") or "")
            if not _SAFE_ROUTE_ID.fullmatch(route_id):
                raise RuntimeError(f"AWS pricing route id is invalid: {path}")
            api = route.get("api_1")
            endpoint = urlparse(str(api.get("endpoint") or "")) if isinstance(api, dict) else None
            if (
                not isinstance(api, dict)
                or api.get("type") != "aws_price_list_query"
                or api.get("service") != "pricing"
                or api.get("action") != "GetProducts"
                or str(api.get("method") or "").upper() != "POST"
                or not str(api.get("service_code") or "").strip()
                or endpoint is None
                or endpoint.scheme != "https"
                or endpoint.hostname != _PRICING_HOST
                or endpoint.path not in {"", "/"}
                or endpoint.username
                or endpoint.password
                or endpoint.port not in {None, 443}
            ):
                raise RuntimeError(
                    f"AWS pricing route must use the read-only AWS Price List API: {path}"
                )
            inputs = route.get("runtime_inputs") or []
            if not isinstance(inputs, list) or len(inputs) > 40:
                raise RuntimeError(f"AWS pricing route inputs are invalid: {path}")
            input_keys = [str(item.get("key") or "") for item in inputs if isinstance(item, dict)]
            if (
                len(input_keys) != len(inputs)
                or len(set(input_keys)) != len(input_keys)
                or any(not _SAFE_ROUTE_KEY.fullmatch(key) for key in input_keys)
                or any(item.get("type") not in {"string", "integer", "boolean"} for item in inputs)
            ):
                raise RuntimeError(f"AWS pricing route inputs are not unique: {path}")
            for collection in ("filters", "post_filters"):
                rules = api.get(collection) or []
                if not isinstance(rules, list) or len(rules) > 80:
                    raise RuntimeError(f"AWS pricing route filters are invalid: {path}")
                for rule in rules:
                    field = str(rule.get("field") or "") if isinstance(rule, dict) else ""
                    if (
                        not isinstance(rule, dict)
                        or not field
                        or not _SAFE_ROUTE_KEY.fullmatch(field)
                        or rule.get("source") not in {"constant", "region", "runtime_input"}
                    ):
                        raise RuntimeError(f"AWS pricing route filter is invalid: {path}")
                    if rule.get("source") == "runtime_input":
                        key = str(rule.get("input_key") or rule.get("runtime_input") or "")
                        if key not in input_keys:
                            raise RuntimeError(
                                f"AWS pricing route references an unknown input: {path}"
                            )
                    operator = _rule_operator(rule)
                    if operator not in {
                        "equals",
                        "not_equals",
                        "regex",
                        "has_key",
                        "decimal_greater_than",
                    }:
                        raise RuntimeError(f"AWS pricing route operator is invalid: {path}")
                    if operator == "regex" and "value" in rule:
                        re.compile(str(rule["value"]))
            page_url = str(route.get("official_price_page") or "")
            sources = route.get("official_sources") or []
            if (
                not _is_official_aws_url(page_url)
                or not isinstance(sources, list)
                or any(not _is_official_aws_url(str(source)) for source in sources)
            ):
                raise RuntimeError(f"AWS pricing route official source is invalid: {path}")

    @staticmethod
    def _validate_runtime_inputs(route: dict[str, Any], supplied: dict[str, Any]) -> dict[str, Any]:
        definitions = {
            str(item["key"]): item
            for item in route.get("runtime_inputs") or []
            if isinstance(item, dict) and item.get("key")
        }
        unknown = sorted(set(supplied) - set(definitions))
        if unknown:
            raise AwsPricingRouteError(
                "The local AWS pricing route received unknown runtime inputs.",
                code="aws_local_route_input_unknown",
                retryable=True,
                details={"route_id": route["route_id"], "input_keys": unknown},
            )
        missing = sorted(
            key
            for key, definition in definitions.items()
            if definition.get("required") is True and key not in supplied
        )
        if missing:
            raise AwsPricingRouteError(
                "The local AWS pricing route is missing required runtime inputs.",
                code="aws_local_route_input_missing",
                retryable=True,
                details={"route_id": route["route_id"], "input_keys": missing},
            )
        normalized = dict(supplied)
        for key, value in normalized.items():
            definition = definitions[key]
            expected_type = definition.get("type")
            valid = (
                (expected_type == "string" and isinstance(value, str))
                or (
                    expected_type == "integer"
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                )
                or (expected_type == "boolean" and isinstance(value, bool))
            )
            if not valid:
                raise AwsPricingRouteError(
                    "A local AWS pricing route runtime input has the wrong type.",
                    code="aws_local_route_input_invalid",
                    retryable=True,
                    details={"route_id": route["route_id"], "input_key": key},
                )
            if "enum" in definition and value not in definition["enum"]:
                raise AwsPricingRouteError(
                    "A local AWS pricing route runtime input is outside its allowed values.",
                    code="aws_local_route_input_invalid",
                    retryable=True,
                    details={"route_id": route["route_id"], "input_key": key},
                )
            if expected_type == "integer":
                minimum = definition.get("minimum")
                maximum = definition.get("maximum")
                multiple = definition.get("multipleOf")
                if minimum is not None and value < minimum:
                    valid = False
                if maximum is not None and value > maximum:
                    valid = False
                if multiple is not None and value % multiple != 0:
                    valid = False
                if not valid:
                    raise AwsPricingRouteError(
                        "A local AWS pricing route integer input is outside its allowed range.",
                        code="aws_local_route_input_invalid",
                        retryable=True,
                        details={"route_id": route["route_id"], "input_key": key},
                    )
        return normalized

    @staticmethod
    def _summary(document: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
        required: list[str] = []
        optional: list[str] = []
        input_schema: list[dict[str, Any]] = []
        for item in route.get("runtime_inputs") or []:
            key = str(item["key"])
            (required if item.get("required") is True else optional).append(key)
            input_schema.append(
                {
                    field: item[field]
                    for field in (
                        "key",
                        "type",
                        "required",
                        "enum",
                        "minimum",
                        "maximum",
                        "multipleOf",
                    )
                    if field in item
                }
            )
        return {
            "route_id": route["route_id"],
            "component_id": document["component_id"],
            "service_code": document["service_code"],
            "api_service_code": route["api_1"].get("service_code"),
            "billing_dimension": route.get("billing_dimension"),
            "supported_pricing_models": route.get("supported_pricing_models") or [],
            "applicability": route.get("applicability") or {},
            "required_inputs": required,
            "optional_inputs": optional,
            "input_schema": input_schema,
            "official_price_page": route.get("official_price_page"),
        }


def _default_route_directory() -> Path:
    configured = os.getenv("ASTRAQUOTE_AWS_PRICING_ROUTES_DIR")
    if configured:
        return Path(configured)
    return (
        Path(__file__).resolve().parents[3] / "policies" / "aws-pricing-routes" / "aws-commercial"
    )


def _is_official_aws_url(value: str) -> bool:
    parsed = urlparse(value)
    host = (parsed.hostname or "").casefold().rstrip(".")
    return (
        parsed.scheme == "https"
        and host in {"aws.amazon.com", "docs.aws.amazon.com"}
        and not parsed.username
        and not parsed.password
        and parsed.port in {None, 443}
    )


def _expected_value(rule: dict[str, Any], inputs: dict[str, Any], region: str) -> Any:
    source = rule.get("source")
    if source == "constant":
        return rule.get("value", _MISSING)
    if source == "region":
        return region
    if source == "runtime_input":
        key = str(rule.get("input_key") or rule.get("runtime_input") or "")
        return inputs.get(key, _MISSING)
    return _MISSING


def _aws_filter_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _rule_scope(rule: dict[str, Any]) -> str:
    scope = str(rule.get("scope") or "").strip()
    if scope in {"product", "term", "price_dimension"}:
        return scope
    path = str(rule.get("path") or rule.get("field") or "")
    if path.startswith("term") or path.startswith("terms."):
        return "term"
    if path.startswith("price") or path.startswith("appliesTo"):
        return "price_dimension"
    return "product"


def _rule_operator(rule: dict[str, Any]) -> str:
    operator = str(rule.get("operator") or rule.get("type") or "equals").strip()
    return {
        "TERM_MATCH": "equals",
        "REGEX": "regex",
    }.get(operator, operator.casefold())


def _rule_matches(
    rule: dict[str, Any],
    candidate: dict[str, Any],
    inputs: dict[str, Any],
    region: str,
    *,
    scope: str,
) -> bool:
    expected = _expected_value(rule, inputs, region)
    if expected is _MISSING and rule.get("omit_if_input_absent") is True:
        return True
    if expected is _MISSING:
        return False
    actual = _rule_value(rule, candidate, scope)
    if actual is _MISSING:
        return rule.get("missing") == "accept"
    operator = _rule_operator(rule)
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "regex":
        return re.search(str(expected), str(actual)) is not None
    if operator == "has_key":
        return isinstance(actual, dict) and str(expected) in actual
    if operator == "decimal_greater_than":
        try:
            return Decimal(str(actual)) > Decimal(str(expected))
        except (InvalidOperation, ValueError):
            return False
    return False


def _rule_value(rule: dict[str, Any], candidate: dict[str, Any], scope: str) -> Any:
    path = str(rule.get("path") or rule.get("field") or "")
    if scope == "product":
        if path.startswith("product.") or path.startswith("serviceCode"):
            return _read_path(candidate, path)
        attributes = candidate.get("product", {}).get("attributes", {})
        if path in attributes:
            return attributes[path]
        product = candidate.get("product", {})
        if path in product:
            return product[path]
        if path.casefold() == "servicecode":
            return attributes.get("servicecode", candidate.get("serviceCode", _MISSING))
        return _MISSING
    if scope == "term":
        if path.startswith("terms."):
            return _MISSING
        return _read_path(candidate, path)
    return _read_path(candidate, path)


def _read_path(value: Any, path: str) -> Any:
    current = value
    for segment in path.split("."):
        if not segment:
            continue
        if not isinstance(current, dict) or segment not in current:
            return _MISSING
        current = current[segment]
    return current
