from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.aws_query_executor import ReadOnlyAwsQueryExecutor


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OfficialCatalogQueryError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class DescribeServiceRequest(StrictModel):
    service_code: str = Field(min_length=2, max_length=120)


class AttributeValuesRequest(DescribeServiceRequest):
    attribute_name: str = Field(min_length=1, max_length=160)
    max_results: int = Field(default=1000, ge=1, le=1000)


class ProductSearchRequest(DescribeServiceRequest):
    region: str = Field(default="global", min_length=3, max_length=40)
    filters: dict[str, str] = Field(default_factory=dict)
    max_results: int = Field(default=100, ge=1, le=1000)


class AwsPriceQuery(ProductSearchRequest):
    provider: Literal["aws"] = "aws"
    query_id: str = Field(min_length=1, max_length=100)
    # At least eleven rows are needed to distinguish a complete ten-row
    # result from a larger result that must be refined by GPT.
    max_results: int = Field(default=100, ge=11, le=1000)
    pricing_model: Literal["on_demand", "reserved"] = "on_demand"
    term_years: Literal[1, 3] | None = None
    payment_option: Literal["no_upfront", "partial_upfront", "all_upfront"] | None = None
    offering_class: Literal["standard", "convertible"] | None = None

    @model_validator(mode="after")
    def validate_purchase_terms(self) -> AwsPriceQuery:
        if self.pricing_model == "on_demand":
            if any(
                value is not None
                for value in (self.term_years, self.payment_option, self.offering_class)
            ):
                raise ValueError("on_demand queries cannot include commitment terms")
            return self
        if self.term_years is None or self.payment_option is None:
            raise ValueError("reserved queries require term_years and payment_option")
        return self


class AzurePriceQuery(StrictModel):
    provider: Literal["azure"] = "azure"
    query_id: str = Field(min_length=1, max_length=100)
    filter: str | None = Field(default=None, max_length=4000)
    currency_code: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    api_version: Literal["2021-10-01", "2023-01-01-preview"] = "2023-01-01-preview"
    next_page_url: str | None = Field(default=None, max_length=8000)

    @model_validator(mode="after")
    def validate_next_page(self) -> AzurePriceQuery:
        if self.next_page_url:
            parsed = urlparse(self.next_page_url)
            if parsed.scheme != "https" or parsed.hostname != "prices.azure.com":
                raise ValueError("next_page_url must use https://prices.azure.com")
        return self


class OciPriceQuery(StrictModel):
    provider: Literal["oci"] = "oci"
    query_id: str = Field(min_length=1, max_length=100)
    part_number: str | None = Field(default=None, min_length=1, max_length=120)
    currency_code: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    response_filters: dict[str, str] = Field(default_factory=dict, max_length=12)

    @model_validator(mode="after")
    def validate_response_filters(self) -> OciPriceQuery:
        _validate_objective_response_filters(self.response_filters)
        return self


class GcpPriceQuery(StrictModel):
    provider: Literal["gcp"] = "gcp"
    query_id: str = Field(min_length=1, max_length=100)
    operation: Literal["list_services", "list_skus"]
    service_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=240,
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    page_size: int = Field(default=5000, ge=1, le=5000)
    page_token: str | None = Field(default=None, max_length=4000)
    currency_code: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    response_filters: dict[str, str] = Field(default_factory=dict, max_length=12)
    max_pages: int = Field(default=8, ge=1, le=20)

    @model_validator(mode="after")
    def validate_operation(self) -> GcpPriceQuery:
        if self.operation == "list_skus" and not self.service_id:
            raise ValueError("list_skus requires service_id")
        if self.operation == "list_services" and self.service_id:
            raise ValueError("list_services does not accept service_id")
        _validate_objective_response_filters(self.response_filters)
        return self


PriceQueryInput = Annotated[
    AwsPriceQuery | AzurePriceQuery | OciPriceQuery | GcpPriceQuery,
    Field(discriminator="provider"),
]
# Backward-compatible class name for callers that construct an AWS query
# directly. The batch schema itself is provider-discriminated.
PriceQuery = AwsPriceQuery


