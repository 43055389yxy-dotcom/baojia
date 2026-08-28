from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

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
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "events": self.events,
            "result": self.result,
            "error": self.error,
            "updated_at": self.updated_at,
        }


class QuoteJobManager:
    """Small in-memory queue for official API quote jobs."""

    def __init__(
        self,
        quote_service: QuoteService,
        provider_name: str = "AWS",
        provider_key: Literal["aws", "azure"] = "aws",
    ):
        self._quote_service = quote_service
        self._provider_name = provider_name
        self._provider_key = provider_key
        self._jobs: dict[str, QuoteJob] = {}

    def start(self, request: QuoteRequest) -> QuoteJob:
        job = QuoteJob(job_id=f"{self._provider_key}-{uuid.uuid4().hex[:12]}")
        self._jobs[job.job_id] = job
        asyncio.create_task(self._run(job, request))
        return job

    def start_preview(self, request: QuoteRequest) -> QuoteJob:
        """Run configuration parsing/preflight as a pollable live job."""

        job = QuoteJob(job_id=f"{self._provider_key}-{uuid.uuid4().hex[:12]}")
        self._jobs[job.job_id] = job
        asyncio.create_task(self._run_preview(job, request))
        return job

    def get(self, job_id: str) -> QuoteJob | None:
        return self._jobs.get(job_id)

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
            await self._event(job, stage, message)

        try:
            result = await self._quote_service.create_quote(request, report)
            job.result = result.model_dump(mode="json")
            job.status = "completed"
            await self._event(job, "done", f"{self._provider_name} 官方报价已生成")
        except QuoteError as exc:
            job.status = "failed"
            job.error = {
                "status": "manual_confirmation",
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
            await self._event(job, "error", exc.message)
        except Exception as exc:  # pragma: no cover - protected by API-level logging
            logger.exception("Unhandled exception while executing quote job %s", job.job_id)
            job.status = "failed"
            job.error = {
                "status": "manual_confirmation",
                "code": "internal_error",
                "message": "报价执行发生内部错误，本次没有生成价格。",
                "details": {"error_type": type(exc).__name__},
            }
            await self._event(job, "error", "报价执行发生内部错误")

    async def _run_preview(self, job: QuoteJob, request: QuoteRequest) -> None:
        job.status = "running"
        await self._event(job, "queue", "配置核验任务已启动")

        async def report(stage: str, message: str) -> None:
            await self._event(job, stage, message)

        try:
            result = await self._quote_service.preview(request, report)
            job.result = result.model_dump(mode="json")
            job.status = "completed"
            await self._event(job, "done", "全部组件已完成配置核验")
        except QuoteError as exc:
            self._recover_configuration_review(request)
            job.status = "failed"
            job.error = {
                "status": "manual_confirmation",
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
            await self._event(job, "error", exc.message)
        except Exception as exc:  # pragma: no cover - API safety boundary
            logger.exception("Unhandled exception while previewing quote job %s", job.job_id)
            self._recover_configuration_review(request)
            job.status = "failed"
            job.error = {
                "status": "manual_confirmation",
                "code": "internal_error",
                "message": "配置核验发生内部错误，请稍后重试。",
                "details": {"error_type": type(exc).__name__},
            }
            await self._event(job, "error", "配置核验发生内部错误")

    def _recover_configuration_review(self, request: QuoteRequest) -> None:
        recover = getattr(
            self._quote_service, "recover_configuration_review_after_failure", None
        )
        if callable(recover):
            recover(request.draft_id, request.customer_request)

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
        await asyncio.sleep(0)
