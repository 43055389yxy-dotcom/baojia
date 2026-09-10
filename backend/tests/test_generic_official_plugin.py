import math

import pytest

from app.core.errors import ManualConfirmationRequired
from app.domain.fact_ledger import unconsumed_customer_pricing_facts
from app.domain.models import SelectedResource, ServiceRequirement, UsageLine
from app.integrations.aws import PricingCatalog
from app.services.plugins.generic_official import (
    GenericOfficialPlugin,
    _CONFIGURATION_CONTEXT_FIELDS,
)


def priced_product(
    service_code: str,
    usage_type: str,
    unit: str,
    price: float,
    *,
    operation: str = "",
    group: str = "",
    **attributes: str,
) -> dict:
    return {
        "serviceCode": service_code,
        "product": {
            "sku": usage_type,
            "attributes": {
                "usagetype": usage_type,
                "operation": operation,
                "regionCode": "ap-northeast-1",
                "group": group,
                **attributes,
            },
        },
        "terms": {
            "OnDemand": {
                "term": {
                    "priceDimensions": {
                        "dimension": {
                            "beginRange": "0",
                            "unit": unit,
                            "pricePerUnit": {"USD": str(price)},
                        }
                    }
                }
            }
        },
    }


class FakeCatalog:
    @staticmethod
    def service_codes() -> list[str]:
        return ["AWSLambda", "AmazonDynamoDB", "AmazonVPC"]

    @staticmethod
    def products(service_code: str, filters: dict[str, str], *, max_pages: int = 20):
        assert service_code == "AWSLambda"
        return [
            {
                "serviceCode": "AWSLambda",
                "product": {
                    "sku": "lambda-request",
                    "attributes": {
                        "usagetype": "Request",
                        "operation": "",
                        "regionCode": "ap-southeast-1",
                    },
                },
                "terms": {
                    "OnDemand": {
                        "term": {
                            "priceDimensions": {
                                "dimension": {
                                    "beginRange": "0",
                                    "unit": "Requests",
                                    "pricePerUnit": {"USD": "0.0000002"},
                                }
                            }
                        }
                    }
                },
            }
        ]


def test_generic_plugin_without_usage_exposes_reference_rate_only() -> None:
    plugin = GenericOfficialPlugin(None, FakeCatalog())  # type: ignore[arg-type]
    requirement = ServiceRequirement(
        service="lambda",
        calculator_service_name="AWS Lambda",
        region="ap-southeast-1",
    )

    preview = plugin.preview(requirement, "ap-southeast-1")
    selected = plugin.select(requirement, "ap-southeast-1")

    assert preview.requires_confirmation is False
    assert selected.usage_lines == []
    assert selected.reference_rates[0].service_code == "AWSLambda"
    assert selected.reference_rates[0].unit_price == 0.0000002


def test_appstream_uses_exact_fleet_model_and_multiplies_monthly_per_user_hours() -> None:
    class AppStreamCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonAppStream"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            assert service_code == "AmazonAppStream"
            return [
                priced_product(
                    service_code,
                    "APS2-stream.standard.large-ib",
                    "hour",
                    0.10,
                    operation="Streaming:001",
                    instanceType="stream.standard.large",
                    instanceFunction="ImageBuilder",
                    vcpu="2",
                    memoryGib="8",
                ),
                priced_product(
                    service_code,
                    "APS2-stream.standard.large-fl",
                    "hour",
                    0.24,
                    operation="Streaming:001",
                    instanceType="stream.standard.large",
                    instanceFunction="Fleet",
                    vcpu="2",
                    memoryGib="8",
                ),
                priced_product(
                    service_code,
                    "APS2-stream.standard.xlarge-fl",
                    "hour",
                    0.48,
                    operation="Streaming:001",
                    instanceType="stream.standard.xlarge",
                    instanceFunction="Fleet",
                    vcpu="4",
                    memoryGib="16",
                ),
            ]

    requirement = ServiceRequirement(
        service="app_stream",
        calculator_service_name="Amazon AppStream 2.0",
        region="ap-southeast-2",
        requirements={
            "requested_model": "stream.standard.large",
            "user_count": 200,
            "hours_per_user_per_month": 120,
        },
    )

    selected = GenericOfficialPlugin(None, AppStreamCatalog()).select(  # type: ignore[arg-type]
        requirement,
        "ap-southeast-2",
    )

    assert selected.model == "stream.standard.large"
    assert len(selected.usage_lines) == 1
    assert selected.usage_lines[0].amount == 24_000
    assert selected.usage_lines[0].usage_type == "APS2-stream.standard.large-fl"
    assert set(selected.applied_requirement_fields) >= {
        "requested_model",
        "user_count",
        "hours_per_user_per_month",
    }


def test_workmail_uses_paid_user_month_dimension_instead_of_free_tier() -> None:
    class WorkMailCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonWorkMail"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            assert service_code == "AmazonWorkMail"
            return [
                priced_product(
                    service_code,
                    "EUW1-WorkMail-FreeTier-UserHrs",
                    "User-Mo",
                    0,
                ),
                priced_product(
                    service_code,
                    "EUW1-WorkMail-NormalTier-UserHrs",
                    "User-Mo",
                    4,
                ),
            ]

    requirement = ServiceRequirement(
        service="work_mail",
        calculator_service_name="Amazon WorkMail",
        region="eu-west-1",
        requirements={"user_count": 1000},
    )

    selected = GenericOfficialPlugin(None, WorkMailCatalog()).select(  # type: ignore[arg-type]
        requirement,
        "eu-west-1",
    )

    assert len(selected.usage_lines) == 1
    assert selected.usage_lines[0].amount == 1000
    assert selected.usage_lines[0].usage_type == "EUW1-WorkMail-NormalTier-UserHrs"
    assert "user_count" in selected.applied_requirement_fields


def test_supplement_uses_learned_alias_without_changing_customer_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = GenericOfficialPlugin(None, object())  # type: ignore[arg-type]

    def select(
        requirement: ServiceRequirement,
        default_region: str,
    ) -> SelectedResource:
        lines = []
        if "storage_gib" in requirement.requirements:
            lines.append(
                UsageLine(
                    key="storage",
                    service_code="AmazonLogs",
                    usage_type="TimedStorage-ByteHrs",
                    operation="",
                    amount=float(requirement.requirements["storage_gib"]),
                    source_fields=["storage_gib"],
                )
            )
        return SelectedResource(
            service="future_logs",
            display_name="Future Logs",
            region=default_region,
            model="official",
            architecture="managed",
            specifications={},
            official_product={"source": "AWS Price List"},
            rationale="official",
            usage_lines=lines,
        )

    monkeypatch.setattr(plugin, "select", select)
    requirement = ServiceRequirement(
        service="future_logs",
        requirements={"retained_logs_gib": 1024},
        field_sources={
            "requirements.retained_logs_gib": "customer_text",
            "_fact_purpose_alias.retained_logs_gib": "storage_gib",
        },
        field_evidence={
            "requirements.retained_logs_gib": "日志存储1T",
        },
    )
    base = SelectedResource(
        service="future_logs",
        display_name="Future Logs",
        region="ap-southeast-1",
        model="official",
        architecture="managed",
        specifications={},
        official_product={"source": "AWS Price List"},
        rationale="official",
    )

    selected = plugin.supplement_selection(
        requirement,
        base,
        ["requirements.retained_logs_gib"],
        "ap-southeast-1",
    )

    assert requirement.requirements == {"retained_logs_gib": 1024}
    assert selected.usage_lines[0].amount == 1024
    assert selected.usage_lines[0].source_fields == ["retained_logs_gib"]
    assert unconsumed_customer_pricing_facts(requirement, selected) == []


def test_supplement_resolves_shared_offer_dimension_for_any_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SharedOfferCatalog:
        @staticmethod
        def location(region: str) -> str:
            assert region == "ap-southeast-5"
            return "Asia Pacific (Malaysia)"

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            assert service_code == "AWSDataTransfer"
            assert filters == {
                "fromLocation": "Asia Pacific (Malaysia)",
                "toLocation": "External",
                "transferType": "AWS Outbound",
            }
            return [
                priced_product(
                    "AWSDataTransfer",
                    "APS12-DataTransfer-Out-Bytes",
                    "GB",
                    0.12,
                    operation="DataTransfer-Out-Bytes",
                )
            ]

    plugin = GenericOfficialPlugin(None, SharedOfferCatalog())  # type: ignore[arg-type]
    requirement = ServiceRequirement(
        service="apigateway",
        region="ap-southeast-5",
        source_text="API Gateway，每月请求量1亿次，每月公网下行流量2T",
        requirements={
            "api_type": "http",
            "requests": 100_000_000,
            "data_transfer_out_gib": 2048,
        },
        field_sources={
            "requirements.requests": "customer_text",
            "requirements.data_transfer_out_gib": "customer_text",
        },
        field_evidence={
            "requirements.requests": "每月请求量1亿次",
            "requirements.data_transfer_out_gib": "每月公网下行流量2T",
        },
    )
    base = SelectedResource(
        service="apigateway",
        display_name="Amazon API Gateway HTTP API",
        region="ap-southeast-5",
        model="HTTP API",
        architecture="每月 1 亿次请求",
        specifications={"apiType": "HTTP", "requests": 100_000_000},
        official_product={"source": "AWS Price List"},
        rationale="official",
        usage_lines=[
            UsageLine(
                key="requests",
                service_code="AmazonApiGateway",
                usage_type="APS12-ApiGatewayHttpApi",
                operation="ApiGatewayHttpApi",
                amount=100_000_000,
                source_fields=["requests", "api_type"],
            )
        ],
    )
    monkeypatch.setattr(plugin, "select", lambda _requirement, _region: base)

    selected = plugin.supplement_selection(
        requirement,
        base,
        ["requirements.data_transfer_out_gib"],
        "ap-southeast-1",
    )

    assert [(line.service_code, line.amount) for line in selected.usage_lines] == [
        ("AmazonApiGateway", 100_000_000),
        ("AWSDataTransfer", 2048),
    ]
    assert selected.usage_lines[1].source_fields == ["data_transfer_out_gib"]
    assert unconsumed_customer_pricing_facts(requirement, selected) == []


def test_shared_offer_dimension_preserves_per_resource_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SharedOfferCatalog:
        @staticmethod
        def location(_region: str) -> str:
            return "Asia Pacific (Singapore)"

        @staticmethod
        def products(
            _service_code: str,
            _filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            return [priced_product("AWSDataTransfer", "DataTransfer-Out", "GB", 0.1)]

    plugin = GenericOfficialPlugin(None, SharedOfferCatalog())  # type: ignore[arg-type]
    requirement = ServiceRequirement(
        service="future_service",
        region="ap-southeast-1",
        quantity=3,
        requirements={"data_transfer_out_gib": 100},
        field_sources={
            "requirements.data_transfer_out_gib": "customer_text",
        },
        field_evidence={
            "requirements.data_transfer_out_gib": "每节点公网出站100G",
        },
        field_scopes={"data_transfer_out_gib": "per_resource"},
    )
    base = SelectedResource(
        service="future_service",
        display_name="Future Service",
        region="ap-southeast-1",
        model="official",
        architecture="managed",
        specifications={},
        official_product={"source": "AWS Price List"},
        rationale="official",
    )
    monkeypatch.setattr(plugin, "select", lambda _requirement, _region: base)

    selected = plugin.supplement_selection(
        requirement,
        base,
        ["requirements.data_transfer_out_gib"],
        "ap-southeast-1",
    )

    assert selected.usage_lines[0].amount == 300


def test_shared_role_storage_uses_declared_topology_and_stable_ebs_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = priced_product(
        "AmazonEC2",
        "EUW1-EBS:VolumeUsage.gp3",
        "GB-Mo",
        0.09,
    )
    storage["product"]["attributes"].update(
        {
            "regionCode": "eu-west-1",
            "volumeApiName": "gp3",
        }
    )

    class SharedOfferCatalog:
        @staticmethod
        def matching_products(
            service_code: str,
            filters: dict[str, str],
            predicate: object,
            *,
            max_pages: int = 20,
            fallback_filters: dict[str, str] | None = None,
            fallback_predicate: object | None = None,
        ) -> list[dict]:
            assert service_code == "AmazonEC2"
            assert filters == {
                "regionCode": "eu-west-1",
                "productFamily": "Storage",
                "volumeApiName": "gp3",
            }
            assert fallback_filters == {
                "regionCode": "eu-west-1",
                "volumeApiName": "gp3",
            }
            selector = fallback_predicate or predicate
            assert callable(selector)
            assert selector(storage["product"]["attributes"])
            return [storage]

    plugin = GenericOfficialPlugin(None, SharedOfferCatalog())  # type: ignore[arg-type]
    requirement = ServiceRequirement(
        service="emr",
        region="eu-west-1",
        quantity=2,
        requirements={
            "core_nodes": 4,
            "core_storage_gib_per_node": 500,
        },
        field_sources={
            "quantity": "customer_text",
            "requirements.core_nodes": "customer_text",
            "requirements.core_storage_gib_per_node": "customer_text",
        },
        field_evidence={
            "quantity": "2套集群",
            "requirements.core_nodes": "每套4个核心节点",
            "requirements.core_storage_gib_per_node": "核心节点每台500GB EBS",
        },
    )
    base = SelectedResource(
        service="emr",
        display_name="Amazon EMR",
        region="eu-west-1",
        model="official",
        architecture="managed",
        specifications={},
        official_product={"source": "AWS Price List"},
        rationale="official",
    )
    monkeypatch.setattr(plugin, "select", lambda _requirement, _region: base)

    selected = plugin.supplement_selection(
        requirement,
        base,
        ["requirements.core_storage_gib_per_node"],
        "eu-west-1",
    )

    assert len(selected.usage_lines) == 1
    assert selected.usage_lines[0].service_code == "AmazonEC2"
    assert selected.usage_lines[0].usage_type == "EUW1-EBS:VolumeUsage.gp3"
    assert selected.usage_lines[0].amount == 4000
    assert selected.usage_lines[0].source_fields == [
        "core_nodes",
        "core_storage_gib_per_node",
        "quantity",
    ]
    assert unconsumed_customer_pricing_facts(requirement, selected) == []


def test_supplement_does_not_alias_over_a_different_customer_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = GenericOfficialPlugin(None, object())  # type: ignore[arg-type]

    def select(
        requirement: ServiceRequirement,
        default_region: str,
    ) -> SelectedResource:
        return SelectedResource(
            service="future_logs",
            display_name="Future Logs",
            region=default_region,
            model="official",
            architecture="managed",
            specifications={},
            official_product={"source": "AWS Price List"},
            rationale="official",
            usage_lines=[
                UsageLine(
                    key="storage",
                    service_code="AmazonLogs",
                    usage_type="TimedStorage-ByteHrs",
                    operation="",
                    amount=float(requirement.requirements["storage_gib"]),
                    source_fields=["storage_gib"],
                )
            ],
        )

    monkeypatch.setattr(plugin, "select", select)
    requirement = ServiceRequirement(
        service="future_logs",
        requirements={"retained_logs_gib": 1024, "storage_gib": 2048},
        field_sources={
            "requirements.retained_logs_gib": "customer_text",
            "requirements.storage_gib": "customer_text",
            "_fact_purpose_alias.retained_logs_gib": "storage_gib",
        },
        field_evidence={
            "requirements.retained_logs_gib": "日志保留1T",
            "requirements.storage_gib": "归档存储2T",
        },
    )
    base = SelectedResource(
        service="future_logs",
        display_name="Future Logs",
        region="ap-southeast-1",
        model="official",
        architecture="managed",
        specifications={},
        official_product={"source": "AWS Price List"},
        rationale="official",
    )

    selected = plugin.supplement_selection(
        requirement,
        base,
        ["requirements.retained_logs_gib"],
        "ap-southeast-1",
    )

    assert selected.usage_lines == []


def test_curated_generic_service_uses_official_profile_field_bindings() -> None:
    product = priced_product(
        "AWSCloudMap",
        "APS1-ServiceInstance",
        "Resources",
        0.10,
        operation="RegisterInstance",
    )

    class CloudMapCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AWSCloudMap"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            assert service_code == "AWSCloudMap"
            return [product]

    class CloudMapDiscovery:
        calls = 0

        @classmethod
        def ensure_profile(cls, **_: object) -> dict[str, object]:
            cls.calls += 1
            return {
                "status": "verified",
                "service_code": "AWSCloudMap",
                "fields": ["service_instances"],
                "field_bindings": [
                    {
                        "field": "service_instances",
                        "label": "服务实例数量",
                        "usage_type": "APS1-ServiceInstance",
                        "operation": "RegisterInstance",
                        "unit": "Resources",
                    }
                ],
                "dimensions": [],
            }

    selected = GenericOfficialPlugin(
        None,  # type: ignore[arg-type]
        CloudMapCatalog(),  # type: ignore[arg-type]
        CloudMapDiscovery(),  # type: ignore[arg-type]
    ).select(
        ServiceRequirement(
            service="cloud_map",
            calculator_service_name="AWS Cloud Map",
            region="ap-southeast-1",
            requirements={"service_instances": 12},
        ),
        "ap-southeast-1",
    )

    assert CloudMapDiscovery.calls == 1
    assert selected.usage_lines[0].amount == 12
    assert selected.reference_rates == []


def test_backup_retention_is_consumed_as_configuration_not_second_charge() -> None:
    product = priced_product(
        "AWSBackup",
        "APS2-WarmStorage-ByteHrs",
        "GB-Mo",
        0.05,
        operation="Storage",
        group="AWS-BackupStorage",
    )

    class BackupCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AWSBackup"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            assert service_code == "AWSBackup"
            return [product]

    class BackupDiscovery:
        @staticmethod
        def ensure_profile(**_: object) -> dict[str, object]:
            return {
                "status": "verified",
                "service_code": "AWSBackup",
                "fields": ["backup_storage_gib"],
                "field_bindings": [
                    {
                        "field": "backup_storage_gib",
                        "label": "备份存储",
                        "usage_type": "APS2-WarmStorage-ByteHrs",
                        "operation": "Storage",
                        "unit": "GB-Mo",
                    }
                ],
                "dimensions": [],
            }

    requirement = ServiceRequirement(
        service="backup",
        calculator_service_name="AWS Backup",
        region="ap-southeast-2",
        requirements={
            "backup_storage_gib": 5120,
            "backup_frequency": "daily",
            "backup_retention_days": 30,
        },
        field_sources={
            "requirements.backup_storage_gib": "customer_text",
            "requirements.backup_retention_days": "customer_text",
        },
        field_evidence={
            "requirements.backup_storage_gib": "备份数据容量5T",
            "requirements.backup_retention_days": "保留30天",
        },
    )
    selected = GenericOfficialPlugin(
        None,  # type: ignore[arg-type]
        BackupCatalog(),  # type: ignore[arg-type]
        BackupDiscovery(),  # type: ignore[arg-type]
    ).select(requirement, "ap-southeast-2")

    assert selected.usage_lines[0].amount == 5120
    assert selected.usage_lines[0].source_fields == ["backup_storage_gib"]
    assert "backup_frequency" in selected.applied_requirement_fields
    assert "backup_retention_days" in selected.applied_requirement_fields
    assert unconsumed_customer_pricing_facts(requirement, selected) == []


def test_backup_official_child_keeps_efs_storage_and_restore_out_of_ebs_rates() -> None:
    rows = [
        ("APS1-WarmStorage-ByteHrs-EFS", "Storage", "GB-month", 0.06,
         "warm backup storage for EFS", "warm_storage_gib"),
        ("APS1-ColdStorage-ByteHrs-EFS", "Storage", "GB-month", 0.012,
         "cold backup storage for EFS", "cold_storage_gib"),
        ("APS1-Restore-WarmBytes-EFS", "RestoreRecoveryPoint", "GB", 0.024,
         "restore from warm backup storage for EFS", "restore_gib"),
        ("APS1-Restore-WarmBytes-EBS", "RestoreRecoveryPoint", "GB", 0.0,
         "restore from warm backup storage for EBS", "restore_gib"),
    ]
    products = []
    bindings = []
    dimensions = []
    for usage_type, operation, unit, price, description, field in rows:
        product = priced_product(
            "AWSBackup", usage_type, unit, price, operation=operation
        )
        product["terms"]["OnDemand"]["term"]["priceDimensions"]["dimension"][
            "description"
        ] = description
        products.append(product)
        bindings.append(
            {
                "field": field,
                "label": field,
                "usage_type": usage_type,
                "operation": operation,
                "unit": unit,
                "description": description,
            }
        )
        dimensions.append(
            {
                "usage_type": usage_type,
                "operation": operation,
                "unit": unit,
                "price": price,
                "description": description,
            }
        )

    class BackupCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AWSBackup"]

        @staticmethod
        def products(*_: object, **__: object) -> list[dict]:
            return products

    class BackupDiscovery:
        @staticmethod
        def ensure_profile(**_: object) -> dict[str, object]:
            return {
                "status": "verified",
                "service_code": "AWSBackup",
                "fields": ["warm_storage_gib", "cold_storage_gib", "restore_gib"],
                "field_bindings": bindings,
                "dimensions": dimensions,
            }

    requirement = ServiceRequirement(
        service="backup",
        calculator_service_name="AWS Backup",
        region="ap-southeast-1",
        requirements={
            "protected_service": "EFS",
            "backup_storage_gib": 5120,
            "restore_gib": 500,
        },
        field_sources={
            "requirements.protected_service": "customer_confirmation",
            "requirements.backup_storage_gib": "customer_text",
            "requirements.restore_gib": "customer_text",
        },
        field_evidence={
            "requirements.protected_service": "客户选择 EFS Backup",
            "requirements.backup_storage_gib": "备份容量5TB",
            "requirements.restore_gib": "每月恢复500GB",
        },
    )

    selected = GenericOfficialPlugin(
        None,  # type: ignore[arg-type]
        BackupCatalog(),  # type: ignore[arg-type]
        BackupDiscovery(),  # type: ignore[arg-type]
    ).select(requirement, "ap-southeast-1")

    identities = {line.usage_type: line for line in selected.usage_lines}
    assert set(identities) == {
        "APS1-WarmStorage-ByteHrs-EFS",
        "APS1-Restore-WarmBytes-EFS",
    }
    assert identities["APS1-WarmStorage-ByteHrs-EFS"].amount == 5120
    assert identities["APS1-Restore-WarmBytes-EFS"].amount == 500
    assert unconsumed_customer_pricing_facts(requirement, selected) == []


def test_profile_customer_amount_replaces_same_dimension_reference_row() -> None:
    product = priced_product(
        "AmazonCognito",
        "APS1-CognitoUserPoolsMAU",
        "Users",
        0.0055,
        group="CognitoUserPoolsOperation",
    )

    class CognitoCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonCognito"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            return [product]

    class CognitoDiscovery:
        @staticmethod
        def ensure_profile(**_: object) -> dict[str, object]:
            return {
                "status": "verified",
                "service_code": "AmazonCognito",
                "fields": ["monthly_active_users"],
                "field_bindings": [
                    {
                        "field": "monthly_active_users",
                        "label": "每月活跃用户数",
                        "usage_type": "APS1-CognitoUserPoolsMAU",
                        "operation": "",
                        "unit": "Users",
                    }
                ],
                "dimensions": [],
            }

    selected = GenericOfficialPlugin(
        None,  # type: ignore[arg-type]
        CognitoCatalog(),  # type: ignore[arg-type]
        CognitoDiscovery(),  # type: ignore[arg-type]
    ).select(
        ServiceRequirement(
            service="cognito",
            region="ap-southeast-1",
            requirements={"monthly_active_users": 20_000},
        ),
        "ap-southeast-1",
    )

    assert selected.usage_lines[0].amount == 20_000
    assert selected.reference_rates == []


def test_efs_standard_regional_never_uses_archive_early_delete_rate() -> None:
    products = [
        priced_product(
            "AmazonEFS",
            "APS1-ArchiveEarlyDelete-SmallFiles",
            "GB-Mo",
            0.01,
            operation="Delete",
        ),
        priced_product(
            "AmazonEFS", "APS1-TimedStorage-ByteHrs", "GB-Mo", 0.36
        ),
        priced_product(
            "AmazonEFS", "APS1-IATimedStorage-ET-ByteHrs", "GB-Mo", 0.02
        ),
    ]
    products[-1]["product"]["attributes"]["instanceType"] = "t4g.small"
    products[0]["product"]["attributes"]["storageClass"] = (
        "Archive-EarlyDelete-SmallFiles"
    )
    products[1]["product"]["attributes"]["storageClass"] = "General Purpose"
    products[2]["product"]["attributes"]["storageClass"] = "Infrequent Access-ET"
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))
    requirement = ServiceRequirement(
        service="efs",
        quantity=1,
        requirements={
            "storage_gib": 6144,
            "storage_class": "standard",
            "deployment_type": "regional",
        },
    )

    selected = GenericOfficialPlugin._semantic_rates(requirement, rates)

    assert len(selected) == 1
    assert selected[0][1] == 6144
    assert selected[0][2][0] == 0.36
    assert selected[0][2][2] == "APS1-TimedStorage-ByteHrs"
    assert selected[0][1] * selected[0][2][0] == pytest.approx(2211.84)


