from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.data_paths import AZURE_DATA_ROOT
from app.core.errors import QuoteError
from app.domain.models import (
    ConfigurationFeedbackSubmission,
    ConfirmationSessionResponse,
    ConfirmationSubmission,
    ErrorResponse,
    QuotePreviewResponse,
    QuoteRequest,
    QuoteResponse,
    SalesRegionPreflightRequest,
    SalesRegionPreflightResponse,
    ServiceRequirement,
)
from app.integrations.ai_gateway import AiGateway
from app.integrations.azure_adaptation_audit import AzureAdaptationAudit
from app.integrations.azure_auto_service_discovery import AzureAutoServiceDiscovery
from app.integrations.azure_bulk_cache import AzureBulkRetailCache
from app.integrations.azure_catalog import AzureOfficialCatalog
from app.integrations.azure_intent import AzureIntentParser
from app.integrations.azure_product_registry import AzureProductRegistry
from app.integrations.azure_prompt_library import (
    azure_prompt_library_payload,
    update_azure_prompt_text,
)
from app.integrations.azure_warmup import AzureCatalogWarmer
from app.integrations.component_result_cache import ValidatedComponentResultCache
from app.services.azure_plugins import AZURE_RETAIL_SERVICE_NAMES, AzurePluginRegistry
from app.services.azure_quote_service import AzureQuoteService
from app.services.confirmation_sessions import ConfirmationSessionStore
from app.services.quote_jobs import QuoteJobManager

logger = logging.getLogger(__name__)
settings = get_settings()
bulk_cache = AzureBulkRetailCache(AZURE_DATA_ROOT / "azure_bulk_retail_catalog.sqlite3")
catalog = AzureOfficialCatalog(settings, bulk_cache=bulk_cache)
product_registry = AzureProductRegistry(AZURE_DATA_ROOT / "azure_product_registry.sqlite3")
adaptation_audit = AzureAdaptationAudit(product_registry)
auto_service_discovery = AzureAutoServiceDiscovery(catalog, product_registry)
component_cache = ValidatedComponentResultCache(
    AZURE_DATA_ROOT / "azure_validated_component_results.sqlite3"
)
confirmation_sessions = ConfirmationSessionStore(
    AZURE_DATA_ROOT / "azure_confirmation_sessions.sqlite3",
    "azure",
)
quote_service = AzureQuoteService(
    AzureIntentParser(
        AiGateway(settings),
        component_cache,
        settings.ai_model,
        auto_service_discovery,
    ),
    AzurePluginRegistry(catalog, auto_service_discovery, product_registry),
    confirmation_sessions,
    settings.ai_display_name,
)
quote_jobs = QuoteJobManager(quote_service, "Microsoft Azure", "azure")
catalog_warmer = AzureCatalogWarmer(catalog, product_registry, AZURE_RETAIL_SERVICE_NAMES)

app = FastAPI(
    title="Microsoft Azure 智能报价 API",
    version="1.0.0",
    description="仅处理 Microsoft Azure 报价；AWS 数据与任务不可访问。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.app_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type"],
)


def _require_azure_request(request: QuoteRequest) -> None:
    if request.cloud_provider != "azure":
        raise QuoteError(
            "provider_boundary_violation",
            "Microsoft Azure 报价程序禁止处理 AWS 任务。",
            {"expected": "azure", "received": request.cloud_provider},
            403,
        )


def _require_azure_token(token: str) -> None:
    if not token.startswith("azure_"):
        raise QuoteError(
            "provider_boundary_violation",
            "Microsoft Azure 报价程序禁止读取 AWS 确认链接。",
            {"expected_prefix": "azure_"},
            403,
        )


@app.on_event("startup")
async def start_azure_catalog_maintenance() -> None:
    async def maintain_complete_catalog() -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await bulk_cache.sync()
                await asyncio.to_thread(
                    product_registry.sync_official_services,
                    bulk_cache.services(),
                )
            except Exception:
                logger.exception("Azure complete retail snapshot synchronization failed")
            await asyncio.sleep(6 * 60 * 60)

    async def maintain_profiles() -> None:
        await asyncio.sleep(30)
        while True:
            try:
                await catalog_warmer.warm(refresh_profiles=True)
                await auto_service_discovery.refresh_registered_profiles()
            except Exception:
                logger.exception("Azure official field profile maintenance failed")
            await asyncio.sleep(6 * 60 * 60)

    app.state.bulk_sync_task = asyncio.create_task(maintain_complete_catalog())
    app.state.profile_task = asyncio.create_task(maintain_profiles())


