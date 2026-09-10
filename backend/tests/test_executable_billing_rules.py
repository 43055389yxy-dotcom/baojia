from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.integrations.aws_component_templates.registry import (
    component_template_spec,
    validate_component_template_values,
)


@pytest.mark.parametrize(
    "service,field", [("ec2", "system_disk_gib"), ("s3", "storage_gib"), ("elb", "lcu_count")]
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_template_rejects_nonfinite_and_negative_customer_quantities(service, field, value):
    with pytest.raises(ValueError):
        validate_component_template_values(service, {field: value})


def evaluate(service, output, **values):
    template = component_template_spec(service)
    assert template is not None
    return template.evaluate(output, values)


@pytest.mark.parametrize("quantity,hours,percent", [(1, 730, 100), (6, 365, 50), (4, 0.1, 10)])
def test_compute_rule_executes_percent_as_a_ratio(quantity, hours, percent):
    result = evaluate(
        "ec2",
        "instance_hours",
        quantity=quantity,
        hours_per_month=hours,
        utilization_percent=percent,
    )
    assert (
        result.amount == Decimal(str(quantity)) * Decimal(str(hours)) * Decimal(str(percent)) / 100
    )
    assert set(result.inputs) == {"quantity", "hours_per_month", "utilization_percent"}
    assert result.unit == "Hours"
    assert result.rule_id == "ec2.instance_hours"
    assert result.rule_version


def test_missing_optional_utilization_is_disclosed_not_written_back():
    values = {"quantity": 2, "hours_per_month": 730}
    result = evaluate("ec2", "instance_hours", **values)
    assert result.amount == 1460
    assert result.defaults == {"utilization_percent": "100"}
    assert values == {"quantity": 2, "hours_per_month": 730}


@pytest.mark.parametrize("value", [False, "730", float("nan"), float("inf"), -2])
def test_runtime_rejects_invalid_numbers_without_coercing_them(value):
    with pytest.raises(ValueError):
        evaluate("ec2", "instance_hours", quantity=2, hours_per_month=value)


def test_runtime_distinguishes_zero_from_missing():
    zero = evaluate("s3", "write_requests", put_copy_post_list_requests=0)
    missing = evaluate("s3", "write_requests")
    assert zero.amount == 0
    assert zero.inputs == {"put_copy_post_list_requests": "0"}
    assert missing.amount is None
    assert missing.missing_fields == ("put_copy_post_list_requests",)


@pytest.mark.parametrize("quantity,per_unit", [(1, 100), (6, 300), (9, 0.1)])
def test_total_and_per_resource_are_reconciled_not_added(quantity, per_unit):
    total = float(Decimal(str(quantity)) * Decimal(str(per_unit)))
    result = evaluate(
        "ec2",
        "system_ebs_gib_month",
        quantity=quantity,
        system_disk_gib=per_unit,
        total_system_disk_gib=total,
    )
    assert result.amount == Decimal(str(total))
    with pytest.raises(ValueError, match="conflict"):
        evaluate(
            "ec2",
            "system_ebs_gib_month",
            quantity=quantity,
            system_disk_gib=per_unit,
            total_system_disk_gib=total + 1,
        )


def test_total_transfer_is_not_multiplied_again():
    result = evaluate("ec2", "internet_data_transfer_out", quantity=10, data_transfer_out_gib=3072)
    assert result.amount == 3072
    assert (
        evaluate(
            "ec2",
            "internet_data_transfer_out",
            quantity=10,
            data_transfer_out_gib_per_instance=3072,
        ).amount
        == 30720
    )


@pytest.mark.parametrize("engine", ["mysql", "postgresql"])
def test_rds_multi_az_sku_is_not_multiplied_by_standby_count(engine):
    values = dict(
        quantity=1, hours_per_month=730, engine=engine, deployment="multi_az", instance_count=2
    )
    assert evaluate("rds", "db_instance_hours", **values).amount == 730
    assert evaluate("rds", "database_storage", **values, storage_gib=2048).amount == 2048


def test_redis_total_and_topology_are_reconciled():
    values = dict(quantity=2, hours_per_month=730, shards=2, replicas_per_shard=1)
    assert evaluate("redis", "cache_node_hours", **values).amount == 8 * 730
    assert evaluate("redis", "cache_node_hours", **values, node_count=8).amount == 8 * 730
    with pytest.raises(ValueError, match="conflict"):
        evaluate("redis", "cache_node_hours", **values, node_count=4)
    assert (
        evaluate("redis", "cache_node_hours", quantity=2, hours_per_month=730, node_count=4).amount
        == 4 * 730
    )


def test_alb_direct_lcu_and_monthly_traffic_have_different_scope():
    assert (
        evaluate("elb", "lcu_hours_direct", quantity=3, hours_per_month=730, lcu_count=8).amount
        == 17520
    )
    assert (
        evaluate(
            "elb", "lcu_hours_component_total_bytes", quantity=3, processed_bytes_gib=6144
        ).amount
        == 6144
    )
    assert (
        evaluate(
            "elb",
            "lcu_hours_per_load_balancer_bytes",
            quantity=3,
            processed_bytes_gib_per_load_balancer=6144,
        ).amount
        == 18432
    )


def test_alb_metric_expression_uses_max_and_does_not_consume_unused_fields():
    result = evaluate(
        "elb",
        "lcu_hours_calculated_metrics",
        quantity=2,
        hours_per_month=730,
        new_connections_per_second=50,
        active_connections_per_minute=3000,
        processed_bytes_ec2_ip_gib_per_hour=1,
        rule_evaluations_per_second=1000,
    )
    assert result.amount == 2920
    assert "requests_per_second" not in result.inputs


def test_changed_executable_formula_changes_prompt_and_rule_fingerprint():
    from app.domain.billing_rules import number, product

    template = component_template_spec("ec2")
    original = next(item for item in template.billing_outputs if item.key == "instance_hours")
    changed = replace(original, calculation=product(number("quantity"), number("hours_per_month")))
    modified = replace(
        template,
        billing_outputs=tuple(
            changed if item.key == changed.key else item for item in template.billing_outputs
        ),
    )
    values = {"quantity": 2, "hours_per_month": 730, "utilization_percent": 50}
    assert template.evaluate("instance_hours", values).amount == 730
    assert modified.evaluate("instance_hours", values).amount == 1460
    assert template.rule_version != modified.rule_version
    assert template.prompt_contract() != modified.prompt_contract()


def test_unimplemented_meter_is_explicit_and_cannot_be_executed():
    with pytest.raises(ValueError, match="not executable"):
        evaluate("ec2", "ebs_snapshot_storage", snapshot_retention_days=7)


def test_additional_disks_keep_separate_rows_and_no_missing_sizes_are_skipped():
    template = component_template_spec("ec2")
    values = {
        "quantity": 6,
        "additional_ebs_volumes": [
            {"size_gib": 500, "volume_type": "gp3"},
            {"size_gib": 100, "volume_type": "io2", "count_per_instance": 2},
        ],
    }
    assert template.evaluate("additional_ebs_gib_month", values).amount == 4200
    second = template.evaluate("additional_ebs_gib_month", values, item_index=1)
    assert second.amount == 1200
    assert second.inputs["additional_ebs_volumes.1.size_gib"] == "100"
    assert second.item_index == 1
    with pytest.raises(ValueError):
        template.evaluate(
            "additional_ebs_gib_month", {"quantity": 6, "additional_ebs_volumes": [{}]}
        )


def test_unknown_scope_cannot_silently_become_component_total():
    template = component_template_spec("s3")
    with pytest.raises(ValueError, match="scope"):
        template.evaluate(
            "write_requests",
            {"quantity": 3, "put_copy_post_list_requests": 100},
            scopes={"put_copy_post_list_requests": "per_node"},
        )
