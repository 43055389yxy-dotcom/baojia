import json
from pathlib import Path

from app.core.errors import ManualConfirmationRequired
from app.domain.models import ServiceRequirement
from app.integrations.auto_service_discovery import (
    PROFILE_SCHEMA_VERSION,
    PROFILE_TTL_SECONDS,
    AutoServiceDiscovery,
    _dimension_field,
    _dimension_fields,
    _flat_rate_dimensions,
)
from app.integrations.deepseek import _official_profile_cache_model
from app.services.plugins.generic_official import GenericOfficialPlugin


def appflow_product() -> dict:
    return {
        "serviceCode": "AmazonAppFlow",
        "product": {
            "sku": "appflow-data",
            "attributes": {
                "usagetype": "APS1-DataProcessed",
                "operation": "RunFlow",
                "regionCode": "ap-southeast-1",
                "productFamily": "Data Processing",
            },
        },
        "terms": {
            "OnDemand": {
                "term": {
                    "priceDimensions": {
                        "dimension": {
                            "beginRange": "0",
                            "unit": "GB",
                            "description": "Data processed by an AppFlow flow",
                            "pricePerUnit": {"USD": "0.02"},
                        }
                    }
                }
            }
        },
    }


def test_official_minute_processing_dimension_uses_aggregate_processing_hours() -> None:
    assert _dimension_field(
        {
            "unit": "minutes",
            "usage_type": "IAD-B-AVC-HD-S-30",
            "description": "HD video transcoding per minute",
        }
    ) == ("processing_hours", "处理时长（小时）")


def test_official_upload_and_download_rows_use_neutral_processed_data() -> None:
    assert _dimension_field(
        {
            "unit": "GigaBytes",
            "usage_type": "USE1-UploadBytes",
            "description": "GigaByte uploaded over SFTP to S3",
        }
    ) == ("data_processed_gib", "处理或传输数据量（GiB）")


class AppFlowCatalog:
    def __init__(self) -> None:
        self.product_calls = 0

    @staticmethod
    def service_codes() -> list[str]:
        return ["AWSLambda", "AmazonAppFlow", "AmazonDynamoDB"]

    def products(self, service_code: str, filters: dict[str, str], *, max_pages: int = 20):
        self.product_calls += 1
        assert service_code == "AmazonAppFlow"
        return [appflow_product()]


def test_official_field_profiles_expire_after_ten_days() -> None:
    assert PROFILE_TTL_SECONDS == 10 * 24 * 60 * 60


def test_distinct_official_products_never_share_a_profile_cache_key() -> None:
    assert AutoServiceDiscovery._profile_key(
        "bedrock", "us-east-1"
    ) != AutoServiceDiscovery._profile_key(
        "bedrock_service", "us-east-1"
    )
    assert AutoServiceDiscovery._profile_key(
        "chime", "us-east-1"
    ) != AutoServiceDiscovery._profile_key(
        "chime_services", "us-east-1"
    )


def test_dynamic_profiles_expose_configuration_facts_before_ai_extraction() -> None:
    fields = set(_dimension_fields([]))

    assert {
        "data_in_gib",
        "endpoint_count",
        "task_count",
        "write_records",
        "memory_retention_hours",
        "magnetic_retention_days",
        "product_variant",
    } <= fields


def test_official_hour_and_storage_dimensions_keep_service_semantics() -> None:
    assert _dimension_field(
        {
            "unit": "Hourly",
            "usage_type": "APN2-FirewallEndpoint-Hours",
            "description": "Firewall endpoint hour",
        }
    )[0] == "endpoint_hours"
    assert _dimension_field(
        {
            "unit": "GB-Hours",
            "usage_type": "EUC1-MemoryStore-ByteHrs",
        }
    )[0] == "memory_store_gib_hours"
    assert _dimension_field(
        {
            "unit": "GB-Mo",
            "usage_type": "EUC1-MagneticStore-ByteHrs",
        }
    )[0] == "magnetic_store_gib_months"


def test_official_profiles_name_common_billing_dimensions_semantically() -> None:
    assert _dimension_field(
        {
            "unit": "IOPS-Mo",
            "usage_type": "APS1-EBS:VolumeP-IOPS.gp3",
            "description": "Provisioned gp3 IOPS-month",
        }
    )[0] == "iops"
    assert _dimension_field(
        {
            "unit": "Queries",
            "usage_type": "APS1-DNS-Queries",
            "operation": "DNSQuery",
        }
    )[0] == "dns_queries"
    assert _dimension_field(
        {
            "unit": "Mo",
            "usage_type": "Health-Check-AWS",
            "description": "Health Check for an AWS endpoint",
        }
    )[0] == "health_checks"
    assert _dimension_field(
        {
            "unit": "GB",
            "usage_type": "APS1-AttachmentsSize-Bytes",
            "operation": "Send",
        }
    )[0] == "attachments_gib"


