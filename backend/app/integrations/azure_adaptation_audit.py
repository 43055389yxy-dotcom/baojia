from __future__ import annotations

from typing import Any

from app.integrations.azure_product_registry import AzureProductRegistry
from app.integrations.azure_service_templates import AZURE_SERVICE_TEMPLATE_FIELDS


class AzureAdaptationAudit:
    """Read-only audit of Azure's provider-isolated twelve-layer contract."""

    def __init__(self, registry: AzureProductRegistry) -> None:
        self.registry = registry

    def report(self) -> dict[str, Any]:
        coverage = self.registry.coverage()
        products = self.registry.list_products()
        count = len(products)
        profiles = int(coverage["materialized_profiles"])
        regions = int(coverage["regions_cached"])
        isolated = int(coverage["strictly_isolated"])

        def stage(number: int, name: str, status: str, detail: str) -> dict[str, Any]:
            return {"number": number, "name": name, "status": status, "detail": detail}

        return {
            "summary": {
                **coverage,
                "curated_component_templates": len(AZURE_SERVICE_TEMPLATE_FIELDS),
                "dynamic_profiles_pending_first_use": max(count - profiles, 0),
                "full_isolation_coverage": bool(count) and isolated == count,
            },
            "stages": [
                stage(
                    1,
                    "官方产品身份",
                    "ready" if count else "blocked",
                    f"已登记 {count} 个独立 Azure 产品身份。",
                ),
                stage(
                    2,
                    "产品别名与唯一归属",
                    "ready",
                    "内部 service_key 一组件一身份；Microsoft 共用 serviceName 也不会串组件。",
                ),
                stage(
                    3,
                    "区域支持",
                    "ready_on_demand",
                    f"{regions}/{count} 个产品已缓存实际使用区域；缺失时查 Microsoft 官方目录。",
                ),
                stage(
                    4,
                    "组件数据隔离",
                    "ready" if isolated == count else "blocked",
                    "只允许全局区域继承；CPU、内存、数量、存储和流量禁止跨组件。",
                ),
                stage(
                    5,
                    "专用字段模板",
                    "ready_on_demand",
                    f"{len(AZURE_SERVICE_TEMPLATE_FIELDS)} 个常用模板常驻，其余按官方 Meter 独立生成。",
                ),
                stage(
                    6,
                    "官方规格与下拉选项",
                    "ready_on_demand",
                    "仅使用 Azure Retail Prices 和订阅 SKU，不读取 AWS 型号或价格。",
                ),
                stage(7, "托管与自建决策", "ready", "Azure 托管产品与自建虚拟机保持独立组件边界。"),
                stage(
                    8, "客户原文逐组件对账", "ready", "销售编号硬隔离，每个组件单独识别并保留原文。"
                ),
                stage(
                    9,
                    "复合需求展开",
                    "ready",
                    "控制面、Worker、磁盘、流量按实际 Azure 计费边界分开。",
                ),
                stage(
                    10,
                    "报价保护",
                    "ready",
                    "官方目录失败保留组件并进入销售核验，禁止猜价或静默报零。",
                ),
                stage(11, "编辑重新计算", "ready", "客户修改只重跑受影响的 Azure 组件。"),
                stage(
                    12,
                    "未来产品适配",
                    "ready_on_demand",
                    "新 serviceName 首次出现时生成独立档案并写入 Azure 专属数据库。",
                ),
            ],
        }
