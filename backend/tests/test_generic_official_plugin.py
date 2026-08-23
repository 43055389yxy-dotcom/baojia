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
) -> dict:
    return {
        "serviceCode": service_code,
        "product": {
            "sku": usage_type,
            "attributes": {
                "usagetype": usage_type,
                "operation": operation,
                "regionCode": "ap-northeast-1",
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
        return ["AWSLambda", "AmazonDynamoDB"]

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


def test_generic_plugin_discovers_service_and_uses_one_minimum_official_unit() -> None:
    plugin = GenericOfficialPlugin(None, FakeCatalog())  # type: ignore[arg-type]
    requirement = ServiceRequirement(
        service="lambda",
        calculator_service_name="AWS Lambda",
        region="ap-southeast-1",
    )

    preview = plugin.preview(requirement, "ap-southeast-1")
    selected = plugin.select(requirement, "ap-southeast-1")

    assert preview.requires_confirmation is False
    assert selected.reference_rates == []
    assert selected.usage_lines[0].service_code == "AWSLambda"
    assert selected.usage_lines[0].amount == 1


def test_generic_plugin_resolves_official_code_by_unique_stem() -> None:
    plugin = GenericOfficialPlugin(None, FakeCatalog())  # type: ignore[arg-type]

    assert plugin._service_code(ServiceRequirement(service="dynamodb")) == "AmazonDynamoDB"


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
        priced_product("AWSLambda", "APN1-Request", "Request", 0.0000002),
        priced_product(
            "AWSLambda", "APN1-Lambda-GB-Second", "Lambda-GB-Second", 0.000015
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

    assert selected.reference_rates == []
    assert selected.usage_lines[0].service_code == "AmazonGrafana"
    assert selected.usage_lines[0].amount == 1
    assert selected.usage_lines[0].usage_type.endswith("ViewerUser")


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
