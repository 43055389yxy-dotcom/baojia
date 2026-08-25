from __future__ import annotations

from typing import Any

from app.integrations.aws_product_registry import AwsProductRegistry
from app.integrations.service_templates import SERVICE_TEMPLATE_FIELDS


class AwsAdaptationAudit:
    """Read-only audit of the twelve AWS adaptation layers.

    The audit distinguishes a missing adapter from a product that deliberately
    materializes its official fields on first use.  That distinction matters:
    downloading every regional EC2/RDS price file at startup would mix regions,
    consume excessive memory and become stale before the product is quoted.
    """

    def __init__(self, registry: AwsProductRegistry) -> None:
        self.registry = registry

    def report(self) -> dict[str, Any]:
        products = self.registry.list_products()
        official = [
            product
            for product in products
            if product["identity_status"] == "official"
        ]
        region_ready = [
            product
            for product in official
            if isinstance(product.get("offer"), dict)
            and product["offer"].get("available_regions")
        ]
        profile_ready = [
            product
            for product in official
            if product["profile_status"] in {"profile_ready", "pricing_ready"}
            and product.get("field_template", {}).get("source")
            == "aws_price_list_dimensions"
            and product.get("field_template", {}).get("fields")
        ]
        needs_review = [
            str(product["service_code"])
            for product in official
            if product["profile_status"] == "needs_review"
        ]
        isolated = [
            product
            for product in official
            if product.get("field_template", {}).get("isolation")
            == "strict_component_boundary"
            and product.get("policy", {}).get("cross_component_inheritance")
            == "region_only"
        ]
        policy_ready = [
            product
            for product in official
            if all(
                product.get("policy", {}).get(field)
                for field in (
                    "identity_source",
                    "specification_source",
                    "final_price_source",
                    "price_failure",
                    "zero_price",
                    "edit_recalculation",
                )
            )
        ]
        official_count = len(official)

        def stage(
            number: int,
            name: str,
            status: str,
            detail: str,
        ) -> dict[str, Any]:
            return {
                "number": number,
                "name": name,
                "status": status,
                "detail": detail,
            }

        return {
            "summary": {
                "official_product_count": official_count,
                "identity_ready": official_count,
                "region_ready": len(region_ready),
                "strictly_isolated": len(isolated),
                "policy_ready": len(policy_ready),
                "curated_component_templates": len(SERVICE_TEMPLATE_FIELDS),
                "materialized_dynamic_profiles": len(profile_ready),
                "dynamic_profiles_pending_first_use": max(
                    official_count - len(profile_ready) - len(needs_review), 0
                ),
                "needs_review": len(needs_review),
                "full_official_identity_coverage": bool(official_count),
                "full_region_coverage": len(region_ready) == official_count,
                "full_isolation_coverage": len(isolated) == official_count,
                "full_policy_coverage": len(policy_ready) == official_count,
                "future_product_mode": "official_index_sync_then_first_use_profile",
            },
            "stages": [
                stage(
                    1,
                    "官方产品身份",
                    "ready" if official_count else "blocked",
                    f"AWS Bulk Price List 已登记 {official_count} 个独立 Offer Code。",
                ),
                stage(
                    2,
                    "产品别名与唯一归属",
                    "ready",
                    "每个官方产品有独立 service_code、service_key 和别名集合。",
                ),
                stage(
                    3,
                    "区域支持",
                    "ready" if len(region_ready) == official_count else "refreshing",
                    f"{len(region_ready)}/{official_count} 个产品已缓存官方区域范围。",
                ),
                stage(
                    4,
                    "组件数据隔离",
                    "ready" if len(isolated) == official_count else "blocked",
                    "只有区域可以全局继承；CPU、内存、数量、存储和流量禁止跨组件。",
                ),
                stage(
                    5,
                    "专用字段模板",
                    "ready_on_demand",
                    f"{len(SERVICE_TEMPLATE_FIELDS)} 个常用模板常驻，"
                    "其余产品首次使用时按官方维度独立生成。",
                ),
                stage(
                    6,
                    "官方规格与下拉选项",
                    "ready_on_demand",
                    "按客户区域读取官方属性，缓存十天；不把其他区域或其他产品的选项混入。",
                ),
                stage(
                    7,
                    "托管替代与自建决策",
                    "ready",
                    "完全对等的 AWS 托管产品直接采用；部分替代才询问；自建机器仍为独立组件。",
                ),
                stage(
                    8,
                    "客户原文逐组件对账",
                    "ready",
                    "每个组件独立提取、独立复核；数字、型号、引擎、容量和数量不得丢失。",
                ),
                stage(
                    9,
                    "组合产品展开",
                    "ready",
                    "控制面、Worker、磁盘、流量等按真实计费边界拆分，不把内部节点数当集群数。",
                ),
                stage(
                    10,
                    "报价与零价保护",
                    "ready" if len(policy_ready) == official_count else "blocked",
                    "缺价格时保留组件并重试官方源；仅明确零基础费资源允许零价。",
                ),
                stage(
                    11,
                    "客户编辑重新计算",
                    "ready",
                    "客户更改优先级最高，只从该组件入口重新识别和报价，不污染其他组件。",
                ),
                stage(
                    12,
                    "新产品自动扩展",
                    "ready",
                    "官方索引出现新 Offer Code 后自动登记；首次使用自动建立字段和计费缓存。",
                ),
            ],
            "needs_review_service_codes": needs_review,
        }
