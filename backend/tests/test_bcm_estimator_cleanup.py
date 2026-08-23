from datetime import datetime

import pytest
from botocore.exceptions import ClientError

from app.core.config import Settings
from app.core.errors import ManualConfirmationRequired
from app.domain.models import PricedLine, UsageLine
from app.integrations.aws import AwsClients
from app.services.bcm_estimator import BcmQuoteResult, BcmWorkloadEstimator


class FakeBcm:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def create_workload_estimate(self, **_: object) -> dict[str, str]:
        return {"id": "temporary-estimate"}

    def delete_workload_estimate(self, *, identifier: str) -> None:
        self.deleted.append(identifier)


def _result(estimate_id: str) -> BcmQuoteResult:
    return BcmQuoteResult(
        priced_lines=[
            PricedLine(
                key="line1",
                service_code="AmazonEC2",
                usage_type="BoxUsage:test",
                operation="RunInstances",
                amount=1,
                cost=1,
            )
        ],
        total_cost=1,
        currency="USD",
        rate_type="BEFORE_DISCOUNTS",
        rate_timestamp=datetime.now(),
        estimate_id=estimate_id,
    )


def test_auto_created_workload_estimate_is_deleted_after_quote(monkeypatch) -> None:
    bcm = FakeBcm()
    clients = AwsClients(session=object(), pricing=None, bcm=bcm, ssm=None)  # type: ignore[arg-type]
    estimator = BcmWorkloadEstimator(
        clients,
        Settings(bcm_workload_estimate_ids=[], bcm_allow_estimate_create=True),
    )
    monkeypatch.setattr(estimator, "_verify_owned", lambda _: None)
    monkeypatch.setattr(estimator, "_create_usage", lambda *_: None)
    monkeypatch.setattr(estimator, "_wait_for_result", lambda estimate_id, _: _result(estimate_id))

    result = estimator.quote(
        [
            UsageLine(
                key="line1",
                service_code="AmazonEC2",
                usage_type="BoxUsage:test",
                operation="RunInstances",
                amount=1,
            )
        ]
    )

    assert result.total_cost == 1
    assert bcm.deleted == ["temporary-estimate"]


def test_configured_pool_is_cleared_and_never_deleted(monkeypatch) -> None:
    bcm = FakeBcm()
    clients = AwsClients(session=object(), pricing=None, bcm=bcm, ssm=None)  # type: ignore[arg-type]
    estimator = BcmWorkloadEstimator(
        clients,
        Settings(bcm_workload_estimate_ids=["pool-estimate"]),
    )
    cleared: list[str] = []
    monkeypatch.setattr(estimator, "_verify_owned", lambda _: None)
    monkeypatch.setattr(estimator, "_clear_usage", lambda estimate_id: cleared.append(estimate_id))
    monkeypatch.setattr(estimator, "_create_usage", lambda *_: None)
    monkeypatch.setattr(estimator, "_wait_for_result", lambda estimate_id, _: _result(estimate_id))

    estimator.quote(
        [
            UsageLine(
                key="line1",
                service_code="AmazonEC2",
                usage_type="BoxUsage:test",
                operation="RunInstances",
                amount=1,
            )
        ]
    )

    assert cleared == ["pool-estimate", "pool-estimate"]
    assert bcm.deleted == []


def test_transient_bcm_create_failure_is_retried_with_one_idempotency_token(
    monkeypatch,
) -> None:
    class FlakyBcm(FakeBcm):
        def __init__(self) -> None:
            super().__init__()
            self.requests: list[dict[str, object]] = []

        def create_workload_estimate(self, **kwargs: object) -> dict[str, str]:
            self.requests.append(kwargs)
            if len(self.requests) < 3:
                raise ClientError(
                    {
                        "Error": {
                            "Code": "InternalFailure",
                            "Message": "AWS internal service error",
                        }
                    },
                    "CreateWorkloadEstimate",
                )
            return {"id": "temporary-estimate"}

    bcm = FlakyBcm()
    clients = AwsClients(session=object(), pricing=None, bcm=bcm, ssm=None)  # type: ignore[arg-type]
    estimator = BcmWorkloadEstimator(
        clients,
        Settings(bcm_workload_estimate_ids=[], bcm_allow_estimate_create=True),
    )
    delays: list[float] = []
    monkeypatch.setattr("app.services.bcm_estimator.time.sleep", delays.append)

    estimate_id = estimator._ensure_estimate()

    assert estimate_id == "temporary-estimate"
    assert len(bcm.requests) == 3
    assert delays == [0.5, 1.5]
    assert len({request["clientToken"] for request in bcm.requests}) == 1
    assert len({request["name"] for request in bcm.requests}) == 1


def test_non_transient_bcm_create_failure_is_not_retried(monkeypatch) -> None:
    class DeniedBcm(FakeBcm):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def create_workload_estimate(self, **_: object) -> dict[str, str]:
            self.calls += 1
            raise ClientError(
                {
                    "Error": {
                        "Code": "AccessDeniedException",
                        "Message": "not authorized",
                    }
                },
                "CreateWorkloadEstimate",
            )

    bcm = DeniedBcm()
    clients = AwsClients(session=object(), pricing=None, bcm=bcm, ssm=None)  # type: ignore[arg-type]
    estimator = BcmWorkloadEstimator(
        clients,
        Settings(bcm_workload_estimate_ids=[], bcm_allow_estimate_create=True),
    )
    sleep_calls: list[float] = []
    monkeypatch.setattr("app.services.bcm_estimator.time.sleep", sleep_calls.append)

    with pytest.raises(ManualConfirmationRequired) as captured:
        estimator._ensure_estimate()

    assert captured.value.code == "bcm_estimate_create_failed"
    assert captured.value.details["aws_error_code"] == "AccessDeniedException"
    assert captured.value.details["attempts"] == 1
    assert bcm.calls == 1
    assert sleep_calls == []
