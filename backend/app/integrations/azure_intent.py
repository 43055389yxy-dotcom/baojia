from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable

from pydantic import ValidationError

from app.core.errors import ManualConfirmationRequired
from app.domain.models import ParsedIntent, ServiceRequirement
from app.integrations.ai_gateway import AiGateway
from app.integrations.azure_auto_service_discovery import AzureAutoServiceDiscovery
from app.integrations.azure_prompt_library import (
    AZURE_CORE_PROMPT,
    AZURE_GENERIC_SERVICE_PROMPT,
    AZURE_SERVICE_PROMPTS,
)
from app.integrations.azure_service_templates import (
    azure_component_template,
    azure_requirement_fields,
)
from app.integrations.component_result_cache import ValidatedComponentResultCache

AzureReporter = Callable[[str, str], Awaitable[None]]


AZURE_SERVICE_NAMES: dict[str, str] = {
    "azure_vm": "Azure Virtual Machines",
    "managed_disks": "Azure Managed Disks",
    "azure_sql": "Azure SQL Database",
    "azure_postgresql": "Azure Database for PostgreSQL",
    "azure_mysql": "Azure Database for MySQL",
    "azure_cache": "Azure Managed Redis",
    "blob_storage": "Azure Blob Storage",
    "load_balancer": "Azure Load Balancer",
    "application_gateway": "Azure Application Gateway",
    "front_door": "Azure Front Door",
    "bandwidth": "Azure Bandwidth",
    "aks": "Azure Kubernetes Service",
    "monitor": "Azure Monitor",
    "api_management": "Azure API Management",
}

_SERVICE_ALIASES = {
    "vm": "azure_vm",
    "virtual_machine": "azure_vm",
    "virtual_machines": "azure_vm",
    "azure_virtual_machines": "azure_vm",
    "disk": "managed_disks",
    "managed_disk": "managed_disks",
    "postgresql": "azure_postgresql",
    "azure_database_for_postgresql": "azure_postgresql",
    "mysql": "azure_mysql",
    "azure_database_for_mysql": "azure_mysql",
    "sql_database": "azure_sql",
    "redis": "azure_cache",
    "managed_redis": "azure_cache",
    "azure_cache_for_redis": "azure_cache",
    "azure_redis_cache": "azure_cache",
    "microsoft_cache_redis": "azure_cache",
    "microsoft_cache_redisenterprise": "azure_cache",
    "microsoft_compute_virtualmachines": "azure_vm",
    "microsoft_compute_disks": "managed_disks",
    "microsoft_sql_servers_databases": "azure_sql",
    "microsoft_dbforpostgresql_flexibleservers": "azure_postgresql",
    "microsoft_dbformysql_flexibleservers": "azure_mysql",
    "microsoft_storage_storageaccounts_blobservices": "blob_storage",
    "microsoft_network_loadbalancers": "load_balancer",
    "microsoft_network_applicationgateways": "application_gateway",
    "microsoft_cdn_profiles_afdendpoints": "front_door",
    "microsoft_containerservice_managedclusters": "aks",
    "microsoft_insights_components": "monitor",
    "microsoft_operationalinsights_workspaces": "monitor",
    "microsoft_apimanagement_service": "api_management",
    "storage": "blob_storage",
    "blob": "blob_storage",
    "applicationgateway": "application_gateway",
    "app_gateway": "application_gateway",
    "cdn": "front_door",
    "data_transfer": "bandwidth",
    "kubernetes": "aks",
    "log_analytics": "monitor",
    "apim": "api_management",
}

_NUMBERED_HEADING = re.compile(r"^\s*(\d{1,2})\s*(?:[、，,.．。)）]|[-:]\s+)\s*(\S.*)$")

_AZURE_NUMBERED_IDENTITIES: tuple[tuple[str, str, str], ...] = (
    ("azure_postgresql", "Azure Database for PostgreSQL", r"postgres(?:ql)?"),
    ("azure_mysql", "Azure Database for MySQL", r"\bmysql\b"),
    ("azure_sql", "Azure SQL Database", r"azure\s+sql|sql\s+database"),
    ("azure_cache", "Azure Managed Redis", r"redis|azure\s+cache"),
    (
        "managed_disks",
        "Azure Managed Disks",
        r"managed\s+disks?|托管磁盘|premium\s+ssd|standard\s+ssd",
    ),
    ("blob_storage", "Azure Blob Storage", r"blob(?:\s+storage)?|对象存储"),
    ("application_gateway", "Azure Application Gateway", r"application\s+gateway|应用程序网关"),
    ("front_door", "Azure Front Door", r"front\s+door"),
    ("load_balancer", "Azure Load Balancer", r"load\s+balancer|负载均衡"),
    ("aks", "Azure Kubernetes Service", r"\baks\b|kubernetes"),
    ("api_management", "Azure API Management", r"api\s+management|\bapim\b"),
    ("monitor", "Azure Monitor", r"azure\s+monitor|log\s+analytics|日志监控"),
    ("bandwidth", "Azure Bandwidth", r"bandwidth|公网.*(?:流量|出站)|data\s+transfer"),
    (
        "azure_vm",
        "Azure Virtual Machines",
        r"virtual\s+machines?|azure\s+vm|虚拟机|standard_[a-z0-9_]+",
    ),
)