def test_efs_one_zone_ia_storage_and_read_usage_keep_complete_fact_lineage() -> None:
    storage = priced_product(
        "AmazonEFS", "APN1-IATimedStorage-Z-ByteHrs", "GB-Mo", 0.016
    )
    storage["product"]["attributes"]["storageClass"] = (
        "One Zone-Infrequent Access"
    )
    read = priced_product(
        "AmazonEFS",
        "APN1-ETDataAccessBytes",
        "GB",
        0.04,
        operation="Read",
    )
    rates = []
    for product in (storage, read):
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="efs",
            requirements={
                "storage_gib": 20 * 1024,
                "storage_class": "infrequent_access",
                "deployment_type": "one_zone",
                "data_out_gib": 5 * 1024,
            },
        ),
        rates,
    )

    assert [item[1] for item in selected] == [20 * 1024, 5 * 1024]
    assert selected[0][2][4]["_astra_source_fields"] == [
        "storage_gib",
        "storage_class",
        "deployment_type",
    ]
    assert selected[1][2][4]["_astra_source_fields"] == ["data_out_gib"]


def test_discovered_daily_inventory_and_top_level_hours_use_monthly_amounts() -> None:
    bucket_product = priced_product(
        "AmazonMacie",
        "APN2-PaidDataInventoryEvaluation-Bucket-Days",
        "Bucket-days",
        0.0033,
    )
    hour_product = priced_product(
        "AWSNetworkFirewall",
        "APN2-FirewallEndpoint-Hours",
        "Hourly",
        0.395,
        operation="Operation:Metering",
    )
    rates = []
    for product in (bucket_product, hour_product):
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    bucket_result = GenericOfficialPlugin._auto_semantic_rates(
        ServiceRequirement(
            service="macie",
            requirements={"bucket_count": 500},
        ),
        rates,
        profile={
            "field_bindings": [
                {
                    "field": "bucket_count",
                    "label": "存储桶数量",
                    "usage_type": "APN2-PaidDataInventoryEvaluation-Bucket-Days",
                    "operation": "",
                    "unit": "Bucket-days",
                }
            ]
        },
    )
    hour_result = GenericOfficialPlugin._auto_semantic_rates(
        ServiceRequirement(
            service="network_firewall",
            quantity=2,
            hours_per_month=730,
            source_text="2 个端点，每个端点每月运行 730 小时",
            field_evidence={"hours_per_month": "每月运行 730 小时"},
        ),
        rates,
        profile={
            "field_bindings": [
                {
                    "field": "hours_per_month",
                    "label": "运行时长",
                    "usage_type": "APN2-FirewallEndpoint-Hours",
                    "operation": "Operation:Metering",
                    "unit": "Hourly",
                }
            ]
        },
    )

    assert bucket_result[0][1] == 500 * 30
    assert hour_result[0][1] == 2 * 730


def test_network_firewall_endpoint_count_derives_endpoint_hours() -> None:
    product = priced_product(
        "AWSNetworkFirewall",
        "APN2-FirewallEndpoint-Hours",
        "Hourly",
        0.395,
        operation="Operation:Metering",
    )
    price, unit = PricingCatalog.on_demand_unit_rate(product)
    _, usage_type, operation = PricingCatalog.billing_identity(product)

    result = GenericOfficialPlugin._auto_semantic_rates(
        ServiceRequirement(
            service="network_firewall",
            hours_per_month=730,
            requirements={"endpoint_count": 2},
        ),
        [(price, unit, usage_type, operation, product)],
        profile={
            "field_bindings": [
                {
                    "field": "endpoint_hours",
                    "label": "端点运行时长",
                    "usage_type": usage_type,
                    "operation": operation,
                    "unit": unit,
                }
            ]
        },
    )

    assert result[0][1] == 2 * 730


def test_timestream_liveanalytics_excludes_influx_and_derives_storage_usage() -> None:
    live_products = (
        priced_product("AmazonTimestream", "EUC1-DataIngestion-Bytes", "GB", 0.6),
        priced_product("AmazonTimestream", "EUC1-MemoryStore-ByteHrs", "GB-Hours", 0.04),
        priced_product("AmazonTimestream", "EUC1-MagneticStore-ByteHrs", "GB-Mo", 0.03),
    )
    influx = priced_product(
        "AmazonTimestream", "EUC1-InstanceUsage-Db.influx.medium", "Hrs", 0.1
    )
    influx["product"]["attributes"].update(
        {"instanceType": "db.influx.medium", "vcpu": "1", "memory": "8 GiB"}
    )
    rates = []
    for product in (*live_products, influx):
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))
    profile = {
        "field_bindings": [
            {
                "field": field,
                "label": field,
                "usage_type": usage_type,
                "operation": "",
                "unit": unit,
            }
            for field, usage_type, unit in (
                ("data_in_gib", "EUC1-DataIngestion-Bytes", "GB"),
                ("memory_store_gib_hours", "EUC1-MemoryStore-ByteHrs", "GB-Hours"),
                ("magnetic_store_gib_months", "EUC1-MagneticStore-ByteHrs", "GB-Mo"),
            )
        ]
    }
    requirement = ServiceRequirement(
        service="timestream",
        requirements={
            "product_variant": "live_analytics",
            "write_records": 400_000_000,
            "memory_retention_hours": 24,
            "magnetic_retention_days": 180,
        },
    )

    result = GenericOfficialPlugin._auto_semantic_rates(
        requirement,
        rates,
        profile=profile,
    )

    amounts = {item[2][2]: item[1] for item in result}
    monthly_ingest_gib = 400_000_000 / 1_048_576
    assert amounts["EUC1-DataIngestion-Bytes"] == pytest.approx(monthly_ingest_gib)
    assert amounts["EUC1-MemoryStore-ByteHrs"] == pytest.approx(
        monthly_ingest_gib * 24
    )
    assert amounts["EUC1-MagneticStore-ByteHrs"] == pytest.approx(
        monthly_ingest_gib * 180 / 30
    )
    assert all("influx" not in item[2][2].casefold() for item in result)


def test_timestream_direct_storage_capacity_binds_to_magnetic_gib_months() -> None:
    product = priced_product(
        "AmazonTimestream",
        "USE1-MagneticStore-ByteHrs",
        "GB-Mo",
        0.03,
    )
    price, unit = PricingCatalog.on_demand_unit_rate(product)
    _, usage_type, operation = PricingCatalog.billing_identity(product)
    result = GenericOfficialPlugin._auto_semantic_rates(
        ServiceRequirement(
            service="timestream",
            requirements={
                "product_variant": "live_analytics",
                "storage_gib": 2048,
            },
        ),
        [(price, unit, usage_type, operation, product)],
        profile={
            "field_bindings": [
                {
                    "field": "magnetic_store_gib_months",
                    "label": "磁性存储",
                    "usage_type": usage_type,
                    "operation": operation,
                    "unit": unit,
                }
            ]
        },
    )

    assert result[0][1] == 2048


def test_profile_processing_hours_are_converted_to_official_minutes() -> None:
    product = priced_product(
        "AWSElementalMediaConvert",
        "IAD-B-AVC-HD-S-30",
        "minutes",
        0.012,
    )
    price, unit = PricingCatalog.on_demand_unit_rate(product)
    _, usage_type, operation = PricingCatalog.billing_identity(product)
    requirement = ServiceRequirement(
        service="elemental_media_convert",
        source_text="视频转码，每月处理高清视频5000小时",
        requirements={"processing_hours": 5000},
    )

    result = GenericOfficialPlugin._auto_semantic_rates(
        requirement,
        [(price, unit, usage_type, operation, product)],
        profile={
            "field_bindings": [
                {
                    "field": "processing_hours",
                    "label": "处理时长",
                    "usage_type": usage_type,
                    "operation": operation,
                    "unit": unit,
                }
            ]
        },
    )

    assert result[0][1] == 5000 * 60


def test_dms_shape_constraints_do_not_turn_task_count_into_instance_count() -> None:
    tiny = priced_product(
        "AWSDatabaseMigrationSvc", "APS3-InstanceUsg:dms.t2.micro", "Hrs", 0.02
    )
    tiny["product"]["attributes"].update(
        {"instanceType": "t2.micro", "vcpu": "1", "memory": "1 GiB"}
    )
    matching = priced_product(
        "AWSDatabaseMigrationSvc", "APS3-InstanceUsg:dms.r5.xlarge", "Hrs", 0.5
    )
    matching["product"]["attributes"].update(
        {"instanceType": "r5.xlarge", "vcpu": "4", "memory": "32 GiB"}
    )
    rates = []
    for product in (tiny, matching):
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    result = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="dms",
            hours_per_month=730,
            requirements={"vcpu": 4, "memory_gib": 16, "task_count": 3},
        ),
        rates,
    )

    assert result[0][2][2].endswith("dms.r5.xlarge")
    assert result[0][1] == 730


def test_dms_per_instance_storage_uses_replication_instance_count() -> None:
    storage = priced_product(
        "AWSDatabaseMigrationSvc",
        "DMS:GP2-Storage",
        "GB-Mo",
        0.115,
        operation="CreateDMSInstance",
    )
    price, unit = PricingCatalog.on_demand_unit_rate(storage)
    _, usage_type, operation = PricingCatalog.billing_identity(storage)
    requirement = ServiceRequirement(
        service="dms",
        requirements={"replication_instances": 2, "storage_gib": 200},
        field_scopes={"storage_gib": "per_resource"},
    )

    result = GenericOfficialPlugin._semantic_rates(
        requirement,
        [(price, unit, usage_type, operation, storage)],
    )

    assert result[0][1] == 400


def test_dms_task_count_is_configuration_context_not_a_second_meter() -> None:
    product = priced_product(
        "AWSDatabaseMigrationSvc",
        "APS3-InstanceUsg:dms.r6i.large",
        "Hrs",
        0.5,
        operation="CreateDMSInstance",
    )
    product["product"]["attributes"].update(
        {"instanceType": "r6i.large", "vcpu": "2", "memory": "16 GiB"}
    )

    class DmsCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AWSDatabaseMigrationSvc"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
        ) -> list[dict]:
            assert service_code == "AWSDatabaseMigrationSvc"
            return [product]

    plugin = GenericOfficialPlugin(None, DmsCatalog())  # type: ignore[arg-type]
    requirement = ServiceRequirement(
        service="dms",
        region="ap-northeast-1",
        hours_per_month=730,
        requirements={
            "requested_model": "dms.r6i.large",
            "replication_instances": 2,
            "task_count": 4,
        },
        field_sources={
            "hours_per_month": "customer_text",
            "requirements.requested_model": "customer_text",
            "requirements.replication_instances": "customer_text",
            "requirements.task_count": "customer_text",
        },
        field_evidence={
            "hours_per_month": "每月运行730小时",
            "requirements.requested_model": "dms.r6i.large",
            "requirements.replication_instances": "2个复制实例",
            "requirements.task_count": "4个迁移任务",
        },
    )

    selected = plugin.select(requirement, "ap-northeast-1")

    assert len(selected.usage_lines) == 1
    assert selected.usage_lines[0].amount == 1460
    assert "task_count" in selected.applied_requirement_fields
    assert unconsumed_customer_pricing_facts(requirement, selected) == []


def test_kms_semantic_rates_bind_both_customer_usage_facts() -> None:
    products = [
        priced_product("awskms", "APS1-KMS-Keys", "Keys", 1.0),
        priced_product("awskms", "APS1-KMS-Requests", "Requests", 0.000003),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))
    requirement = ServiceRequirement(
        service="kms",
        requirements={"key_count": 50, "requests": 10_000_000},
    )

    selected = GenericOfficialPlugin._semantic_rates(requirement, rates)

    assert [(item[1], item[2][4]["_astra_source_fields"]) for item in selected] == [
        (50.0, ["key_count"]),
        (10_000_000.0, ["requests"]),
    ]


def test_xray_semantic_rates_bind_recorded_and_retrieved_trace_facts() -> None:
    products = [
        priced_product(
            "AWSXRay",
            "APN2-XRay-TracesStored",
            "traces",
            0.000005,
            group="Traces Stored",
        ),
        priced_product(
            "AWSXRay",
            "APN2-XRay-TracesAccessed",
            "traces",
            0.0000005,
            operation="XRay-Traces-Retrieved",
            group="Traces Retrieved",
        ),
        priced_product(
            "AWSXRay",
            "APN2-XRay-TracesAccessed",
            "traces",
            0.0000005,
            operation="XRay-Traces-Scanned",
            group="Traces Scanned",
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))
    requirement = ServiceRequirement(
        service="xray",
        requirements={
            "traces_recorded": 80_000_000,
            "traces_retrieved": 20_000_000,
        },
    )

    selected = GenericOfficialPlugin._semantic_rates(requirement, rates)

    assert [(item[1], item[2][4]["_astra_source_fields"]) for item in selected] == [
        (80_000_000.0, ["traces_recorded"]),
        (20_000_000.0, ["traces_retrieved"]),
    ]
    assert selected[1][2][3] == "XRay-Traces-Retrieved"


def test_guardduty_semantic_rates_distinguish_flow_log_bytes_from_cloudtrail_events() -> None:
    products = [
        priced_product(
            "AmazonGuardDuty",
            "APS4-PaidEventsAnalyzed-Bytes",
            "GB",
            1.15,
            group="Paid Data Events Processed",
        ),
        priced_product(
            "AmazonGuardDuty",
            "APS4-PaidEventsAnalyzed",
            "Events",
            0.0000046,
            group="Paid CloudTrail Events Processed",
        ),
        priced_product(
            "AmazonGuardDuty",
            "APS4-FreeEventsAnalyzed-Bytes",
            "GB",
            0,
            group="Free Data Events Processed",
        ),
        priced_product(
            "AmazonGuardDuty",
            "APS4-PaidS3DataEventsAnalyzed",
            "Events",
            0.0000008,
            group="Paid S3 Data Events Processed",
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))
    requirement = ServiceRequirement(
        service="guard_duty",
        requirements={
            "data_processed_gib": 20_480,
            "events": 1_000_000_000,
        },
    )

    selected = GenericOfficialPlugin._semantic_rates(requirement, rates)

    assert [
        (item[1], item[2][2], item[2][4]["_astra_source_fields"])
        for item in selected
    ] == [
        (20_480.0, "APS4-PaidEventsAnalyzed-Bytes", ["data_processed_gib"]),
        (1_000_000_000.0, "APS4-PaidEventsAnalyzed", ["events"]),
    ]


