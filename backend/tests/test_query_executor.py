import pytest
from botocore.exceptions import ClientError

from app.core.errors import ManualConfirmationRequired
from app.services.aws_query_executor import ReadOnlyAwsQueryExecutor


class FakeClient:
    def can_paginate(self, operation: str) -> bool:
        return False

    def describe_instance_types(self, **parameters: object) -> dict[str, object]:
        return {"InstanceTypes": [{"InstanceType": "m7g.large"}], "request": parameters}

    def terminate_instances(self, **parameters: object) -> dict[str, object]:
        raise AssertionError(f"write operation must never run: {parameters}")


class FakeClients:
    pricing = FakeClient()
    ssm = FakeClient()

    def regional(self, service: str, region: str) -> FakeClient:
        assert service == "ec2"
        assert region == "ap-southeast-1"
        return FakeClient()


class InvalidCredentialClient(FakeClient):
    def describe_instance_types(self, **parameters: object) -> dict[str, object]:
        raise ClientError(
            {"Error": {"Code": "InvalidClientTokenId", "Message": "invalid"}},
            "DescribeInstanceTypes",
        )


class InvalidCredentialClients(FakeClients):
    def regional(self, service: str, region: str) -> FakeClient:
        return InvalidCredentialClient()


class RegionNotEnabledClient(InvalidCredentialClient):
    def describe_instance_types(self, **parameters: object) -> dict[str, object]:
        raise ClientError(
            {"Error": {"Code": "AuthFailure", "Message": "not opted in"}},
            "DescribeInstanceTypes",
        )

    def describe_regions(self, **parameters: object) -> dict[str, object]:
        return {
            "Regions": [
                {"RegionName": "ap-southeast-3", "OptInStatus": "not-opted-in"}
            ]
        }


class RegionNotEnabledClients(FakeClients):
    def regional(self, service: str, region: str) -> FakeClient:
        return RegionNotEnabledClient()


def test_executor_runs_allowlisted_read() -> None:
    executor = ReadOnlyAwsQueryExecutor(FakeClients())  # type: ignore[arg-type]
    result = executor.execute(
        service="ec2",
        operation="describe_instance_types",
        region="ap-southeast-1",
        parameters={"InstanceTypes": ["m7g.large"]},
    )
    assert result["InstanceTypes"][0]["InstanceType"] == "m7g.large"


def test_executor_rejects_write_before_client_call() -> None:
    executor = ReadOnlyAwsQueryExecutor(FakeClients())  # type: ignore[arg-type]
    with pytest.raises(ManualConfirmationRequired) as error:
        executor.execute(
            service="ec2",
            operation="terminate_instances",
            region="ap-southeast-1",
            parameters={"InstanceIds": ["i-example"]},
        )
    assert error.value.code == "aws_query_operation_not_allowed"


def test_executor_reports_invalid_credentials_separately() -> None:
    executor = ReadOnlyAwsQueryExecutor(InvalidCredentialClients())  # type: ignore[arg-type]
    with pytest.raises(ManualConfirmationRequired) as error:
        executor.execute(
            service="ec2",
            operation="describe_instance_types",
            region="ap-southeast-3",
            parameters={"InstanceTypes": ["t3.xlarge"]},
        )

    assert error.value.code == "aws_credentials_invalid"
    assert error.value.details["aws_error_code"] == "InvalidClientTokenId"


def test_executor_reports_region_opt_in_separately_from_credentials() -> None:
    executor = ReadOnlyAwsQueryExecutor(RegionNotEnabledClients())  # type: ignore[arg-type]
    with pytest.raises(ManualConfirmationRequired) as error:
        executor.execute(
            service="ec2",
            operation="describe_instance_types",
            region="ap-southeast-3",
            parameters={"InstanceTypes": ["t3.xlarge"]},
        )

    assert error.value.code == "aws_region_not_enabled"
    assert error.value.details["region"] == "ap-southeast-3"