def test_flat_rate_plans_keep_subscription_overage_and_included_quotas() -> None:
    product = {
        "serviceCode": "CloudFrontPlans",
        "product": {
            "sku": "plan-a",
            "attributes": {
                "usagetype": "Global-CloudFrontPlan-Pro",
                "operation": "CloudFrontPlan",
            },
        },
        "terms": {
            "FlatRate": {
                "plans": [
                    {
                        "sku": "plan-a",
                        "planCode": "Pro",
                        "planFamilyCode": "CloudFrontPlan",
                        "subscriptionPrice": {
                            "description": "Pro plan",
                            "pricePerUnit": {"USD": "100.00"},
                        },
                        "features": [
                            {
                                "featureCode": "Requests",
                                "featureName": "Requests",
                                "usageType": "Global-Requests",
                                "usageQuota": {"unit": "Requests", "value": "1000000"},
                                "overage": {"pricePerUnit": {"USD": "0.01"}},
                            }
                        ],
                    }
                ]
            }
        },
    }

    dimensions, plans = _flat_rate_dimensions(product)

    assert dimensions[0]["instance_type"] == "Pro"
    assert dimensions[0]["unit"] == "Quantity"
    assert dimensions[0]["price"] == 100
    assert dimensions[1]["usage_type"] == "Global-Requests"
    assert plans[0]["features"][0]["included_quantity"] == "1000000"


def test_official_bedrock_marketplace_dimension_is_not_discarded() -> None:
    assert AutoServiceDiscovery._safe_dimension(
        {
            "usage_type": "APS1-MP:InputTokenCount",
            "unit": "1M tokens",
            "description": "AWS Marketplace software usage for Bedrock model tokens",
        }
    )


def test_managed_service_name_resolves_by_unique_official_core_stem(tmp_path: Path) -> None:
    class GrafanaCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonGrafana", "AmazonManagedBlockchain"]

    discovery = AutoServiceDiscovery(
        GrafanaCatalog(),  # type: ignore[arg-type]
        tmp_path / "auto-profiles.sqlite3",
    )

    assert (
        discovery.resolve_service_code("amazon_managed_grafana", "Amazon Managed Grafana")
        == "AmazonGrafana"
    )


def test_curated_component_resolves_its_differently_named_official_offer(
    tmp_path: Path,
) -> None:
    class CuratedCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AWSDatabaseMigrationSvc", "AWSSystemsManager", "AWSLambda"]

    discovery = AutoServiceDiscovery(
        CuratedCatalog(),  # type: ignore[arg-type]
        tmp_path / "auto-profiles.sqlite3",
    )

    assert discovery.resolve_service_code("dms", "AWS DMS") == "AWSDatabaseMigrationSvc"
    assert discovery.resolve_service_code("appconfig", "AWS AppConfig") == "AWSSystemsManager"


def test_curated_component_never_returns_an_offer_missing_from_current_catalog(
    tmp_path: Path,
) -> None:
    class StaleCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AWSLambda"]

    discovery = AutoServiceDiscovery(
        StaleCatalog(),  # type: ignore[arg-type]
        tmp_path / "auto-profiles.sqlite3",
    )

    try:
        discovery.resolve_service_code("dms", "AWS DMS")
    except ManualConfirmationRequired as exc:
        assert exc.code == "auto_discovery_service_code_not_found"
    else:  # pragma: no cover - protects the audited fail-closed contract
        raise AssertionError("stale curated offer should not resolve")


def test_unknown_component_result_cache_is_bound_to_official_contract() -> None:
    first = {
        "status": "verified",
        "profile_schema_version": 2,
        "service_code": "AmazonExample",
        "field_bindings": [
            {
                "field": "storage_gib_month",
                "usage_type": "Storage",
                "operation": "Store",
                "unit": "GB-Mo",
            }
        ],
        "attribute_fields": ["instanceType"],
    }
    changed = {
        **first,
        "field_bindings": [
            {
                "field": "data_processed_gib",
                "usage_type": "DataProcessed",
                "operation": "Process",
                "unit": "GB",
            }
        ],
    }

    first_key = _official_profile_cache_model("model-a", first)
    assert first_key is not None
    assert first_key == _official_profile_cache_model("model-a", dict(first))
    assert first_key != _official_profile_cache_model("model-a", changed)
    assert _official_profile_cache_model("model-a", {"status": "failed"}) is None


