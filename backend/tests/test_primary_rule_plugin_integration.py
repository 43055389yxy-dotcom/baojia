"""Frozen catalog fixtures test plugin wiring, not live AWS availability/prices."""

from __future__ import annotations

import pytest

import app.services.plugins.rds as rds_module
import app.services.plugins.redis as redis_module
from app.domain.models import ServiceRequirement
from app.integrations.aws_component_templates.registry import component_template_spec
from app.services.plugins.ec2 import Ec2Plugin
from app.services.plugins.rds import RdsPlugin
from app.services.plugins.redis import RedisPlugin


def product(service, model, region, **attrs):
    return {
        "serviceCode": service,
        "product": {
            "sku": model,
            "attributes": {
                "regionCode": region,
                "instanceType": model,
                "usagetype": f"Usage:{model}",
                "operation": "Run",
                **attrs,
            },
        },
        "terms": {
            "OnDemand": {
                "term": {
                    "priceDimensions": {
                        "hour": {
                            "beginRange": "0",
                            "unit": "Hrs",
                            "pricePerUnit": {"USD": "0.1"},
                        }
                    }
                }
            }
        },
    }


class Catalog:
    def products(self, service, filters, **kwargs):
        return [product(service, "storage", filters.get("regionCode", "us-east-1"))]

    def attribute_values(self, *args):
        return ["General Purpose-GP3"]

    def location(self, region):
        return region


def ec2_plugin(monkeypatch, region, model):
    plugin = Ec2Plugin(None, Catalog())
    monkeypatch.setattr(
        plugin,
        "_official_candidates",
        lambda *a: [
            {
                "model": model,
                "vcpu": 4,
                "memory_gib": 16,
                "current_generation": True,
            }
        ],
    )
    monkeypatch.setattr(
        plugin, "_compute_product", lambda *a, **k: product("AmazonEC2", model, region)
    )
    return plugin


@pytest.mark.parametrize("region", ["ap-northeast-1", "us-east-1"])
@pytest.mark.parametrize("architecture,model", [("arm64", "m7g.xlarge"), ("x86_64", "m7i.xlarge")])
@pytest.mark.parametrize("quantity", [1, 3, 6])
def test_ec2_real_plugin_uses_rules_for_hours_both_disk_roles_and_total_traffic(
    monkeypatch, region, architecture, model, quantity
):
    selected = ec2_plugin(monkeypatch, region, model).select(
        ServiceRequirement(
            service="ec2",
            quantity=quantity,
            hours_per_month=730,
            region=region,
            requirements={
                "requested_model": model,
                "architecture": architecture,
                "system_disk_gib": 100,
                "volume_type": "gp3",
                "additional_ebs_volumes": [{"size_gib": 500, "volume_type": "gp3"}],
                "data_transfer_out_gib": 3072,
            },
        ),
        region,
    )
    assert selected.model == model
    assert [(line.key, line.amount) for line in selected.usage_lines] == [
        ("ec2", quantity * 730),
        ("ebs", quantity * 100),
        ("ebs2", quantity * 500),
        ("ec2out", 3072),
    ]
    for line in selected.usage_lines:
        assert line.calculation
        result = component_template_spec("ec2").replay(line.calculation.model_dump())
        assert float(result.amount) == line.amount


def test_ec2_zero_runtime_does_not_remove_full_month_provisioned_disk(monkeypatch):
    selected = ec2_plugin(monkeypatch, "us-east-1", "m7g.xlarge").select(
        ServiceRequirement(
            service="ec2",
            quantity=2,
            requirements={
                "requested_model": "m7g.xlarge",
                "utilization_percent": 0,
                "system_disk_gib": 100,
            },
        ),
        "us-east-1",
    )
    assert [(line.key, line.amount) for line in selected.usage_lines] == [("ebs", 200)]
    assert "utilization_percent" in selected.applied_requirement_fields


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
@pytest.mark.parametrize("deployment", ["single_az", "multi_az"])
def test_rds_real_plugin_uses_rules_for_hours_and_deployment_storage(
    monkeypatch, engine, deployment
):
    region, model = "ap-northeast-1", "db.m7g.xlarge"
    db_product = product("AmazonRDS", model, region)
    plugin = RdsPlugin(None, Catalog())
    monkeypatch.setattr(plugin, "_orderable_classes", lambda *a, **k: {model})
    monkeypatch.setattr(
        rds_module,
        "_rds_candidates",
        lambda *a: [
            {
                "model": model,
                "vcpu": 4,
                "memory_gib": 16,
                "products": [db_product],
            }
        ],
    )
    selected = plugin.select(
        ServiceRequirement(
            service="rds",
            quantity=2,
            requirements={
                "requested_model": model,
                "engine": engine,
                "deployment": deployment,
                "storage_gib": 800,
                "storage_type": "gp3",
            },
        ),
        region,
    )
    assert [line.amount for line in selected.usage_lines] == [1460, 1600]
    assert [line.calculation.rule_id for line in selected.usage_lines] == [
        "rds.db_instance_hours",
        "rds.database_storage",
    ]


@pytest.mark.parametrize("nodes", [4, 8, 12])
def test_redis_real_plugin_does_not_multiply_explicit_total_twice(monkeypatch, nodes):
    region, model = "ap-northeast-1", "cache.m7g.xlarge"
    cache_product = product("AmazonElastiCache", model, region, cacheEngine="Redis")
    plugin = RedisPlugin(None, Catalog())
    monkeypatch.setattr(
        redis_module,
        "_cache_candidates",
        lambda *a: [
            {
                "model": model,
                "vcpu": 4,
                "memory_gib": 16,
                "products": [cache_product],
            }
        ],
    )
    selected = plugin.select(
        ServiceRequirement(
            service="redis",
            quantity=2,
            requirements={
                "requested_model": model,
                "node_count": nodes,
            },
        ),
        region,
    )
    assert selected.usage_lines[0].amount == nodes * 730
    assert selected.usage_lines[0].calculation.rule_id == "elasticache.cache_node_hours"
