import math

import pytest

from app.core.errors import ManualConfirmationRequired
from app.domain.models import ServiceRequirement
from app.integrations.aws import PricingCatalog
from app.services.plugins.generic_official import GenericOfficialPlugin


def priced_product(
    service_code: str,
    usage_type: str,
    unit: str,
    price: float,
    *,
    operation: str = "",
    group: str = "",
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
            requirements={"applications": ["spark"], "master_nodes": 1, "core_nodes": 5},
        ),
        [(price, unit, usage_type, operation, instance)],
    )

    assert [item[1] for item in selected] == [730, 5 * 730]
    assert [item[0] for item in selected] == [
        "Amazon EMR 主节点实例小时价",
        "Amazon EMR 核心节点实例小时价",
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


def test_explicit_customer_billing_words_resolve_the_variant_without_reasking() -> None:
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
        requirements={"requests": 50_000_000},
    )

    GenericOfficialPlugin._require_billing_variant_choice(requirement, profile)

    assert requirement.requirements["_billing_variant_requests"] == single_usage
    assert (
        requirement.field_sources["requirements._billing_variant_requests"]
        == "customer_text"
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
        ("EUC1-Reader-Enterprise-Month", "QuickSight Enterprise Edition Reader", "企业版读者（月付）"),
        ("EUC1-Reader-Pro-Enterprise-Month", "QuickSight Enterprise Edition Reader Pro", "企业版 Reader Pro（月付）"),
        ("EUC1-Reader-Pro-Enterprise-Month-Q", "QuickSight Reader Pro with Amazon Q", "企业版 Reader Pro + Amazon Q（月付）"),
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
