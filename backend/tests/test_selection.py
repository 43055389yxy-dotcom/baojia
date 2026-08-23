import pytest

from app.core.errors import ManualConfirmationRequired
from app.domain.models import QueryAction, ServiceKind, ServiceRequirement
from app.services.plugins.ec2 import (
    Ec2Plugin,
    _pricing_operating_system,
    _pricing_tenancy,
    _rank_reasonable_ec2,
    _select_instance,
)
from app.services.plugins.rds import (
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


def test_generic_rds_ssd_defaults_to_official_gp3_value() -> None:
    values = [
        "General Purpose (SSD)",
        "General Purpose-GP3",
        "Provisioned IOPS (SSD)",
        "Provisioned IOPS-IO2",
    ]

    assert _resolve_volume_type("SSD", values) == "General Purpose-GP3"
    assert _resolve_volume_type("通用型 SSD", values) == "General Purpose-GP3"


def test_aurora_cluster_uses_member_instance_pricing_dimension() -> None:
    assert _billing_deployment(
        "aurora_mysql", {"deployment": "multi_az", "aurora_cluster": True}
    ) == "single_az"
    assert _billing_deployment("mysql", {"deployment": "multi_az"}) == "multi_az"
    requirement = ServiceRequirement(
        service="rds",
        quantity=2,
        requirements={"engine": "aurora_mysql", "cluster_members": 3},
    )
    assert _priced_instance_count(
        requirement, "aurora_mysql", requirement.requirements
    ) == 6
    assert _display_name("aurora_mysql") == "Amazon Aurora MySQL"


def test_aurora_defaults_to_standard_instance_usage_product() -> None:
    standard = {
        "product": {"attributes": {"usagetype": "APS1-InstanceUsage:db.r7g.large"}}
    }
    io_optimized = {
        "product": {
            "attributes": {
                "usagetype": "APS1-InstanceUsageIOOptimized:db.r7g.large"
            }
        }
    }

    assert _preferred_rds_products(
        [io_optimized, standard], "aurora_mysql"
    ) == [standard]
    assert _preferred_rds_products([io_optimized, standard], "mysql") == [
        io_optimized,
        standard,
    ]


def test_explicit_rds_provisioned_iops_is_not_replaced_by_gp3() -> None:
    values = ["General Purpose-GP3", "Provisioned IOPS-IO2"]

    assert _resolve_volume_type("io2", values) == "Provisioned IOPS-IO2"


def test_ec2_user_os_maps_to_price_list_family() -> None:
    assert _pricing_operating_system("Ubuntu 22.04") == "Linux"
    assert _pricing_operating_system("Amazon Linux 2023") == "Linux"
    assert _pricing_operating_system("Windows Server 2022") == "Windows"


def test_ec2_default_tenancy_maps_to_shared_pricing_value() -> None:
    assert _pricing_tenancy(None) == "Shared"
    assert _pricing_tenancy("default") == "Shared"
    assert _pricing_tenancy("shared") == "Shared"
    assert _pricing_tenancy("dedicated") == "Dedicated"


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

    notice = plugin.specified_model_compatibility_notice(
        requirement, "ap-southeast-1"
    )

    assert notice is not None
    assert "ARM" in notice
    assert "不支持 Windows" in notice
    assert "c7i.xlarge" in notice


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
        lambda *_args, **_kwargs: [
            {"model": "cache.r4.large", "memory_gib": 12.3, "vcpu": 2.0}
        ],
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
            return [
                {"model": "cache.t4g.medium", "memory_gib": 3.09, "vcpu": 2.0}
            ]
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


def test_invalid_explicit_redis_model_question_names_original_model(
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

    assert preview.requires_confirmation is True
    assert "cache.r7g.medium" in (preview.confirmation_reason or "")
    assert "当前区域支持的配置" in (preview.confirmation_reason or "")
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


def test_redis_shape_without_exact_official_size_requires_customer_choice(
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

    assert preview.selected_model is None
    assert preview.requires_confirmation is True
    assert all(option.is_default is False for option in preview.candidates)
    assert "客户需要 Redis 每节点约 8G" in (preview.confirmation_reason or "")
    assert "当前区域支持的配置" in (preview.confirmation_reason or "")


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
