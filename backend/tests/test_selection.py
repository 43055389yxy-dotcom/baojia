import pytest

import app.services.plugins.ec2 as ec2_module
import app.services.plugins.rds as rds_module

from app.core.errors import ManualConfirmationRequired
from app.domain.models import QueryAction, ServiceKind, ServiceRequirement, UsageLine
from app.services.plugins.ec2 import (
    Ec2Plugin,
    _pricing_operating_system,
    _pricing_tenancy,
    _rank_reasonable_ec2,
    _select_instance,
)
from app.services.plugins.rds import (
    RdsPlugin,
    _billing_deployment,
    _deployment_matches,
    _display_name,
    _preferred_rds_products,
    _priced_instance_count,
    _rds_api_engine,
    _resolve_volume_type,
    _select_rds,
)
from app.services.plugins.redis import (
    RedisPlugin,
    _base_cache_products,
    _invalid_model_neighbors,
    _purchase_compatible_candidates,
    _select_cache,
)


def test_ec2_selects_smallest_official_fit() -> None:
    candidates = [
        {"model": "m7g.large", "vcpu": 2, "memory_gib": 8, "current_generation": True},
        {"model": "t4g.medium", "vcpu": 2, "memory_gib": 4, "current_generation": True},
        {"model": "m6g.large", "vcpu": 2, "memory_gib": 8, "current_generation": False},
    ]
    selected, substituted = _select_instance(
        candidates, requested_model=None, min_vcpu=2, min_memory=3
    )
    assert selected["model"] == "t4g.medium"
    assert substituted is False


def test_ec2_exact_model_query_falls_back_to_reviewed_shape_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ShapeFallbackExecutor:
        def __init__(self, _clients: object) -> None:
            pass

        def execute(self, **arguments: object) -> dict[str, object]:
            parameters = arguments.get("parameters")
            if isinstance(parameters, dict) and parameters.get("InstanceTypes"):
                raise ManualConfirmationRequired(
                    "temporary endpoint failure", code="aws_query_execution_failed"
                )
            return {
                "InstanceTypes": [
                    {
                        "InstanceType": "m5zn.6xlarge",
                        "VCpuInfo": {"DefaultVCpus": 24},
                        "MemoryInfo": {"SizeInMiB": 96 * 1024},
                        "CurrentGeneration": True,
                        "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
                    }
                ]
            }

    monkeypatch.setattr(ec2_module, "ReadOnlyAwsQueryExecutor", ShapeFallbackExecutor)
    plugin = Ec2Plugin(None, None)  # type: ignore[arg-type]

    candidates = plugin._official_candidates("ap-southeast-99", "m5zn.6xlarge", 24, 96)

    assert candidates[0]["model"] == "m5zn.6xlarge"
    assert candidates[0]["vcpu"] == 24
    assert candidates[0]["memory_gib"] == 96


def test_ec2_configuration_picker_uses_complete_regional_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = Ec2Plugin(None, None)  # type: ignore[arg-type]
    captured: dict[str, object] = {}

    def official_candidates(
        region: str,
        requested_model: str | None,
        requested_vcpu: float | None = None,
        requested_memory: float | None = None,
        *,
        include_all_models: bool = False,
    ) -> list[dict[str, object]]:
        captured.update(
            {
                "region": region,
                "requested_model": requested_model,
                "requested_vcpu": requested_vcpu,
                "requested_memory": requested_memory,
                "include_all_models": include_all_models,
            }
        )
        return [
            {
                "model": "m7g.large",
                "vcpu": 2,
                "memory_gib": 8,
                "current_generation": True,
                "family": "general_purpose",
                "architectures": ["arm64"],
            },
            {
                "model": "m6i.large",
                "vcpu": 2,
                "memory_gib": 8,
                "current_generation": True,
                "family": "general_purpose",
                "architectures": ["x86_64"],
            },
            {
                "model": "m4.large",
                "vcpu": 2,
                "memory_gib": 8,
                "current_generation": False,
                "family": "general_purpose",
                "architectures": ["x86_64"],
            },
        ]

    monkeypatch.setattr(plugin, "_official_candidates", official_candidates)
    choices = plugin.configuration_candidates(
        ServiceRequirement(
            service="ec2",
            region="ap-south-1",
            requirements={"vcpu": 16, "memory_gib": 64},
        ),
        "ap-southeast-1",
    )

    assert captured == {
        "region": "ap-south-1",
        "requested_model": None,
        "requested_vcpu": None,
        "requested_memory": None,
        "include_all_models": True,
    }
    assert [choice.model for choice in choices] == [
        "m7g.large",
        "m6i.large",
        "m4.large",
    ]
    assert choices[0].specifications["processorArchitectures"] == ["arm64"]
    assert choices[0].specifications["instanceFamily"] == "general_purpose"
    assert choices[0].specifications["currentGeneration"] is True


