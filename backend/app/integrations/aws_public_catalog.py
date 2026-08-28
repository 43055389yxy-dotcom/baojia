from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from botocore.loaders import create_loader

from app.core.errors import ManualConfirmationRequired

AWS_PRICE_LIST_BASE_URL = "https://pricing.us-east-1.amazonaws.com"
AWS_OFFER_INDEX_PATH = "/offers/v1.0/aws/index.json"
PUBLIC_CATALOG_TIMEOUT_SECONDS = 30


def _canonical(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


@dataclass(frozen=True, slots=True)
class AwsOfferIdentity:
    service_code: str
    offer_code: str
    current_version_url: str
    current_region_index_url: str | None
    version_index_url: str | None
    publication_date: str | None


class PublicAwsPriceCatalog:
    """Unsigned, read-only AWS Bulk Price List catalog.

    The Query API remains the preferred source when AWS credentials work. This
    catalog is the official, public fallback used when a local quote system has
    no valid AWS account credentials. It downloads JSON metadata and prices
    only; it never downloads or executes code.
    """

    def __init__(
        self,
        fetch_json: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._fetch_json = fetch_json or self._download_json
        self._lock = threading.RLock()
        self._offer_index: dict[str, Any] | None = None
        self._region_indexes: dict[str, dict[str, Any]] = {}
        self._location_regions: dict[str, str] | None = None

    @staticmethod
    def _download_json(url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "AstraQuote-AwsCatalog/1.0",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=PUBLIC_CATALOG_TIMEOUT_SECONDS
            ) as response:
                payload = json.load(response)
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            OSError,
        ) as exc:
            raise ManualConfirmationRequired(
                "AWS 官方公开价格目录暂时不可用",
                code="public_pricing_catalog_unavailable",
                url=url,
            ) from exc
        if not isinstance(payload, dict):
            raise ManualConfirmationRequired(
                "AWS 官方公开价格目录返回了无效数据",
                code="public_pricing_catalog_invalid",
                url=url,
            )
        return payload

    @staticmethod
    def _absolute_url(path: str) -> str:
        if path.startswith("https://"):
            return path
        return f"{AWS_PRICE_LIST_BASE_URL}{path}"

    def offer_index(self, *, refresh: bool = False) -> dict[str, Any]:
        with self._lock:
            if self._offer_index is None or refresh:
                self._offer_index = self._fetch_json(
                    self._absolute_url(AWS_OFFER_INDEX_PATH)
                )
            return self._offer_index

    def offers(self, *, refresh: bool = False) -> list[AwsOfferIdentity]:
        index = self.offer_index(refresh=refresh)
        publication_date = str(index.get("publicationDate") or "") or None
        result: list[AwsOfferIdentity] = []
        raw_offers = index.get("offers") or {}
        if not isinstance(raw_offers, dict):
            return result
        for key, raw in raw_offers.items():
            if not isinstance(raw, dict):
                continue
            offer_code = str(raw.get("offerCode") or key).strip()
            current_url = str(raw.get("currentVersionUrl") or "").strip()
            if not offer_code or not current_url:
                continue
            result.append(
                AwsOfferIdentity(
                    service_code=offer_code,
                    offer_code=offer_code,
                    current_version_url=current_url,
                    current_region_index_url=(
                        str(raw.get("currentRegionIndexUrl") or "").strip() or None
                    ),
                    version_index_url=(
                        str(raw.get("versionIndexUrl") or "").strip() or None
                    ),
                    publication_date=publication_date,
                )
            )
        return sorted(result, key=lambda item: item.service_code.casefold())

    def service_codes(self, *, refresh: bool = False) -> list[str]:
        return [item.service_code for item in self.offers(refresh=refresh)]

    def resolve_offer(self, service_code: str) -> AwsOfferIdentity:
        target = _canonical(service_code)
        matches = [
            offer
            for offer in self.offers()
            if target in {_canonical(offer.service_code), _canonical(offer.offer_code)}
        ]
        if len(matches) != 1:
            raise ManualConfirmationRequired(
                "AWS 官方公开价格目录无法唯一匹配服务",
                code="public_pricing_offer_not_found",
                service_code=service_code,
            )
        return matches[0]

    def available_regions(self, service_code: str) -> tuple[list[str], str | None]:
        offer = self.resolve_offer(service_code)
        if not offer.current_region_index_url:
            return ["global"], None
        index = self._region_index(offer)
        raw_regions = index.get("regions") or {}
        regions = (
            sorted(str(code) for code in raw_regions)
            if isinstance(raw_regions, dict)
            else []
        )
        return regions or ["global"], str(index.get("publicationDate") or "") or None

    def _region_index(self, offer: AwsOfferIdentity) -> dict[str, Any]:
        path = offer.current_region_index_url
        if not path:
            return {}
        with self._lock:
            cached = self._region_indexes.get(offer.offer_code)
            if cached is not None:
                return cached
            payload = self._fetch_json(self._absolute_url(path))
            self._region_indexes[offer.offer_code] = payload
            return payload

    def _price_document_url(
        self,
        offer: AwsOfferIdentity,
        region_code: str | None,
    ) -> str | None:
        if offer.current_region_index_url:
            region_index = self._region_index(offer)
            regions = region_index.get("regions") or {}
            if isinstance(regions, dict):
                # Some global offers publish an empty region index while their
                # current all-regions document is the only price document.
                # Falling through to that official document is safe because
                # there are no regional rows to mix.  Previously these offers
                # were reported as unsupported despite having a valid current
                # version URL.
                if not regions:
                    return self._absolute_url(offer.current_version_url)
                if region_code and isinstance(regions.get(region_code), dict):
                    path = str(regions[region_code].get("currentVersionUrl") or "")
                    if path:
                        return self._absolute_url(path)
                # A small set of globally billed products exposes a literal
                # global entry. Prefer it over downloading every region.
                for key in ("global", "Global"):
                    if not isinstance(regions.get(key), dict):
                        continue
                    path = str(regions[key].get("currentVersionUrl") or "")
                    if path:
                        return self._absolute_url(path)
                if region_code:
                    raise ManualConfirmationRequired(
                        f"AWS 官方目录未发布 {offer.offer_code} 在 {region_code} 的价格文件",
                        code="public_pricing_region_not_found",
                        service_code=offer.offer_code,
                        region=region_code,
                    )
                # A regional offer without an explicit region filter can have
                # a very large all-regions document (EC2 is the typical case).
                # Returning that document would mix product rows from unrelated
                # regions and consume hundreds of megabytes.  Callers already
                # query the customer's region first, so an unfiltered follow-up
                # is useful only when the official index exposes a literal
                # global entry.
                return None
        return self._absolute_url(offer.current_version_url)

    @staticmethod
    def _matches_filters(attributes: dict[str, Any], filters: dict[str, str]) -> bool:
        aliases = {
            "regionCode": ("regionCode", "regioncode", "region"),
            "servicecode": ("servicecode", "serviceCode"),
        }
        for field, expected in filters.items():
            candidates = aliases.get(field, (field,))
            actual = next(
                (
                    attributes.get(candidate)
                    for candidate in candidates
                    if attributes.get(candidate) is not None
                ),
                None,
            )
            if str(actual or "") != str(expected):
                return False
        return True

    def _region_from_filters(self, filters: dict[str, str]) -> str | None:
        explicit = filters.get("regionCode") or filters.get("regioncode")
        if explicit:
            return str(explicit)
        location = str(filters.get("location") or "").strip().casefold()
        if not location:
            return None
        with self._lock:
            if self._location_regions is None:
                mapping: dict[str, str] = {}
                endpoints = create_loader().load_data("endpoints")
                for partition in endpoints.get("partitions", []):
                    for code, metadata in partition.get("regions", {}).items():
                        if not isinstance(metadata, dict):
                            continue
                        description = str(metadata.get("description") or "").strip()
                        if description:
                            mapping[description.casefold()] = str(code)
                self._location_regions = mapping
            return self._location_regions.get(location)

    def products(
        self,
        service_code: str,
        filters: dict[str, str],
        *,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        offer = self.resolve_offer(service_code)
        region_code = self._region_from_filters(filters)
        document_url = self._price_document_url(
            offer, str(region_code) if region_code else None
        )
        if document_url is None:
            return []
        document = self._fetch_json(document_url)
        raw_products = document.get("products") or {}
        all_terms = document.get("terms") or {}
        on_demand = all_terms.get("OnDemand") or {}
        reserved = all_terms.get("Reserved") or {}
        flat_rate = all_terms.get("FlatRate") or {}
        flat_rate_plans = (
            flat_rate.get("plans")
            if isinstance(flat_rate, dict)
            and isinstance(flat_rate.get("plans"), list)
            else []
        )
        if not isinstance(raw_products, dict):
            return []
        result: list[dict[str, Any]] = []
        for sku, raw_product in raw_products.items():
            if not isinstance(raw_product, dict):
                continue
            attributes = raw_product.get("attributes") or {}
            if not isinstance(attributes, dict) or not self._matches_filters(
                attributes, filters
            ):
                continue
            terms: dict[str, Any] = {"OnDemand": {}}
            if isinstance(on_demand, dict) and isinstance(on_demand.get(sku), dict):
                terms["OnDemand"] = on_demand[sku]
            if isinstance(reserved, dict) and isinstance(reserved.get(sku), dict):
                terms["Reserved"] = reserved[sku]
            matching_plans = [
                plan
                for plan in flat_rate_plans
                if isinstance(plan, dict) and str(plan.get("sku") or "") == str(sku)
            ]
            if matching_plans:
                terms["FlatRate"] = {"plans": matching_plans}
            result.append(
                {
                    "formatVersion": document.get("formatVersion") or "v1.0",
                    "disclaimer": document.get("disclaimer"),
                    "offerCode": document.get("offerCode") or offer.offer_code,
                    "version": document.get("version"),
                    "publicationDate": document.get("publicationDate"),
                    "serviceCode": offer.service_code,
                    "product": raw_product,
                    "terms": terms,
                }
            )
            if max_items is not None and len(result) >= max_items:
                break
        return result

    def attribute_values(self, service_code: str, attribute_name: str) -> list[str]:
        regions, _ = self.available_regions(service_code)
        preferred = next(
            (
                region
                for region in ("ap-southeast-1", "us-east-1", *regions)
                if region in regions and region != "global"
            ),
            None,
        )
        filters = {"regionCode": preferred} if preferred else {}
        products = self.products(service_code, filters)
        values = {
            str((product.get("product", {}).get("attributes") or {}).get(attribute_name))
            for product in products
            if (product.get("product", {}).get("attributes") or {}).get(attribute_name)
            is not None
        }
        return sorted(values)