def azure_numbered_component_identity(text: str) -> tuple[str, str]:
    """Classify a sales-numbered block locally, matching the proven AWS path."""

    folded = text.casefold()
    for service, display_name, pattern in _AZURE_NUMBERED_IDENTITIES:
        if re.search(pattern, folded, re.IGNORECASE):
            return service, display_name
    for line in text.splitlines():
        match = _NUMBERED_HEADING.match(line)
        if match is None:
            continue
        display_name = re.split(r"[，,；;：:]", match.group(2), maxsplit=1)[0].strip()
        service = canonical_azure_service(display_name)
        if service not in {"azure", "microsoft", "microsoft_azure", "azure_service"}:
            return service, display_name
    return "azure_service", "Microsoft Azure"


def azure_region_from_text(text: str) -> str | None:
    folded = text.casefold()
    aliases = (
        ("southeastasia", ("southeastasia", "新加坡", "东南亚")),
        ("eastasia", ("eastasia", "香港", "东亚")),
        ("japaneast", ("japaneast", "东京", "日本东部")),
        ("koreacentral", ("koreacentral", "首尔", "韩国中部")),
        ("eastus", ("eastus", "美国东部")),
        ("westeurope", ("westeurope", "荷兰", "西欧")),
        ("germanywestcentral", ("germanywestcentral", "德国中西部", "法兰克福")),
        ("uksouth", ("uksouth", "伦敦", "英国南部")),
    )
    for code, names in aliases:
        if any(name in folded for name in names):
            return code
    return None


def canonical_azure_service(value: object) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(value or "").casefold()).strip("_")
    return _SERVICE_ALIASES.get(key, key or "azure_service")


def split_numbered_components(text: str) -> list[str]:
    """Treat sales numbering as a hard ownership boundary.

    Text before the first numbered component is workload-wide context and is
    copied into each isolated component. Numbered field rows inside a component
    are not split unless their number advances the top-level sequence.
    """

    lines = text.splitlines()
    headings: list[tuple[int, int]] = []
    expected: int | None = None
    for index, line in enumerate(lines):
        match = _NUMBERED_HEADING.match(line)
        if not match:
            continue
        number = int(match.group(1))
        if expected is None:
            if number != 1:
                continue
            headings.append((index, number))
            expected = 2
        elif number == expected:
            headings.append((index, number))
            expected += 1
    if not headings:
        return []
    prefix = "\n".join(lines[: headings[0][0]]).strip()
    components: list[str] = []
    for position, (start, _) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        block = "\n".join(lines[start:end]).strip()
        components.append("\n".join(part for part in (prefix, block) if part))
    return components