def test_transit_gateway_semantic_rates_bind_vpc_attachments_and_processed_bytes() -> None:
    products = [
        priced_product(
            "AmazonVPC",
            "USE1-TransitGateway-Hours",
            "hour",
            0.05,
            operation="TransitGatewayVPC",
            group="AWSTransitGateway",
        ),
        priced_product(
            "AmazonVPC",
            "USE1-TransitGateway-Bytes",
            "GigaBytes",
            0.02,
            operation="TransitGatewayVPC",
            group="AWSTransitGateway",
        ),
        priced_product(
            "AmazonVPC",
            "USE1-TransitGateway-Hours",
            "hour",
            0.05,
            operation="TransitGatewayDirectConnect",
            group="AWSTransitGateway",
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))
    requirement = ServiceRequirement(
        service="transit_gateway",
        hours_per_month=730,
        requirements={
            "attachments": 20,
            "attachment_type": "vpc",
            "data_processed_gib": 100 * 1024,
        },
    )

    selected = GenericOfficialPlugin._semantic_rates(requirement, rates)

    assert [(item[1], item[2][2], item[2][3]) for item in selected] == [
        (20 * 730, "USE1-TransitGateway-Hours", "TransitGatewayVPC"),
        (100 * 1024, "USE1-TransitGateway-Bytes", "TransitGatewayVPC"),
    ]
    assert selected[0][2][4]["_astra_source_fields"] == [
        "attachments",
        "hours_per_month",
        "attachment_type",
    ]
    assert selected[1][2][4]["_astra_source_fields"] == [
        "data_processed_gib",
        "attachment_type",
    ]


def test_direct_connect_semantic_rates_bind_dedicated_port_speed_and_outbound_data() -> None:
    products = [
        priced_product(
            "AWSDirectConnect",
            "USE1-EQNY5-PortUsage:10G",
            "Hrs",
            2.25,
            operation="CreateDirectConnectPort",
            portSpeed="10G",
        ),
        priced_product(
            "AWSDirectConnect",
            "USE1-EQNY5-HCPortUsage:10G",
            "Hrs",
            1.50,
            operation="CreateDirectConnectPort",
            portSpeed="HC-10G",
        ),
        priced_product(
            "AWSDirectConnect",
            "USE1-EQNY5-PortUsage:1G",
            "Hrs",
            0.30,
            operation="CreateDirectConnectPort",
            portSpeed="1G",
        ),
        priced_product(
            "AWSDirectConnect",
            "USE1-EQNY5-DataXfer-Out",
            "GB",
            0.02,
            transferType="IntraRegion Outbound",
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))
    requirement = ServiceRequirement(
        service="direct_connect",
        hours_per_month=730,
        requirements={
            "connection_count": 2,
            "port_speed_gbps": 10,
            "data_transfer_out_gib": 80 * 1024,
        },
    )

    selected = GenericOfficialPlugin._semantic_rates(requirement, rates)

    assert [(item[1], item[2][2]) for item in selected] == [
        (2 * 730, "USE1-EQNY5-PortUsage:10G"),
        (80 * 1024, "USE1-EQNY5-DataXfer-Out"),
    ]
    assert selected[0][2][4]["_astra_source_fields"] == [
        "connection_count",
        "port_speed_gbps",
        "hours_per_month",
    ]


def test_site_to_site_vpn_semantic_rate_excludes_large_and_concentrator_variants() -> None:
    products = [
        priced_product(
            "AmazonVPC",
            "VPN-Usage-Hours:ipsec.1",
            "Hrs",
            0.05,
            operation="CreateVpnConnection",
            group="Cloud Connectivity",
        ),
        priced_product(
            "AmazonVPC",
            "USE1-VPN-large-Usage-Hours:ipsec.1",
            "Hrs",
            0.60,
            operation="CreateVpnConnection",
            group="Cloud Connectivity",
        ),
        priced_product(
            "AmazonVPC",
            "USE1-VPN-concentrator-site-Usage-Hours:ipsec.1",
            "Hrs",
            0.10,
            operation="CreateVpnConnection",
            group="Cloud Connectivity",
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="site_to_site_vpn",
            hours_per_month=730,
            requirements={"connection_count": 4, "data_processed_gib": 12 * 1024},
        ),
        rates,
    )

    assert [(item[1], item[2][2]) for item in selected] == [
        (4 * 730, "VPN-Usage-Hours:ipsec.1"),
    ]
    assert selected[0][2][4]["_astra_source_fields"] == [
        "connection_count",
        "hours_per_month",
    ]


def test_interface_endpoint_semantic_rates_exclude_gwlb_and_resource_endpoints() -> None:
    products = [
        priced_product(
            "AmazonVPC", "USE1-VpcEndpoint-Hours", "Hrs", 0.01,
            operation="VpcEndpoint", group="VPCE:VpcEndpoint",
        ),
        priced_product(
            "AmazonVPC", "USE1-VpcEndpoint-Bytes", "GB", 0.01,
            operation="VpcEndpoint", group="VPCE:VpcEndpoint",
        ),
        priced_product(
            "AmazonVPC", "USE1-VpcEndpoint-GWLBE-Hours", "Hrs", 0.01,
            operation="VpcEndpoint", group="VPCE:VpcEndpoint",
        ),
        priced_product(
            "AmazonVPC", "USE1-VpcEndpoint-Resource-Hours", "Hrs", 0.02,
            operation="VpcResourceConsumer", group="VpcResources",
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="vpc_endpoint",
            hours_per_month=730,
            requirements={"endpoint_count": 30, "data_processed_gib": 25 * 1024},
        ),
        rates,
    )

    assert [(item[1], item[2][2]) for item in selected] == [
        (30 * 730, "USE1-VpcEndpoint-Hours"),
        (25 * 1024, "USE1-VpcEndpoint-Bytes"),
    ]


@pytest.mark.parametrize(
    ("service", "requirements", "products", "expected_usage", "expected_amount", "source_field"),
    (
        (
            "textract",
            {"document_pages": 5_000_000, "processing_mode": "async"},
            [
                priced_product("AmazonTextract", "USW2-AsyncTextPagesProcessed", "Pages", 0.0015),
                priced_product("AmazonTextract", "USW2-AsyncFormsPagesProcessed", "Pages", 0.05),
            ],
            "USW2-AsyncTextPagesProcessed",
            5_000_000,
            "document_pages",
        ),
        (
            "comprehend",
            {"characters": 200_000_000, "analysis_type": "sentiment"},
            [
                priced_product(
                    "comprehend", "USW2-DetectSentiment", "Unit", 0.0001,
                    operation="DetectSentiment",
                ),
                priced_product(
                    "comprehend", "USW2-DetectSyntax", "Unit", 0.00005,
                    operation="DetectSyntax",
                ),
            ],
            "USW2-DetectSentiment",
            2_000_000,
            "characters",
        ),
        (
            "rekognition",
            {"images": 30_000_000},
            [
                priced_product("AmazonRekognition", "USW2-ImagesProcessed", "Images Processed", 0.001),
                priced_product("AmazonRekognition", "USW2-Group1-ImagesProcessed", "Images Processed", 0.001),
            ],
            "USW2-ImagesProcessed",
            30_000_000,
            "images",
        ),
        (
            "transcribe",
            {"audio_minutes": 1_200_000, "transcription_type": "standard"},
            [
                priced_product(
                    "transcribe", "USW2-TranscribeAudio", "second", 0.0001,
                    operation="TranscribeAudio",
                ),
                priced_product(
                    "transcribe", "USW2-MedicalTranscribeAudio", "seconds", 0.00125,
                    operation="MedicalTranscribeAudio",
                ),
            ],
            "USW2-TranscribeAudio",
            72_000_000,
            "audio_minutes",
        ),
        (
            "translate",
            {"characters": 500_000_000, "translation_type": "text"},
            [
                priced_product(
                    "translate", "USW2-TranslateText", "Character", 0.000015,
                    operation="TranslateText",
                ),
                priced_product(
                    "translate", "USW2-ActiveCustomTranslationJob", "Character", 0.00006,
                    operation="ActiveCustomTranslationJob",
                ),
            ],
            "USW2-TranslateText",
            500_000_000,
            "characters",
        ),
        (
            "polly",
            {"characters": 300_000_000, "voice_engine": "standard"},
            [
                priced_product("AmazonPolly", "USW2-SynthesizeSpeech-Characters", "Characters", 0.000004),
                priced_product("AmazonPolly", "USW2-SynthesizeSpeechNeural-Characters", "Characters", 0.000016),
            ],
            "USW2-SynthesizeSpeech-Characters",
            300_000_000,
            "characters",
        ),
    ),
)
def test_managed_ai_semantic_rates_bind_customer_units_to_exact_official_dimension(
    service: str,
    requirements: dict[str, object],
    products: list[dict[str, object]],
    expected_usage: str,
    expected_amount: float,
    source_field: str,
) -> None:
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(service=service, requirements=requirements),
        rates,
    )

    assert len(selected) == 1
    assert selected[0][1] == expected_amount
    assert selected[0][2][2] == expected_usage
    assert selected[0][2][4]["_astra_source_fields"] == [source_field]


def test_sagemaker_endpoint_rate_consumes_instance_count_and_runtime() -> None:
    product = priced_product(
        "AmazonSageMaker",
        "USW2-Host:ml.g5.2xlarge",
        "Hrs",
        1.515,
        operation="RunInstance",
    )
    product["product"]["attributes"]["instanceType"] = "ml.g5.2xlarge"
    price, unit = PricingCatalog.on_demand_unit_rate(product)
    _, usage_type, operation = PricingCatalog.billing_identity(product)
    requirement = ServiceRequirement(
        service="sagemaker",
        hours_per_month=730,
        requirements={
            "requested_model": "ml.g5.2xlarge",
            "instance_count": 4,
            "endpoint_type": "real-time",
        },
    )

    selected = GenericOfficialPlugin._semantic_rates(
        requirement,
        [(price, unit, usage_type, operation, product)],
    )

    assert selected[0][1] == 4 * 730
    assert selected[0][2][4]["_astra_source_fields"] == [
        "instance_count",
        "hours_per_month",
        "requested_model",
        "endpoint_type",
    ]

    requirement.requirements["instance_hours"] = 730
    selected = GenericOfficialPlugin._semantic_rates(
        requirement,
        [(price, unit, usage_type, operation, product)],
    )
    assert selected[0][1] == 4 * 730
    assert selected[0][2][4]["_astra_source_fields"] == [
        "instance_count",
        "instance_hours",
        "requested_model",
        "endpoint_type",
    ]


def test_managed_instance_shape_is_enriched_from_official_ec2_specification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = priced_product(
        "ElasticMapReduce",
        "APS1-BoxUsage:m6g.xlarge",
        "Hrs",
        0.05,
    )
    product["product"]["attributes"]["instanceType"] = "m6g.xlarge"
    rate = (0.05, "Hrs", "APS1-BoxUsage:m6g.xlarge", "", product)

    class FakeExecutor:
        def __init__(self, clients: object) -> None:
            pass

        def execute(self, **_: object) -> dict:
            return {
                "InstanceTypes": [
                    {
                        "InstanceType": "m6g.xlarge",
                        "VCpuInfo": {"DefaultVCpus": 4},
                        "MemoryInfo": {"SizeInMiB": 16384},
                    }
                ]
            }

    monkeypatch.setattr(
        "app.services.plugins.generic_official.ReadOnlyAwsQueryExecutor",
        FakeExecutor,
    )
    plugin = GenericOfficialPlugin(object(), object())  # type: ignore[arg-type]

    enriched = plugin._enrich_missing_instance_shapes(
        ServiceRequirement(
            service="emr",
            requirements={"master_vcpu": 4, "master_memory_gib": 16},
        ),
        [rate],
        "ap-southeast-1",
    )

    attrs = PricingCatalog.attributes(enriched[0][4])
    assert attrs["vcpu"] == "4.0"
    assert attrs["memory"] == "16 GiB"


def test_fsx_lustre_uses_exact_official_throughput_tier() -> None:
    products = []
    for tier, price in ((125, 0.14), (250, 0.19), (500, 0.27)):
        item = priced_product(
            "AmazonFSx",
            f"APS1-Storage.SSD.{tier}",
            "GB-Mo",
            price,
            operation="CreateFileSystem:Lustre",
        )
        item["product"]["attributes"].update(
            {
                "fileSystemType": "Lustre",
                "storageType": "SSD",
                "throughputCapacity": str(tier),
            }
        )
        products.append(item)
    rates = []
    for item in products:
        price, unit = PricingCatalog.on_demand_unit_rate(item)
        _, usage_type, operation = PricingCatalog.billing_identity(item)
        rates.append((price, unit, usage_type, operation, item))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="fsx",
            quantity=1,
            requirements={
                "file_system_type": "lustre",
                "storage_gib": 6144,
                "throughput_mbps_per_tib": 250,
            },
        ),
        rates,
    )

    assert len(selected) == 1
    assert selected[0][1] == 6144
    assert selected[0][2][2].endswith("Storage.SSD.250")
    assert selected[0][2][4]["_astra_source_fields"] == [
        "storage_gib",
        "file_system_type",
        "throughput_mbps_per_tib",
    ]


def test_storage_transfer_services_use_exact_official_meter_identities() -> None:
    scenarios = [
        (
            ServiceRequirement(
                service="s3_glacier_deep_archive",
                requirements={
                    "storage_gib": 500 * 1024,
                    "data_retrieval_gib": 2 * 1024,
                    "retrieval_tier": "standard",
                },
            ),
            [
                priced_product(
                    "AmazonS3GlacierDeepArchive",
                    "APN1-EarlyDelete-GDA",
                    "GB-Mo",
                    0.002,
                ),
                priced_product(
                    "AmazonS3GlacierDeepArchive",
                    "APN1-TimedStorage-GDA-ByteHrs",
                    "GB-Mo",
                    0.002,
                ),
                priced_product(
                    "AmazonS3GlacierDeepArchive",
                    "APN1-Bulk-Retrieval-Bytes",
                    "GB",
                    0.005,
                    operation="DeepArchiveRestoreObject",
                ),
                priced_product(
                    "AmazonS3GlacierDeepArchive",
                    "APN1-Standard-Retrieval-Bytes",
                    "GB",
                    0.022,
                    operation="DeepArchiveRestoreObject",
                ),
            ],
            [
                "APN1-TimedStorage-GDA-ByteHrs",
                "APN1-Standard-Retrieval-Bytes",
            ],
        ),
        (
            ServiceRequirement(
                service="storage_gateway",
                requirements={
                    "gateway_type": "file_gateway",
                    "cache_storage_gib": 10 * 1024,
                    "data_processed_gib": 40 * 1024,
                },
            ),
            [
                priced_product(
                    "AWSStorageGateway", "APN1-Uploaded-Bytes", "GB", 0.01
                ),
                priced_product(
                    "AWSStorageGateway",
                    "APN1-Gateway:VTL-Storage",
                    "GB-month",
                    0.025,
                ),
            ],
            ["APN1-Uploaded-Bytes"],
        ),
        (
            ServiceRequirement(
                service="data_sync",
                requirements={
                    "task_mode": "basic",
                    "data_processed_gib": 80 * 1024,
                },
            ),
            [
                priced_product(
                    "AWSDataSync", "APN1-Transferred-Bytes", "GB", 0.0125
                ),
                priced_product(
                    "AWSDataSync",
                    "APN1-Transferred-Bytes-Enhanced",
                    "GB",
                    0.015,
                ),
            ],
            ["APN1-Transferred-Bytes"],
        ),
        (
            ServiceRequirement(
                service="transfer",
                requirements={
                    "protocol": "sftp",
                    "storage_backend": "s3",
                    "transfer_direction": "upload",
                    "endpoint_count": 3,
                    "data_processed_gib": 30 * 1024,
                },
            ),
            [
                priced_product(
                    "AWSTransfer",
                    "APN1-ProtocolHours",
                    "Hourly",
                    0.3,
                    operation="SFTP:S3",
                ),
                priced_product(
                    "AWSTransfer",
                    "APN1-UploadBytes",
                    "GigaBytes",
                    0.04,
                    operation="SFTP:S3",
                ),
                priced_product(
                    "AWSTransfer",
                    "APN1-SFTPConnector-SendBytes",
                    "GB",
                    0.4,
                    operation="SFTP:S3",
                ),
            ],
            ["APN1-ProtocolHours", "APN1-UploadBytes"],
        ),
    ]

    for requirement, products, expected_usage_types in scenarios:
        rates = []
        for product in products:
            price, unit = PricingCatalog.on_demand_unit_rate(product)
            _, usage_type, operation = PricingCatalog.billing_identity(product)
            rates.append((price, unit, usage_type, operation, product))

        selected = GenericOfficialPlugin._semantic_rates(requirement, rates)

        assert [item[2][2] for item in selected] == expected_usage_types
        assert all(item[2][4].get("_astra_source_fields") for item in selected)


def test_fsx_openzfs_prices_storage_throughput_and_backup_not_monitoring() -> None:
    monitoring = priced_product(
        "AmazonFSx",
        "EUC1-Storage.MAZ:INT_Monitoring",
        "GB-Mo",
        0.0006,
        operation="CreateFileSystem:OpenZFS",
    )
    storage = priced_product(
        "AmazonFSx",
        "EUC1-Storage.SAZ2:SSD",
        "GB-Mo",
        0.107,
        operation="CreateFileSystem:OpenZFS",
    )
    throughput = priced_product(
        "AmazonFSx",
        "EUC1-ThroughputCapacity.SAZ2",
        "MiBps-Mo",
        0.35,
        operation="CreateFileSystem:OpenZFS",
    )
    backup = priced_product(
        "AmazonFSx",
        "EUC1-BackupUsage",
        "GB-Mo",
        0.054,
        operation="CreateFileSystem:OpenZFS",
    )
    for item, attributes in (
        (
            monitoring,
            {
                "fileSystemType": "OpenZFS",
                "storageType": "INT",
                "storageTier": "Monitoring",
                "deploymentOption": "Multi-AZ",
            },
        ),
        (
            storage,
            {
                "fileSystemType": "OpenZFS",
                "storageType": "SSD",
                "storageTier": "N/A",
                "deploymentOption": "Single-AZ_2",
            },
        ),
        (
            throughput,
            {
                "fileSystemType": "OpenZFS",
                "storageType": "N/A",
                "storageTier": "N/A",
                "deploymentOption": "Single-AZ_2",
            },
        ),
        (
            backup,
            {
                "fileSystemType": "OpenZFS",
                "storageType": "N/A",
                "storageTier": "Backup",
                "deploymentOption": "N/A",
            },
        ),
    ):
        item["product"]["attributes"].update(attributes)

    rates = []
    for item in (monitoring, storage, throughput, backup):
        price, unit = PricingCatalog.on_demand_unit_rate(item)
        _, usage_type, operation = PricingCatalog.billing_identity(item)
        rates.append((price, unit, usage_type, operation, item))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="fsx",
            requirements={
                "file_system_type": "openzfs",
                "storage_gib": 12288,
                "throughput_mbps": 512,
                "backup_storage_gib": 2048,
            },
        ),
        rates,
    )

    assert [item[1] for item in selected] == [12288, 512, 2048]
    assert all("Monitoring" not in item[2][2] for item in selected)
    assert sum(float(amount) * rate[0] for _, amount, rate in selected) == pytest.approx(
        1604.608
    )


