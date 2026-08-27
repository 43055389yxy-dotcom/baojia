from __future__ import annotations

import json
import threading
from pathlib import Path

AZURE_CORE_PROMPT = """你是 Microsoft Azure 零售价格报价的需求整理员。
把客户原文整理成 Azure Retail Prices API 可查询的严格结构化数据。只理解需求，不编造 SKU、不计算价格。

刚性规则：
1. Azure 与 AWS 是两套完全独立的产品体系；不得输出任何 AWS 服务名、区域、实例型号或计费字段。
2. 客户明确给出 Azure SKU 时原样保留；只给 vCPU/内存时保留规格，由 Azure 官方价格目录选择满足要求且零售价最低的 SKU。
3. 客户未说明的可选功能省略；未说明用量的按量项目只展示官方最小计费单位单价，不虚构月用量。
4. 只有需求冲突、明确 SKU 在目标区域不可用、或缺少会改变价格的核心信息时才要求客户确认。
5. 每项服务保留客户原话，区域统一使用 Azure armRegionName；不得把 displayName 当作 armRegionName。
6. 输出前核对服务、区域、SKU、数量、操作系统、磁盘、数据库层级、存储与出站流量，禁止遗漏或新增服务。
7. 不限制 Azure 服务种类。即使是规则库中没见过的新组件，也必须保留为独立组件；service 使用稳定小写标识，calculator_service_name 填写 Microsoft Retail Prices 目录中的官方服务名称，交给官方目录自动建档，禁止改成已有但无关的服务。
8. 首轮只保留会影响 Azure 选型或价格的事实。联系人、公司、项目背景、交付说明、
备注和宣传性描述不得进入报价字段；SKU/型号、区域、数量、vCPU、内存、操作系统、
容量、性能层级、流量、请求量、运行时长和购买方式属于计价事实，禁止删除。
"""

AZURE_GENERIC_SERVICE_PROMPT = """【其他 Azure 组件通用规则】
这是一个尚未建立专用模板的 Azure 组件。先识别 Microsoft 官方产品名称，再仅提取客户明确给出的 SKU、层级、月用量、计费单位、存储、请求量和出站流量。
不得因为没有专用插件而丢弃组件，也不得选择最便宜但语义无关的 Meter。程序会使用 Retail Prices 官方 serviceName、armSkuName、meterName 和 unitOfMeasure 自动建立只读报价档案；若计费维度不能唯一确定，必须给客户官方候选项。
"""

AZURE_SERVICE_PROMPTS: dict[str, str] = {
    "azure_vm": """【Azure Virtual Machines】字段：arm_sku_name, vcpu, memory_gib, operating_system, quantity, hours_per_month, region。计算费用与 Managed Disks、Bandwidth 分开。客户指定 SKU 时原样保留。""",
    "managed_disks": """【Azure Managed Disks】字段：disk_type, disk_size_gib, quantity, region, iops, throughput_mbps。系统盘和数据盘分别保留；未指定性能时不添加额外性能。""",
    "azure_sql": """【Azure SQL / Azure Database】字段：engine, deployment_model, service_tier, compute_model, vcore, memory_gib, storage_gib, region。不得把 SQL Database、Managed Instance、MySQL 和 PostgreSQL 混为同一产品。""",
    "azure_cache": """【Azure Managed Redis / Azure Cache】字段：service_tier, sku_name, capacity, memory_gib, replicas, region。没有精确档位时由官方目录给出相邻低档和高档供客户选择。""",
    "blob_storage": """【Azure Blob Storage】字段：access_tier, redundancy, storage_gib, operations, data_retrieval_gib, region。未给请求量时只保留存储用量。""",
    "load_balancer": """【Azure Load Balancer / Application Gateway】先识别客户需要四层还是七层能力，再分别查询对应服务；不得互相替代。""",
    "front_door": """【Azure Front Door / CDN】字段：sku_name, data_transfer_out_gib, requests, region_scope。未给用量时只展示官方单位参考价。""",
    "bandwidth": """【Azure Bandwidth】字段：data_transfer_out_gib, source_region, destination_zone。入站流量默认不收费但不得虚构出站量。""",
    "aks": """【Azure Kubernetes Service】控制面与工作节点分开；工作节点按 Azure VM SKU、节点数、磁盘和运行小时报价。""",
    "monitor": """【Azure Monitor / Log Analytics】字段：log_ingestion_gib, retention_days, metrics。未给日志量时只展示最小计费单位单价。""",
}

_OVERRIDE_PATH = Path(__file__).resolve().parents[2] / ".cache" / "azure_prompt_overrides.json"
_LOCK = threading.RLock()


def _defaults() -> dict[str, str]:
    return {"azure_intake_format": AZURE_CORE_PROMPT, "azure_generic_service": AZURE_GENERIC_SERVICE_PROMPT, **{
        f"azure_{key}": value for key, value in AZURE_SERVICE_PROMPTS.items()
    }}


def _load_overrides() -> dict[str, str]:
    try:
        value = json.loads(_OVERRIDE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    defaults = _defaults()
    return {str(key): str(content) for key, content in value.items()
            if key in defaults and isinstance(content, str) and content.strip()}


def update_azure_prompt_text(key: str, content: str) -> None:
    defaults = _defaults()
    if key not in defaults:
        raise KeyError(key)
    cleaned = content.strip()
    if not cleaned or len(cleaned) > 50000:
        raise ValueError("提示词内容不能为空，且不能超过 50,000 字符")
    with _LOCK:
        overrides = _load_overrides()
        if cleaned == defaults[key].strip():
            overrides.pop(key, None)
        else:
            overrides[key] = cleaned
        _OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = _OVERRIDE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(_OVERRIDE_PATH)


def azure_prompt_library_payload() -> dict[str, object]:
    overrides = _load_overrides()
    items = [{
        "key": "azure_intake_format",
        "title": "Azure 首轮需求整理",
        "category": "Microsoft Azure · 核心流程",
        "order": 1,
        "content": overrides.get("azure_intake_format", AZURE_CORE_PROMPT),
        "is_overridden": "azure_intake_format" in overrides,
    }]
    for order, (key, content) in enumerate(AZURE_SERVICE_PROMPTS.items(), start=10):
        items.append({
            "key": f"azure_{key}",
            "title": key.replace("_", " ").title(),
            "category": "Microsoft Azure · 服务规则",
            "order": order,
            "content": overrides.get(f"azure_{key}", content),
            "is_overridden": f"azure_{key}" in overrides,
        })
    items.append({
        "key": "azure_generic_service",
        "title": "其他 Azure 组件通用规则",
        "category": "Microsoft Azure · 扩展组件",
        "order": 99,
        "content": overrides.get("azure_generic_service", AZURE_GENERIC_SERVICE_PROMPT),
        "is_overridden": "azure_generic_service" in overrides,
    })
    return {
        "provider": "azure",
        "items": items,
        "usage": "此页面只维护 Microsoft Azure 需求整理规则，不会影响 AWS 提示词。",
    }