@app.on_event("shutdown")
async def stop_azure_catalog_maintenance() -> None:
    for task_name in ("bulk_sync_task", "profile_task"):
        task = getattr(app.state, task_name, None)
        if task is None:
            continue
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@app.exception_handler(QuoteError)
async def quote_error_handler(_: Request, exc: QuoteError) -> JSONResponse:
    payload = ErrorResponse(code=exc.code, message=exc.message, details=exc.details)
    return JSONResponse(status_code=exc.http_status, content=payload.model_dump(mode="json"))


@app.exception_handler(Exception)
async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unexpected Azure quote API failure", exc_info=exc)
    payload = ErrorResponse(
        code="internal_error",
        message="Microsoft Azure 报价程序内部错误；本次未生成猜测价格。",
    )
    return JSONResponse(status_code=500, content=payload.model_dump(mode="json"))


@app.get("/api/health")
async def health() -> dict[str, Any]:
    bulk_status = bulk_cache.status()
    ready = bool(settings.ai_api_key) and bulk_status.get("state") == "complete"
    return {
        "status": "ok" if ready else "configuration_required",
        "calculatorReady": ready,
        "pricingMode": "azure_retail_prices",
        "aiProvider": settings.ai_display_name,
        "provider": "azure",
        "bulkCatalog": bulk_status,
    }


@app.get("/api/azure-product-registry")
async def get_product_registry(details: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "coverage": product_registry.coverage(),
        "adaptationAudit": adaptation_audit.report(),
        "catalogSource": "Microsoft Azure Retail Prices",
        "componentIsolation": "region-only inheritance",
        "providerBoundary": "azure-only; AWS data access forbidden",
        "bulkCatalog": bulk_cache.status(),
    }
    if details:
        payload["products"] = product_registry.list_products()
    return payload


class PromptUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=50000)


@app.get("/api/prompt-library")
async def get_prompt_library() -> dict[str, object]:
    return azure_prompt_library_payload()


@app.put("/api/prompt-library/{key}")
async def update_prompt_library_item(key: str, request: PromptUpdate) -> dict[str, object]:
    try:
        update_azure_prompt_text(key, request.content)
    except KeyError:
        return JSONResponse(status_code=404, content={"message": "提示词模块不存在"})
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"message": str(exc)})
    return azure_prompt_library_payload()


@app.get("/api/cache/status")
async def cache_status() -> dict[str, object]:
    return {
        "provider": "azure",
        "catalog": {
            **catalog_warmer.status.as_dict(),
            "productRegistry": product_registry.coverage(),
            "bulkCatalog": bulk_cache.status(),
        },
    }


@app.post("/api/quotes/preview", response_model=QuotePreviewResponse)
async def preview_quote(request: QuoteRequest) -> QuotePreviewResponse:
    _require_azure_request(request)
    return await quote_service.preview(request)


@app.post(
    "/api/azure/quotes/region-preflight",
    response_model=SalesRegionPreflightResponse,
)
async def sales_region_preflight(
    request: SalesRegionPreflightRequest,
) -> SalesRegionPreflightResponse:
    result = await quote_service.identify_sales_region(request.customer_request)
    detected = [str(region) for region in result.get("regions", []) if isinstance(region, str)]
    requires_confirmation = bool(result.get("requires_confirmation")) or len(detected) != 1
    options = [
        {"code": str(code), "label": str(label)}
        for code, label in result.get("options", [])
        if str(code).strip() and str(label).strip()
    ]
    return SalesRegionPreflightResponse(
        detected_regions=detected,
        selected_region=detected[0] if len(detected) == 1 else None,
        requires_confirmation=requires_confirmation,
        options=options if requires_confirmation else [],
    )


class AzureConfigurationOptionsRequest(BaseModel):
    service: str = Field(min_length=2, max_length=120, pattern=r"^[a-z0-9_\-]+$")
    region: str = Field(min_length=2, max_length=64)
    requirements: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/azure/configuration-field-options")