def test_fsx_ontap_consumes_storage_deployment_and_throughput_contract() -> None:
    products = [
        priced_product(
            "AmazonFSx",
            "Storage.SSD.SingleAZ",
            "GB-Mo",
            0.10,
            operation="CreateFileSystem:ONTAP",
        ),
        priced_product(
            "AmazonFSx",
            "Storage.HDD.MultiAZ",
            "GB-Mo",
            0.05,
            operation="CreateFileSystem:ONTAP",
        ),
        priced_product(
            "AmazonFSx",
            "Storage.SSD.MultiAZ",
            "GB-Mo",
            0.20,
            operation="CreateFileSystem:ONTAP",
        ),
        priced_product(
            "AmazonFSx",
            "Throughput.SingleAZ",
            "MiBps-Mo",
            0.10,
            operation="CreateFileSystem:ONTAP",
        ),
        priced_product(
            "AmazonFSx",
            "Throughput.MultiAZ",
            "MiBps-Mo",
            0.30,
            operation="CreateFileSystem:ONTAP",
        ),
    ]
    for item, (deployment, storage_type) in zip(
        products,
        (
            ("Single-AZ", "SSD"),
            ("Multi-AZ", "HDD"),
            ("Multi-AZ", "SSD"),
            ("Single-AZ", "N/A"),
            ("Multi-AZ", "N/A"),
        ),
        strict=True,
    ):
        item["product"]["attributes"].update(
            {
                "fileSystemType": "ONTAP",
                "storageType": storage_type,
                "deploymentOption": deployment,
            }
        )

    rates = []
    for item in products:
        price, unit = PricingCatalog.on_demand_unit_rate(item)
        _, usage_type, operation = PricingCatalog.billing_identity(item)
        rates.append((price, unit, usage_type, operation, item))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="fsx",
            requirements={
                "file_system_type": "ontap",
                "deployment_type": "multi_az",
                "storage_type": "ssd",
                "storage_gib": 8192,
                "throughput_mbps": 512,
            },
        ),
        rates,
    )

    assert [amount for _, amount, _ in selected] == [8192, 512]
    assert [rate[0] for _, _, rate in selected] == [0.20, 0.30]


def test_system_chosen_profile_variant_cannot_override_fsx_product_configuration() -> None:
    wrong_storage = priced_product(
        "AmazonFSx",
        "USE1-Storage.MAZ:INT_Monitoring",
        "GB-Mo",
        0.0006,
        operation="CreateFileSystem:OpenZFS",
    )
    correct_storage = priced_product(
        "AmazonFSx",
        "USE1-Storage.MAZ:SSD",
        "GB-Mo",
        0.20,
        operation="CreateFileSystem:ONTAP",
    )
    for product, file_system_type, storage_type in (
        (wrong_storage, "OpenZFS", "INT"),
        (correct_storage, "ONTAP", "SSD"),
    ):
        product["product"]["attributes"].update(
            {
                "fileSystemType": file_system_type,
                "storageType": storage_type,
                "deploymentOption": "Multi-AZ",
            }
        )

    class Catalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonFSx"]

        @staticmethod
        def products(service_code: str, filters: dict[str, str], *, max_pages: int = 20):
            return [wrong_storage, correct_storage]

    class Discovery:
        @staticmethod
        def ensure_profile(**kwargs: object) -> dict[str, object]:
            return {
                "service_code": "AmazonFSx",
                "field_bindings": [
                    {
                        "field": "storage_gib",
                        "usage_type": "USE1-Storage.MAZ:INT_Monitoring",
                        "operation": "CreateFileSystem:OpenZFS",
                        "unit": "GB-Mo",
                    }
                ],
                "dimensions": [
                    {
                        "usage_type": "USE1-Storage.MAZ:INT_Monitoring",
                        "operation": "CreateFileSystem:OpenZFS",
                        "unit": "GB-Mo",
                        "price": 0.0006,
                    }
                ],
            }

    requirement = ServiceRequirement(
        service="fsx",
        calculator_service_name="Amazon FSx for NetApp ONTAP",
        region="us-east-1",
        requirements={
            "file_system_type": "ontap",
            "deployment_type": "multi_az",
            "storage_type": "ssd",
            "storage_gib": 8192,
            "_billing_variant_storage_gib": "USE1-Storage.MAZ:INT_Monitoring",
        },
        field_sources={
            "requirements._billing_variant_storage_gib": "system_lowest_compatible"
        },
    )

    selected = GenericOfficialPlugin(  # type: ignore[arg-type]
        None,
        Catalog(),
        Discovery(),
    ).select(requirement, "us-east-1")

    assert [line.usage_type for line in selected.usage_lines] == [
        "USE1-Storage.MAZ:SSD"
    ]


@pytest.mark.parametrize(
    "service",
    (
        "efs",
        "fsx",
        "s3_glacier_deep_archive",
        "storage_gateway",
        "data_sync",
        "transfer",
    ),
)
def test_closed_semantic_services_never_persist_generic_lowest_price_variant(
    service: str,
) -> None:
    requirement = ServiceRequirement(
        service=service,
        requirements={"storage_gib": 1024},
    )
    profile = {
        "field_bindings": [
            {
                "field": "storage_gib",
                "usage_type": "APN1-EarlyDelete-Or-Monitoring",
                "operation": "",
                "unit": "GB-Mo",
                "description": "low-priced add-on that is not primary storage",
            },
            {
                "field": "storage_gib",
                "usage_type": "APN1-PrimaryStorage",
                "operation": "",
                "unit": "GB-Mo",
                "description": "primary storage",
            },
        ],
        "dimensions": [
            {
                "usage_type": "APN1-EarlyDelete-Or-Monitoring",
                "operation": "",
                "unit": "GB-Mo",
                "price": 0.001,
            },
            {
                "usage_type": "APN1-PrimaryStorage",
                "operation": "",
                "unit": "GB-Mo",
                "price": 0.10,
            },
        ],
    }

    GenericOfficialPlugin._require_billing_variant_choice(requirement, profile)

    assert "_billing_variant_storage_gib" not in requirement.requirements


def test_codedeploy_to_ec2_is_a_valid_zero_cost_official_result() -> None:
    class CatalogMustNotBeCalled:
        @staticmethod
        def service_codes() -> list[str]:
            raise AssertionError("CodeDeploy EC2 pricing must not query catalog")

    plugin = GenericOfficialPlugin(None, CatalogMustNotBeCalled())  # type: ignore[arg-type]
    selected = plugin.select(
        ServiceRequirement(
            service="codedeploy",
            calculator_service_name="AWS CodeDeploy",
            region="ap-southeast-1",
            source_text="使用 CodeDeploy 持续部署到 EC2",
            requirements={"deployment_target": "ec2"},
        ),
        "ap-southeast-1",
    )

    assert selected.model == "EC2 部署（无额外服务费）"
    assert selected.usage_lines == []
    assert selected.reference_rates == []
    assert "不收取额外服务费" in selected.rationale


def test_generic_plugin_resolves_official_code_by_unique_stem() -> None:
    plugin = GenericOfficialPlugin(None, FakeCatalog())  # type: ignore[arg-type]

    assert plugin._service_code(ServiceRequirement(service="dynamodb")) == "AmazonDynamoDB"


def test_step_functions_uses_the_real_official_service_code() -> None:
    class StepFunctionsCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonStates"]

    plugin = GenericOfficialPlugin(None, StepFunctionsCatalog())  # type: ignore[arg-type]

    assert (
        plugin._service_code(ServiceRequirement(service="step_functions"))
        == "AmazonStates"
    )


def test_appconfig_uses_the_systems_manager_official_offer() -> None:
    class AppConfigCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AWSSystemsManager"]

    plugin = GenericOfficialPlugin(None, AppConfigCatalog())  # type: ignore[arg-type]

    assert (
        plugin._service_code(ServiceRequirement(service="appconfig"))
        == "AWSSystemsManager"
    )


@pytest.mark.parametrize(
    ("service", "official_code"),
    [
        ("ebs", "AmazonEC2"),
        ("nat_gateway", "AmazonEC2"),
        ("opensearch", "AmazonES"),
        ("sqs", "AWSQueueService"),
        ("scheduler", "AWSEvents"),
        ("eventbridge", "AWSEvents"),
    ],
)
def test_shared_offer_services_resolve_to_their_official_parent_offer(
    service: str,
    official_code: str,
) -> None:
    class SharedOfferCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return [official_code]

    plugin = GenericOfficialPlugin(None, SharedOfferCatalog())  # type: ignore[arg-type]

    assert plugin._service_code(ServiceRequirement(service=service)) == official_code


def test_stale_alias_is_never_returned_when_absent_from_official_registry() -> None:
    class CatalogWithoutConfiguredAlias:
        @staticmethod
        def service_codes() -> list[str]:
            return ["SomeOtherOffer"]

    plugin = GenericOfficialPlugin(
        None, CatalogWithoutConfiguredAlias()  # type: ignore[arg-type]
    )

    with pytest.raises(ManualConfirmationRequired) as exc_info:
        plugin._service_code(ServiceRequirement(service="step_functions"))

    assert exc_info.value.code == "generic_service_code_not_found"


def test_semantic_selection_does_not_choose_unrelated_cheapest_dimensions() -> None:
    rates = []
    for product in [
        priced_product(
            "AmazonDynamoDB", "APN1-ChangeDataCaptureUnits", "Units", 0.000001
        ),
        priced_product(
            "AmazonDynamoDB", "APN1-TimedStorage-ByteHrs", "GB-Mo", 0.285
        ),
        priced_product(
            "AmazonDynamoDB", "APN1-IA-TimedStorage-ByteHrs", "GB-Mo", 0.1
        ),
    ]:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        service_code, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(service="dynamodb", requirements={"storage_gib": 500}),
        rates,
    )

    assert len(selected) == 1
    assert selected[0][1] == 500
    assert selected[0][2][2] == "APN1-TimedStorage-ByteHrs"


def test_step_functions_standard_transitions_bind_only_to_standard_dimension() -> None:
    products = [
        priced_product(
            "AmazonStates",
            "APE1-StepFunctions-Request",
            "Requests",
            0.000001,
            group="SFN-ExpressWorkflows-Requests",
        ),
        priced_product(
            "AmazonStates",
            "APE1-StateTransition",
            "StateTransitions",
            0.0000275,
            group="SFN-StateTransitions",
        ),
        priced_product(
            "AmazonStates",
            "APE1-StepFunctions-GB-Second",
            "GB-Seconds",
            0.00001667,
            group="SFN-ExpressWorkflows-Duration",
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="step_functions",
            requirements={
                "workflow_type": "Standard",
                "state_transitions": 12_000_000,
            },
        ),
        rates,
    )

    assert len(selected) == 1
    assert selected[0][1] == 12_000_000
    assert selected[0][2][2] == "APE1-StateTransition"


def test_step_functions_standard_falls_back_to_usage_identity_without_group() -> None:
    transition = priced_product(
        "AmazonStates",
        "CAN1-StateTransition",
        "StateTransitions",
        0.000025,
    )
    price, unit = PricingCatalog.on_demand_unit_rate(transition)
    _, usage_type, operation = PricingCatalog.billing_identity(transition)

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="step_functions",
            requirements={
                "workflow_type": "standard",
                "state_transitions": 75_000,
            },
        ),
        [(price, unit, usage_type, operation, transition)],
    )

    assert len(selected) == 1
    assert selected[0][1] == 75_000
    assert selected[0][2][2] == "CAN1-StateTransition"
    assert selected[0][2][4]["_astra_source_fields"] == [
        "state_transitions",
        "workflow_type",
    ]


def test_step_functions_express_binds_requests_and_duration_separately() -> None:
    products = [
        priced_product(
            "AmazonStates",
            "APS1-StepFunctions-Request",
            "Requests",
            0.000001,
            group="SFN-ExpressWorkflows-Requests",
        ),
        priced_product(
            "AmazonStates",
            "APS1-StepFunctions-GB-Second",
            "GB-Seconds",
            0.00001667,
            group="SFN-ExpressWorkflows-Duration",
        ),
        priced_product(
            "AmazonStates",
            "APS1-StateTransition",
            "StateTransitions",
            0.000025,
            group="SFN-StateTransitions",
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="step_functions",
            requirements={
                "workflow_type": "Express",
                "requests": 3_000_000,
                "duration_gb_seconds": 400_000,
            },
        ),
        rates,
    )

    assert [(item[1], item[2][2]) for item in selected] == [
        (3_000_000, "APS1-StepFunctions-Request"),
        (400_000, "APS1-StepFunctions-GB-Second"),
    ]


@pytest.mark.parametrize("user_field", ["monthly_active_users", "user_count"])
def test_cognito_mau_accepts_either_supported_customer_field(
    user_field: str,
) -> None:
    mau = priced_product(
        "AmazonCognito",
        "CAN1-CognitoUserPoolsMAU",
        "Users",
        0.0055,
    )
    price, unit = PricingCatalog.on_demand_unit_rate(mau)
    _, usage_type, operation = PricingCatalog.billing_identity(mau)

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="cognito",
            requirements={user_field: 25_000},
        ),
        [(price, unit, usage_type, operation, mau)],
    )

    assert len(selected) == 1
    assert selected[0][1] == 25_000
    assert selected[0][2][2] == "CAN1-CognitoUserPoolsMAU"
    assert selected[0][2][4]["_astra_source_fields"] == [user_field]


def test_appconfig_never_selects_unrelated_systems_manager_dimensions() -> None:
    products = [
        priced_product(
            "AWSSystemsManager", "APS1-AppConfig-Requests", "Configuration Requests", 0.0000002
        ),
        priced_product(
            "AWSSystemsManager", "APS1-AppConfig-Deployments", "Configuration Received", 0.0008
        ),
        priced_product(
            "AWSSystemsManager", "APS1-AppConfig-ExperimentHours", "Hours", 0.9
        ),
        priced_product(
            "AWSSystemsManager", "APS1-OpsCenter-OpsItems", "OpsItem", 0.000001
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="appconfig",
            requirements={
                "configuration_requests": 2_000_000,
                "configuration_retrievals": 600,
                "targets_receiving_configuration": 200,
                "experiment_hours": 10,
            },
        ),
        rates,
    )

    assert [(item[1], item[2][2]) for item in selected] == [
        (2_000_000, "APS1-AppConfig-Requests"),
        (600, "APS1-AppConfig-Deployments"),
        (10, "APS1-AppConfig-ExperimentHours"),
    ]


def test_eventbridge_fields_bind_to_distinct_official_operations() -> None:
    products = [
        priced_product(
            "AWSEvents", "APE1-Event-64K-Chunks", "64K-Chunks", 0.000001,
            operation="PutEvents",
        ),
        priced_product(
            "AWSEvents", "APE1-Event-8K-Chunks", "8K-Chunks", 0.0000001,
            operation="DiscoveryEvent",
        ),
        priced_product(
            "AWSEvents", "APE1-Request-64K-Chunks", "64K-Chunks", 0.00000055,
            operation="PipeRequest",
        ),
        priced_product(
            "AWSEvents", "Global-Event-8K-Chunks", "8K-Chunks", 0,
            operation="DiscoveryEvent",
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="eventbridge",
            requirements={
                "events": 1_000_000,
                "schema_discovery_events": 200_000,
                "pipes_requests": 300_000,
            },
        ),
        rates,
    )

    assert [(item[1], item[2][3]) for item in selected] == [
        (1_000_000, "PutEvents"),
        (200_000, "DiscoveryEvent"),
        (300_000, "PipeRequest"),
    ]


def test_eventbridge_inventory_alias_consumes_custom_event_fact() -> None:
    product = priced_product(
        "AWSEvents",
        "APS4-Event-64K-Chunks",
        "64K-Chunks",
        0.000001,
        operation="PutEvents",
    )
    price, unit = PricingCatalog.on_demand_unit_rate(product)
    _, usage_type, operation = PricingCatalog.billing_identity(product)

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(service="events", requirements={"events": 400_000_000}),
        [(price, unit, usage_type, operation, product)],
    )

    assert len(selected) == 1
    assert selected[0][1] == 400_000_000
    assert selected[0][2][4]["_astra_source_fields"] == ["events"]


def test_config_semantics_bind_recorded_items_and_rule_evaluations() -> None:
    products = [
        priced_product(
            "AWSConfig",
            "APS4-ConfigurationItemRecorded",
            "ConfigurationItemRecorded",
            0.003,
        ),
        priced_product(
            "AWSConfig",
            "APS4-ConfigRuleEvaluations",
            "ConfigRuleEvaluations",
            0.0008,
        ),
        priced_product(
            "AWSConfig",
            "APS4-ProactiveConfigRuleEvaluations",
            "ProactiveConfigRuleEvaluations",
            0.001,
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="config",
            requirements={
                "configuration_items_recorded": 30_000_000,
                "rule_evaluations": 2_000_000,
            },
        ),
        rates,
    )

    assert [(item[1], item[2][2], item[2][4]["_astra_source_fields"]) for item in selected] == [
        (
            30_000_000,
            "APS4-ConfigurationItemRecorded",
            ["configuration_items_recorded"],
        ),
        (2_000_000, "APS4-ConfigRuleEvaluations", ["rule_evaluations"]),
    ]


def test_athena_scanned_gib_is_converted_to_official_terabytes() -> None:
    products = [
        priced_product("AmazonAthena", "APN1-DPU-Hour", "DPU-Hour", 0.01),
        priced_product("AmazonAthena", "APN1-DataScannedInTB", "Terabytes", 5),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(service="athena", requirements={"data_scanned_gib": 5120}),
        rates,
    )

    assert selected[0][1] == 5
    assert selected[0][2][2] == "APN1-DataScannedInTB"
    assert selected[0][2][4]["_astra_source_fields"] == ["data_scanned_gib"]


@pytest.mark.parametrize(
    ("architecture", "expected_vcpu_usage", "expected_memory_usage"),
    [
        (None, "EUN1-Fargate-vCPU-Hours:perCPU", "EUN1-Fargate-GB-Hours"),
        (
            "arm64",
            "EUN1-Fargate-ARM-vCPU-Hours:perCPU",
            "EUN1-Fargate-ARM-GB-Hours",
        ),
    ],
)
def test_fargate_launch_type_is_case_insensitive_and_architecture_specific(
    architecture: str | None,
    expected_vcpu_usage: str,
    expected_memory_usage: str,
) -> None:
    products = [
        priced_product(
            "AmazonECS", "EUN1-Fargate-vCPU-Hours:perCPU", "hours", 0.04
        ),
        priced_product("AmazonECS", "EUN1-Fargate-GB-Hours", "hours", 0.004),
        priced_product(
            "AmazonECS", "EUN1-Fargate-ARM-vCPU-Hours:perCPU", "hours", 0.032
        ),
        priced_product(
            "AmazonECS", "EUN1-Fargate-ARM-GB-Hours", "hours", 0.0035
        ),
        priced_product(
            "AmazonECS",
            "EUN1-Fargate-EphemeralStorage-GB-Hours",
            "GB-Hours",
            0.0001,
        ),
        priced_product(
            "AmazonECS", "EUN1-Fargate-Windows-vCPU-Hours:perCPU", "hours", 0.09
        ),
        priced_product(
            "AmazonECS",
            "EUN1-ECS-Managed-Instances:t4g.small-management-hours",
            "hours",
            0.001,
            operation="ECSManagedInstancesUsage",
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))
    requirements: dict[str, object] = {
        "launch_type": "Fargate",
        "tasks": 24,
        "task_vcpu": 2,
        "task_memory_gib": 4,
        "task_hours": 730,
    }
    if architecture is not None:
        requirements["architecture"] = architecture

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(service="ecs", requirements=requirements),
        rates,
    )

    assert [item[2][2] for item in selected] == [
        expected_vcpu_usage,
        expected_memory_usage,
    ]
    assert [item[1] for item in selected] == [24 * 730 * 2, 24 * 730 * 4]
    assert set(selected[0][2][4]["_astra_source_fields"]) >= {
        "launch_type",
        "task_hours",
        "task_vcpu",
        "tasks",
    }
    assert set(selected[1][2][4]["_astra_source_fields"]) >= {
        "launch_type",
        "task_hours",
        "task_memory_gib",
        "tasks",
    }
    automatic = GenericOfficialPlugin._auto_semantic_rates(
        ServiceRequirement(service="ecs", requirements=requirements),
        rates,
    )
    assert all(item[1] is None for item in automatic)


