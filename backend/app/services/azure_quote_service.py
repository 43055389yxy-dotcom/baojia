from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.errors import QuoteError
from app.domain.models import (
    ExecutionEvent, PreviewSelection, PricedLine, QuotePreviewResponse, QuoteRequest,
    QuoteResponse, QuoteStatus, ReferenceRate, SelectedResource,
)
from app.integrations.ai_gateway import AiGateway
from app.integrations.azure_prompt_library import AZURE_CORE_PROMPT, AZURE_SERVICE_PROMPTS


AZURE_OUTPUT_CONTRACT = """
返回严格 JSON：
{"customer_summary":"摘要","services":[{
 "service":"azure_vm","display_name":"Azure Virtual Machines","service_name":"Virtual Machines",
 "region":"southeastasia","quantity":1,"hours_per_month":730,
 "requested_sku":null,"product_name":null,"sku_name":null,"meter_name":null,
 "monthly_quantity":null,"requirements":{},"source_text":"客户原话"
}],"ambiguities":[]}

要求：service_name 必须尽量使用 Azure Retail Prices API 的正式 serviceName；客户明确给出的容量、
流量、请求量写 monthly_quantity，单位同时写 requirements.usage_unit。VM 的 monthly_quantity 留空，
程序按 quantity × hours_per_month 计算。客户没给按量用量则 monthly_quantity=null，只展示单位价。
区域必须转换成 armRegionName（例如新加坡 southeastasia、东京 japaneast、香港 eastasia、伦敦 uksouth）。
ambiguities 只写真正阻止报价的客户问题；未给可选用量不提问。
"""