class GetPricesRequest(StrictModel):
    queries: list[PriceQueryInput] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_query_ids(self) -> GetPricesRequest:
        query_ids = [query.query_id for query in self.queries]
        if len(set(query_ids)) != len(query_ids):
            raise ValueError("query_id must be unique within one batch")
        return self


class OfficialPricingService:
    """Thin dispatcher for official cloud price catalogs.

    Caller-supplied query parameters are sent to the selected provider and the
    official response is preserved. This layer never chooses a SKU, converts
    usage or calculates a quote.
    """

    AZURE_URL = "https://prices.azure.com/api/retail/prices"
    OCI_URL = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/"
    GCP_BASE_URL = "https://cloudbilling.googleapis.com/v1"
    MAX_RETURNED_CANDIDATES = 10

    def __init__(
        self,
        executor: ReadOnlyAwsQueryExecutor,
        *,
        http_get: Any = httpx.get,
        gcp_api_key: str | None = None,
    ) -> None:
        self._executor = executor
        self._http_get = http_get
        self._gcp_api_key = (
            os.getenv("GCP_BILLING_API_KEY", "")
            if gcp_api_key is None
            else gcp_api_key
        )

    def catalog_availability(self) -> dict[str, dict[str, Any]]:
        return {
            "aws": {"available": True},
            "azure": {"available": True},
            "oci": {"available": True},
            "gcp": {
                "available": bool(self._gcp_api_key),
                "message": (
                    "Google Cloud 官方价格接口可用"
                    if self._gcp_api_key
                    else "Google Cloud 官方价格接口待配置 API Key"
                ),
            },
        }

    def describe_service(self, request: DescribeServiceRequest) -> dict[str, Any]:
        payload = self._executor.execute(
            service="pricing",
            operation="describe_services",
            region="us-east-1",
            parameters={"ServiceCode": request.service_code},
            max_items=100,
        )
        services = _collect(payload, "Services")
        return {
            "status": _identity_status(len(services)),
            "provider": "aws",
            "service_code": request.service_code,
            "services": services,
            "source": "AWS Price List API",
        }

    def get_attribute_values(self, request: AttributeValuesRequest) -> dict[str, Any]:
        payload = self._executor.execute(
            service="pricing",
            operation="get_attribute_values",
            region="us-east-1",
            parameters={
                "ServiceCode": request.service_code,
                "AttributeName": request.attribute_name,
            },
            max_items=request.max_results,
        )
        values = _collect(payload, "AttributeValues")
        return {
            "status": "found" if values else "not_found",
            "provider": "aws",
            "service_code": request.service_code,
            "attribute_name": request.attribute_name,
            "values": values,
            "source": "AWS Price List API",
        }

    def search_products(self, request: ProductSearchRequest) -> dict[str, Any]:
        products = self._price_list_products(request)
        return {
            "status": _identity_status(len(products)),
            "provider": "aws",
            "service_code": request.service_code,
            "region": request.region,
            "product_count": len(products),
            "products": [_product_identity(product) for product in products],
            "source": "AWS Price List API",
        }

    def get_prices(self, request: GetPricesRequest) -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=min(8, len(request.queries))) as pool:
            results = list(pool.map(self._safe_price_result, request.queries))
        return {"status": "completed", "result_count": len(results), "results": results}

    def _safe_price_result(self, query: PriceQueryInput) -> dict[str, Any]:
        try:
            return self._get_price_result(query)
        except Exception as exc:
            return {
                "query_id": query.query_id,
                "provider": query.provider,
                "status": "query_failed",
                "code": getattr(exc, "code", None) or "official_catalog_query_failed",
                "message": str(exc),
                "official_item_ids": [],
            }

    def _get_price_result(self, query: PriceQueryInput) -> dict[str, Any]:
        if isinstance(query, AwsPriceQuery):
            result = (
                self._get_aws_on_demand(query)
                if query.pricing_model == "on_demand"
                else self._get_aws_reserved(query)
            )
        elif isinstance(query, AzurePriceQuery):
            result = self._get_azure_prices(query)
        elif isinstance(query, OciPriceQuery):
            result = self._get_oci_prices(query)
        else:
            result = self._get_gcp_catalog(query)
        identified = {"query_id": query.query_id, "provider": query.provider, **result}
        narrowed = _require_refinement_for_large_result(
            query,
            identified,
            maximum=self.MAX_RETURNED_CANDIDATES,
        )
        if narrowed.get("status") != "needs_refinement":
            narrowed["official_rate_candidates"] = _official_rate_candidates(
                query.provider,
                narrowed,
            )
        return narrowed

    def _price_list_products(self, request: ProductSearchRequest) -> list[dict[str, Any]]:
        filters = [
            {"Type": "TERM_MATCH", "Field": field, "Value": str(value)}
            for field, value in sorted(request.filters.items())
        ]
        if request.region.casefold() != "global" and "regionCode" not in request.filters:
            filters.append(
                {"Type": "TERM_MATCH", "Field": "regionCode", "Value": request.region}
            )
        payload = self._executor.execute(
            service="pricing",
            operation="get_products",
            region="us-east-1",
            parameters={"ServiceCode": request.service_code, "Filters": filters},
            max_items=request.max_results,
        )
        products: list[dict[str, Any]] = []
        for item in _collect(payload, "PriceList"):
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except json.JSONDecodeError:
                    continue
            if isinstance(item, dict):
                products.append(item)
        return products

    def _get_aws_on_demand(self, query: AwsPriceQuery) -> dict[str, Any]:
        normalized = [
            _priced_product(product, term_key="OnDemand")
            for product in self._price_list_products(query)
        ]
        return {
            "status": _identity_status(len(normalized)),
            "pricing_model": "on_demand",
            "service_code": query.service_code,
            "region": query.region,
            "product_count": len(normalized),
            "price_dimension_count": sum(
                len(product["price_dimensions"]) for product in normalized
            ),
            "official_item_ids": [str(item["sku"]) for item in normalized if item.get("sku")],
            "products": normalized,
            "source": "AWS Price List API",
        }

    def _get_aws_reserved(self, query: AwsPriceQuery) -> dict[str, Any]:
        normalized: list[dict[str, Any]] = []
        for product in self._price_list_products(query):
            priced = _priced_product(product, term_key="Reserved")
            matching_terms = [
                term for term in priced["terms"] if _reserved_term_matches(term, query)
            ]
            if not matching_terms:
                continue
            priced["terms"] = matching_terms
            priced["price_dimensions"] = [
                dimension for term in matching_terms for dimension in term["price_dimensions"]
            ]
            normalized.append(priced)
        term_count = sum(len(product["terms"]) for product in normalized)
        status = "not_found" if not normalized else (
            "exact" if len(normalized) == 1 and term_count == 1 else "ambiguous"
        )
        return {
            "status": status,
            "pricing_model": "reserved",
            "term_years": query.term_years,
            "payment_option": query.payment_option,
            "offering_class": query.offering_class,
            "service_code": query.service_code,
            "region": query.region,
            "product_count": len(normalized),
            "term_count": term_count,
            "price_dimension_count": sum(
                len(product["price_dimensions"]) for product in normalized
            ),
            "official_item_ids": [str(item["sku"]) for item in normalized if item.get("sku")],
            "products": normalized,
            "source": "AWS Price List API",
        }

    def _get_azure_prices(self, query: AzurePriceQuery) -> dict[str, Any]:
        url = query.next_page_url or self.AZURE_URL
        params: dict[str, Any] = {} if query.next_page_url else {
            "api-version": query.api_version,
            "currencyCode": query.currency_code,
        }
        if query.filter and not query.next_page_url:
            params["$filter"] = query.filter
        payload = self._official_json(url, params=params)
        items = payload.get("Items") if isinstance(payload.get("Items"), list) else []
        item_ids = [_azure_item_id(item) for item in items if isinstance(item, dict)]
        return {
            "status": _identity_status(len(item_ids)),
            "official_item_ids": item_ids,
            "items": items,
            "next_page_url": payload.get("NextPageLink"),
            "currency": query.currency_code,
            "source": "Azure Retail Prices API",
        }

    def _get_oci_prices(self, query: OciPriceQuery) -> dict[str, Any]:
        params: dict[str, Any] = {"currencyCode": query.currency_code}
        if query.part_number:
            params["partNumber"] = query.part_number
        payload = self._official_json(self.OCI_URL, params=params)
        raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
        items = _filter_official_candidates(raw_items, query.response_filters)
        item_ids = [
            str(item.get("partNumber"))
            for item in items
            if isinstance(item, dict) and item.get("partNumber")
        ]
        return {
            "status": _identity_status(len(item_ids)),
            "official_item_ids": item_ids,
            "items": items,
            "response_filters": query.response_filters,
            "currency": query.currency_code,
            "source": "Oracle Cloud Price List API",
        }

    def _get_gcp_catalog(self, query: GcpPriceQuery) -> dict[str, Any]:
        if not self._gcp_api_key:
            raise OfficialCatalogQueryError(
                "GCP_BILLING_API_KEY is not configured",
                code="gcp_api_key_not_configured",
            )
        if query.operation == "list_services":
            url = f"{self.GCP_BASE_URL}/services"
            response_key = "services"
        else:
            url = f"{self.GCP_BASE_URL}/services/{quote(query.service_id or '', safe='')}/skus"
            response_key = "skus"
        items: list[Any] = []
        page_token = query.page_token
        pages_scanned = 0
        scanned_item_count = 0
        while True:
            params: dict[str, Any] = {
                "key": self._gcp_api_key,
                "pageSize": query.page_size,
            }
            if query.operation == "list_skus":
                params["currencyCode"] = query.currency_code
            if page_token:
                params["pageToken"] = page_token
            payload = self._official_json(url, params=params)
            page_items = (
                payload.get(response_key)
                if isinstance(payload.get(response_key), list)
                else []
            )
            scanned_item_count += len(page_items)
            items.extend(_filter_official_candidates(page_items, query.response_filters))
            pages_scanned += 1
            page_token = str(payload.get("nextPageToken") or "") or None
            if not page_token or not query.response_filters or pages_scanned >= query.max_pages:
                break
        item_ids = [
            str(item.get("skuId") or item.get("serviceId") or item.get("name"))
            for item in items
            if isinstance(item, dict)
            and (item.get("skuId") or item.get("serviceId") or item.get("name"))
        ]
        return {
            "status": _identity_status(len(item_ids)),
            "operation": query.operation,
            "official_item_ids": item_ids,
            response_key: items,
            "next_page_token": page_token,
            "response_filters": query.response_filters,
            "pages_scanned": pages_scanned,
            "scanned_item_count": scanned_item_count,
            "currency": query.currency_code,
            "source": "Google Cloud Billing Catalog API",
        }

    def _official_json(self, url: str, *, params: dict[str, Any]) -> dict[str, Any]:
        response = self._http_get(url, params=params, timeout=30.0)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Official catalog returned a non-object JSON payload")
        return payload