def test_fargate_uses_component_monthly_runtime_when_task_hours_are_absent() -> None:
    products = [
        priced_product(
            "AmazonECS", "USE2-Fargate-ARM-vCPU-Hours:perCPU", "hours", 0.032
        ),
        priced_product(
            "AmazonECS", "USE2-Fargate-ARM-GB-Hours", "hours", 0.0035
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="ecs",
            hours_per_month=720,
            requirements={
                "launch_type": "Fargate",
                "tasks": 40,
                "task_vcpu": 2,
                "task_memory_gib": 4,
                "architecture": "arm64",
            },
        ),
        rates,
    )

    assert [item[1] for item in selected] == [40 * 720 * 2, 40 * 720 * 4]
    assert all(
        "hours_per_month" in item[2][4]["_astra_source_fields"]
        for item in selected
    )
    assert all(
        "task_hours" not in item[2][4]["_astra_source_fields"]
        for item in selected
    )


def test_cloud_map_binds_registry_resources_and_discovery_calls_exactly() -> None:
    products = [
        priced_product(
            "AWSCloudMap", "USE2-Cloud-Map-Resources", "CloudMapResource", 0.10
        ),
        priced_product(
            "AWSCloudMap", "USE2-Cloud-Map-API-Calls", "CloudMapAPICall", 0.000001
        ),
        priced_product(
            "AWSCloudMap",
            "USE2-Cloud-Map-DIR-API-Calls",
            "CloudMapAPICall",
            0.0000005,
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="cloud_map",
            requirements={"service_instances": 200, "api_calls": 200_000_000},
        ),
        rates,
    )

    assert [(item[1], item[2][2]) for item in selected] == [
        (200, "USE2-Cloud-Map-Resources"),
        (200_000_000, "USE2-Cloud-Map-API-Calls"),
    ]
    assert selected[0][2][4]["_astra_source_fields"] == ["service_instances"]
    assert selected[1][2][4]["_astra_source_fields"] == ["api_calls"]


def test_sns_fifo_binds_publish_and_subscriber_messages_not_standard_requests() -> None:
    products = [
        priced_product("AmazonSNS", "USW1-F-Request-Tier1", "Requests", 0.00000039),
        priced_product("AmazonSNS", "USW1-F-DA-SQS", "Messages", 0.000000013),
        priced_product("AmazonSNS", "USW1-Requests-Tier1", "Requests", 0.0000005),
        priced_product(
            "AmazonSNS", "USW1-DeliveryAttempts-HTTP", "Notifications", 0.0000006
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="sns",
            requirements={
                "topic_type": "fifo",
                "requests": 250_000_000,
                "deliveries": 400_000_000,
            },
        ),
        rates,
    )

    assert [(item[1], item[2][2]) for item in selected] == [
        (250_000_000, "USW1-F-Request-Tier1"),
        (400_000_000, "USW1-F-DA-SQS"),
    ]
    assert selected[0][2][4]["_astra_source_fields"] == [
        "requests",
        "topic_type",
    ]
    assert selected[1][2][4]["_astra_source_fields"] == [
        "deliveries",
        "topic_type",
    ]


def test_scheduler_uses_invocations_and_treats_schedule_count_as_context() -> None:
    product = priced_product(
        "AWSEvents",
        "USW1-ScheduledInvocation",
        "Invocations",
        0.000001,
        operation="Invocation",
    )
    price, unit = PricingCatalog.on_demand_unit_rate(product)
    _, usage_type, operation = PricingCatalog.billing_identity(product)

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="scheduler",
            requirements={"scheduled_invocations": 50_000_000, "schedules": 250},
        ),
        [(price, unit, usage_type, operation, product)],
    )

    assert [(item[1], item[2][2], item[2][3]) for item in selected] == [
        (50_000_000, "USW1-ScheduledInvocation", "Invocation")
    ]
    assert selected[0][2][4]["_astra_source_fields"] == [
        "scheduled_invocations"
    ]
    assert "schedules" in _CONFIGURATION_CONTEXT_FIELDS["scheduler"]


def test_appconfig_target_count_is_non_billing_context_for_total_retrievals() -> None:
    assert "targets_receiving_configuration" in _CONFIGURATION_CONTEXT_FIELDS[
        "appconfig"
    ]


def test_codebuild_selects_exact_architecture_and_compute_type() -> None:
    products = [
        priced_product("CodeBuild", "USE1-Build-Min:Linux:g1.medium", "minutes", 0.01),
        priced_product("CodeBuild", "USE1-Build-Min:ARM:g1.medium", "minutes", 0.007),
        priced_product(
            "CodeBuild", "USE1-Build-Min:Linux:Reserved:t4g.medium", "minutes", 0.001
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    x86 = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="code_build",
            requirements={
                "build_minutes": 900_000,
                "architecture": "x86_64",
                "operating_system": "linux",
                "compute_type": "g1.medium",
            },
        ),
        rates,
    )
    arm = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="code_build",
            requirements={
                "build_minutes": 600_000,
                "architecture": "arm64",
                "operating_system": "linux",
                "compute_type": "g1.medium",
            },
        ),
        rates,
    )

    assert [(item[1], item[2][2]) for item in x86] == [
        (900_000, "USE1-Build-Min:Linux:g1.medium")
    ]
    assert [(item[1], item[2][2]) for item in arm] == [
        (600_000, "USE1-Build-Min:ARM:g1.medium")
    ]


def test_devsecops_services_bind_each_fact_to_one_official_meter() -> None:
    products = [
        priced_product(
            "AWSCodePipeline", "USE1-actionExecutionMinute", "minutes", 0.002
        ),
        priced_product("AWSCodePipeline", "USE1-activePipeline", "pipelines", 1.0),
        priced_product(
            "AWSCloudFormation",
            "USE1-Resource-Invocation-Count",
            "Operations",
            0.0009,
            operation="ProcessResourceHandlers",
        ),
        priced_product(
            "AmazonInspectorV2", "USE1-EC2-Scanning", "Instance-hrs", 0.00174
        ),
        priced_product(
            "AmazonInspectorV2",
            "USE1-container-image-initial-scan",
            "Resource-assessment",
            0.09,
        ),
        priced_product(
            "AmazonInspectorV2", "USE1-Lambda-Standard-Scanning", "Hourly", 0.000417
        ),
        priced_product(
            "AWSSecurityHub", "USE1-PaidComplianceCheck", "Security Checks", 0.0005
        ),
        priced_product(
            "auditmanager",
            "USE1-Resource-Assessment-Collected",
            "resource assessment",
            0.00125,
        ),
        priced_product(
            "AmazonMacie", "USE1-PaidDataInventoryEvaluation", "Bucket-days", 0.003288
        ),
        priced_product(
            "AmazonMacie", "USE1-SensitiveDataDiscovery", "GB", 1.0
        ),
        priced_product("AWSCodeArtifact", "USE1-Requests", "Requests", 0.000005),
        priced_product(
            "AWSCodeArtifact", "USE1-TimedStorage-ByteHrs", "GB-Mo", 0.05
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    cases = [
        (
            ServiceRequirement(
                service="code_pipeline",
                requirements={"pipeline_type": "v2", "action_execution_minutes": 1_200_000},
            ),
            [(1_200_000, "USE1-actionExecutionMinute")],
        ),
        (
            ServiceRequirement(
                service="cloud_formation",
                requirements={"resource_handler_operations": 5_000_000},
            ),
            [(5_000_000, "USE1-Resource-Invocation-Count")],
        ),
        (
            ServiceRequirement(
                service="inspector_v2",
                requirements={
                    "ec2_instances": 2_000,
                    "ecr_images": 5_000,
                    "lambda_functions": 300,
                },
            ),
            [
                (2_000 * 730, "USE1-EC2-Scanning"),
                (5_000, "USE1-container-image-initial-scan"),
                (300 * 730, "USE1-Lambda-Standard-Scanning"),
            ],
        ),
        (
            ServiceRequirement(
                service="security_hub",
                requirements={"resource_count": 4_000, "security_checks": 30_000_000},
            ),
            [(30_000_000, "USE1-PaidComplianceCheck")],
        ),
        (
            ServiceRequirement(
                service="auditmanager",
                requirements={
                    "resource_assessments": 15_000,
                    "evidence_items": 8_000_000,
                },
            ),
            [(15_000, "USE1-Resource-Assessment-Collected")],
        ),
        (
            ServiceRequirement(
                service="macie",
                requirements={"bucket_count": 400, "data_scanned_gib": 60 * 1024},
            ),
            [
                (400 * 30, "USE1-PaidDataInventoryEvaluation"),
                (60 * 1024, "USE1-SensitiveDataDiscovery"),
            ],
        ),
        (
            ServiceRequirement(
                service="code_artifact",
                requirements={"requests": 200_000_000, "storage_gib": 10 * 1024},
            ),
            [
                (200_000_000, "USE1-Requests"),
                (10 * 1024, "USE1-TimedStorage-ByteHrs"),
            ],
        ),
    ]

    for requirement, expected in cases:
        selected = GenericOfficialPlugin._semantic_rates(requirement, rates)
        assert [(item[1], item[2][2]) for item in selected] == expected

    assert "resource_count" in _CONFIGURATION_CONTEXT_FIELDS["securityhub"]
    assert "evidence_items" in _CONFIGURATION_CONTEXT_FIELDS["auditmanager"]


def test_iot_and_media_services_bind_only_exact_official_meters() -> None:
    products = [
        priced_product("AWSIoT", "APN1-ConnectionMinutes", "Minutes", 0.000000096),
        priced_product("AWSIoT", "APN1-Messages", "Messages", 0.0000012),
        priced_product(
            "IoTDeviceManagement", "APN1-ThingRegistration", "Things Registered", 0.00012
        ),
        priced_product(
            "IoTDeviceManagement", "APN1-JobExecutions", "Remote Actions", 0.0018
        ),
        priced_product("IoTDeviceDefender", "APN1-Audit", "Devices", 0.00135),
        priced_product(
            "IoTDeviceDefender", "APN1-Detect", "Metric Datapoints", 0.00000034
        ),
        priced_product(
            "AmazonKinesisVideo", "APN1-BytesIn", "GB", 0.010965, operation="PutMedia"
        ),
        priced_product(
            "AmazonKinesisVideo", "APN1-BytesOut", "GB", 0.010965, operation="GetMedia"
        ),
        priced_product(
            "AmazonKinesisVideo", "APN1-BytesOutWarm", "GB", 0.010965, operation="GetMedia"
        ),
        priced_product(
            "AmazonKinesisVideo", "APN1-BytesHr", "GB-Month", 0.025, operation="PutMedia"
        ),
        priced_product(
            "AWSElementalMediaConvert",
            "NRT-Normalized-Transcode-Minute-Basic",
            "minutes",
            0.0021,
        ),
        priced_product(
            "AWSElementalMediaPackage", "APN1-EMP-ingest-bytes", "GB", 0.044
        ),
        priced_product(
            "AWSElementalMediaPackage", "APN1-EMP-origin-packaging-bytes", "GB", 0.06
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    cases = [
        (
            ServiceRequirement(
                service="io_t",
                requirements={
                    "device_count": 500_000,
                    "connection_minutes": 360_000_000,
                    "messages": 2_000_000_000,
                    "message_size_kib": 5,
                },
            ),
            [
                (360_000_000, "APN1-ConnectionMinutes"),
                (2_000_000_000, "APN1-Messages"),
            ],
        ),
        (
            ServiceRequirement(
                service="io_t_device_management",
                requirements={"things_registered": 500_000, "remote_actions": 10_000_000},
            ),
            [(500_000, "APN1-ThingRegistration"), (10_000_000, "APN1-JobExecutions")],
        ),
        (
            ServiceRequirement(
                service="io_t_device_defender",
                requirements={"device_count": 500_000, "metric_datapoints": 1_500_000_000},
            ),
            [(500_000, "APN1-Audit"), (1_500_000_000, "APN1-Detect")],
        ),
        (
            ServiceRequirement(
                service="kinesis_video",
                requirements={
                    "data_in_gib": 100 * 1024,
                    "data_out_gib": 300 * 1024,
                    "storage_gib": 50 * 1024,
                },
            ),
            [
                (100 * 1024, "APN1-BytesIn"),
                (300 * 1024, "APN1-BytesOut"),
                (50 * 1024, "APN1-BytesHr"),
            ],
        ),
        (
            ServiceRequirement(
                service="elemental_media_convert",
                requirements={
                    "transcode_minutes": 8_000_000,
                    "resolution": "hd",
                    "transcoding_tier": "basic",
                },
            ),
            [(8_000_000, "NRT-Normalized-Transcode-Minute-Basic")],
        ),
        (
            ServiceRequirement(
                service="elemental_media_package",
                requirements={"data_in_gib": 100 * 1024, "data_out_gib": 300 * 1024},
            ),
            [(100 * 1024, "APN1-EMP-ingest-bytes"), (300 * 1024, "APN1-EMP-origin-packaging-bytes")],
        ),
    ]

    for requirement, expected in cases:
        selected = GenericOfficialPlugin._semantic_rates(requirement, rates)
        assert [(item[1], item[2][2]) for item in selected] == expected


@pytest.mark.parametrize(
    ("service", "requirements", "error_code"),
    [
        (
            "ivs",
            {"input_channel_hours": 20_000, "viewer_hours": 4_000_000},
            "ivs_stream_profile_required",
        ),
        (
            "elemental_media_live",
            {"channel_count": 30, "channel_class": "standard", "channel_hours": 730},
            "medialive_io_profile_required",
        ),
        (
            "media_connect",
            {"output_count": 20, "output_hours": 730, "data_transfer_out_gib": 80 * 1024},
            "mediaconnect_output_profile_required",
        ),
    ],
)
def test_media_services_request_missing_billable_profile_instead_of_guessing(
    service: str,
    requirements: dict[str, object],
    error_code: str,
) -> None:
    with pytest.raises(ManualConfirmationRequired) as error:
        GenericOfficialPlugin._semantic_rates(
            ServiceRequirement(service=service, requirements=requirements),
            [],
        )

    assert error.value.code == error_code
    assert error.value.details["nearby_candidates"]


def test_ecr_same_region_transfer_is_free_context_and_standard_storage_is_billed() -> None:
    archive = priced_product(
        "AmazonECR", "EUN1-TimedStorage-Archive-ByteHrs", "GB-Mo", 0.01
    )
    standard = priced_product(
        "AmazonECR", "EUN1-TimedStorage-ByteHrs", "GB-Mo", 0.10
    )

    class EcrCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonECR"]

        @staticmethod
        def products(
            service_code: str,
            _filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            assert service_code == "AmazonECR"
            return [archive, standard]

    plugin = GenericOfficialPlugin(None, EcrCatalog())  # type: ignore[arg-type]
    requirement = ServiceRequirement(
        service="ecr",
        region="eu-north-1",
        requirements={
            "storage_gib": 2048,
            "data_transfer_out_gib": 20 * 1024,
            "transfer_scope": "same_region",
        },
        field_sources={
            "requirements.storage_gib": "customer_text",
            "requirements.data_transfer_out_gib": "customer_text",
            "requirements.transfer_scope": "customer_text",
        },
        field_evidence={
            "requirements.storage_gib": "镜像存储2TB",
            "requirements.data_transfer_out_gib": "传输20TB",
            "requirements.transfer_scope": "同区域",
        },
    )

    selected = plugin.select(requirement, "eu-north-1")

    assert [(line.usage_type, line.amount) for line in selected.usage_lines] == [
        ("EUN1-TimedStorage-ByteHrs", 2048)
    ]
    assert set(selected.applied_requirement_fields) >= {
        "data_transfer_out_gib",
        "storage_gib",
        "transfer_scope",
    }
    assert unconsumed_customer_pricing_facts(requirement, selected) == []


def test_lambda_explicit_requests_memory_and_duration_create_two_usage_dimensions() -> None:
    products = [
        priced_product(
            "AWSLambda", "APN1-Request", "Request", 0.0000002,
            group="AWS-Lambda-Requests",
        ),
        priced_product(
            "AWSLambda", "APN1-Lambda-GB-Second", "Lambda-GB-Second", 0.000015,
            group="AWS-Lambda-Duration",
        ),
        priced_product(
            "AWSLambda", "APN1-Lambda-Provisioned-Concurrency", "Lambda-GB-Second",
            0.000005, group="AWS-Lambda-Provisioned-Concurrency",
        ),
        priced_product(
            "AWSLambda", "APN1-Lambda-Managed-Instances-Request", "Requests", 0.0000001
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="lambda",
            requirements={"requests": 5_000_000, "memory_mb": 512, "duration_ms": 3000},
        ),
        rates,
    )

    assert [item[1] for item in selected] == [5_000_000, 7_500_000]
    assert [item[2][2] for item in selected] == [
        "APN1-Request",
        "APN1-Lambda-GB-Second",
    ]


def test_lambda_aggregate_invocations_are_not_multiplied_by_function_count() -> None:
    products = [
        priced_product(
            "AWSLambda", "APN1-Request", "Request", 0.00000028,
            group="AWS-Lambda-Requests",
        ),
        priced_product(
            "AWSLambda", "APN1-Lambda-GB-Second", "Lambda-GB-Second", 0.00002292,
            group="AWS-Lambda-Duration",
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))
    requirement = ServiceRequirement(
        service="lambda",
        quantity=5,
        requirements={"requests": 20_000_000, "memory_mb": 1024, "duration_ms": 800},
        field_scopes={"requests": "aggregate"},
    )

    selected = GenericOfficialPlugin._semantic_rates(requirement, rates)

    assert [item[1] for item in selected] == [20_000_000, 16_000_000]
    assert sum(item[1] * item[2][0] for item in selected if item[1]) == pytest.approx(372.32)


def test_lambda_selection_traces_every_derived_input_without_multiplying_functions() -> None:
    products = [
        priced_product(
            "AWSLambda", "APN1-Request", "Request", 0.00000028,
            group="AWS-Lambda-Requests",
        ),
        priced_product(
            "AWSLambda", "APN1-Lambda-GB-Second", "Lambda-GB-Second", 0.00002292,
            group="AWS-Lambda-Duration",
        ),
    ]

    class LambdaCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AWSLambda"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            assert service_code == "AWSLambda"
            return products

    requirement = ServiceRequirement(
        service="lambda",
        calculator_service_name="AWS Lambda",
        region="ap-northeast-1",
        quantity=10,
        requirements={
            "requests": 30_000_000,
            "memory_mb": 2048,
            "duration_ms": 1000,
            "architecture": "x86_64",
        },
        field_sources={
            "quantity": "customer_text",
            "requirements.requests": "customer_text",
            "requirements.memory_mb": "customer_text",
            "requirements.duration_ms": "customer_text",
            "requirements.architecture": "customer_text",
        },
        field_evidence={
            "quantity": "10个函数",
            "requirements.requests": "每月总调用量3000万次",
            "requirements.memory_mb": "单函数内存2G",
            "requirements.duration_ms": "平均执行时长1秒",
            "requirements.architecture": "x86_64",
        },
        field_scopes={"requirements.requests": "aggregate"},
    )

    selected = GenericOfficialPlugin(None, LambdaCatalog()).select(  # type: ignore[arg-type]
        requirement,
        "ap-northeast-1",
    )

    assert [line.amount for line in selected.usage_lines] == [
        30_000_000,
        60_000_000,
    ]
    assert selected.specifications["function_count"] == 10
    assert "quantity" in selected.applied_requirement_fields
    assert {"requests", "memory_mb", "duration_ms"}.issubset(
        set(selected.usage_lines[1].source_fields)
    )
    assert unconsumed_customer_pricing_facts(requirement, selected) == []


def test_eks_control_plane_traces_cluster_count_to_cluster_hours() -> None:
    products = [
        priced_product(
            "AmazonEKS",
            "APN1-AmazonEKS-Hours:perCluster",
            "Hrs",
            0.10,
        )
    ]

    class EksCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonEKS"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            assert service_code == "AmazonEKS"
            return products

    requirement = ServiceRequirement(
        service="eks",
        calculator_service_name="Amazon EKS",
        region="ap-northeast-1",
        quantity=1,
        hours_per_month=730,
        requirements={"cluster_count": 2},
        field_sources={"requirements.cluster_count": "customer_text"},
        field_evidence={"requirements.cluster_count": "2套集群"},
    )

    selected = GenericOfficialPlugin(None, EksCatalog()).select(  # type: ignore[arg-type]
        requirement,
        "ap-northeast-1",
    )

    assert selected.usage_lines[0].amount == 1460
    assert {"cluster_count", "hours_per_month"}.issubset(
        set(selected.usage_lines[0].source_fields)
    )
    assert unconsumed_customer_pricing_facts(requirement, selected) == []


def test_ecs_ec2_control_plane_is_free_and_consumes_cluster_count() -> None:
    class CatalogMustNotBeCalled:
        @staticmethod
        def service_codes() -> list[str]:
            raise AssertionError("ECS EC2 control plane must not query an instance rate")

        @staticmethod
        def products(*args, **kwargs):
            raise AssertionError("ECS EC2 control plane must not query an instance rate")

    requirement = ServiceRequirement(
        service="ecs",
        calculator_service_name="Amazon ECS",
        region="ap-south-1",
        quantity=1,
        requirements={"cluster_count": 1, "launch_type": "ec2"},
        field_sources={"requirements.cluster_count": "customer_text"},
        field_evidence={"requirements.cluster_count": "1套集群"},
    )

    selected = GenericOfficialPlugin(  # type: ignore[arg-type]
        None, CatalogMustNotBeCalled()
    ).select(requirement, "ap-south-1")

    assert selected.pricing_status == "free"
    assert selected.model == "ECS 集群控制面（EC2 启动类型）"
    assert selected.usage_lines == []
    assert "cluster_count" in selected.applied_requirement_fields
    assert unconsumed_customer_pricing_facts(requirement, selected) == []


def test_kinesis_explicit_shards_are_priced_as_monthly_shard_hours() -> None:
    products = [
        priced_product(
            "AmazonKinesis",
            "SAE1-Storage-ShardHour",
            "ShardHour",
            0.03,
            operation="shardHourStorage",
        ),
        priced_product(
            "AmazonKinesis",
            "SAE1-OnDemand-StreamHour",
            "StreamHour",
            0.08,
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="kinesis",
            quantity=1,
            hours_per_month=730,
            requirements={"shards": 2},
        ),
        rates,
    )

    assert len(selected) == 1
    assert selected[0][1] == 1460
    assert selected[0][2][0] == 0.03
    assert selected[0][2][2] == "SAE1-Storage-ShardHour"
    assert selected[0][1] * selected[0][2][0] == 43.8


def test_kinesis_monthly_write_volume_adds_put_payload_units() -> None:
    products = [
        priced_product(
            "AmazonKinesis",
            "SAE1-Storage-ShardHour",
            "ShardHour",
            0.03,
            operation="shardHourStorage",
        ),
        priced_product(
            "AmazonKinesis",
            "SAE1-PutRequestPayloadUnits",
            "PutRequest",
            0.000000014,
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="kinesis",
            quantity=1,
            hours_per_month=730,
            requirements={
                "capacity_mode": "provisioned",
                "shards": 12,
                "data_in_gib": 5120,
            },
        ),
        rates,
    )

    assert len(selected) == 2
    assert selected[0][1] == 12 * 730
    assert selected[1][1] == math.ceil(5120 * 1024**3 / 25_000)
    assert selected[1][2][2] == "SAE1-PutRequestPayloadUnits"
    assert selected[0][2][4]["_astra_source_fields"] == [
        "shards",
        "capacity_mode",
    ]
    assert selected[1][2][4]["_astra_source_fields"] == [
        "data_in_gib",
        "capacity_mode",
    ]


def test_kinesis_provisioned_read_volume_is_consumed_as_included_capacity_context() -> None:
    products = [
        priced_product(
            "AmazonKinesis",
            "EUC1-Storage-ShardHour",
            "ShardHour",
            0.03,
            operation="shardHourStorage",
        ),
        priced_product(
            "AmazonKinesis",
            "EUC1-PutRequestPayloadUnits",
            "PutRequest",
            0.000000014,
        ),
    ]

    class KinesisCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonKinesis"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            del max_pages, refresh
            assert service_code == "AmazonKinesis"
            return [
                item
                for item in products
                if all(
                    item["product"]["attributes"].get(key) == value
                    for key, value in filters.items()
                )
            ]

    requirement = ServiceRequirement(
        service="kinesis",
        region="ap-northeast-1",
        requirements={
            "capacity_mode": "provisioned",
            "shards": 8,
            "data_in_gib": 2048,
            "data_out_gib": 4096,
        },
        field_sources={
            "requirements.shards": "customer_text",
            "requirements.data_in_gib": "customer_text",
            "requirements.data_out_gib": "customer_text",
        },
        field_evidence={
            "requirements.shards": "8个Shard",
            "requirements.data_in_gib": "每月写入2TB",
            "requirements.data_out_gib": "每月读取4TB",
        },
    )

    selected = GenericOfficialPlugin(  # type: ignore[arg-type]
        None,
        KinesisCatalog(),
    ).select(requirement, "ap-northeast-1")

    assert {line.usage_type for line in selected.usage_lines} == {
        "EUC1-Storage-ShardHour",
        "EUC1-PutRequestPayloadUnits",
    }
    assert "data_out_gib" in selected.applied_requirement_fields
    assert unconsumed_customer_pricing_facts(requirement, selected) == []


def test_documentdb_selects_instance_and_preserves_explicit_storage() -> None:
    instance = priced_product("AmazonDocDB", "APS1-InstanceUsage:db.t4g.medium", "Hrs", 0.1)
    instance["product"]["attributes"].update(
        {
            "productFamily": "Database Instance",
            "instanceType": "db.t4g.medium",
            "vcpu": "2",
            "memory": "4 GiB",
        }
    )
    storage = priced_product("AmazonDocDB", "APS1-StorageUsage", "GB-Mo", 0.1)
    storage["product"]["attributes"]["productFamily"] = "Database Storage"
    rates = []
    for product in (instance, storage):
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="documentdb",
            quantity=1,
            hours_per_month=730,
            requirements={"storage_gib": 2048},
        ),
        rates,
    )

    assert [item[1] for item in selected] == [730, 2048]


def test_amazon_mq_uses_broker_topology_and_minimum_requested_shape() -> None:
    undersized = priced_product(
        "AmazonMQ", "APS1-RabbitMQ-3-InstanceUsage:mq.t3.micro", "Hrs", 0.06,
        operation="CreateBroker:RabbitMQ",
    )
    undersized["product"]["attributes"].update(
        {"instanceType": "mq.t3.micro", "vcpu": "2", "memory": "1 GiB"}
    )
    fitting = priced_product(
        "AmazonMQ", "APS1-RabbitMQ-3-InstanceUsage:mq.m5.xlarge", "Hrs", 1.2,
        operation="CreateBroker:RabbitMQ",
    )
    fitting["product"]["attributes"].update(
        {"instanceType": "mq.m5.xlarge", "vcpu": "4", "memory": "16 GiB"}
    )
    rates = []
    for product in (undersized, fitting):
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="mq",
            quantity=2,
            hours_per_month=730,
            requirements={
                "engine_type": "rabbitmq",
                "broker_count": 3,
                "vcpu": 4,
                "memory_gib": 16,
            },
        ),
        rates,
    )

    assert len(selected) == 1
    assert selected[0][1] == 2 * 730
    assert selected[0][2][4]["product"]["attributes"]["instanceType"] == "mq.m5.xlarge"


def test_amazon_mq_semantic_rates_declare_every_customer_fact_source() -> None:
    compute = priced_product(
        "AmazonMQ",
        "APS1-RabbitMQ-3-InstanceUsage:mq.m5.xlarge",
        "Hrs",
        1.2,
        operation="CreateBroker:RabbitMQ",
    )
    compute["product"]["attributes"].update(
        {"instanceType": "mq.m5.xlarge", "vcpu": "4", "memory": "16 GiB"}
    )
    storage = priced_product(
        "AmazonMQ",
        "APS1-RabbitMQ-Storage",
        "GB-Mo",
        0.1,
        operation="CreateBroker:RabbitMQ",
    )

    class MqCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonMQ"]

        @staticmethod
        def products(service_code: str, filters: dict[str, str], *, max_pages: int = 20):
            assert service_code == "AmazonMQ"
            return [compute, storage]

    source = "RabbitMQ 消息队列，3个节点，单节点4核16G，磁盘500G"
    requirement = ServiceRequirement(
        service="mq",
        calculator_service_name="Amazon MQ for RabbitMQ",
        region="ap-northeast-1",
        quantity=1,
        hours_per_month=730,
        requirements={
            "engine_type": "rabbitmq",
            "broker_count": 3,
            "vcpu": 4,
            "memory_gib": 16,
            "storage_gib_per_broker": 500,
        },
        source_text=source,
        field_sources={
            "requirements.broker_count": "customer_text",
            "requirements.vcpu": "customer_text",
            "requirements.memory_gib": "customer_text",
            "requirements.storage_gib_per_broker": "customer_text",
        },
        field_evidence={
            "requirements.broker_count": "3个节点",
            "requirements.vcpu": "4核16G",
            "requirements.memory_gib": "4核16G",
            "requirements.storage_gib_per_broker": "磁盘500G",
        },
    )

    selected = GenericOfficialPlugin(None, MqCatalog()).select(  # type: ignore[arg-type]
        requirement,
        "ap-northeast-1",
    )

    assert len(selected.usage_lines) == 2
    assert unconsumed_customer_pricing_facts(requirement, selected) == []


def test_generic_profile_cannot_add_mutually_exclusive_mq_compute_rate() -> None:
    bundled = priced_product(
        "AmazonMQ",
        "APS1-RabbitMQ-3-InstanceUsage:mq.m7g.xlarge",
        "Hrs",
        2.05,
        operation="CreateBroker:RabbitMQ",
    )
    single = priced_product(
        "AmazonMQ",
        "APS1-RabbitMQ-Single-InstanceUsage:mq.m7g.xlarge",
        "Hrs",
        0.68,
        operation="CreateBroker:RabbitMQ",
    )
    storage = priced_product(
        "AmazonMQ",
        "APS1-RabbitMQ-Storage",
        "GB-Mo",
        0.12,
        operation="CreateBroker:RabbitMQ",
    )
    incompatible_profile_compute = priced_product(
        "AmazonMQ",
        "APS1-Multi-AZUsage:mq.m5.2xlarge",
        "Hrs",
        0.4,
        operation="ActiveMQ-CRDR",
    )
    for product in (bundled, single):
        product["product"]["attributes"].update(
            {"instanceType": "mq.m7g.xlarge", "vcpu": "4", "memory": "16 GiB"}
        )
    incompatible_profile_compute["product"]["attributes"].update(
        {"instanceType": "mq.m5.2xlarge", "vcpu": "8", "memory": "32 GiB"}
    )

    class MqCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonMQ"]

        @staticmethod
        def products(service_code: str, filters: dict[str, str], *, max_pages: int = 20):
            return [bundled, single, storage, incompatible_profile_compute]

    class Discovery:
        @staticmethod
        def ensure_profile(**kwargs: object) -> dict[str, object]:
            return {
                "service_code": "AmazonMQ",
                "field_bindings": [
                    {
                        "field": "hours_per_month",
                        "usage_type": "APS1-Multi-AZUsage:mq.m5.2xlarge",
                        "operation": "ActiveMQ-CRDR",
                        "unit": "Hrs",
                    }
                ],
            }

    selected = GenericOfficialPlugin(  # type: ignore[arg-type]
        None,
        MqCatalog(),
        Discovery(),
    ).select(
        ServiceRequirement(
            service="mq",
            calculator_service_name="Amazon MQ for RabbitMQ",
            region="ap-northeast-1",
            quantity=1,
            hours_per_month=730,
            requirements={
                "engine_type": "rabbitmq",
                "broker_count": 3,
                "vcpu": 4,
                "memory_gib": 16,
                "storage_gib_per_broker": 300,
            },
        ),
        "ap-northeast-1",
    )

    assert [line.usage_type for line in selected.usage_lines] == [
        "APS1-RabbitMQ-3-InstanceUsage:mq.m7g.xlarge",
        "APS1-RabbitMQ-Storage",
    ]


def test_emr_prices_master_and_core_roles_instead_of_one_generic_instance() -> None:
    instance = priced_product(
        "ElasticMapReduce", "APS1-InstanceUsage:m5.xlarge", "Hrs", 0.05,
        operation="RunJobFlow",
    )
    instance["product"]["attributes"].update(
        {"instanceType": "m5.xlarge", "vcpu": "4", "memory": "16 GiB"}
    )
    price, unit = PricingCatalog.on_demand_unit_rate(instance)
    _, usage_type, operation = PricingCatalog.billing_identity(instance)

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="emr",
            quantity=1,
            hours_per_month=730,
            requirements={
                "applications": ["spark"],
                "master_nodes": 1,
                "master_requested_model": "m5.xlarge",
                "core_nodes": 5,
                "core_requested_model": "m5.xlarge",
            },
        ),
        [(price, unit, usage_type, operation, instance)],
    )

    assert [item[1] for item in selected] == [730, 5 * 730]
    assert [item[0] for item in selected] == [
        "Amazon EMR 主节点实例小时价",
        "Amazon EMR 核心节点实例小时价",
    ]
    assert selected[0][2][4]["_astra_source_fields"] == [
        "hours_per_month",
        "master_nodes",
        "master_requested_model",
        "quantity",
    ]
    assert selected[1][2][4]["_astra_source_fields"] == [
        "core_nodes",
        "core_requested_model",
        "hours_per_month",
        "quantity",
    ]


