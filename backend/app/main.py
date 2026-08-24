from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.errors import QuoteError
from app.domain.models import (
    ConfigurationFeedbackSubmission,
    ConfirmationSessionResponse,
    ConfirmationSubmission,
    ErrorResponse,
    QuotePreviewResponse,
    QuoteRequest,
    QuoteResponse,
)
from app.integrations.ai_gateway import AiGateway
from app.integrations.auto_service_discovery import AutoServiceDiscovery
from app.integrations.aws import AwsClients, PricingCatalog, RegionResolver
from app.integrations.azure_auto_service_discovery import AzureAutoServiceDiscovery
from app.integrations.azure_catalog import AzureOfficialCatalog
from app.integrations.azure_intent import AzureIntentParser
from app.integrations.azure_prompt_library import (
    azure_prompt_library_payload,
    update_azure_prompt_text,
)
from app.integrations.azure_warmup import AzureCatalogWarmer
from app.integrations.catalog_warmup import CommonCatalogWarmer
from app.integrations.component_result_cache import ValidatedComponentResultCache
from app.integrations.deepseek import DeepSeekIntentParser
from app.integrations.ec2_calculator_capabilities import EC2_CALCULATOR_CAPABILITIES
from app.integrations.prompt_library import prompt_library_payload, update_prompt_text
from app.services.azure_plugins import AzurePluginRegistry
from app.services.azure_quote_service import AzureQuoteService
from app.services.bcm_estimator import BcmWorkloadEstimator
from app.services.confirmation_sessions import ConfirmationSessionStore
from app.services.plugins import (
    AlbPlugin,
    ApiGatewayPlugin,
    CloudFrontPlugin,
    CloudWatchPlugin,
    DataTransferPlugin,
    EbsPlugin,
    Ec2Plugin,
    EventBridgeSchedulerPlugin,
    GlobalAcceleratorPlugin,
    MskPlugin,
    NatGatewayPlugin,
    OpenSearchPlugin,
    PluginRegistry,
    RdsPlugin,
    RedisPlugin,
    Route53Plugin,
    S3Plugin,
    SesPlugin,
    SqsPlugin,
    WafPlugin,
)
from app.services.plugins.generic_official import GenericOfficialPlugin
from app.services.quote_jobs import QuoteJobManager
from app.services.quote_service import QuoteService

logger = logging.getLogger(__name__)
settings = get_settings()
clients = AwsClients.from_settings(settings)
regions = RegionResolver(clients)
catalog = PricingCatalog(clients, regions)
auto_service_discovery = AutoServiceDiscovery(catalog)
plugins = PluginRegistry(
    [
        Ec2Plugin(clients, catalog),
        RdsPlugin(clients, catalog),
        RedisPlugin(clients, catalog),
        S3Plugin(clients, catalog),
        AlbPlugin(clients, catalog),
        CloudFrontPlugin(clients, catalog),
        Route53Plugin(clients, catalog),
        WafPlugin(clients, catalog),
        SqsPlugin(clients, catalog),
        SesPlugin(clients, catalog),
        CloudWatchPlugin(clients, catalog),
        EbsPlugin(clients, catalog),
        DataTransferPlugin(clients, catalog),
        GlobalAcceleratorPlugin(clients, catalog),
        MskPlugin(clients, catalog),
        ApiGatewayPlugin(clients, catalog),
        EventBridgeSchedulerPlugin(clients, catalog),
        OpenSearchPlugin(clients, catalog),
        NatGatewayPlugin(clients, catalog),
    ]
)
estimator = BcmWorkloadEstimator(clients, settings)
aws_confirmation_sessions = ConfirmationSessionStore(
    Path(__file__).resolve().parents[1] / ".cache" / "aws_confirmation_sessions.sqlite3",
    "aws",
)
azure_confirmation_sessions = ConfirmationSessionStore(
    Path(__file__).resolve().parents[1] / ".cache" / "azure_confirmation_sessions.sqlite3",
    "azure",
)
quote_service = QuoteService(
    DeepSeekIntentParser(
        settings,
        auto_service_discovery,
        ValidatedComponentResultCache(),
    ),
    plugins,
    estimator,
    None,
    aws_confirmation_sessions,
    settings.ai_display_name,
    GenericOfficialPlugin(clients, catalog, auto_service_discovery),
)
quote_jobs = QuoteJobManager(quote_service, "AWS", "aws")
azure_catalog = AzureOfficialCatalog(settings)
azure_auto_service_discovery = AzureAutoServiceDiscovery(azure_catalog)
azure_component_cache = ValidatedComponentResultCache(
    Path(__file__).resolve().parents[1]
    / ".cache"
    / "azure_validated_component_results.sqlite3"
)
azure_quote_service = AzureQuoteService(
    AzureIntentParser(
        AiGateway(settings),
        azure_component_cache,
        settings.ai_model,
        azure_auto_service_discovery,
    ),
    AzurePluginRegistry(azure_catalog, azure_auto_service_discovery),
    azure_confirmation_sessions,
    settings.ai_display_name,
)
azure_quote_jobs = QuoteJobManager(azure_quote_service, "Microsoft Azure", "azure")
azure_catalog_warmer = AzureCatalogWarmer(azure_catalog)
# Keep background prewarming on separate boto3 clients. This prevents its
# adaptive retry state and HTTP connection pool from delaying foreground quotes.
warmup_clients = AwsClients.from_settings(settings)
warmup_regions = RegionResolver(warmup_clients)
warmup_catalog = PricingCatalog(warmup_clients, warmup_regions)
catalog_warmer = CommonCatalogWarmer(warmup_clients, warmup_catalog)
last_healthy_aws: dict[str, Any] = {}