def _collect(payload: dict[str, Any], key: str) -> list[Any]:
    values: list[Any] = []
    pages = payload.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if isinstance(page, dict) and isinstance(page.get(key), list):
                values.extend(page[key])
    elif isinstance(payload.get(key), list):
        values.extend(payload[key])
    return values


def _identity_status(count: int) -> str:
    if count == 0:
        return "not_found"
    if count == 1:
        return "exact"
    return "ambiguous"


def _validate_objective_response_filters(filters: dict[str, str]) -> None:
    for field, value in filters.items():
        parts = field.split(".")
        if (
            not parts
            or any(
                not part or len(part) > 120 or not part.replace("_", "").isalnum()
                for part in parts
            )
            or len(field) > 360
        ):
            raise ValueError("response_filters keys must be dotted official JSON field paths")
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise ValueError("response_filters values must be non-empty strings")


def _candidate_field(candidate: dict[str, Any], field: str) -> Any:
    current: Any = candidate
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _filter_official_candidates(
    candidates: list[Any],
    filters: dict[str, str],
) -> list[Any]:
    if not filters:
        return list(candidates)
    return [
        candidate
        for candidate in candidates
        if isinstance(candidate, dict)
        and all(
            str(_candidate_field(candidate, field)) == expected
            for field, expected in filters.items()
        )
    ]


