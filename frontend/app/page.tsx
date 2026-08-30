"use client";

import { FormEvent, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ConfigurationOptionPicker, type ConfigurationChoice } from "./components/configuration-option-picker";

type Health = { status: string; awsAccount?: string; calculatorReady?: boolean; aiProvider?: string; pricingMode?: string };
type JobEvent = { stage: string; message: string; time: string };
type ActivityChannel = {
  id: string;
  name: string;
  message: string;
  history: string[];
  state: "running" | "repair" | "blocked" | "done";
  order: number;
  updatedAt: string;
};
type ComponentRetryStatus = {
  componentIds: number[];
  attempt: number;
  remainingSeconds: number;
  phase: "waiting" | "running";
};

function serviceActivityLogs(name: string): string[] {
  const normalized = name.toLowerCase();
  if (normalized.includes("ec2")) return ["识别实例系列", "核对区域可用性", "整理处理器与内存组合", "筛选处理器架构", "检查操作系统兼容性", "分析云硬盘配置", "核对网络基线", "验证租用方式", "读取按需计费维度", "准备预付费价格矩阵", "校验官方产品编号", "实例候选集准备完成"];
  if (normalized.includes("rds")) return ["识别数据库引擎", "确认引擎版本", "核对多可用区拓扑", "查询数据库实例类型", "关联处理器与内存", "匹配存储类型", "检查性能上限", "核对备份设置", "识别授权计费维度", "读取区域价格", "校验官方产品编号", "数据库候选集准备完成"];
  if (normalized.includes("elasticache") || normalized.includes("redis")) return ["识别缓存引擎", "核对 Redis 与 Valkey 能力", "解析主从拓扑", "隔离分片数量", "匹配节点内存", "检查故障转移策略", "核对可用区部署", "查询预留节点", "扫描网络计费维度", "读取区域价格", "校验缓存产品编号", "缓存候选集准备完成"];
  if (normalized.includes("msk") || normalized.includes("kafka")) return ["识别 Kafka 工作负载", "锁定 Amazon MSK 映射", "解析 Broker 拓扑", "隔离集群数量", "恢复 Broker 数量", "核对每节点云硬盘", "检查吞吐量配置", "验证可用区分布", "查询实例类型", "匹配存储计费维度", "校验 MSK 产品编号", "MSK 候选集准备完成"];
  if (normalized.includes("s3") || normalized.includes("storage")) return ["识别存储类型", "规范化对象容量", "转换容量单位", "拆分读写请求", "识别流量方向", "分析生命周期策略", "扫描取回计费维度", "检查复制策略", "读取区域请求价格", "查询存储单价", "校验 S3 产品编号", "S3 计费维度准备完成"];
  if (normalized.includes("eks") || normalized.includes("kubernetes")) return ["识别容器集群需求", "隔离托管控制面", "识别工作节点池", "计算集群与节点总数", "匹配 EC2 节点配置", "核对系统盘", "检查版本支持", "验证区域端点", "计算控制面时长", "关联工作节点目录", "校验 EKS 拓扑", "EKS 计费维度准备完成"];
  if (normalized.includes("opensearch")) return ["解析搜索节点拓扑", "拆分数据与主节点角色", "恢复节点数量", "关联处理器与内存", "核对每节点云硬盘", "分析索引工作负载", "验证高可用布局", "检查分层存储能力", "查询区域型号", "匹配存储计费维度", "校验 OpenSearch 产品编号", "搜索服务候选集准备完成"];
  if (normalized.includes("cloudfront")) return ["读取边缘节点网络", "确认全球服务范围", "匹配出站流量阶梯", "识别 HTTPS 请求量", "检查源站路由", "分析缓存行为", "确认价格等级", "匹配缓存刷新计费", "检查源站防护能力", "查询边缘单价", "校验 CloudFront 产品编号", "CloudFront 价格准备完成"];
  if (normalized.includes("waf")) return ["读取访问控制列表", "确认服务范围", "识别托管规则组", "统计自定义规则", "规范化请求量", "匹配机器人防护", "扫描验证码计费维度", "检查区域范围", "核对 CloudFront 关联", "查询 WAF 单价", "校验安全策略", "WAF 价格准备完成"];
  return ["识别产品身份", "锁定组件数据边界", "复核客户原始要求", "分析 AWS 托管覆盖范围", "比较功能适配程度", "解析自建部署拓扑", "恢复节点数量", "恢复处理器与内存", "规范化存储单位", "检查架构决策", "选择官方目录路径", "安全报价路径准备完成"];
}

function activityLogStream(channel: ActivityChannel): string[] {
  const isPricingChannel = channel.history.some((message) =>
    /报价队列|官方产品|计费项|月费|报价计算/.test(message),
  );
  const pricingLogs = isPricingChannel ? [
    "初始化独立报价上下文",
    "同步区域产品目录索引",
    "查询 AWS Price List Service Code",
    "扫描产品属性与 SKU 约束",
    "校验区域与部署方式兼容性",
    "关联型号、规格和购买方式",
    "展开官方计费维度集合",
    "规范化月度用量与单位",
    "生成 BCM Calculator Usage Lines",
    "执行重复计费项去重检查",
    "核对数量与单项用量边界",
    "提交官方价格计算请求",
    "读取按需与预付价格结果",
    "执行币种和月费精度校验",
    "检查零价格与缺失维度",
    "写入组件级报价结果",
  ] : [];
  if (channel.state === "blocked") {
    return [
      `${channel.name} 独立核验任务`,
      ...channel.history.slice(-5),
      "本轮校验已停止，未生成客户链接",
    ];
  }
  if (channel.state === "done") {
    return [
      `${channel.name} 独立核验任务`,
      ...channel.history.slice(-5),
      ...(isPricingChannel ? ["官方价格返回值校验通过", "组件月费结果已安全写入"] : ["官方规格校验已完成"]),
      "本模块已停止运行",
    ];
  }
  return [
    `${channel.name} 安全处理通道已启动`,
    `独立任务编号 ${channel.id.slice(-8)}`,
    "区域目录同步完成",
    ...serviceActivityLogs(channel.name),
    ...pricingLogs,
    `${channel.name} 数据边界已锁定`,
    ...channel.history,
    "持续轮询本组件最新状态",
    "等待下一条真实处理记录",
    `${channel.name} 处理通道保持运行`,
  ];
}
type QuoteError = { code: string; message: string; details?: Record<string, unknown> };
type Preview = {
  draft_id: string;
  customer_summary?: string;
  confirmation_token?: string | null;
  confirmation_text?: string | null;
  confirmation_items?: ConfirmationItem[];
  configuration_review_required?: boolean;
  sales_validation_required?: boolean;
  sales_validation_message?: string | null;
  notices?: string[];
  selections?: PreviewSelection[];
  execution_trace?: { stage: string; message: string; status?: string }[];
  expert_review?: ExpertReview | null;
};

function previewHasUnfinishedComponents(preview: Preview | null): boolean {
  if (!preview) return false;
  return Boolean(
    preview.sales_validation_required
    || (preview.selections ?? []).some(
      (selection) => {
        const action = previewSelectionNextAction(selection);
        return action === "retry_component" || action === "internal_block";
      },
    )
  );
}

type ExpertReview = {
  run_id: string;
  provider: string;
  mode: "single_pass_read_only";
  status: "ready" | "awaiting_customer" | "partial";
  ai_calls: number;
  components: number;
  official_checks: number;
  customer_questions: number;
  unsupported_components: number;
  safeguards: string[];
};
type PreviewCandidate = {
  model: string;
  specifications: Record<string, unknown>;
  is_default?: boolean;
};
type PreviewSelection = {
  component_id?: string;
  component_number?: string | null;
  parent_component_id?: string | null;
  parent_component_number?: string | null;
  parent_display_name?: string | null;
  service: string;
  display_name: string;
  region?: string;
  quantity?: number;
  requirements?: Record<string, unknown>;
  source_text?: string;
  requested_model?: string | null;
  selected_model?: string | null;
  selection_reason?: string;
  candidates: PreviewCandidate[];
  requires_confirmation?: boolean;
  confirmation_reason?: string | null;
  status?: "ready" | "customer_issue" | "technical_issue" | "unsupported";
  issue_message?: string | null;
  issue_code?: string | null;
  issue_category?: "retryable" | "compatibility" | "catalog_mapping" | "system_configuration" | "unsupported" | null;
  next_action?: PreviewNextAction;
};
type PreviewNextAction = "none" | "retry_component" | "request_customer" | "internal_block";

function previewSelectionNextAction(selection: PreviewSelection): PreviewNextAction {
  // ``next_action`` is authoritative for new responses.  The fallback keeps
  // old confirmation sessions readable while they age out of local storage.
  if (selection.next_action) return selection.next_action;
  if (selection.requires_confirmation || selection.status === "customer_issue") {
    return "request_customer";
  }
  if (selection.status === "technical_issue" && selection.issue_category === "retryable") {
    return "retry_component";
  }
  if (selection.status === "technical_issue" || selection.status === "unsupported") {
    return "internal_block";
  }
  return "none";
}
type ConfirmationItem = {
  question: string;
  answer_key?: string | null;
  options: ConfigurationChoice[];
  component_id?: string | null;
  service?: string | null;
  selection_mode?: "text" | "buttons" | "catalog";
};

function confirmationAnswerKey(item: ConfirmationItem): string {
  return item.answer_key ?? item.question;
}
type SalesRegionOption = { code: string; label: string };
type SalesRegionPreflight = {
  detected_regions: string[];
  selected_region?: string | null;
  requires_confirmation: boolean;
  options: SalesRegionOption[];
};
type Selection = {
  component_id?: string | null;
  component_number?: string | null;
  parent_component_id?: string | null;
  parent_component_number?: string | null;
  parent_display_name?: string | null;
  service: string;
  display_name: string;
  region: string;
  model: string;
  quantity?: number;
  architecture: string;
  specifications: Record<string, unknown>;
  rationale: string;
  substitution_notice?: string | null;
  pricing_status?: "priced" | "reference_only" | "free" | "unpriced";
  pricing_issue_code?: string | null;
  pricing_notice?: string | null;
  remarks?: string[];
  reference_rates?: ReferenceRate[];
  usage_lines?: Array<{ key: string; amount: number; group?: string | null }>;
};

function hierarchyOrdered<T extends {
  component_id?: string | null;
  parent_component_id?: string | null;
}>(items: T[]): Array<{ item: T; originalIndex: number }> {
  const entries = items.map((item, originalIndex) => ({
    item,
    originalIndex,
    id: item.component_id ?? String(originalIndex),
  }));
  const knownIds = new Set(entries.map((entry) => entry.id));
  const children = new Map<string, typeof entries>();
  entries.forEach((entry) => {
    const parentId = entry.item.parent_component_id;
    if (!parentId || !knownIds.has(parentId) || parentId === entry.id) return;
    children.set(parentId, [...(children.get(parentId) ?? []), entry]);
  });
  const ordered: typeof entries = [];
  const visited = new Set<string>();
  const append = (entry: (typeof entries)[number]) => {
    if (visited.has(entry.id)) return;
    visited.add(entry.id);
    ordered.push(entry);
    (children.get(entry.id) ?? []).forEach(append);
  };
  entries
    .filter((entry) => !entry.item.parent_component_id
      || !knownIds.has(entry.item.parent_component_id))
    .forEach(append);
  entries.forEach(append);
  return ordered.map(({ item, originalIndex }) => ({ item, originalIndex }));
}
type ReferenceRate = {
  description: string;
  unit: string;
  unit_price: number;
  currency: string;
  service_code: string;
  usage_type: string;
  operation: string;
};
type PricedLine = {
  key: string;
  service_code: string;
  usage_type: string;
  operation: string;
  amount: number;
  unit?: string | null;
  cost: number;
  currency: string;
};
type Quote = {
  quote_id: string;
  customer_summary: string;
  selections: Selection[];
  priced_lines: PricedLine[];
  total_cost: number;
  upfront_cost: number;
  currency: string;
  notices: string[];
  share_url?: string | null;
  source_url?: string | null;
  calculator_details?: string[];
  pricing_scenarios?: PricingScenario[];
  is_partial?: boolean;
  incomplete_component_ids?: string[];
};
type PricingScenario = {
  label: string;
  pricing_mode: PricingMode | AzurePricingMode;
  reserved_term_years?: 1 | 3 | null;
  payment_option?: PaymentOption | null;
  quote_id: string;
  total_cost: number;
  upfront_cost: number;
  currency: string;
  priced_lines: PricedLine[];
  component_costs?: Record<string, number>;
  component_pricing_basis?: Record<string, "on_demand" | "reserved" | "on_demand_fallback">;
  is_partial?: boolean;
  incomplete_component_ids?: string[];
};
type Job = {
  job_id: string;
  kind?: "preview" | "quote";
  status: "queued" | "running" | "completed" | "failed";
  events: JobEvent[];
  result?: Quote | Preview | null;
  error?: QuoteError | null;
};
type PricingMode = "on_demand" | "standard_reserved" | "convertible_reserved";
type PaymentOption = "no_upfront" | "partial_upfront" | "all_upfront";
type AzurePricingMode = "pay_as_you_go" | "reservation" | "savings_plan" | "spot";
type AzurePaymentOption = "monthly" | "upfront";
type CloudProvider = "aws" | "azure";
const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/backend";
const CONFIRMATION_CONTEXT_KEY = "astraquote.aws.pending-confirmation.v1";
const QUOTE_JOB_CONTEXT_KEY = "astraquote.aws.pending-quote.v1";
const SALES_REGION_CONTEXT_KEY = "astraquote.aws.current-sales-region.v2";
const QUOTE_SESSION_STORAGE_PREFIXES = [
  "astraquote.aws.pending-",
  "astraquote.aws.current-sales-region",
  "astraquote:aws:addition:",
  "astraquote:aws:region-confirmation:",
];

function clearQuoteSessionStorage() {
  for (let index = window.sessionStorage.length - 1; index >= 0; index -= 1) {
    const key = window.sessionStorage.key(index);
    if (key && QUOTE_SESSION_STORAGE_PREFIXES.some((prefix) => key.startsWith(prefix))) {
      window.sessionStorage.removeItem(key);
    }
  }
}

function readSalesRegionContext(_provider: CloudProvider): string | null {
  void _provider;
  return window.sessionStorage.getItem(SALES_REGION_CONTEXT_KEY);
}

function writeSalesRegionContext(_provider: CloudProvider, region: string) {
  void _provider;
  window.sessionStorage.setItem(SALES_REGION_CONTEXT_KEY, region);
}

function clearSalesRegionContext(_provider: CloudProvider) {
  void _provider;
  window.sessionStorage.removeItem(SALES_REGION_CONTEXT_KEY);
}

