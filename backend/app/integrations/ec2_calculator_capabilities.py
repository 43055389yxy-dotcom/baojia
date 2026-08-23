"""Observed EC2 fields in AWS Pricing Calculator.

This is a page capability map, not a customer quote template.  Keep semantic
field names here and keep DOM locators in calculator_selectors.py.
"""

from __future__ import annotations

EC2_CALCULATOR_CAPABILITIES = {
    "schema_version": 1,
    "observed_at": "2026-08-09",
    "source": "https://calculator.aws/#/addService",
    "fields": {
        "description": {"kind": "text", "optional": True},
        "location_type": {
            "kind": "select",
            "values": ["region", "local_zone", "wavelength_zone"],
        },
        "region": {"kind": "select", "depends_on": ["location_type"]},
        "tenancy": {
            "kind": "select",
            "values": ["shared", "dedicated_instance", "dedicated_host"],
        },
        "operating_system": {
            "kind": "select",
            "values": [
                "linux",
                "windows",
                "windows_sql_standard",
                "windows_sql_web",
                "windows_sql_enterprise",
                "rhel",
                "suse",
                "linux_sql_standard",
                "linux_sql_web",
                "linux_sql_enterprise",
                "rhel_ha",
                "rhel_sql_web",
                "rhel_sql_standard",
                "rhel_sql_enterprise",
                "rhel_ha_sql_standard",
                "rhel_ha_sql_enterprise",
                "ubuntu_pro",
            ],
        },
        "workload": {
            "kind": "radio",
            "values": ["constant", "daily_peak", "weekly_peak", "monthly_peak"],
        },
        "quantity": {"kind": "number", "minimum": 1},
        "instance_type": {"kind": "instance_table"},
        "current_generation_only": {"kind": "boolean"},
        "purchase_option": {
            "kind": "radio",
            "values": [
                "compute_savings_plan",
                "ec2_instance_savings_plan",
                "on_demand",
                "spot",
                "standard_reserved",
                "convertible_reserved",
            ],
        },
        "term_years": {
            "kind": "radio",
            "values": [1, 3],
            "depends_on": ["purchase_option"],
        },
        "payment_option": {
            "kind": "radio",
            "values": ["no_upfront", "partial_upfront", "all_upfront"],
            "depends_on": ["purchase_option", "term_years"],
        },
        "utilization_percent": {
            "kind": "number",
            "minimum": 0,
            "maximum": 100,
            "depends_on": ["purchase_option=on_demand"],
        },
        "spot_discount_percent": {
            "kind": "number",
            "minimum": 0,
            "maximum": 100,
            "depends_on": ["purchase_option=spot"],
        },
        "ebs_volume_type": {
            "kind": "select",
            "values": ["gp3", "gp2", "io1", "io2", "st1", "sc1", "magnetic"],
        },
        "ebs_iops": {"kind": "number", "depends_on": ["ebs_volume_type"]},
        "ebs_throughput_mbps": {
            "kind": "number",
            "depends_on": ["ebs_volume_type=gp3"],
        },
        "ebs_storage_gib": {"kind": "number"},
        "additional_ebs_volumes": {
            "kind": "repeatable_intent_field",
            "calculator_mapping": "aggregate when volume type and performance are identical",
            "fields": ["size_gib", "volume_type", "count_per_instance"],
        },
        "snapshot_frequency": {
            "kind": "select",
            "values": [
                "none",
                "hourly",
                "daily",
                "twice_daily",
                "three_times_daily",
                "four_times_daily",
                "six_times_daily",
                "weekly",
                "monthly",
            ],
        },
        "snapshot_changed_gib": {
            "kind": "number",
            "depends_on": ["snapshot_frequency!=none"],
        },
        "detailed_monitoring": {"kind": "boolean"},
        "data_transfer_in": {"kind": "repeatable", "fields": ["from", "amount", "unit"]},
        "data_transfer_regional": {"kind": "amount_with_unit"},
        "data_transfer_out": {"kind": "repeatable", "fields": ["to", "amount", "unit"]},
        "data_transfer_per_instance": {
            "kind": "intent_scope",
            "calculator_mapping": "multiply by EC2 quantity before filling group total",
        },
        "additional_monthly_cost": {"kind": "number", "optional": True},
    },
    "not_page_fields": {
        "snapshot_retention_days": (
            "AWS Calculator EC2 page has no retention-days control; it asks for "
            "snapshot frequency and changed GiB per snapshot."
        )
    },
}