def test_unknown_service_builds_and_reuses_verified_official_profile(tmp_path: Path) -> None:
    catalog = AppFlowCatalog()
    discovery = AutoServiceDiscovery(
        catalog,  # type: ignore[arg-type]
        tmp_path / "auto-profiles.sqlite3",
    )

    first = discovery.ensure_profile(
        service_key="appflow",
        display_name="Amazon AppFlow",
        region="ap-southeast-1",
    )
    second = discovery.ensure_profile(
        service_key="appflow",
        display_name="Amazon AppFlow",
        region="ap-southeast-1",
    )

    assert first is not None
    assert first["status"] == "verified"
    assert first["service_code"] == "AmazonAppFlow"
    assert "data_processed_gib" in first["fields"]
    assert first["profile_schema_version"] == PROFILE_SCHEMA_VERSION
    assert first["field_bindings"][0]["field"] == "data_processed_gib"
    assert first["field_bindings"][0]["usage_type"] == "APS1-DataProcessed"
    assert "官方目录自动生成" in first["prompt_text"]
    assert "UsageType=APS1-DataProcessed" in first["prompt_text"]
    assert second is not None
    # One regional query plus one global-dimension query; the second profile
    # read is served from the persistent discovery cache.
    assert catalog.product_calls == 2
    assert discovery.list_profiles()[0]["service_key"] == "appflow"


def test_failed_refresh_never_overwrites_last_verified_profile(tmp_path: Path) -> None:
    discovery = AutoServiceDiscovery(
        AppFlowCatalog(),  # type: ignore[arg-type]
        tmp_path / "auto-profiles.sqlite3",
    )
    verified = discovery.ensure_profile(
        service_key="appflow",
        display_name="Amazon AppFlow",
        region="ap-southeast-1",
    )
    assert verified is not None and verified["status"] == "verified"

    discovery._save(
        {
            "profile_schema_version": PROFILE_SCHEMA_VERSION,
            "service_key": "appflow",
            "display_name": "Amazon AppFlow",
            "service_code": "AmazonAppFlow",
            "region": "ap-southeast-1",
            "fields": [],
            "dimensions": [],
        },
        status="failed",
        error_code="temporary_catalog_failure",
    )

    cached = discovery.get_profile("appflow", "ap-southeast-1")
    assert cached is not None
    assert cached["status"] == "verified"
    assert cached["field_bindings"]


def test_stale_profile_is_refreshed_from_official_catalog(tmp_path: Path) -> None:
    database_path = tmp_path / "auto-profiles.sqlite3"
    catalog = AppFlowCatalog()
    discovery = AutoServiceDiscovery(
        catalog,  # type: ignore[arg-type]
        database_path,
    )
    discovery.ensure_profile(
        service_key="appflow",
        display_name="Amazon AppFlow",
        region="ap-southeast-1",
    )
    with discovery._connect() as connection:
        connection.execute(
            "UPDATE auto_service_profiles SET updated_at = updated_at - ?",
            (PROFILE_TTL_SECONDS + 1,),
        )

    result = discovery.refresh_stale_profiles()

    assert result == {"checked": 1, "refreshed": 1, "failed": 0}
    assert catalog.product_calls == 4


