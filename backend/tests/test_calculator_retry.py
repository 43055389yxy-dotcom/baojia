from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from app.core.errors import ManualConfirmationRequired
from app.integrations.calculator_web import (
    AwsCalculatorWebAutomator,
    CalculatorEc2GroupResult,
    CalculatorWebResult,
    Ec2CalculatorInput,
)


@pytest.mark.asyncio
async def test_calculator_retries_entire_temporary_estimate_once() -> None:
    automator = AwsCalculatorWebAutomator(Settings())
    result = CalculatorWebResult(
        monthly_total=10,
        groups=[CalculatorEc2GroupResult("t4g.micro", 2, 1)],
        steps=["报价完成"],
    )
    attempt = AsyncMock(
        side_effect=[
            ManualConfirmationRequired("页面未完成跳转", code="calculator_web_automation_failed"),
            result,
        ]
    )
    automator._quote_ec2_groups_once = attempt  # type: ignore[method-assign]

    actual = await automator.quote_ec2_groups(
        [Ec2CalculatorInput(region="ap-southeast-1", instance_type="t4g.micro", quantity=1)]
    )

    assert attempt.await_count == 2
    assert actual.monthly_total == 10
    assert actual.steps[0] == "Calculator 页面首次未完成跳转，系统已自动安全重试"


@pytest.mark.asyncio
async def test_calculator_stops_after_one_safe_retry() -> None:
    automator = AwsCalculatorWebAutomator(Settings())
    failure = ManualConfirmationRequired("页面未完成跳转", code="calculator_web_automation_failed")
    attempt = AsyncMock(side_effect=[failure, failure])
    automator._quote_ec2_groups_once = attempt  # type: ignore[method-assign]

    with pytest.raises(ManualConfirmationRequired):
        await automator.quote_ec2_groups(
            [Ec2CalculatorInput(region="ap-southeast-1", instance_type="t4g.micro", quantity=1)]
        )

    assert attempt.await_count == 2