def test_emr_accepts_current_official_box_usage_dimensions() -> None:
    instance = priced_product(
        "ElasticMapReduce", "APS1-BoxUsage:m6g.xlarge", "Hrs", 0.048,
    )
    instance["product"]["attributes"].update(
        {"instanceType": "m6g.xlarge"}
    )
    price, unit = PricingCatalog.on_demand_unit_rate(instance)
    _, usage_type, operation = PricingCatalog.billing_identity(instance)

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="emr",
            quantity=1,
            hours_per_month=730,
            requirements={"applications": ["spark"], "master_nodes": 1, "core_nodes": 5},
        ),
        [(price, unit, usage_type, operation, instance)],
    )

    assert [item[1] for item in selected] == [730, 5 * 730]


def test_managed_grafana_without_user_count_returns_official_reference_rate() -> None:
    class GrafanaCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonGrafana"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
        ) -> list[dict]:
            assert service_code == "AmazonGrafana"
            return [
                priced_product(
                    "AmazonGrafana",
                    "APS1-Grafana:ViewerUser",
                    "Users",
                    5,
                ),
                priced_product(
                    "AmazonGrafana",
                    "APS1-Grafana:EditorUser",
                    "Users",
                    9,
                ),
            ]

    plugin = GenericOfficialPlugin(None, GrafanaCatalog())  # type: ignore[arg-type]
    selected = plugin.select(
        ServiceRequirement(
            service="amazon_managed_grafana",
            calculator_service_name="Amazon Managed Grafana",
            region="ap-southeast-1",
            quantity=1,
        ),
        "ap-southeast-1",
    )

    assert selected.usage_lines == []
    assert selected.reference_rates[0].service_code == "AmazonGrafana"
    assert selected.reference_rates[0].unit_price == 5
    assert selected.reference_rates[0].usage_type.endswith("ViewerUser")


def test_redshift_capacity_uses_ra3_compute_and_managed_storage() -> None:
    dc2 = priced_product("AmazonRedshift", "APS1-Node:dc2.large", "Hrs", 0.1)
    dc2["product"]["attributes"].update(
        {"instanceType": "dc2.large", "productFamily": "Compute Node"}
    )
    ra3 = priced_product("AmazonRedshift", "APS1-Node:ra3.xlplus", "Hrs", 0.5)
    ra3["product"]["attributes"].update(
        {"instanceType": "ra3.xlplus", "productFamily": "Compute Node"}
    )
    storage = priced_product(
        "AmazonRedshift", "APS1-ManagedStorage", "GB-Mo", 0.024,
    )
    storage["product"]["attributes"]["productFamily"] = "Managed Storage"
    rates = []
    for product in (dc2, ra3, storage):
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))

    selected = GenericOfficialPlugin._semantic_rates(
        ServiceRequirement(
            service="redshift",
            quantity=1,
            hours_per_month=730,
            requirements={"deployment_type": "provisioned", "storage_gib": 20 * 1024},
        ),
        rates,
    )

    assert [item[1] for item in selected] == [730, 20 * 1024]
    assert selected[0][2][4]["product"]["attributes"]["instanceType"] == "ra3.xlplus"


def test_vpc_returns_zero_cost_base_network_without_catalog_lookup() -> None:
    plugin = GenericOfficialPlugin(None, FakeCatalog())  # type: ignore[arg-type]
    selected = plugin.select(
        ServiceRequirement(service="vpc", quantity=1, region="eu-central-1"),
        "eu-central-1",
    )

    assert selected.model == "VPC + Subnets"
    assert selected.usage_lines == []
    assert "不收取基础费用" in (selected.substitution_notice or "")


def test_memorydb_keeps_redis_engine_and_uses_its_own_reserved_term() -> None:
    redis = priced_product(
        "AmazonMemoryDB",
        "APE1-NodeUsage:db.r6g.xlarge",
        "Hrs",
        0.812,
        operation="CreateCluster",
    )
    redis["product"]["attributes"].update(
        {
            "instanceType": "db.r6g.xlarge",
            "engine": "Redis",
            "vcpu": "4",
            "memory": "26.32 GiB",
            "regionCode": "ap-east-1",
        }
    )
    redis["terms"]["Reserved"] = {
        "one-year-all-upfront": {
            "termAttributes": {
                "LeaseContractLength": "1yr",
                "PurchaseOption": "All Upfront",
            },
            "priceDimensions": {
                "upfront": {
                    "unit": "Quantity",
                    "pricePerUnit": {"USD": "4552.397"},
                }
            },
        }
    }
    valkey = priced_product(
        "AmazonMemoryDB",
        "APE1-NodeUsage:db.r6g.xlarge:Valkey",
        "Hrs",
        0.5684,
        operation="CreateCluster",
    )
    valkey["product"]["attributes"].update(
        {
            "instanceType": "db.r6g.xlarge",
            "engine": "Valkey",
            "vcpu": "4",
            "memory": "26.32 GiB",
            "regionCode": "ap-east-1",
        }
    )

    class MemoryDbCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonMemoryDB"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
        ) -> list[dict]:
            assert service_code == "AmazonMemoryDB"
            return [redis, valkey]

    plugin = GenericOfficialPlugin(None, MemoryDbCatalog())  # type: ignore[arg-type]
    base = {
        "service": "memorydb",
        "calculator_service_name": "Amazon MemoryDB",
        "region": "ap-east-1",
        "quantity": 1,
        "hours_per_month": 730,
    }

    on_demand = plugin.select(
        ServiceRequirement(
            **base,
            requirements={
                "requested_model": "db.r6g.xlarge",
                "engine": "Redis",
                "purchase_option": "on_demand",
            },
        ),
        "ap-east-1",
    )
    reserved = plugin.select(
        ServiceRequirement(
            **base,
            requirements={
                "requested_model": "db.r6g.xlarge",
                "engine": "Redis",
                "purchase_option": "reserved",
                "reserved_term_years": 1,
                "payment_option": "all_upfront",
            },
        ),
        "ap-east-1",
    )

    assert on_demand.usage_lines[0].usage_type == "APE1-NodeUsage:db.r6g.xlarge"
    assert on_demand.usage_lines[0].amount == 730
    assert reserved.usage_lines == []
    assert reserved.monthly_commitment_cost == 4552.397 / 12
    assert reserved.upfront_commitment_cost == 4552.397


