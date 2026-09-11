from __future__ import annotations

import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.aws_query_executor import ReadOnlyAwsQueryExecutor
from app.services.cloud_quote_profiles import active_market_profile
from app.services.official_cloud_clients import (
    OfficialCloudApiClient,
    OfficialCloudClientError,
)


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
    currency_code: str = Field(pattern=r"^[A-Z]{3}$")
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
    currency_code: str = Field(pattern=r"^[A-Z]{3}$")
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
    currency_code: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    response_filters: dict[str, str] = Field(default_factory=dict, max_length=12)
    max_pages: int = Field(default=8, ge=1, le=20)

    @model_validator(mode="after")
    def validate_operation(self) -> GcpPriceQuery:
        if self.operation == "list_skus" and not self.service_id:
            raise ValueError("list_skus requires service_id")
        if self.operation == "list_skus" and not self.currency_code:
            raise ValueError("list_skus requires currency_code")
        if self.operation == "list_services" and self.service_id:
            raise ValueError("list_services does not accept service_id")
        _validate_objective_response_filters(self.response_filters)
        return self


class CommercialRateField(StrictModel):
    """Caller-declared paths to a price value in one official response item.

    GPT identifies the fields from the provider's official API contract. The
    MCP only dereferences them and creates stable evidence identities.
    """

    unit_price_path: str = Field(min_length=1, max_length=360)
    item_id_path: str | None = Field(default=None, min_length=1, max_length=360)
    currency_code: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    currency_path: str | None = Field(default=None, min_length=1, max_length=360)
    unit: str | None = Field(default=None, min_length=1, max_length=120)
    unit_path: str | None = Field(default=None, min_length=1, max_length=360)
    description_path: str | None = Field(default=None, min_length=1, max_length=360)
    pricing_model_path: str | None = Field(default=None, min_length=1, max_length=360)
    tier_start_path: str | None = Field(default=None, min_length=1, max_length=360)
    tier_end_path: str | None = Field(default=None, min_length=1, max_length=360)

    @model_validator(mode="after")
    def validate_paths(self) -> CommercialRateField:
        if not self.currency_code and not self.currency_path:
            raise ValueError(
                "rate fields require currency_path or an official documented currency_code"
            )
        for value in (
            self.unit_price_path,
            self.item_id_path,
            self.currency_path,
            self.unit_path,
            self.description_path,
            self.pricing_model_path,
            self.tier_start_path,
            self.tier_end_path,
        ):
            if value is not None:
                _validate_response_path(value)
        return self