app = FastAPI(
    title="AWS 智能报价 API",
    version="0.1.0",
    description="系统仅解析需求，AWS 官方 API 决定型号、计费维度与最终价格。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.app_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type"],
)


@app.on_event("startup")
async def warm_common_aws_catalogs() -> None:
    # Warm in the background so the UI is available immediately. Persistent
    # SQLite entries make subsequent restarts and quotations fast as well.
    async def delayed_warmup() -> None:
        # Give the first health check or quote priority over low-priority cache filling.
        await asyncio.sleep(15)
        await asyncio.to_thread(catalog_warmer.warm)

    asyncio.create_task(delayed_warmup())

    async def delayed_azure_warmup() -> None:
        await asyncio.sleep(30)
        await azure_catalog_warmer.warm()

    asyncio.create_task(delayed_azure_warmup())

    async def maintain_official_field_profiles() -> None:
        # Scan periodically, but only refresh rows older than ten days (or
        # failed rows whose shorter retry window has elapsed). Quoting remains
        # available while the read-only metadata refresh runs in the background.
        await asyncio.sleep(60)
        while True:
            try:
                result = await asyncio.to_thread(
                    auto_service_discovery.refresh_stale_profiles
                )
                if result["refreshed"] or result["failed"]:
                    logger.info("Official field profile maintenance: %s", result)
                await azure_catalog_warmer.warm(refresh_profiles=True)
                dynamic_result = await azure_auto_service_discovery.refresh_used_profiles()
                logger.info(
                    "Azure official field profile maintenance: fixed=%s dynamic=%s",
                    azure_catalog_warmer.status.as_dict(),
                    dynamic_result,
                )
            except Exception:
                logger.exception("Official field profile maintenance failed")
            await asyncio.sleep(6 * 60 * 60)

    app.state.official_field_maintenance_task = asyncio.create_task(
        maintain_official_field_profiles()
    )


@app.on_event("shutdown")
async def stop_official_field_profile_maintenance() -> None:
    task = getattr(app.state, "official_field_maintenance_task", None)
    if task is not None:
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
    logger.exception("Unexpected quote API failure", exc_info=exc)
    payload = ErrorResponse(
        code="internal_error",
        message="系统内部错误；未生成或返回任何猜测价格，请人工确认。",
    )
    return JSONResponse(status_code=500, content=payload.model_dump(mode="json"))


