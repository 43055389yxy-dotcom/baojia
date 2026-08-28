from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.data_paths import AWS_DATA_ROOT
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
    ServiceKind,
    ServiceRequirement,
)
from app.integrations.auto_service_discovery import AutoServiceDiscovery
from app.integrations.aws import AwsClients, PricingCatalog, RegionResolver
from app.integrations.aws_adaptation_audit import AwsAdaptationAudit
from app.integrations.aws_product_registry import AwsProductRegistry
from app.integrations.aws_regions import commercial_aws_region_options
from app.integrations.catalog_warmup import CommonCatalogWarmer
from app.integrations.component_result_cache import ValidatedComponentResultCache
from app.integrations.deepseek import DeepSeekIntentParser
from app.integrations.prompt_library import prompt_library_payload, update_prompt_text
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
product_registry = AwsProductRegistry(database_path=AWS_DATA_ROOT / "aws_product_registry.sqlite3")
adaptation_audit = AwsAdaptationAudit(product_registry)
auto_service_discovery = AutoServiceDiscovery(
    catalog,
    database_path=AWS_DATA_ROOT / "auto_service_profiles.sqlite3",
    product_registry=product_registry,
)
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
confirmation_sessions = ConfirmationSessionStore(
    AWS_DATA_ROOT / "aws_confirmation_sessions.sqlite3",
    "aws",
)
quote_service = QuoteService(
    DeepSeekIntentParser(
        settings,
        auto_service_discovery,
        ValidatedComponentResultCache(AWS_DATA_ROOT / "validated_component_results.sqlite3"),
    ),
    plugins,
    estimator,
    None,
    confirmation_sessions,
    settings.ai_display_name,
    GenericOfficialPlugin(clients, catalog, auto_service_discovery),
)
quote_jobs = QuoteJobManager(quote_service, "AWS", "aws")
warmup_clients = AwsClients.from_settings(settings)
warmup_regions = RegionResolver(warmup_clients)
warmup_catalog = PricingCatalog(warmup_clients, warmup_regions)
catalog_warmer = CommonCatalogWarmer(warmup_clients, warmup_catalog)
last_healthy_aws: dict[str, Any] = {}
last_aws_health_probe_at = 0.0
aws_health_probe_lock = asyncio.Lock()
AWS_HEALTH_CACHE_SECONDS = 60.0

app = FastAPI(
    title="AWS 智能报价 API",
    version="1.0.0",
    description="仅处理 AWS 报价；Microsoft Azure 数据与任务不可访问。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.app_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type"],
)


def _require_aws_request(request: QuoteRequest) -> None:
    if request.cloud_provider != "aws":
        raise QuoteError(
            "provider_boundary_violation",
            "AWS 报价程序禁止处理 Microsoft Azure 任务。",
            {"expected": "aws", "received": request.cloud_provider},
            403,
        )


def _require_aws_token(token: str) -> None:
    if not token.startswith("aws_"):
        raise QuoteError(
            "provider_boundary_violation",
            "AWS 报价程序禁止读取 Microsoft Azure 确认链接。",
            {"expected_prefix": "aws_"},
            403,
        )


@app.on_event("startup")
async def start_aws_catalog_maintenance() -> None:
    async def warm_catalogs() -> None:
        await asyncio.sleep(15)
        await asyncio.to_thread(catalog_warmer.warm)

    async def sync_registry() -> None:
        await asyncio.sleep(5)
        try:
            await asyncio.to_thread(product_registry.sync)
            await asyncio.to_thread(product_registry.sync_region_availability)
        except Exception:
            logger.exception("AWS full product registry synchronization failed")

    async def maintain_profiles() -> None:
        await asyncio.sleep(60)
        while True:
            try:
                await asyncio.to_thread(auto_service_discovery.refresh_stale_profiles)
            except Exception:
                logger.exception("AWS official field profile maintenance failed")
            await asyncio.sleep(6 * 60 * 60)

    app.state.warm_task = asyncio.create_task(warm_catalogs())
    app.state.registry_task = asyncio.create_task(sync_registry())
    app.state.profile_task = asyncio.create_task(maintain_profiles())