def test_old_profile_schema_is_upgraded_without_waiting_ten_days(tmp_path: Path) -> None:
    database_path = tmp_path / "auto-profiles.sqlite3"
    catalog = AppFlowCatalog()
    discovery = AutoServiceDiscovery(
        catalog,  # type: ignore[arg-type]
        database_path,
    )
    discovery.ensure_profile(
        service_key="appflow",
        display_name="Amazon AppFlow",
        region="ap-southeast-1",
    )
    with discovery._connect() as connection:
        row = connection.execute(
            "SELECT profile_key, payload_json FROM auto_service_profiles"
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload.pop("profile_schema_version", None)
        payload.pop("field_bindings", None)
        connection.execute(
            "UPDATE auto_service_profiles SET payload_json = ? WHERE profile_key = ?",
            (json.dumps(payload), row["profile_key"]),
        )

    upgraded = discovery.ensure_profile(
        service_key="appflow",
        display_name="Amazon AppFlow",
        region="ap-southeast-1",
    )

    assert upgraded is not None
    assert upgraded["profile_schema_version"] == PROFILE_SCHEMA_VERSION
    assert upgraded["field_bindings"]
    assert catalog.product_calls == 4


def storage_product() -> dict:
    product = appflow_product()
    product["product"]["sku"] = "appflow-storage"
    product["product"]["attributes"]["usagetype"] = "APS1-FlowStorage"
    product["product"]["attributes"]["operation"] = "StoreFlowData"
    dimension = product["terms"]["OnDemand"]["term"]["priceDimensions"]["dimension"]
    dimension["unit"] = "GB-Mo"
    dimension["description"] = "Storage used by AppFlow"
    dimension["pricePerUnit"]["USD"] = "0.005"
    return product


class MultiDimensionAppFlowCatalog(AppFlowCatalog):
    def products(self, service_code: str, filters: dict[str, str], *, max_pages: int = 20):
        self.product_calls += 1
        assert service_code == "AmazonAppFlow"
        return [storage_product(), appflow_product()]


def custom_unit_product() -> dict:
    product = appflow_product()
    product["product"]["sku"] = "appflow-execution"
    product["product"]["attributes"]["usagetype"] = "APS1-FlowExecution"
    product["product"]["attributes"]["operation"] = "ExecuteFlow"
    dimension = product["terms"]["OnDemand"]["term"]["priceDimensions"]["dimension"]
    dimension["unit"] = "Executions"
    dimension["description"] = "Successful AppFlow executions"
    dimension["pricePerUnit"]["USD"] = "0.10"
    return product


class CustomUnitAppFlowCatalog(AppFlowCatalog):
    def products(self, service_code: str, filters: dict[str, str], *, max_pages: int = 20):
        self.product_calls += 1
        assert service_code == "AmazonAppFlow"
        return [custom_unit_product()]


def test_unknown_service_uses_exact_official_binding_not_cheapest_same_unit(
    tmp_path: Path,
) -> None:
    catalog = MultiDimensionAppFlowCatalog()
    discovery = AutoServiceDiscovery(
        catalog,  # type: ignore[arg-type]
        tmp_path / "auto-profiles.sqlite3",
    )
    plugin = GenericOfficialPlugin(
        None,  # type: ignore[arg-type]
        catalog,  # type: ignore[arg-type]
        discovery,
    )

    selected = plugin.select(
        ServiceRequirement(
            service="appflow",
            calculator_service_name="Amazon AppFlow",
            region="ap-southeast-1",
            requirements={"data_processed_gib": 500},
        ),
        "ap-southeast-1",
    )

    assert selected.usage_lines[0].usage_type == "APS1-DataProcessed"
    assert selected.usage_lines[0].operation == "RunFlow"
    assert selected.usage_lines[0].amount == 500


def test_unknown_official_unit_gets_guarded_field_and_can_be_priced(
    tmp_path: Path,
) -> None:
    catalog = CustomUnitAppFlowCatalog()
    discovery = AutoServiceDiscovery(
        catalog,  # type: ignore[arg-type]
        tmp_path / "auto-profiles.sqlite3",
    )
    profile = discovery.ensure_profile(
        service_key="appflow",
        display_name="Amazon AppFlow",
        region="ap-southeast-1",
    )
    assert profile is not None
    field = next(
        binding["field"]
        for binding in profile["field_bindings"]
        if binding["usage_type"] == "APS1-FlowExecution"
    )
    assert field == "flow_runs"

    plugin = GenericOfficialPlugin(
        None,  # type: ignore[arg-type]
        catalog,  # type: ignore[arg-type]
        discovery,
    )
    selected = plugin.select(
        ServiceRequirement(
            service="appflow",
            calculator_service_name="Amazon AppFlow",
            region="ap-southeast-1",
            requirements={field: 12},
        ),
        "ap-southeast-1",
    )

    assert selected.usage_lines[0].usage_type == "APS1-FlowExecution"
    assert selected.usage_lines[0].amount == 12


def test_uncommon_official_units_get_stable_customer_fields() -> None:
    assert _dimension_field(
        {
            "unit": "Processing-Bytes",
            "operation": "DataProcessing",
            "description": "GB of data processed",
        }
    ) == ("data_processed_gib", "处理数据量（GiB）")
    assert _dimension_field(
        {
            "unit": "Bucket-days",
            "description": "S3 Bucket analyzed daily",
        }
    ) == ("bucket_count", "存储桶数量")
    assert _dimension_field(
        {
            "unit": "GB",
            "description": "Sensitive Data Discovery",
        }
    ) == ("data_scanned_gib", "扫描数据量（GiB）")


def test_role_session_and_on_premise_units_keep_separate_billable_fields() -> None:
    assert _dimension_field(
        {
            "unit": "OnPremUpdates",
            "description": "Deployment update to an on-premises instance",
        }
    ) == ("deployment_updates", "本地服务器更新次数")
    assert _dimension_field(
        {
            "unit": "User-Month",
            "description": "QuickSight Enterprise author subscription",
        }
    ) == ("author_users", "作者数量")
    assert _dimension_field(
        {
            "unit": "User-Month",
            "description": "QuickSight Enterprise reader subscription",
        }
    ) == ("reader_users", "读者数量")
    assert _dimension_field(
        {
            "unit": "Session",
            "description": "QuickSight reader session",
        }
    ) == ("session_capacity", "读者会话次数")
    assert _dimension_field(
        {
            "unit": "User",
            "usage_type": "QS-User-Enterprise-Month",
            "description": "QuickSight Enterprise Edition User",
        }
    ) == ("author_users", "作者数量")
    assert _dimension_field(
        {
            "unit": "Users",
            "usage_type": "QS-User-Enterprise-Month-Q",
            "description": "QuickSight Q Author $10 Monthly Add-on Fee",
        }
    ) == (None, None)


def test_unknown_service_can_quote_explicit_usage_without_custom_adapter(tmp_path: Path) -> None:
    catalog = AppFlowCatalog()
    discovery = AutoServiceDiscovery(
        catalog,  # type: ignore[arg-type]
        tmp_path / "auto-profiles.sqlite3",
    )
    discovery.ensure_profile(
        service_key="appflow",
        display_name="Amazon AppFlow",
        region="ap-southeast-1",
    )
    plugin = GenericOfficialPlugin(
        None,  # type: ignore[arg-type]
        catalog,  # type: ignore[arg-type]
        discovery,
    )

    selected = plugin.select(
        ServiceRequirement(
            service="appflow",
            calculator_service_name="Amazon AppFlow",
            region="ap-southeast-1",
            requirements={"data_processed_gib": 500},
        ),
        "ap-southeast-1",
    )

    assert selected.usage_lines[0].amount == 500
    assert selected.usage_lines[0].service_code == "AmazonAppFlow"
    assert "自动建立" in selected.rationale


def test_unknown_service_without_usage_exposes_reference_unit_only(tmp_path: Path) -> None:
    catalog = AppFlowCatalog()
    discovery = AutoServiceDiscovery(
        catalog,  # type: ignore[arg-type]
        tmp_path / "auto-profiles.sqlite3",
    )
    plugin = GenericOfficialPlugin(
        None,  # type: ignore[arg-type]
        catalog,  # type: ignore[arg-type]
        discovery,
    )

    selected = plugin.select(
        ServiceRequirement(
            service="appflow",
            calculator_service_name="Amazon AppFlow",
            region="ap-southeast-1",
        ),
        "ap-southeast-1",
    )

    assert selected.usage_lines == []
    assert selected.reference_rates[0].service_code == "AmazonAppFlow"
    assert selected.reference_rates[0].unit_price == 0.02


def test_discovery_profile_never_drops_price_rows_behind_selectable_bindings(
    tmp_path: Path,
) -> None:
    products = []
    for index in range(125):
        product = json.loads(json.dumps(appflow_product()))
        product["product"]["sku"] = f"appflow-{index}"
        product["product"]["attributes"]["usagetype"] = f"APS1-DataProcessed-{index}"
        products.append(product)

    class LargeCatalog(AppFlowCatalog):
        def products(
            self,
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            assert service_code == "AmazonAppFlow"
            return products

    discovery = AutoServiceDiscovery(
        LargeCatalog(),  # type: ignore[arg-type]
        tmp_path / "auto-profiles.sqlite3",
    )
    profile = discovery.ensure_profile(
        service_key="appflow",
        display_name="Amazon AppFlow",
        region="ap-southeast-1",
    )

    assert profile is not None
    assert len(profile["dimensions"]) == 125
    dimension_identities = {
        (item["usage_type"], item["operation"], item["unit"])
        for item in profile["dimensions"]
    }
    assert all(
        (binding["usage_type"], binding["operation"], binding["unit"])
        in dimension_identities
        for binding in profile["field_bindings"]
    )
