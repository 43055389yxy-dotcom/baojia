from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from botocore.loaders import create_loader

from app.core.config import Settings
from app.core.errors import ManualConfirmationRequired
from app.integrations.aws_cache import PersistentAwsCache
from app.integrations.aws_public_catalog import PublicAwsPriceCatalog

RETRY_CONFIG = Config(
    connect_timeout=5,
    read_timeout=20,
    retries={"max_attempts": 2, "mode": "adaptive"},
)


@dataclass(frozen=True, slots=True)
class AwsClients:
    session: boto3.Session
    pricing: Any
    bcm: Any
    ssm: Any

    @classmethod
    def from_settings(cls, settings: Settings) -> AwsClients:
        credentials: dict[str, str] = {}
        if settings.aws_access_key_id and settings.aws_secret_access_key:
            credentials = {
                "aws_access_key_id": settings.aws_access_key_id,
                "aws_secret_access_key": settings.aws_secret_access_key,
            }
            if settings.aws_session_token:
                credentials["aws_session_token"] = settings.aws_session_token
        session = boto3.Session(
            region_name=settings.aws_default_region,
            **credentials,
        )
        return cls(
            session=session,
            pricing=session.client(
                "pricing", region_name=settings.aws_pricing_region, config=RETRY_CONFIG
            ),
            bcm=session.client(
                "bcm-pricing-calculator", region_name="us-east-1", config=RETRY_CONFIG
            ),
            ssm=session.client("ssm", region_name="us-east-1", config=RETRY_CONFIG),
        )

    def regional(self, service: str, region: str) -> Any:
        return self.session.client(service, region_name=region, config=RETRY_CONFIG)


@dataclass(frozen=True, slots=True)
class ReservedPrice:
    monthly_amortized: float
    upfront: float
    hourly_recurring: float
    term_code: str


class RegionResolver:
    """Resolves region codes from AWS public infrastructure parameters."""

    def __init__(self, clients: AwsClients):
        self._ssm = clients.ssm
        self._cache: dict[str, str] = {}
        # Quote previews validate many components concurrently.  Without a
        # single-flight boundary every component can issue the same SSM region
        # lookup before the first response populates the cache; one transient
        # timeout then appears as an unrelated service specification failure.
        self._cache_lock = threading.Lock()

    def long_name(self, region: str) -> str:
        if region in self._cache:
            return self._cache[region]
        with self._cache_lock:
            if region in self._cache:
                return self._cache[region]
            parameter_name = f"/aws/service/global-infrastructure/regions/{region}/longName"
            try:
                response = self._ssm.get_parameter(Name=parameter_name)
                value = response["Parameter"]["Value"]
            except (ClientError, BotoCoreError, KeyError) as exc:
                # Botocore ships AWS's signed endpoint metadata with boto3.
                # It is an official, read-only fallback when the local AWS
                # credentials cannot call the public SSM parameter.
                value = self._bundled_region_name(region)
                if value is None:
                    raise ManualConfirmationRequired(
                        f"AWS 官方区域目录无法确认区域 {region}",
                        code="unsupported_or_unknown_region",
                        region=region,
                    ) from exc
            self._cache[region] = value
            return value

    @staticmethod
    def _bundled_region_name(region: str) -> str | None:
        endpoints = create_loader().load_data("endpoints")
        for partition in endpoints.get("partitions", []):
            metadata = partition.get("regions", {}).get(region)
            if not isinstance(metadata, dict):
                continue
            description = str(metadata.get("description") or "").strip()
            if description:
                return description
        return None


