from __future__ import annotations

import os

import pytest

from app.core.config import Settings
from app.integrations.calculator_web import AwsCalculatorWebAutomator, Ec2CalculatorInput

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_CALCULATOR_REGRESSION") != "1",
    reason="set RUN_CALCULATOR_REGRESSION=1 to exercise the live AWS Calculator page",
)


SCENARIOS = [
    Ec2CalculatorInput(
        region="eu-central-1",
        instance_type=None,
        quantity=2,
        requested_vcpu=4,
        requested_memory_gib=16,
        operating_system="linux",
        ebs_gib_per_instance=200,
    ),
    Ec2CalculatorInput(
        region="ap-southeast-2",
        instance_type="t4g.micro",
        quantity=1,
        operating_system="linux",
    ),
    Ec2CalculatorInput(
        region="ap-southeast-2",
        instance_type="c5a.large",
        quantity=1,
        operating_system="windows",
    ),
    Ec2CalculatorInput(
        region="ap-southeast-2",
        instance_type="c5a.large",
        quantity=1,
        operating_system="windows",
        purchase_option="standard_reserved",
        term_years=3,
        payment_option="all_upfront",
    ),
    Ec2CalculatorInput(
        region="ap-southeast-2",
        instance_type="t4g.micro",
        quantity=1,
        ebs_gib_per_instance=100,
        ebs_volume_type="gp3",
        snapshot_frequency="daily",
        snapshot_changed_gib=10,
    ),
    Ec2CalculatorInput(
        region="ap-southeast-2",
        instance_type="t4g.micro",
        quantity=1,
        data_transfer_out_gib=2048,
        detailed_monitoring=True,
    ),
]


@pytest.mark.parametrize("scenario", SCENARIOS)
async def test_live_ec2_calculator_scenarios(scenario: Ec2CalculatorInput) -> None:
    settings = Settings(
        calculator_generate_share_link=False,
        calculator_headless=True,
        calculator_timeout_seconds=120,
    )
    result = await AwsCalculatorWebAutomator(settings).quote_ec2(scenario)
    assert result.monthly_total > 0 or result.upfront_total > 0
    assert result.details
    assert result.selected_instance_type
    if scenario.instance_type is None:
        assert result.selected_instance_type == "t4g.xlarge"


async def test_live_multiple_ec2_groups_share_one_estimate() -> None:
    settings = Settings(
        calculator_generate_share_link=False,
        calculator_headless=True,
        calculator_timeout_seconds=120,
    )
    result = await AwsCalculatorWebAutomator(settings).quote_ec2_groups(
        [
            Ec2CalculatorInput(
                region="ap-southeast-1",
                instance_type="t4g.micro",
                quantity=1,
            ),
            Ec2CalculatorInput(
                region="ap-southeast-2",
                instance_type="t4g.small",
                quantity=1,
            ),
        ]
    )

    assert result.monthly_total > 0
    assert [group.instance_type for group in result.groups] == [
        "t4g.micro",
        "t4g.small",
    ]
    assert len(result.details) >= 2
    assert "第 1 组已保存到同一个 Estimate" in result.steps