@app.get("/api/health")
async def health() -> dict[str, Any]:
    def check() -> dict[str, Any]:
        health_config = Config(
            connect_timeout=2,
            read_timeout=4,
            retries={"max_attempts": 1, "mode": "standard"},
        )
        identity: dict[str, Any] = {}
        pricing: dict[str, Any] = {}
        preferences: dict[str, Any] = {}
        try:
            identity = clients.session.client("sts", config=health_config).get_caller_identity()
            pricing = clients.session.client(
                "pricing", region_name=settings.aws_pricing_region, config=health_config
            ).describe_services(ServiceCode="AmazonEC2", MaxResults=1)
            preferences = clients.session.client(
                "bcm-pricing-calculator", region_name="us-east-1", config=health_config
            ).get_preferences()
        except (BotoCoreError, ClientError):
            logger.warning("AWS health probe timed out or was unavailable")
        result = {
            "awsAccount": identity.get("Account"),
            "awsArn": identity.get("Arn"),
            "pricingCatalog": bool(pricing.get("Services")),
            "bcmReady": bool(preferences) and (
                bool(settings.bcm_workload_estimate_ids) or settings.bcm_allow_estimate_create
            ),
        }
        if result["awsAccount"] and result["pricingCatalog"] and result["bcmReady"]:
            last_healthy_aws.clear()
            last_healthy_aws.update(result)
        return result if result["awsAccount"] else dict(last_healthy_aws)

    try:
        result = await asyncio.wait_for(asyncio.to_thread(check), timeout=8)
    except TimeoutError:
        logger.warning("AWS health probe exceeded its hard timeout")
        result = dict(last_healthy_aws) or {
            "awsAccount": None,
            "awsArn": None,
            "pricingCatalog": False,
            "bcmReady": False,
        }
    quote_ready = bool(settings.ai_api_key) and bool(result.get("bcmReady"))
    return {
        "status": "ok" if quote_ready else "configuration_required",
        "calculatorReady": quote_ready,
        "pricingMode": "bcm_api",
        "aiProvider": settings.ai_display_name,
        **result,
    }


@app.get("/api/services")
async def list_services() -> dict[str, Any]:
    return {"services": plugins.list()}


class PromptUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=50000)


@app.get("/api/prompt-library")
async def get_prompt_library(provider: str = "aws") -> dict[str, object]:
    """Expose the exact runtime prompt modules for local developer maintenance."""

    if provider.lower() == "azure":
        return azure_prompt_library_payload()
    return prompt_library_payload()


@app.put("/api/prompt-library/{key}")
async def update_prompt_library_item(key: str, request: PromptUpdate, provider: str = "aws") -> dict[str, object]:
    try:
        if provider.lower() == "azure":
            update_azure_prompt_text(key, request.content)
        else:
            update_prompt_text(key, request.content)
    except KeyError:
        return JSONResponse(status_code=404, content={"message": "提示词模块不存在"})
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"message": str(exc)})
    return azure_prompt_library_payload() if provider.lower() == "azure" else prompt_library_payload()


@app.get("/api/cache/status")
async def cache_status() -> dict[str, object]:
    return {
        "aws": catalog_warmer.status.as_dict(),
        "azure": azure_catalog_warmer.status.as_dict(),
    }


@app.get("/api/capabilities/ec2")
async def ec2_capabilities() -> dict[str, Any]:
    """Return the observed Calculator field schema for maintenance and auditing."""
    return EC2_CALCULATOR_CAPABILITIES


@app.post(
    "/api/quotes/preview",
    response_model=QuotePreviewResponse,
)
async def preview_quote(request: QuoteRequest) -> QuotePreviewResponse:
    if request.cloud_provider == "azure":
        return await azure_quote_service.preview(request)
    return await quote_service.preview(request)