class AzureQuoteService:
    """Small isolated Azure retail-pricing pipeline; it never calls AWS clients."""

    def __init__(self, gateway: AiGateway):
        self._gateway = gateway
        self._drafts: dict[str, dict[str, Any]] = {}

    async def _parse(self, request: QuoteRequest) -> dict[str, Any]:
        prompt = "\n\n".join([AZURE_CORE_PROMPT, *AZURE_SERVICE_PROMPTS.values(), AZURE_OUTPUT_CONTRACT])
        try:
            payload = await self._gateway.complete_json(
                system_prompt=prompt,
                user_content=(request.customer_request + (
                    "\n\n【客户确认回复，优先于原文】\n" + "\n".join(
                        f"{question}: {answer}" for question, answer in request.confirmation_responses.items()
                    ) if request.confirmation_responses else ""
                )),
                timeout_seconds=45,
                expected_keys=("customer_summary", "services", "ambiguities"),
            )
        except Exception as exc:
            raise QuoteError("azure_ai_parse_failed", "系统暂时未能整理 Azure 需求，请重试。", {"type": type(exc).__name__}, 503) from exc
        if not isinstance(payload.get("services"), list) or not payload["services"]:
            raise QuoteError("azure_no_services", "未识别到可报价的 Microsoft Azure 服务。")
        self._normalize_payload(payload, request)
        return payload

    @staticmethod
    def _normalize_payload(payload: dict[str, Any], request: QuoteRequest) -> None:
        """Reject common cross-field hallucinations before preview/pricing."""
        explicit_global = bool(re.search(r"(?:区域|地域|region)\s*[：:]?\s*(?:全球|global)", request.customer_request, re.I))
        answers_text = " ".join(request.confirmation_responses.values()).casefold()
        region_aliases = {
            "新加坡": "southeastasia", "东京": "japaneast", "日本东部": "japaneast",
            "香港": "eastasia", "伦敦": "uksouth", "英国南部": "uksouth",
            "东亚": "eastasia", "美国东部": "eastus", "西欧": "westeurope",
        }
        confirmed_region = next((code for label, code in region_aliases.items() if label in answers_text), None)
        missing_region = False
        for item in payload.get("services", []):
            requirements = item.setdefault("requirements", {})
            source = str(item.get("source_text") or "")
            region = str(item.get("region") or "").strip().lower()
            if not region and confirmed_region:
                item["region"] = confirmed_region
                region = confirmed_region
            if (not region or region == "global") and not explicit_global and not re.search(r"全球|global", source, re.I):
                item["region"] = None
                missing_region = True
            sku = str(item.get("requested_sku") or item.get("sku_name") or requirements.get("sku_name") or "").strip()
            numeric_sku = re.fullmatch(r"([\d.]+)\s*(gib|gb|mb|tb)", sku, re.I)
            service = str(item.get("service") or "")
            if numeric_sku and service == "azure_cache":
                value, unit = float(numeric_sku.group(1)), numeric_sku.group(2).lower()
                requirements["memory_gib"] = value * (1024 if unit == "tb" else 1 / 1024 if unit == "mb" else 1)
                item["requested_sku"] = None
                item["sku_name"] = None
                requirements.pop("sku_name", None)
            elif numeric_sku and service in {"api_management", "apim"}:
                requirements["stated_capacity"] = sku
                item["requested_sku"] = None
                item["sku_name"] = None
                requirements.pop("sku_name", None)
            if service in {"api_management", "apim", "azure_api_management"} and re.search(r"5120\s*MB", source, re.I):
                requirements.pop("bandwidth_mbps", None)
                requirements["stated_capacity"] = "5120 MB"
            # The UI and the existing quote schema consistently use GiB/TiB binary conversion.
            storage_match = re.search(r"([\d.]+)\s*(?:TB|TiB)", source, re.I)
            if storage_match and "storage_gib" in requirements:
                requirements["storage_gib"] = float(storage_match.group(1)) * 1024
        ambiguities = [str(value).strip() for value in payload.get("ambiguities", []) if str(value).strip()]
        non_region = [value for value in ambiguities if not re.search(r"区域|地域|region", value, re.I)]
        if missing_region and not request.confirmation_responses:
            non_region.insert(0, "这些区域型服务部署在哪个 Azure 区域？")
        if any(
            str(item.get("service") or "") in {"api_management", "apim", "azure_api_management"}
            and (item.get("requirements") or {}).get("stated_capacity") == "5120 MB"
            for item in payload.get("services", [])
        ) and not request.confirmation_responses:
            non_region.append("API 管理的 5120 MB 指每月流量，还是单次请求大小？")
        payload["ambiguities"] = list(dict.fromkeys(non_region))

    @staticmethod
    def _component_id(index: int) -> str:
        return f"az-{index + 1:02d}"

    async def preview(self, request: QuoteRequest) -> QuotePreviewResponse:
        payload = await self._parse(request)
        draft_id = uuid.uuid4().hex[:12]
        self._drafts[draft_id] = payload
        selections = []
        for index, item in enumerate(payload["services"]):
            requirements = dict(item.get("requirements") or {})
            requested = item.get("requested_sku")
            if requested:
                requirements["requested_sku"] = requested
            selections.append(PreviewSelection(
                component_id=self._component_id(index),
                service=str(item.get("service") or "azure_service"),
                display_name=str(item.get("display_name") or item.get("service_name") or "Microsoft Azure"),
                region=str(item.get("region") or "global"),
                quantity=max(1, int(item.get("quantity") or 1)),
                requirements=requirements,
                source_text=str(item.get("source_text") or ""),
                requested_model=requested,
                selected_model=requested,
                selection_reason="由 Microsoft Azure Retail Prices API 核价",
                status="ready",
            ))
        ambiguities = [str(value) for value in payload.get("ambiguities", []) if str(value).strip()]
        confirmation = None
        if ambiguities:
            confirmation = "您好，请确认：\n" + "\n".join(f"{i + 1}. {q}" for i, q in enumerate(ambiguities))
            for selection in selections:
                if selection.region == "global" or selection.region == "None":
                    selection.region = "未指定区域"
                    selection.requires_confirmation = True
                    selection.status = "customer_issue"
                    selection.confirmation_reason = ambiguities[0]
        return QuotePreviewResponse(
            draft_id=draft_id,
            customer_summary=str(payload.get("customer_summary") or "Azure 成本估算"),
            selections=selections,
            confirmation_text=confirmation,
            confirmation_items=[{"question": question, "options": []} for question in ambiguities],
            notices=["价格来源：Microsoft Azure Retail Prices API（公开零售价）"],
            execution_trace=[ExecutionEvent(stage="ai", message="系统已拆分 Microsoft Azure 服务与规格", status="completed")],
        )

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("'", "''")

    async def _retail_items(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        filters = [f"serviceName eq '{self._escape(str(item.get('service_name') or item.get('display_name') or ''))}'"]
        region = str(item.get("region") or "").strip()
        if region and region != "global":
            filters.append(f"armRegionName eq '{self._escape(region)}'")
        sku = str(item.get("requested_sku") or "").strip()
        if sku:
            filters.append(f"armSkuName eq '{self._escape(sku)}'")
        params = {"$filter": " and ".join(filters), "currencyCode": "USD"}
        async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
            response = await client.get("https://prices.azure.com/api/retail/prices", params=params)
            response.raise_for_status()
            return list(response.json().get("Items") or [])

    @staticmethod
    def _choose_rate(item: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        product = str(item.get("product_name") or "").casefold()
        sku_name = str(item.get("sku_name") or "").casefold()
        meter = str(item.get("meter_name") or "").casefold()
        candidates = [row for row in rows if float(row.get("unitPrice") or 0) >= 0 and str(row.get("type") or "Consumption") == "Consumption"]
        requested_sku = str(item.get("requested_sku") or "").strip().casefold()
        if requested_sku:
            exact = [row for row in candidates if str(row.get("skuName") or "").strip().casefold() == requested_sku]
            if exact: candidates = exact
        # Pay-as-you-go must never silently become Spot, Low Priority or Dev/Test.
        candidates = [row for row in candidates if not any(
            marker in f"{row.get('skuName', '')} {row.get('meterName', '')}".casefold()
            for marker in ("spot", "low priority")
        )]
        operating_system = str((item.get("requirements") or {}).get("operating_system") or "").casefold()
        if operating_system:
            if "windows" in operating_system:
                matching_os = [row for row in candidates if "windows" in str(row.get("productName") or "").casefold()]
            else:
                matching_os = [row for row in candidates if "windows" not in str(row.get("productName") or "").casefold()]
            if matching_os: candidates = matching_os
        if product:
            exact = [row for row in candidates if product in str(row.get("productName") or "").casefold()]
            if exact: candidates = exact
        if sku_name:
            exact = [row for row in candidates if sku_name in str(row.get("skuName") or "").casefold()]
            if exact: candidates = exact
        if meter:
            exact = [row for row in candidates if meter in str(row.get("meterName") or "").casefold()]
            if exact: candidates = exact
        return min(candidates, key=lambda row: float(row.get("unitPrice") or 0)) if candidates else None

    async def create_quote(self, request: QuoteRequest, reporter=None) -> QuoteResponse:
        payload = self._drafts.get(request.draft_id or "") or await self._parse(request)
        selections: list[SelectedResource] = []
        priced_lines: list[PricedLine] = []
        notices = ["Microsoft Azure 公开零售价；不含 EA/MCA/CSP 协议折扣、税费和抵扣。"]
        for index, item in enumerate(payload["services"]):
            if reporter: await reporter("pricing", f"正在查询第 {index + 1} 项 {item.get('display_name') or item.get('service_name')}")
            try:
                rows = await self._retail_items(item)
            except Exception as exc:
                raise QuoteError("azure_retail_api_failed", "Azure Retail Prices API 暂时未返回价格，请重试。", {"type": type(exc).__name__}, 503) from exc
            rate = self._choose_rate(item, rows)
            if rate is None:
                notices.append(f"{item.get('display_name') or item.get('service_name')} 未找到精确零售价，未计入合计。")
                continue
            unit_price = float(rate.get("unitPrice") or 0)
            unit = str(rate.get("unitOfMeasure") or "unit")
            quantity = max(1, int(item.get("quantity") or 1))
            monthly_quantity = item.get("monthly_quantity")
            if str(item.get("service")) == "azure_vm":
                amount = quantity * float(item.get("hours_per_month") or 730)
            elif monthly_quantity is not None:
                amount = float(monthly_quantity) * quantity
            else:
                amount = 0
            cost = unit_price * amount
            reference_rates = [] if amount else [ReferenceRate(
                description=str(rate.get("meterName") or rate.get("productName") or "Azure 单位价"),
                unit=unit, unit_price=unit_price, service_code=str(rate.get("serviceName") or "Azure"),
                usage_type=str(rate.get("skuName") or ""), operation=str(rate.get("meterName") or ""),
            )]
            model = str(rate.get("armSkuName") or rate.get("skuName") or item.get("requested_sku") or "按量计费")
            selections.append(SelectedResource(
                service=str(item.get("service") or "azure_service"), display_name=str(item.get("display_name") or rate.get("serviceName") or "Microsoft Azure"),
                region=str(item.get("region") or "global"), model=model, architecture="Microsoft Azure Retail",
                specifications={**dict(item.get("requirements") or {}), "quantity": quantity}, official_product=rate,
                rationale="由 Azure Retail Prices API 返回公开零售价", reference_rates=reference_rates,
            ))
            if amount:
                priced_lines.append(PricedLine(key=f"AZ{index + 1}", service_code=str(rate.get("serviceName") or "Azure"), usage_type=str(rate.get("skuName") or ""), operation=str(rate.get("meterName") or ""), amount=amount, unit=unit, cost=cost))
        return QuoteResponse(
            quote_id=f"azure-{uuid.uuid4().hex[:10]}", status=QuoteStatus.QUOTED,
            customer_summary=str(payload.get("customer_summary") or "Azure 成本估算"), selections=selections,
            priced_lines=priced_lines, total_cost=sum(line.cost for line in priced_lines), currency="USD",
            rate_type="Azure retail price", rate_timestamp=datetime.now(UTC), notices=notices,
            pricing_source="Microsoft Azure Retail Prices API", source_url="https://prices.azure.com/api/retail/prices",
        )
