from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError

from app.core.errors import ManualConfirmationRequired
from app.integrations.aws import AwsClients
from app.integrations.aws_cache import PersistentAwsCache

logger = logging.getLogger(__name__)

# Explicitly read-only. Adding a service never grants its write APIs automatically.
READ_ONLY_OPERATIONS: dict[str, frozenset[str]] = {
    "ec2": frozenset({"describe_instance_type_offerings", "describe_instance_types"}),
    "rds": frozenset(
        {
            "describe_db_engine_versions",
            "describe_orderable_db_instance_options",
        }
    ),
    "elasticache": frozenset(
        {
            "describe_cache_engine_versions",
            "describe_cache_parameter_groups",
        }
    ),
    "pricing": frozenset(
        {
            "describe_services",
            "get_attribute_values",
            "get_products",
        }
    ),
    "ssm": frozenset({"get_parameter"}),
}


class ReadOnlyAwsQueryExecutor:
    """Execute AI-planned AWS reads behind a strict operation and size boundary."""

    def __init__(self, clients: AwsClients, *, default_max_items: int = 500):
        self._clients = clients
        self._default_max_items = default_max_items
        self._cache = PersistentAwsCache()

    def execute(
        self,
        *,
        service: str,
        operation: str,
        region: str,
        parameters: dict[str, Any] | None = None,
        max_items: int | None = None,
        paginate: bool = True,
    ) -> dict[str, Any]:
        normalized_service = service.strip().lower()
        normalized_operation = operation.strip().lower()
        allowed = READ_ONLY_OPERATIONS.get(normalized_service, frozenset())
        if normalized_operation not in allowed:
            raise ManualConfirmationRequired(
                "系统查询计划包含未授权的 AWS 操作",
                code="aws_query_operation_not_allowed",
                service=normalized_service,
                operation=normalized_operation,
            )

        client = self._client(normalized_service, region)
        request = dict(parameters or {})
        limit = min(max_items or self._default_max_items, 1000)
        cache_key = self._cache.key(
            "aws-read",
            {
                "service": normalized_service,
                "operation": normalized_operation,
                "region": region,
                "parameters": request,
                "limit": limit,
                "paginate": paginate,
            },
        )
        cached = self._cache.get(cache_key)
        if isinstance(cached, dict):
            return cached
        logger.info(
            "Executing allowlisted AWS read service=%s operation=%s region=%s",
            normalized_service,
            normalized_operation,
            region,
        )
        payload: dict[str, Any] | None = None
        last_error: ClientError | BotoCoreError | ParamValidationError | AttributeError | None = None
        # Botocore already retries many transport failures.  Keep this small
        # application-level retry as a second safety net because catalog reads
        # also fail occasionally after a connection-pool or endpoint reset.
        # These are read-only operations, so retrying cannot duplicate a write.
        for attempt in range(3):
            try:
                if paginate and client.can_paginate(normalized_operation):
                    paginator = client.get_paginator(normalized_operation)
                    pages = paginator.paginate(
                        **request,
                        PaginationConfig={"PageSize": min(limit, 100), "MaxItems": limit},
                    )
                    payload = {"pages": [_json_safe(page) for page in pages]}
                else:
                    method = getattr(client, normalized_operation)
                    payload = _json_safe(method(**request))
                break
            except (ClientError, BotoCoreError, ParamValidationError, AttributeError) as exc:
                last_error = exc
                if not _is_retryable_read_error(exc) or attempt == 2:
                    break

        if payload is None:
            # If AWS is temporarily unavailable after the normal TTL, an exact
            # response previously returned by the official API is safer than
            # failing an already-reviewed quote.  This cache never contains a
            # guessed product and remains bound to the full request key.
            stale = self._cache.get(cache_key, allow_stale=True)
            if isinstance(stale, dict):
                logger.warning(
                    "Using stale official AWS read cache after query failure "
                    "service=%s operation=%s region=%s",
                    normalized_service,
                    normalized_operation,
                    region,
                )
                return stale
            assert last_error is not None
            exc = last_error
            aws_error_code = ""
            if isinstance(exc, ClientError):
                aws_error_code = str(
                    exc.response.get("Error", {}).get("Code", "")
                )
            # EC2 returns the very broad AuthFailure code when an otherwise
            # valid account has not opted in to an optional region.  Do not
            # misreport that as an expired access key: verify the region from
            # the always-enabled us-east-1 control plane first.
            if aws_error_code == "AuthFailure" and self._region_not_enabled(region):
                raise ManualConfirmationRequired(
                    f"AWS 账号尚未启用区域 {region}，无法查询或生成该区域报价；请先在 AWS 控制台启用该区域，或改选已启用区域",
                    code="aws_region_not_enabled",
                    service=normalized_service,
                    operation=normalized_operation,
                    region=region,
                    aws_error_code=aws_error_code,
                ) from exc
            if aws_error_code in {
                "AuthFailure",
                "InvalidClientTokenId",
                "ExpiredToken",
                "ExpiredTokenException",
                "UnrecognizedClientException",
                "SignatureDoesNotMatch",
            }:
                raise ManualConfirmationRequired(
                    "后端 AWS 凭证已失效，无法实时查询官方规格；请更新环境变量凭证或使用 IAM Role",
                    code="aws_credentials_invalid",
                    service=normalized_service,
                    operation=normalized_operation,
                    region=region,
                    aws_error_code=aws_error_code,
                ) from exc
            raise ManualConfirmationRequired(
                "AWS 无法执行系统生成的只读查询计划",
                code="aws_query_execution_failed",
                service=normalized_service,
                operation=normalized_operation,
                region=region,
            ) from exc
        assert payload is not None
        logger.info(
            "Completed allowlisted AWS read service=%s operation=%s region=%s",
            normalized_service,
            normalized_operation,
            region,
        )
        self._cache.set(cache_key, payload)
        return payload

    def _region_not_enabled(self, region: str) -> bool:
        if not region or region in {"us-east-1", "global"}:
            return False
        try:
            response = self._clients.regional("ec2", "us-east-1").describe_regions(
                AllRegions=True,
                RegionNames=[region],
            )
        except (ClientError, BotoCoreError, ParamValidationError, AttributeError):
            return False
        regions = response.get("Regions", [])
        return bool(regions) and regions[0].get("OptInStatus") == "not-opted-in"

    def _client(self, service: str, region: str) -> Any:
        if service == "pricing":
            return self._clients.pricing
        if service == "ssm":
            return self._clients.ssm
        return self._clients.regional(service, region)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _is_retryable_read_error(
    error: ClientError | BotoCoreError | ParamValidationError | AttributeError,
) -> bool:
    if isinstance(error, BotoCoreError):
        return True
    if not isinstance(error, ClientError):
        return False
    code = str(error.response.get("Error", {}).get("Code", "")).casefold()
    return code in {
        "internalerror",
        "internalfailure",
        "priorrequestnotcomplete",
        "requestlimitexceeded",
        "requesttimeout",
        "requesttimeoutexception",
        "serviceunavailable",
        "slowdown",
        "throttling",
        "throttlingexception",
        "toomanyrequestsexception",
    }