def test_generic_rds_ssd_defaults_to_official_gp3_value() -> None:
    values = [
        "General Purpose (SSD)",
        "General Purpose-GP3",
        "Provisioned IOPS (SSD)",
        "Provisioned IOPS-IO2",
    ]

    assert _resolve_volume_type("SSD", values) == "General Purpose-GP3"
    assert _resolve_volume_type("通用型 SSD", values) == "General Purpose-GP3"


def test_rds_bare_community_patch_does_not_become_literal_rds_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Executor:
        def __init__(self, _clients: object) -> None:
            pass

        def execute(self, **arguments: object) -> dict[str, object]:
            captured.update(arguments)
            return {"OrderableDBInstanceOptions": [{"DBInstanceClass": "db.m6g.large"}]}

    monkeypatch.setattr(rds_module, "ReadOnlyAwsQueryExecutor", Executor)
    plugin = RdsPlugin(None, None)  # type: ignore[arg-type]

    classes = plugin._orderable_classes("ap-southeast-1", "mysql", "5.7.44")

    assert classes == {"db.m6g.large"}
    assert "EngineVersion" not in captured["parameters"]


def test_rds_unavailable_legacy_model_falls_back_to_orderable_engine_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, object]] = []

    class Executor:
        def __init__(self, _clients: object) -> None:
            pass

        def execute(self, **arguments: object) -> dict[str, object]:
            parameters = dict(arguments.get("parameters") or {})
            captured.append(parameters)
            if parameters.get("DBInstanceClass") == "db.m4.xlarge":
                return {"OrderableDBInstanceOptions": []}
            return {
                "OrderableDBInstanceOptions": [
                    {"DBInstanceClass": "db.m6g.xlarge"},
                    {"DBInstanceClass": "db.m7g.xlarge"},
                ]
            }

    monkeypatch.setattr(rds_module, "ReadOnlyAwsQueryExecutor", Executor)
    plugin = RdsPlugin(None, None)  # type: ignore[arg-type]

    classes = plugin._orderable_classes(
        "us-east-1",
        "mysql",
        "8.4.11",
        requested_model="db.m4.xlarge",
    )

    assert classes == {"db.m6g.xlarge", "db.m7g.xlarge"}
    assert captured == [
        {"Engine": "mysql", "DBInstanceClass": "db.m4.xlarge"},
        {"Engine": "mysql"},
    ]


def test_rds_configuration_picker_uses_complete_orderable_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    first_product = {
        "rate": 0.4,
        "product": {"sku": "arm", "attributes": {"regionCode": "ap-south-1"}},
    }
    second_product = {
        "rate": 0.5,
        "product": {"sku": "x86", "attributes": {"regionCode": "ap-south-1"}},
    }

    class Catalog:
        def products(
            self,
            service_code: str,
            filters: dict[str, str],
            *,
            max_pages: int,
        ) -> list[dict[str, object]]:
            captured.update(
                {
                    "service_code": service_code,
                    "filters": filters,
                    "max_pages": max_pages,
                }
            )
            return [first_product, second_product]

    plugin = RdsPlugin(None, Catalog())  # type: ignore[arg-type]
    monkeypatch.setattr(
        plugin,
        "_orderable_classes",
        lambda *_args, **_kwargs: {"db.m7g.large", "db.m7i.large"},
    )
    monkeypatch.setattr(
        rds_module,
        "_rds_candidates",
        lambda *_args, **_kwargs: [
            {
                "model": "db.m7g.large",
                "vcpu": 2.0,
                "memory_gib": 8.0,
                "products": [first_product],
            },
            {
                "model": "db.m7i.large",
                "vcpu": 2.0,
                "memory_gib": 8.0,
                "products": [second_product],
            },
        ],
    )
    monkeypatch.setattr(
        rds_module.PricingCatalog,
        "on_demand_rate",
        staticmethod(lambda product: float(product["rate"])),
    )

    choices = plugin.configuration_candidates(
        ServiceRequirement(
            service="rds",
            region="ap-south-1",
            quantity=2,
            requirements={"engine": "mysql", "deployment": "multi_az"},
        ),
        "ap-southeast-1",
    )

    assert captured["service_code"] == "AmazonRDS"
    assert captured["max_pages"] == 50
    assert [choice.model for choice in choices] == ["db.m7g.large", "db.m7i.large"]
    assert choices[0].monthly_catalog_cost == pytest.approx(0.4 * 730 * 2)


def test_aurora_cluster_uses_member_instance_pricing_dimension() -> None:
    assert (
        _billing_deployment("aurora_mysql", {"deployment": "multi_az", "aurora_cluster": True})
        == "single_az"
    )
    assert _billing_deployment("mysql", {"deployment": "multi_az"}) == "multi_az"
    requirement = ServiceRequirement(
        service="rds",
        quantity=2,
        requirements={"engine": "aurora_mysql", "cluster_members": 3},
    )
    assert _priced_instance_count(requirement, "aurora_mysql", requirement.requirements) == 6
    assert _display_name("aurora_mysql") == "Amazon Aurora MySQL"