type PendingConfirmationContext = {
  token: string;
  draftId: string;
  customerRequest: string;
  cloudProvider: CloudProvider;
  preview: Preview;
  answers?: Record<string, string>;
  latePricingConfirmation?: boolean;
};

const serviceNames: Record<string, string> = {
  ec2: "EC2 云服务器",
  rds: "RDS 数据库",
  redis: "ElastiCache",
  elasticache: "ElastiCache",
  elb: "应用负载均衡",
  alb: "应用负载均衡",
  nlb: "网络负载均衡",
  gwlb: "网关负载均衡",
  s3: "S3 对象存储",
  cloudfront: "CloudFront CDN",
  route53: "Route 53 DNS",
  waf: "AWS WAF",
  sqs: "Amazon SQS",
  ses: "Amazon SES",
  cloudwatch: "Amazon CloudWatch",
  backup: "AWS Backup",
  aws_backup: "AWS Backup",
  documentdb: "Amazon DocumentDB",
  msk: "Amazon MSK",
  eks: "Amazon EKS",
  opensearch: "Amazon OpenSearch Service",
  nat_gateway: "AWS NAT Gateway",
  vpc: "Amazon VPC",
  dms: "AWS DMS",
  kms: "AWS KMS",
  xray: "AWS X-Ray",
  cloud_map: "AWS Cloud Map",
  appconfig: "AWS AppConfig",
};

function serviceDisplayName(selection: { service: string; display_name: string }): string {
  if (/\baurora\b/i.test(selection.display_name)) return selection.display_name;
  if (
    ["elb", "alb", "nlb", "gwlb"].includes(selection.service)
    && /load\s*balancer|负载均衡/i.test(selection.display_name)
  ) return selection.display_name;
  if (
    selection.service === "ec2"
    && /(?:自建|用于|工作节点|worker\s*nodes?)/i.test(selection.display_name)
  ) return selection.display_name;
  return serviceNames[selection.service] ?? selection.display_name;
}

const specificationNames: Record<string, string> = {
  vCPU: "vCPU", memoryGiB: "内存", operatingSystem: "系统", tenancy: "租用方式",
  engine: "引擎", deploymentOption: "部署", storageType: "存储类型", storageGiB: "存储",
  customerDeployment: "客户确认架构", clusterMembers: "每集群数据库实例数",
  shards: "分片", replicasPerShard: "每分片副本", totalNodes: "节点", quantity: "数量",
  dataTransferOutGiB: "公网下行", processedBytesGiB: "处理流量",
  systemDiskGiB: "系统盘", volumeType: "磁盘类型", additional_ebs_volumes: "数据盘",
  hostedZones: "域名托管区", webACLs: "Web ACL", rules: "规则总数", requests: "每月请求总量",
  messages: "每月消息数", connectionMinutes: "每月连接分钟",
  throughputMbpsPerTiB: "每 TiB 吞吐量",
  data_in_gib: "每月写入", data_out_gib: "每月读取",
  throughput_mode: "吞吐模式", provisioned_throughput_mibps: "预置吞吐量",
  rulesPerWebACL: "每个 ACL 规则", requestsPerWebACL: "每个 ACL 每月请求",
  memory_mb: "函数内存", duration_ms: "平均执行时长",
  queueType: "队列类型", outboundMessages: "出站邮件", logIngestionGiB: "日志写入",
  customMetrics: "自定义指标",
  vcpu: "vCPU", memory_gib: "内存", operating_system: "系统", system_disk_gib: "系统盘",
  storage_gib: "单项存储", total_storage_gib: "总存储", system_disk_gib: "系统盘",
  total_system_disk_gib: "系统盘总容量", total_worker_system_disk_gib: "工作节点系统盘总容量",
  storage_gib_per_node: "每节点存储", storage_gib_per_broker: "每个 Broker 存储",
  storage_type: "存储类型", volume_type: "磁盘类型", purchase_option: "购买方式",
  utilization_percent: "使用率", detailed_monitoring: "详细监控", enhanced_monitoring: "增强监控",
  performance_insights: "Performance Insights", data_transfer_monitoring: "流量监控", multi_az: "Multi-AZ",
  deployment: "部署方式", cluster_members: "数据库实例数", broker_count: "Broker 节点", data_nodes: "数据节点", node_count: "节点",
  cluster_type: "集群类型", cluster_mode: "集群模式", replicas_per_shard: "每分片副本",
  backup_retention_days: "备份天数",
  load_balancer_type: "类型", data_transfer_out_gib: "公网下行", processed_bytes_gib: "处理流量",
  dataNodes: "数据节点", storageGiBPerNode: "每节点存储", brokerCount: "Broker 节点",
  storageGiBPerBroker: "每个 Broker 存储", storageClass: "存储类型",
  processedGiB: "处理流量", scheduledInvocations: "每月调用量",
  requested_sku: "Azure SKU", service_tier: "服务层级", compute_model: "计算模式",
  access_tier: "访问层", redundancy: "冗余方式", disk_type: "磁盘类型",
  disk_size_gib: "磁盘容量", vcore: "vCore", high_availability: "高可用",
  capacity_units: "容量单位", log_ingestion_gib: "日志写入", retention_days: "保留天数",
};

function formatAdditionalEbsVolumes(value: unknown): string | null {
  if (!Array.isArray(value)) return null;
  const descriptions = value.flatMap((entry) => {
    if (!entry || typeof entry !== "object") return [];
    const volume = entry as Record<string, unknown>;
    const size = Number(volume.size_gib);
    if (!Number.isFinite(size)) return [];
    const count = Math.max(1, Number(volume.count_per_instance) || 1);
    const type = typeof volume.volume_type === "string" ? volume.volume_type : "gp3";
    return [`${size.toLocaleString("zh-CN")} GiB ${type} × ${count} 块/台`];
  });
  return descriptions.length > 0 ? descriptions.join("；") : null;
}

function formatPreviewValue(key: string, value: unknown): string {
  if (typeof value === "boolean") return value ? "是" : "否";
  if (key === "additional_ebs_volumes") {
    return formatAdditionalEbsVolumes(value) ?? "未识别";
  }
  if (Array.isArray(value)) return value.join("、");
  if (value && typeof value === "object") return JSON.stringify(value);
  const normalizedValues: Record<string, string> = {
    on_demand: "按需付费", reserved: "预留实例", linux: "Linux", windows: "Windows",
    mysql: "MySQL", postgresql: "PostgreSQL", aurora_mysql: "Aurora MySQL", aurora_postgresql: "Aurora PostgreSQL", redis: "Redis", multi_az: "多可用区高可用",
    single_az: "单可用区", provisioned: "预置容量集群", serverless: "无服务器集群",
  };
  if (typeof value === "string" && normalizedValues[value.toLowerCase()]) {
    return normalizedValues[value.toLowerCase()];
  }
  const suffix = key.toLowerCase().includes("gib")
    ? " GiB"
    : key === "memory_mb"
      ? " MB"
      : key === "duration_ms"
        ? " ms"
        : key === "utilization_percent"
          ? "%"
          : "";
  return `${String(value)}${suffix}`;
}

function previewRequirementTags(selection: PreviewSelection): { label: string; value: string }[] {
  const ignored = new Set([
    "requested_model", "usage_lines", "reference_only", "reference_unit_only",
    "reference_lcu_unit_only", "system_default_assumption",
    "purchase_option", "reserved_term_years", "payment_option",
    "utilization_percent",
  ]);
  const selectedSpecifications = selection.candidates.find(
    (candidate) => candidate.model === selection.selected_model,
  )?.specifications ?? {};
  const displayRequirements = { ...(selection.requirements ?? {}) };
  if (typeof selectedSpecifications.vCPU === "number") displayRequirements.vcpu = selectedSpecifications.vCPU;
  if (typeof selectedSpecifications.memoryGiB === "number") displayRequirements.memory_gib = selectedSpecifications.memoryGiB;
  const entries = Object.entries(displayRequirements)
    .filter(([key, value]) => {
      if (key.startsWith("_") || ignored.has(key) || value === null || value === undefined || value === "") return false;
      if (["detailed_monitoring", "enhanced_monitoring", "performance_insights", "data_transfer_monitoring", "cluster_mode"].includes(key) && value === false) return false;
      if (key === "backup_retention_days" && Number(value) === 0) return false;
      return true;
    })
    .slice(0, 7)
    .map(([key, value]) => ({
      label: specificationNames[key] ?? key,
      value: formatPreviewValue(key, value),
    }));
  const quantityTag = selection.service === "msk"
    ? { label: "集群数量", value: `${selection.quantity ?? 1} 套` }
    : { label: "数量", value: String(selection.quantity ?? 1) };
  return [quantityTag, ...entries];
}

function questionMatchesSelection(item: ConfirmationItem, selection: PreviewSelection): boolean {
  if (item.component_id !== null && item.component_id !== undefined) {
    return item.component_id === selection.component_id;
  }
  const question = item.question.toLowerCase();
  const service = `${selection.service} ${selection.display_name}`.toLowerCase();
  if (/redis|elasticache|缓存/.test(question)) return /redis|elasticache/.test(service);
  if (/ec2|服务器|云主机/.test(question)) return service.includes("ec2");
  if (/rds|数据库|mysql|postgres|sql server/.test(question)) return service.includes("rds");
  if (/cloudfront|cdn/.test(question)) return service.includes("cloudfront");
  if (/s3|对象存储/.test(question)) return service.includes("s3");
  if (/负载均衡|alb|elb|nlb/.test(question)) return /elb|alb|load balancing/.test(service);
  if (/waf/.test(question)) return service.includes("waf");
  return false;
}

const formatMoney = (value: number, currency = "USD") =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);

const readableRequestError = (error: unknown, fallback: string) =>
  error instanceof DOMException && error.name === "AbortError"
    ? "需求检查耗时过长，系统已安全停止。本次尚未开始正式报价，请稍后重试。"
    : error instanceof TypeError && /fetch/i.test(error.message)
    ? "本机报价服务暂时未连接。请确认后端已启动，然后刷新页面重试。"
    : error instanceof Error ? error.message : fallback;

async function fetchWithTimeout(url: string, init: RequestInit, timeoutMs = 90000) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } finally {
    window.clearTimeout(timeout);
  }
}

async function cancelQuoteJob(jobId: string) {
  if (!/^aws-[a-z0-9]+$/i.test(jobId)) return;
  try {
    await fetchWithTimeout(`${API_BASE}/api/quote-jobs/${jobId}/cancel`, {
      method: "POST",
      cache: "no-store",
    }, 5000);
  } catch {
    // The generation guard still prevents a late response from touching the UI.
  }
}

const stageName: Record<string, string> = {
  queue: "任务调度",
  ai: "需求解析",
  ai_prompt: "智能分析",
  ai_response: "分析结果",
  ai_result: "配置清单",
  intake_start: "需求识别",
  intake_done: "识别完成",
  component_plan: "独立调度",
  component_start: "参数解析",
  component_done: "一致性核验",
  ai_repair: "定向修正",
  catalog: "产品目录同步",
  aws_start: "官方规格核验",
  aws_done: "规格核验完成",
  quote_component_waiting: "等待官方报价",
  aws_match_done: "官方计费项匹配",
  quote_done: "组件报价完成",
  review_options_start: "整理编辑选项",
  review_options_done: "确认页面准备",
  official_start: "Microsoft 官方核验",
  official_done: "Microsoft 核验完成",
  agent: "报价编排",
  aws: "官方产品匹配",
  browser: "官方数据通道",
  calculator: "成本核算",
  result: "计费结果",
  done: "流程完成",
  error: "流程中止",
};

function customerFacingNotices(notices: string[] = []) {
  const internalMarkers = [
    "客户未指定", "客户未提供", "没有恰好", "已直接选择", "最接近且不低于",
    "按满足已知规格", "从 AWS 官方目录选择", "适配器", "最低计费", "最低官方",
  ];
  return notices.filter((notice) => !internalMarkers.some((marker) => notice.includes(marker)));
}

function selectionPricingOrdinal(selection: Selection, fallbackIndex: number) {
  const componentIndex = Number(selection.component_id);
  return Number.isInteger(componentIndex) && componentIndex >= 0
    ? componentIndex + 1
    : fallbackIndex + 1;
}

function isSelectionPricedLine(key: string, selection: Selection, index: number) {
  const ordinal = selectionPricingOrdinal(selection, index);
  return new RegExp(`^(?:s|az)${ordinal}(?:l\\d+|commit)$`).test(key);
}

function serviceCost(quote: Quote, selection: Selection, index: number) {
  return (quote.priced_lines ?? [])
    .filter((line) => isSelectionPricedLine(line.key, selection, index))
    .reduce((sum, line) => sum + Number(line.cost || 0), 0);
}

function quoteScenarios(quote: Quote): PricingScenario[] {
  if (quote.pricing_scenarios?.length) return quote.pricing_scenarios;
  return [{
    label: "官方报价",
    pricing_mode: "on_demand",
    quote_id: quote.quote_id,
    total_cost: quote.total_cost,
    upfront_cost: quote.upfront_cost,
    currency: quote.currency,
    priced_lines: quote.priced_lines,
  }];
}

function scenarioServiceCost(scenario: PricingScenario, selection: Selection, index: number) {
  const componentId = selection.component_id ?? String(index);
  if (
    scenario.component_costs
    && Object.prototype.hasOwnProperty.call(scenario.component_costs, componentId)
  ) {
    return Number(scenario.component_costs[componentId] || 0);
  }
  return (scenario.priced_lines ?? [])
    .filter((line) => isSelectionPricedLine(line.key, selection, index))
    .reduce((sum, line) => sum + Number(line.cost || 0), 0);
}

function scenarioComponentIsIncomplete(
  scenario: PricingScenario,
  selection: Selection,
  index: number,
) {
  const componentId = selection.component_id ?? String(index);
  return Boolean(scenario.incomplete_component_ids?.includes(componentId));
}

function formatUnitRate(value: number, currency = "USD") {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 8,
  }).format(value);
}

function referenceRateText(selection: Selection) {
  return (selection.reference_rates ?? [])
    .map((rate) => `${rate.description}：${formatUnitRate(rate.unit_price, rate.currency)}/${rate.unit}`)
    .join("；");
}

function compactReferenceRateText(selection: Selection) {
  return (selection.reference_rates ?? [])
    .map((rate) => `${formatUnitRate(rate.unit_price, rate.currency)}/${rate.unit}`)
    .join("\n");
}

