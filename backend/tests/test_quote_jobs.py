from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from app.domain.models import QuoteRequest
from app.services.quote_jobs import QuoteJobManager


class _BlockingQuoteService:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def preview(self, request: QuoteRequest, reporter: Any) -> None:
        del request, reporter
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()


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
