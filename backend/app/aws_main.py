"""AstraQuote official multi-cloud pricing API.

The legacy AWS interpretation, selection, confirmation and BCM quote runtime
is deliberately not imported here. GPT makes quote decisions; this process
only exposes signed official-price reads and the sales browser relay.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from typing import Any, Literal
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from app.core.config import get_settings
from app.core.diagnostics import (
    bind_request_id,
    current_request_id,
    diagnostic_log,
    reset_request_id,
)
from app.core.errors import QuoteError
from app.domain.models import ErrorResponse
from app.integrations.aws import AwsClients
from app.services.aws_query_executor import ReadOnlyAwsQueryExecutor
from app.services.gpt_quote_relay import GptQuoteRelayStore, GptRelayError
from app.services.mcp_v2_pricing import (
    AttributeValuesRequest,
    DescribeServiceRequest,
    GetPricesRequest,
    OfficialPricingService,
    ProductSearchRequest,
)
from app.services.quote_artifacts import QuoteArtifactError, QuoteArtifactStore

logger = logging.getLogger(__name__)
settings = get_settings()
diagnostic_log.configure(
    enabled=settings.app_env.strip().lower() not in {"production", "prod"}
)

clients = AwsClients.from_settings(settings)
mcp_v2_pricing = OfficialPricingService(ReadOnlyAwsQueryExecutor(clients))
gpt_quote_relay = GptQuoteRelayStore()
quote_artifacts = QuoteArtifactStore()

app = FastAPI(
    title="AstraQuote 多云报价 API",
    version="3.2.0",
    description="提供官方云价目读取与销售报价任务入口。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.app_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-AstraQuote-MCP-Token"],
)


@app.middleware("http")
async def attach_request_context(request: Request, call_next):
    request_id = request.headers.get("X-Diagnostic-Request-Id") or f"req_{uuid.uuid4().hex}"
    request.state.diagnostic_request_id = request_id
    token = bind_request_id(request_id)
    started_at = time.perf_counter()
    try:
        response = await call_next(request)
        response.headers["X-Diagnostic-Request-Id"] = request_id
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        if request.url.path != "/api/health" and not request.url.path.startswith(
            "/api/debug/logs"
        ):
            diagnostic_log.record(
                "api_request_completed",
                level="error" if response.status_code >= 500 else (
                    "warning" if response.status_code >= 400 else "info"
                ),
                message=f"{request.method} {request.url.path} -> {response.status_code}",
                context={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
                },
                request_id=request_id,
            )
        return response
    finally:
        reset_request_id(token)


@app.exception_handler(QuoteError)
async def quote_error_handler(request: Request, exc: QuoteError) -> JSONResponse:
    request_id = getattr(request.state, "diagnostic_request_id", None) or current_request_id()
    diagnostic_id = diagnostic_log.record_exception(
        "quote_api_error",
        exc,
        level="warning" if exc.http_status < 500 else "error",
        context={
            "method": request.method,
            "path": request.url.path,
            "error_code": exc.code,
            "http_status": exc.http_status,
            "details": exc.details,
        },
        request_id=request_id,
    )
    details = dict(exc.details)
    if diagnostic_id:
        details.update({"diagnostic_id": diagnostic_id, "request_id": request_id})
    payload = ErrorResponse(code=exc.code, message=exc.message, details=details)
    return JSONResponse(status_code=exc.http_status, content=payload.model_dump(mode="json"))


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    if not request.url.path.startswith("/api/mcp/v2/"):
        return await request_validation_exception_handler(request, exc)
    violations = [
        {
            "path": ".".join(str(part) for part in error.get("loc", ())),
            "message": str(error.get("msg") or "Invalid request field."),
            "type": str(error.get("type") or "validation_error"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "code": "request_schema_invalid",
            "message": "AstraQuote 请求字段与官方查价契约不一致，请按字段错误修正后重试。",
            "details": {"violations": violations},
            "retryable": True,
        },
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unexpected AstraQuote V2 API failure", exc_info=exc)
    request_id = getattr(request.state, "diagnostic_request_id", None) or current_request_id()
    diagnostic_id = diagnostic_log.record_exception(
        "unexpected_quote_api_error",
        exc,
        context={"method": request.method, "path": request.url.path},
        request_id=request_id,
    )
    payload = ErrorResponse(
        code="internal_error",
        message="报价服务内部错误，本次未生成猜测结果。",
        details={"diagnostic_id": diagnostic_id, "request_id": request_id}
        if diagnostic_id
        else {},
    )
    return JSONResponse(status_code=500, content=payload.model_dump(mode="json"))


def _require_mcp_internal_token(request: Request) -> None:
    expected = settings.astraquote_mcp_internal_token
    if not expected:
        raise QuoteError(
            "mcp_internal_auth_not_configured",
            "AstraQuote MCP 内部认证尚未配置。",
            {},
            503,
        )
    supplied = request.headers.get("X-AstraQuote-MCP-Token", "")
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise QuoteError("mcp_internal_auth_failed", "AstraQuote MCP 内部认证失败。", {}, 401)


@app.get("/api/debug/logs")
async def get_diagnostic_logs(limit: int = 500, since: str | None = None) -> JSONResponse:
    if not diagnostic_log.enabled:
        return JSONResponse(status_code=404, content={"message": "诊断日志仅在测试环境开放"})
    return JSONResponse(
        content={
            "enabled": True,
            "environment": settings.app_env,
            "provider": "multi-cloud",
            "entries": diagnostic_log.snapshot(limit=limit, since=since),
        }
    )


@app.post("/api/debug/logs/clear", response_model=None)
async def clear_diagnostic_logs() -> dict[str, bool] | JSONResponse:
    if not diagnostic_log.enabled:
        return JSONResponse(status_code=404, content={"message": "诊断日志仅在测试环境开放"})
    diagnostic_log.clear()
    return {"cleared": True}


@app.get("/api/health")
async def health() -> dict[str, Any]:
    relay = gpt_quote_relay.health()
    return {
        "status": "ok",
        "provider": "multi-cloud",
        "workflow": "astraquote-v3",
        "quoteRelay": relay["status"],
    }


@app.get("/api/mcp/v2/health")
async def mcp_v2_health(request: Request) -> dict[str, Any]:
    _require_mcp_internal_token(request)
    return {
        "status": "ready",
        "workflow_version": "3.7.0",
        "internal_ai_enabled": False,
        "role": "official cloud catalog client",
        "price_sources": [
            "AWS Price List API",
            "Azure Retail Prices API",
            "Oracle Cloud Price List API",
            "Google Cloud Billing Catalog API",
            "Tencent Cloud official API",
            "Alibaba Cloud official API",
            "Huawei Cloud official API",
            "Baidu AI Cloud official API",
            "Volcengine official API",
            "CTyun official API",
        ],
        "provider_catalogs": mcp_v2_pricing.catalog_availability(),
    }


@app.post("/api/mcp/v2/describe-service")
async def mcp_v2_describe_service(
    request: Request,
    payload: DescribeServiceRequest,
) -> dict[str, Any]:
    _require_mcp_internal_token(request)
    return await asyncio.to_thread(mcp_v2_pricing.describe_service, payload)


@app.post("/api/mcp/v2/attribute-values")
async def mcp_v2_attribute_values(
    request: Request,
    payload: AttributeValuesRequest,
) -> dict[str, Any]:
    _require_mcp_internal_token(request)
    return await asyncio.to_thread(mcp_v2_pricing.get_attribute_values, payload)


@app.post("/api/mcp/v2/search-products")
async def mcp_v2_search_products(
    request: Request,
    payload: ProductSearchRequest,
) -> dict[str, Any]:
    _require_mcp_internal_token(request)
    return await asyncio.to_thread(mcp_v2_pricing.search_products, payload)


@app.post("/api/mcp/v2/prices")
async def mcp_v2_get_prices(
    request: Request,
    payload: GetPricesRequest,
) -> dict[str, Any]:
    _require_mcp_internal_token(request)
    return await asyncio.to_thread(mcp_v2_pricing.get_prices, payload)


class GptRelayQuoteRequest(BaseModel):
    customer_request: str = Field(min_length=3, max_length=12000)
    cloud_provider: Literal[
        "aws",
        "azure",
        "oci",
        "gcp",
        "tencent",
        "alibaba",
        "huawei",
        "baidu",
        "volcengine",
        "ctyun",
    ] = "aws"
    pricing_scenarios: list[
        Literal["on_demand", "one_year_commitment", "three_year_commitment"]
    ] = Field(default_factory=lambda: ["on_demand"], min_length=1, max_length=3)
    utilization_percent: int = Field(default=100, ge=1, le=100)
    client_request_id: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    )

    @model_validator(mode="after")
    def validate_provider_scenarios(self) -> GptRelayQuoteRequest:
        scenarios = list(dict.fromkeys(self.pricing_scenarios))
        if len(scenarios) != len(self.pricing_scenarios):
            raise ValueError("pricing_scenarios must be unique")
        if self.cloud_provider == "oci" and scenarios != ["on_demand"]:
            raise ValueError("OCI public catalog currently supports on_demand only")
        self.pricing_scenarios = scenarios
        return self


def _gpt_relay_error_response(exc: GptRelayError) -> JSONResponse:
    status = 404 if exc.code == "gpt_relay_job_not_found" else 422
    return JSONResponse(
        status_code=status,
        content={"code": exc.code, "message": str(exc), "details": exc.details},
    )


@app.post("/api/quote-relay/jobs", response_model=None)
async def create_gpt_relay_job(request: GptRelayQuoteRequest) -> dict[str, Any] | JSONResponse:
    try:
        return gpt_quote_relay.create(
            request.customer_request,
            {
                "cloud_provider": request.cloud_provider,
                "pricing_scenarios": request.pricing_scenarios,
                "utilization_percent": request.utilization_percent,
                "display_result_on_page": True,
                "client_request_id": request.client_request_id,
            },
        )
    except GptRelayError as exc:
        return _gpt_relay_error_response(exc)


@app.get("/api/quote-relay/jobs/{job_id}", response_model=None)
async def get_gpt_relay_job(job_id: str) -> dict[str, Any] | JSONResponse:
    try:
        return gpt_quote_relay.public_get(job_id)
    except GptRelayError as exc:
        return _gpt_relay_error_response(exc)


@app.post("/api/quote-relay/jobs/{job_id}/cancel", response_model=None)
async def cancel_gpt_relay_job(job_id: str) -> dict[str, Any] | JSONResponse:
    try:
        return gpt_quote_relay.cancel(job_id)
    except GptRelayError as exc:
        return _gpt_relay_error_response(exc)


@app.get("/api/quote-relay/health")
async def gpt_relay_health() -> dict[str, Any]:
    return {
        **gpt_quote_relay.health(),
        "provider_catalogs": mcp_v2_pricing.catalog_availability(),
    }


@app.get("/api/quote-artifacts/{token}", response_model=None)
async def download_quote_artifact(token: str) -> StreamingResponse | JSONResponse:
    try:
        artifact = quote_artifacts.get(token)
    except QuoteArtifactError as exc:
        status = 410 if exc.code == "quote_artifact_expired" else 404
        return JSONResponse(
            status_code=status,
            content={"code": exc.code, "message": str(exc)},
        )
    try:
        response = await asyncio.to_thread(
            clients.regional("s3", artifact["region"]).get_object,
            Bucket=artifact["bucket"],
            Key=artifact["key"],
        )
        body = response["Body"]
    except Exception:
        logger.exception("Private quote artifact could not be read from S3")
        return JSONResponse(
            status_code=502,
            content={
                "code": "quote_artifact_read_failed",
                "message": "报价文件暂时无法下载，请稍后重试。",
            },
        )

    async def chunks():
        try:
            while True:
                chunk = await asyncio.to_thread(body.read, 64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(body.close)

    filename = str(artifact["filename"]).replace('"', "").replace("\r", "").replace("\n", "")
    return StreamingResponse(
        chunks(),
        media_type=artifact["content_type"],
        headers={
            "Content-Disposition": (
                f"attachment; filename=quote.xlsx; filename*=UTF-8''"
                f"{quote(filename)}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


__all__ = ["app", "gpt_quote_relay", "mcp_v2_pricing", "quote_artifacts", "settings"]