def test_aurora_defaults_to_standard_instance_usage_product() -> None:
    standard = {"product": {"attributes": {"usagetype": "APS1-InstanceUsage:db.r7g.large"}}}
    io_optimized = {
        "product": {"attributes": {"usagetype": "APS1-InstanceUsageIOOptimized:db.r7g.large"}}
    }

    assert _preferred_rds_products([io_optimized, standard], "aurora_mysql") == [standard]
    assert _preferred_rds_products([io_optimized, standard], "mysql") == [
        io_optimized,
        standard,
    ]


def test_explicit_rds_provisioned_iops_is_not_replaced_by_gp3() -> None:
    values = ["General Purpose-GP3", "Provisioned IOPS-IO2"]

    assert _resolve_volume_type("io2", values) == "Provisioned IOPS-IO2"


def test_rds_quote_keeps_priced_storage_capacity_in_display_specifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = {
        "serviceCode": "AmazonRDS",
        "product": {
            "sku": "rds-test-sku",
            "attributes": {
                "usagetype": "APN1-Multi-AZUsage:db.m6g.4xl",
                "operation": "CreateDBInstance:0002",
                "databaseEngine": "MySQL",
                "deploymentOption": "Multi-AZ",
                "regionCode": "ap-northeast-1",
            },
        },
    }

    class Catalog:
        def products(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return [product]

    plugin = RdsPlugin(None, Catalog())  # type: ignore[arg-type]
    monkeypatch.setattr(plugin, "_orderable_classes", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(
        rds_module,
        "_rds_candidates",
        lambda *_args, **_kwargs: [
            {
                "model": "db.m6g.4xlarge",
                "vcpu": 16.0,
                "memory_gib": 64.0,
                "products": [product],
            }
        ],
    )
    monkeypatch.setattr(
        plugin,
        "_storage_usage",
        lambda *_args, **_kwargs: (
            UsageLine(
                key="rdsstg",
                service_code="AmazonRDS",
                usage_type="APN1-RDS:Multi-AZ-GP3-Storage",
                operation="CreateDBInstance:0002",
                amount=300,
                group="rds-storage",
            ),
            "General Purpose-GP3",
        ),
    )
    requirement = ServiceRequirement(
        service="rds",
        region="ap-northeast-1",
        requirements={
            "engine": "mysql",
            "deployment": "multi_az",
            "requested_model": "db.m6g.4xlarge",
            "storage_gib": 300,
        },
    )

    selection = plugin.select(requirement, "ap-northeast-1")

    assert selection.specifications["storageType"] == "General Purpose-GP3"
    assert selection.specifications["storageGiB"] == 300
    assert selection.usage_lines[-1].amount == 300


def test_ec2_user_os_maps_to_price_list_family() -> None:
    assert _pricing_operating_system("Ubuntu 22.04") == "Linux"
    assert _pricing_operating_system("Amazon Linux 2023") == "Linux"
    assert _pricing_operating_system("Windows Server 2022") == "Windows"


def test_ec2_default_tenancy_maps_to_shared_pricing_value() -> None:
    assert _pricing_tenancy(None) == "Shared"
    assert _pricing_tenancy("default") == "Shared"
    assert _pricing_tenancy("shared") == "Shared"
    assert _pricing_tenancy("dedicated") == "Dedicated"


def test_ec2_reserved_quote_selects_product_record_with_exact_reserved_terms() -> None:
    def product(sku: str, operation: str, *, reserved: bool) -> dict[str, object]:
        terms: dict[str, object] = {}
        if reserved:
            terms = {
                "offer": {
                    "termAttributes": {
                        "LeaseContractLength": "1yr",
                        "PurchaseOption": "All Upfront",
                        "OfferingClass": "standard",
                    },
                    "priceDimensions": {
                        "upfront": {
                            "unit": "Quantity",
                            "pricePerUnit": {"USD": "1200"},
                        }
                    },
                }
            }
        return {
            "serviceCode": "AmazonEC2",
            "product": {
                "sku": sku,
                "attributes": {
                    "operation": operation,
                    "usagetype": "APS1-BoxUsage:t3.xlarge",
                },
            },
            "terms": {"Reserved": terms},
        }

    on_demand_only = product("000-first", "RunInstances:0800", reserved=False)
    reserved_record = product("999-reserved", "RunInstances:0002", reserved=True)

    class Catalog:
        def products(self, *_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return [on_demand_only, reserved_record]

    plugin = Ec2Plugin(None, Catalog())  # type: ignore[arg-type]

    selected = plugin._compute_product(
        "ap-southeast-1",
        "t3.xlarge",
        "Windows",
        "Shared",
        reserved_years=1,
        payment_option="all_upfront",
        offering_class="standard",
    )

    assert selected is reserved_record


def test_ec2_compute_shape_is_valid_for_general_application() -> None:
    candidates = [
        {
            "model": "m6a.xlarge",
            "vcpu": 4,
            "memory_gib": 16,
            "current_generation": True,
            "family": "general_purpose",
            "architectures": ["x86_64"],
        },
        {
            "model": "c7g.xlarge",
            "vcpu": 4,
            "memory_gib": 8,
            "current_generation": True,
            "family": "compute_optimized",
            "architectures": ["arm64"],
        },
    ]
    ranked = _rank_reasonable_ec2(
        candidates,
        business_type="general_purpose",
        architecture=None,
        min_vcpu=4,
        min_memory=8,
    )
    assert ranked[0]["model"] == "c7g.xlarge"


def test_ec2_requested_model_does_not_query_price_list(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = Ec2Plugin(None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        plugin,
        "_official_candidates",
        lambda _region, _model, *_shape: [
            {
                "model": "c5a.4xlarge",
                "vcpu": 16,
                "memory_gib": 32,
                "current_generation": True,
                "family": "compute_optimized",
                "architectures": ["x86_64"],
            }
        ],
    )

    monkeypatch.setattr(
        plugin,
        "_compute_product",
        lambda *_args, **_kwargs: {
            "serviceCode": "AmazonEC2",
            "product": {
                "sku": "sku",
                "attributes": {
                    "usagetype": "APS2-BoxUsage:c5a.4xlarge",
                    "operation": "RunInstances:0002",
                },
            },
        },
    )
    requirement = ServiceRequirement(
        service=ServiceKind.EC2,
        region="ap-southeast-2",
        quantity=4,
        requirements={
            "requested_model": "c5a.4xlarge",
            "operating_system": "windows",
            "purchase_option": "on_demand",
        },
        query_action=QueryAction.DISCOVER_EC2_INSTANCES,
    )

    preview = plugin.preview(requirement, "ap-southeast-1")
    selection = plugin.select(
        requirement.model_copy(
            update={"requirements": {**requirement.requirements, "system_disk_gib": None}}
        ),
        "ap-southeast-1",
    )
    assert preview.selected_model == "c5a.4xlarge"
    assert selection.model == "c5a.4xlarge"
    assert selection.usage_lines[0].usage_type == "APS2-BoxUsage:c5a.4xlarge"


def test_confirmed_ec2_model_wins_over_restored_original_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = Ec2Plugin(None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        plugin,
        "_official_candidates",
        lambda *_args: [
            {
                "model": "t2.micro",
                "vcpu": 1.0,
                "memory_gib": 1.0,
                "current_generation": True,
                "family": "general_purpose",
                "architectures": ["x86_64"],
            }
        ],
    )
    monkeypatch.setattr(
        plugin,
        "_compute_product",
        lambda *_args, **_kwargs: {
            "serviceCode": "AmazonEC2",
            "product": {
                "sku": "t2-micro-sku",
                "attributes": {
                    "usagetype": "APN1-BoxUsage:t2.micro",
                    "operation": "RunInstances",
                },
            },
        },
    )
    requirement = ServiceRequirement(
        service="ec2",
        region="ap-northeast-1",
        quantity=8,
        requirements={
            "requested_model": "t2.micro",
            "vcpu": 6,
            "memory_gib": 24,
            "operating_system": "linux",
        },
    )

    selection = plugin.select(requirement, "ap-southeast-1")

    assert selection.model == "t2.micro"
    assert selection.specifications["vCPU"] == 1.0
    assert selection.specifications["memoryGiB"] == 1.0


def test_windows_on_arm_instance_is_customer_conflict_before_pricing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = Ec2Plugin(None, None)  # type: ignore[arg-type]

    def candidates(_region: str, model: str | None, *_shape: object):
        if model:
            return [
                {
                    "model": "c7g.xlarge",
                    "vcpu": 4.0,
                    "memory_gib": 8.0,
                    "current_generation": True,
                    "family": "compute_optimized",
                    "architectures": ["arm64"],
                }
            ]
        return [
            {
                "model": "c7i.xlarge",
                "vcpu": 4.0,
                "memory_gib": 8.0,
                "current_generation": True,
                "family": "compute_optimized",
                "architectures": ["x86_64"],
            }
        ]

    monkeypatch.setattr(plugin, "_official_candidates", candidates)
    requirement = ServiceRequirement(
        service="ec2",
        region="ap-northeast-1",
        requirements={
            "requested_model": "c7g.xlarge",
            "operating_system": "Windows Server 2022",
        },
    )

    notice = plugin.specified_model_compatibility_notice(requirement, "ap-southeast-1")

    assert notice is not None
    assert "ARM" in notice
    assert "不支持 Windows" in notice
    assert "c7i.xlarge" in notice


def test_windows_and_arm_edit_is_rejected_even_without_a_cached_model() -> None:
    plugin = Ec2Plugin(None, None)  # type: ignore[arg-type]
    requirement = ServiceRequirement(
        service="ec2",
        region="ap-southeast-1",
        requirements={
            "operating_system": "Windows Server 2022",
            "architecture": "arm64",
            "vcpu": 12,
            "memory_gib": 32,
        },
    )

    notice = plugin.specified_model_compatibility_notice(requirement, "ap-southeast-1")

    assert notice is not None
    assert "Windows Server" in notice
    assert "ARM64" in notice
    assert "x86_64" in notice


def test_ec2_nonstandard_shape_returns_lower_and_upper_official_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = Ec2Plugin(None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        plugin,
        "_official_candidates",
        lambda _region, _model, *_shape: [
            {
                "model": "m7g.large",
                "vcpu": 2.0,
                "memory_gib": 8.0,
                "current_generation": True,
                "family": "general_purpose",
                "architectures": ["arm64"],
            },
            {
                "model": "m7g.xlarge",
                "vcpu": 4.0,
                "memory_gib": 16.0,
                "current_generation": True,
                "family": "general_purpose",
                "architectures": ["arm64"],
            },
        ],
    )
    requirement = ServiceRequirement(
        service="ec2",
        region="ap-northeast-1",
        requirements={"vcpu": 3, "memory_gib": 12},
    )

    options = plugin.nearest_shape_options(requirement, "ap-southeast-1")

    assert [(item["vcpu"], item["memory_gib"]) for item in options] == [
        (2.0, 8.0),
        (4.0, 16.0),
    ]


def test_ec2_replacement_picker_keeps_smaller_official_shapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = Ec2Plugin(None, None)  # type: ignore[arg-type]
    official = [
        {
            "model": "m7g.large",
            "vcpu": 2.0,
            "memory_gib": 8.0,
            "current_generation": True,
            "family": "general_purpose",
            "architectures": ["arm64"],
        },
        {
            "model": "m7g.xlarge",
            "vcpu": 4.0,
            "memory_gib": 16.0,
            "current_generation": True,
            "family": "general_purpose",
            "architectures": ["arm64"],
        },
        {
            "model": "m7g.2xlarge",
            "vcpu": 8.0,
            "memory_gib": 32.0,
            "current_generation": True,
            "family": "general_purpose",
            "architectures": ["arm64"],
        },
        {
            "model": "m7i.2xlarge",
            "vcpu": 8.0,
            "memory_gib": 32.0,
            "current_generation": True,
            "family": "general_purpose",
            "architectures": ["x86_64"],
        },
    ]
    monkeypatch.setattr(plugin, "_official_candidates", lambda *_args: official)
    monkeypatch.setattr(
        plugin,
        "_compute_product",
        lambda _region, model, *_args: {
            "serviceCode": "AmazonEC2",
            "product": {
                "sku": f"sku-{model}",
                "attributes": {
                    "usagetype": f"APS1-BoxUsage:{model}",
                    "operation": "RunInstances",
                    "regionCode": "ap-southeast-1",
                },
            },
            "terms": {
                "OnDemand": {
                    "term": {"priceDimensions": {"rate": {"pricePerUnit": {"USD": "0.1"}}}}
                }
            },
        },
    )

    preview = plugin.preview(
        ServiceRequirement(
            service="ec2",
            region="ap-northeast-1",
            requirements={"vcpu": 6, "memory_gib": 24},
        ),
        "ap-southeast-1",
    )

    assert preview.requires_confirmation is False
    assert preview.selected_model in {"m7g.2xlarge", "m7i.2xlarge"}
    assert {option.specifications["vCPU"] for option in preview.candidates} == {
        2.0,
        4.0,
        8.0,
    }


def test_ec2_exact_shape_with_multiple_models_does_not_ask_customer_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = Ec2Plugin(None, None)  # type: ignore[arg-type]
    official = [
        {
            "model": "c7g.xlarge",
            "vcpu": 4.0,
            "memory_gib": 8.0,
            "current_generation": True,
            "family": "compute_optimized",
            "architectures": ["arm64"],
        },
        {
            "model": "c6i.xlarge",
            "vcpu": 4.0,
            "memory_gib": 8.0,
            "current_generation": True,
            "family": "compute_optimized",
            "architectures": ["x86_64"],
        },
    ]
    monkeypatch.setattr(plugin, "_official_candidates", lambda *_args: official)
    monkeypatch.setattr(
        plugin,
        "_compute_product",
        lambda _region, model, *_args: {
            "serviceCode": "AmazonEC2",
            "product": {
                "sku": f"sku-{model}",
                "attributes": {
                    "usagetype": f"APS1-BoxUsage:{model}",
                    "operation": "RunInstances",
                    "regionCode": "ap-southeast-1",
                },
            },
            "terms": {
                "OnDemand": {
                    "term": {"priceDimensions": {"rate": {"pricePerUnit": {"USD": "0.1"}}}}
                }
            },
        },
    )

    preview = plugin.preview(
        ServiceRequirement(
            service="ec2",
            region="ap-southeast-1",
            requirements={"vcpu": 4, "memory_gib": 8},
        ),
        "ap-southeast-1",
    )

    assert preview.requires_confirmation is False
    assert preview.selected_model in {"c7g.xlarge", "c6i.xlarge"}
    assert len(preview.candidates) == 2


def test_self_hosted_workload_requires_customer_to_choose_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = Ec2Plugin(None, None)  # type: ignore[arg-type]
    official = [
        {
            "model": "t4g.small",
            "vcpu": 2.0,
            "memory_gib": 2.0,
            "current_generation": True,
            "family": "general_purpose",
            "architectures": ["arm64"],
        },
        {
            "model": "t4g.medium",
            "vcpu": 2.0,
            "memory_gib": 4.0,
            "current_generation": True,
            "family": "general_purpose",
            "architectures": ["arm64"],
        },
    ]
    monkeypatch.setattr(plugin, "_official_candidates", lambda *_args: official)
    monkeypatch.setattr(
        plugin,
        "_compute_product",
        lambda _region, model, *_args: {
            "serviceCode": "AmazonEC2",
            "product": {
                "sku": f"sku-{model}",
                "attributes": {
                    "usagetype": f"APS1-BoxUsage:{model}",
                    "operation": "RunInstances",
                    "regionCode": "ap-southeast-1",
                },
            },
            "terms": {
                "OnDemand": {
                    "term": {"priceDimensions": {"rate": {"pricePerUnit": {"USD": "0.1"}}}}
                }
            },
        },
    )

    preview = plugin.preview(
        ServiceRequirement(
            service="ec2",
            region="ap-southeast-1",
            requirements={"vcpu": 2, "memory_gib": 2},
            field_sources={
                "requirements.vcpu": "system_minimum",
                "requirements.memory_gib": "system_minimum",
                "_customer_select_configuration": "customer_confirmation",
            },
        ),
        "ap-southeast-1",
    )

    assert preview.requires_confirmation is True
    assert preview.selected_model is None
    assert preview.confirmation_reason == "请选择自建服务的机器台数和每台 EC2 配置（当前 1 台）。"


def test_ec2_rejects_invalid_model_without_replacement_basis() -> None:
    with pytest.raises(ManualConfirmationRequired) as error:
        _select_instance(
            [{"model": "t4g.micro", "vcpu": 2, "memory_gib": 1, "current_generation": True}],
            requested_model="made.up",
            min_vcpu=None,
            min_memory=None,
        )
    assert error.value.code == "invalid_ec2_model_without_replacement_basis"


def test_redis_one_gib_uses_next_official_size() -> None:
    candidates = [
        {"model": "cache.t4g.micro", "memory_gib": 0.5, "vcpu": 2, "products": [{}]},
        {"model": "cache.t4g.small", "memory_gib": 1.37, "vcpu": 2, "products": [{}]},
        {"model": "cache.t4g.medium", "memory_gib": 3.09, "vcpu": 2, "products": [{}]},
    ]
    selected, substituted = _select_cache(
        candidates, requested_model=None, min_memory=1, min_vcpu=None
    )
    assert selected["model"] == "cache.t4g.small"
    assert selected["memory_gib"] == 1.37
    assert substituted is False


def test_redis_confirmed_official_model_does_not_ask_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = RedisPlugin(None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        plugin,
        "nearby_candidates",
        lambda *_args, **_kwargs: [{"model": "cache.r4.large", "memory_gib": 12.3, "vcpu": 2.0}],
    )
    requirement = ServiceRequirement(
        service="elasticache",
        requirements={
            "engine": "redis",
            "memory_gib": 8,
            "requested_model": "cache.r4.large",
        },
    )

    preview = plugin.preview(requirement, "ap-southeast-1")

    assert preview.selected_model == "cache.r4.large"
    assert preview.requires_confirmation is False


def test_redis_exact_model_is_authoritative_over_descriptive_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = RedisPlugin(None, None)  # type: ignore[arg-type]

    def nearby(
        requirement: ServiceRequirement, *_args: object, **_kwargs: object
    ) -> list[dict[str, object]]:
        if requirement.requirements.get("requested_model"):
            return [{"model": "cache.t4g.medium", "memory_gib": 3.09, "vcpu": 2.0}]
        return [
            {"model": "cache.t4g.medium", "memory_gib": 3.09, "vcpu": 2.0},
            {"model": "cache.m7g.large", "memory_gib": 6.38, "vcpu": 2.0},
        ]

    monkeypatch.setattr(plugin, "nearby_candidates", nearby)
    requirement = ServiceRequirement(
        service="elasticache",
        requirements={
            "engine": "redis",
            "memory_gib": 6,
            "requested_model": "cache.t4g.medium",
        },
    )

    preview = plugin.preview(requirement, "ap-southeast-1")

    assert preview.selected_model == "cache.t4g.medium"
    assert preview.requires_confirmation is False
    assert {option.model for option in preview.candidates} == {"cache.t4g.medium"}


def test_redis_select_keeps_explicit_model_even_when_shape_is_larger() -> None:
    candidates = [
        {
            "model": "cache.t4g.medium",
            "memory_gib": 3.09,
            "vcpu": 2.0,
            "hourly_rate": 0.1,
        },
        {
            "model": "cache.m5.large",
            "memory_gib": 6.38,
            "vcpu": 2.0,
            "hourly_rate": 0.2,
        },
    ]

    selected, substituted = _select_cache(
        candidates,
        requested_model="cache.t4g.medium",
        min_memory=6,
        min_vcpu=2,
    )

    assert selected["model"] == "cache.t4g.medium"
    assert substituted is False


def test_invalid_redis_family_size_uses_meaningful_lower_and_upper_neighbors() -> None:
    candidates = [
        {"model": "cache.t2.micro", "memory_gib": 0.5, "hourly_rate": 0.02},
        {"model": "cache.t4g.medium", "memory_gib": 3.09, "hourly_rate": 0.095},
        {"model": "cache.t3.medium", "memory_gib": 3.09, "hourly_rate": 0.10},
        {"model": "cache.r7g.large", "memory_gib": 13.07, "hourly_rate": 0.263},
        {"model": "cache.r7g.xlarge", "memory_gib": 26.32, "hourly_rate": 0.525},
    ]

    choices = _invalid_model_neighbors(candidates, "cache.r7g.medium")

    assert [item["model"] for item in choices] == [
        "cache.t4g.medium",
        "cache.r7g.large",
    ]


def test_invalid_explicit_redis_model_is_auto_replaced_without_customer_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = RedisPlugin(None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        plugin,
        "nearby_candidates",
        lambda *_args, **_kwargs: [
            {"model": "cache.t4g.medium", "memory_gib": 3.09, "vcpu": 2.0},
            {"model": "cache.r7g.large", "memory_gib": 13.07, "vcpu": 2.0},
        ],
    )
    requirement = ServiceRequirement(
        service="elasticache",
        requirements={"engine": "redis", "requested_model": "cache.r7g.medium"},
    )

    preview = plugin.preview(requirement, "ap-southeast-1")

    assert preview.requires_confirmation is False
    assert preview.selected_model == "cache.t4g.medium"
    assert preview.confirmation_reason is None
    assert len(preview.candidates) == 2


def test_redis_unspecified_model_uses_cheapest_eligible_official_rate() -> None:
    candidates = [
        {
            "model": "cache.older.small",
            "memory_gib": 6.0,
            "vcpu": 2.0,
            "hourly_rate": 0.3,
        },
        {
            "model": "cache.newer.large",
            "memory_gib": 8.0,
            "vcpu": 2.0,
            "hourly_rate": 0.15,
        },
    ]

    selected, substituted = _select_cache(
        candidates,
        requested_model=None,
        min_memory=5,
        min_vcpu=2,
    )

    assert selected["model"] == "cache.newer.large"
    assert substituted is False


def test_redis_preview_without_model_or_capacity_requires_catalog_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = RedisPlugin(None, None)  # type: ignore[arg-type]

    def lowest_candidates(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return [
            {"model": "cache.t4g.micro", "memory_gib": 0.5, "vcpu": 2, "region": "ap-southeast-3"},
            {"model": "cache.t4g.small", "memory_gib": 1.37, "vcpu": 2, "region": "ap-southeast-3"},
        ]

    monkeypatch.setattr(plugin, "nearby_candidates", lowest_candidates)
    requirement = ServiceRequirement(
        service="elasticache",
        requirements={"engine": "redis", "shards": 1, "replicas_per_shard": 1},
    )

    preview = plugin.preview(requirement, "ap-southeast-3")

    assert preview.selected_model is None
    assert preview.requires_confirmation is True
    assert [option.model for option in preview.candidates] == [
        "cache.t4g.micro",
        "cache.t4g.small",
    ]


def test_redis_shape_without_exact_official_size_uses_non_underprovisioned_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = RedisPlugin(None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        plugin,
        "nearby_candidates",
        lambda *_args, **_kwargs: [
            {"model": "cache.m4.large", "memory_gib": 6.42, "vcpu": 2.0},
            {"model": "cache.r6g.large", "memory_gib": 13.07, "vcpu": 2.0},
        ],
    )
    preview = plugin.preview(
        ServiceRequirement(
            service="elasticache",
            requirements={"engine": "redis", "memory_gib": 8},
        ),
        "ap-southeast-1",
    )

    assert preview.selected_model == "cache.r6g.large"
    assert preview.requires_confirmation is False
    assert preview.confirmation_reason is None


def test_redis_exact_memory_with_multiple_models_does_not_ask_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = RedisPlugin(None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        plugin,
        "nearby_candidates",
        lambda *_args, **_kwargs: [
            {"model": "cache.c7g.large", "memory_gib": 8.0, "vcpu": 2.0},
            {"model": "cache.m7g.large", "memory_gib": 8.0, "vcpu": 4.0},
        ],
    )

    preview = plugin.preview(
        ServiceRequirement(
            service="elasticache",
            requirements={"engine": "redis", "memory_gib": 8},
        ),
        "ap-southeast-1",
    )

    assert preview.requires_confirmation is False
    assert preview.selected_model in {"cache.c7g.large", "cache.m7g.large"}


def test_redis_reserved_selection_excludes_old_family_without_requested_offer() -> None:
    def product(*, purchase_option: str, upfront: float) -> dict[str, object]:
        return {
            "product": {"attributes": {"usagetype": "NodeUsage:cache.test"}},
            "terms": {
                "Reserved": {
                    "term": {
                        "termAttributes": {
                            "LeaseContractLength": "1yr",
                            "PurchaseOption": purchase_option,
                        },
                        "priceDimensions": {
                            "upfront": {
                                "unit": "Quantity",
                                "pricePerUnit": {"USD": str(upfront)},
                            }
                        },
                    }
                }
            },
        }

    candidates = [
        {
            "model": "cache.m4.xlarge",
            "memory_gib": 14.28,
            "vcpu": 4.0,
            "hourly_rate": 0.10,
            "products": [product(purchase_option="Heavy Utilization", upfront=500)],
        },
        {
            "model": "cache.r6g.xlarge",
            "memory_gib": 26.32,
            "vcpu": 4.0,
            "hourly_rate": 0.20,
            "products": [product(purchase_option="All Upfront", upfront=1200)],
        },
    ]

    compatible = _purchase_compatible_candidates(
        candidates,
        years=1,
        payment_option="all_upfront",
        hours_per_month=730,
    )
    selected, _ = _select_cache(
        compatible,
        requested_model=None,
        min_memory=16,
        min_vcpu=4,
    )

    assert [item["model"] for item in compatible] == ["cache.r6g.xlarge"]
    assert selected["model"] == "cache.r6g.xlarge"


def test_redis_exact_official_memory_does_not_require_customer_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = RedisPlugin(None, None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        plugin,
        "nearby_candidates",
        lambda *_args, **_kwargs: [
            {"model": "cache.r6g.large", "memory_gib": 13.07, "vcpu": 2.0},
        ],
    )

    preview = plugin.preview(
        ServiceRequirement(
            service="elasticache",
            requirements={"engine": "redis", "memory_gib": 13.07},
        ),
        "ap-southeast-1",
    )

    assert preview.selected_model == "cache.r6g.large"
    assert preview.requires_confirmation is False
    assert preview.confirmation_reason is None


def test_redis_excludes_add_on_billing_products() -> None:
    def product(usage_type: str) -> dict[str, object]:
        return {"product": {"attributes": {"usagetype": usage_type}}}

    products = [
        product("APN1-NodeUsage:cache.m5.large"),
        product("APN1-Outpost-NodeUsage:cache.m5.large"),
        product("APN1-ExtendedSupportYr1_Yr2-NodeUsage:cache.m5.large"),
    ]
    assert _base_cache_products(products) == [products[0]]  # type: ignore[arg-type]


def test_rds_deployment_logic_is_service_specific() -> None:
    assert _deployment_matches("single_az", "Single-AZ")
    assert _deployment_matches("multi_az", "Multi-AZ")
    assert not _deployment_matches("multi_az", "Multi-AZ DB Cluster")
    assert _deployment_matches("multi_az_cluster", "Multi-AZ DB Cluster")


def test_rds_customer_engine_maps_to_api_identifier() -> None:
    assert _rds_api_engine("PostgreSQL") == "postgres"
    assert _rds_api_engine("Aurora PostgreSQL") == "aurora-postgresql"
    assert _rds_api_engine("sql_server_standard") == "sqlserver-se"
    assert _rds_api_engine("SQL Server Web") == "sqlserver-web"


def test_rds_requested_model_conflict_allows_one_replacement() -> None:
    candidates = [
        {"model": "db.t4g.medium", "vcpu": 2, "memory_gib": 4, "products": [{}]},
        {"model": "db.m7g.large", "vcpu": 2, "memory_gib": 8, "products": [{}]},
    ]
    selected, substituted = _select_rds(
        candidates,
        requested_model="db.t4g.medium",
        min_vcpu=2,
        min_memory=6,
    )
    assert selected["model"] == "db.m7g.large"
    assert substituted is True
