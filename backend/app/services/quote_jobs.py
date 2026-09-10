from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from app.core.diagnostics import diagnostic_log
from app.core.errors import QuoteError
from app.domain.models import QuoteRequest
from app.services.quote_service import QuoteService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class QuoteJob:
    job_id: str
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    events: list[dict[str, str]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    cleaned_request: str | None = None
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "events": self.events,
            "result": self.result,
            "error": self.error,
            "cleaned_request": self.cleaned_request,
            "updated_at": self.updated_at,
        }


class QuoteJobManager:
    """Small in-memory queue for official API quote jobs."""

    _CONFIGURATION_HEARTBEAT_INTERVAL_SECONDS = 15.0

    def __init__(
        self,
        quote_service: QuoteService,
        provider_name: str = "AWS",
        provider_key: Literal["aws", "azure"] = "aws",
        preview_timeout_seconds: float = 300.0,
    ):
        self._quote_service = quote_service
        self._provider_name = provider_name
        self._provider_key = provider_key
        self._preview_timeout_seconds = max(float(preview_timeout_seconds), 0.01)
        self.instance_id = f"worker-{uuid.uuid4().hex}"
        self._jobs: dict[str, QuoteJob] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def _start_task(self, job: QuoteJob, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self._tasks[job.job_id] = task

        def forget(completed: asyncio.Task[None]) -> None:
            if self._tasks.get(job.job_id) is completed:
                self._tasks.pop(job.job_id, None)

        task.add_done_callback(forget)

    def start(self, request: QuoteRequest) -> QuoteJob:
        job = QuoteJob(job_id=f"{self._provider_key}-{uuid.uuid4().hex[:12]}")
        self._jobs[job.job_id] = job
        diagnostic_log.record(
            "quote_job_started",
            message=f"{self._provider_name} 正式报价任务已创建",
            context={
                "job_id": job.job_id,
                "provider": self._provider_key,
                "draft_id": request.draft_id,
                "sales_region": request.sales_region,
                "request_length": len(request.customer_request),
            },
        )
        self._start_task(job, self._run(job, request))
        return job

    def start_preview(self, request: QuoteRequest) -> QuoteJob:
        """Run configuration parsing/preflight as a pollable live job."""

        job = QuoteJob(job_id=f"{self._provider_key}-{uuid.uuid4().hex[:12]}")
        self._jobs[job.job_id] = job
        diagnostic_log.record(
            "preview_job_started",
            message=f"{self._provider_name} 配置核验任务已创建",
            context={
                "job_id": job.job_id,
                "provider": self._provider_key,
                "draft_id": request.draft_id,
                "sales_region": request.sales_region,
                "request_length": len(request.customer_request),
            },
        )
        self._start_task(job, self._run_preview(job, request))
        return job

    def get(self, job_id: str) -> QuoteJob | None:
        return self._jobs.get(job_id)

    async def cancel(self, job_id: str) -> bool:
        """Stop one live job before a browser starts a clean quote session."""

        task = self._tasks.get(job_id)
        job = self._jobs.get(job_id)
        if task is None or job is None or task.done():
            return False
        job.status = "failed"
        job.error = {
            "status": "cancelled",
            "code": "quote_session_reset",
            "message": "该任务已由重新报价操作终止。",
            "details": {},
        }
        job.updated_at = datetime.now(UTC).isoformat()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def _run(self, job: QuoteJob, request: QuoteRequest) -> None:
        job.status = "running"
        await self._event(job, "queue", f"已进入报价队列，正在准备 {self._provider_name} 官方接口核价")

        async def report(stage: str, message: str) -> None:
            # Raw prompts, model JSON and full customer payloads belong to the
            # backend audit trail, not the sales progress screen. Component
            # workflow events remain visible and are grouped into isolated
            # live channels by the frontend.
            if stage in {"ai_prompt", "ai_response", "ai_result"}:
                return
            if stage == "input_cleaned":
                job.cleaned_request = message
                await self._event(job, "input_cleaned", "原始输入已清洗，后续仅使用标准化配置")
                return
            await self._event(job, stage, message)

        try:
            result = await self._quote_service.create_quote(request, report)
            job.result = result.model_dump(mode="json")
            job.status = "completed"
            await self._event(job, "done", f"{self._provider_name} 官方报价已生成")
        except QuoteError as exc:
            diagnostic_id = diagnostic_log.record_exception(
                "quote_job_failed",
                exc,
                level="warning" if exc.http_status < 500 else "error",
                context={
                    "job_id": job.job_id,
                    "draft_id": request.draft_id,
                    "error_code": exc.code,
                    "details": exc.details,
                    "events": job.events,
                },
            )
            job.status = "failed"
            job.error = {
                "status": "manual_confirmation",
                "code": exc.code,
                "message": exc.message,
                "details": {
                    **exc.details,
                    **({"diagnostic_id": diagnostic_id} if diagnostic_id else {}),
                },
            }
            await self._event(job, "error", exc.message)
        except Exception as exc:  # pragma: no cover - protected by API-level logging
            logger.exception("Unhandled exception while executing quote job %s", job.job_id)
            diagnostic_id = diagnostic_log.record_exception(
                "quote_job_unhandled_exception",
                exc,
                context={
                    "job_id": job.job_id,
                    "draft_id": request.draft_id,
                    "events": job.events,
                },
            )
            job.status = "failed"
            job.error = {
                "status": "manual_confirmation",
                "code": "internal_error",
                "message": "报价执行发生内部错误，本次没有生成价格。",
                "details": {
                    "error_type": type(exc).__name__,
                    **({"diagnostic_id": diagnostic_id} if diagnostic_id else {}),
                },
            }
            await self._event(job, "error", "报价执行发生内部错误")

    async def _run_preview(self, job: QuoteJob, request: QuoteRequest) -> None:
        job.status = "running"
        self._heartbeat_configuration_reprocessing(request)
        heartbeat_task = asyncio.create_task(
            self._renew_configuration_reprocessing_lease(request)
        )
        await self._event(job, "queue", "配置核验任务已启动")

        async def report(stage: str, message: str) -> None:
            self._heartbeat_configuration_reprocessing(request)
            if stage == "input_cleaned":
                job.cleaned_request = message
                await self._event(job, "input_cleaned", "原始输入已清洗，后续仅使用标准化配置")
                return
            await self._event(job, stage, message)

        try:
            result = await asyncio.wait_for(
                self._quote_service.preview(request, report),
                timeout=self._preview_timeout_seconds,
            )
            job.result = result.model_dump(mode="json")
            job.status = "completed"
            await self._event(job, "done", "全部组件已完成配置核验")
        except TimeoutError:
            self._recover_configuration_review(request)
            job.status = "failed"
            job.error = {
                "status": "manual_confirmation",
                "code": "configuration_processing_timeout",
                "message": "配置处理超过安全时限，已停止本次任务并保留原配置。",
                "details": {},
            }
            await self._event(job, "error", "配置处理超时，原配置已保留")
        except QuoteError as exc:
            diagnostic_id = diagnostic_log.record_exception(
                "preview_job_failed",
                exc,
                level="warning" if exc.http_status < 500 else "error",
                context={
                    "job_id": job.job_id,
                    "draft_id": request.draft_id,
                    "error_code": exc.code,
                    "details": exc.details,
                    "events": job.events,
                },
            )
            self._recover_configuration_review(request)
            job.status = "failed"
            job.error = {
                "status": "manual_confirmation",
                "code": exc.code,
                "message": exc.message,
                "details": {
                    **exc.details,
                    **({"diagnostic_id": diagnostic_id} if diagnostic_id else {}),
                },
            }
            await self._event(job, "error", exc.message)
        except Exception as exc:  # pragma: no cover - API safety boundary
            logger.exception("Unhandled exception while previewing quote job %s", job.job_id)
            diagnostic_id = diagnostic_log.record_exception(
                "preview_job_unhandled_exception",
                exc,
                context={
                    "job_id": job.job_id,
                    "draft_id": request.draft_id,
                    "events": job.events,
                },
            )
            self._recover_configuration_review(request)
            job.status = "failed"
            job.error = {
                "status": "manual_confirmation",
                "code": "internal_error",
                "message": "配置核验发生内部错误，请稍后重试。",
                "details": {
                    "error_type": type(exc).__name__,
                    **({"diagnostic_id": diagnostic_id} if diagnostic_id else {}),
                },
            }
            await self._event(job, "error", "配置核验发生内部错误")
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    def _recover_configuration_review(self, request: QuoteRequest) -> None:
        recover = getattr(
            self._quote_service, "recover_configuration_review_after_failure", None
        )
        if callable(recover):
            recover(request.draft_id, request.customer_request)

    def _heartbeat_configuration_reprocessing(self, request: QuoteRequest) -> None:
        heartbeat = getattr(
            self._quote_service, "heartbeat_configuration_reprocessing", None
        )
        if callable(heartbeat):
            heartbeat(request.draft_id, self.instance_id)

    async def _renew_configuration_reprocessing_lease(
        self, request: QuoteRequest,
    ) -> None:
        """Keep a quiet AI/catalog call distinguishable from a dead worker."""
        while True:
            await asyncio.sleep(self._CONFIGURATION_HEARTBEAT_INTERVAL_SECONDS)
            self._heartbeat_configuration_reprocessing(request)

    async def shutdown(self) -> None:
        """Cancel in-memory jobs and release their persisted processing leases."""
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        release = getattr(self._quote_service, "release_configuration_reprocessing", None)
        if callable(release):
            release(self.instance_id)

    @staticmethod
    async def _event(job: QuoteJob, stage: str, message: str) -> None:
        if job.events:
            previous = job.events[-1]
            if previous.get("stage") == stage and previous.get("message") == message:
                job.updated_at = datetime.now(UTC).isoformat()
                await asyncio.sleep(0)
                return
        job.events.append(
            {
                "stage": stage,
                "message": message,
                "time": datetime.now().strftime("%H:%M:%S"),
            }
        )
        job.updated_at = datetime.now(UTC).isoformat()
        diagnostic_log.record(
            "quote_job_event",
            level="error" if stage == "error" else "info",
            message=message,
            context={
                "job_id": job.job_id,
                "job_status": job.status,
                "stage": stage,
            },
        )
        await asyncio.sleep(0)