@app.on_event("shutdown")
async def stop_aws_catalog_maintenance() -> None:
    for task_name in ("warm_task", "registry_task", "profile_task"):
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
    logger.exception("Unexpected AWS quote API failure", exc_info=exc)
    payload = ErrorResponse(
        code="internal_error",
        message="AWS 报价程序内部错误；本次未生成猜测价格。",
    )
    return JSONResponse(status_code=500, content=payload.model_dump(mode="json"))


@app.get("/api/health")
async def health() -> dict[str, Any]:
    global last_aws_health_probe_at

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
                "pricing",
                region_name=settings.aws_pricing_region,
                config=health_config,
            ).describe_services(ServiceCode="AmazonEC2", MaxResults=1)
            preferences = clients.session.client(
                "bcm-pricing-calculator",
                region_name="us-east-1",
                config=health_config,
            ).get_preferences()
        except (BotoCoreError, ClientError):
            logger.warning("AWS health probe timed out or was unavailable")
        result = {
            "awsAccount": identity.get("Account"),
            "awsArn": identity.get("Arn"),
            "pricingCatalog": bool(pricing.get("Services")),
            "bcmReady": bool(preferences)
            and (bool(settings.bcm_workload_estimate_ids) or settings.bcm_allow_estimate_create),
        }
        if result["awsAccount"] and result["pricingCatalog"] and result["bcmReady"]:
            last_healthy_aws.clear()
            last_healthy_aws.update(result)
        return result if result["awsAccount"] else dict(last_healthy_aws)

    now = time.monotonic()
    if last_healthy_aws and now - last_aws_health_probe_at < AWS_HEALTH_CACHE_SECONDS:
        result = dict(last_healthy_aws)
    else:
        # Multiple browser tabs poll this endpoint.  Only one request may run
        # the three external AWS checks; all other requests reuse the latest
        # known result instead of competing with actual quote work.
        async with aws_health_probe_lock:
            now = time.monotonic()
            if last_healthy_aws and now - last_aws_health_probe_at < AWS_HEALTH_CACHE_SECONDS:
                result = dict(last_healthy_aws)
            else:
                try:
                    result = await asyncio.wait_for(asyncio.to_thread(check), timeout=8)
                except TimeoutError:
                    result = dict(last_healthy_aws) or {
                        "awsAccount": None,
                        "awsArn": None,
                        "pricingCatalog": False,
                        "bcmReady": False,
                    }
                finally:
                    last_aws_health_probe_at = time.monotonic()
    ready = bool(settings.ai_api_key) and bool(result.get("bcmReady"))
    return {
        "status": "ok" if ready else "configuration_required",
        "calculatorReady": ready,
        "pricingMode": "bcm_api",
        "aiProvider": settings.ai_display_name,
        "provider": "aws",
        **result,
    }


@app.get("/api/aws-product-registry")
async def get_product_registry(details: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "coverage": product_registry.coverage(),
        "adaptationAudit": adaptation_audit.report(),
        "catalogSource": "AWS Bulk Price List",
        "componentIsolation": "region-only inheritance",
        "providerBoundary": "aws-only; Azure data access forbidden",
    }
    if details:
        payload["products"] = product_registry.list_products()
    return payload


class PromptUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=50000)


@app.get("/api/prompt-library")
async def get_prompt_library() -> dict[str, object]:
    return prompt_library_payload()


@app.put("/api/prompt-library/{key}")
async def update_prompt_library_item(key: str, request: PromptUpdate) -> dict[str, object]:
    try:
        update_prompt_text(key, request.content)
    except KeyError:
        return JSONResponse(status_code=404, content={"message": "提示词模块不存在"})
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"message": str(exc)})
    return prompt_library_payload()


@app.get("/api/cache/status")
async def cache_status() -> dict[str, object]:
    return {"provider": "aws", "catalog": catalog_warmer.status.as_dict()}


@app.post("/api/quotes/preview", response_model=QuotePreviewResponse)
async def preview_quote(request: QuoteRequest) -> QuotePreviewResponse:
    _require_aws_request(request)
    return await quote_service.preview(request)


