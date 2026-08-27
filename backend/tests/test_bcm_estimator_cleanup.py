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


def test_exhausted_pool_estimate_rotates_to_next_configured_slot(monkeypatch) -> None:
    bcm = FakeBcm()
    clients = AwsClients(session=object(), pricing=None, bcm=bcm, ssm=None)  # type: ignore[arg-type]
    estimator = BcmWorkloadEstimator(
        clients,
        Settings(bcm_workload_estimate_ids=["full-estimate", "ready-estimate"]),
    )
    cleared: list[str] = []
    create_calls: list[str] = []
    monkeypatch.setattr(estimator, "_verify_owned", lambda _: None)
    monkeypatch.setattr(estimator, "_clear_usage", lambda estimate_id: cleared.append(estimate_id))

    def create_usage(estimate_id: str, _: list[UsageLine]) -> None:
        create_calls.append(estimate_id)
        if estimate_id == "full-estimate":
            raise ManualConfirmationRequired(
                "AWS BCM 拒绝了计费行，禁止改用本地单价",
                code="bcm_usage_create_failed",
                aws_error_code="ServiceQuotaExceededException",
                aws_error_message=(
                    "The limit for usage modifications for this workload estimate "
                    "has been exceeded. Create a new estimate to make additional changes."
                ),
            )

    monkeypatch.setattr(estimator, "_create_usage", create_usage)
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

    assert result.estimate_id == "ready-estimate"
    assert create_calls == ["full-estimate", "ready-estimate"]
    assert cleared == [
        "full-estimate",
        "full-estimate",
        "ready-estimate",
        "ready-estimate",
    ]
    assert estimator._exhausted_estimate_ids == {"full-estimate"}


def test_owned_pool_tag_is_verified_once_per_running_estimator() -> None:
    class TaggedBcm(FakeBcm):
        def __init__(self) -> None:
            super().__init__()
            self.tag_reads = 0

        def list_tags_for_resource(self, *, arn: str) -> dict[str, object]:
            self.tag_reads += 1
            return {"tags": {"Application": "aws-smart-quote"}}

    class FakeSts:
        @staticmethod
        def get_caller_identity() -> dict[str, str]:
            return {"Account": "123456789012"}

    class FakeSession:
        @staticmethod
        def client(name: str) -> FakeSts:
            assert name == "sts"
            return FakeSts()

    bcm = TaggedBcm()
    clients = AwsClients(
        session=FakeSession(), pricing=None, bcm=bcm, ssm=None  # type: ignore[arg-type]
    )
    estimator = BcmWorkloadEstimator(
        clients,
        Settings(bcm_workload_estimate_ids=["pool-estimate"]),
    )

    estimator._verify_owned("pool-estimate")
    estimator._verify_owned("pool-estimate")

    assert bcm.tag_reads == 1


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
