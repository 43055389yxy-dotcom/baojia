from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from app.core.errors import ManualConfirmationRequired
from app.domain.models import (
    CandidateOption,
    PreviewSelection,
    PricedLine,
    QuoteRequest,
    ReferenceRate,
    SelectedResource,
    ServiceRequirement,
)
from app.integrations.azure_auto_service_discovery import AzureAutoServiceDiscovery
from app.integrations.azure_catalog import AzureOfficialCatalog
from app.integrations.azure_intent import AZURE_SERVICE_NAMES, canonical_azure_service

AZURE_RETAIL_SERVICE_NAMES: dict[str, str] = {
    "azure_vm": "Virtual Machines",
    "managed_disks": "Storage",
    "azure_sql": "SQL Database",
    "azure_postgresql": "Azure Database for PostgreSQL",
    "azure_mysql": "Azure Database for MySQL",
    "azure_cache": "Redis Cache",
    "blob_storage": "Storage",
    "load_balancer": "Load Balancer",
    "application_gateway": "Application Gateway",
    "front_door": "Azure Front Door Service",
    "bandwidth": "Bandwidth",
    "aks": "Azure Kubernetes Service",
    "monitor": "Azure Monitor",
    "api_management": "API Management",
}

AZURE_REGION_FALLBACK: tuple[tuple[str, str], ...] = (
    ("australiacentral", "澳大利亚中部"),
    ("australiaeast", "澳大利亚东部"),
    ("australiasoutheast", "澳大利亚东南部"),
    ("brazilsouth", "巴西南部"),
    ("brazilsoutheast", "巴西东南部"),
    ("canadacentral", "加拿大中部"),
    ("canadaeast", "加拿大东部"),
    ("centralindia", "印度中部"),
    ("centralus", "美国中部"),
    ("eastasia", "东亚（香港）"),
    ("eastus", "美国东部"),
    ("eastus2", "美国东部 2"),
    ("francecentral", "法国中部"),
    ("germanywestcentral", "德国中西部"),
    ("indonesiacentral", "印度尼西亚中部"),
    ("israelcentral", "以色列中部"),
    ("italynorth", "意大利北部"),
    ("japaneast", "日本东部（东京）"),
    ("japanwest", "日本西部（大阪）"),
    ("koreacentral", "韩国中部（首尔）"),
    ("koreasouth", "韩国南部"),
    ("malaysiawest", "马来西亚西部"),
    ("mexicocentral", "墨西哥中部"),
    ("newzealandnorth", "新西兰北部"),
    ("northcentralus", "美国中北部"),
    ("northeurope", "北欧（爱尔兰）"),
    ("norwayeast", "挪威东部"),
    ("polandcentral", "波兰中部"),
    ("qatarcentral", "卡塔尔中部"),
    ("southafricanorth", "南非北部"),
    ("southcentralus", "美国中南部"),
    ("southeastasia", "东南亚（新加坡）"),
    ("southindia", "印度南部"),
    ("spaincentral", "西班牙中部"),
    ("swedencentral", "瑞典中部"),
    ("switzerlandnorth", "瑞士北部"),
    ("uaenorth", "阿联酋北部"),
    ("uksouth", "英国南部（伦敦）"),
    ("ukwest", "英国西部"),
    ("westcentralus", "美国中西部"),
    ("westeurope", "西欧（荷兰）"),
    ("westindia", "印度西部"),
    ("westus", "美国西部"),
    ("westus2", "美国西部 2"),
    ("westus3", "美国西部 3"),
)


@dataclass(slots=True)
class AzureComponentQuote:
    selection: SelectedResource
    priced_lines: list[PricedLine]
    upfront_cost: float = 0


def _fold(value: object) -> str:
    return str(value or "").strip().casefold()


def _rate_model(row: dict[str, Any]) -> str:
    product = _fold(row.get("productName"))
    if "application gateway waf v2" in product:
        return "WAF_v2"
    if "application gateway standard v2" in product:
        return "Standard_v2"
    return str(row.get("armSkuName") or row.get("skuName") or row.get("productName") or "")