def _flatten_scalar_fields(value: Any, *, prefix: str = "") -> dict[str, set[str]]:
    fields: dict[str, set[str]] = {}
    if isinstance(value, dict):
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            child_fields = _flatten_scalar_fields(child, prefix=name)
            for field, values in child_fields.items():
                fields.setdefault(field, set()).update(values)
    elif isinstance(value, (str, int, float, bool)):
        text = str(value).strip()
        if text and len(text) <= 160:
            fields[prefix] = {text}
    return fields


def _objective_refinement_fields(
    candidates: list[dict[str, Any]],
    *,
    excluded: set[str],
) -> list[dict[str, Any]]:
    collected: dict[str, set[str]] = {}
    for candidate in candidates:
        for field, values in _flatten_scalar_fields(candidate).items():
            short_field = field.removeprefix("attributes.")
            if field in excluded or short_field in excluded:
                continue
            collected.setdefault(field, set()).update(values)
    ranked = sorted(
        (
            (field, sorted(values))
            for field, values in collected.items()
            if values
        ),
        key=lambda item: (len(item[1]), item[0]),
    )
    return [
        {
            "field": field,
            "candidate_values": values[:10],
            "value_count": len(values),
        }
        for field, values in ranked[:12]
    ]


def _require_refinement_for_large_result(
    query: PriceQueryInput,
    result: dict[str, Any],
    *,
    maximum: int,
) -> dict[str, Any]:
    candidate_key = next(
        (
            key
            for key in ("products", "items", "services", "skus")
            if isinstance(result.get(key), list)
        ),
        None,
    )
    candidates = list(result.get(candidate_key) or []) if candidate_key else []
    more_available = bool(result.get("next_page_url") or result.get("next_page_token"))
    if len(candidates) <= maximum and not more_available:
        return result

    excluded = set()
    filters = getattr(query, "filters", None)
    if isinstance(filters, dict):
        excluded.update(str(key) for key in filters)
    response_filters = getattr(query, "response_filters", None)
    if isinstance(response_filters, dict):
        excluded.update(str(key) for key in response_filters)
    compact = {
        key: value
        for key, value in result.items()
        if key not in {
            "products",
            "items",
            "services",
            "skus",
            "official_item_ids",
            "official_rate_candidates",
        }
    }
    compact.update(
        {
            "status": "needs_refinement",
            "terminal": False,
            "next_action": "refine_query",
            "matched_count": len(candidates),
            "matched_count_is_lower_bound": more_available,
            "more_results_available": more_available,
            "query": query.model_dump(exclude_none=True),
            "refinement_fields": _objective_refinement_fields(
                [item for item in candidates if isinstance(item, dict)],
                excluded=excluded,
            ),
            "official_item_ids": [],
        }
    )
    return compact