def _confirmation_store_for_token(token: str) -> ConfirmationSessionStore | None:
    if token.startswith("aws_"):
        return aws_confirmation_sessions
    if token.startswith("azure_"):
        return azure_confirmation_sessions
    # Keep an already-open customer link usable after the stores were split.
    # An unprefixed token is accepted only when it has been explicitly placed
    # in exactly one provider store, so it can never cross cloud engines.
    in_aws = aws_confirmation_sessions.get(token) is not None
    in_azure = azure_confirmation_sessions.get(token) is not None
    if in_aws != in_azure:
        return aws_confirmation_sessions if in_aws else azure_confirmation_sessions
    return None


@app.get(
    "/api/confirmation-sessions/{token}",
    response_model=ConfirmationSessionResponse,
)
async def get_confirmation_session(token: str) -> ConfirmationSessionResponse | JSONResponse:
    store = _confirmation_store_for_token(token)
    session = store.get(token) if store is not None else None
    if session is None:
        return JSONResponse(status_code=404, content={"message": "确认单不存在或已失效"})
    return session


@app.post(
    "/api/confirmation-sessions/{token}",
    response_model=ConfirmationSessionResponse,
)
async def submit_confirmation_session(
    token: str, submission: ConfirmationSubmission
) -> ConfirmationSessionResponse | JSONResponse:
    store = _confirmation_store_for_token(token)
    if store is None:
        return JSONResponse(status_code=404, content={"message": "确认单不存在或已失效"})
    try:
        session = store.submit(token, submission.answers)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"message": str(exc)})
    if session is None:
        return JSONResponse(status_code=404, content={"message": "确认单不存在或已失效"})
    return session


@app.post(
    "/api/confirmation-sessions/{token}/approve",
    response_model=ConfirmationSessionResponse,
)
async def approve_confirmation_configuration(
    token: str,
) -> ConfirmationSessionResponse | JSONResponse:
    store = _confirmation_store_for_token(token)
    if store is None:
        return JSONResponse(status_code=404, content={"message": "确认单不存在或已失效"})
    try:
        session = store.approve_configuration(token)
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
    token: str, submission: ConfigurationFeedbackSubmission
) -> ConfirmationSessionResponse | JSONResponse:
    store = _confirmation_store_for_token(token)
    if store is None:
        return JSONResponse(status_code=404, content={"message": "确认单不存在或已失效"})
    try:
        session = store.submit_configuration_feedback(
            token,
            feedback=submission.feedback,
            component_feedback=submission.component_feedback,
            component_updates=submission.component_updates,
        )
    except ValueError as exc:
        return JSONResponse(status_code=409, content={"message": str(exc)})
    if session is None:
        return JSONResponse(status_code=404, content={"message": "确认单不存在或已失效"})
    return session


@app.post(
    "/api/quotes",
    response_model=QuoteResponse,
    responses={422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def create_quote(request: QuoteRequest) -> QuoteResponse:
    if request.cloud_provider == "azure":
        return await azure_quote_service.create_quote(request)
    return await quote_service.create_quote(request)


@app.post("/api/quote-jobs")
async def start_quote_job(request: QuoteRequest) -> dict[str, str]:
    if request.cloud_provider == "azure":
        job = azure_quote_jobs.start(request)
        return {"job_id": job.job_id, "status": job.status}
    job = quote_jobs.start(request)
    return {"job_id": job.job_id, "status": job.status}


@app.post("/api/preview-jobs")
async def start_preview_job(request: QuoteRequest) -> dict[str, str]:
    """Start AWS configuration review with live component progress."""

    job = (
        azure_quote_jobs.start_preview(request)
        if request.cloud_provider == "azure"
        else quote_jobs.start_preview(request)
    )
    return {"job_id": job.job_id, "status": job.status}


@app.get("/api/quote-jobs/{job_id}")
async def get_quote_job(job_id: str) -> JSONResponse:
    job = (
        quote_jobs.get(job_id)
        if job_id.startswith("aws-")
        else azure_quote_jobs.get(job_id)
        if job_id.startswith("azure-")
        else None
    )
    if job is None:
        return JSONResponse(status_code=404, content={"message": "报价任务不存在"})
    return JSONResponse(content=job.public())