def _sales_region_options() -> list[dict[str, str]]:
    return [
        {"code": code, "label": label}
        for code, label in commercial_aws_region_options()
    ]


@app.post(
    "/api/quotes/region-preflight",
    response_model=SalesRegionPreflightResponse,
)
async def sales_region_preflight(
    request: SalesRegionPreflightRequest,
) -> SalesRegionPreflightResponse:
    result = await quote_service.identify_sales_region(request.customer_request)
    official_regions = set(DeepSeekIntentParser.official_aws_region_labels())
    detected = [
        str(region)
        for region in result.get("regions", [])
        if isinstance(region, str) and region in official_regions
    ]
    requires_confirmation = bool(result.get("requires_confirmation")) or not detected
    return SalesRegionPreflightResponse(
        detected_regions=detected,
        selected_region=detected[0] if len(detected) == 1 else None,
        requires_confirmation=requires_confirmation,
        options=_sales_region_options() if requires_confirmation else [],
    )


class AwsConfigurationOptionsRequest(BaseModel):
    service: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9_\-]+$")
    region: str = Field(min_length=5, max_length=32)
    requirements: dict[str, Any] = Field(default_factory=dict)


@app.post("/api/aws/configuration-field-options")
async def get_configuration_field_options(
    request: AwsConfigurationOptionsRequest,
) -> dict[str, list[Any]]:
    aliases = {"aurora": "rds", "redis": "elasticache", "valkey": "elasticache"}
    try:
        kind = ServiceKind(aliases.get(request.service, request.service))
    except ValueError:
        return {}
    requirement = ServiceRequirement(
        service=request.service,
        region=request.region,
        requirements=request.requirements,
    )
    provider = getattr(plugins.get(kind), "configuration_field_options", None)
    if not callable(provider):
        return {}
    try:
        options = await asyncio.wait_for(
            asyncio.to_thread(provider, requirement, request.region),
            timeout=8,
        )
    except Exception:
        return {}
    if not isinstance(options, dict):
        return {}
    return {
        str(field): [value for value in values if isinstance(value, (str, int, float, bool))]
        for field, values in options.items()
        if isinstance(field, str) and isinstance(values, list)
    }


@app.get(
    "/api/confirmation-sessions/{token}",
    response_model=ConfirmationSessionResponse,
)
async def get_confirmation_session(token: str) -> ConfirmationSessionResponse | JSONResponse:
    _require_aws_token(token)
    session = confirmation_sessions.get(token)
    if session is None:
        return JSONResponse(status_code=404, content={"message": "确认单不存在或已失效"})
    if session.status == "pending":
        hydrated = await quote_service.hydrate_confirmation_session_choices(session)
        if hydrated.confirmation_items != session.confirmation_items:
            confirmation_sessions.replace_pending_confirmation_items(
                token,
                hydrated.confirmation_items,
            )
        session = hydrated
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
    _require_aws_token(token)
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
    _require_aws_token(token)
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
    _require_aws_token(token)
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
    _require_aws_request(request)
    return await quote_service.create_quote(request)


@app.post("/api/quote-jobs")
async def start_quote_job(request: QuoteRequest) -> dict[str, str]:
    _require_aws_request(request)
    job = quote_jobs.start(request)
    return {"job_id": job.job_id, "status": job.status}


@app.post("/api/preview-jobs")
async def start_preview_job(request: QuoteRequest) -> dict[str, str]:
    _require_aws_request(request)
    job = quote_jobs.start_preview(request)
    return {"job_id": job.job_id, "status": job.status}


@app.get("/api/quote-jobs/{job_id}")
async def get_quote_job(job_id: str) -> JSONResponse:
    if not job_id.startswith("aws-"):
        raise QuoteError(
            "provider_boundary_violation",
            "AWS 报价程序禁止读取 Microsoft Azure 任务。",
            {"expected_prefix": "aws-"},
            403,
        )
    job = quote_jobs.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"message": "报价任务不存在"})
    return JSONResponse(content=job.public())