def _azure_item_id(item: dict[str, Any]) -> str:
    identity = {
        key: item.get(key)
        for key in (
            "meterId",
            "type",
            "armSkuName",
            "skuName",
            "meterName",
            "productName",
            "unitOfMeasure",
            "tierMinimumUnits",
            "retailPrice",
            "reservationTerm",
            "effectiveStartDate",
            "currencyCode",
        )
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:20]
    return f"azure:{item.get('meterId') or 'item'}:{digest}"


def _price_text(value: Any) -> str | None:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not decimal.is_finite():
        return None
    return format(decimal, "f")


def _rate_candidate(
    provider: str,
    official_item_id: str,
    *,
    unit_price: Any,
    currency: str,
    unit: Any = None,
    pricing_model: Any = None,
    description: Any = None,
    tier_start: Any = None,
    tier_end: Any = None,
    source_identity: Any = None,
) -> dict[str, Any] | None:
    price = _price_text(unit_price)
    if price is None:
        return None
    identity = {
        "provider": provider,
        "official_item_id": official_item_id,
        "currency": str(currency),
        "unit_price": price,
        "unit": None if unit is None else str(unit),
        "pricing_model": None if pricing_model is None else str(pricing_model),
        "tier_start": None if tier_start is None else str(tier_start),
        "tier_end": None if tier_end is None else str(tier_end),
        "source_identity": source_identity,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    return {
        "rate_id": f"{provider}:{official_item_id}:{digest}",
        "official_item_id": official_item_id,
        "unit_price": price,
        "currency": str(currency),
        "unit": None if unit is None else str(unit),
        "pricing_model": None if pricing_model is None else str(pricing_model),
        "description": None if description is None else str(description),
        "tier_start": None if tier_start is None else str(tier_start),
        "tier_end": None if tier_end is None else str(tier_end),
        "is_zero_rate": Decimal(price) == 0,
    }


def _official_rate_candidates(provider: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten official price dimensions without selecting or calculating them."""
    candidates: list[dict[str, Any]] = []
    if provider == "aws":
        for product in result.get("products") or []:
            if not isinstance(product, dict) or not product.get("sku"):
                continue
            item_id = str(product["sku"])
            for dimension in product.get("price_dimensions") or []:
                if not isinstance(dimension, dict):
                    continue
                prices = dimension.get("price_per_unit") or {}
                if not isinstance(prices, dict):
                    continue
                for currency, value in prices.items():
                    candidate = _rate_candidate(
                        provider,
                        item_id,
                        unit_price=value,
                        currency=str(currency),
                        unit=dimension.get("unit"),
                        description=dimension.get("description"),
                        tier_start=dimension.get("begin_range"),
                        tier_end=dimension.get("end_range"),
                        source_identity=(
                            dimension.get("rate_code") or dimension.get("dimension_code")
                        ),
                    )
                    if candidate:
                        candidates.append(candidate)
    elif provider == "azure":
        for item in result.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_id = _azure_item_id(item)
            candidate = _rate_candidate(
                provider,
                item_id,
                unit_price=item.get("retailPrice", item.get("unitPrice")),
                currency=str(item.get("currencyCode") or result.get("currency") or "USD"),
                unit=item.get("unitOfMeasure"),
                pricing_model=item.get("type"),
                description=item.get("meterName") or item.get("productName"),
                tier_start=item.get("tierMinimumUnits"),
                source_identity=item_id,
            )
            if candidate:
                candidates.append(candidate)
    elif provider == "oci":
        for item in result.get("items") or []:
            if not isinstance(item, dict) or not item.get("partNumber"):
                continue
            item_id = str(item["partNumber"])
            localizations = item.get("currencyCodeLocalizations")
            if not isinstance(localizations, list):
                localizations = [{
                    "currencyCode": result.get("currency") or "USD",
                    "prices": item.get("prices") or [],
                }]
            for localization in localizations:
                if not isinstance(localization, dict):
                    continue
                currency = str(localization.get("currencyCode") or result.get("currency") or "USD")
                for index, price in enumerate(localization.get("prices") or []):
                    if not isinstance(price, dict):
                        continue
                    candidate = _rate_candidate(
                        provider,
                        item_id,
                        unit_price=price.get("value"),
                        currency=currency,
                        unit=item.get("metricName"),
                        pricing_model=price.get("model"),
                        description=item.get("displayName"),
                        tier_start=price.get("rangeMin"),
                        tier_end=price.get("rangeMax"),
                        source_identity={"index": index, "price": price},
                    )
                    if candidate:
                        candidates.append(candidate)
    elif provider == "gcp":
        for sku in result.get("skus") or []:
            if not isinstance(sku, dict):
                continue
            item_id = str(sku.get("skuId") or sku.get("name") or "")
            if not item_id:
                continue
            for info_index, pricing_info in enumerate(sku.get("pricingInfo") or []):
                if not isinstance(pricing_info, dict):
                    continue
                expression = pricing_info.get("pricingExpression") or {}
                if not isinstance(expression, dict):
                    continue
                for tier_index, tier in enumerate(expression.get("tieredRates") or []):
                    if not isinstance(tier, dict):
                        continue
                    unit_price = tier.get("unitPrice") or {}
                    if not isinstance(unit_price, dict):
                        continue
                    try:
                        units = Decimal(str(unit_price.get("units") or "0"))
                        nanos = Decimal(str(unit_price.get("nanos") or "0"))
                        value = units + (nanos / Decimal("1000000000"))
                    except (InvalidOperation, TypeError, ValueError):
                        continue
                    candidate = _rate_candidate(
                        provider,
                        item_id,
                        unit_price=value,
                        currency=str(
                            unit_price.get("currencyCode")
                            or result.get("currency")
                            or "USD"
                        ),
                        unit=expression.get("usageUnit"),
                        description=(
                            sku.get("description")
                            or (sku.get("category") or {}).get("resourceFamily")
                        ),
                        tier_start=tier.get("startUsageAmount"),
                        source_identity={"pricing_info": info_index, "tier": tier_index},
                    )
                    if candidate:
                        candidates.append(candidate)
    return candidates


def _product_identity(payload: dict[str, Any]) -> dict[str, Any]:
    product = payload.get("product", {})
    return {
        "sku": product.get("sku"),
        "product_family": product.get("productFamily"),
        "attributes": product.get("attributes", {}),
        "service_code": payload.get("serviceCode"),
    }


def _priced_product(payload: dict[str, Any], *, term_key: str) -> dict[str, Any]:
    identity = _product_identity(payload)
    terms: list[dict[str, Any]] = []
    dimensions: list[dict[str, Any]] = []
    term_map = payload.get("terms", {}).get(term_key, {})
    if isinstance(term_map, dict):
        for term_code, term in term_map.items():
            if not isinstance(term, dict):
                continue
            term_dimensions: list[dict[str, Any]] = []
            price_dimensions = term.get("priceDimensions", {})
            if isinstance(price_dimensions, dict):
                for dimension_code, dimension in price_dimensions.items():
                    if not isinstance(dimension, dict):
                        continue
                    normalized = {
                        "dimension_code": dimension_code,
                        "rate_code": dimension.get("rateCode"),
                        "description": dimension.get("description"),
                        "begin_range": dimension.get("beginRange"),
                        "end_range": dimension.get("endRange"),
                        "unit": dimension.get("unit"),
                        "price_per_unit": dimension.get("pricePerUnit", {}),
                        "applies_to": dimension.get("appliesTo", []),
                    }
                    term_dimensions.append(normalized)
                    dimensions.append(normalized)
            terms.append(
                {
                    "term_code": term_code,
                    "offer_term_code": term.get("offerTermCode"),
                    "effective_date": term.get("effectiveDate"),
                    "term_attributes": term.get("termAttributes", {}),
                    "price_dimensions": term_dimensions,
                }
            )
    return {**identity, "terms": terms, "price_dimensions": dimensions}


def _reserved_term_matches(term: dict[str, Any], query: AwsPriceQuery) -> bool:
    attributes = term.get("term_attributes") or {}
    if attributes.get("LeaseContractLength") != f"{query.term_years}yr":
        return False
    if attributes.get("PurchaseOption") != _payment_label(query.payment_option):
        return False
    if query.offering_class is None:
        return True
    return str(attributes.get("OfferingClass") or "standard").casefold() == (
        query.offering_class.casefold()
    )


def _payment_label(payment_option: str | None) -> str:
    return {
        "no_upfront": "No Upfront",
        "partial_upfront": "Partial Upfront",
        "all_upfront": "All Upfront",
    }[payment_option or "no_upfront"]
