from pathlib import Path

from botocore.exceptions import ClientError

from app.integrations.aws import RegionResolver
from app.integrations.aws_product_registry import AwsProductRegistry
from app.integrations.aws_public_catalog import (
    AWS_PRICE_LIST_BASE_URL,
    PublicAwsPriceCatalog,
)


def fixture_fetch(url: str) -> dict:
    if url.endswith("/offers/v1.0/aws/index.json"):
        return {
            "publicationDate": "2026-08-24T00:00:00Z",
            "offers": {
                "AmazonExample": {
                    "offerCode": "AmazonExample",
                    "versionIndexUrl": "/example/versions.json",
                    "currentVersionUrl": "/example/current/index.json",
                    "currentRegionIndexUrl": "/example/current/region_index.json",
                },
                "AWSFutureService": {
                    "offerCode": "AWSFutureService",
                    "currentVersionUrl": "/future/current/index.json",
                },
            },
        }
    if url.endswith("/example/current/region_index.json"):
        return {
            "regions": {
                "ap-southeast-1": {
                    "regionCode": "ap-southeast-1",
                    "currentVersionUrl": "/example/current/ap-southeast-1/index.json",
                }
            }
        }
    if url.endswith("/example/current/ap-southeast-1/index.json"):
        return {
            "formatVersion": "v1.0",
            "offerCode": "AmazonExample",
            "version": "20260824",
            "publicationDate": "2026-08-24T00:00:00Z",
            "products": {
                "sku-a": {
                    "sku": "sku-a",
                    "productFamily": "Data Processing",
                    "attributes": {
                        "regionCode": "ap-southeast-1",
                        "location": "Asia Pacific (Singapore)",
                        "operation": "Process",
                        "usagetype": "APS1-ProcessedBytes",
                    },
                }
            },
            "terms": {
                "OnDemand": {
                    "sku-a": {
                        "sku-a.term": {
                            "priceDimensions": {
                                "sku-a.term.dimension": {
                                    "beginRange": "0",
                                    "unit": "GB",
                                    "pricePerUnit": {"USD": "0.02"},
                                }
                            }
                        }
                    }
                }
            },
        }
    if url.endswith("/future/current/index.json"):
        return {"products": {}, "terms": {}}
    raise AssertionError(f"unexpected fixture URL: {url}")


def test_public_catalog_lists_every_offer_and_reads_regional_products() -> None:
    catalog = PublicAwsPriceCatalog(fixture_fetch)

    assert catalog.service_codes() == ["AmazonExample", "AWSFutureService"]
    products = catalog.products(
        "AmazonExample",
        {"regionCode": "ap-southeast-1"},
    )

    assert len(products) == 1
    assert products[0]["serviceCode"] == "AmazonExample"
    assert products[0]["product"]["sku"] == "sku-a"
    assert products[0]["terms"]["OnDemand"]["sku-a.term"]


def test_public_catalog_uses_only_official_aws_price_list_host() -> None:
    seen: list[str] = []

    def fetch(url: str) -> dict:
        seen.append(url)
        return fixture_fetch(url)

    catalog = PublicAwsPriceCatalog(fetch)
    catalog.products("AmazonExample", {"regionCode": "ap-southeast-1"})

    assert seen
    assert all(url.startswith(AWS_PRICE_LIST_BASE_URL) for url in seen)


def test_public_catalog_maps_official_location_to_regional_bulk_file() -> None:
    seen: list[str] = []

    def fetch(url: str) -> dict:
        seen.append(url)
        return fixture_fetch(url)

    catalog = PublicAwsPriceCatalog(fetch)
    products = catalog.products(
        "AmazonExample",
        {"location": "Asia Pacific (Singapore)"},
    )

    assert products
    assert any("ap-southeast-1/index.json" in url for url in seen)


def test_public_catalog_builds_dropdown_values_from_official_attributes() -> None:
    catalog = PublicAwsPriceCatalog(fixture_fetch)

    assert catalog.attribute_values("AmazonExample", "operation") == ["Process"]


def test_public_catalog_never_downloads_all_regions_without_region_filter() -> None:
    seen: list[str] = []

    def fetch(url: str) -> dict:
        seen.append(url)
        return fixture_fetch(url)

    catalog = PublicAwsPriceCatalog(fetch)

    assert catalog.products("AmazonExample", {}) == []
    assert not any(url.endswith("/example/current/index.json") for url in seen)


