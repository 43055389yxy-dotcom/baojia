from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from app.domain.models import QuotePreviewResponse, QuoteRequest
from app.services.quote_jobs import QuoteJobManager


class _BlockingQuoteService:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.recovered: list[str | None] = []
        self.heartbeats: list[tuple[str | None, str]] = []
        self.released: list[str] = []

    async def preview(self, request: QuoteRequest, reporter: Any) -> None:
        del request, reporter
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()

    def heartbeat_configuration_reprocessing(self, draft_id: str | None, owner_id: str) -> None:
        self.heartbeats.append((draft_id, owner_id))

    def recover_configuration_review_after_failure(self, draft_id: str | None, _request: str) -> None:
        self.recovered.append(draft_id)

    def release_configuration_reprocessing(self, owner_id: str) -> int:
        self.released.append(owner_id)
        return 1


class _CleaningQuoteService(_BlockingQuoteService):
    async def preview(self, request: QuoteRequest, reporter: Any) -> QuotePreviewResponse:
        await reporter("input_cleaned", "Amazon EC2｜数量：2台")
        return QuotePreviewResponse(
            draft_id="awcleaned123",
            customer_summary="已清洗 1 项配置。",
            selections=[],
            cleaned_request="Amazon EC2｜数量：2台",
        )


@pytest.mark.asyncio
async def test_cancel_stops_live_preview_before_a_new_quote_session() -> None:
    service = _BlockingQuoteService()
    manager = QuoteJobManager(cast(Any, service), "AWS", "aws")
    job = manager.start_preview(
        QuoteRequest(cloud_provider="aws", customer_request="1、测试组件")
    )
    await asyncio.wait_for(service.started.wait(), timeout=1)

    assert await manager.cancel(job.job_id) is True
    await asyncio.wait_for(service.cancelled.wait(), timeout=1)
    assert job.status == "failed"
    assert job.error is not None
    assert job.error["code"] == "quote_session_reset"
    assert await manager.cancel(job.job_id) is False


@pytest.mark.asyncio
async def test_job_replaces_raw_request_with_cleaned_request_for_browser_recovery() -> None:
    service = _CleaningQuoteService()
    manager = QuoteJobManager(cast(Any, service), "AWS", "aws")
    job = manager.start_preview(
        QuoteRequest(cloud_provider="aws", customer_request="麻烦报两台服务器")
    )
    for _ in range(20):
        if job.status == "completed":
            break
        await asyncio.sleep(0.01)

    public = job.public()
    assert public["cleaned_request"] == "Amazon EC2｜数量：2台"
    assert "麻烦报两台服务器" not in str(public)


@pytest.mark.asyncio
async def test_preview_timeout_stops_spinner_and_recovers_saved_configuration() -> None:
    service = _BlockingQuoteService()
    manager = QuoteJobManager(cast(Any, service), "AWS", "aws", preview_timeout_seconds=0.04)
    manager._CONFIGURATION_HEARTBEAT_INTERVAL_SECONDS = 0.005
    job = manager.start_preview(QuoteRequest(
        cloud_provider="aws", customer_request="1、测试组件", draft_id="drafttimeout",
    ))
    await asyncio.wait_for(service.started.wait(), timeout=1)
    for _ in range(20):
        if job.status == "failed":
            break
        await asyncio.sleep(0.01)
    assert job.status == "failed"
    assert job.error is not None and job.error["code"] == "configuration_processing_timeout"
    assert service.recovered == ["drafttimeout"]
    assert len(service.heartbeats) >= 2
    assert all(item[0] == "drafttimeout" for item in service.heartbeats)


@pytest.mark.asyncio
async def test_worker_shutdown_releases_processing_lease_for_immediate_resume() -> None:
    service = _BlockingQuoteService()
    manager = QuoteJobManager(cast(Any, service), "AWS", "aws")
    manager.start_preview(
        QuoteRequest(
            cloud_provider="aws",
            customer_request="1、测试组件",
            draft_id="reload-draft",
        )
    )
    await asyncio.wait_for(service.started.wait(), timeout=1)

    await manager.shutdown()

    await asyncio.wait_for(service.cancelled.wait(), timeout=1)
    assert service.released == [manager.instance_id]