def _unit_divisor(unit: str) -> float:
    compact = unit.casefold().replace(" ", "")
    match = re.match(r"([\d.]+)", compact)
    return float(match.group(1)) if match else 1.0


class AzureRetailPlugin:
    def __init__(
        self,
        service: str,
        catalog: AzureOfficialCatalog,
        *,
        retail_service_name: str | None = None,
        display_name: str | None = None,
    ):
        self.service = service
        self.display_name = display_name or AZURE_SERVICE_NAMES.get(
            service, "Microsoft Azure"
        )
        self.retail_service_name = retail_service_name or AZURE_RETAIL_SERVICE_NAMES[
            service
        ]
        self._catalog = catalog

    async def preview(
        self,
        requirement: ServiceRequirement,
        request: QuoteRequest,
        component_id: str,
    ) -> PreviewSelection:
        region = requirement.region
        if not region and self.service != "front_door":
            return PreviewSelection(
                component_id=component_id,
                service=self.service,
                display_name=self.display_name,
                region="未指定区域",
                quantity=requirement.quantity,
                requirements=requirement.requirements,
                source_text=requirement.source_text,
                candidates=[],
                requires_confirmation=True,
                confirmation_reason="请确认该组件部署在哪个 Azure 区域。",
                status="customer_issue",
                issue_message="请确认该组件部署在哪个 Azure 区域。",
            )
        requested_sku = self._requested_sku(requirement)
        catalog_region = None if self.service in {"front_door", "load_balancer"} else region
        rows = await self._catalog.retail_items(
            service_name=self.retail_service_name,
            region=catalog_region,
            arm_sku_name=(requested_sku if self.service == "azure_vm" else None),
        )
        if self.service == "azure_vm" and requested_sku and not rows:
            rows = await self._catalog.retail_items(
                service_name=self.retail_service_name,
                region=catalog_region,
            )
            eligible = self._eligible_rows(requirement, request, rows)
            candidates = self._candidate_options(eligible)
            return self._confirmation_preview(
                requirement,
                component_id,
                "客户填写的 Azure VM SKU 在当前区域不可用，请从官方可用型号中选择。",
            ).model_copy(update={"candidates": candidates})
        if self.service == "azure_vm" and not requested_sku:
            return await self._preview_vm_by_shape(requirement, request, component_id, rows)
        eligible = self._eligible_rows(requirement, request, rows)
        candidates = self._candidate_options(eligible)
        if self.service == "managed_disks" and not requested_sku:
            message = (
                "Azure Managed Disks 按固定磁盘 SKU 计费；请确认磁盘档位，例如 P10 LRS 或 E10 LRS。"
            )
            return self._confirmation_preview(requirement, component_id, message).model_copy(
                update={"candidates": candidates[:12]}
            )
        if (
            self.service == "monitor"
            and requirement.requirements.get("log_ingestion_gib") is not None
            and not requested_sku
            and len(candidates) > 1
        ):
            message = (
                "Azure Monitor 日志写入包含多个官方日志层级，"
                "请从下方选择与客户日志类型对应的计费项。"
            )
            return self._confirmation_preview(requirement, component_id, message).model_copy(
                update={"candidates": candidates[:12]}
            )
        if not candidates:
            message = f"Microsoft 官方目录没有找到 {self.display_name} 在当前区域的精确计费项。"
            return PreviewSelection(
                component_id=component_id,
                service=self.service,
                display_name=self.display_name,
                region=region or "global",
                quantity=requirement.quantity,
                requirements=requirement.requirements,
                source_text=requirement.source_text,
                requested_model=requested_sku,
                candidates=[],
                requires_confirmation=True,
                confirmation_reason=message,
                status="customer_issue",
                issue_message=message,
            )
        selected = candidates[0]
        return PreviewSelection(
            component_id=component_id,
            service=self.service,
            display_name=self.display_name,
            region=region or "global",
            quantity=requirement.quantity,
            requirements=requirement.requirements,
            source_text=requirement.source_text,
            requested_model=requested_sku,
            selected_model=selected.model,
            selection_reason=selected.rationale,
            candidates=candidates[:12],
            status="ready",
        )

    async def quote(
        self,
        requirement: ServiceRequirement,
        request: QuoteRequest,
        component_index: int,
    ) -> AzureComponentQuote:
        region = requirement.region
        requested_sku = self._requested_sku(requirement)
        review_model = str(requirement.requirements.get("_review_selected_model") or "").strip()
        effective_sku = requested_sku or review_model or None
        catalog_region = None if self.service in {"front_door", "load_balancer"} else region
        rows = await self._catalog.retail_items(
            service_name=self.retail_service_name,
            region=catalog_region,
            arm_sku_name=(effective_sku if self.service == "azure_vm" else None),
        )
        eligible = self._eligible_rows(requirement, request, rows)
        if effective_sku:
            exact = [row for row in eligible if _fold(_rate_model(row)) == _fold(effective_sku)]
            if exact:
                eligible = exact
        if not eligible:
            raise ManualConfirmationRequired(
                f"Microsoft 官方目录没有返回 {self.display_name} 的精确价格，禁止猜价。",
                code="azure_exact_meter_not_found",
                service=self.service,
                region=region,
                requested_sku=effective_sku,
            )
        charges = self._charges(requirement, request, eligible)
        if not charges:
            raise ManualConfirmationRequired(
                f"{self.display_name} 未能唯一确定 Azure Meter。",
                code="azure_meter_ambiguous",
                service=self.service,
            )
        priced_lines: list[PricedLine] = []
        reference_rates: list[ReferenceRate] = []
        upfront = 0.0
        official_rows: list[dict[str, Any]] = []
        for line_index, (row, amount, monthly_cost, upfront_cost) in enumerate(charges, start=1):
            official_rows.append(row)
            unit_price = float(row.get("unitPrice") or row.get("retailPrice") or 0)
            unit = str(row.get("unitOfMeasure") or "1 Unit")
            if amount is None:
                reference_rates.append(
                    ReferenceRate(
                        description=str(
                            row.get("meterName") or row.get("productName") or "Azure 单位价"
                        ),
                        unit=unit,
                        unit_price=unit_price,
                        service_code=str(row.get("serviceName") or self.retail_service_name),
                        usage_type=str(row.get("skuName") or ""),
                        operation=str(row.get("meterName") or ""),
                    )
                )
                continue
            priced_lines.append(
                PricedLine(
                    key=f"az{component_index + 1}l{line_index}",
                    service_code=str(row.get("serviceName") or self.retail_service_name),
                    usage_type=str(row.get("skuName") or ""),
                    operation=str(row.get("meterName") or ""),
                    amount=amount,
                    unit=unit,
                    cost=monthly_cost,
                )
            )
            upfront += upfront_cost
        primary = official_rows[0]
        model = _rate_model(primary)
        selection = SelectedResource(
            service=self.service,
            display_name=self.display_name,
            region=region or "global",
            model=model,
            quantity=requirement.quantity,
            architecture="Microsoft Azure Retail",
            specifications={
                key: value
                for key, value in requirement.requirements.items()
                if not key.startswith("_")
            },
            official_product={
                "source": "Microsoft Azure Retail Prices API",
                "productId": primary.get("productId"),
                "skuId": primary.get("skuId"),
                "meterId": primary.get("meterId"),
                "armSkuName": primary.get("armSkuName"),
                "effectiveStartDate": primary.get("effectiveStartDate"),
                "meters": official_rows,
            },
            rationale="由 Microsoft 官方目录精确匹配产品、SKU、区域与计费方式。",
            reference_rates=reference_rates,
            upfront_commitment_cost=upfront,
        )
        return AzureComponentQuote(selection, priced_lines, upfront)

    async def _preview_vm_by_shape(
        self,
        requirement: ServiceRequirement,
        request: QuoteRequest,
        component_id: str,
        rows: list[dict[str, Any]],
    ) -> PreviewSelection:
        vcpu = requirement.requirements.get("vcpu")
        memory = requirement.requirements.get("memory_gib")
        if not isinstance(vcpu, (int, float)) or not isinstance(memory, (int, float)):
            message = "请提供 Azure VM 的官方 SKU，或同时提供 vCPU 和内存。"
            return self._confirmation_preview(requirement, component_id, message)
        sku_rows = await self._catalog.compute_skus(str(requirement.region))
        if not sku_rows:
            public_candidates = self._public_vm_shape_candidates(
                requirement,
                self._eligible_rows(requirement, request, rows),
            )
            prior_sku = next(
                reversed(
                    re.findall(
                        r"\bStandard_[A-Za-z0-9_-]+\b",
                        requirement.source_text,
                        re.I,
                    )
                ),
                None,
            )
            exact_candidates = [
                candidate
                for candidate in public_candidates
                if candidate.specifications.get("vCPU") == float(vcpu)
                and candidate.specifications.get("memoryGiB") == float(memory)
            ]
            force_customer_selection = (
                requirement.requirements.get("_customer_select_official_sku") is True
            )
            if exact_candidates and prior_sku is None and not force_customer_selection:
                selected = exact_candidates[0]
                return PreviewSelection(
                    component_id=component_id,
                    service=self.service,
                    display_name=self.display_name,
                    region=str(requirement.region),
                    quantity=requirement.quantity,
                    requirements=requirement.requirements,
                    source_text=requirement.source_text,
                    selected_model=selected.model,
                    selection_reason=(
                        "已找到完全满足处理器和内存要求的 Microsoft 官方 SKU，"
                        "按公开零售价选择成本最低项。"
                    ),
                    candidates=exact_candidates[:12],
                    status="ready",
                )
            message = (
                f"原型号 {prior_sku} 与新要求 {float(vcpu):g} 核 "
                f"{float(memory):g} GiB 不一致，请从下方匹配或最接近的 "
                "Microsoft 官方 SKU 中重新选择。"
                if prior_sku
                else (
                    f"您要求 Azure 虚拟机使用 {float(vcpu):g} 核 "
                    f"{float(memory):g} GiB，请从下方匹配或最接近的 "
                    "Microsoft 官方 SKU 中选择。"
                )
            )
            return self._confirmation_preview(
                requirement, component_id, message
            ).model_copy(update={"candidates": public_candidates})
        capabilities: dict[str, dict[str, float]] = {}
        exact_models: set[str] = set()
        for sku in sku_rows:
            if sku.get("resourceType") != "virtualMachines":
                continue
            restrictions = sku.get("restrictions") or []
            if restrictions:
                continue
            values = {
                str(item.get("name")): str(item.get("value"))
                for item in sku.get("capabilities") or []
                if isinstance(item, dict)
            }
            try:
                sku_vcpu = float(values["vCPUs"])
                sku_memory = float(values["MemoryGB"])
            except (KeyError, ValueError):
                continue
            if sku_vcpu >= float(vcpu) and sku_memory >= float(memory):
                model = str(sku.get("name"))
                capabilities[model] = {
                    "vCPU": sku_vcpu,
                    "memoryGiB": sku_memory,
                }
                if sku_vcpu == float(vcpu) and sku_memory == float(memory):
                    exact_models.add(model)
        eligible = self._eligible_rows(requirement, request, rows)
        joined = [row for row in eligible if _rate_model(row) in capabilities]
        candidates = self._candidate_options(joined, capabilities)
        if not candidates:
            return self._confirmation_preview(
                requirement,
                component_id,
                "该 Azure 订阅在当前区域没有满足规格且可核价的 VM SKU。",
            )
        exact_candidates = [
            candidate for candidate in candidates if candidate.model in exact_models
        ]
        if not exact_candidates:
            return self._confirmation_preview(
                requirement,
                component_id,
                (
                    f"您要求 Azure 虚拟机使用 {float(vcpu):g} 核 {float(memory):g} GiB，"
                    "但当前区域没有完全相同的官方型号，请从下方最接近的可用 SKU 中重新选择。"
                ),
            ).model_copy(update={"candidates": candidates[:12]})
        selected = exact_candidates[0]
        return PreviewSelection(
            component_id=component_id,
            service=self.service,
            display_name=self.display_name,
            region=str(requirement.region),
            quantity=requirement.quantity,
            requirements=requirement.requirements,
            source_text=requirement.source_text,
            selected_model=selected.model,
            selection_reason="先按订阅官方规格筛选，再选择零售价最低的合格 SKU。",
            candidates=exact_candidates[:12],
            status="ready",
        )

    def _public_vm_shape_candidates(
        self,
        requirement: ServiceRequirement,
        rows: list[dict[str, Any]],
    ) -> list[CandidateOption]:
        """Compact public retail SKUs using Microsoft's standard VM name shape."""

        requested_vcpu = float(requirement.requirements["vcpu"])
        requested_memory = float(requirement.requirements["memory_gib"])
        capabilities: dict[str, dict[str, float]] = {}
        for row in rows:
            model = _rate_model(row)
            shape = self._shape_from_standard_vm_sku(model)
            if shape is not None:
                capabilities[model] = shape
        candidates = self._candidate_options(rows, capabilities)
        shaped = [candidate for candidate in candidates if candidate.specifications]
        exact = [
            candidate
            for candidate in shaped
            if candidate.specifications.get("vCPU") == requested_vcpu
            and candidate.specifications.get("memoryGiB") == requested_memory
        ]
        if exact:
            return exact[:12]

        def distance(candidate: CandidateOption) -> tuple[float, float, str]:
            vcpu = float(candidate.specifications.get("vCPU") or 0)
            memory = float(candidate.specifications.get("memoryGiB") or 0)
            score = (
                abs(vcpu - requested_vcpu) / max(requested_vcpu, 1)
                + abs(memory - requested_memory) / max(requested_memory, 1)
            )
            return score, candidate.monthly_catalog_cost or float("inf"), candidate.model

        return sorted(shaped, key=distance)[:12] or candidates[:12]

    @staticmethod
    def _shape_from_standard_vm_sku(model: str) -> dict[str, float] | None:
        """Decode mainstream D/E/F SKU names; unknown families stay untrusted."""

        match = re.match(
            r"^Standard_([DEFdef])(\d+)(?:-(\d+))?[A-Za-z0-9_]*$",
            model,
        )
        if match is None:
            return None
        family = match.group(1).upper()
        full_vcpu = float(match.group(2))
        active_vcpu = float(match.group(3) or full_vcpu)
        memory_per_full_vcpu = {"D": 4.0, "E": 8.0, "F": 2.0}[family]
        return {
            "vCPU": active_vcpu,
            "memoryGiB": full_vcpu * memory_per_full_vcpu,
        }

    def _confirmation_preview(
        self,
        requirement: ServiceRequirement,
        component_id: str,
        message: str,
    ) -> PreviewSelection:
        return PreviewSelection(
            component_id=component_id,
            service=self.service,
            display_name=self.display_name,
            region=requirement.region or "未指定区域",
            quantity=requirement.quantity,
            requirements=requirement.requirements,
            source_text=requirement.source_text,
            candidates=[],
            requires_confirmation=True,
            confirmation_reason=message,
            status="customer_issue",
            issue_message=message,
        )

    def _eligible_rows(
        self,
        requirement: ServiceRequirement,
        request: QuoteRequest,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        mode = request.azure_pricing_mode
        rows = self._identity_rows(rows)
        primary_rows = [row for row in rows if row.get("isPrimaryMeterRegion") is not False]
        if primary_rows:
            rows = primary_rows
        consumption = [
            row
            for row in rows
            if _fold(row.get("type") or "Consumption") == "consumption"
            and float(row.get("unitPrice") or row.get("retailPrice") or 0) > 0
        ]

        def text(row: dict[str, Any]) -> str:
            return _fold(
                f"{row.get('productName', '')} {row.get('skuName', '')} {row.get('meterName', '')}"
            )

        if mode == "spot" and self.service == "azure_vm":
            candidates = [row for row in consumption if "spot" in text(row)]
        else:
            candidates = [
                row
                for row in consumption
                if not any(marker in text(row) for marker in ("spot", "low priority"))
            ]
        candidates = [row for row in candidates if _fold(row.get("type")) != "devtestconsumption"]
        os_name = _fold(requirement.requirements.get("operating_system"))
        if self.service == "azure_vm" and os_name:
            if "windows" in os_name:
                candidates = [
                    row for row in candidates if "windows" in _fold(row.get("productName"))
                ]
            else:
                candidates = [
                    row for row in candidates if "windows" not in _fold(row.get("productName"))
                ]
        candidates = self._apply_requirement_keywords(requirement, candidates)
        candidates = self._apply_numeric_requirements(requirement, candidates)
        if mode == "reservation":
            term = f"{request.azure_term_years or 1} year"
            reserved = [
                row
                for row in rows
                if _fold(row.get("type")) == "reservation"
                and term in _fold(row.get("reservationTerm"))
            ]
            reserved = self._apply_requirement_keywords(requirement, reserved)
            reserved = self._apply_numeric_requirements(requirement, reserved)
            if reserved:
                candidates = reserved
        elif mode == "savings_plan":
            term = f"{request.azure_term_years or 1} year"
            expanded: list[dict[str, Any]] = []
            for row in candidates:
                for saving in row.get("savingsPlan") or []:
                    if term not in _fold(saving.get("term")):
                        continue
                    expanded.append(
                        {
                            **row,
                            "unitPrice": saving.get("unitPrice"),
                            "retailPrice": saving.get("retailPrice"),
                            "_azurePriceType": "SavingsPlan",
                            "_azureTerm": saving.get("term"),
                        }
                    )
            if expanded:
                candidates = expanded
        return candidates

    def _apply_numeric_requirements(
        self,
        requirement: ServiceRequirement,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        requirements = requirement.requirements
        result = rows
        if self.service in {"azure_postgresql", "azure_mysql", "azure_sql"}:
            vcore = requirements.get("vcore")
            if isinstance(vcore, (int, float)):
                expected = f"{float(vcore):g} vcore"
                exact = [
                    row
                    for row in result
                    if _fold(row.get("skuName")) == expected
                ]
                if exact:
                    result = exact
        if self.service == "monitor" and requirements.get("log_ingestion_gib") is not None:
            log_rows = [
                row
                for row in result
                if "data ingestion" in _fold(row.get("meterName"))
                and "metric" not in _fold(row.get("meterName"))
                and "gb" in _fold(row.get("unitOfMeasure"))
            ]
            if log_rows:
                result = log_rows
        return result

    def _identity_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.service == "managed_disks":
            return [
                row
                for row in rows
                if "managed disks" in _fold(row.get("productName"))
                and _fold(row.get("meterName")).endswith(" disk")
            ]
        if self.service == "blob_storage":
            return [
                row
                for row in rows
                if "managed disks" not in _fold(row.get("productName"))
                and "page blob" not in _fold(row.get("productName"))
                and any(
                    marker in _fold(row.get("meterName")) for marker in ("data stored", "storage")
                )
            ]
        return rows

    def _apply_requirement_keywords(
        self,
        requirement: ServiceRequirement,
        rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        requirements = requirement.requirements
        keys = (
            "requested_sku",
            "sku_name",
            "service_tier",
            "access_tier",
            "redundancy",
            "disk_type",
            "compute_model",
        )
        result = rows
        for key in keys:
            value = _fold(requirements.get(key))
            if not value:
                continue
            normalized = value.replace("_", " ")
            matching = [
                row
                for row in result
                if normalized
                in _fold(
                    f"{row.get('armSkuName', '')} {row.get('skuName', '')} "
                    f"{row.get('productName', '')} {row.get('meterName', '')}"
                ).replace("_", " ")
            ]
            if matching:
                result = matching
        return result

    def _candidate_options(
        self,
        rows: list[dict[str, Any]],
        capabilities: dict[str, dict[str, float]] | None = None,
    ) -> list[CandidateOption]:
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            model = _rate_model(row)
            if not model:
                continue
            key = model.casefold()
            if key not in unique or float(row.get("unitPrice") or 0) < float(
                unique[key].get("unitPrice") or 0
            ):
                unique[key] = row
        ordered = sorted(
            unique.values(),
            key=lambda row: (float(row.get("unitPrice") or 0), _rate_model(row)),
        )
        return [
            CandidateOption(
                model=_rate_model(row),
                family=str(row.get("productName") or self.display_name),
                specifications=(capabilities or {}).get(_rate_model(row), {}),
                monthly_catalog_cost=float(row.get("unitPrice") or 0),
                rationale="Microsoft 官方目录中满足当前筛选条件的零售计费项。",
                official_product={
                    "productId": row.get("productId"),
                    "skuId": row.get("skuId"),
                    "meterId": row.get("meterId"),
                    "unitOfMeasure": row.get("unitOfMeasure"),
                },
                is_default=index == 0,
            )
            for index, row in enumerate(ordered)
        ]

    def _charges(
        self,
        requirement: ServiceRequirement,
        request: QuoteRequest,
        rows: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], float | None, float, float]]:
        primary = min(rows, key=lambda row: (float(row.get("unitPrice") or 0), _rate_model(row)))
        amount = self._monthly_amount(requirement, primary)
        unit_price = float(primary.get("unitPrice") or primary.get("retailPrice") or 0)
        upfront = 0.0
        if (
            request.azure_pricing_mode == "reservation"
            and _fold(primary.get("type")) == "reservation"
        ):
            years = request.azure_term_years or 1
            monthly_cost = unit_price * requirement.quantity / (years * 12)
            if request.azure_payment_option == "upfront":
                upfront = unit_price * requirement.quantity
            return [(primary, float(requirement.quantity), monthly_cost, upfront)]
        if amount is None:
            return [(primary, None, 0.0, 0.0)]
        billed_amount = amount / _unit_divisor(str(primary.get("unitOfMeasure") or "1 Unit"))
        return [(primary, amount, billed_amount * unit_price, 0.0)]

    def _monthly_amount(self, requirement: ServiceRequirement, row: dict[str, Any]) -> float | None:
        requirements = requirement.requirements
        if self.service in {
            "azure_vm",
            "azure_sql",
            "azure_postgresql",
            "azure_mysql",
            "azure_cache",
            "application_gateway",
            "load_balancer",
            "aks",
            "api_management",
        }:
            return requirement.hours_per_month * requirement.quantity
        if self.service == "managed_disks":
            return float(requirement.quantity)
        if self.service == "blob_storage":
            value = requirements.get("storage_gib")
            return float(value) * requirement.quantity if isinstance(value, (int, float)) else None
        if self.service in {"bandwidth", "front_door"}:
            value = requirements.get("data_transfer_out_gib")
            return float(value) * requirement.quantity if isinstance(value, (int, float)) else None
        if self.service == "monitor":
            value = requirements.get("log_ingestion_gib")
            return float(value) * requirement.quantity if isinstance(value, (int, float)) else None
        value = requirements.get("monthly_quantity")
        return float(value) * requirement.quantity if isinstance(value, (int, float)) else None

    @staticmethod
    def _requested_sku(requirement: ServiceRequirement) -> str | None:
        value = requirement.requirements.get("requested_sku") or requirement.requirements.get(
            "sku_name"
        )
        compact = str(value or "").strip()
        return compact or None


class AzureGenericRetailPlugin:
    """Safe Azure fallback built from the component's official retail profile."""

    def __init__(
        self,
        service: str,
        catalog: AzureOfficialCatalog,
        auto_discovery: AzureAutoServiceDiscovery,
    ):
        self.service = service
        self.display_name = service.replace("_", " ").title()
        self._catalog = catalog
        self._auto_discovery = auto_discovery

    async def _delegate(
        self, requirement: ServiceRequirement
    ) -> AzureRetailPlugin:
        display_name = requirement.calculator_service_name or self.display_name
        profile = await self._auto_discovery.ensure_profile(
            service_key=self.service,
            display_name=display_name,
            region=requirement.region,
        )
        return AzureRetailPlugin(
            self.service,
            self._catalog,
            retail_service_name=str(profile["service_name"]),
            display_name=display_name,
        )

    async def preview(
        self,
        requirement: ServiceRequirement,
        request: QuoteRequest,
        component_id: str,
    ) -> PreviewSelection:
        delegate = await self._delegate(requirement)
        selection = await delegate.preview(requirement, request, component_id)
        requested_sku = delegate._requested_sku(requirement)
        if (
            self.service == "azure_event_hubs"
            and "kafka" in requirement.source_text.casefold()
            and selection.status == "ready"
        ):
            standard = next(
                (
                    candidate
                    for candidate in selection.candidates
                    if candidate.model.casefold() == "standard"
                ),
                None,
            )
            if standard is not None:
                return selection.model_copy(
                    update={
                        "selected_model": standard.model,
                        "selection_reason": (
                            "客户要求 Kafka 兼容接口，已自动选择满足该要求的最低官方层级 Standard。"
                        ),
                        "requires_confirmation": False,
                        "confirmation_reason": None,
                        "status": "ready",
                    }
                )
        if (
            not requested_sku
            and selection.status == "ready"
            and len(selection.candidates) > 1
        ):
            return selection.model_copy(
                update={
                    "selected_model": None,
                    "requires_confirmation": True,
                    "confirmation_reason": (
                        f"{delegate.display_name} 包含多个 Microsoft 官方计费 SKU，"
                        "请从下方选择与客户用途对应的项目。"
                    ),
                    "status": "customer_issue",
                    "candidates": selection.candidates[:24],
                }
            )
        return selection

    async def quote(
        self,
        requirement: ServiceRequirement,
        request: QuoteRequest,
        component_index: int,
    ) -> AzureComponentQuote:
        delegate = await self._delegate(requirement)
        return await delegate.quote(requirement, request, component_index)

    def _confirmation_preview(
        self,
        requirement: ServiceRequirement,
        component_id: str,
        message: str,
    ) -> PreviewSelection:
        return PreviewSelection(
            component_id=component_id,
            service=self.service,
            display_name=requirement.calculator_service_name or self.display_name,
            region=requirement.region or "未指定区域",
            quantity=requirement.quantity,
            requirements=requirement.requirements,
            source_text=requirement.source_text,
            candidates=[],
            requires_confirmation=True,
            confirmation_reason=message,
            status="customer_issue",
            issue_message=message,
        )


class AzurePluginRegistry:
    def __init__(
        self,
        catalog: AzureOfficialCatalog,
        auto_discovery: AzureAutoServiceDiscovery | None = None,
    ):
        self._catalog = catalog
        self._auto_discovery = auto_discovery
        self._plugins: dict[str, AzureRetailPlugin | AzureGenericRetailPlugin] = {
            service: AzureRetailPlugin(service, catalog) for service in AZURE_RETAIL_SERVICE_NAMES
        }

    def get(self, service: str) -> AzureRetailPlugin | AzureGenericRetailPlugin:
        key = canonical_azure_service(service)
        try:
            return self._plugins[key]
        except KeyError as exc:
            if self._auto_discovery is not None:
                plugin = AzureGenericRetailPlugin(
                    key,
                    self._catalog,
                    self._auto_discovery,
                )
                self._plugins[key] = plugin
                return plugin
            raise ManualConfirmationRequired(
                f"暂时无法识别 Microsoft Azure 服务“{service}”的官方产品类型，"
                "该组件本次不计价。",
                code="azure_unsupported_service",
                service=service,
            ) from exc

    async def region_options(self) -> list[tuple[str, str]]:
        try:
            regions = await asyncio.wait_for(
                next(iter(self._plugins.values()))._catalog.available_regions(),
                timeout=6,
            )
        except Exception:
            return list(AZURE_REGION_FALLBACK)
        options = [
            (str(item.get("code") or ""), str(item.get("label") or ""))
            for item in regions
            if item.get("code") and item.get("label")
        ]
        return options or list(AZURE_REGION_FALLBACK)