class AzureIntentParser:
    """Two-pass Azure intake: inventory first, then one isolated AI call per component."""

    def __init__(
        self,
        gateway: AiGateway,
        component_cache: ValidatedComponentResultCache | None = None,
        model_name: str = "configured-ai",
        auto_discovery: AzureAutoServiceDiscovery | None = None,
    ):
        self._gateway = gateway
        self._component_cache = component_cache
        self._cache_model_name = f"azure|{model_name}|component-template-v2"
        self._auto_discovery = auto_discovery

    async def parse(self, text: str, reporter: AzureReporter | None = None) -> ParsedIntent:
        numbered = split_numbered_components(text)
        if reporter:
            await reporter("intake_start", "正在识别并拆分 Microsoft Azure 客户需求")
        if numbered:
            components = []
            for source in numbered:
                service, display_name = azure_numbered_component_identity(source)
                components.append(
                    ServiceRequirement(
                        service=service,
                        product_identity=service,
                        calculator_service_name=display_name,
                        region=azure_region_from_text(source),
                        source_text=source,
                    )
                )
            if reporter:
                await reporter("intake_done", "已按销售编号建立独立 Azure 组件任务")
        else:
            components = await self._inventory(text)
            if reporter:
                await reporter("intake_done", "Azure 客户需求初步拆分完成")
        if not components:
            raise ManualConfirmationRequired(
                "未识别到可报价的 Microsoft Azure 组件",
                code="azure_no_services",
            )
        if reporter:
            await reporter(
                "component_plan",
                f"已建立 {len(components)} 项独立配置任务｜启动 {len(components)} 路并行参数解析",
            )

        semaphore = asyncio.Semaphore(max(1, min(len(components), 8)))

        async def parse_one(index: int, component: ServiceRequirement):
            display = component.calculator_service_name or f"Azure 组件 {index + 1}"
            if reporter:
                await reporter(
                    "component_start",
                    f"组件 {index + 1}｜{display}｜正在执行 Azure 参数解析",
                )
            try:
                cached = (
                    await asyncio.to_thread(
                        self._component_cache.get,
                        component,
                        self._cache_model_name,
                    )
                    if self._component_cache is not None
                    else None
                )
                if cached is not None:
                    parsed = cached
                    parsed.service = canonical_azure_service(parsed.service)
                    parsed.product_identity = parsed.service
                    parsed.calculator_service_name = AZURE_SERVICE_NAMES.get(
                        parsed.service,
                        parsed.calculator_service_name or "Microsoft Azure",
                    )
                    parsed.source_text = component.source_text
                    self._reconcile_explicit_sku(parsed)
                else:
                    async with semaphore:
                        parsed = await self._component(
                            component, reporter=reporter, component_index=index
                        )
                    if self._component_cache is not None:
                        await asyncio.to_thread(
                            self._component_cache.put,
                            component,
                            self._cache_model_name,
                            parsed,
                        )
            except Exception:
                # Match the AWS fallback: preserve the deterministic inventory
                # component and let the official rules engine validate it. AI
                # timeout, malformed output or one failed isolated call cannot
                # discard this component or its neighbours.
                parsed = component.model_copy(deep=True)
                parsed.calculator_service_name = (
                    parsed.calculator_service_name or f"Azure 组件 {index + 1}"
                )
                self._reconcile_explicit_sku(parsed)
                if reporter:
                    await reporter(
                        "ai_repair",
                        f"组件 {index + 1}｜{parsed.calculator_service_name}｜"
                        "结构化结果未通过，已转入官方规则引擎复核",
                    )
            if (
                parsed.service not in AZURE_SERVICE_NAMES
                and self._auto_discovery is not None
            ):
                try:
                    profile = await self._auto_discovery.ensure_profile(
                        service_key=parsed.service,
                        display_name=parsed.calculator_service_name
                        or parsed.service,
                        region=parsed.region,
                    )
                    async with semaphore:
                        parsed = await self._component(
                            parsed,
                            reporter=reporter,
                            component_index=index,
                            profile=profile,
                        )
                    if self._component_cache is not None:
                        await asyncio.to_thread(
                            self._component_cache.put,
                            component,
                            self._cache_model_name,
                            parsed,
                        )
                except Exception:
                    # Preserve the component for the generic official adapter.
                    # A discovery or optional second AI pass miss is internal
                    # and never discards customer input.
                    pass
            if reporter:
                parsed_name = parsed.calculator_service_name or parsed.service
                await reporter(
                    "component_done",
                    f"组件 {index + 1}｜{parsed_name}｜参数解析完成",
                )
            return index, parsed

        results = await asyncio.gather(
            *(parse_one(index, component) for index, component in enumerate(components))
        )
        services = self._expand_embedded_charges([component for _, component in sorted(results)])
        regions = {service.region for service in services if service.region}
        ambiguities: list[str] = []
        regional = [service for service in services if service.service not in {"front_door"}]
        if regional and not regions:
            ambiguities.append("这些区域型服务部署在哪个 Azure 区域？")
        summary = self._summary(services)
        return ParsedIntent(customer_summary=summary, services=services, ambiguities=ambiguities)

    async def revise_component_from_feedback(
        self,
        original_text: str,
        component: ServiceRequirement,
        feedback: str,
        reporter: AzureReporter | None = None,
    ) -> ServiceRequirement:
        source = f"客户最新修改：{feedback.strip()}\n客户原始配置：{component.source_text.strip()}"
        if reporter:
            await reporter(
                "component_start",
                f"{component.calculator_service_name or component.service}｜正在重新识别",
            )
        revised = await self._component(
            component.model_copy(deep=True, update={"source_text": source})
        )
        revised.service = component.service
        revised.product_identity = component.product_identity
        revised.calculator_service_name = component.calculator_service_name
        revised.source_text = source
        return revised

    async def _inventory(self, text: str) -> list[ServiceRequirement]:
        prompt = """你只负责把 Microsoft Azure 客户原文按独立产品组件拆开。
不得提取详细参数，不得选择 SKU、Meter 或价格。重复产品但规格、区域或用途不同必须分开。
返回严格 JSON：
{"services":[{"service":"稳定小写标识","display_name":"Azure 官方产品名",
"source_text":"该组件对应的完整客户原话"}]}"""
        raw = await self._gateway.complete_json(
            system_prompt=prompt,
            user_content=text,
            timeout_seconds=45,
            expected_keys=("services",),
            max_attempts=1,
        )
        services = raw.get("services")
        if not isinstance(services, list):
            return []
        result: list[ServiceRequirement] = []
        for item in services:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source_text") or "").strip()
            if not source or source not in text:
                source = text
            service = canonical_azure_service(item.get("service"))
            detected_service, detected_name = azure_numbered_component_identity(source)
            if detected_service != "azure_service":
                service = detected_service
            result.append(
                ServiceRequirement(
                    service=service,
                    product_identity=service,
                    calculator_service_name=AZURE_SERVICE_NAMES.get(
                        service,
                        detected_name
                        if detected_service != "azure_service"
                        else str(item.get("display_name") or "Microsoft Azure"),
                    ),
                    source_text=source,
                )
            )
        return result

    async def _component(
        self,
        component: ServiceRequirement,
        *,
        reporter: AzureReporter | None = None,
        component_index: int = 0,
        profile: dict[str, object] | None = None,
    ) -> ServiceRequirement:
        service_hint = canonical_azure_service(component.service)
        service_rule = AZURE_SERVICE_PROMPTS.get(
            service_hint, AZURE_GENERIC_SERVICE_PROMPT
        )
        generated_prompt = str((profile or {}).get("prompt_text") or "").strip()
        if generated_prompt:
            service_rule = f"{service_rule}\n\n{generated_prompt}"
        extra_fields = tuple(
            str(field)
            for field in (profile or {}).get("fields", [])
            if isinstance(field, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,63}", field)
        )
        template = azure_component_template(component, extra_fields=extra_fields)
        prompt = f"""{AZURE_CORE_PROMPT}

{service_rule}

这是一个完全隔离的单组件识别任务。只能读取当前组件原文，不得增加其他组件。
service 必须是稳定 Azure 标识；不得输出 AWS 字段。客户明确写出的 SKU 原样放入
requirements.requested_sku；没有写 SKU 时不得编造。区域使用 armRegionName。
只能填写固定模板中存在的字段，原文没有的字段保持 null。
返回严格 JSON：
{{"component":{json.dumps(template, ensure_ascii=False)}}}
"""
        previous_raw: dict[str, object] | None = None
        validation_error = ""
        parsed: ServiceRequirement | None = None
        for attempt in range(1, 4):
            user_content = f"当前组件客户原话：\n{component.source_text}"
            if previous_raw is not None:
                user_content += (
                    "\n\n上一次输出未通过程序校验，只修正报错字段，不得改变服务、"
                    "遗漏客户值或增加其他组件。\n"
                    f"上一次输出：\n{json.dumps(previous_raw, ensure_ascii=False)}\n"
                    f"程序校验错误：\n{validation_error}"
                )
            raw = await self._gateway.complete_json(
                system_prompt=prompt,
                user_content=user_content,
                timeout_seconds=35,
                expected_keys=("component",),
                max_attempts=1,
            )
            value = raw.get("component")
            try:
                if not isinstance(value, dict):
                    raise TypeError("component 必须是 JSON 对象")
                value = dict(value)
                service = canonical_azure_service(value.get("service") or service_hint)
                if service_hint != "azure_service" and service != service_hint:
                    raise ValueError("组件服务类型被修改")
                value["service"] = service
                value["product_identity"] = service
                value["calculator_service_name"] = AZURE_SERVICE_NAMES.get(
                    service, str(value.get("calculator_service_name") or "Microsoft Azure")
                )
                value["source_text"] = component.source_text
                value["query_action"] = None
                # AI fixed templates use null for absent customer facts; the
                # execution model uses concrete safe defaults for these fields.
                if value.get("quantity") is None:
                    value["quantity"] = 1
                if value.get("hours_per_month") is None:
                    value["hours_per_month"] = 730
                requirements = value.get("requirements")
                value["requirements"] = (
                    dict(requirements) if isinstance(requirements, dict) else {}
                )
                for legacy in ("requested_sku", "sku_name", "monthly_quantity"):
                    if value.get(legacy) is not None:
                        value["requirements"].setdefault(legacy, value.pop(legacy))
                allowed = set(azure_requirement_fields(service)) | set(extra_fields)
                value["requirements"] = {
                    key: item
                    for key, item in value["requirements"].items()
                    if key in allowed and item is not None and item != ""
                }
                parsed = ServiceRequirement.model_validate(value)
                break
            except (ValidationError, TypeError, ValueError) as exc:
                previous_raw = raw
                validation_error = str(exc)[:1600]
                if reporter:
                    await reporter(
                        "ai_repair",
                        f"组件 {component_index + 1}｜"
                        f"{component.calculator_service_name or component.service}｜"
                        f"参数校验未通过，正在定向修正（第 {attempt}/3 次）",
                    )
        if parsed is None:
            raise ManualConfirmationRequired(
                "Azure 组件结构未通过校验",
                code="azure_component_schema_failed",
                error=validation_error,
            )
        self._reconcile_explicit_sku(parsed)
        parsed.field_sources = {
            "region": "customer_text" if parsed.region else "",
            "quantity": "customer_text"
            if re.search(r"数量|\d+\s*(?:台|套|个|节点)", parsed.source_text)
            else "default",
            **{f"requirements.{key}": "customer_text" for key in parsed.requirements},
        }
        parsed.field_sources = {
            key: source for key, source in parsed.field_sources.items() if source
        }
        parsed.locked_fields = sorted(
            key for key, source in parsed.field_sources.items() if source == "customer_text"
        )
        return parsed

    @staticmethod
    def _reconcile_explicit_sku(component: ServiceRequirement) -> None:
        """Preserve literal Azure SKUs exactly as the AWS literal guards do."""

        source = component.source_text
        pattern = None
        if component.service == "azure_vm":
            pattern = r"\bStandard_[A-Za-z0-9_]+\b"
        elif component.service == "managed_disks":
            pattern = r"\b(?:P|E|S|M)\d{1,2}(?:\s+(?:LRS|ZRS))?\b"
        elif component.service == "azure_cache":
            pattern = r"\b(?:Basic|Standard|Premium|Enterprise)\s+[A-Za-z]?\d+\b"
        if not pattern:
            return
        match = re.search(pattern, source, re.IGNORECASE)
        if not match:
            return
        literal_sku = match.group(0).strip()
        previous = str(component.requirements.get("requested_sku") or "").strip()
        if component.service == "managed_disks" and previous and previous != literal_sku:
            component.requirements.setdefault("disk_type", previous)
        component.requirements["requested_sku"] = literal_sku

    @staticmethod
    def _summary(services: list[ServiceRequirement]) -> str:
        names = [service.calculator_service_name or service.service for service in services]
        return f"已识别 {len(services)} 项 Azure 配置：" + "、".join(names)

    @staticmethod
    def _expand_embedded_charges(
        services: list[ServiceRequirement],
    ) -> list[ServiceRequirement]:
        """Expose separately billed Azure resources as their own components."""

        expanded: list[ServiceRequirement] = []
        for service in services:
            expanded.append(service)
            if service.service != "azure_vm":
                continue
            disk_size = service.requirements.get("system_disk_gib")
            if not isinstance(disk_size, (int, float)):
                continue
            if any(
                candidate.service == "managed_disks"
                and candidate.source_text == service.source_text
                for candidate in services
            ):
                continue
            disk_requirements: dict[str, object] = {"storage_gib": disk_size}
            disk_sku = service.requirements.get("system_disk_sku")
            disk_type = service.requirements.get("system_disk_type")
            if disk_sku:
                disk_requirements["requested_sku"] = disk_sku
            if disk_type:
                disk_requirements["disk_type"] = disk_type
            expanded.append(
                ServiceRequirement(
                    service="managed_disks",
                    calculator_service_name="Azure Managed Disks",
                    product_identity="managed_disks",
                    region=service.region,
                    quantity=service.quantity,
                    hours_per_month=service.hours_per_month,
                    requirements=disk_requirements,
                    source_text=service.source_text,
                    field_sources={
                        f"requirements.{key}": "derived_customer_component"
                        for key in disk_requirements
                    },
                )
            )
        return expanded