async def get_configuration_field_options(
    request: AzureConfigurationOptionsRequest,
) -> dict[str, Any]:
    requirement = ServiceRequirement(
        service=request.service,
        region=request.region,
        requirements=request.requirements,
    )
    try:
        payload = await asyncio.wait_for(
            quote_service.configuration_field_options(requirement),
            timeout=10,
        )
    except Exception:
        logger.exception("Could not load Azure configuration field options")
        return {"options": {}, "shapes": [], "source": "unavailable"}
    return payload if isinstance(payload, dict) else {"options": {}, "shapes": []}


@app.get(
    "/api/confirmation-sessions/{token}",
    response_model=ConfirmationSessionResponse,
)
async def get_confirmation_session(token: str) -> ConfirmationSessionResponse | JSONResponse:
    _require_azure_token(token)
    session = confirmation_sessions.get(token)
    if session is None:
        return JSONResponse(status_code=404, content={"message": "确认单不存在或已失效"})
    if session.status == "pending":
        polished = quote_service.professionalize_confirmation_session(session)
        if polished.confirmation_items != session.confirmation_items:
            confirmation_sessions.replace_pending_confirmation_items(
                token,
                polished.confirmation_items,
            )
        session = polished
    reprocess_request = confirmation_sessions.begin_configuration_reprocessing(token)
    if reprocess_request is not None:
        quote_jobs.start_preview(reprocess_request)
        session = confirmation_sessions.get(token) or session
    return session


@app.post(
    "/api/confirmation-sessions/{token}",
    response_model=ConfirmationSessionResponse,
)
async def submit_confirmation_session(
    token: str,
    submission: ConfirmationSubmission,
) -> ConfirmationSessionResponse | JSONResponse:
    _require_azure_token(token)
    try:
        session = confirmation_sessions.submit(token, submission.answers)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"message": str(exc)})
    if session is None:
        return JSONResponse(status_code=404, content={"message": "确认单不存在或已失效"})
    reprocess_request = confirmation_sessions.begin_configuration_reprocessing(token)
    if reprocess_request is not None:
        quote_jobs.start_preview(reprocess_request)
        session = confirmation_sessions.get(token) or session
    return session


@app.post(
    "/api/confirmation-sessions/{token}/approve",
    response_model=ConfirmationSessionResponse,
)
async def approve_confirmation_configuration(
    token: str,
) -> ConfirmationSessionResponse | JSONResponse:
    _require_azure_token(token)
    try:
        session = confirmation_sessions.approve_configuration(token)
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"message": str(exc)})
    if session is None:
        return JSONResponse(status_code=404, content={"message": "确认单不存在或已失效"})
    return session


@app.post(
    "/api/confirmation-sessions/{token}/feedback",
    response_model=ConfirmationSessionResponse,
)
async def submit_configuration_feedback(
    token: str,
    submission: ConfigurationFeedbackSubmission,
) -> ConfirmationSessionResponse | JSONResponse:
    _require_azure_token(token)
    try:
        session = confirmation_sessions.submit_configuration_feedback(
            token,
            feedback=submission.feedback,
            component_feedback=submission.component_feedback,
            component_updates=submission.component_updates,
        )
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"message": str(exc)})
    if session is None:
        return JSONResponse(status_code=404, content={"message": "确认单不存在或已失效"})
    reprocess_request = confirmation_sessions.begin_configuration_reprocessing(token)
    if reprocess_request is not None:
        quote_jobs.start_preview(reprocess_request)
        session = confirmation_sessions.get(token) or session
    return session


@app.post("/api/quotes", response_model=QuoteResponse)
async def create_quote(request: QuoteRequest) -> QuoteResponse:
    _require_azure_request(request)
    return await quote_service.create_quote(request)


@app.post("/api/quote-jobs")
async def start_quote_job(request: QuoteRequest) -> dict[str, str]:
    _require_azure_request(request)
    job = quote_jobs.start(request)
    return {"job_id": job.job_id, "status": job.status}


@app.post("/api/preview-jobs")
async def start_preview_job(request: QuoteRequest) -> dict[str, str]:
    _require_azure_request(request)
    job = quote_jobs.start_preview(request)
    return {"job_id": job.job_id, "status": job.status}


@app.get("/api/quote-jobs/{job_id}")
async def get_quote_job(job_id: str) -> JSONResponse:
    if not job_id.startswith("azure-"):
        raise QuoteError(
            "provider_boundary_violation",
            "Microsoft Azure 报价程序禁止读取 AWS 任务。",
            {"expected_prefix": "azure-"},
            403,
        )
    job = quote_jobs.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"message": "报价任务不存在"})
    return JSONResponse(content=job.public())