def test_dynamodb_reserved_capacity_uses_official_blocks_and_heavy_utilization() -> None:
    read = priced_product(
        "AmazonDynamoDB",
        "APN1-ReadCapacityUnit-Hrs",
        "ReadCapacityUnit-Hrs",
        0.0002,
        operation="CommittedThroughput",
        group="DDB-ReadUnits",
    )
    write = priced_product(
        "AmazonDynamoDB",
        "APN1-WriteCapacityUnit-Hrs",
        "WriteCapacityUnit-Hrs",
        0.0004,
        operation="CommittedThroughput",
        group="DDB-WriteUnits",
    )
    for item, hourly, upfront in (
        (read, 0.0001, 1.0),
        (write, 0.0002, 2.0),
    ):
        item["terms"]["Reserved"] = {
            "heavy": {
                "termAttributes": {
                    "LeaseContractLength": "1yr",
                    "PurchaseOption": "Heavy Utilization",
                    "OfferingClass": "standard",
                },
                "priceDimensions": {
                    "hourly": {
                        "unit": item["terms"]["OnDemand"]["term"]
                        ["priceDimensions"]["dimension"]["unit"],
                        "pricePerUnit": {"USD": str(hourly)},
                    },
                    "upfront": {
                        "unit": "Quantity",
                        "pricePerUnit": {"USD": str(upfront)},
                    },
                },
            }
        }

    class DynamoCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonDynamoDB"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
        ) -> list[dict]:
            del max_pages
            assert service_code == "AmazonDynamoDB"
            return [
                item
                for item in (read, write)
                if all(
                    item["product"]["attributes"].get(key) == value
                    for key, value in filters.items()
                )
            ]

    selected = GenericOfficialPlugin(None, DynamoCatalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="dynamodb",
            calculator_service_name="Amazon DynamoDB",
            region="ap-northeast-1",
            hours_per_month=730,
            requirements={
                "capacity_mode": "provisioned",
                "read_request_units": 150,
                "write_request_units": 20,
                "purchase_option": "reserved",
                "reserved_term_years": 1,
                # DynamoDB has its own Heavy Utilization offer; a global
                # instance payment choice must not change that official term.
                "payment_option": "all_upfront",
            },
        ),
        "ap-northeast-1",
    )

    assert selected.usage_lines == []
    assert selected.upfront_commitment_cost == pytest.approx(400)
    assert selected.monthly_commitment_cost == pytest.approx(
        (0.0001 * 730 + 1 / 12) * 200
        + (0.0002 * 730 + 2 / 12) * 100
    )
    assert selected.specifications["reservedReadCapacityUnits"] == 200
    assert selected.specifications["reservedWriteCapacityUnits"] == 100
    assert "Heavy Utilization" in (selected.substitution_notice or "")


def test_redshift_reserved_price_multiplies_every_customer_node() -> None:
    compute = priced_product(
        "AmazonRedshift",
        "APN1-Node:ra3.xlplus",
        "Hrs",
        1.0,
        operation="RunComputeNode:0001",
    )
    compute["product"]["attributes"].update(
        {
            "productFamily": "Compute Instance",
            "instanceType": "ra3.xlplus",
            "vcpu": "4",
            "memory": "32 GiB",
            "currentGeneration": "Yes",
        }
    )
    compute["terms"]["Reserved"] = {
        "one-year-no-upfront": {
            "termAttributes": {
                "LeaseContractLength": "1yr",
                "PurchaseOption": "No Upfront",
                "OfferingClass": "standard",
            },
            "priceDimensions": {
                "hourly": {
                    "unit": "Hrs",
                    "pricePerUnit": {"USD": "0.6"},
                }
            },
        }
    }

    class RedshiftCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonRedshift"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
        ) -> list[dict]:
            del max_pages
            assert service_code == "AmazonRedshift"
            return [
                compute
                if all(
                    compute["product"]["attributes"].get(key) == value
                    for key, value in filters.items()
                )
                else None
            ] if all(
                compute["product"]["attributes"].get(key) == value
                for key, value in filters.items()
            ) else []

    selected = GenericOfficialPlugin(None, RedshiftCatalog()).select(  # type: ignore[arg-type]
        ServiceRequirement(
            service="redshift",
            calculator_service_name="Amazon Redshift",
            region="ap-northeast-1",
            quantity=1,
            hours_per_month=730,
            requirements={
                "deployment_type": "provisioned",
                "requested_model": "ra3.xlplus",
                "nodes": 4,
                "purchase_option": "reserved",
                "reserved_term_years": 1,
                "payment_option": "no_upfront",
            },
        ),
        "ap-northeast-1",
    )

    assert selected.monthly_commitment_cost == pytest.approx(0.6 * 730 * 4)


def test_memorydb_unavailable_model_uses_cheapest_same_capacity_official_node() -> None:
    replacement = priced_product(
        "AmazonMemoryDB",
        "APE1-NodeUsage:db.r6g.xlarge",
        "Hrs",
        0.812,
        operation="CreateCluster",
    )
    replacement["product"]["attributes"].update(
        {
            "instanceType": "db.r6g.xlarge",
            "engine": "Redis",
            "vcpu": "4",
            "memory": "26.32 GiB",
            "regionCode": "ap-east-1",
        }
    )
    snapshot = priced_product(
        "AmazonMemoryDB",
        "APE1-SnapshotUsage",
        "GB-Mo",
        0.023,
    )

    class ReplacementCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonMemoryDB"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            return [replacement, snapshot]

    selected = GenericOfficialPlugin(
        None, ReplacementCatalog()  # type: ignore[arg-type]
    ).select(
        ServiceRequirement(
            service="memorydb",
            calculator_service_name="Amazon MemoryDB",
            region="ap-east-1",
            hours_per_month=730,
            requirements={
                "requested_model": "db.r7g.xlarge",
                "engine": "Redis",
                "vcpu": 4,
                "memory_gib": 26.32,
            },
        ),
        "ap-east-1",
    )

    assert selected.model == "db.r6g.xlarge"
    assert selected.usage_lines[0].usage_type == "APE1-NodeUsage:db.r6g.xlarge"
    assert selected.usage_lines[0].amount == 730
    assert "同配置" in (selected.substitution_notice or "")
    assert "db.r7g.xlarge" in (selected.substitution_notice or "")


def test_regional_service_without_catalog_is_not_mislabeled_as_timeout() -> None:
    class EmptyPinpointCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonPinpoint"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            return []

    plugin = GenericOfficialPlugin(
        None, EmptyPinpointCatalog()  # type: ignore[arg-type]
    )

    with pytest.raises(ManualConfirmationRequired) as captured:
        plugin.select(
            ServiceRequirement(
                service="pinpoint",
                calculator_service_name="Amazon Pinpoint",
                region="ap-east-1",
                requirements={"outbound_messages": 1_000_000},
            ),
            "ap-east-1",
        )

    assert captured.value.code == "service_region_not_supported"
    assert captured.value.details["region"] == "ap-east-1"


def test_supported_regions_come_from_local_official_endpoint_metadata() -> None:
    class EndpointSession:
        @staticmethod
        def get_available_services() -> list[str]:
            return ["appstream"]

        @staticmethod
        def get_available_regions(service_id: str) -> list[str]:
            assert service_id == "appstream"
            return ["ap-southeast-1", "ap-southeast-2", "us-east-1"]

    class Clients:
        session = EndpointSession()

    plugin = GenericOfficialPlugin(
        Clients(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    assert plugin.supported_regions(
        ServiceRequirement(
            service="app_stream",
            calculator_service_name="Amazon AppStream 2.0",
        )
    ) == ["ap-southeast-1", "ap-southeast-2", "us-east-1"]


def test_unsupported_service_region_is_rejected_before_price_catalog_download() -> None:
    class EndpointSession:
        @staticmethod
        def get_available_services() -> list[str]:
            return ["memorydb"]

        @staticmethod
        def get_available_regions(service_id: str) -> list[str]:
            assert service_id == "memorydb"
            return ["ap-southeast-1", "ap-southeast-2", "us-east-1"]

    class Clients:
        session = EndpointSession()

    class CatalogMustNotRun:
        @staticmethod
        def service_codes() -> list[str]:
            raise AssertionError("不支持的区域不应再下载价格目录")

    plugin = GenericOfficialPlugin(
        Clients(),  # type: ignore[arg-type]
        CatalogMustNotRun(),  # type: ignore[arg-type]
    )

    with pytest.raises(ManualConfirmationRequired) as captured:
        plugin.select(
            ServiceRequirement(
                service="memorydb",
                calculator_service_name="Amazon MemoryDB",
                region="ap-southeast-3",
                requirements={"engine": "redis", "memory_gib": 13},
            ),
            "ap-southeast-3",
        )

    assert captured.value.code == "service_region_not_supported"
    assert captured.value.details["region"] == "ap-southeast-3"
    assert {item["model"] for item in captured.value.details["nearby_candidates"]} == {
        "ap-southeast-1",
        "ap-southeast-2",
        "us-east-1",
    }


def test_retired_service_becomes_customer_replacement_choice() -> None:
    plugin = GenericOfficialPlugin(None, object())  # type: ignore[arg-type]

    with pytest.raises(ManualConfirmationRequired) as captured:
        plugin.select(
            ServiceRequirement(
                service="qldb",
                calculator_service_name="Amazon QLDB",
                region="us-east-1",
            ),
            "us-east-1",
        )

    assert captured.value.code == "service_retired"
    candidates = captured.value.details["nearby_candidates"]
    assert isinstance(candidates, list)
    assert {candidate["specifications"]["decision"] for candidate in candidates} == {
        "replace_service:rds:aurora_postgresql",
        "exclude_component",
    }


def test_generic_official_service_never_uses_unrelated_fee_for_requested_shape() -> None:
    service_fee = priced_product(
        "AmazonWorkSpaces",
        "APE1-WH-ManagedInstances-Usage",
        "Hrs",
        0.02,
        group="Usage",
    )
    service_fee["product"]["attributes"].update(
        {
            "regionCode": "ap-east-1",
            "resourceType": "Service fee",
        }
    )

    class WorkSpacesServiceFeeOnlyCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonWorkSpaces"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            assert service_code == "AmazonWorkSpaces"
            return [service_fee]

    plugin = GenericOfficialPlugin(
        None, WorkSpacesServiceFeeOnlyCatalog()  # type: ignore[arg-type]
    )
    requirement = ServiceRequirement(
        service="work_spaces",
        calculator_service_name="Amazon WorkSpaces",
        region="ap-east-1",
        quantity=50,
        requirements={
            "vcpu": 2,
            "memory_gib": 8,
            "system_disk_gib": 80,
            "user_volume_gib": 50,
        },
    )

    with pytest.raises(ManualConfirmationRequired) as captured:
        plugin.select(requirement, "ap-east-1")

    assert captured.value.code == "generic_official_shape_not_exposed"
    assert "不会用无关计费项猜价" in captured.value.message


def test_generic_official_shape_conflict_becomes_live_catalog_choice() -> None:
    products = []
    for model, vcpu, memory, price in (
        ("db.r6g.large", 2, 16, 0.4),
        ("db.r5d.xlarge", 4, 32, 0.9),
        ("db.r6g.2xlarge", 8, 64, 1.6),
    ):
        product = priced_product(
            "AmazonNeptune",
            f"APE1-InstanceUsage:{model}",
            "Hrs",
            price,
            operation="CreateDBInstance:0022",
        )
        product["product"]["attributes"].update(
            {
                "regionCode": "ap-east-1",
                "instanceType": model,
                "vcpu": str(vcpu),
                "memory": f"{memory} GiB",
                "databaseEngine": "Amazon Neptune",
                "deploymentOption": "Multi-AZ",
            }
        )
        products.append(product)

    class NeptuneCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonNeptune"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            assert service_code == "AmazonNeptune"
            return products

    plugin = GenericOfficialPlugin(None, NeptuneCatalog())  # type: ignore[arg-type]
    requirement = ServiceRequirement(
        service="neptune",
        calculator_service_name="Amazon Neptune",
        region="ap-east-1",
        requirements={
            "requested_model": "db.r6g.large",
            "vcpu": 8,
            "memory_gib": 32,
            "instance_count": 3,
        },
    )

    with pytest.raises(ManualConfirmationRequired) as captured:
        plugin.select(requirement, "ap-east-1")

    assert captured.value.code == "generic_official_specification_not_found"
    choices = plugin.configuration_candidates(requirement, "ap-east-1")
    assert [choice.model for choice in choices] == [
        "db.r6g.large",
        "db.r5d.xlarge",
        "db.r6g.2xlarge",
    ]
    assert choices[1].specifications == {
        "instanceType": "db.r5d.xlarge",
        "vCPU": 4.0,
        "memoryGiB": 32.0,
    }


def test_quicksight_merges_global_subscription_with_regional_catalog() -> None:
    regional_reader = priced_product(
        "AmazonQuickSight",
        "APS1-Reader-Enterprise-Month",
        "User",
        3,
    )
    regional_reader["product"]["attributes"].update(
        {
            "edition": "Enterprise",
            "group": "Reader Subscription",
            "location": "Asia Pacific (Singapore)",
            "regionCode": "ap-southeast-1",
        }
    )
    global_user = priced_product(
        "AmazonQuickSight",
        "QS-User-Enterprise-Month",
        "User",
        24,
    )
    global_user["product"]["attributes"].update(
        {
            "edition": "Enterprise",
            "group": "User Subscription",
            "location": "Any",
            "regionCode": "",
        }
    )
    other_region_spice = priced_product(
        "AmazonQuickSight",
        "USE1-QS-Enterprise-SPICE",
        "GB-Mo",
        0.25,
    )
    other_region_spice["product"]["attributes"].update(
        {
            "edition": "Enterprise",
            "group": "SPICE Capacity",
            "location": "US East (N. Virginia)",
            "regionCode": "us-east-1",
        }
    )

    class QuickSightCatalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonQuickSight"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
        ) -> list[dict]:
            assert service_code == "AmazonQuickSight"
            if filters:
                return [regional_reader]
            return [regional_reader, global_user, other_region_spice]

    plugin = GenericOfficialPlugin(None, QuickSightCatalog())  # type: ignore[arg-type]
    selected = plugin.select(
        ServiceRequirement(
            service="quicksight",
            calculator_service_name="Amazon QuickSight",
            region="ap-southeast-1",
            requirements={"edition": "enterprise", "users": 10},
        ),
        "ap-southeast-1",
    )

    assert selected.usage_lines[0].service_code == "AmazonQuickSight"
    assert selected.usage_lines[0].usage_type == "QS-User-Enterprise-Month"
    assert selected.usage_lines[0].amount == 10


def test_generic_instance_preview_keeps_official_shape_separate_from_customer_request() -> None:
    product = priced_product(
        "AmazonDocDB",
        "APE1-InstanceUsage:db.r6g.xlarge",
        "Hrs",
        0.4,
        operation="CreateDBInstance",
    )
    product["product"]["attributes"].update(
        {
            "instanceType": "db.r6g.xlarge",
            "productFamily": "Database Instance",
            "vcpu": "4",
            "memory": "32 GiB",
            "regionCode": "ap-east-1",
        }
    )

    class Catalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonDocDB"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            return [product]

    requirement = ServiceRequirement(
        service="documentdb",
        region="ap-east-1",
        requirements={"vcpu": 4, "memory_gib": 16, "instance_count": 3},
    )
    plugin = GenericOfficialPlugin(None, Catalog())  # type: ignore[arg-type]

    preview = plugin.preview(requirement, "ap-east-1")
    selected = plugin.select(requirement, "ap-east-1")

    assert preview.candidates[0].specifications["vCPU"] == 4
    assert preview.candidates[0].specifications["memoryGiB"] == 32
    assert preview.candidates[0].specifications["memory_gib"] == 16
    assert selected.usage_lines[0].amount == 3 * 730


def test_generic_configuration_candidates_expose_multiple_official_shapes() -> None:
    products = []
    for model, vcpu, memory, price in (
        ("db.r6g.large", 2, 16, 0.2),
        ("db.r6g.xlarge", 4, 32, 0.4),
        ("db.r6g.2xlarge", 8, 64, 0.8),
    ):
        product = priced_product(
            "AmazonDocDB",
            f"APE1-InstanceUsage:{model}",
            "Hrs",
            price,
            operation="CreateDBInstance",
        )
        product["product"]["attributes"].update(
            {
                "instanceType": model,
                "vcpu": str(vcpu),
                "memory": f"{memory} GiB",
                "regionCode": "ap-east-1",
            }
        )
        products.append(product)

    class Catalog:
        @staticmethod
        def service_codes() -> list[str]:
            return ["AmazonDocDB"]

        @staticmethod
        def products(
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int = 20,
            refresh: bool = False,
        ) -> list[dict]:
            return products

    plugin = GenericOfficialPlugin(None, Catalog())  # type: ignore[arg-type]
    candidates = plugin.configuration_candidates(
        ServiceRequirement(service="documentdb", region="ap-east-1"),
        "ap-east-1",
    )

    assert [candidate.model for candidate in candidates] == [
        "db.r6g.large",
        "db.r6g.xlarge",
        "db.r6g.2xlarge",
    ]
    assert [candidate.specifications for candidate in candidates] == [
        {"instanceType": "db.r6g.large", "vCPU": 2.0, "memoryGiB": 16.0},
        {"instanceType": "db.r6g.xlarge", "vCPU": 4.0, "memoryGiB": 32.0},
        {"instanceType": "db.r6g.2xlarge", "vCPU": 8.0, "memoryGiB": 64.0},
    ]


def test_dynamic_profile_chooses_the_ordinary_base_variant_without_asking() -> None:
    normal_usage = "APS1-Traffic-GB-Processed"
    advanced_usage = "APS1-AdvancedThreatProtection-Traffic-GB-Processed"
    profile = {
        "field_bindings": [
            {
                "field": "data_processed_gib",
                "label": "每月处理流量",
                "usage_type": normal_usage,
                "operation": "",
                "unit": "GB",
                "description": "USD 0.065 per GB processed by AWS Network Firewall",
            },
            {
                "field": "data_processed_gib",
                "label": "每月处理流量",
                "usage_type": advanced_usage,
                "operation": "",
                "unit": "GB",
                "description": "USD 0.005 per GB advanced threat protection",
            },
        ],
        "dimensions": [
            {
                "usage_type": normal_usage,
                "operation": "",
                "unit": "GB",
                "price": 0.065,
            },
            {
                "usage_type": advanced_usage,
                "operation": "",
                "unit": "GB",
                "price": 0.005,
            },
        ],
    }
    requirement = ServiceRequirement(
        service="network_firewall",
        calculator_service_name="AWS Network Firewall",
        requirements={"data_processed_gib": 1024},
    )

    GenericOfficialPlugin._require_billing_variant_choice(requirement, profile)

    assert requirement.requirements["_billing_variant_data_processed_gib"] == normal_usage
    assert (
        requirement.field_sources["requirements._billing_variant_data_processed_gib"]
        == "system_lowest_compatible"
    )


def test_session_capacity_automatically_uses_the_lowest_complete_plan() -> None:
    usage_types = (
        "QS-Reader-Capacity-200K-Usage",
        "QS-Reader-Capacity-400K-Usage",
        "QS-Reader-Capacity-400K-Extra",
        "QS-Reader-Usage-Paid-Session",
        "QS-Reader-Usage-Paid-Session-Q",
    )
    profile = {
        "field_bindings": [
            {
                "field": "session_capacity",
                "label": "读者会话次数",
                "usage_type": usage_type,
                "operation": "",
                "unit": "Sessions",
            }
            for usage_type in usage_types
        ],
        "dimensions": [
            {
                "usage_type": usage_type,
                "operation": "",
                "unit": "Sessions",
                "price": 0.2,
            }
            for usage_type in usage_types
        ],
    }
    requirement = ServiceRequirement(
        service="quick_sight",
        calculator_service_name="Amazon QuickSight",
        source_text="Amazon QuickSight：每月2万次读者会话",
        requirements={"session_capacity": 20_000},
    )

    GenericOfficialPlugin._require_billing_variant_choice(requirement, profile)

    assert requirement.requirements["_billing_variant_session_capacity"] == (
        "QS-Reader-Capacity-400K-Usage"
    )


def test_quicksight_reader_count_and_sessions_ask_for_one_billing_method() -> None:
    requirement = ServiceRequirement(
        service="quick_sight",
        calculator_service_name="Amazon QuickSight",
        source_text="Amazon QuickSight：120名读者，每月2万次读者会话",
        requirements={"reader_users": 120, "session_capacity": 20_000},
    )

    with pytest.raises(ManualConfirmationRequired) as exc_info:
        GenericOfficialPlugin._require_cross_field_billing_mode(requirement)

    error = exc_info.value
    assert error.code == "billing_variant_required"
    assert error.details["field"] == "reader_billing_mode"
    assert "不能两种一起算" in str(error)
    assert [
        item["specifications"]["decision"]
        for item in error.details["nearby_candidates"]
    ] == [
        "billing_variant:reader_billing_mode:per_user",
        "billing_variant:reader_billing_mode:capacity",
    ]


def test_quicksight_author_variant_uses_lowest_compatible_edition_without_q() -> None:
    usage_types = (
        "QS-User-Standard-Month",
        "QS-User-Enterprise-Month",
        "QS-User-Enterprise-Annual",
        "EUC1-Author-Pro-Enterprise-Month-Q",
    )
    profile = {
        "field_bindings": [
            {
                "field": "author_users",
                "label": "作者数量",
                "usage_type": usage_type,
                "operation": "",
                "unit": "User",
            }
            for usage_type in usage_types
        ],
        "dimensions": [
            {
                "usage_type": usage_type,
                "operation": "",
                "unit": "User",
                "price": 10,
            }
            for usage_type in usage_types
        ],
    }
    requirement = ServiceRequirement(
        service="quick_sight",
        calculator_service_name="Amazon QuickSight",
        source_text="Amazon QuickSight：企业版，10名作者",
        requirements={"edition": "enterprise", "author_users": 10},
    )

    GenericOfficialPlugin._require_billing_variant_choice(requirement, profile)

    assert requirement.requirements["_billing_variant_author_users"] == (
        "QS-User-Enterprise-Annual"
    )


def test_confirmed_billing_variant_is_reused_instead_of_selecting_the_cheapest_rate() -> None:
    normal_usage = "APS1-Traffic-GB-Processed"
    advanced_usage = "APS1-AdvancedThreatProtection-Traffic-GB-Processed"
    normal_product = priced_product("AWSNetworkFirewall", normal_usage, "GB", 0.065)
    advanced_product = priced_product(
        "AWSNetworkFirewall", advanced_usage, "GB", 0.005
    )
    rates = []
    for product in (normal_product, advanced_product):
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))
    profile = {
        "field_bindings": [
            {
                "field": "data_processed_gib",
                "label": "每月处理流量",
                "usage_type": normal_usage,
                "operation": "",
                "unit": "GB",
            },
            {
                "field": "data_processed_gib",
                "label": "每月处理流量",
                "usage_type": advanced_usage,
                "operation": "",
                "unit": "GB",
            },
        ]
    }
    requirement = ServiceRequirement(
        service="network_firewall",
        requirements={
            "data_processed_gib": 1024,
            "_billing_variant_data_processed_gib": normal_usage,
        },
    )

    selected = GenericOfficialPlugin._auto_semantic_rates(
        requirement,
        rates,
        profile=profile,
    )

    assert len(selected) == 1
    assert selected[0][2][2] == normal_usage
    assert selected[0][2][0] == 0.065