class PricingCatalog:
    """Thin fail-closed wrapper over the AWS Price List Query API."""

    def __init__(
        self,
        clients: AwsClients,
        regions: RegionResolver,
        public_catalog: PublicAwsPriceCatalog | None = None,
    ):
        self._pricing = clients.pricing
        self._regions = regions
        self._public_catalog = public_catalog or PublicAwsPriceCatalog()
        self._query_api_available: bool | None = None
        self._persistent_cache = PersistentAwsCache()
        self._product_cache: dict[
            tuple[str, tuple[tuple[str, str], ...], int], list[dict[str, Any]]
        ] = {}

    def location(self, region: str) -> str:
        return self._regions.long_name(region)

    def products(
        self,
        service_code: str,
        filters: dict[str, str],
        *,
        max_pages: int = 20,
        refresh: bool = False,
    ) -> list[dict[str, Any]]:
        cache_key = (service_code, tuple(sorted(filters.items())), max_pages)
        if not refresh and cache_key in self._product_cache:
            return self._product_cache[cache_key]
        persistent_key = self._persistent_cache.key("pricing-products", cache_key)
        stale = self._persistent_cache.get(persistent_key, allow_stale=True)
        if not refresh:
            persistent = self._persistent_cache.get(persistent_key)
            # Never keep an empty official response alive for the full cache
            # TTL. Empty responses can be transient, or can reflect an AWS
            # catalog label that changed after an adapter was released.
            if isinstance(persistent, list) and persistent:
                self._product_cache[cache_key] = persistent
                return persistent
        api_filters = [
            {"Type": "TERM_MATCH", "Field": field, "Value": value}
            for field, value in filters.items()
        ]
        products: list[dict[str, Any]] = []
        if self._query_api_available is False:
            try:
                products = self._public_catalog.products(
                    service_code,
                    filters,
                    max_items=max_pages * 100,
                )
            except ManualConfirmationRequired:
                products = []
            if products:
                self._product_cache[cache_key] = products
                self._persistent_cache.set(persistent_key, products)
                return products
            if isinstance(stale, list) and stale:
                self._product_cache[cache_key] = stale
                return stale
            raise ManualConfirmationRequired(
                "AWS 官方公开价格目录查询失败，禁止继续报价",
                code="pricing_catalog_unavailable",
                service_code=service_code,
            )
        try:
            paginator = self._pricing.get_paginator("get_products")
            pages: Iterator[dict[str, Any]] = paginator.paginate(
                ServiceCode=service_code,
                FormatVersion="aws_v1",
                Filters=api_filters,
                PaginationConfig={"PageSize": 100, "MaxItems": max_pages * 100},
            )
            for page in pages:
                products.extend(json.loads(raw) for raw in page.get("PriceList", []))
        except (ClientError, BotoCoreError, ValueError) as exc:
            if self._is_auth_failure(exc):
                self._query_api_available = False
            try:
                products = self._public_catalog.products(
                    service_code,
                    filters,
                    max_items=max_pages * 100,
                )
            except ManualConfirmationRequired:
                products = []
            if products:
                self._product_cache[cache_key] = products
                self._persistent_cache.set(persistent_key, products)
                return products
            if isinstance(stale, list) and stale:
                self._product_cache[cache_key] = stale
                return stale
            raise ManualConfirmationRequired(
                "AWS Price List API 查询失败，禁止继续报价",
                code="pricing_catalog_unavailable",
                service_code=service_code,
            ) from exc
        else:
            self._query_api_available = True
        # One empty response must not immediately become a component failure.
        # Retry every adapter's exact official query once without cache before
        # the adapter decides whether broader semantic discovery is needed.
        if not products and not refresh:
            return self.products(
                service_code,
                filters,
                max_pages=max_pages,
                refresh=True,
            )
        if not products and isinstance(stale, list) and stale:
            products = stale
        if products:
            self._product_cache[cache_key] = products
            self._persistent_cache.set(persistent_key, products)
        return products

    def matching_products(
        self,
        service_code: str,
        filters: dict[str, str],
        predicate: Callable[[dict[str, str]], bool],
        *,
        max_pages: int = 20,
        fallback_filters: dict[str, str] | None = None,
        fallback_predicate: Callable[[dict[str, str]], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """Find official products with refresh and optional schema discovery.

        The adapter supplies business semantics while this common layer owns
        cache recovery.  It never guesses a SKU: every returned record is an
        exact product from AWS Price List and still has to pass the adapter's
        predicate.
        """

        def query(
            query_filters: dict[str, str],
            selector: Callable[[dict[str, str]], bool],
            *,
            refresh: bool,
        ) -> list[dict[str, Any]]:
            return [
                product
                for product in self.products(
                    service_code,
                    query_filters,
                    max_pages=max_pages,
                    refresh=refresh,
                )
                if selector(self.attributes(product))
            ]

        matches = query(filters, predicate, refresh=False)
        if matches:
            return matches
        matches = query(filters, predicate, refresh=True)
        if matches or fallback_filters is None:
            return matches
        return query(
            fallback_filters,
            fallback_predicate or predicate,
            refresh=True,
        )

    def attribute_values(
        self, service_code: str, attribute_name: str, *, max_pages: int = 5
    ) -> list[str]:
        persistent_key = self._persistent_cache.key(
            "pricing-attributes", (service_code, attribute_name, max_pages)
        )
        persistent = self._persistent_cache.get(persistent_key)
        if isinstance(persistent, list):
            return [str(item) for item in persistent]
        if self._query_api_available is False:
            values = self._public_catalog.attribute_values(
                service_code, attribute_name
            )
            self._persistent_cache.set(persistent_key, values)
            return values
        values: list[str] = []
        token: str | None = None
        for _ in range(max_pages):
            kwargs: dict[str, Any] = {
                "ServiceCode": service_code,
                "AttributeName": attribute_name,
                "MaxResults": 100,
            }
            if token:
                kwargs["NextToken"] = token
            try:
                response = self._pricing.get_attribute_values(**kwargs)
            except (ClientError, BotoCoreError) as exc:
                if self._is_auth_failure(exc):
                    self._query_api_available = False
                try:
                    values = self._public_catalog.attribute_values(
                        service_code, attribute_name
                    )
                except ManualConfirmationRequired as public_exc:
                    raise ManualConfirmationRequired(
                        "AWS Price List 无法读取产品属性值",
                        code="pricing_attribute_values_unavailable",
                        service_code=service_code,
                        attribute=attribute_name,
                    ) from public_exc
                break
            values.extend(item["Value"] for item in response.get("AttributeValues", []))
            token = response.get("NextToken")
            if not token:
                break
        self._persistent_cache.set(persistent_key, values)
        return values

    def service_codes(self) -> list[str]:
        """Return the official Price List service-code registry."""

        # v2 includes the unsigned AWS Bulk Offer registry as well as the
        # credentialed Query API.  The previous cache could contain only the
        # handful of service codes touched by one local AWS account.
        persistent_key = self._persistent_cache.key("pricing-service-codes-v2", "all")
        persistent = self._persistent_cache.get(persistent_key)
        if isinstance(persistent, list):
            return [str(item) for item in persistent]
        codes: list[str] = []
        token: str | None = None
        try:
            while True:
                kwargs: dict[str, Any] = {"MaxResults": 100}
                if token:
                    kwargs["NextToken"] = token
                response = self._pricing.describe_services(**kwargs)
                codes.extend(
                    str(item["ServiceCode"])
                    for item in response.get("Services", [])
                    if item.get("ServiceCode")
                )
                token = response.get("NextToken")
                if not token:
                    break
        except (ClientError, BotoCoreError, KeyError) as exc:
            if self._is_auth_failure(exc):
                self._query_api_available = False
            try:
                codes = self._public_catalog.service_codes()
            except ManualConfirmationRequired as public_exc:
                raise ManualConfirmationRequired(
                    "AWS Price List 无法读取服务目录",
                    code="pricing_service_registry_unavailable",
                ) from public_exc
        else:
            self._query_api_available = True
            # The Bulk Offer registry is AWS's broader billable-product index.
            # Merge it when available so newly launched offers are visible even
            # before a regional Query API cache happens to contain them.
            try:
                codes.extend(self._public_catalog.service_codes())
            except ManualConfirmationRequired:
                pass
        unique = sorted(set(codes))
        self._persistent_cache.set(persistent_key, unique)
        return unique

    @staticmethod
    def _is_auth_failure(exc: Exception) -> bool:
        if not isinstance(exc, ClientError):
            return False
        code = str(exc.response.get("Error", {}).get("Code") or "")
        return code in {
            "UnrecognizedClientException",
            "InvalidClientTokenId",
            "ExpiredToken",
            "ExpiredTokenException",
            "SignatureDoesNotMatch",
            "AuthFailure",
            "AccessDenied",
            "AccessDeniedException",
        }

    @staticmethod
    def attributes(product: dict[str, Any]) -> dict[str, str]:
        return product.get("product", {}).get("attributes", {})

    @staticmethod
    def billing_identity(product: dict[str, Any]) -> tuple[str, str, str]:
        attrs = PricingCatalog.attributes(product)
        service_code = product.get("serviceCode") or attrs.get("servicecode")
        usage_type = attrs.get("usagetype") or attrs.get("usageType")
        operation = attrs.get("operation")
        if not service_code or not usage_type or operation is None:
            raise ManualConfirmationRequired(
                "AWS 产品记录缺少 serviceCode、usageType 或 operation",
                code="incomplete_billing_dimensions",
                sku=product.get("product", {}).get("sku"),
            )
        return service_code, usage_type, operation

    @staticmethod
    def on_demand_rate(product: dict[str, Any]) -> float | None:
        """Read an official USD On-Demand rate for candidate ranking only.

        This optional value is only for legacy candidate ordering. Calculator
        web results remain authoritative for EC2.
        """

        terms = product.get("terms", {}).get("OnDemand", {})
        rates: list[float] = []
        for term in terms.values():
            for dimension in term.get("priceDimensions", {}).values():
                raw = dimension.get("pricePerUnit", {}).get("USD")
                if raw in (None, ""):
                    continue
                try:
                    rates.append(float(raw))
                except (TypeError, ValueError):
                    continue
        return min(rates) if rates else None

    @staticmethod
    def on_demand_unit_rate(product: dict[str, Any]) -> tuple[float, str] | None:
        """Return the first-tier official USD unit rate and its AWS unit.

        This is used only for a reference price when the customer supplied no
        quantity.  It is never multiplied into, or submitted as, customer
        usage.  Prefer the dimension beginning at zero for tiered products.
        """

        dimensions: list[tuple[float, float, str]] = []
        for term in product.get("terms", {}).get("OnDemand", {}).values():
            for dimension in term.get("priceDimensions", {}).values():
                raw = dimension.get("pricePerUnit", {}).get("USD")
                if raw in (None, ""):
                    continue
                try:
                    rate = float(raw)
                    begin = float(dimension.get("beginRange") or 0)
                except (TypeError, ValueError):
                    continue
                unit = str(dimension.get("unit") or "unit")
                dimensions.append((begin, rate, unit))
        if not dimensions:
            return None
        _, rate, unit = min(dimensions, key=lambda item: (item[0], item[1]))
        return rate, unit

    @staticmethod
    def reserved_price(
        product: dict[str, Any],
        *,
        years: int,
        payment_option: str,
        offering_class: str | None = None,
        hours_per_month: float = 730,
    ) -> ReservedPrice:
        """Read one exact Reserved term from the official AWS Price List.

        The returned monthly amount is the recurring hourly charge plus the
        upfront charge amortized across the selected contract term.  It is a
        deterministic catalog calculation, not a locally maintained price.
        """

        lease = f"{years}yr"
        purchase = {
            "no_upfront": "No Upfront",
            "partial_upfront": "Partial Upfront",
            "all_upfront": "All Upfront",
        }.get(payment_option)
        if purchase is None:
            raise ManualConfirmationRequired(
                f"无法识别预留实例付款方式 {payment_option!r}",
                code="invalid_reserved_payment_option",
            )

        matches: list[tuple[str, dict[str, Any]]] = []
        for code, term in product.get("terms", {}).get("Reserved", {}).items():
            attributes = term.get("termAttributes", {})
            if attributes.get("LeaseContractLength") != lease:
                continue
            if attributes.get("PurchaseOption") != purchase:
                continue
            if offering_class is not None and str(
                attributes.get("OfferingClass", "standard")
            ).casefold() != offering_class.casefold():
                continue
            matches.append((code, term))

        if not matches:
            raise ManualConfirmationRequired(
                "AWS 官方目录没有返回所选预留期限与付款方式的价格",
                code="reserved_term_not_found",
                years=years,
                payment_option=payment_option,
                offering_class=offering_class,
            )

        def amounts(term: dict[str, Any]) -> tuple[float, float]:
            hourly = 0.0
            upfront = 0.0
            for dimension in term.get("priceDimensions", {}).values():
                raw = dimension.get("pricePerUnit", {}).get("USD")
                if raw in (None, ""):
                    continue
                try:
                    price = float(raw)
                except (TypeError, ValueError):
                    continue
                unit = str(dimension.get("unit") or "").casefold()
                if unit in {"hrs", "hour", "hours"}:
                    hourly += price
                elif unit in {"quantity", "unit", "units"}:
                    upfront += price
            return hourly, upfront

        priced = [(code, *amounts(term)) for code, term in matches]
        priced = [item for item in priced if item[1] > 0 or item[2] > 0]
        if not priced:
            raise ManualConfirmationRequired(
                "AWS 官方预留价格记录缺少有效金额",
                code="reserved_price_dimensions_missing",
            )
        code, hourly, upfront = min(
            priced,
            key=lambda item: item[1] * hours_per_month + item[2] / (years * 12),
        )
        return ReservedPrice(
            monthly_amortized=hourly * hours_per_month + upfront / (years * 12),
            upfront=upfront,
            hourly_recurring=hourly,
            term_code=code,
        )

    @staticmethod
    def require_unique(
        products: list[dict[str, Any]],
        *,
        context: str,
    ) -> dict[str, Any]:
        if not products:
            raise ManualConfirmationRequired(
                f"{context} 没有匹配到 AWS 官方产品记录",
                code="billing_product_not_found",
            )
        identities: dict[tuple[str, str, str], dict[str, Any]] = {}
        for product in products:
            identity = PricingCatalog.billing_identity(product)
            identities[identity] = product
        if len(identities) != 1:
            raise ManualConfirmationRequired(
                f"{context} 无法唯一确定 AWS 计费项",
                code="ambiguous_billing_dimensions",
                candidates=len(identities),
            )
        return next(iter(identities.values()))


def parse_number(value: Any, *, field: str) -> float:
    """Parse numeric catalog values such as '1.37 GiB' without assuming a fixed unit."""

    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise ManualConfirmationRequired(
            f"AWS 产品属性 {field} 不是可识别的数值",
            code="unparseable_official_specification",
            field=field,
        )
    token = value.replace(",", "").strip().split()[0]
    try:
        return float(token)
    except ValueError as exc:
        raise ManualConfirmationRequired(
            f"AWS 产品属性 {field}={value!r} 不是可识别的数值",
            code="unparseable_official_specification",
            field=field,
            value=value,
        ) from exc