function quotationRemark(selection: Selection) {
  const remarks: string[] = [];
  if (selection.pricing_status === "unpriced") {
    remarks.push(selection.pricing_notice || "当前未取得官方价格，本项未计入合计");
  }
  if (selection.parent_component_number) {
    remarks.push(
      `由 ${selection.parent_component_number} · ${selection.parent_display_name ?? "父组件"} 衍生`,
    );
  }
  const selfHostedProduct = selection.service === "ec2"
    ? selection.display_name.match(/自建\s*([^）)]+)/i)?.[1]?.trim()
    : undefined;
  if (selfHostedProduct) {
    remarks.push(
      `由“${selfHostedProduct}”部署需求衍生，用于在 Amazon EC2 上运行 ${selfHostedProduct}；这里计算的是所列云服务器与磁盘资源，不是 AWS 托管版服务。`,
    );
  }
  const internalMarkers = [
    "客户未指定", "客户未提供", "按最低", "最低官方", "最低计费", "本次按",
    "暂未接入", "适配器", "不计入月费", "仅展示", "系统默认", "官方目录",
    "没有恰好", "已直接选择", "最接近且不低于", "按满足已知规格",
  ];
  const customerSafeSentences = (item: string) => {
    const rewritten = item
      .replace(
        /本项仅含 EKS 集群控制平面；实际运行容器还需 EC2 或 Fargate 工作节点，客户未提供节点配置，本次未计入。/g,
        "本项仅含 EKS 集群控制平面；实际运行容器还需另配 EC2 或 Fargate 工作节点。",
      )
      .replace(
        /CloudFront 需配置源站；客户未提供源站服务，本次未新增或计入源站费用。/g,
        "CloudFront 需另行配置源站，源站费用另计。",
      )
      .replace(
        /本项不自动包含域名注册、健康检查等客户未指定的附加费用。/g,
        "域名注册与健康检查费用另计。",
      );
    return rewritten
      .split(/(?<=[。；])/u)
      .map((part) => part.trim())
      .filter((part) => part && !internalMarkers.some((marker) => part.includes(marker)));
  };
  for (const item of selection.remarks ?? []) {
    remarks.push(...customerSafeSentences(item));
  }
  if (selection.substitution_notice) {
    remarks.push(...customerSafeSentences(selection.substitution_notice));
  }
  if (selection.reference_rates?.length) {
    remarks.push("按官方单位价格列示");
  }
  return [...new Set(remarks)].join("；") || "-";
}

function compactSpecifications(selection: Selection) {
  const specifications = { ...selection.specifications };
  // Keep already-generated quotes readable after the backend fix: older RDS
  // selections priced storage in usage_lines but omitted its capacity from
  // the display object.
  if (selection.service === "rds" && specifications.storageGiB == null) {
    const storageLine = selection.usage_lines?.find(
      (line) => line.key === "rdsstg" || line.group === "rds-storage",
    );
    if (storageLine) {
      const isAurora = /aurora/i.test(`${selection.display_name} ${selection.model}`);
      const instanceCount = Math.max(1, selection.quantity ?? 1);
      specifications.storageGiB = isAurora
        ? storageLine.amount
        : storageLine.amount / instanceCount;
    }
  }
  const hiddenSpecificationKeys = new Set([
    "requested_model",
    "system_default_assumption",
    "calculator_adjustment_notices",
    "ebs_storage_breakdown",
    "purchase_option",
    "reserved_term_years",
    "payment_option",
    "utilization_percent",
    "tenancy",
    "total_system_disk_gib",
    "totalSystemDiskGiB",
  ]);
  return Object.entries(specifications)
    .filter(([key, value]) => (
      value != null
      && value !== false
      && !key.startsWith("_")
      && !hiddenSpecificationKeys.has(key)
    ))
    .map(([key, value]) => {
      if (key === "additional_ebs_volumes") {
        const volumes = formatAdditionalEbsVolumes(value);
        if (volumes) return `${specificationNames[key] ?? key}: ${volumes}`;
      }
      const numericValue = typeof value === "number" ? value.toLocaleString("zh-CN") : String(value);
      const suffix = key.toLowerCase().includes("gib")
        ? " GiB"
        : key === "memory_mb"
          ? " MB"
          : key === "duration_ms"
            ? " ms"
        : key === "requests" || key === "requestsPerWebACL"
          ? " 次/月"
          : "";
      return `${specificationNames[key] ?? key}: ${numericValue}${suffix}`;
    })
    .join(" · ");
}

