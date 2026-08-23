from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from app.core.config import Settings
from app.core.errors import ConfigurationError, ManualConfirmationRequired
from app.domain.models import PricedLine, UsageLine
from app.integrations.aws import AwsClients

OWNER_TAG_KEY = "Application"
OWNER_TAG_VALUE = "aws-smart-quote"
BCM_CREATE_MAX_ATTEMPTS = 3
BCM_CREATE_RETRY_DELAYS_SECONDS = (0.5, 1.5)
RETRYABLE_BCM_ERROR_CODES = {
    "InternalFailure",
    "InternalServerError",
    "InternalServerException",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
    "ServiceUnavailableException",
    "Throttling",
    "ThrottlingException",
    "TooManyRequestsException",
}
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BcmQuoteResult:
    priced_lines: list[PricedLine]
    total_cost: float
    currency: str
    rate_type: str
    rate_timestamp: datetime | None
    estimate_id: str


class BcmWorkloadEstimator:
    """Serializes access to a dedicated, reusable BCM Workload Estimate."""

    def __init__(self, clients: AwsClients, settings: Settings):
        self._clients = clients
        self._bcm = clients.bcm
        self._settings = settings
        self._estimate_ids = list(settings.bcm_workload_estimate_ids)
        self._account_id: str | None = None
        self._lock = threading.Lock()

    def quote(self, lines: list[UsageLine]) -> BcmQuoteResult:
        if not lines:
            raise ManualConfirmationRequired("没有可提交给 AWS 的计费行项目")
        if len(lines) > 25:
            raise ManualConfirmationRequired(
                "单次报价超过 BCM Batch API 的 25 行限制",
                code="too_many_usage_lines",
                count=len(lines),
            )

        with self._lock:
            pooled = bool(self._estimate_ids)
            estimate_id = self._ensure_estimate()
            self._verify_owned(estimate_id)
            if pooled:
                self._clear_usage(estimate_id)
            captured: BcmQuoteResult | None = None
            try:
                self._create_usage(estimate_id, lines)
                captured = self._wait_for_result(estimate_id, lines)
            finally:
                # Keep no per-quote history. A configured pool is emptied and
                # reused; an automatically created temporary estimate is deleted.
                if pooled:
                    self._clear_usage(estimate_id)
                else:
                    self._delete_estimate(estimate_id)
            if captured is None:  # pragma: no cover - defensive guard
                raise ManualConfirmationRequired("BCM 未返回报价结果")
            return captured

    def readiness(self) -> dict[str, Any]:
        try:
            preferences = self._bcm.get_preferences()
        except (ClientError, BotoCoreError) as exc:
            return {"ready": False, "reason": str(exc)}
        return {
            "ready": bool(self._estimate_ids or self._settings.bcm_allow_estimate_create),
            "rateTypes": preferences.get("memberAccountRateTypeSelections", []),
            "poolConfigured": bool(self._estimate_ids),
            "autoCreateEnabled": self._settings.bcm_allow_estimate_create,
        }

    def _ensure_estimate(self) -> str:
        if self._estimate_ids:
            return self._estimate_ids[0]
        if not self._settings.bcm_allow_estimate_create:
            raise ConfigurationError(
                "未配置专用 BCM Workload Estimate 池；为防止误改已有 Estimate，报价已停止",
                setting="BCM_WORKLOAD_ESTIMATE_IDS",
            )
        name = f"smartquote-{datetime.now(UTC):%Y%m%d}-{uuid.uuid4().hex[:8]}"
        # Reuse one idempotency token for every attempt. If AWS accepted the
        # first request but the response was lost, retrying cannot create a
        # duplicate estimate.
        client_token = uuid.uuid4().hex
        response: dict[str, Any] | None = None
        for attempt in range(1, BCM_CREATE_MAX_ATTEMPTS + 1):
            try:
                response = self._bcm.create_workload_estimate(
                    name=name,
                    rateType=self._settings.bcm_rate_type,
                    clientToken=client_token,
                    tags={OWNER_TAG_KEY: OWNER_TAG_VALUE},
                )
                break
            except (ClientError, BotoCoreError) as exc:
                retryable = self._is_retryable_create_error(exc)
                if not retryable or attempt >= BCM_CREATE_MAX_ATTEMPTS:
                    error_code = (
                        exc.response.get("Error", {}).get("Code")
                        if isinstance(exc, ClientError)
                        else type(exc).__name__
                    )
                    raise ManualConfirmationRequired(
                        "AWS BCM 无法创建应用专用 Workload Estimate",
                        code="bcm_estimate_create_failed",
                        aws_error_code=error_code,
                        attempts=attempt,
                    ) from exc
                delay = BCM_CREATE_RETRY_DELAYS_SECONDS[attempt - 1]
                logger.warning(
                    "AWS BCM estimate creation failed transiently; retrying "
                    "(%d/%d) after %.1fs",
                    attempt,
                    BCM_CREATE_MAX_ATTEMPTS,
                    delay,
                )
                time.sleep(delay)
        if response is None:  # pragma: no cover - defensive guard
            raise ManualConfirmationRequired(
                "AWS BCM 无法创建应用专用 Workload Estimate",
                code="bcm_estimate_create_failed",
            )
        estimate_id = response.get("id")
        if not estimate_id:
            raise ManualConfirmationRequired(
                "AWS BCM 创建响应缺少 Estimate ID", code="bcm_invalid_response"
            )
        return estimate_id

    @staticmethod
    def _is_retryable_create_error(error: ClientError | BotoCoreError) -> bool:
        if isinstance(error, ClientError):
            code = str(error.response.get("Error", {}).get("Code") or "")
            return code in RETRYABLE_BCM_ERROR_CODES
        return isinstance(
            error,
            (
                ConnectionClosedError,
                ConnectTimeoutError,
                EndpointConnectionError,
                ReadTimeoutError,
            ),
        )

    def _verify_owned(self, estimate_id: str) -> None:
        account_id = self._get_account_id()
        arn = f"arn:aws:bcm-pricing-calculator::{account_id}:workload-estimate/{estimate_id}"
        try:
            response = self._bcm.list_tags_for_resource(arn=arn)
        except (ClientError, BotoCoreError) as exc:
            raise ManualConfirmationRequired(
                "无法验证 BCM Estimate 所有权，禁止修改",
                code="bcm_ownership_check_failed",
                estimate_id=estimate_id,
            ) from exc
        if response.get("tags", {}).get(OWNER_TAG_KEY) != OWNER_TAG_VALUE:
            raise ConfigurationError(
                "配置的 BCM Estimate 不是本应用专用资源，禁止清理或写入",
                estimate_id=estimate_id,
                required_tag=f"{OWNER_TAG_KEY}={OWNER_TAG_VALUE}",
            )

    def _get_account_id(self) -> str:
        if self._account_id:
            return self._account_id
        try:
            sts = self._clients.session.client("sts")
            self._account_id = sts.get_caller_identity()["Account"]
        except (ClientError, BotoCoreError, KeyError) as exc:
            raise ConfigurationError("无法确认当前 AWS 账号，禁止调用 BCM") from exc
        return self._account_id

    def _list_usage(self, estimate_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        try:
            paginator = self._bcm.get_paginator("list_workload_estimate_usage")
            for page in paginator.paginate(workloadEstimateId=estimate_id):
                items.extend(page.get("items", []))
        except (ClientError, BotoCoreError) as exc:
            raise ManualConfirmationRequired(
                "AWS BCM 无法读取估算用量行",
                code="bcm_usage_read_failed",
                estimate_id=estimate_id,
            ) from exc
        return items

    def _clear_usage(self, estimate_id: str) -> None:
        ids = [item["id"] for item in self._list_usage(estimate_id) if item.get("id")]
        for start in range(0, len(ids), 25):
            try:
                response = self._bcm.batch_delete_workload_estimate_usage(
                    workloadEstimateId=estimate_id,
                    ids=ids[start : start + 25],
                )
            except (ClientError, BotoCoreError) as exc:
                raise ManualConfirmationRequired(
                    "AWS BCM 无法清理应用专用估算池",
                    code="bcm_pool_cleanup_failed",
                    estimate_id=estimate_id,
                ) from exc
            if response.get("errors"):
                raise ManualConfirmationRequired(
                    "AWS BCM 返回估算池清理错误",
                    code="bcm_pool_cleanup_failed",
                    errors=response["errors"],
                )

    def _delete_estimate(self, estimate_id: str) -> None:
        try:
            self._bcm.delete_workload_estimate(identifier=estimate_id)
        except (ClientError, BotoCoreError) as exc:
            raise ManualConfirmationRequired(
                "AWS BCM 临时报价清理失败",
                code="bcm_estimate_cleanup_failed",
                estimate_id=estimate_id,
            ) from exc

    def _create_usage(self, estimate_id: str, lines: list[UsageLine]) -> None:
        usage = [
            {
                "serviceCode": line.service_code,
                "usageType": line.usage_type,
                "operation": line.operation,
                "key": line.key,
                "group": line.group or "quote",
                "usageAccountId": self._get_account_id(),
                "amount": line.amount,
            }
            for line in lines
        ]
        try:
            response = self._bcm.batch_create_workload_estimate_usage(
                workloadEstimateId=estimate_id,
                usage=usage,
                clientToken=uuid.uuid4().hex,
            )
        except (ClientError, BotoCoreError) as exc:
            raise ManualConfirmationRequired(
                "AWS BCM 拒绝了计费行，禁止改用本地单价",
                code="bcm_usage_create_failed",
            ) from exc
        if response.get("errors"):
            raise ManualConfirmationRequired(
                "AWS BCM 无法识别一个或多个官方计费维度，需人工确认",
                code="bcm_usage_rejected",
                errors=response["errors"],
            )

    def _wait_for_result(
        self, estimate_id: str, requested_lines: list[UsageLine]
    ) -> BcmQuoteResult:
        deadline = time.monotonic() + self._settings.bcm_poll_timeout_seconds
        last: dict[str, Any] = {}
        items: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            try:
                last = self._bcm.get_workload_estimate(identifier=estimate_id)
            except (ClientError, BotoCoreError) as exc:
                raise ManualConfirmationRequired(
                    "AWS BCM 无法读取最终估算结果", code="bcm_result_read_failed"
                ) from exc
            status = last.get("status")
            if status in {"INVALID", "ACTION_NEEDED"}:
                raise ManualConfirmationRequired(
                    "AWS BCM 未能生成有效报价，需人工确认",
                    code="bcm_estimate_invalid",
                    status=status,
                    failure_message=last.get("failureMessage"),
                )
            if status == "VALID":
                items = self._list_usage(estimate_id)
                if items and all(item.get("status") == "VALID" for item in items):
                    break
            time.sleep(self._settings.bcm_poll_interval_seconds)
        else:
            raise ManualConfirmationRequired(
                "等待 AWS BCM 最终报价超时，未返回猜测价格",
                code="bcm_estimate_timeout",
            )

        priced: list[PricedLine] = []
        for item in items:
            source = _match_source_line(item, requested_lines)
            if source is None or item.get("cost") is None:
                raise ManualConfirmationRequired(
                    "AWS BCM 返回的报价行不完整",
                    code="bcm_incomplete_line_result",
                    key=item.get("key"),
                )
            quantity = item.get("quantity", {})
            priced.append(
                PricedLine(
                    key=source.key,
                    service_code=item["serviceCode"],
                    usage_type=item["usageType"],
                    operation=item["operation"],
                    amount=float(quantity.get("amount", source.amount)),
                    unit=quantity.get("unit"),
                    cost=float(item["cost"]),
                    currency=item.get("currency", "USD"),
                )
            )
        if len(priced) != len(requested_lines) or last.get("totalCost") is None:
            raise ManualConfirmationRequired(
                "AWS BCM 返回的总价或行项目数量不完整",
                code="bcm_incomplete_result",
            )
        return BcmQuoteResult(
            priced_lines=priced,
            total_cost=float(last["totalCost"]),
            currency=last.get("costCurrency", "USD"),
            rate_type=last.get("rateType", self._settings.bcm_rate_type),
            rate_timestamp=last.get("rateTimestamp"),
            estimate_id=estimate_id,
        )


def _match_source_line(item: dict[str, Any], requested_lines: list[UsageLine]) -> UsageLine | None:
    """Match BCM lines even when the optional client key is omitted in the response."""

    if key := item.get("key"):
        keyed = [line for line in requested_lines if line.key == key]
        if len(keyed) == 1:
            return keyed[0]
    candidates = requested_lines
    if group := item.get("group"):
        grouped = [line for line in requested_lines if line.group == group]
        if len(grouped) == 1:
            return grouped[0]
        if grouped:
            # BCM currently omits the caller-provided key from some VALID rows.
            # A group can legitimately contain more than one dimension (for
            # example EC2 instance hours plus data transfer).  Keep matching
            # inside that group so an identical instance dimension in another
            # workload does not make this row look ambiguous.
            candidates = grouped
    dimensioned = [
        line
        for line in candidates
        if line.service_code == item.get("serviceCode")
        and line.usage_type == item.get("usageType")
        and line.operation == item.get("operation")
    ]
    return dimensioned[0] if len(dimensioned) == 1 else None