def test_region_resolver_uses_bundled_official_metadata_without_credentials() -> None:
    class InvalidSsm:
        @staticmethod
        def get_parameter(**_: object) -> dict:
            raise ClientError(
                {"Error": {"Code": "UnrecognizedClientException", "Message": "invalid"}},
                "GetParameter",
            )

    class Clients:
        ssm = InvalidSsm()

    resolver = RegionResolver(Clients())  # type: ignore[arg-type]

    assert resolver.long_name("ap-southeast-1") == "Asia Pacific (Singapore)"


def test_product_registry_creates_one_independent_row_per_offer(tmp_path: Path) -> None:
    registry = AwsProductRegistry(
        PublicAwsPriceCatalog(fixture_fetch),
        tmp_path / "aws-products.sqlite3",
    )

    result = registry.sync()
    products = registry.list_products()

    assert result["official_offer_count"] == 2
    assert result["inserted"] == 2
    assert {item["service_code"] for item in products} == {
        "AmazonExample",
        "AWSFutureService",
    }
    assert all(item["profile_status"] == "identity_ready" for item in products)
    assert all(
        item["field_template"]["isolation"] == "strict_component_boundary"
        for item in products
    )
    assert all(
        item["policy"]["cross_component_inheritance"] == "region_only"
        for item in products
    )
    assert registry.coverage() == {
        "total": 2,
        "official": 2,
        "retired": 0,
        "profile_status": {"identity_ready": 2},
    }

    # Official identity lookup is independent of workload-specific adapters.
    # CamelCase names and acronyms must remain human-resolvable instead of
    # degrading into keys such as ``e_c2`` or ``m_q``.
    example = registry.resolve_product("Amazon Example")
    assert example is not None
    assert example["service_code"] == "AmazonExample"
    assert example["service_key"] == "example"

    assert registry.sync_region_availability(workers=2) == {
        "checked": 2,
        "updated": 2,
        "failed": 0,
    }
    registry.update_profile(
        "AmazonExample",
        {
            "profile_schema_version": 2,
            "region": "ap-southeast-1",
            "fields": ["data_processed_gib", "requests"],
            "field_bindings": [
                {
                    "field": "data_processed_gib",
                    "unit": "GB",
                    "usage_type": "APS1-ProcessedBytes",
                }
            ],
            "attribute_names": ["operation", "usagetype"],
            "dimensions": [{"unit": "GB"}],
        },
        status="profile_ready",
    )
    registry.sync()
    enriched = {
        item["service_code"]: item for item in registry.list_products()
    }["AmazonExample"]
    assert enriched["field_template"]["fields"] == [
        "data_processed_gib",
        "requests",
    ]
    assert enriched["policy"]["billing_dimension_count"] == 1
    assert enriched["profile_status"] == "profile_ready"
    registry.sync()
    refreshed = {item["service_code"]: item for item in registry.list_products()}
    assert refreshed["AmazonExample"]["offer"]["available_regions"] == [
        "ap-southeast-1"
    ]
    assert refreshed["AWSFutureService"]["offer"]["available_regions"] == [
        "global"
    ]
    assert registry.sync_region_availability(workers=2) == {
        "checked": 0,
        "updated": 0,
        "failed": 0,
    }


def test_product_registry_accepts_official_marketing_version_suffix(tmp_path: Path) -> None:
    def fetch(url: str) -> dict:
        if url.endswith("/offers/v1.0/aws/index.json"):
            return {
                "publicationDate": "2026-08-26T00:00:00Z",
                "offers": {
                    "AmazonAppStream": {
                        "offerCode": "AmazonAppStream",
                        "currentVersionUrl": "/appstream/current/index.json",
                    }
                },
            }
        if url.endswith("/appstream/current/index.json"):
            return {"products": {}, "terms": {}}
        raise AssertionError(f"unexpected fixture URL: {url}")

    registry = AwsProductRegistry(
        PublicAwsPriceCatalog(fetch),
        tmp_path / "appstream-products.sqlite3",
    )
    registry.sync()

    resolved = registry.resolve_product("Amazon AppStream 2.0")

    assert resolved is not None
    assert resolved["service_code"] == "AmazonAppStream"
    # The safe normalization is deliberately terminal-version-only. A
    # different product phrase must not become a fuzzy prefix match.
    assert registry.resolve_product("Amazon AppStream 2.0 Connector") is None