export default function Home() {
  const [cloudProvider] = useState<CloudProvider>("aws");
  const [health, setHealth] = useState<Health | null>(null);
  const [requirement, setRequirement] = useState("");
  const [job, setJob] = useState<Job | null>(null);
  const [copied, setCopied] = useState(false);
  const [customerLinkModalOpen, setCustomerLinkModalOpen] = useState(false);
  const [portalReady, setPortalReady] = useState(false);
  const [confirmationReply, setConfirmationReply] = useState("");
  const [confirmationAnswers, setConfirmationAnswers] = useState<string[]>([]);
  const [logExpanded, setLogExpanded] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [activityLogTick, setActivityLogTick] = useState(0);
  const [pricingMode, setPricingMode] = useState<PricingMode | null>("standard_reserved");
  const [includeOnDemandScenario, setIncludeOnDemandScenario] = useState(true);
  const [reservedTermYears, setReservedTermYears] = useState<(1 | 3)[]>([1, 3]);
  const [paymentOption, setPaymentOption] = useState<PaymentOption>("all_upfront");
  const [utilizationPercent, setUtilizationPercent] = useState(100);
  const [azurePricingMode, setAzurePricingMode] = useState<AzurePricingMode>("pay_as_you_go");
  const [azureTermYears, setAzureTermYears] = useState<1 | 3>(1);
  const [azurePaymentOption, setAzurePaymentOption] = useState<AzurePaymentOption>("monthly");
  const [previewDraftId, setPreviewDraftId] = useState<string | null>(null);
  const [confirmationToken, setConfirmationToken] = useState<string | null>(null);
  const [salesReview, setSalesReview] = useState<Preview | null>(null);
  const [salesRegion, setSalesRegion] = useState<string | null>(null);
  const [salesRegionOptions, setSalesRegionOptions] = useState<SalesRegionOption[]>([]);
  const [salesRegionPromptOpen, setSalesRegionPromptOpen] = useState(false);
  const [salesRegionChecking, setSalesRegionChecking] = useState(false);
  const [quoteResetting, setQuoteResetting] = useState(false);
  const [componentRetryStatus, setComponentRetryStatus] = useState<ComponentRetryStatus | null>(null);
  const [receivedCustomerAnswers, setReceivedCustomerAnswers] = useState<Record<string, string>>({});
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const confirmationTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const confirmationPollGeneration = useRef<number | null>(null);
  const confirmationRecoveryStarted = useRef(false);
  const quoteRecoveryStarted = useRef(false);
  const lateConfirmationTokenStarted = useRef<string | null>(null);
  const directQuotePreflightRecoveryStarted = useRef<string | null>(null);
  const identityRecoveryAttempted = useRef(false);
  const previewPollFailures = useRef<Map<string, number>>(new Map());
  const previewRestartedJobs = useRef<Set<string>>(new Set());
  const componentRetryAttempts = useRef<Map<string, number>>(new Map());
  const componentRetryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const openedCustomerLinkVersion = useRef<string | null>(null);
  // Every asynchronous response belongs to the generation that created it.
  // "重新报价" advances the generation, so a late response from an older
  // preview, confirmation poll or pricing job can no longer restore stale data.
  const quoteRunGeneration = useRef(0);
  // Async preview, customer-confirmation polling and official pricing can all
  // finish out of order. Keep the workflow monotonic so an older response can
  // never replace a newer official-pricing screen.
  const workflowPhase = useRef<"idle" | "preview" | "confirmation" | "quote">("idle");
  const timeline = useRef<HTMLOListElement | null>(null);
  const requirementInput = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    setPortalReady(true);
  }, []);

  useEffect(() => {
    const savedRegion = readSalesRegionContext(cloudProvider);
    setSalesRegion(savedRegion);
  }, [cloudProvider]);

  useEffect(() => {
    let active = true;
    const checkHealth = () => {
      fetch(`${API_BASE}/api/health`, { cache: "no-store" })
        .then((response) => {
          if (!response.ok) throw new Error("health check failed");
          return response.json();
        })
        .then((payload) => {
          if (active) setHealth(payload);
        })
        .catch(() => {
          if (active) setHealth({ status: "offline" });
        });
    };
    checkHealth();
    // The backend caches the external AWS probe.  A slower UI heartbeat is
    // enough to detect a stopped local server without flooding AWS while a
    // real quote is being calculated.
    const healthTimer = window.setInterval(checkHealth, 30000);
    return () => {
      active = false;
      window.clearInterval(healthTimer);
      if (timer.current) clearTimeout(timer.current);
      if (confirmationTimer.current) clearTimeout(confirmationTimer.current);
    };
  }, []);

  useEffect(() => {
    if (confirmationRecoveryStarted.current) return;
    confirmationRecoveryStarted.current = true;
    try {
      const recoveryGeneration = quoteRunGeneration.current;
      const saved = window.sessionStorage.getItem(CONFIRMATION_CONTEXT_KEY);
      if (!saved) return;
      const context = JSON.parse(saved) as PendingConfirmationContext;
      if (
        !context.token
        || !context.draftId
        || !context.customerRequest
        || context.cloudProvider !== cloudProvider
      ) {
        window.sessionStorage.removeItem(CONFIRMATION_CONTEXT_KEY);
        return;
      }
      setRequirement(context.customerRequest);
      workflowPhase.current = "confirmation";
      setPreviewDraftId(context.draftId);
      setConfirmationToken(context.token);
      setSalesReview(context.latePricingConfirmation ? null : context.preview);
      setReceivedCustomerAnswers(context.answers ?? {});
      setJob({ job_id: "confirmation-waiting", status: "failed", events: [], error: null });
      confirmationTimer.current = setTimeout(
        () => pollConfirmation(
          context.token,
          context.draftId,
          context.customerRequest,
          context.cloudProvider,
          recoveryGeneration,
        ),
        250,
      );
    } catch {
      window.sessionStorage.removeItem(CONFIRMATION_CONTEXT_KEY);
    }
  // Recovery runs once on mount; pollConfirmation is intentionally read from the current component closure.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (quoteRecoveryStarted.current) return;
    quoteRecoveryStarted.current = true;
    try {
      const recoveryGeneration = quoteRunGeneration.current;
      if (window.sessionStorage.getItem(CONFIRMATION_CONTEXT_KEY)) return;
      const saved = window.sessionStorage.getItem(QUOTE_JOB_CONTEXT_KEY);
      if (!saved) return;
      const context = JSON.parse(saved) as {
        jobId?: string;
        customerRequest?: string;
        cloudProvider?: CloudProvider;
        draftId?: string;
      };
      if (!context.jobId || !context.customerRequest || context.cloudProvider !== cloudProvider) {
        window.sessionStorage.removeItem(QUOTE_JOB_CONTEXT_KEY);
        return;
      }
      if (context.draftId) setPreviewDraftId(context.draftId);
      workflowPhase.current = "quote";
      setRequirement(context.customerRequest);
      setJob({
        job_id: context.jobId,
        kind: "quote",
        status: "queued",
        events: [{ stage: "recovery", message: "正在恢复报价进度", time: "刚刚" }],
      });
      void poll(context.jobId, [
        { stage: "recovery", message: "正在恢复报价进度", time: "刚刚" },
      ], recoveryGeneration);
    } catch {
      window.sessionStorage.removeItem(QUOTE_JOB_CONTEXT_KEY);
    }
  // Recovery runs once on mount; poll is intentionally read from the current component closure.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!confirmationToken || !previewDraftId) return;
    const pollingGeneration = quoteRunGeneration.current;
    const checkConfirmation = () => {
      if (document.visibilityState === "hidden" || workflowPhase.current === "quote") return;
      void pollConfirmation(
        confirmationToken,
        previewDraftId,
        requirement,
        cloudProvider,
        pollingGeneration,
      );
    };
    const handleVisibility = () => {
      if (document.visibilityState === "visible") checkConfirmation();
    };
    checkConfirmation();
    const interval = window.setInterval(checkConfirmation, 2200);
    window.addEventListener("focus", checkConfirmation);
    document.addEventListener("visibilitychange", handleVisibility);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener("focus", checkConfirmation);
      document.removeEventListener("visibilitychange", handleVisibility);
    };
  // The poller reads the current workflow through refs and is intentionally
  // restarted only when the persisted confirmation context changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [confirmationToken, previewDraftId, requirement, cloudProvider]);

  const running = job?.status === "queued" || job?.status === "running";
  const retryActivityVisible = Boolean(componentRetryStatus);
  const internalValidationPending = previewHasUnfinishedComponents(salesReview);
  const latest = useMemo(() => job?.events.at(-1), [job]);
  const liveComponentActivity = useMemo(() => {
    const channels = new Map<string, ActivityChannel>();
    const templateDone = new Set<string>();
    const officialDone = new Set<string>();
    let total = 0;
    for (const [order, event] of (job?.events ?? []).entries()) {
      const plan = event.stage === "component_plan"
        ? event.message.match(/(?:共|已建立)\s*(\d+)\s*(?:个组件|项)/)
        : null;
      if (plan) total = Number(plan[1]);
      const match = event.message.match(/^组件\s*(\d+)｜([^｜]+)｜(.+)$/);
      if (!match) continue;
      const [, id, name, message] = match;
      const state = event.stage === "ai_repair"
        ? "repair"
        : event.stage === "component_done" || event.stage === "aws_done" || event.stage === "official_done" || event.stage === "quote_done"
          ? "done"
          : "running";
      if (event.stage === "component_done") templateDone.add(id);
      if (event.stage === "aws_done" || event.stage === "official_done" || event.stage === "quote_done") officialDone.add(id);
      const previous = channels.get(id);
      const history = previous?.history ?? [];
      if (history.at(-1) !== message) history.push(message);
      channels.set(id, {
        id,
        name,
        message,
        history: history.slice(-10),
        state,
        order: previous?.order ?? order,
        updatedAt: event.time,
      });
    }
    const statePriority: Record<ActivityChannel["state"], number> = {
      blocked: 0,
      repair: 0,
      running: 1,
      done: 2,
    };
    const rows = [...channels.values()].sort((a, b) => {
      const priorityDifference = statePriority[a.state] - statePriority[b.state];
      return priorityDifference || a.order - b.order;
    });
    return {
      total,
      completed: officialDone.size || templateDone.size,
      channels: rows,
    };
  }, [job?.events]);
  const channelNodes = useRef(new Map<string, HTMLDivElement>());
  const systemIssueSelections = (salesReview?.selections ?? []).filter(
    (selection) => {
      const action = previewSelectionNextAction(selection);
      return action === "retry_component" || action === "internal_block";
    },
  );
  const internalValidationBlocked = Boolean(
    internalValidationPending
    && !running
    && !componentRetryStatus
    && systemIssueSelections.length,
  );
  const internalValidationBlockMessage = systemIssueSelections[0]
    ? `${serviceDisplayName(systemIssueSelections[0])}：${systemIssueSelections[0].issue_message ?? "内部计费字段校验没有通过"}`
    : "内部计费字段校验没有通过";
  const processingChannelIds = new Set(
    (salesReview?.selections ?? []).flatMap((selection, index) => {
      const componentId = Number(selection.component_id);
      return componentRetryStatus?.componentIds.includes(componentId)
        ? [String(index + 1)]
        : [];
    }),
  );
  const blockedChannelIds = new Set(
    internalValidationBlocked
      ? (salesReview?.selections ?? []).flatMap((selection, index) => (
        selection.status === "technical_issue" || selection.status === "unsupported"
          ? [String(index + 1)]
          : []
      ))
      : [],
  );
  const visibleCompletedComponents = Math.max(
    0,
    liveComponentActivity.completed - new Set([
      ...processingChannelIds,
      ...blockedChannelIds,
    ]).size,
  );
  const previousChannelPositions = useRef(new Map<string, DOMRect>());
  const channelLayoutSignature = liveComponentActivity.channels
    .map((channel) => `${channel.id}:${channel.state}`)
    .join("|");
  useLayoutEffect(() => {
    const nextPositions = new Map<string, DOMRect>();
    channelNodes.current.forEach((node, id) => nextPositions.set(id, node.getBoundingClientRect()));
    if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      nextPositions.forEach((nextPosition, id) => {
        const previousPosition = previousChannelPositions.current.get(id);
        const node = channelNodes.current.get(id);
        if (!previousPosition || !node) return;
        const deltaX = previousPosition.left - nextPosition.left;
        const deltaY = previousPosition.top - nextPosition.top;
        if (Math.abs(deltaX) < 1 && Math.abs(deltaY) < 1) return;
        node.animate(
          [
            { transform: `translate(${deltaX}px, ${deltaY}px)`, zIndex: 3 },
            { transform: "translate(0, 0)", zIndex: 3 },
          ],
          { duration: 720, easing: "cubic-bezier(.2,.78,.25,1)", fill: "none" },
        );
      });
    }
    previousChannelPositions.current = nextPositions;
  }, [channelLayoutSignature]);
  const confirmationText =
    typeof job?.error?.details?.confirmation_text === "string"
      ? job.error.details.confirmation_text
      : null;
  const confirmationItems =
    Array.isArray(job?.error?.details?.confirmation_items)
      ? (job?.error?.details?.confirmation_items as ConfirmationItem[])
      : [];
  // Once official pricing has started, a completed or failed request still
  // belongs to step 3. Showing the intake form again makes a transient AWS
  // failure look like the customer's confirmed configuration was discarded.
  const officialPricingStarted = job?.kind === "quote";
  const checking = running && job?.kind === "preview";
  const activeStep = confirmationText || salesReview
    ? 2
    : officialPricingStarted || job?.status === "completed" || (running && !checking)
      ? 3
      : checking
        ? 2
        : 1;
  useEffect(() => {
    const element = timeline.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [job?.events.length]);

  useEffect(() => {
    if (job?.status !== "failed") return;
    const details = job.error?.details;
    const token = typeof details?.confirmation_token === "string"
      ? details.confirmation_token
      : null;
    const draftId = typeof details?.draft_id === "string"
      ? details.draft_id
      : null;
    if (!token || !draftId || lateConfirmationTokenStarted.current === token) return;

    lateConfirmationTokenStarted.current = token;
    workflowPhase.current = "confirmation";
    window.sessionStorage.removeItem(QUOTE_JOB_CONTEXT_KEY);
    setConfirmationToken(token);
    setPreviewDraftId(draftId);
    setSalesReview(null);
    setConfirmationAnswers(confirmationItems.map(() => ""));
    const latePreview: Preview = {
      draft_id: draftId,
      customer_summary: "官方核价发现新的配置确认项",
      confirmation_token: token,
      confirmation_text: confirmationText,
      confirmation_items: confirmationItems,
      notices: [],
      selections: [],
      execution_trace: [],
    };
    window.sessionStorage.setItem(
      CONFIRMATION_CONTEXT_KEY,
      JSON.stringify({
        token,
        draftId,
        customerRequest: requirement,
        cloudProvider,
        preview: latePreview,
        latePricingConfirmation: true,
      } satisfies PendingConfirmationContext),
    );
    const pollingGeneration = quoteRunGeneration.current;
    if (confirmationTimer.current) clearTimeout(confirmationTimer.current);
    confirmationTimer.current = setTimeout(
      () => pollConfirmation(
        token,
        draftId,
        requirement,
        cloudProvider,
        pollingGeneration,
      ),
      250,
    );
  // A late confirmation starts once per token; including pollConfirmation would restart this effect every render.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.status, job?.error, confirmationItems, confirmationText, requirement, cloudProvider]);

  useEffect(() => {
    if (
      job?.status !== "failed"
      || job.kind !== "quote"
      || job.error?.code !== "official_spec_confirmation_required"
      || !confirmationText
      || typeof job.error?.details?.confirmation_token === "string"
      || !requirement.trim()
      || directQuotePreflightRecoveryStarted.current === job.job_id
    ) return;
    // Older/direct retry paths could reach formal pricing before a customer
    // confirmation URL existed. Send the unchanged request back through the
    // normal sales preflight once, which batches every question and publishes
    // one stable link instead of showing a dead-end copy-only prompt.
    directQuotePreflightRecoveryStarted.current = job.job_id;
    window.sessionStorage.removeItem(QUOTE_JOB_CONTEXT_KEY);
    workflowPhase.current = "idle";
    setJob(null);
    setPreviewDraftId(null);
    setConfirmationToken(null);
    void submitRequirement();
  // This recovery is keyed by the failed job id and must run at most once.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.job_id, job?.status, job?.kind, job?.error, confirmationText, requirement]);

  useEffect(() => {
    if (
      job?.status !== "failed"
      || job.error?.code !== "service_identity_resolution_failed"
      || !requirement.trim()
      || identityRecoveryAttempted.current
    ) return;
    // Product-name resolution is an internal operation. Retry the unchanged
    // request once through the repaired/local-cache path instead of asking a
    // salesperson to interpret an old failed job or resubmit every component.
    identityRecoveryAttempted.current = true;
    workflowPhase.current = "idle";
    window.sessionStorage.removeItem(QUOTE_JOB_CONTEXT_KEY);
    setPreviewDraftId(null);
    setSalesReview(null);
    void runPreflight(
      requirement,
      undefined,
      {},
      false,
      cloudProvider,
      salesRegion ?? readSalesRegionContext(cloudProvider) ?? undefined,
      [],
      quoteRunGeneration.current,
    );
  // The recovery is deliberately limited to one attempt per entered request.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.job_id, job?.status, job?.error, requirement, cloudProvider, salesRegion]);

  useEffect(() => {
    if (!running && !retryActivityVisible) return;
    const interval = window.setInterval(() => setElapsedSeconds((value) => value + 1), 1000);
    return () => window.clearInterval(interval);
  }, [running, retryActivityVisible]);

  useEffect(() => {
    if (!running && !retryActivityVisible) return;
    const interval = window.setInterval(
      () => setActivityLogTick((value) => value + 1),
      520,
    );
    return () => window.clearInterval(interval);
  }, [running, retryActivityVisible]);

  useEffect(() => {
    if (!salesReview?.confirmation_token || salesReview.sales_validation_required) return;
    const linkMode = salesReview.configuration_review_required ? "configuration" : "questions";
    const linkVersion = `${salesReview.confirmation_token}:${linkMode}`;
    if (openedCustomerLinkVersion.current === linkVersion) return;
    openedCustomerLinkVersion.current = linkVersion;
    setCopied(false);
    setCustomerLinkModalOpen(true);
  }, [
    salesReview?.confirmation_token,
    salesReview?.configuration_review_required,
    salesReview?.sales_validation_required,
  ]);

  useEffect(() => {
    if (componentRetryTimer.current) {
      clearTimeout(componentRetryTimer.current);
      componentRetryTimer.current = null;
    }
    if (
      !salesReview?.sales_validation_required
      || salesReview.confirmation_token
      || running
      || cloudProvider !== "aws"
    ) {
      if (!running) setComponentRetryStatus(null);
      return;
    }
    const failedComponentIds = (salesReview.selections ?? [])
      .filter((selection) => previewSelectionNextAction(selection) === "retry_component")
      .map((selection) => Number(selection.component_id))
      .filter((componentId) => Number.isInteger(componentId) && componentId >= 0);
    if (!failedComponentIds.length) {
      setComponentRetryStatus(null);
      return;
    }
    const retryKey = `${salesReview.draft_id}:${failedComponentIds.join(",")}`;
    const attempt = componentRetryAttempts.current.get(retryKey) ?? 0;
    if (attempt >= 3) {
      setComponentRetryStatus(null);
      return;
    }
    const delays = [1500, 4000, 10000, 30000, 60000, 120000];
    const delay = delays[Math.min(attempt, delays.length - 1)];
    const retryGeneration = quoteRunGeneration.current;
    setComponentRetryStatus({
      componentIds: failedComponentIds,
      attempt: attempt + 1,
      remainingSeconds: Math.max(1, Math.ceil(delay / 1000)),
      phase: "waiting",
    });
    const countdown = window.setInterval(() => {
      setComponentRetryStatus((current) => current ? {
        ...current,
        remainingSeconds: Math.max(0, current.remainingSeconds - 1),
      } : null);
    }, 1000);
    componentRetryTimer.current = setTimeout(() => {
      componentRetryAttempts.current.set(retryKey, attempt + 1);
      setComponentRetryStatus({
        componentIds: failedComponentIds,
        attempt: attempt + 1,
        remainingSeconds: 0,
        phase: "running",
      });
      void runPreflight(
        requirement,
        salesReview.draft_id,
        {},
        false,
        cloudProvider,
        undefined,
        failedComponentIds,
        retryGeneration,
      );
    }, delay);
    return () => {
      window.clearInterval(countdown);
      if (componentRetryTimer.current) clearTimeout(componentRetryTimer.current);
      componentRetryTimer.current = null;
    };
  // The retry is intentionally driven only by the persisted preview snapshot;
  // runPreflight is a function declaration and must not restart this timer on render.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [salesReview, running, cloudProvider, requirement]);

  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, [activeStep]);

  async function poll(
    jobId: string,
    prefixEvents: JobEvent[] = [],
    runGeneration = quoteRunGeneration.current,
  ) {
    if (runGeneration !== quoteRunGeneration.current || workflowPhase.current !== "quote") return;
    try {
      const response = await fetchWithTimeout(`${API_BASE}/api/quote-jobs/${jobId}`, {
        cache: "no-store",
      }, 10000);
      const payload = await response.json() as Partial<Job> & { message?: string };
      if (
        !response.ok
        || !payload.job_id
        || !["queued", "running", "completed", "failed"].includes(String(payload.status))
      ) {
        throw new Error(
          response.status === 404
            ? "当前报价任务已中断，请返回后重新提交。"
            : payload.message ?? "无法读取报价任务状态，请重新提交。",
        );
      }
      const next = payload as Job;
      if (runGeneration !== quoteRunGeneration.current || workflowPhase.current !== "quote") return;
      const merged = { ...next, kind: "quote" as const, events: [...prefixEvents, ...next.events] };
      setJob(merged);
      if (next.status === "queued" || next.status === "running") {
        timer.current = setTimeout(() => poll(jobId, prefixEvents, runGeneration), 1200);
      }
    } catch (error) {
      if (runGeneration !== quoteRunGeneration.current || workflowPhase.current !== "quote") return;
      setJob((current) => ({
        job_id: current?.job_id ?? jobId,
        kind: "quote",
        status: "failed",
        events: current?.events ?? [],
        error: {
          code: "connection_lost",
          message: readableRequestError(error, "报价任务连接已中断，请重新提交。"),
        },
      }));
    }
  }

  async function startQuote(
    requestText = requirement,
    draftId?: string,
    prefixEvents: JobEvent[] = [],
    provider: CloudProvider = cloudProvider,
    runGeneration = quoteRunGeneration.current,
  ) {
    if (runGeneration !== quoteRunGeneration.current) return;
    const currentSalesRegion = salesRegion ?? readSalesRegionContext(provider);
    workflowPhase.current = "quote";
    if (timer.current) clearTimeout(timer.current);
    if (confirmationTimer.current) clearTimeout(confirmationTimer.current);
    setCopied(false);
    setElapsedSeconds(0);
    setJob({
      job_id: "pending",
      kind: "quote",
      status: "queued",
      events: [...prefixEvents, { stage: "queue", message: "正在提交报价任务", time: "刚刚" }],
    });
    try {
      const response = await fetch(`${API_BASE}/api/quote-jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          cloud_provider: provider,
          customer_request: requestText,
          draft_id: draftId,
          sales_region: currentSalesRegion,
          pricing_mode: pricingMode ?? "on_demand",
          reserved_term_years: pricingMode ? reservedTermYears[0] : null,
          reserved_term_options: pricingMode ? reservedTermYears : null,
          payment_option: pricingMode ? paymentOption : null,
          include_on_demand_scenario: pricingMode ? includeOnDemandScenario : true,
          utilization_percent: utilizationPercent,
          azure_pricing_mode: azurePricingMode,
          azure_term_years: ["reservation", "savings_plan"].includes(azurePricingMode) ? azureTermYears : null,
          azure_payment_option: ["reservation", "savings_plan"].includes(azurePricingMode) ? azurePaymentOption : null,
        }),
      });
      const payload = await response.json();
      if (runGeneration !== quoteRunGeneration.current) {
        await cancelQuoteJob(String(payload.job_id ?? ""));
        return;
      }
      if (!response.ok) throw new Error(payload.message ?? "任务提交失败");
      if (!String(payload.job_id ?? "").startsWith(`${provider}-`)) {
        throw new Error("报价任务的云厂商标识不一致，系统已阻止继续处理。");
      }
      window.sessionStorage.setItem(
        QUOTE_JOB_CONTEXT_KEY,
        JSON.stringify({
          jobId: payload.job_id,
          customerRequest: requestText,
          cloudProvider: provider,
          draftId: draftId ?? null,
          salesRegion: currentSalesRegion,
        }),
      );
      await poll(payload.job_id, prefixEvents, runGeneration);
    } catch (error) {
      if (runGeneration !== quoteRunGeneration.current) return;
      setJob({
        job_id: "failed",
        kind: "quote",
        status: "failed",
        events: [],
        error: {
          code: "backend_unavailable",
          message: readableRequestError(error, "报价后端暂时不可用。"),
        },
      });
    }
  }

  async function pollConfirmation(
    token: string,
    draftId: string,
    customerRequest: string,
    provider: CloudProvider,
    runGeneration = quoteRunGeneration.current,
  ) {
    if (
      runGeneration !== quoteRunGeneration.current
      || workflowPhase.current === "quote"
      || confirmationPollGeneration.current === runGeneration
    ) return;
    confirmationPollGeneration.current = runGeneration;
    try {
      const response = await fetch(`${API_BASE}/api/confirmation-sessions/${token}`, {
        cache: "no-store",
      });
      if (!response.ok) throw new Error("无法读取客户确认状态");
      const session = await response.json() as {
        status: string;
        cloud_provider?: CloudProvider;
        answers?: Record<string, string>;
      };
      if (runGeneration !== quoteRunGeneration.current) return;
      if (session.cloud_provider !== provider) {
        setJob({
          job_id: "provider-boundary-violation",
          status: "failed",
          events: [],
          error: {
            code: "cloud_provider_boundary_violation",
            message: "确认链接与当前云厂商不一致，系统已停止处理。",
          },
        });
        window.sessionStorage.removeItem(CONFIRMATION_CONTEXT_KEY);
        return;
      }
      if (["quote"].includes(workflowPhase.current)) return;
      if (session.status === "approved") {
        setConfirmationToken(null);
        setSalesReview(null);
        window.sessionStorage.removeItem(CONFIRMATION_CONTEXT_KEY);
        await startQuote(customerRequest, draftId, [
          { stage: "customer", message: "客户已确认完整配置清单，开始官方报价", time: "刚刚" },
        ], provider, runGeneration);
        return;
      }
      if ((session.status === "reviewing" || session.status === "submitted") && session.answers) {
        setReceivedCustomerAnswers(session.answers);
        const saved = window.sessionStorage.getItem(CONFIRMATION_CONTEXT_KEY);
        if (saved) {
          const context = JSON.parse(saved) as PendingConfirmationContext;
          window.sessionStorage.setItem(
            CONFIRMATION_CONTEXT_KEY,
            JSON.stringify({ ...context, answers: session.answers }),
          );
        }
        setConfirmationToken(null);
        await runPreflight(
          customerRequest,
          draftId,
          session.answers,
          true,
          provider,
          undefined,
          [],
          runGeneration,
        );
        return;
      }
      confirmationTimer.current = setTimeout(
        () => pollConfirmation(token, draftId, customerRequest, provider, runGeneration),
        2500,
      );
    } catch {
      if (runGeneration !== quoteRunGeneration.current) return;
      confirmationTimer.current = setTimeout(
        () => pollConfirmation(token, draftId, customerRequest, provider, runGeneration),
        5000,
      );
    } finally {
      if (confirmationPollGeneration.current === runGeneration) {
        confirmationPollGeneration.current = null;
      }
    }
  }

  async function handlePreviewResult(
    preview: Preview,
    requestText: string,
    confirmationResponses: Record<string, string>,
    stopForSalesReview: boolean,
    liveEvents: JobEvent[] = [],
    provider: CloudProvider = cloudProvider,
    runGeneration = quoteRunGeneration.current,
  ) {
    if (runGeneration !== quoteRunGeneration.current || workflowPhase.current === "quote") return;
    const expectedTokenPrefix = provider === "azure" ? "azure_" : "aws_";
    const expectedDraftPrefix = provider === "azure" ? "az" : "aw";
    if (
      !preview.draft_id.startsWith(expectedDraftPrefix)
      || (
        preview.confirmation_token
        && !preview.confirmation_token.startsWith(expectedTokenPrefix)
      )
    ) {
      setJob({
        job_id: "provider-boundary-violation",
        status: "failed",
        events: [],
        error: {
          code: "cloud_provider_boundary_violation",
          message: "检测到跨云草稿或确认链接，系统已阻止展示。",
        },
      });
      return;
    }
    const previewEvents: JobEvent[] = liveEvents.length
      ? liveEvents
      : (preview.execution_trace ?? []).map((event) => ({
          stage: event.stage,
          message: event.message,
          time: "刚刚",
        }));
    setPreviewDraftId(preview.draft_id);
    if (previewHasUnfinishedComponents(preview)) {
      workflowPhase.current = "preview";
      setConfirmationToken(null);
      setCustomerLinkModalOpen(false);
      setSalesReview(preview);
      setJob({
        job_id: "sales-validation-required",
        kind: "preview",
        status: "failed",
        events: previewEvents,
        error: null,
      });
      window.sessionStorage.removeItem(CONFIRMATION_CONTEXT_KEY);
      return;
    }
    if (preview.configuration_review_required && preview.confirmation_token) {
      workflowPhase.current = "confirmation";
      setConfirmationToken(preview.confirmation_token);
      setSalesReview(preview);
      setJob({
        job_id: "configuration-review-required",
        status: "failed",
        events: previewEvents,
        error: null,
      });
      window.sessionStorage.setItem(
        CONFIRMATION_CONTEXT_KEY,
        JSON.stringify({
          token: preview.confirmation_token,
          draftId: preview.draft_id,
          customerRequest: requestText,
          cloudProvider: provider,
          preview,
        } satisfies PendingConfirmationContext),
      );
      confirmationTimer.current = setTimeout(
        () => pollConfirmation(
          preview.confirmation_token as string,
          preview.draft_id,
          requestText,
          provider,
          runGeneration,
        ),
        1200,
      );
      return;
    }
    if (preview.confirmation_text) {
      workflowPhase.current = "confirmation";
      const backendItems = preview.confirmation_items ?? [];
      const questions = backendItems.length
        ? backendItems.map((item) => item.question)
        : Array.from(
            preview.confirmation_text.matchAll(/^\s*\d+\.\s*(.+?)\s*$/gm),
            (match) => match[1],
          );
      const unresolved = (preview.selections ?? []).filter(
        (selection) => selection.requires_confirmation,
      );
      const items: ConfirmationItem[] = backendItems.length ? backendItems : questions.map((question) => {
        const lowerQuestion = question.toLowerCase();
        const matching = unresolved.find((selection) => {
          const service = `${selection.service} ${selection.display_name}`.toLowerCase();
          if (lowerQuestion.includes("redis")) return /redis|elasticache/.test(service);
          if (lowerQuestion.includes("ec2") || lowerQuestion.includes("服务器")) return service.includes("ec2");
          if (lowerQuestion.includes("rds") || lowerQuestion.includes("数据库")) return service.includes("rds");
          return false;
        });
        const options = (matching?.candidates ?? []).map((candidate) => {
          const memory = candidate.specifications.memoryGiB;
          const vcpu = candidate.specifications.vCPU;
          const specs = [
            typeof vcpu === "number" ? `${vcpu} vCPU` : null,
            typeof memory === "number" ? `${memory} GiB` : null,
          ].filter(Boolean).join(" · ");
          return {
            label: `${candidate.model}${specs ? ` · ${specs}` : ""}`,
            value: `选择 ${candidate.model}`,
          };
        });
        return { question, options };
      });
      setConfirmationAnswers(items.map(() => ""));
      setConfirmationToken(preview.confirmation_token ?? null);
      setSalesReview({ ...preview, confirmation_items: items });
      setJob({
        job_id: "confirmation-required",
        status: "failed",
        events: previewEvents,
        error: null,
      });
      if (preview.confirmation_token) {
        window.sessionStorage.setItem(
          CONFIRMATION_CONTEXT_KEY,
          JSON.stringify({
            token: preview.confirmation_token,
            draftId: preview.draft_id,
            customerRequest: requestText,
            cloudProvider: provider,
            preview: { ...preview, confirmation_items: items },
            answers: Object.keys(confirmationResponses).length ? confirmationResponses : undefined,
          } satisfies PendingConfirmationContext),
        );
        confirmationTimer.current = setTimeout(
          () => pollConfirmation(
            preview.confirmation_token as string,
            preview.draft_id,
            requestText,
            provider,
            runGeneration,
          ),
          1200,
        );
      }
      return;
    }
    window.sessionStorage.removeItem(CONFIRMATION_CONTEXT_KEY);
    if (stopForSalesReview) {
      setSalesReview(null);
      setReceivedCustomerAnswers({});
      await startQuote(requestText, preview.draft_id, [
        {
          stage: "customer",
          message: "客户回复已合并并通过配置复核，开始官方报价",
          time: "刚刚",
        },
      ], provider, runGeneration);
      return;
    }
    setSalesReview(preview);
    setJob({ job_id: "sales-review", status: "failed", events: previewEvents, error: null });
  }

  async function pollPreviewJob(
    jobId: string,
    requestText: string,
    draftId: string | undefined,
    confirmationResponses: Record<string, string>,
    stopForSalesReview: boolean,
    provider: CloudProvider,
    retryComponentIds: number[] = [],
    runGeneration = quoteRunGeneration.current,
  ) {
    if (runGeneration !== quoteRunGeneration.current || workflowPhase.current === "quote") return;
    try {
      const response = await fetchWithTimeout(
        `${API_BASE}/api/quote-jobs/${jobId}`,
        { cache: "no-store" },
        10000,
      );
      const next = await response.json() as Job;
      if (runGeneration !== quoteRunGeneration.current || ["quote"].includes(workflowPhase.current)) return;
      if (response.status === 404 && !previewRestartedJobs.current.has(jobId)) {
        previewRestartedJobs.current.add(jobId);
        previewPollFailures.current.delete(jobId);
        await runPreflight(
          requestText,
          draftId,
          confirmationResponses,
          stopForSalesReview,
          provider,
          undefined,
          retryComponentIds,
          runGeneration,
        );
        return;
      }
      if (!response.ok) {
        throw new Error(
          response.status === 404
            ? "当前配置核验任务已中断，请返回后重新识别。"
            : "配置核验任务状态读取失败。",
        );
      }
      previewPollFailures.current.delete(jobId);
      setJob({ ...next, kind: "preview" });
      if (next.status === "queued" || next.status === "running") {
        timer.current = setTimeout(
          () => pollPreviewJob(
            jobId,
            requestText,
            draftId,
            confirmationResponses,
            stopForSalesReview,
            provider,
            retryComponentIds,
            runGeneration,
          ),
          700,
        );
        return;
      }
      if (next.status === "failed") return;
      if (next.result) {
        await handlePreviewResult(
          next.result as Preview,
          requestText,
          confirmationResponses,
          stopForSalesReview,
          next.events,
          provider,
          runGeneration,
        );
      }
    } catch (error) {
      if (runGeneration !== quoteRunGeneration.current) return;
      const failures = (previewPollFailures.current.get(jobId) ?? 0) + 1;
      previewPollFailures.current.set(jobId, failures);
      if (failures <= 12 && !["quote"].includes(workflowPhase.current)) {
        setJob((current) => ({
          job_id: jobId,
          kind: "preview",
          status: "running",
          events: current?.events ?? [],
          error: null,
        }));
        timer.current = setTimeout(
          () => pollPreviewJob(
            jobId,
            requestText,
            draftId,
            confirmationResponses,
            stopForSalesReview,
            provider,
            retryComponentIds,
            runGeneration,
          ),
          Math.min(5000, 1000 + failures * 500),
        );
        return;
      }
      setJob({
        job_id: jobId,
        kind: "preview",
        status: "failed",
        events: [],
        error: {
          code: "backend_unavailable",
          message: readableRequestError(error, "配置核验进度连接已中断。"),
        },
      });
    }
  }

  async function runPreflight(
    requestText: string,
    draftId?: string,
    confirmationResponses: Record<string, string> = {},
    stopForSalesReview = false,
    provider: CloudProvider = cloudProvider,
    salesRegionOverride?: string,
    retryComponentIds: number[] = [],
    runGeneration = quoteRunGeneration.current,
  ) {
    if (runGeneration !== quoteRunGeneration.current || workflowPhase.current === "quote") return;
    workflowPhase.current = "preview";
    setCopied(false);
    setElapsedSeconds(0);
    setJob({
      job_id: "preflight",
      kind: "preview",
      status: "running",
      events: [
        { stage: "ai", message: `正在解析需求并查询 ${provider === "aws" ? "AWS" : "Microsoft Azure"} 官方规格`, time: "刚刚" },
      ],
    });
    try {
      const currentSalesRegion = salesRegionOverride
        ?? salesRegion
        ?? readSalesRegionContext(provider);
      const requestPayload = {
        cloud_provider: provider,
        customer_request: requestText,
        draft_id: draftId,
        confirmation_responses: confirmationResponses,
        retry_component_ids: retryComponentIds,
        sales_region: currentSalesRegion,
        pricing_mode: pricingMode ?? "on_demand",
        reserved_term_years: pricingMode ? reservedTermYears[0] : null,
        reserved_term_options: pricingMode ? reservedTermYears : null,
        payment_option: pricingMode ? paymentOption : null,
        include_on_demand_scenario: pricingMode ? includeOnDemandScenario : true,
        utilization_percent: utilizationPercent,
        azure_pricing_mode: azurePricingMode,
        azure_term_years: ["reservation", "savings_plan"].includes(azurePricingMode) ? azureTermYears : null,
        azure_payment_option: ["reservation", "savings_plan"].includes(azurePricingMode) ? azurePaymentOption : null,
      };
      {
        const startResponse = await fetch(`${API_BASE}/api/preview-jobs`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(requestPayload),
        });
        const started = await startResponse.json() as { job_id?: string; message?: string };
        if (runGeneration !== quoteRunGeneration.current) {
          await cancelQuoteJob(String(started.job_id ?? ""));
          return;
        }
        if (!startResponse.ok || !started.job_id) {
          const fallbackResponse = await fetchWithTimeout(`${API_BASE}/api/quotes/preview`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(requestPayload),
          }, 180000);
          const fallback = await fallbackResponse.json();
          if (runGeneration !== quoteRunGeneration.current) return;
          if (!fallbackResponse.ok) {
            throw new Error(fallback.message ?? started.message ?? "配置核验任务启动失败");
          }
          await handlePreviewResult(
            fallback as Preview,
            requestText,
            confirmationResponses,
            stopForSalesReview,
            [],
            provider,
            runGeneration,
          );
          return;
        }
        if (!started.job_id.startsWith(`${provider}-`)) {
          throw new Error("配置任务的云厂商标识不一致，系统已阻止继续处理。");
        }
        setJob({ job_id: started.job_id, kind: "preview", status: "queued", events: [] });
        await pollPreviewJob(
          started.job_id,
          requestText,
          draftId,
          confirmationResponses,
          stopForSalesReview,
          provider,
          retryComponentIds,
          runGeneration,
        );
        return;
      }
    } catch (error) {
      if (runGeneration !== quoteRunGeneration.current) return;
      setJob({
        job_id: "preflight-error",
        kind: "preview",
        status: "failed",
        events: [],
        error: {
          code: "backend_unavailable",
          message: readableRequestError(error, "配置预检服务暂时不可用。"),
        },
      });
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    await submitRequirement();
  }

  async function submitRequirement() {
    if (running || salesRegionChecking || quoteResetting || requirement.trim().length < 3) return;
    const runGeneration = quoteRunGeneration.current + 1;
    quoteRunGeneration.current = runGeneration;
    identityRecoveryAttempted.current = false;
    workflowPhase.current = "idle";
    setConfirmationReply("");
    setConfirmationToken(null);
    setSalesReview(null);
    setPreviewDraftId(null);
    setReceivedCustomerAnswers({});
    window.sessionStorage.removeItem(CONFIRMATION_CONTEXT_KEY);
    window.sessionStorage.removeItem(QUOTE_JOB_CONTEXT_KEY);
    clearSalesRegionContext(cloudProvider);
    setSalesRegion(null);
    const provider = cloudProvider;
    setSalesRegionChecking(true);
    try {
      const endpoint = "/api/quotes/region-preflight";
      const response = await fetch(`${API_BASE}${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ customer_request: requirement }),
      });
      const result = await response.json() as SalesRegionPreflight & { message?: string };
      if (runGeneration !== quoteRunGeneration.current) return;
      if (!response.ok) throw new Error(result.message ?? "地区识别失败");
      if (result.requires_confirmation) {
        setSalesRegionOptions(result.options);
        setSalesRegionPromptOpen(true);
        return;
      }
      const detectedRegion = result.selected_region ?? null;
      setSalesRegion(detectedRegion);
      if (detectedRegion) {
        writeSalesRegionContext(provider, detectedRegion);
      }
      await runPreflight(
        requirement,
        undefined,
        {},
        false,
        provider,
        detectedRegion ?? undefined,
        [],
        runGeneration,
      );
    } catch (error) {
      if (runGeneration !== quoteRunGeneration.current) return;
      setJob({
        job_id: "region-preflight-error",
        kind: "preview",
        status: "failed",
        events: [],
        error: {
          code: "region_preflight_unavailable",
          message: readableRequestError(error, "地区识别服务暂时不可用。"),
        },
      });
    } finally {
      if (runGeneration === quoteRunGeneration.current) setSalesRegionChecking(false);
    }
  }

  async function submitConfirmationReply() {
    const reply = confirmationItems.length
      ? confirmationAnswers.map((answer) => answer.trim()).join("\n").trim()
      : confirmationReply.trim();
    if (running || !reply || !previewDraftId) return;
    if (confirmationItems.length && confirmationAnswers.some((answer) => !answer.trim())) return;
    const responses = confirmationItems.length
      ? Object.fromEntries(
          confirmationItems.map((item, index) => [
            confirmationAnswerKey(item),
            confirmationAnswers[index].trim(),
          ]),
        )
      : { 客户确认: confirmationReply.trim() };
    setConfirmationReply("");
    await runPreflight(requirement, previewDraftId, responses);
  }

  async function copyConfirmationText() {
    if (!confirmationText) return;
    await navigator.clipboard.writeText(confirmationText);
    setCopied(true);
  }

  async function exportQuoteWorkbook() {
    const quote = job?.result;
    if (!quote || exporting) return;
    setExporting(true);
    try {
      const ExcelJS = await import("exceljs");
      const workbook = new ExcelJS.Workbook();
      workbook.creator = "AstraQuote";
      workbook.created = new Date();
      const scenarios = quoteScenarios(quote);
      const orderedSelections = hierarchyOrdered(quote.selections);

      const summary = workbook.addWorksheet("报价单", {
        views: [{ state: "frozen", ySplit: 1 }],
        pageSetup: { orientation: "landscape", fitToPage: true, fitToWidth: 1 },
      });
      summary.columns = [
        { key: "index", width: 7 }, { key: "service", width: 25 }, { key: "region", width: 22 },
        { key: "model", width: 25 }, { key: "quantity", width: 10 }, { key: "configuration", width: 48 },
        ...scenarios.map((_, index) => ({ key: `scenario_${index}`, width: 22 })),
        { key: "reference", width: 30 }, { key: "remarks", width: 42 },
      ];
      const columnCount = 8 + scenarios.length;
      const lastColumn = summary.getColumn(columnCount).letter;
      const headerRowNumber = 1;
      const header = summary.addRow(["序号", "AWS 服务", "区域", "型号 / 方案", "数量", "配置", ...scenarios.map((scenario) => `${scenario.label}月费`), "参考单价", "备注"]);
      header.eachCell((cell) => {
        cell.font = { bold: true, color: { argb: "FFFFFFFF" } };
        cell.fill = { type: "pattern", pattern: "solid", fgColor: { argb: "FF0D7C72" } };
      });
      orderedSelections.forEach(({ item: selection, originalIndex: index }, displayIndex) => {
        const isUnpriced = selection.pricing_status === "unpriced";
        const isReferenceOnly = !isUnpriced && (
          selection.pricing_status === "reference_only" || Boolean(selection.reference_rates?.length)
        )
          && scenarios.every((scenario) => scenarioServiceCost(scenario, selection, index) === 0);
        const row = summary.addRow([
          selection.component_number ?? index + 1,
          `${selection.parent_component_id ? "↳ " : ""}${serviceDisplayName(selection)}`,
          selection.region,
          selection.model,
          selection.quantity ?? 1,
          compactSpecifications(selection),
          ...scenarios.map((scenario) => {
            const cost = scenarioServiceCost(scenario, selection, index);
            return (isUnpriced || isReferenceOnly
              || scenarioComponentIsIncomplete(scenario, selection, index)) && cost === 0
              ? null
              : cost;
          }),
          compactReferenceRateText(selection) || "-",
          quotationRemark(selection),
        ]);
        scenarios.forEach((_, scenarioIndex) => {
          row.getCell(7 + scenarioIndex).numFmt = "$#,##0.00";
        });
        row.alignment = { vertical: "top", wrapText: true };
        if (displayIndex % 2 === 1) row.eachCell((cell) => {
          cell.fill = { type: "pattern", pattern: "solid", fgColor: { argb: "FFF1F6F2" } };
        });
      });
      const totalRow = summary.addRow(["", quote.is_partial ? "已核价小计（报价不完整）" : "合计", "", "", "", "", ...scenarios.map((scenario) => scenario.total_cost), "", ""]);
      totalRow.font = { bold: true };
      scenarios.forEach((_, scenarioIndex) => {
        const cell = totalRow.getCell(7 + scenarioIndex);
        cell.numFmt = "$#,##0.00";
        cell.fill = { type: "pattern", pattern: "solid", fgColor: { argb: "FFFFE2D7" } };
      });
      if (scenarios.some((scenario) => scenario.upfront_cost > 0)) {
        const upfrontRow = summary.addRow(["", "预付费合计", "", "", "", "", ...scenarios.map((scenario) => scenario.upfront_cost), "", ""]);
        upfrontRow.font = { bold: true };
        scenarios.forEach((_, scenarioIndex) => {
          upfrontRow.getCell(7 + scenarioIndex).numFmt = "$#,##0.00";
        });
      }
      summary.autoFilter = { from: `A${headerRowNumber}`, to: `${lastColumn}${Math.max(headerRowNumber, headerRowNumber + orderedSelections.length)}` };
      const tableLastRow = summary.rowCount;
      for (let rowNumber = headerRowNumber; rowNumber <= tableLastRow; rowNumber += 1) {
        const row = summary.getRow(rowNumber);
        for (let columnNumber = 1; columnNumber <= columnCount; columnNumber += 1) {
          row.getCell(columnNumber).border = {
            top: { style: "thin", color: { argb: "FFB8C8C2" } },
            left: { style: "thin", color: { argb: "FFB8C8C2" } },
            bottom: { style: "thin", color: { argb: "FFB8C8C2" } },
            right: { style: "thin", color: { argb: "FFB8C8C2" } },
          };
        }
      }

      const buffer = await workbook.xlsx.writeBuffer();
      const blob = new Blob([buffer], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `AstraQuote-${quote.quote_id.slice(0, 12)}.xlsx`;
      anchor.click();
      URL.revokeObjectURL(url);
    } finally {
      setExporting(false);
    }
  }

  function reviseRequirement() {
    requirementInput.current?.scrollIntoView({ behavior: "smooth", block: "center" });
    window.setTimeout(() => requirementInput.current?.focus(), 350);
  }

  async function returnToHome() {
    if (quoteResetting) return;
    setQuoteResetting(true);
    const activeJobIds = new Set<string>();
    if (job?.job_id?.startsWith("aws-")) activeJobIds.add(job.job_id);
    try {
      const persisted = JSON.parse(
        window.sessionStorage.getItem(QUOTE_JOB_CONTEXT_KEY) ?? "{}",
      ) as { jobId?: string };
      if (persisted.jobId?.startsWith("aws-")) activeJobIds.add(persisted.jobId);
    } catch {
      // Invalid recovery data is removed with the rest of the quote session.
    }
    quoteRunGeneration.current += 1;
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    if (confirmationTimer.current) {
      clearTimeout(confirmationTimer.current);
      confirmationTimer.current = null;
    }
    if (componentRetryTimer.current) {
      clearTimeout(componentRetryTimer.current);
      componentRetryTimer.current = null;
    }
    workflowPhase.current = "idle";
    confirmationPollGeneration.current = null;
    lateConfirmationTokenStarted.current = null;
    directQuotePreflightRecoveryStarted.current = null;
    identityRecoveryAttempted.current = false;
    previewPollFailures.current.clear();
    previewRestartedJobs.current.clear();
    componentRetryAttempts.current.clear();
    openedCustomerLinkVersion.current = null;
    setRequirement("");
    setJob(null);
    setCopied(false);
    setCustomerLinkModalOpen(false);
    setConfirmationReply("");
    setConfirmationAnswers([]);
    setLogExpanded(false);
    setExporting(false);
    setElapsedSeconds(0);
    setActivityLogTick(0);
    setPricingMode("standard_reserved");
    setIncludeOnDemandScenario(true);
    setReservedTermYears([1, 3]);
    setPaymentOption("all_upfront");
    setUtilizationPercent(100);
    setAzurePricingMode("pay_as_you_go");
    setAzureTermYears(1);
    setAzurePaymentOption("monthly");
    setPreviewDraftId(null);
    setConfirmationToken(null);
    setSalesReview(null);
    setSalesRegion(null);
    setSalesRegionOptions([]);
    setSalesRegionPromptOpen(false);
    setSalesRegionChecking(false);
    setComponentRetryStatus(null);
    setReceivedCustomerAnswers({});
    clearQuoteSessionStorage();
    // Clear the visible/local log immediately. The backend log is cleared only
    // after the previous job has acknowledged cancellation, so it cannot write
    // another late entry into the new run.
    window.dispatchEvent(new Event("astraquote:clear-diagnostics"));
    window.scrollTo({ top: 0, behavior: "smooth" });
    window.setTimeout(() => requirementInput.current?.focus(), 350);
    try {
      await Promise.all([...activeJobIds].map((jobId) => cancelQuoteJob(jobId)));
      await fetchWithTimeout(`${API_BASE}/api/debug/logs/clear`, {
        method: "POST",
        cache: "no-store",
      }, 5000);
    } catch {
      // Resetting the quote itself remains successful if diagnostics are disabled.
    } finally {
      window.dispatchEvent(new Event("astraquote:clear-diagnostics"));
      setQuoteResetting(false);
    }
  }

  return (
    <main className="app">
      <header className="site-header">
        <a className="brand" href="#top" aria-label="AstraQuote 首页">
          <span>A</span>
          <strong>AstraQuote</strong>
        </a>
        <div className="header-actions">
          <button
            className="global-requote-button"
            type="button"
            disabled={quoteResetting}
            onClick={() => void returnToHome()}
          >{quoteResetting ? "正在清理" : "重新报价"}</button>
          <div className={`health ${health?.status === "ok" ? "online" : ""}`}>
            <i />
            {health?.status === "ok"
              ? cloudProvider === "aws" ? `AWS ${health.awsAccount}` : "Azure Retail Prices · 公开接口"
              : health
                ? "后台连接中断 · 正在重连"
                : "正在连接服务"}
          </div>
        </div>
      </header>

      <nav className="quote-steps" aria-label="报价步骤">
        {["输入需求", "配置确认", "官方报价"].map((label, index) => {
          const step = index + 1;
          return (
            <div className={`${step === activeStep ? "active" : ""} ${step < activeStep ? "done" : ""}`} key={label}>
              <span>{step < activeStep ? "✓" : step}</span>
              <strong>{label}</strong>
            </div>
          );
        })}
      </nav>

      {!job && !confirmationText && !salesReview && (
      <section className="hero" id="top">
        <form className="quote-form" onSubmit={submit}>
          <label className="sr-only" htmlFor="requirement">客户需求</label>
          <textarea
            ref={requirementInput}
            id="requirement"
            value={requirement}
            onChange={(event) => {
              setRequirement(event.target.value);
              setSalesRegion(null);
              clearSalesRegionContext(cloudProvider);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                if (!running && requirement.trim().length >= 3) void submitRequirement();
              }
            }}
            maxLength={12000}
            placeholder={`请按 1、2、3、4 分条粘贴客户需求；序号后可用顿号、逗号、句号或空格。\n\n示例：\n1、云服务器：数量、区域、规格……\n2、数据库：引擎、容量、部署方式……`}
          />
          <div className="counter">{requirement.length.toLocaleString()} / 12,000</div>
          {cloudProvider === "aws" ? <fieldset className="pricing-choice">
            <legend>报价方案 <small>客户明确要求优先；未指定时按已选方案生成对比报价</small></legend>
            <div className="pricing-mode-grid">
              {([
                ["on_demand", "按需", "按实际使用付费"],
                ["standard_reserved", "标准预留", "可选择 1 年或 3 年"],
                ["convertible_reserved", "可转换预留", "支持调整实例系列"],
              ] as const).map(([value, label, description]) => (
                <button
                  type="button"
                  className={value === "on_demand" ? (includeOnDemandScenario ? "selected" : "") : (pricingMode === value ? "selected" : "")}
                  key={value}
                  onClick={() => {
                    if (value === "on_demand") {
                      setIncludeOnDemandScenario((current) => pricingMode ? !current : true);
                    } else {
                      setPricingMode((current) => {
                        if (current === value) {
                          setIncludeOnDemandScenario(true);
                          return null;
                        }
                        return value;
                      });
                    }
                  }}
                >
                  <i aria-hidden="true" />
                  <span><strong>{label}</strong><small>{description}</small></span>
                </button>
              ))}
            </div>
            {!pricingMode ? (
              <div className="pricing-details compact">
                <label>预计使用率
                  <span className="number-field">
                    <input
                      type="number"
                      min={1}
                      max={100}
                      value={utilizationPercent}
                      onChange={(event) => setUtilizationPercent(Math.min(100, Math.max(1, Number(event.target.value) || 100)))}
                    />%
                  </span>
                </label>
              </div>
            ) : (
              <div className="pricing-details">
                <div>
                  <span>预留期限</span>
                  <div className="segmented">
                    {([1, 3] as const).map((year) => (
                      <button
                        type="button"
                        className={reservedTermYears.includes(year) ? "selected" : ""}
                        key={year}
                        aria-pressed={reservedTermYears.includes(year)}
                        onClick={() => setReservedTermYears((current) => {
                          if (current.includes(year)) {
                            return current.length === 1 ? current : current.filter((item) => item !== year);
                          }
                          return [...current, year].sort() as (1 | 3)[];
                        })}
                      >
                        {year} 年
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <span>付款方式</span>
                  <div className="segmented">
                    {([
                      ["no_upfront", "无预付"],
                      ["partial_upfront", "部分预付"],
                      ["all_upfront", "全预付"],
                    ] as const).map(([value, label]) => (
                      <button type="button" className={paymentOption === value ? "selected" : ""} key={value} onClick={() => setPaymentOption(value)}>
                        {label}
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            )}
          </fieldset> : <fieldset className="pricing-choice">
            <legend>Azure 报价方案 <small>公开零售价；无需登录 Azure 账号</small></legend>
            <div className="pricing-mode-grid">
              {([
                ["pay_as_you_go", "按量付费", "按实际使用量计费"],
                ["reservation", "预留", "1 年或 3 年承诺"],
                ["savings_plan", "Savings Plan", "按小时消费承诺"],
                ["spot", "Spot", "可中断的低价计算资源"],
              ] as const).map(([value, label, description]) => (
                <button
                  type="button"
                  className={azurePricingMode === value ? "selected" : ""}
                  key={value}
                  onClick={() => setAzurePricingMode(value)}
                >
                  <i aria-hidden="true" />
                  <span><strong>{label}</strong><small>{description}</small></span>
                </button>
              ))}
            </div>
            {["reservation", "savings_plan"].includes(azurePricingMode) && (
              <div className="pricing-details">
                <div>
                  <span>承诺期限</span>
                  <div className="segmented">
                    {([1, 3] as const).map((year) => (
                      <button type="button" className={azureTermYears === year ? "selected" : ""} key={year} onClick={() => setAzureTermYears(year)}>{year} 年</button>
                    ))}
                  </div>
                </div>
                <div>
                  <span>付款方式</span>
                  <div className="segmented">
                    <button type="button" className={azurePaymentOption === "monthly" ? "selected" : ""} onClick={() => setAzurePaymentOption("monthly")}>月付</button>
                    <button type="button" className={azurePaymentOption === "upfront" ? "selected" : ""} onClick={() => setAzurePaymentOption("upfront")}>一次性支付</button>
                  </div>
                </div>
              </div>
            )}
            <div className="azure-pricing-note"><strong>Microsoft 官方公开价</strong><span>不包含 EA/MCA/CSP 协议折扣、税费和抵扣</span></div>
          </fieldset>}
          <div className="form-actions">
            <button
              type="submit"
              className="primary"
              disabled={running || salesRegionChecking || quoteResetting || requirement.trim().length < 3}
            >
              {quoteResetting
                ? <><i className="spinner" aria-hidden="true" />正在清理上一轮</>
                : running || salesRegionChecking
                  ? <><i className="spinner" aria-hidden="true" />正在提交</>
                  : <>提交并生成配置 <span aria-hidden="true">→</span></>}
            </button>
          </div>
        </form>
      </section>
      )}

      {portalReady && salesRegionPromptOpen && createPortal((
        <div className="sales-region-modal-backdrop" role="presentation">
          <section
            className="sales-region-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="sales-region-modal-title"
          >
            <small>销售确认 · 第一步</small>
            <h2 id="sales-region-modal-title">请由销售确认客户部署地区</h2>
            <p>这是组件识别和生成客户链接之前的销售内部步骤。客户未填写可用 {cloudProvider === "aws" ? "AWS" : "Microsoft Azure"} 地区，或填写的地点无法对应到真实官方区域时，请销售从当前官方列表确认；系统不会擅自替换，也不会把地区问题发给客户。</p>
            <div className="sales-region-option-grid">
              {salesRegionOptions.map((option) => (
                <button
                  type="button"
                  className={salesRegion === option.code ? "selected" : ""}
                  key={option.code}
                  onClick={() => {
                    setSalesRegion(option.code);
                    writeSalesRegionContext(cloudProvider, option.code);
                  }}
                >
                  <strong>{option.label}</strong>
                  <span>{option.code}</span>
                </button>
              ))}
            </div>
            <div className="sales-region-modal-actions">
              <button type="button" className="secondary" onClick={() => {
                setSalesRegionPromptOpen(false);
                setSalesRegion(null);
                clearSalesRegionContext(cloudProvider);
              }}>返回修改需求</button>
              <button type="button" className="primary" disabled={!salesRegion} onClick={() => {
                const confirmedRegion = salesRegion;
                if (!confirmedRegion) return;
                setSalesRegionPromptOpen(false);
                void runPreflight(
                  requirement,
                  undefined,
                  {},
                  false,
                  cloudProvider,
                  confirmedRegion,
                );
              }}>销售确认地区并开始整理</button>
            </div>
          </section>
        </div>
      ), document.body)}

      {salesReview && !internalValidationPending && (
        <section className="sales-review-card">
          {salesReview.confirmation_token && (
            <div className="customer-link-reminder">
              <span>↗</span>
              <div>
                <strong>客户配置确认链接已生成</strong>
                <small>请复制并发送给客户；客户提交后，系统将自动进入复核流程。</small>
              </div>
              <button type="button" onClick={async () => {
                await navigator.clipboard.writeText(`${window.location.origin}/confirm/${salesReview.confirmation_token}`);
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1800);
              }}>{copied ? "已复制" : "复制客户链接"}</button>
            </div>
          )}
          <div className="sales-review-head">
            <div><p className="kicker">CONFIGURATION REVIEW</p><h2>组件配置清单</h2><p>系统已按 {cloudProvider === "aws" ? "AWS" : "Microsoft Azure"} 服务拆分客户需求。问题项需客户确认，其余项目可直接进入报价。</p></div>
            <span>{salesReview.confirmation_text ? "有待确认项" : (salesReview.selections ?? []).some((item) => item.status === "technical_issue") ? "部分官方查询待重试" : "组件已完整识别"}</span>
          </div>
          {salesReview.expert_review && (
            <div className="expert-review" role="status">
              <div>
                <span>配置处理引擎</span>
                <strong>多任务并行核验</strong>
                <small>运行正常</small>
              </div>
              <dl>
                <div><dt>结构解析</dt><dd>{salesReview.expert_review.ai_calls} 次</dd></div>
                <div><dt>配置组件</dt><dd>{salesReview.expert_review.components} 项</dd></div>
                <div><dt>官方核验</dt><dd>{salesReview.expert_review.official_checks} 项</dd></div>
                <div><dt>待确认项</dt><dd>{salesReview.expert_review.customer_questions} 项</dd></div>
              </dl>
              <details>
                <summary>查看核验规则</summary>
                <ul>{salesReview.expert_review.safeguards.map((item) => <li key={item}>{item}</li>)}</ul>
              </details>
            </div>
          )}
          {salesReview.customer_summary && <div className="sales-review-summary">{salesReview.customer_summary}</div>}
          {Object.keys(receivedCustomerAnswers).length > 0 && (
            <div className="customer-answer-received" role="status">
              <strong>✓ 已收到客户回复</strong>
              <div>
                {Object.entries(receivedCustomerAnswers).map(([question, answer], index) => (
                  <p key={question}><b>{index + 1}</b><span>{answer}</span></p>
                ))}
              </div>
            </div>
          )}
          {salesReview.sales_validation_required && (
            <div className="customer-answer-received sales-internal-validation" role="status">
              <strong>系统正在自动完成组件核验</strong>
              <p>{salesReview.sales_validation_message ?? "仅重新处理未通过的组件，已通过组件不会重复运行。"}</p>
            </div>
          )}
          <div className="component-review-grid">
            {hierarchyOrdered(salesReview.selections ?? []).map(({ item: selection, originalIndex }) => {
              const questions = (salesReview.confirmation_items ?? []).filter((item) => questionMatchesSelection(item, selection));
              const state = questions.length > 0 || selection.requires_confirmation
                ? "customer_issue"
                : selection.status ?? "ready";
              const activity = liveComponentActivity.channels.find(
                (channel) => channel.id === String(originalIndex + 1),
              );
              const numericComponentId = Number(selection.component_id);
              const retryingThisComponent = Boolean(
                componentRetryStatus
                && Number.isInteger(numericComponentId)
                && componentRetryStatus.componentIds.includes(numericComponentId),
              );
              const systemManaged = ["technical_issue", "unsupported"].includes(state);
              const currentProcessingMessage = componentRetryStatus?.phase === "waiting" && retryingThisComponent
                ? `本轮尚未通过，${componentRetryStatus.remainingSeconds} 秒后开始第 ${componentRetryStatus.attempt} 次重试`
                : retryingThisComponent
                  ? `第 ${componentRetryStatus?.attempt ?? 1} 次重试中：先检查本地缓存，必要时同步官方目录`
                  : activity?.state === "done" && systemManaged
                    ? "本轮查询已结束，但组件尚未通过安全报价检查"
                    : activity?.message ?? "等待组件级自动核验任务";
              const processingHistory = (activity?.history ?? [])
                .filter((message) => !/核验完成|报价计算完成|停止运行/.test(message))
                .slice(-3);
              return (
                <article className={`component-review-card ${state} ${selection.parent_component_id ? "derived-component" : ""}`} key={`${selection.component_id ?? originalIndex}-${selection.service}`}>
                  <header>
                    <span>{selection.component_number ?? String(originalIndex + 1).padStart(2, "0")}</span>
                    <div><small>{selection.parent_component_number ? `隶属于 ${selection.parent_component_number} · ${selection.parent_display_name ?? "父组件"}` : selection.region ?? "未指定区域"}</small><h3>{serviceDisplayName(selection)}</h3></div>
                    <b>{state === "ready" ? "可报价" : state === "customer_issue" ? "需客户确认" : retryingThisComponent ? componentRetryStatus?.phase === "waiting" ? "等待重试" : "处理中" : state === "unsupported" ? "已识别" : "待处理"}</b>
                  </header>
                  <div className="component-model">
                    {selection.requested_model || selection.selected_model || `由 ${cloudProvider === "aws" ? "AWS" : "Azure"} 规格匹配`}
                  </div>
                  <div className="component-tags">
                    {previewRequirementTags(selection).map((tag) => <span key={`${tag.label}-${tag.value}`}><small>{tag.label}</small>{tag.value}</span>)}
                  </div>
                  {selection.source_text && <details><summary>查看客户原话</summary><p>{selection.source_text}</p></details>}
                  {(questions.length > 0 || selection.issue_message) && (
                    <div className="component-issue">
                      {questions.length > 0
                        ? questions.map((item) => <p key={item.question}>{item.question}</p>)
                        : <p>{selection.issue_message}</p>}
                    </div>
                  )}
                  {systemManaged && retryingThisComponent && (
                    <div className="component-processing-log" role="status" aria-live="polite">
                      <div><i className="active" aria-hidden="true" /><strong>处理记录</strong></div>
                      <p>{currentProcessingMessage}</p>
                      {processingHistory.length > 0 && (
                        <ol>
                          {processingHistory.map((message, index) => (
                            <li key={`${message}-${index}`}>{message}</li>
                          ))}
                        </ol>
                      )}
                      <small>{activity ? `最近更新 ${activity.updatedAt}` : "已进入处理队列"}</small>
                    </div>
                  )}
                </article>
              );
            })}
          </div>
          <div className="sales-review-actions">
            <button type="button" className="secondary" onClick={() => { workflowPhase.current = "idle"; setSalesReview(null); setJob(null); reviseRequirement(); }}>返回修改</button>
            {salesReview.confirmation_token && !salesReview.sales_validation_required && (
              <button type="button" className="primary" onClick={() => {
                setCopied(false);
                setCustomerLinkModalOpen(true);
              }}>获取客户确认链接</button>
            )}
          </div>
        </section>
      )}

      {portalReady && salesReview?.confirmation_token && !internalValidationPending && customerLinkModalOpen && createPortal((
        <div className="customer-link-modal-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setCustomerLinkModalOpen(false);
        }}>
          <section className="customer-link-modal" role="dialog" aria-modal="true" aria-labelledby="customer-link-modal-title">
            <button className="customer-link-modal-close" type="button" aria-label="关闭" onClick={() => setCustomerLinkModalOpen(false)}>×</button>
            <div className="customer-link-modal-icon"><span>↗</span></div>
            <small>客户确认</small>
            <h2 id="customer-link-modal-title">发送配置确认链接</h2>
            <p>{salesReview.configuration_review_required ? "客户确认完整配置后，系统将进入正式报价流程。" : "客户提交全部待确认事项后，系统将自动执行配置复核。"}</p>
            <div className="customer-link-value">
              <span>{typeof window !== "undefined" ? `${window.location.origin}/confirm/${salesReview.confirmation_token}` : `/confirm/${salesReview.confirmation_token}`}</span>
            </div>
            <div className="customer-link-modal-actions">
              <button type="button" onClick={async () => {
                await navigator.clipboard.writeText(`${window.location.origin}/confirm/${salesReview.confirmation_token}`);
                setCopied(true);
                window.setTimeout(() => setCustomerLinkModalOpen(false), 650);
              }}>{copied ? "链接已复制" : "复制确认链接"}</button>
            </div>
            <footer><i /> 等待客户提交确认结果</footer>
          </section>
        </div>
      ), document.body)}

      {(!salesReview || internalValidationPending) && (running || componentRetryStatus || internalValidationPending) && (
        <section className="workbench running" aria-live="polite">
          <div className="workbench-head">
            <div><p className="kicker">报价处理进度</p><h2>{internalValidationBlocked ? "组件校验未通过" : internalValidationPending ? "正在完成全部组件校验" : componentRetryStatus ? "正在持续修复未通过组件" : checking ? "正在核验配置" : "正在生成报价"}</h2></div>
            <div className="workbench-actions">
              <button type="button" className="log-toggle" onClick={() => setLogExpanded((value) => !value)}>
                {logExpanded ? "收起记录" : "展开记录"}
              </button>
              <span className="status-pill">{internalValidationBlocked ? "已停止" : componentRetryStatus?.phase === "waiting" ? `${componentRetryStatus.remainingSeconds} 秒后继续` : "处理中"}</span>
            </div>
          </div>
          {(running || componentRetryStatus || internalValidationBlocked) && <div className={`current-step ${internalValidationBlocked ? "blocked" : ""}`}><i /><span>{internalValidationBlocked ? internalValidationBlockMessage : componentRetryStatus?.phase === "waiting" ? `未通过组件将在 ${componentRetryStatus.remainingSeconds} 秒后继续核验` : latest?.message ?? "正在准备组件级官方核验"}</span><b>{internalValidationBlocked ? "未发布" : `${elapsedSeconds}s`}</b></div>}
          {liveComponentActivity.channels.length > 0 && (
            <div className="ai-channel-section">
              <div className="ai-channel-summary">
                <span>组件处理通道</span>
                <b>{liveComponentActivity.total || liveComponentActivity.channels.length} 个独立组件 · {visibleCompletedComponents}/{liveComponentActivity.total || "-"}</b>
              </div>
              <div className="ai-channel-grid">
                {liveComponentActivity.channels.map((channel, index) => {
                  const visibleChannel: ActivityChannel = blockedChannelIds.has(channel.id)
                    ? { ...channel, state: "blocked" }
                    : processingChannelIds.has(channel.id)
                    ? { ...channel, state: "running" }
                    : channel;
                  return (
                    <div
                      className={`ai-channel ${visibleChannel.state}`}
                      key={visibleChannel.id}
                      ref={(node) => {
                        if (node) channelNodes.current.set(visibleChannel.id, node);
                        else channelNodes.current.delete(visibleChannel.id);
                      }}
                    >
                      <header><span>通道 {index + 1}</span><i /></header>
                      <strong>{visibleChannel.name}</strong>
                      <div className="ai-channel-log" aria-hidden="true">
                        <div
                          className="ai-channel-log-group"
                        >
                          {(() => {
                          const stream = activityLogStream(visibleChannel);
                          const end = visibleChannel.state === "done"
                            ? stream.length - 1
                            : (activityLogTick + index * 2) % stream.length;
                          return Array.from({ length: Math.min(5, stream.length) }, (_, row) => {
                            const messageIndex = (end - 4 + row + stream.length) % stream.length;
                            return (
                              <p
                                className={row === 0 ? "log-row-exiting" : row === 4 ? "log-row-new" : ""}
                                key={`${visibleChannel.id}-${messageIndex}-${stream[messageIndex]}`}
                                style={{ top: `${(row - 1) * 19}px` }}
                              >
                                <span>{String(messageIndex + 1).padStart(2, "0")}</span>
                                {stream[messageIndex]}
                              </p>
                            );
                          });
                          })()}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
          <ol className={`timeline ${liveComponentActivity.channels.length ? "channel-mode" : ""} ${logExpanded ? "expanded" : ""}`} ref={timeline}>
            {job?.events.map((event, index) => (
              <li key={`${event.time}-${index}`}>
                <span className="step-dot">{index + 1}</span>
                <div><small>{stageName[event.stage] ?? event.stage} · {event.time}</small><p>{event.message}</p></div>
              </li>
            ))}
          </ol>
        </section>
      )}

      {job?.status === "failed" && job.error && confirmationText && !salesReview && (
        <section className="confirmation-card">
          <div className="confirmation-head">
            <div><h2>客户确认</h2></div>
            <span>待回复</span>
          </div>
          <pre>{confirmationText}</pre>
          {confirmationToken ? (
            <div className="customer-link-panel">
              <div>
                <strong>客户确认链接</strong>
                <p>把链接发给客户。客户逐项提交后，系统会自动继续整理并报价。</p>
                <a href={`/confirm/${confirmationToken}`} target="_blank" rel="noreferrer">
                  {typeof window !== "undefined" ? `${window.location.origin}/confirm/${confirmationToken}` : `/confirm/${confirmationToken}`}
                </a>
              </div>
              <span><i />等待客户提交</span>
            </div>
          ) : confirmationItems.length ? (
            <div className="confirmation-list">
              {confirmationItems.map((item, index) => (
                <div className="confirmation-item" key={`${index}-${item.question}`}>
                  <label htmlFor={`confirmation-reply-${index}`}>
                    <b>{index + 1}</b><span>{item.question}</span>
                  </label>
                  {item.options.length > 0 && (
                    <ConfigurationOptionPicker
                      className="confirmation-options"
                      options={item.options}
                      value={confirmationAnswers[index]}
                      catalog={item.selection_mode === "catalog" || item.options.some((option) => Boolean(option.model))}
                      onChange={(selected) => setConfirmationAnswers((current) => current.map((answer, answerIndex) => answerIndex === index ? selected : answer))}
                    />
                  )}
                  {item.selection_mode === "text" && item.options.length === 0 && <input
                    id={`confirmation-reply-${index}`}
                    value={confirmationAnswers[index] ?? ""}
                    onChange={(event) => setConfirmationAnswers((current) => current.map((answer, answerIndex) => answerIndex === index ? event.target.value : answer))}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        if (confirmationAnswers.every((answer) => answer.trim())) void submitConfirmationReply();
                      }
                    }}
                    placeholder="填写该问题的客户回复"
                  />}
                  {item.selection_mode !== "text" && item.options.length === 0 && <div
                    className="configuration-picker-empty"
                    role="alert"
                  >官方可选项尚未加载完成，系统已阻止手动填写，请刷新后重试。</div>}
                </div>
              ))}
              <div className="reply-hint">每项分别填写，系统会自动编号对应</div>
            </div>
          ) : (
            <>
              <label htmlFor="confirmation-reply">客户回复</label>
              <textarea
                id="confirmation-reply"
                className="confirmation-reply"
                value={confirmationReply}
                onChange={(event) => setConfirmationReply(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    if (confirmationReply.trim() && !running) void submitConfirmationReply();
                  }
                }}
                placeholder="粘贴客户回复，按 Enter 发送"
              />
              <div className="reply-hint">Enter 发送 · Shift + Enter 换行</div>
            </>
          )}
          <div className="confirmation-actions">
            <button className="secondary" type="button" onClick={async () => {
              if (confirmationToken) {
                await navigator.clipboard.writeText(`${window.location.origin}/confirm/${confirmationToken}`);
                setCopied(true);
              } else {
                await copyConfirmationText();
              }
            }}>
              {copied ? "已复制" : confirmationToken ? "复制客户链接" : "复制确认话术"}
            </button>
            {confirmationItems.length > 0 && !confirmationToken && (
              <button
                className="primary"
                type="button"
                disabled={confirmationAnswers.some((answer) => !answer.trim())}
                onClick={() => void submitConfirmationReply()}
              >
                提交确认
              </button>
            )}
          </div>
        </section>
      )}

      {job?.status === "failed" && job.error && !confirmationText && (
        <section className="error-card">
          <span>!</span>
          <div>
            <p className="kicker">需要检查</p>
            <h2>本次没有生成价格</h2>
            <p>{job.error.message}</p>
            {Array.isArray(job.error.details?.questions) && job.error.details.questions.length > 0 && (
              <div className="error-component-list">
                <strong>需要系统继续处理的问题</strong>
                <ul>
                  {(job.error.details.questions as unknown[]).map((question, index) => (
                    <li key={`${index}-${String(question)}`}>
                      <b>问题 {index + 1}</b>
                      <span>{String(question)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {Array.isArray(job.error.details?.components) && job.error.details.components.length > 0 && (
              <div className="error-component-list">
                <strong>本次未通过的组件</strong>
                <ul>
                  {(job.error.details.components as Array<Record<string, unknown> | string>).map((rawComponent, index) => {
                    const component = typeof rawComponent === "string"
                      ? { display_name: rawComponent }
                      : rawComponent;
                    return (
                      <li key={`${String(component.component_id ?? index)}-${String(component.display_name ?? "")}`}>
                        <b>组件 {String(component.component_id ?? index + 1)} · {String(component.display_name ?? "未识别组件")}</b>
                        {component.reason && <span>{String(component.reason)}</span>}
                        {component.source_text && <small>客户填写：{String(component.source_text)}</small>}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}
            <button
              className="reconnect-button"
              type="button"
              disabled={!requirement.trim()}
              onClick={() => {
                const restartFromPreflight = job.kind === "preview" || [
                  "connection_lost",
                  "official_spec_confirmation_required",
                  "internal_error",
                ].includes(job.error?.code ?? "");
                if (restartFromPreflight) {
                  workflowPhase.current = "idle";
                  window.sessionStorage.removeItem(QUOTE_JOB_CONTEXT_KEY);
                  void runPreflight(
                    requirement,
                    job.kind === "preview" ? previewDraftId ?? undefined : undefined,
                    {},
                    false,
                    cloudProvider,
                  );
                  return;
                }
                void startQuote(
                  requirement,
                  previewDraftId ?? undefined,
                  [{ stage: "recovery", message: "保留全部已确认配置，重新匹配官方计费项", time: "刚刚" }],
                  cloudProvider,
                );
              }}
            >
              保留配置并重新报价
            </button>
          </div>
        </section>
      )}

      {job?.status === "completed" && job.result && (
        <section className="result">
          <div className="result-top">
            <div><p className="kicker">{cloudProvider === "aws" ? "AWS OFFICIAL ESTIMATE" : "MICROSOFT AZURE RETAIL ESTIMATE"} · {job.result.quote_id}</p><h2>{job.result.is_partial ? "部分组件报价待处理" : "官方报价已完成"}</h2><p>{job.result.customer_summary}</p></div>
            <div className="result-summary-actions">
              <div className="scenario-totals">
                {quoteScenarios(job.result).map((scenario) => (
                  <div className="total" key={`${scenario.pricing_mode}-${scenario.reserved_term_years ?? 0}`}>
                    <small>{scenario.label}</small>
                    <strong>{formatMoney(scenario.total_cost, scenario.currency)}</strong>
                    {scenario.upfront_cost > 0 && <span>预付 {formatMoney(scenario.upfront_cost, scenario.currency)}</span>}
                  </div>
                ))}
              </div>
              <div className="result-action-buttons">
                <button className="home-button" type="button" onClick={() => void returnToHome()}>返回首页</button>
                <button className="export-button" type="button" onClick={exportQuoteWorkbook} disabled={exporting}>
                  {exporting ? "正在生成…" : "导出 Excel"}
                </button>
              </div>
            </div>
          </div>
          {job.result.is_partial && (
            <div className="partial-quote-warning" role="alert">
              <strong>当前金额只是已核价组件的小计，不能作为完整预算</strong>
              <span>{job.result.selections
                .filter((selection, index) => (job.result?.incomplete_component_ids ?? [])
                  .includes(selection.component_id ?? String(index)))
                .map((selection) => serviceDisplayName(selection))
                .join("、")} 尚未取得完整官方价格，系统没有猜价，也没有把它们计入合计。</span>
            </div>
          )}
          {customerFacingNotices(job.result.notices).length > 0 && (
            <div className="result-notices">
              <strong>报价说明</strong>
              <ul>{customerFacingNotices(job.result.notices).map((notice) => <li key={notice}>{notice}</li>)}</ul>
            </div>
          )}
          <div className="quote-table-wrap">
            <table className="quote-table">
              <thead><tr><th>#</th><th>AWS 服务</th><th>区域</th><th>型号 / 方案</th><th>配置</th>{quoteScenarios(job.result).map((scenario) => <th key={scenario.label}>{scenario.label}<small>月均成本</small></th>)}<th>缺少用量时的官方单价</th></tr></thead>
              <tbody>
                {hierarchyOrdered(job.result.selections).map(({ item: selection, originalIndex: index }) => (
                  <tr className={`${selection.parent_component_id ? "quote-child-row " : ""}${selection.pricing_status === "unpriced" ? "quote-unpriced-row" : ""}`.trim()} key={`${selection.region}-${index}`}>
                    <td>{selection.component_number ?? String(index + 1).padStart(2, "0")}</td>
                    <td><strong>{selection.parent_component_id ? "↳ " : ""}{serviceDisplayName(selection)}</strong>{selection.parent_component_number && <small className="component-parent-label">由 {selection.parent_component_number} · {selection.parent_display_name ?? "父组件"} 衍生</small>}{quotationRemark(selection) !== "-" && <small className="quote-purpose-note">说明：{quotationRemark(selection)}</small>}<span className={`verified-inline ${selection.pricing_status === "unpriced" ? "unpriced" : ""}`}>{selection.pricing_status === "unpriced" ? "报价异常 · 未计入" : selection.pricing_status === "free" ? `${cloudProvider === "aws" ? "AWS" : "Azure"} 官方免费项 ✓` : (selection.pricing_status === "reference_only" || selection.reference_rates?.length) && serviceCost(job.result!, selection, index) === 0 ? "缺少月用量 · 仅展示单价" : `${cloudProvider === "aws" ? "AWS" : "Azure"} 已核价 ✓`}</span></td>
                    <td>{selection.region}</td>
                    <td><strong>{selection.model}</strong><small>{selection.architecture}</small></td>
                    <td className="quote-specifications">{compactSpecifications(selection) || "-"}</td>
                    {quoteScenarios(job.result!).map((scenario) => {
                      const cost = scenarioServiceCost(scenario, selection, index);
                      const scenarioIncomplete = scenarioComponentIsIncomplete(scenario, selection, index);
                      const componentId = selection.component_id ?? String(index);
                      const pricingBasis = scenario.component_pricing_basis?.[componentId];
                      return <td className="row-cost" key={scenario.label}>
                        {selection.pricing_status === "unpriced" || scenarioIncomplete ? "报价异常" : cost > 0 ? formatMoney(cost, scenario.currency) : ((selection.pricing_status === "reference_only" || selection.reference_rates?.length) ? "缺少用量" : formatMoney(0, scenario.currency))}
                        {scenario.pricing_mode !== "on_demand" && pricingBasis === "on_demand_fallback" && <small>无此预留方案 · 仍按需计费</small>}
                        {pricingBasis === "reserved" && <small>官方承诺价</small>}
                      </td>;
                    })}
                    <td className="reference-rate">{referenceRateText(selection) || "-"}</td>
                  </tr>
                ))}
              </tbody>
              <tfoot><tr><td colSpan={5}>{job.result.is_partial ? "当前已核价组件月均小计" : "官方月均成本合计"}</td>{quoteScenarios(job.result).map((scenario) => <td key={scenario.label}>{formatMoney(scenario.total_cost, scenario.currency)}{scenario.upfront_cost > 0 && <small>预付 {formatMoney(scenario.upfront_cost, scenario.currency)}</small>}</td>)}<td>参考单价未加入小计</td></tr></tfoot>
            </table>
          </div>
          <div className="result-conversation">
            <span>想调整数量或规格？直接修改客户原话，我会重新检查后再报价。</span>
            <button type="button" onClick={reviseRequirement}>调整需求</button>
          </div>
          <footer>{cloudProvider === "aws" ? "已知用量金额读取自 AWS BCM Pricing Calculator API" : "已知用量金额读取自 Microsoft Azure Retail Prices API"}；未提供用量仅展示官方单位参考价且不计入合计。不含税，实际账单可能变化。</footer>
        </section>
      )}
    </main>
  );
}