class AuthenticatedCatalogQuery(StrictModel):
    query_id: str = Field(min_length=1, max_length=100)
    endpoint: str = Field(min_length=4, max_length=255)
    service: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    action: str = Field(default="", max_length=160, pattern=r"^[A-Za-z0-9._-]*$")
    version: str | None = Field(default=None, min_length=1, max_length=40)
    region: str = Field(min_length=2, max_length=80)
    method: Literal["GET", "POST"] = "POST"
    path: str = Field(default="/", min_length=1, max_length=1000)
    region_parameter: str | None = Field(
        default=None,
        pattern=r"^(?:none|[A-Za-z][A-Za-z0-9_.-]{0,119})$",
    )
    query_parameters: dict[str, Any] = Field(default_factory=dict, max_length=100)
    body: dict[str, Any] = Field(default_factory=dict, max_length=200)
    response_items_path: str | None = Field(default=None, min_length=1, max_length=360)
    response_filters: dict[str, str] = Field(default_factory=dict, max_length=12)
    item_id_paths: list[str] = Field(default_factory=list, max_length=12)
    rate_fields: list[CommercialRateField] = Field(default_factory=list, max_length=24)
    next_page_path: str | None = Field(default=None, min_length=1, max_length=360)
    official_source_url: str | None = Field(default=None, min_length=8, max_length=2000)
    sdk_version: str | None = Field(default=None, min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_official_request(self) -> AuthenticatedCatalogQuery:
        _validate_authenticated_catalog_query(self)
        _validate_objective_response_filters(self.response_filters)
        for path in self.item_id_paths:
            _validate_response_path(path)
        for path in (self.response_items_path, self.next_page_path):
            if path is not None:
                _validate_response_path(path)
        return self


class TencentPriceQuery(AuthenticatedCatalogQuery):
    provider: Literal["tencent"] = "tencent"


class AlibabaPriceQuery(AuthenticatedCatalogQuery):
    provider: Literal["alibaba"] = "alibaba"


class HuaweiPriceQuery(AuthenticatedCatalogQuery):
    provider: Literal["huawei"] = "huawei"


class BaiduPriceQuery(AuthenticatedCatalogQuery):
    provider: Literal["baidu"] = "baidu"


class VolcenginePriceQuery(AuthenticatedCatalogQuery):
    provider: Literal["volcengine"] = "volcengine"


class CtyunPriceQuery(AuthenticatedCatalogQuery):
    provider: Literal["ctyun"] = "ctyun"


PriceQueryInput = Annotated[
    AwsPriceQuery
    | AzurePriceQuery
    | OciPriceQuery
    | GcpPriceQuery
    | TencentPriceQuery
    | AlibabaPriceQuery
    | HuaweiPriceQuery
    | BaiduPriceQuery
    | VolcenginePriceQuery
    | CtyunPriceQuery,
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


AUTHENTICATED_PROVIDERS = (
    "tencent",
    "alibaba",
    "huawei",
    "baidu",
    "volcengine",
    "ctyun",
)

PROVIDER_SOURCE_LABELS = {
    "tencent": "Tencent Cloud official API",
    "alibaba": "Alibaba Cloud official API",
    "huawei": "Huawei Cloud official API",
    "baidu": "Baidu AI Cloud official API",
    "volcengine": "Volcengine official API",
    "ctyun": "CTyun official API",
}

_PROVIDER_CREDENTIAL_ENV = {
    "tencent": ("TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY"),
    "alibaba": (
        "ALIBABA_CLOUD_ACCESS_KEY_ID",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
    ),
    "huawei": ("HUAWEICLOUD_ACCESS_KEY", "HUAWEICLOUD_SECRET_KEY"),
    "baidu": ("BAIDUCLOUD_ACCESS_KEY_ID", "BAIDUCLOUD_SECRET_ACCESS_KEY"),
    "volcengine": ("VOLCENGINE_ACCESS_KEY", "VOLCENGINE_SECRET_KEY"),
    "ctyun": ("CTYUN_ACCESS_KEY", "CTYUN_SECRET_KEY"),
}

_PROVIDER_OFFICIAL_SUFFIXES = {
    "tencent": (".tencentcloudapi.com",),
    "alibaba": (".aliyuncs.com",),
    "huawei": (".myhuaweicloud.com", ".huaweicloud.com"),
    "baidu": (".baidubce.com",),
    "volcengine": (".volcengineapi.com",),
    "ctyun": (".ctyun.cn",),
}

_PROVIDER_OFFICIAL_SOURCE_SUFFIXES = {
    "tencent": (".tencentcloudapi.com", ".tencentcloud.com", ".tencent.com"),
    "alibaba": (".aliyuncs.com", ".aliyun.com"),
    "huawei": (".myhuaweicloud.com", ".huaweicloud.com"),
    "baidu": (".baidubce.com", ".baidu.com"),
    "volcengine": (".volcengineapi.com", ".volcengine.com"),
    "ctyun": (".ctyun.cn",),
}

_PROVIDER_AUTH_SCHEMES = {
    "tencent": "tc3_hmac_sha256",
    "alibaba": "alibaba_rpc_hmac_sha1",
    "huawei": "huawei_sdk_hmac_sha256",
    "baidu": "bce_auth_v1_hmac_sha256",
    "volcengine": "volcengine_hmac_sha256",
    "ctyun": "ctyun_eop_hmac_sha256",
}

_SAFE_ACTION = re.compile(
    r"^(?:describe|list|get|query|inquiry|inquire|check|search|show|batchquery)",
    re.IGNORECASE,
)
_SAFE_REST_PATH = re.compile(
    r"(?:price|pricing|inquiry|rating|describe|query|list|flavou?r|sku|product|region|zone|spec)",
    re.IGNORECASE,
)
_MUTATING_ACTION = re.compile(
    r"^(?:batch)?(?:create|run|launch|start|stop|restart|reboot|execute|invoke|"
    r"apply|submit|change|update|modify|delete|remove|"
    r"terminate|purchase|buy|pay|renew|resize|upgrade|downgrade|allocate|"
    r"release|bind|unbind|attach|detach|enable|disable|reset|set)",
    re.IGNORECASE,
)
_MUTATING_REST_PATH = re.compile(
    r"/(?:batch[-_]?)?(?:create|run|launch|start|stop|restart|reboot|"
    r"execute|invoke|apply|submit|change|update|modify|delete|remove|"
    r"terminate|purchase|buy|pay|renew|resize|upgrade|downgrade|allocate|"
    r"release|bind|unbind|attach|detach|enable|disable|reset|set)(?:/|-|$)",
    re.IGNORECASE,
)
_SENSITIVE_PARAMETER = re.compile(
    r"(?:secret|password|private.?key|access.?key|authorization|security.?token)",
    re.IGNORECASE,
)


def _provider_credentials_from_environment() -> dict[str, dict[str, str]]:
    return {
        provider: {
            "access_key_id": os.getenv(access_key_env, ""),
            "secret_access_key": os.getenv(secret_key_env, ""),
        }
        for provider, (access_key_env, secret_key_env) in _PROVIDER_CREDENTIAL_ENV.items()
    }


def _credentials_available(credentials: dict[str, str]) -> bool:
    return bool(
        str(credentials.get("access_key_id") or "")
        and str(credentials.get("secret_access_key") or "")
    )


def _validate_response_path(path: str) -> None:
    if path.startswith("/"):
        parts = path.split("/")[1:]
        invalid_escape = any(re.search(r"~(?:[^01]|$)", part) for part in parts)
        invalid_control = any(any(ord(character) < 32 for character in part) for part in parts)
        if len(parts) > 20 or invalid_escape or invalid_control:
            raise ValueError(
                "official response paths must be dotted fields or RFC 6901 JSON Pointers"
            )
        return
    parts = path.split(".")
    if (
        not parts
        or len(parts) > 20
        or any(
            not part
            or len(part) > 120
            or not part.replace("_", "").replace("-", "").isalnum()
            for part in parts
        )
    ):
        raise ValueError(
            "official response paths must be dotted fields or RFC 6901 JSON Pointers"
        )


def _validate_authenticated_catalog_query(query: AuthenticatedCatalogQuery) -> None:
    endpoint = query.endpoint.strip().lower().rstrip(".")
    suffixes = _PROVIDER_OFFICIAL_SUFFIXES[query.provider]
    if (
        "://" in endpoint
        or "/" in endpoint
        or ":" in endpoint
        or not any(endpoint.endswith(suffix) for suffix in suffixes)
    ):
        raise ValueError(f"{query.provider} query must use an official endpoint")
    query.endpoint = endpoint
    if (
        not query.path.startswith("/")
        or ".." in query.path
        or "?" in query.path
        or "#" in query.path
        or "//" in query.path
    ):
        raise ValueError("official API path is invalid")
    if (
        (query.action and _MUTATING_ACTION.match(query.action))
        or _MUTATING_REST_PATH.search(query.path)
    ):
        raise ValueError("only official read-only discovery or price operations are allowed")
    if not (
        (query.action and _SAFE_ACTION.match(query.action))
        or _SAFE_REST_PATH.search(query.path)
        or query.official_source_url
    ):
        raise ValueError("only official read-only discovery or price operations are allowed")
    for key in (*query.query_parameters.keys(), *query.body.keys()):
        if _SENSITIVE_PARAMETER.search(str(key)):
            raise ValueError("credentials and authorization fields cannot be supplied by GPT")
    serialized_size = len(
        json.dumps(
            {"query": query.query_parameters, "body": query.body},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if serialized_size > 128 * 1024:
        raise ValueError("official API request exceeds the 128 KiB boundary")
    if query.official_source_url:
        parsed_source = urlparse(query.official_source_url)
        source_host = (parsed_source.hostname or "").casefold().rstrip(".")
        if (
            parsed_source.scheme != "https"
            or parsed_source.username
            or parsed_source.password
            or parsed_source.port not in {None, 443}
            or not any(
                source_host == suffix.removeprefix(".")
                or source_host.endswith(suffix)
                for suffix in _PROVIDER_OFFICIAL_SOURCE_SUFFIXES[query.provider]
            )
        ):
            # The official endpoint and read-only operation are independently
            # verified. A stale or cross-site documentation link is optional
            # metadata, so discard it and use the verified API URL as evidence
            # instead of rejecting every valid query in the batch.
            query.official_source_url = None


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
        authenticated_request: Any | None = None,
        provider_credentials: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self._executor = executor
        self._http_get = http_get
        self._gcp_api_key = (
            os.getenv("GCP_BILLING_API_KEY", "")
            if gcp_api_key is None
            else gcp_api_key
        )
        self._provider_credentials = (
            _provider_credentials_from_environment()
            if provider_credentials is None
            else provider_credentials
        )
        self._authenticated_request = authenticated_request or OfficialCloudApiClient(
            self._provider_credentials
        ).execute

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
            **{
                provider: {
                    "available": _credentials_available(
                        self._provider_credentials.get(provider) or {}
                    ),
                    "credential_configured": _credentials_available(
                        self._provider_credentials.get(provider) or {}
                    ),
                    "readiness": (
                        "configured_unverified"
                        if _credentials_available(
                            self._provider_credentials.get(provider) or {}
                        )
                        else "credentials_missing"
                    ),
                    "message": (
                        f"{PROVIDER_SOURCE_LABELS[provider]} 已配置，按产品动态验证"
                        if _credentials_available(
                            self._provider_credentials.get(provider) or {}
                        )
                        else f"{PROVIDER_SOURCE_LABELS[provider]}待配置访问密钥"
                    ),
                }
                for provider in AUTHENTICATED_PROVIDERS
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
        last_error: Exception | None = None
        category, retryable = "official_api_error", False
        for attempt in range(1, 4):
            try:
                result = self._get_price_result(query)
                if attempt > 1:
                    result["attempt_count"] = attempt
                return result
            except Exception as exc:
                last_error = exc
                category, retryable = _error_recovery_traits(exc)
                if (
                    attempt >= 3
                    or category
                    not in {"transport", "rate_limit", "provider_unavailable"}
                ):
                    break
        assert last_error is not None
        recovery = _recovery_plan(category, retryable)
        details = getattr(last_error, "details", {})
        return {
            "query_id": query.query_id,
            "provider": query.provider,
            "status": "query_failed",
            "terminal": not retryable,
            "retryable": retryable,
            "error_category": category,
            "code": getattr(last_error, "code", None)
            or "official_catalog_query_failed",
            "message": _safe_error_message(str(last_error)),
            "details": details if isinstance(details, dict) else {},
            "recovery": recovery,
            "route_fingerprint": (
                _route_fingerprint(query)
                if isinstance(query, AuthenticatedCatalogQuery)
                else None
            ),
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
        elif isinstance(query, GcpPriceQuery):
            result = self._get_gcp_catalog(query)
        else:
            result = self._get_authenticated_catalog(query)
        identified = {"query_id": query.query_id, "provider": query.provider, **result}
        narrowed = _require_refinement_for_large_result(
            query,
            identified,
            maximum=self.MAX_RETURNED_CANDIDATES,
        )
        if narrowed.get("status") != "needs_refinement":
            rate_candidates = _official_rate_candidates(
                query.provider,
                narrowed,
                query=query,
            )
            narrowed["official_rate_candidates"] = rate_candidates
            official_item_ids = list(narrowed.get("official_item_ids") or [])
            for candidate in rate_candidates:
                item_id = str(candidate.get("official_item_id") or "")
                if item_id and item_id not in official_item_ids:
                    official_item_ids.append(item_id)
            narrowed["official_item_ids"] = official_item_ids
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

    def _get_authenticated_catalog(
        self, query: AuthenticatedCatalogQuery
    ) -> dict[str, Any]:
        if not _credentials_available(
            self._provider_credentials.get(query.provider) or {}
        ):
            raise OfficialCatalogQueryError(
                f"{query.provider} official API credentials are not configured",
                code=f"{query.provider}_credentials_not_configured",
            )
        payload = self._authenticated_request(query)
        extracted: Any = (
            _candidate_field(payload, query.response_items_path)
            if query.response_items_path
            else payload
        )
        if isinstance(extracted, list):
            raw_items = extracted
        elif isinstance(extracted, dict):
            raw_items = [extracted]
        else:
            raw_items = []
        items = _filter_official_candidates(raw_items, query.response_filters)
        item_ids = [
            _authenticated_item_id(query.provider, item, query.item_id_paths)
            for item in items
            if isinstance(item, dict)
        ]
        next_page_token = (
            _candidate_field(payload, query.next_page_path)
            if query.next_page_path
            else None
        )
        return {
            "status": _identity_status(len(item_ids)),
            "operation": query.action or query.path,
            "official_item_ids": item_ids,
            "items": items,
            "response_filters": query.response_filters,
            "next_page_token": next_page_token,
            "source": PROVIDER_SOURCE_LABELS[query.provider],
            "route_verification": _route_verification(query, payload),
        }

    def _official_json(self, url: str, *, params: dict[str, Any]) -> dict[str, Any]:
        response = self._http_get(url, params=params, timeout=30.0)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Official catalog returned a non-object JSON payload")
        return payload


def _schema_shape(value: Any, *, depth: int = 0) -> Any:
    if depth >= 12:
        return "depth_limit"
    if isinstance(value, dict):
        return {
            str(key): _schema_shape(item, depth=depth + 1)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, list):
        shapes = {
            json.dumps(
                _schema_shape(item, depth=depth + 1),
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            for item in value[:20]
        }
        return [json.loads(item) for item in sorted(shapes)]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    return "string"


def _schema_hash(value: Any) -> str:
    encoded = json.dumps(
        _schema_shape(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _route_identity(query: AuthenticatedCatalogQuery) -> dict[str, Any]:
    market_profile = active_market_profile(query.provider)
    return {
        "provider": query.provider,
        "endpoint": query.endpoint,
        "service": query.service,
        "action": query.action,
        "version": query.version,
        "region": query.region,
        "market_profile": market_profile["market_profile"],
        "credential_scope": market_profile["credential_scope"],
        "region_parameter": query.region_parameter,
        "method": query.method,
        "path": query.path,
        "request_schema_hash": _schema_hash(
            {
                "query_parameters": query.query_parameters,
                "body": query.body,
            }
        ),
    }


def _route_fingerprint(query: AuthenticatedCatalogQuery) -> str:
    encoded = json.dumps(
        _route_identity(query),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _route_verification(
    query: AuthenticatedCatalogQuery, payload: dict[str, Any]
) -> dict[str, Any]:
    verified_at = datetime.now(UTC)
    source_url = query.official_source_url or (
        f"https://{query.endpoint}{query.path}"
    )
    return {
        **_route_identity(query),
        "route_contract_version": 3,
        "route_fingerprint": _route_fingerprint(query),
        "response_schema_hash": _schema_hash(payload),
        "response_items_path": query.response_items_path,
        "item_id_paths": list(query.item_id_paths),
        "rate_fields": [item.model_dump(mode="json") for item in query.rate_fields],
        "next_page_path": query.next_page_path,
        "auth_scheme": _PROVIDER_AUTH_SCHEMES[query.provider],
        "sdk_version": query.sdk_version or "astraquote-direct-signer/1",
        "last_verified_at": verified_at.isoformat(),
        "failure_count": 0,
        "confidence": 0.65,
        "revalidate_after": (verified_at + timedelta(days=7)).isoformat(),
        "expires_at": (verified_at + timedelta(days=30)).isoformat(),
        "official_source_url": source_url,
    }


def _safe_error_message(value: str) -> str:
    without_queries = re.sub(r"(https://[^?\s]+)\?[^\s]+", r"\1?[REDACTED]", value)
    without_secrets = re.sub(
        r"(?i)(accesskeyid|access[_-]?key|secret|signature|authorization)"
        r"(?:\s*[:=]\s*|%3[dD])[^&\s,}\]]+",
        lambda match: f"{match.group(1)}=[REDACTED]",
        without_queries,
    )
    return without_secrets[:800]


def _error_recovery_traits(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, OfficialCloudClientError):
        return exc.category, exc.retryable
    code = str(getattr(exc, "code", "") or "").casefold()
    message = str(exc).casefold()
    folded = f"{code} {message}"
    if any(token in folded for token in ("signaturedoesnotmatch", "invalidsignature")):
        return "request_signing", True
    if any(token in folded for token in ("timeout", "temporar", "connection", "tls", "ssl")):
        return "transport", True
    if any(token in folded for token in ("429", "throttl", "rate limit")):
        return "rate_limit", True
    if any(token in folded for token in ("400", "missing parameter", "invalid parameter")):
        return "invalid_request", True
    if any(token in folded for token in ("404", "not found", "unknown action")):
        return "route_not_found", True
    if any(token in folded for token in ("401", "403", "unauthor", "forbidden")):
        return "authorization", False
    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "response_schema", True
    return "official_api_error", False


def _recovery_plan(category: str, retryable: bool) -> dict[str, Any]:
    next_actions = {
        "invalid_request": "repair_official_request_schema",
        "route_not_found": "discover_alternate_official_route",
        "response_schema": "revalidate_official_response_schema",
        "transport": "retry_or_discover_official_endpoint",
        "rate_limit": "retry_with_backoff",
        "provider_unavailable": "retry_with_backoff",
        "request_signing": "revalidate_canonical_request_and_signing_contract",
        "authorization": "verify_cloud_read_and_billing_access",
        "credentials": "configure_official_api_credentials",
        "official_api_error": "inspect_official_error_contract",
    }
    return {
        "retryable": retryable,
        "next_action": next_actions.get(category, "inspect_official_error_contract"),
        "allowed_sources": [
            "official_documentation",
            "official_sdk",
            "official_openapi",
            "official_pricing_calculator",
        ],
        "third_party_price_forbidden": True,
        "mutating_api_forbidden": True,
    }


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
        _validate_response_path(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 500:
            raise ValueError("response_filters values must be non-empty strings")


def _candidate_field(candidate: Any, field: str | None) -> Any:
    if not field:
        return None
    current: Any = candidate
    parts = (
        [part.replace("~1", "/").replace("~0", "~") for part in field.split("/")[1:]]
        if field.startswith("/")
        else field.split(".")
    )
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return None
            current = current[index]
            continue
        else:
            return None
    return current


def _authenticated_item_id(
    provider: str,
    item: dict[str, Any],
    item_id_paths: list[str],
) -> str:
    identity_values = {
        path: _candidate_field(item, path)
        for path in item_id_paths
        if _candidate_field(item, path) is not None
    }
    if identity_values:
        return "|".join(str(value).strip() for value in identity_values.values())
    identity: Any = identity_values or item
    digest = hashlib.sha256(
        json.dumps(
            identity,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"{provider}:item:{digest}"


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


def _official_rate_candidates(
    provider: str,
    result: dict[str, Any],
    *,
    query: PriceQueryInput | None = None,
) -> list[dict[str, Any]]:
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
                currency=str(item.get("currencyCode") or result.get("currency") or ""),
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
                    "currencyCode": result.get("currency") or "",
                    "prices": item.get("prices") or [],
                }]
            for localization in localizations:
                if not isinstance(localization, dict):
                    continue
                currency = str(localization.get("currencyCode") or result.get("currency") or "")
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
                            or ""
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
    elif provider in AUTHENTICATED_PROVIDERS and isinstance(
        query, AuthenticatedCatalogQuery
    ):
        for item in result.get("items") or []:
            if not isinstance(item, dict):
                continue
            default_item_id = _authenticated_item_id(
                provider,
                item,
                query.item_id_paths,
            )
            for index, field in enumerate(query.rate_fields):
                item_id_value = _candidate_field(item, field.item_id_path)
                item_id = (
                    str(item_id_value).strip()
                    if item_id_value is not None and str(item_id_value).strip()
                    else default_item_id
                )
                currency = _candidate_field(item, field.currency_path)
                unit = _candidate_field(item, field.unit_path)
                candidate = _rate_candidate(
                    provider,
                    item_id,
                    unit_price=_candidate_field(item, field.unit_price_path),
                    currency=str(currency or field.currency_code),
                    unit=unit if unit is not None else field.unit,
                    pricing_model=_candidate_field(item, field.pricing_model_path),
                    description=_candidate_field(item, field.description_path),
                    tier_start=_candidate_field(item, field.tier_start_path),
                    tier_end=_candidate_field(item, field.tier_end_path),
                    source_identity={
                        "rate_field_index": index,
                        "unit_price_path": field.unit_price_path,
                    },
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