def test_opensearch_dedicated_master_count_is_included_in_instance_usage() -> None:
    product = priced_product(
        "AmazonES",
        "SAE1-ESInstance:r6g.xlarge.search",
        "Hrs",
        0.50,
    )
    product["product"]["attributes"].update(
        {
            "instanceType": "r6g.xlarge.search",
            "vcpu": "4",
            "memory": "32 GiB",
        }
    )
    price, unit = PricingCatalog.on_demand_unit_rate(product)
    _, usage_type, operation = PricingCatalog.billing_identity(product)
    requirement = ServiceRequirement(
        service="opensearch",
        hours_per_month=730,
        requirements={
            "requested_model": "r6g.xlarge.search",
            "data_nodes": 6,
            "vcpu": 4,
            "memory_gib": 32,
            "master_nodes": 3,
            "dedicated_master": True,
        },
    )

    selected = GenericOfficialPlugin._auto_semantic_rates(
        requirement,
        [(price, unit, usage_type, operation, product)],
        profile={"field_bindings": []},
    )

    assert len(selected) == 1
    assert selected[0][1] == 9 * 730
    assert set(selected[0][2][4]["_astra_source_fields"]) >= {
        "master_nodes",
        "requested_model",
    }


def test_flink_kpu_and_running_storage_are_derived_from_customer_kpu_count() -> None:
    products = [
        priced_product(
            "AmazonKinesisAnalytics",
            "EUW1-KPU-Hour-Java",
            "KPU-Hour",
            0.12,
            operation="StartApplication",
        ),
        priced_product(
            "AmazonKinesisAnalytics",
            "EUW1-KPU-Hour-Interactive",
            "KPU-Hour",
            0.05,
            operation="StartApplication",
        ),
        priced_product(
            "AmazonKinesisAnalytics",
            "EUW1-RunningApplicationStorage",
            "GB-month",
            0.11,
            operation="StartApplication",
        ),
        priced_product(
            "AmazonKinesisAnalytics",
            "EUW1-RunningApplicationStorage-Interactive",
            "GB-month",
            0.02,
            operation="StartApplication",
        ),
    ]
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))
    profile = {
        "field_bindings": [
            {
                "field": "kpu_hours",
                "label": "KPU 小时",
                "usage_type": product["product"]["attributes"]["usagetype"],
                "operation": "StartApplication",
                "unit": "KPU-Hour",
            }
            for product in products[:2]
        ]
        + [
            {
                "field": "storage_gib",
                "label": "运行应用存储",
                "usage_type": product["product"]["attributes"]["usagetype"],
                "operation": "StartApplication",
                "unit": "GB-month",
            }
            for product in products[2:]
        ]
    }
    requirement = ServiceRequirement(
        service="kinesis_analytics",
        calculator_service_name="Amazon Managed Service for Apache Flink",
        source_text="持续运行，配置4个KPU",
        quantity=1,
        hours_per_month=730,
        requirements={"kpu_count": 4, "data_processed_gib": 6144},
    )

    selected = GenericOfficialPlugin._auto_semantic_rates(
        requirement,
        rates,
        profile=profile,
    )

    assert [(item[1], item[2][2]) for item in selected] == [
        (3650.0, "EUW1-KPU-Hour-Java"),
        (200.0, "EUW1-RunningApplicationStorage"),
    ]

    node_shaped_requirement = ServiceRequirement(
        service="kinesis_analytics",
        calculator_service_name="Amazon Managed Service for Apache Flink",
        source_text="3个计算节点，单节点8核32GB，持续运行",
        quantity=1,
        hours_per_month=730,
        requirements={"node_count": 3, "vcpu": 8, "memory_gib": 32},
    )

    node_shaped_selected = GenericOfficialPlugin._auto_semantic_rates(
        node_shaped_requirement,
        rates,
        profile=profile,
    )

    assert node_shaped_requirement.requirements["kpu_count"] == 24
    assert [(item[1], item[2][2]) for item in node_shaped_selected] == [
        (18_250.0, "EUW1-KPU-Hour-Java"),
        (1200.0, "EUW1-RunningApplicationStorage"),
    ]


def test_structured_billing_variant_is_reused_without_reopening_customer_text() -> None:
    single_usage = "APN2-SingleAuthorizationRequest-API-Requests"
    batch_usage = "APN2-BatchAuthorizationRequest-API-Requests"
    profile = {
        "field_bindings": [
            {
                "field": "requests",
                "label": "请求数量",
                "usage_type": single_usage,
                "operation": "",
                "unit": "Requests",
            },
            {
                "field": "requests",
                "label": "请求数量",
                "usage_type": batch_usage,
                "operation": "",
                "unit": "Requests",
            },
            {
                "field": "requests",
                "label": "请求数量",
                "usage_type": "Global-SingleAuthorizationRequest-API-Requests",
                "operation": "",
                "unit": "Requests",
            },
        ],
        "dimensions": [
            {
                "usage_type": single_usage,
                "operation": "",
                "unit": "Requests",
                "price": 0.000005,
            },
            {
                "usage_type": batch_usage,
                "operation": "",
                "unit": "Requests",
                "price": 0.00001,
            },
            {
                "usage_type": "Global-SingleAuthorizationRequest-API-Requests",
                "operation": "",
                "unit": "Requests",
                "price": 0.000005,
            },
        ],
    }
    requirement = ServiceRequirement(
        service="verified_permissions",
        region="ap-northeast-2",
        source_text="每月 5000 万次单次授权请求",
        requirements={
            "requests": 50_000_000,
            "_billing_variant_requests": single_usage,
        },
        field_sources={
            "requirements._billing_variant_requests": "customer_text",
        },
    )

    GenericOfficialPlugin._require_billing_variant_choice(requirement, profile)

    assert requirement.requirements["_billing_variant_requests"] == single_usage
    assert (
        requirement.field_sources["requirements._billing_variant_requests"]
        == "customer_text"
    )


def test_billing_variant_adapter_does_not_reparse_customer_text() -> None:
    single_usage = "APN2-SingleAuthorizationRequest-API-Requests"
    batch_usage = "APN2-BatchAuthorizationRequest-API-Requests"
    profile = {
        "field_bindings": [
            {
                "field": "requests",
                "usage_type": single_usage,
                "operation": "",
                "unit": "Requests",
            },
            {
                "field": "requests",
                "usage_type": batch_usage,
                "operation": "",
                "unit": "Requests",
            },
        ],
        "dimensions": [
            {
                "usage_type": single_usage,
                "operation": "",
                "unit": "Requests",
                "price": 0.000005,
            },
            {
                "usage_type": batch_usage,
                "operation": "",
                "unit": "Requests",
                "price": 0.00001,
            },
        ],
    }
    requirement = ServiceRequirement(
        service="verified_permissions",
        region="ap-northeast-2",
        source_text="这段原话故意写批量授权请求，插件不允许再解析它",
        requirements={"requests": 50_000_000},
    )

    GenericOfficialPlugin._require_billing_variant_choice(requirement, profile)

    assert requirement.requirements["_billing_variant_requests"] == single_usage
    assert (
        requirement.field_sources["requirements._billing_variant_requests"]
        == "system_lowest_compatible"
    )


def test_private_link_variant_is_not_collapsed_into_normal_aws_destination() -> None:
    binding = {
        "usage_type": "APN2-DataProcDestAWSPL-Bytes",
        "operation": "",
        "description": "per GB data processed to AWS services through AWS PrivateLink",
    }

    assert GenericOfficialPlugin._billing_variant_label(binding) == "通过 PrivateLink 处理"


@pytest.mark.parametrize(
    ("usage_type", "description", "expected"),
    [
        ("EUC1-ConnectionDuration", "GraphQL real-time connection", "GraphQL 实时连接"),
        ("EUC1-EventAPIConnection", "Event API connection", "Event API 连接"),
        ("EUC1-GraphSnapshotUsage", "Neptune graph snapshot storage", "图数据库快照存储"),
        ("EUC1-BackupUsage", "Neptune database backup storage", "数据库备份存储"),
        ("EUC1-QSEnterpriseSPICE", "QuickSight enterprise SPICE", "QuickSight 企业版 SPICE"),
        ("QS-User-Enterprise-Month", "QuickSight Enterprise Edition User", "企业版作者（月付）"),
        (
            "EUC1-Reader-Enterprise-Month",
            "QuickSight Enterprise Edition Reader",
            "企业版读者（月付）",
        ),
        (
            "EUC1-Reader-Pro-Enterprise-Month",
            "QuickSight Enterprise Edition Reader Pro",
            "企业版 Reader Pro（月付）",
        ),
        (
            "EUC1-Reader-Pro-Enterprise-Month-Q",
            "QuickSight Reader Pro with Amazon Q",
            "企业版 Reader Pro + Amazon Q（月付）",
        ),
        ("QS-Reader-Usage-Paid-Session", "QuickSight Reader Sessions - Paid", "按实际读者会话付费"),
    ],
)
def test_uncommon_official_variants_have_plain_language_labels(
    usage_type: str,
    description: str,
    expected: str,
) -> None:
    assert GenericOfficialPlugin._billing_variant_label(
        {
            "usage_type": usage_type,
            "operation": "",
            "description": description,
        }
    ) == expected


def test_confirmed_neptune_storage_and_backup_are_kept_beside_instance_hours() -> None:
    instance = priced_product(
        "AmazonNeptune",
        "EUC1-InstanceUsage:db.r6g.xl",
        "Hrs",
        0.8,
        operation="CreateDBInstance:0022",
    )
    instance["product"]["attributes"].update(
        {"instanceType": "db.r6g.xlarge", "vcpu": "4", "memory": "32 GiB"}
    )
    storage = priced_product(
        "AmazonNeptune",
        "EUC1-StorageUsage",
        "GB-Mo",
        0.119,
        operation="CreateDBInstance:0022",
    )
    backup = priced_product(
        "AmazonNeptune",
        "EUC1-BackupUsage",
        "GB-Mo",
        0.023,
        operation="CreateDBInstance:0022",
    )

    def rate(product: dict):
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        return price, unit, usage_type, operation, product

    profile = {
        "field_bindings": [
            {
                "field": "storage_gib",
                "label": "存储容量",
                "usage_type": "EUC1-StorageUsage",
                "operation": "CreateDBInstance:0022",
                "unit": "GB-Mo",
            },
            {
                "field": "backup_storage_gib",
                "label": "备份容量",
                "usage_type": "EUC1-BackupUsage",
                "operation": "CreateDBInstance:0022",
                "unit": "GB-Mo",
            },
        ]
    }
    requirement = ServiceRequirement(
        service="neptune",
        quantity=1,
        requirements={
            "requested_model": "db.r6g.xlarge",
            "vcpu": 4,
            "memory_gib": 32,
            "instance_count": 3,
            "storage_gib": 500,
            "backup_storage_gib": 100,
            "_billing_variant_storage_gib": "EUC1-StorageUsage",
            "_billing_variant_backup_storage_gib": "EUC1-BackupUsage",
        },
    )

    selected = GenericOfficialPlugin._auto_semantic_rates(
        requirement,
        [rate(product) for product in (instance, storage, backup)],
        profile=profile,
    )

    assert [(item[2][2], item[1]) for item in selected] == [
        ("EUC1-InstanceUsage:db.r6g.xl", 2190),
        ("EUC1-StorageUsage", 500),
        ("EUC1-BackupUsage", 100),
    ]


def test_generic_managed_storage_converts_per_node_capacity_to_total_usage() -> None:
    storage = priced_product(
        "AmazonNeptune",
        "EUC1-StorageUsage",
        "GB-Mo",
        0.119,
        operation="CreateDBInstance:0022",
    )

    price, unit = PricingCatalog.on_demand_unit_rate(storage)
    _, usage_type, operation = PricingCatalog.billing_identity(storage)
    profile = {
        "field_bindings": [
            {
                "field": "storage_gib",
                "label": "存储容量",
                "usage_type": usage_type,
                "operation": operation,
                "unit": unit,
            }
        ]
    }
    requirement = ServiceRequirement(
        service="neptune",
        quantity=1,
        source_text="3个节点，单节点存储1T",
        requirements={
            "instance_count": 3,
            "storage_gib_per_node": 1024,
        },
    )

    selected = GenericOfficialPlugin._auto_semantic_rates(
        requirement,
        [(price, unit, usage_type, operation, storage)],
        profile=profile,
    )

    assert len(selected) == 1
    assert selected[0][1] == 3072
    assert selected[0][2][4]["_astra_source_fields"] == [
        "instance_count",
        "storage_gib_per_node",
    ]


def test_quicksight_per_reader_billing_does_not_also_charge_sessions() -> None:
    products = [
        priced_product("AmazonQuickSight", "QS-User-Enterprise-Month", "User", 24),
        priced_product("AmazonQuickSight", "EUC1-Reader-Enterprise-Month", "User", 3),
        priced_product("AmazonQuickSight", "QS-Reader-Usage-Paid-Session", "Sessions", 0.3),
        priced_product("AmazonQuickSight", "QS-Reader-Usage-Cap-Session-Q", "Sessions", 0.1),
        priced_product("AmazonQuickSight", "EUC1-QS-Enterprise-SPICE", "GB-Mo", 0.38),
    ]
    for product in products:
        product["product"]["attributes"]["edition"] = "Enterprise"
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))
    requirement = ServiceRequirement(
        service="quick_sight",
        requirements={
            "edition": "enterprise",
            "author_users": 10,
            "reader_users": 120,
            "session_capacity": 20_000,
            "spice_gib": 200,
            "_billing_variant_reader_billing_mode": "per_user",
            "_billing_variant_author_users": "QS-User-Enterprise-Month",
            "_billing_variant_reader_users": "EUC1-Reader-Enterprise-Month",
        },
    )

    selected = GenericOfficialPlugin._semantic_rates(requirement, rates)

    assert [(item[2][2], item[1]) for item in selected] == [
        ("QS-User-Enterprise-Month", 10),
        ("EUC1-Reader-Enterprise-Month", 120),
        ("EUC1-QS-Enterprise-SPICE", 200),
    ]


def test_quicksight_session_capacity_billing_does_not_also_charge_readers() -> None:
    products = [
        priced_product("AmazonQuickSight", "QS-User-Enterprise-Month", "User", 24),
        priced_product("AmazonQuickSight", "EUC1-Reader-Enterprise-Month", "User", 3),
        priced_product("AmazonQuickSight", "QS-Reader-Usage-Paid-Session", "Sessions", 0.3),
        priced_product("AmazonQuickSight", "EUC1-QS-Enterprise-SPICE", "GB-Mo", 0.38),
    ]
    for product in products:
        product["product"]["attributes"]["edition"] = "Enterprise"
    rates = []
    for product in products:
        price, unit = PricingCatalog.on_demand_unit_rate(product)
        _, usage_type, operation = PricingCatalog.billing_identity(product)
        rates.append((price, unit, usage_type, operation, product))
    requirement = ServiceRequirement(
        service="quick_sight",
        requirements={
            "edition": "enterprise",
            "author_users": 10,
            "reader_users": 120,
            "session_capacity": 20_000,
            "spice_gib": 200,
            "_billing_variant_reader_billing_mode": "capacity",
            "_billing_variant_author_users": "QS-User-Enterprise-Month",
            "_billing_variant_session_capacity": "QS-Reader-Usage-Paid-Session",
        },
    )

    selected = GenericOfficialPlugin._semantic_rates(requirement, rates)

    assert [(item[2][2], item[1]) for item in selected] == [
        ("QS-User-Enterprise-Month", 10),
        ("EUC1-QS-Enterprise-SPICE", 200),
        ("QS-Reader-Usage-Paid-Session", 20_000),
    ]
