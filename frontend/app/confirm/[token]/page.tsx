"use client";

import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { ConfigurationOptionPicker, type ConfigurationChoice } from "../../components/configuration-option-picker";

type Item = {
  question: string;
  answer_key?: string | null;
  options: ConfigurationChoice[];
  dependent_options?: ConfigurationChoice[];
  dependent_on_values?: string[];
  component_id?: string | null;
  service?: string | null;
  selection_mode?: "text" | "buttons" | "catalog";
};

function confirmationAnswerKey(item: Item): string {
  return item.answer_key ?? item.question;
}

function confirmationComplete(item: Item, answer?: string): boolean {
  const compact = answer?.trim() ?? "";
  if (!compact) return false;
  const baseValue = compact.split("；", 1)[0];
  const requiresDependentChoice = (item.dependent_on_values ?? []).includes(baseValue);
  return !requiresDependentChoice || /；选择\s+[^；]+；机器数量\s+\d+/.test(compact);
}

function configurationForConfirmation(
  item: Item,
  configurations: ConfigurationItem[],
): ConfigurationItem | undefined {
  const exact = configurations.find(
    (configuration) => configuration.component_id === item.component_id,
  );
  if (exact) return exact;
  const zeroBasedIndex = Number(item.component_id);
  if (!Number.isInteger(zeroBasedIndex) || zeroBasedIndex < 0) return undefined;
  return configurations.find(
    (configuration) => String(configuration.component_number) === String(zeroBasedIndex + 1),
  );
}

function choiceNumber(choice: ConfigurationChoice, keys: string[]): number | null {
  for (const key of keys) {
    const value = choice.specifications?.[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
    if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) {
      return Number(value);
    }
  }
  return null;
}

type ProcessorArchitecture = "arm64" | "x86_64";

function choiceArchitecture(choice: ConfigurationChoice): ProcessorArchitecture | null {
  const declared = choice.specifications?.processorArchitecture
    ?? choice.specifications?.processor_architecture;
  const plural = choice.specifications?.processorArchitectures
    ?? choice.specifications?.architectures;
  const declaredValues = [
    ...(Array.isArray(plural) ? plural : plural === undefined ? [] : [plural]),
    ...(declared === undefined ? [] : [declared]),
  ];
  for (const value of declaredValues) {
    if (typeof value !== "string") continue;
    const normalized = value.trim().toLowerCase();
    if (["arm", "arm64", "aarch64", "graviton"].includes(normalized)) return "arm64";
    if (["x86", "x86_64", "amd64", "i386"].includes(normalized)) return "x86_64";
  }
  const model = (choice.model ?? "").trim().toLowerCase();
  if (!model.includes(".")) return null;
  const segments = model.split(".");
  if (segments.some((segment) => segment === "a1" || segment === "mac2" || segment.startsWith("mac2-"))) return "arm64";
  if (segments.some((segment) => /\d+g[a-z]*$/.test(segment))) return "arm64";
  return "x86_64";
}

function itemHasModelChoices(item: Item): boolean {
  return [...item.options, ...(item.dependent_options ?? [])].some(
    (choice) => Boolean(choice.model && choiceArchitecture(choice)),
  );
}

function choicesForArchitecture(
  choices: ConfigurationChoice[],
  architecture: ProcessorArchitecture,
): ConfigurationChoice[] {
  const matching = choices.filter((choice) => choiceArchitecture(choice) === architecture);
  if (matching.length > 0) return matching;
  // ARM is the default preference. AWS services that have no ARM model must
  // keep their official x86-only catalogue available. An explicit x86 choice
  // never falls back to ARM.
  return architecture === "arm64" ? choices : [];
}

function preferredDependentChoice(
  item: Item,
  configuration?: ConfigurationItem,
  architecture: ProcessorArchitecture = "arm64",
): ConfigurationChoice | undefined {
  const options = choicesForArchitecture(item.dependent_options ?? [], architecture);
  if (options.length === 0 || !configuration) return undefined;
  const requestedModel = typeof configuration.requirements.requested_model === "string"
    ? configuration.requirements.requested_model
    : configuration.selected_model;
  const requestedVcpu = Number(configuration.requirements.vcpu);
  const requestedMemory = Number(configuration.requirements.memory_gib);
  const hasVcpu = Number.isFinite(requestedVcpu) && requestedVcpu > 0;
  const hasMemory = Number.isFinite(requestedMemory) && requestedMemory > 0;
  if (requestedModel) {
    const modelMatch = options.find((option) => option.model === requestedModel);
    const modelCpu = modelMatch ? choiceNumber(modelMatch, ["vCPU", "vcpu", "vcpus"]) : null;
    const modelMemory = modelMatch
      ? choiceNumber(modelMatch, ["memoryGiB", "memory_gib", "memory"])
      : null;
    if (
      modelMatch
      && (!hasVcpu || modelCpu === requestedVcpu)
      && (!hasMemory || modelMemory === requestedMemory)
    ) return modelMatch;
  }
  // Missing customer facts must stay visible for confirmation.  Automatic
  // selection is safe only when the requested CPU and memory are both known.
  if (!hasVcpu || !hasMemory) return undefined;
  // Prefill only a real exact match. When AWS has no exact machine, leave the
  // picker open so the customer explicitly chooses one official alternative.
  const exactOptions = options.filter((option) => (
    choiceNumber(option, ["vCPU", "vcpu", "vcpus"]) === requestedVcpu
    && choiceNumber(option, ["memoryGiB", "memory_gib", "memory"]) === requestedMemory
  ));
  return exactOptions.sort((left, right) => (
    (left.monthly_catalog_cost ?? Number.POSITIVE_INFINITY)
      - (right.monthly_catalog_cost ?? Number.POSITIVE_INFINITY)
    || (left.model ?? left.label).localeCompare(right.model ?? right.label)
  ))[0];
}

function dependentSelectionValue(
  item: Item,
  baseValue: string,
  configuration?: ConfigurationItem,
  architecture: ProcessorArchitecture = "arm64",
): string {
  if (!(item.dependent_on_values ?? []).includes(baseValue)) return baseValue;
  const choice = preferredDependentChoice(item, configuration, architecture);
  if (!choice || !configuration) return baseValue;
  return `${baseValue}；${choice.value}；机器数量 ${Math.max(configuration.quantity, 1)}`;
}

function isRegionConfirmation(item: Item): boolean {
  const question = item.question.trim();
  return question.includes("区域") && (
    question.includes("Azure 部署区域")
    || question.includes("支持该服务的 Azure 区域")
    || question.includes("的部署区域")
    || question.includes("部署在哪")
    || question.includes("哪个 AWS 区域")
    || question.includes("请选择区域")
    || question.includes("未指定区域")
  );
}
type ConfigurationItem = {
  component_id: string;
  component_number?: string | null;
  parent_component_id?: string | null;
  parent_component_number?: string | null;
  parent_display_name?: string | null;
  service: string;
  display_name: string;
  region?: string | null;
  quantity: number;
  selected_model?: string | null;
  official_specifications?: Record<string, unknown>;
  available_shapes?: Array<{ model?: string; vcpu: number; memory_gib: number }>;
  available_options?: Record<string, EditableValue[]>;
  available_billing_fields?: string[];
  available_billing_labels?: Record<string, string>;
  pricing_status: "ready" | "unpriced";
  pricing_notice?: string | null;
  pricing_issue_code?: string | null;
  pricing_issue_category?: "retryable" | "compatibility" | "catalog_mapping" | "system_configuration" | "unsupported" | null;
  requirements: Record<string, unknown>;
  source_text: string;
};

function hierarchyOrderedConfigurationItems(items: ConfigurationItem[]) {
  const entries = items.map((item, originalIndex) => ({ item, originalIndex }));
  const knownIds = new Set(entries.map(({ item }) => item.component_id));
  const children = new Map<string, typeof entries>();
  entries.forEach((entry) => {
    const parentId = entry.item.parent_component_id;
    if (!parentId || !knownIds.has(parentId) || parentId === entry.item.component_id) return;
    children.set(parentId, [...(children.get(parentId) ?? []), entry]);
  });
  const ordered: typeof entries = [];
  const visited = new Set<string>();
  const append = (entry: (typeof entries)[number]) => {
    if (visited.has(entry.item.component_id)) return;
    visited.add(entry.item.component_id);
    ordered.push(entry);
    (children.get(entry.item.component_id) ?? []).forEach(append);
  };
  entries
    .filter((entry) => !entry.item.parent_component_id
      || !knownIds.has(entry.item.parent_component_id))
    .forEach(append);
  entries.forEach(append);
  return ordered;
}
type EditableValue = string | number | boolean | null;
type ComponentUpdate = {
  region?: string;
  quantity?: number;
  requirements?: Record<string, EditableValue>;
};
type ComponentDraft = {
  region: string;
  quantity: number | "";
  requirements: Record<string, EditableValue>;
};
type Session = {
  token: string;
  cloud_provider: "aws" | "azure";
  status: "pending" | "submitted" | "reviewing" | "processing" | "configuration_review" | "approved" | "completed";
  customer_summary: string;
  confirmation_text?: string | null;
  confirmation_items: Item[];
  answers: Record<string, string>;
  configuration_items: ConfigurationItem[];
};

type PendingAddition = {
  sourceText: string;
  existingComponentIds: string[];
  expectedNumber: number;
};

function additionStorageKey(token: string): string {
  return `astraquote:aws:addition:${token}`;
}

function readPendingAddition(token: string): PendingAddition | null {
  if (typeof window === "undefined" || !token) return null;
  try {
    const raw = window.sessionStorage.getItem(additionStorageKey(token));
    if (!raw || raw === "active") return null;
    const parsed = JSON.parse(raw) as Partial<PendingAddition>;
    if (!parsed.sourceText || !Array.isArray(parsed.existingComponentIds)) return null;
    return {
      sourceText: parsed.sourceText,
      existingComponentIds: parsed.existingComponentIds.map(String),
      expectedNumber: Number(parsed.expectedNumber) || parsed.existingComponentIds.length + 1,
    };
  } catch {
    return null;
  }
}

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/backend";
const EXPECTED_CLOUD_PROVIDER = "aws" as const;
const PROCESSOR_ARCHITECTURE_ANSWER_KEY = "__processor_architecture__";
const DELETE_COMPONENT_MARKER = "__DELETE_COMPONENT__";
const FIELD_LABELS: Record<string, string> = {
  requested_model: "官方型号",
  vcpu: "处理器", memory_gib: "内存", operating_system: "操作系统",
  architecture: "处理器架构", tenancy: "租用方式", business_type: "实例用途",
  system_disk_gib: "系统盘", total_system_disk_gib: "系统盘总容量",
  additional_ebs_volumes: "数据盘", size_gib: "容量", count_per_instance: "每台数量",
  total_worker_system_disk_gib: "工作节点系统盘总容量", storage_gib: "单项存储容量",
  total_storage_gib: "总存储容量", storage_gib_per_node: "每节点存储",
  storage_gib_per_broker: "每个 Broker 存储", engine: "数据库或缓存引擎", engine_version: "引擎版本",
  operating_system_version: "操作系统版本", traffic_geography: "访问者流量地区",
  deployment: "部署方式", cluster_members: "数据库实例数", broker_count: "Broker 节点数", data_nodes: "数据节点数",
  master_nodes: "主节点数", master_requested_model: "主节点型号",
  master_vcpu: "主节点处理器", master_memory_gib: "主节点内存",
  master_storage_gib_per_node: "主节点单节点存储",
  core_nodes: "核心节点数", core_requested_model: "核心节点型号",
  core_vcpu: "核心节点处理器", core_memory_gib: "核心节点内存",
  core_storage_gib_per_node: "核心节点单节点存储",
  task_nodes: "任务节点数", task_requested_model: "任务节点型号",
  task_vcpu: "任务节点处理器", task_memory_gib: "任务节点内存",
  task_storage_gib_per_node: "任务节点单节点存储",
  shards: "分片数", replicas_per_shard: "每分片副本数",
  node_count: "节点数", load_balancer_type: "负载均衡类型", storage_class: "存储类型",
  storage_type: "磁盘类型", volume_type: "磁盘类型", api_type: "API 类型",
  web_acls: "Web ACL 数量", rules: "规则数量", purpose: "用途", purchase_option: "购买方式",
  reserved_term_years: "预留期限", payment_option: "付款方式", utilization_percent: "预计使用率",
  cluster_type: "集群类型", cluster_mode: "集群模式", gateway_count: "网关数量",
  cluster_count: "集群数量", instance_count: "实例数量", replication_instances: "复制实例数量",
  kubernetes_version: "Kubernetes 版本", support_tier: "版本支持级别",
  control_plane_hours: "集群控制面运行小时",
  applications: "应用框架", deployment_type: "部署类型", nodes: "计算节点数",
  managed_storage_gib: "托管存储容量", snapshot_storage_gib: "快照存储容量",
  rpu: "计算容量（RPU）", hours_per_month: "每月运行时长",
  data_scanned_gib: "查询扫描数据量", queries: "查询次数",
  provisioned_dpu_hours: "预置容量（DPU 小时）",
  requested_sku: "Azure SKU", service_tier: "服务层级", compute_model: "计算模式",
  disk_type: "磁盘类型", disk_size_gib: "磁盘容量", capacity: "容量等级",
  replicas: "副本数", high_availability: "高可用", license_model: "许可模式",
  data_retrieval_gib: "数据读取量", log_ingestion_gib: "日志摄入量",
  secret_count: "密钥数量", key_count: "KMS 密钥数量", vpc_count: "VPC 数量",
  public_subnets: "公有子网数量", private_subnets: "私有子网数量", availability_zones: "可用区数量",
  data_transfer_out_gib: "每月出站流量", data_processed_gib: "每月处理数据量",
  data_transfer_in_gib: "每月入站流量",
  requests: "每月请求量", memory_mb: "函数内存", duration_ms: "平均执行时长",
  flow_runs: "每月流程运行次数", bucket_count: "存储桶数量", object_count: "对象数量",
  messages: "每月消息数", connection_minutes: "每月连接分钟",
  throughput_mbps: "吞吐量（MB/s）", throughput_mbps_per_tib: "每 TiB 吞吐量（MB/s/TiB）",
  https_requests: "每月 HTTPS 请求量", listeners: "监听器数量",
  iops: "IOPS", storage_iops: "存储 IOPS", storage_throughput_mbps: "存储吞吐量",
  backup_retention_days: "备份保留天数", read_replica_count: "只读副本数",
  detailed_monitoring: "详细监控", performance_insights: "性能分析",
  enhanced_monitoring: "增强监控", dedicated_master: "专用主节点",
  data_transfer_regional_gib: "每月区域内流量", snapshot_changed_gib: "每月快照增量",
  repositories: "镜像仓库数量", image_scans: "镜像扫描次数",
  put_copy_post_list_requests: "PUT/COPY/POST/LIST 请求量",
  get_select_requests: "GET/SELECT 请求量", processed_bytes_gib: "每月处理数据量",
  hosted_zones: "托管区域数量", health_checks: "健康检查数量",
  outbound_messages: "每月外发邮件数", inbound_messages: "每月接收邮件数",
  attachments_gib: "邮件附件流量", log_storage_gib: "日志存储量",
  custom_metrics: "自定义指标数量", alarms: "告警数量",
  active_series: "活跃指标序列", samples_ingested: "写入样本数",
  query_samples_processed: "查询处理样本数", collector_hours: "采集器运行小时",
  backup_storage_gib: "备份存储量", restore_gib: "恢复数据量",
  deployment_updates: "本地服务器更新次数", author_users: "作者数量",
  reader_users: "读者数量", session_capacity: "读者会话次数",
  spice_gib: "SPICE 容量", endpoint_hours: "端点运行小时",
  resource_count: "计费资源数量", memory_store_gib_hours: "内存存储量（GiB 小时）",
  magnetic_store_gib_months: "磁性存储量（GiB 月）",
  accelerators: "加速器数量", broker_hours: "Broker 运行小时",
  scheduled_invocations: "计划调用次数", schedules: "计划数量",
  io_requests: "I/O 请求量", api_calls: "每月服务发现 API 调用量",
  dns_queries: "每月 DNS 查询量（Route 53）", namespaces: "DNS 命名空间数量（Route 53）",
  service_instances: "注册资源数量", configuration_requests: "配置请求量",
  configuration_retrievals: "接收配置次数", experiment_hours: "实验运行小时",
  targets_receiving_configuration: "接收配置的目标数量",
  events: "事件数量", event_buses: "事件总线数量",
  schema_discovery_events: "Schema Discovery 事件数", pipes_requests: "Pipes 请求量",
  state_transitions: "状态转换次数", duration_gb_seconds: "执行时长（GB-秒）",
  input_tokens: "输入 Token 数", output_tokens: "输出 Token 数", images: "图片数量",
  data_in_gib: "每月写入数据量", data_out_gib: "每月读取数据量",
  throughput_mode: "吞吐模式", provisioned_throughput_mibps: "预置吞吐量",
  capacity_mode: "容量模式", put_payload_units: "写入计费单位数",
  read_request_units: "读请求单位", write_request_units: "写请求单位",
  active_connections_per_minute: "每分钟活动连接数",
  advanced_security: "高级安全功能",
  aurora_cluster: "Aurora 集群",
  average_connection_duration_seconds: "平均连接时长（秒）",
  backup_frequency: "备份频率", cold_storage_gib: "冷存储容量",
  crawler_dpu_hours: "Crawler 运行量（DPU 小时）",
  cross_region_copy_gib: "跨区域复制数据量",
  data_catalog_objects: "数据目录对象数量", data_tiering: "数据分层",
  data_transfer_in_gib_per_instance: "每台每月入站流量",
  data_transfer_out_gib_per_instance: "每台每月出站流量",
  data_transfer_regional_gib_per_instance: "每台每月区域内流量",
  deliveries: "投递数量", delivery_type: "投递方式",
  deployment_mode: "部署模式", destination: "目标位置",
  destination_geography: "目标地区", dpu_hours: "运行量（DPU 小时）",
  ebs_iops: "磁盘 IOPS", ebs_throughput_mbps: "磁盘吞吐量",
  edition: "版本", endpoint_count: "端点数量", endpoint_type: "终端类型",
  engine_type: "引擎类型", ephemeral_storage_gib: "临时存储容量",
  ephemeral_storage_mb: "临时存储容量（MB）",
  extended_retention_hours: "延长保留时长（小时）",
  file_system_type: "文件系统类型", include_logs: "包含日志",
  include_metrics: "包含指标", instance_hours: "实例运行小时",
  interactive_session_dpu_hours: "交互会话运行量（DPU 小时）",
  job_count: "作业数量", job_type: "作业类型", key_type: "密钥类型",
  launch_type: "运行方式", lcu_count: "LCU 数量",
  lifecycle_policy: "生命周期规则", listener_count: "监听器数量",
  machine_to_machine_tokens: "机器间访问令牌数量",
  monthly_active_users: "每月活跃用户数", multi_az: "多可用区部署",
  new_connections_per_second: "每秒新建连接数",
  payload_size_kib: "单次消息大小（KiB）", price_class: "价格区域范围",
  processed_bytes_ec2_ip_gib_per_hour: "每小时处理数据量",
  protected_resource: "受保护资源", protected_service: "备份对象服务",
  provisioned_concurrency: "预置并发数",
  provisioned_throughput_units: "预置吞吐容量",
  queue_type: "队列类型", request_size_mb: "单次请求大小（MB）",
  requests_per_second: "每秒请求数", rotation_enabled: "自动轮换",
  rule_evaluations_per_request: "每次请求规则检查数",
  rule_evaluations_per_second: "每秒规则检查数", scheme: "访问方式",
  shard_hours: "分片运行小时", snapshot_frequency: "快照频率",
  snapshot_retention_days: "快照保留天数", source_regions: "来源区域",
  source_storage_gib_per_node: "客户原环境容量（仅迁移参考，不计费）",
  streams_read_requests: "Streams 读取请求量",
  task_count: "任务数量", task_hours: "任务运行小时", tasks: "运行任务数",
  traces_recorded: "记录的追踪数量", traces_retrieved: "查询的追踪数量",
  traces_stored: "存储的追踪数量", user_count: "用户数量", users: "用户数量",
  warm_node_count: "Warm 节点数量", warm_storage_gib: "温存储容量",
  worker_management: "工作节点管理方式", worker_memory_gib: "工作节点内存",
  worker_node_count: "工作节点总数", worker_nodes_per_cluster: "每集群工作节点数",
  worker_requested_model: "工作节点型号", worker_system_disk_gib: "工作节点系统盘",
  worker_vcpu: "工作节点处理器", workflow_type: "工作流类型",
  hours_per_user_per_day: "每位用户每天使用小时",
  kpu_count: "KPU 数量", kpu_hours: "KPU 运行小时",
  magnetic_retention_days: "磁性存储保留天数",
  memory_retention_hours: "内存存储保留小时",
  product_variant: "产品类型", quantity_detail: "数量说明",
  reader_nodes: "只读节点数", replica_count: "副本数量",
  user_volume_gib: "用户盘容量", write_records: "写入记录数量",
  writer_nodes: "写入节点数",
};

const AWS_SERVICE_EDIT_FIELDS: Record<string, string[]> = {
  ec2: ["vcpu", "memory_gib", "system_disk_gib", "operating_system", "operating_system_version", "architecture", "purpose"],
  rds: ["engine", "engine_version", "vcpu", "memory_gib", "storage_gib", "deployment", "storage_type", "backup_retention_days", "read_replica_count"],
  elasticache: ["engine", "vcpu", "memory_gib", "node_count", "shards", "replicas_per_shard", "deployment"],
  s3: ["storage_gib", "storage_class"],
  msk: ["broker_count", "vcpu", "memory_gib", "storage_gib_per_broker", "cluster_type"],
  opensearch: ["data_nodes", "vcpu", "memory_gib", "storage_gib_per_node", "dedicated_master", "master_nodes"],
  eks: ["support_tier"],
  cloudfront: ["traffic_geography"],
  waf: ["web_acls", "rules"],
  elb: ["load_balancer_type", "listeners"],
  apigateway: ["api_type"],
  fsx: ["file_system_type", "storage_gib", "throughput_mbps_per_tib"],
  nat_gateway: ["gateway_count"],
  ebs: ["storage_gib", "volume_type", "storage_iops", "storage_throughput_mbps"],
  route53: [],
  global_accelerator: [],
  cloudwatch: ["log_ingestion_gib"],
};

const AZURE_SERVICE_EDIT_FIELDS: Record<string, string[]> = {};

// Optional billing dimensions are service contracts, not a universal menu.
// Never show one product's storage/log/query fields on an unrelated product.
// A future service with no known contract intentionally shows no extra menu;
// its fields appear after the backend discovers and persists them.
const AWS_SERVICE_OPTIONAL_USAGE_FIELDS: Record<string, string[]> = {
  ec2: ["data_transfer_out_gib", "data_transfer_in_gib", "data_transfer_regional_gib", "snapshot_changed_gib"],
  eks: ["control_plane_hours"],
  ecr: ["storage_gib", "image_scans", "data_transfer_out_gib"],
  rds: ["storage_gib", "storage_iops", "storage_throughput_mbps", "backup_storage_gib"],
  elasticache: ["backup_storage_gib"],
  elb: ["processed_bytes_gib", "requests"],
  s3: ["storage_gib", "put_copy_post_list_requests", "get_select_requests", "data_retrieval_gib", "data_transfer_out_gib"],
  cloudfront: ["data_transfer_out_gib", "https_requests"],
  route53: ["hosted_zones", "dns_queries", "health_checks"],
  waf: ["requests"],
  sqs: ["requests"],
  ses: ["outbound_messages", "inbound_messages", "attachments_gib"],
  pinpoint: ["outbound_messages"],
  cloudwatch: ["log_ingestion_gib", "log_storage_gib", "custom_metrics", "alarms"],
  amp: ["active_series", "samples_ingested", "query_samples_processed", "collector_hours", "storage_gib"],
  prometheus: ["active_series", "samples_ingested", "query_samples_processed", "collector_hours", "storage_gib"],
  backup: ["backup_storage_gib", "restore_gib"],
  ebs: ["storage_gib", "storage_iops", "storage_throughput_mbps"],
  data_transfer: ["data_transfer_out_gib"],
  global_accelerator: ["data_transfer_out_gib"],
  msk: ["broker_hours", "storage_gib_per_broker", "data_transfer_in_gib", "data_transfer_out_gib"],
  apigateway: ["requests", "messages", "connection_minutes", "data_transfer_out_gib"],
  fsx: ["storage_gib", "throughput_mbps", "throughput_mbps_per_tib", "iops", "backup_storage_gib"],
  scheduler: ["scheduled_invocations", "schedules"],
  opensearch: ["total_storage_gib", "data_transfer_out_gib"],
  documentdb: ["storage_gib", "io_requests", "backup_storage_gib"],
  nat_gateway: ["hours_per_month", "data_processed_gib"],
  secrets_manager: ["secret_count", "api_calls"],
  dms: ["hours_per_month", "storage_gib", "data_processed_gib"],
  kms: ["key_count", "requests"],
  lambda: ["memory_mb", "duration_ms", "requests"],
  dynamodb: ["read_request_units", "write_request_units", "storage_gib", "backup_storage_gib", "restore_gib"],
  efs: ["storage_gib", "provisioned_throughput_mibps", "data_in_gib", "data_out_gib"],
  sns: ["requests", "data_transfer_out_gib"],
  kinesis: ["data_in_gib", "data_out_gib"],
  redshift: ["managed_storage_gib", "snapshot_storage_gib", "hours_per_month"],
  athena: ["data_scanned_gib", "queries", "provisioned_dpu_hours"],
  sagemaker: ["hours_per_month", "storage_gib"],
  mq: ["storage_gib", "hours_per_month"],
  step_functions: ["state_transitions", "requests", "duration_gb_seconds"],
  bedrock: ["input_tokens", "output_tokens", "images"],
  cloud_map: ["service_instances", "api_calls", "dns_queries", "namespaces"],
  appconfig: ["configuration_requests", "configuration_retrievals", "experiment_hours"],
  eventbridge: ["events", "event_buses", "schema_discovery_events", "pipes_requests"],
};
const SELECT_FIELD_OPTIONS: Record<string, string[]> = {
  operating_system: ["linux", "windows"],
  architecture: ["x86_64", "arm64"],
  tenancy: ["shared", "dedicated", "host"],
  deployment: ["single_az", "multi_az", "multi_az_cluster"],
  storage_type: ["gp2", "gp3", "io1", "io2"],
  volume_type: ["gp2", "gp3", "io1", "io2", "st1", "sc1", "standard"],
  cluster_type: ["provisioned", "serverless"],
  load_balancer_type: ["application", "network", "gateway"],
  api_type: ["http", "rest", "websocket"],
  business_type: ["general_purpose", "compute_optimized", "memory_optimized", "storage_optimized", "accelerated_computing"],
  cluster_mode: ["disabled", "enabled"],
  deployment_type: ["single_instance", "multi_instance", "serverless"],
  traffic_geography: ["Asia Pacific", "United States", "Europe", "Japan", "Australia", "Canada"],
};

const AWS_SERVICE_SELECT_OPTIONS: Record<string, Record<string, string[]>> = {
  ec2: {
    operating_system: ["linux", "windows"],
    architecture: ["x86_64", "arm64"],
    tenancy: ["shared", "dedicated", "host"],
  },
  rds: {
    engine: [
      "mysql", "postgresql", "mariadb", "aurora_mysql", "aurora_postgresql",
      "oracle_ee", "oracle_se2", "sqlserver_ee", "sqlserver_se", "sqlserver_ex", "sqlserver_web",
    ],
    deployment: ["single_az", "multi_az", "multi_az_cluster"],
    storage_type: ["gp2", "gp3", "io1", "io2", "standard"],
  },
  elasticache: {
    engine: ["valkey", "redis", "memcached"],
    deployment: ["single_az", "multi_az"],
    cluster_mode: ["disabled", "enabled"],
  },
  s3: {
    storage_class: [
      "standard", "intelligent_tiering", "standard_ia", "one_zone_ia",
      "glacier_instant_retrieval", "glacier_flexible_retrieval", "deep_archive",
    ],
  },
  msk: {
    cluster_type: ["provisioned", "serverless"],
    storage_type: ["ebs"],
  },
  opensearch: {
    storage_type: ["gp2", "gp3", "io1"],
  },
  elb: {
    load_balancer_type: ["application", "network", "gateway"],
  },
  apigateway: {
    api_type: ["http", "rest", "websocket"],
  },
  ebs: {
    volume_type: ["gp3", "gp2", "io2", "io1", "st1", "sc1", "standard"],
  },
  eks: {
    support_tier: ["standard", "extended"],
  },
};

const OFFICIAL_OPTION_ALIASES: Record<string, string> = {
  "s3 standard": "standard",
  linux: "linux",
  windows: "windows",
  shared: "shared",
  rest: "rest",
  http: "http",
  websocket: "websocket",
};

function configuredFieldOptions(
  item: ConfigurationItem,
  field: string,
  isAzure = false,
): EditableValue[] {
  // Azure reuses the editor mechanism only; option data stays provider-specific.
  const serviceOptions = isAzure ? [] : (AWS_SERVICE_SELECT_OPTIONS[item.service]?.[field] ?? []);
  const commonOptions = isAzure ? [] : (SELECT_FIELD_OPTIONS[field] ?? []);
  const discoveredOptions = (item.available_options?.[field] ?? [])
    .filter((option) => option !== null)
    .map((option) => typeof option === "string"
      ? (OFFICIAL_OPTION_ALIASES[option.trim().toLowerCase()] ?? option)
      : option);
  return [...serviceOptions, ...commonOptions, ...discoveredOptions].filter(
    (option, optionIndex, allOptions) => allOptions.findIndex(
      (candidate) => String(candidate).toLowerCase() === String(option).toLowerCase(),
    ) === optionIndex,
  );
}

function uniqueNumericOptions(values: number[]): number[] {
  return Array.from(new Set(values.filter(Number.isFinite))).sort((left, right) => left - right);
}

function availableShapes(
  item: ConfigurationItem,
  draft: ComponentDraft,
  liveShapes: Array<{ model?: string; vcpu: number; memory_gib: number }> = [],
) {
  const shapes = [...(item.available_shapes ?? []), ...liveShapes].filter(
    (shape) => Number.isFinite(shape.vcpu) && Number.isFinite(shape.memory_gib),
  );
  const currentVcpu = draft.requirements.vcpu;
  const currentMemory = draft.requirements.memory_gib;
  if (typeof currentVcpu === "number" && typeof currentMemory === "number") {
    shapes.push({ vcpu: currentVcpu, memory_gib: currentMemory });
  }
  return shapes.filter((shape, index, all) => all.findIndex(
    (candidate) => candidate.vcpu === shape.vcpu && candidate.memory_gib === shape.memory_gib,
  ) === index);
}

function pairedShapeField(field: string, target: "cpu" | "memory"): string {
  if (target === "memory") {
    return field === "vcpu" ? "memory_gib" : field.replace(/_vcpu$/, "_memory_gib");
  }
  return field === "memory_gib" ? "vcpu" : field.replace(/_memory_gib$/, "_vcpu");
}

function buildComponentDraft(item: ConfigurationItem, isAzure = false): ComponentDraft {
  const requirements: Record<string, EditableValue> = {};
  Object.entries(item.requirements).forEach(([key, value]) => {
    if (!HIDDEN_CONFIGURATION_FIELDS.has(key) && ["string", "number", "boolean"].includes(typeof value)) {
      requirements[key] = value as EditableValue;
    }
  });
  if (item.selected_model) {
    requirements[isAzure ? "requested_sku" : "requested_model"] = item.selected_model;
  }
  // Old S3 confirmation rows can already display "Standard" from the
  // selected plan while lacking the equivalent editable requirement. Carry
  // that official selection into the editor so it never opens on a misleading
  // "请选择" placeholder.
  if (
    !isAzure
    && item.service === "s3"
    && !requirements.storage_class
    && /standard|标准存储/i.test(String(item.selected_model ?? ""))
  ) {
    requirements.storage_class = "standard";
  }
  const usesInstanceSizing = isAzure || AWS_INSTANCE_SIZED_SERVICES.has(item.service);
  if (usesInstanceSizing && typeof item.official_specifications?.vCPU === "number") {
    requirements.vcpu = item.official_specifications.vCPU as number;
  }
  if (usesInstanceSizing && typeof item.official_specifications?.memoryGiB === "number") {
    requirements.memory_gib = item.official_specifications.memoryGiB as number;
  }
  return { region: item.region ?? "", quantity: item.quantity, requirements };
}

function editableRequirementFields(
  item: ConfigurationItem,
  draft: ComponentDraft,
  additional: string[] = [],
  isAzure = false,
): string[] {
  const known = isAzure
    ? (AZURE_SERVICE_EDIT_FIELDS[item.service] ?? [])
    : (AWS_SERVICE_EDIT_FIELDS[item.service] ?? []);
  const existing = Object.keys(draft.requirements).filter(
    (key) => !HIDDEN_CONFIGURATION_FIELDS.has(key)
      && !key.startsWith("_")
      && draft.requirements[key] !== null
      && draft.requirements[key] !== "",
  );
  const discovered = Object.keys(item.available_options ?? {}).filter(
    (key) => key in draft.requirements || key in FIELD_LABELS,
  );
  return Array.from(new Set([...known, ...existing, ...discovered, ...additional]))
    .filter((key) => key !== "requested_model")
    .filter((key) => item.service !== "eks" || !EKS_WORKER_CONFIGURATION_FIELDS.has(key))
    .filter((key) => isAzure
      || AWS_INSTANCE_SIZED_SERVICES.has(item.service)
      || !GENERIC_INSTANCE_CONFIGURATION_FIELDS.has(key));
}

function isUsageGibField(field: string): boolean {
  return field.endsWith("_gib") || field.includes("_gib_");
}

function editableFieldLabel(field: string): string {
  const label = FIELD_LABELS[field] ?? field
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
  if (field === "vcpu" || field.endsWith("_vcpu")) return `${label}（vCPU）`;
  if (field.endsWith("_gib") || field.includes("_gib_")) return `${label}（GiB）`;
  return label;
}

const REGION_LABELS: Record<string, string> = {
  "us-east-1": "美国东部（弗吉尼亚北部）（us-east-1）", "us-east-2": "美国东部（俄亥俄）（us-east-2）",
  "us-west-1": "美国西部（加利福尼亚北部）（us-west-1）", "us-west-2": "美国西部（俄勒冈）（us-west-2）",
  "af-south-1": "开普敦（af-south-1）", "ap-east-1": "香港（ap-east-1）", "ap-east-2": "台北（ap-east-2）",
  "ap-south-1": "孟买（ap-south-1）", "ap-south-2": "海得拉巴（ap-south-2）",
  "ap-southeast-1": "新加坡（ap-southeast-1）", "ap-southeast-2": "悉尼（ap-southeast-2）",
  "ap-southeast-3": "雅加达（ap-southeast-3）", "ap-southeast-4": "墨尔本（ap-southeast-4）",
  "ap-southeast-5": "马来西亚（ap-southeast-5）", "ap-southeast-6": "新西兰（ap-southeast-6）",
  "ap-southeast-7": "泰国（ap-southeast-7）", "ap-northeast-1": "东京（ap-northeast-1）",
  "ap-northeast-2": "首尔（ap-northeast-2）", "ap-northeast-3": "大阪（ap-northeast-3）",
  "ca-central-1": "加拿大中部（ca-central-1）", "ca-west-1": "卡尔加里（ca-west-1）",
  "eu-central-1": "法兰克福（eu-central-1）", "eu-central-2": "苏黎世（eu-central-2）",
  "eu-west-1": "爱尔兰（eu-west-1）", "eu-west-2": "伦敦（eu-west-2）", "eu-west-3": "巴黎（eu-west-3）",
  "eu-north-1": "斯德哥尔摩（eu-north-1）", "eu-south-1": "米兰（eu-south-1）", "eu-south-2": "西班牙（eu-south-2）",
  "il-central-1": "特拉维夫（il-central-1）", "mx-central-1": "墨西哥中部（mx-central-1）",
  "me-south-1": "巴林（me-south-1）", "me-central-1": "阿联酋（me-central-1）", "sa-east-1": "圣保罗（sa-east-1）",
  southeastasia: "东南亚（新加坡）（southeastasia）", eastasia: "东亚（香港）（eastasia）",
  japaneast: "日本东部（东京）（japaneast）", eastus: "美国东部（eastus）",
  westus: "美国西部（westus）", westeurope: "西欧（westeurope）",
  uksouth: "英国南部（伦敦）（uksouth）",
  global: "全球",
};

const VALUE_LABELS: Record<string, string> = {
  on_demand: "按需付费", standard_reserved: "标准预留实例", convertible_reserved: "可转换预留实例",
  no_upfront: "无预付", partial_upfront: "部分预付", all_upfront: "全预付",
  multi_az: "多可用区主备", multi_az_cluster: "多可用区集群", single_az: "单可用区",
  provisioned: "预置容量集群", serverless: "无服务器集群", application: "应用型负载均衡器",
  network: "网络型负载均衡器", gateway: "网关型负载均衡器", shared: "共享实例",
  linux: "Linux", windows: "Windows", standard: "标准存储", http: "HTTP API", rest: "REST API",
  ebs: "EBS 云硬盘", redis: "Redis", mysql: "MySQL", postgresql: "PostgreSQL",
  valkey: "Valkey", memcached: "Memcached", mariadb: "MariaDB",
  aurora_mysql: "Aurora MySQL", aurora_postgresql: "Aurora PostgreSQL",
  oracle_ee: "Oracle Enterprise Edition", oracle_se2: "Oracle Standard Edition 2",
  sqlserver_ee: "SQL Server Enterprise", sqlserver_se: "SQL Server Standard",
  sqlserver_ex: "SQL Server Express", sqlserver_web: "SQL Server Web",
  x86_64: "x86_64", arm64: "ARM64", dedicated: "专用实例", host: "专用宿主机",
  gp2: "通用型 SSD（gp2）", gp3: "通用型 SSD（gp3）",
  io1: "预置 IOPS SSD（io1）", io2: "预置 IOPS SSD（io2）",
  st1: "吞吐优化型 HDD（st1）", sc1: "Cold HDD（sc1）",
  intelligent_tiering: "S3 Intelligent-Tiering", standard_ia: "S3 Standard-IA",
  one_zone_ia: "S3 One Zone-IA", glacier_instant_retrieval: "S3 Glacier Instant Retrieval",
  glacier_flexible_retrieval: "S3 Glacier Flexible Retrieval", deep_archive: "S3 Glacier Deep Archive",
  general_purpose: "通用型", compute_optimized: "计算优化型", memory_optimized: "内存优化型",
  storage_optimized: "存储优化型", accelerated_computing: "加速计算型",
  disabled: "关闭", enabled: "开启", single_instance: "单实例", multi_instance: "多实例",
  websocket: "WebSocket API",
  pay_as_you_go: "按量付费", reservation: "预留", savings_plan: "Savings Plan", spot: "Spot",
};

const FIELD_OPTION_LABELS: Record<string, Record<string, string>> = {
  support_tier: { standard: "标准支持", extended: "延长支持" },
};

const HIDDEN_CONFIGURATION_FIELDS = new Set([
  "requested_model", "system_default_assumption", "reference_unit_only",
  "reference_lcu_unit_only", "_review_selected_model", "_quote_skip_reason",
  "data_transfer_monitoring", "purchase_option", "reserved_term_years",
  "payment_option", "utilization_percent",
]);

const EKS_WORKER_CONFIGURATION_FIELDS = new Set([
  "cluster_count", "nodes", "vcpu", "memory_gib", "system_disk_gib",
  "total_system_disk_gib", "worker_nodes_per_cluster", "worker_node_count",
  "worker_requested_model", "worker_vcpu", "worker_memory_gib",
  "worker_system_disk_gib", "total_worker_system_disk_gib",
]);

// Only these AWS products expose customer-selectable instance or node sizes.
// Other current and future managed services must never inherit generic server
// CPU, memory or disk controls merely because a catalog result contains them.
const AWS_INSTANCE_SIZED_SERVICES = new Set([
  "ec2", "rds", "aurora", "elasticache", "msk", "opensearch",
  "documentdb", "dms", "emr", "redshift", "sagemaker", "mq",
]);

const GENERIC_INSTANCE_CONFIGURATION_FIELDS = new Set([
  "vcpu", "memory_gib", "system_disk_gib", "total_system_disk_gib", "nodes",
]);

const NUMERIC_CONFIGURATION_FIELDS = new Set([
  "vcpu", "memory_gib", "system_disk_gib", "total_system_disk_gib",
  "total_worker_system_disk_gib", "storage_gib", "total_storage_gib", "storage_gib_per_node",
  "storage_gib_per_broker", "broker_count", "data_nodes", "master_nodes", "shards",
  "master_vcpu", "master_memory_gib", "master_storage_gib_per_node",
  "core_nodes", "core_vcpu", "core_memory_gib", "core_storage_gib_per_node",
  "task_nodes", "task_vcpu", "task_memory_gib", "task_storage_gib_per_node",
  "replicas_per_shard", "node_count", "web_acls", "rules", "utilization_percent",
  "cluster_count", "instance_count", "replication_instances", "secret_count", "key_count",
  "vpc_count", "public_subnets", "private_subnets", "availability_zones", "gateway_count",
  "data_transfer_out_gib", "data_transfer_in_gib", "data_processed_gib", "requests", "https_requests", "listeners",
  "storage_iops", "storage_throughput_mbps", "backup_retention_days", "read_replica_count",
  "nodes", "managed_storage_gib", "snapshot_storage_gib", "rpu", "hours_per_month",
  "data_scanned_gib", "queries", "provisioned_dpu_hours",
  "data_transfer_regional_gib", "snapshot_changed_gib", "repositories", "image_scans",
  "put_copy_post_list_requests", "get_select_requests", "processed_bytes_gib",
  "hosted_zones", "health_checks", "outbound_messages", "inbound_messages", "attachments_gib",
  "log_storage_gib", "custom_metrics", "alarms", "active_series", "samples_ingested",
  "query_samples_processed", "collector_hours", "backup_storage_gib", "restore_gib",
  "accelerators", "broker_hours", "scheduled_invocations", "schedules", "io_requests",
  "api_calls", "dns_queries", "namespaces", "service_instances", "configuration_requests",
  "configuration_retrievals", "targets_receiving_configuration", "events", "event_buses",
  "schema_discovery_events", "pipes_requests", "state_transitions", "duration_gb_seconds",
  "input_tokens", "output_tokens", "images", "data_in_gib", "data_out_gib",
  "put_payload_units",
  "read_request_units", "write_request_units", "control_plane_hours",
  "experiment_hours", "memory_mb", "duration_ms", "flow_runs", "bucket_count", "object_count",
  "messages", "connection_minutes", "throughput_mbps_per_tib",
]);

function formatConfigurationValue(key: string, value: unknown): string {
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  if (typeof value === "string") return VALUE_LABELS[value.toLowerCase()] ?? value;
  if (Array.isArray(value)) return value.map((entry) => formatConfigurationValue(key, entry)).join("、");
  if (value && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .map(([childKey, childValue]) => `${FIELD_LABELS[childKey] ?? childKey}：${formatConfigurationValue(childKey, childValue)}`)
      .join("，");
  }
  if (
    typeof value === "number"
    && (key.endsWith("_gib") || key.includes("_gib_"))
    && value >= 1024
    && Number.isInteger(value / 1024)
  ) return `${value / 1024} TB`;
  const suffix = key.endsWith("_gib") || key.endsWith("_gib_per_node")
    ? " GiB"
    : key === "memory_mb"
      ? " MB"
      : key === "duration_ms"
        ? " ms"
    : key.endsWith("_percent")
      ? "%"
      : key.endsWith("_hours") || key === "hours_per_month"
        ? " 小时"
        : "";
  return `${String(value)}${suffix}`;
}

function configurationText(
  requirements: Record<string, unknown>,
  service?: string,
  officialSpecifications: Record<string, unknown> = {},
): string {
  const displayedRequirements = { ...requirements };
  if (typeof officialSpecifications.vCPU === "number") displayedRequirements.vcpu = officialSpecifications.vCPU;
  if (typeof officialSpecifications.memoryGiB === "number") displayedRequirements.memory_gib = officialSpecifications.memoryGiB;
  const redisShards = service === "elasticache" && typeof requirements.shards === "number"
    ? requirements.shards
    : null;
  const redisReplicas = service === "elasticache" && typeof requirements.replicas_per_shard === "number"
    ? requirements.replicas_per_shard
    : null;
  const entries = Object.entries(displayedRequirements).filter(
    ([key, value]) => !HIDDEN_CONFIGURATION_FIELDS.has(key)
      && !(service === "elasticache" && ["shards", "replicas_per_shard"].includes(key))
      && value !== null
      && value !== ""
      && (!NUMERIC_CONFIGURATION_FIELDS.has(key) || typeof value === "number"),
  );
  const descriptions = entries.map(
    ([key, value]) => `${FIELD_LABELS[key] ?? editableFieldLabel(key)}：${formatConfigurationValue(key, value)}`,
  );
  if (redisShards !== null && redisReplicas !== null) {
    descriptions.push(`主节点：${redisShards}`);
    descriptions.push(`Replica 节点：${redisShards * redisReplicas}`);
    descriptions.push(`节点总数：${redisShards * (1 + redisReplicas)}`);
  }
  if (requirements.reference_unit_only === true) descriptions.push("仅展示官方参考单价，不计入月费合计");
  if (requirements.reference_lcu_unit_only === true) descriptions.push("仅展示负载均衡官方参考单价，不计入月费合计");
  return descriptions.length ? descriptions.join(" · ") : "按最小单位单价计算";
}

function displayServiceName(item: ConfigurationItem): string {
  const names: Record<string, string> = {
    ec2: "Amazon EC2 云服务器", rds: "Amazon RDS 数据库", elasticache: "Amazon ElastiCache Redis",
    msk: "Amazon MSK", opensearch: "Amazon OpenSearch Service", eks: "Amazon EKS",
    elb: "Elastic Load Balancing", cloudfront: "Amazon CloudFront", s3: "Amazon S3",
    azure_vm: "Azure 虚拟机", managed_disks: "Azure 托管磁盘",
    azure_sql: "Azure SQL Database", azure_postgresql: "Azure Database for PostgreSQL",
    azure_mysql: "Azure Database for MySQL", azure_cache: "Azure Cache for Redis",
    blob_storage: "Azure Blob Storage", load_balancer: "Azure Load Balancer",
    application_gateway: "Azure Application Gateway", front_door: "Azure Front Door",
    bandwidth: "Azure 公网流量", aks: "Azure Kubernetes Service",
    monitor: "Azure Monitor", api_management: "Azure API Management",
  };
  if (/\baurora\b/i.test(item.display_name)) return item.display_name;
  if (
    ["elb", "alb", "nlb", "gwlb"].includes(item.service)
    && /load\s*balancer|负载均衡/i.test(item.display_name)
  ) return item.display_name;
  if (
    item.service === "ec2"
    && /(?:自建|用于|工作节点|worker\s*nodes?)/i.test(item.display_name)
  ) return item.display_name;
  return names[item.service] ?? item.display_name;
}

function displayRegion(item: ConfigurationItem): string {
  if (["cloudfront", "route53", "global_accelerator", "front_door"].includes(item.service)) return "全球";
  return item.region ? REGION_LABELS[item.region] ?? item.region : "使用本次报价区域";
}

function isGlobalService(item: ConfigurationItem): boolean {
  return ["cloudfront", "route53", "global_accelerator", "front_door"].includes(item.service)
    || ["global", "全球"].includes(String(item.region ?? "").toLowerCase());
}

function customerQuestionContext(
  question: Item,
  configurations: ConfigurationItem[],
): { title: string; source: string } | null {
  const component = configurationForConfirmation(question, configurations);
  if (!component) return null;
  const source = component.source_text?.trim();
  if (!source) {
    return {
      title: `对应组件：${displayServiceName(component)}`,
      source: "该组件没有单独保存客户原话。",
    };
  }
  const relationOnly = /^(?:用于|基于|依赖|关联|连接|挂载|保护|提供给|承载)/.test(source);
  if (component.service === "ec2" && relationOnly) {
    const componentIndex = configurations.findIndex(
      (candidate) => candidate.component_id === component.component_id,
    );
    const containedParent = configurations.find((candidate) => (
      candidate.component_id !== component.component_id
      && Boolean(candidate.source_text?.trim())
      && candidate.source_text.includes(source)
      && (candidate.service !== "ec2" || candidate.source_text.trim().length > source.length)
    ));
    const previous = componentIndex > 0 ? configurations[componentIndex - 1] : null;
    const parent = containedParent
      || (previous && ["vpc", "eks"].includes(previous.service) ? previous : null);
    if (parent) {
      const parentName = parent.source_text.split(/[：:]/, 1)[0]?.trim()
        || displayServiceName(parent);
      return { title: `由“${parentName}”需求衍生`, source: `创建原因：${source}` };
    }
    return { title: "系统衍生的 EC2 计算资源", source: `创建原因：${source}` };
  }
  return {
    title: `对应组件：${displayServiceName(component)}`,
    source: `客户原话：${source}`,
  };
}

function estimatedReviewDuration(componentCount: number): string {
  if (componentCount <= 10) return "1–3 分钟";
  if (componentCount <= 25) return "2–5 分钟";
  if (componentCount <= 50) return "4–8 分钟";
  return "6–12 分钟";
}

function displayPlan(item: ConfigurationItem): string {
  if (item.service === "eks") return "AWS 托管控制面";
  const requested = typeof item.requirements.requested_model === "string"
    && /^[a-z][a-z0-9-]*(?:\.[a-z0-9-]+)+$/i.test(item.requirements.requested_model)
    ? item.requirements.requested_model
    : null;
  const raw = item.selected_model ?? requested ?? "按官方规格自动匹配";
  if (raw === "AWS 官方计费维度") {
    const serviceLabels: Record<string, string> = {
      athena: "按查询数据扫描量计费",
      glue: "按数据处理用量计费",
      emr: "Amazon EMR 托管集群",
      redshift: "Amazon Redshift 数据仓库",
    };
    return serviceLabels[item.service] ?? "按官方用量计费";
  }
  const labels: Record<string, string> = {
    "Application Load Balancer": "应用型负载均衡器",
    "Network Load Balancer": "网络型负载均衡器",
    "Gateway Load Balancer": "网关型负载均衡器",
    "CloudFront Pay-as-you-go": "按量付费",
    "S3 Standard": "标准存储",
  };
  return labels[raw] ?? raw;
}

function displayQuantity(item: ConfigurationItem): string {
  if (item.service === "eks") return `${item.quantity} 个集群`;
  if (item.service === "msk") {
    const brokerCount = Number(item.requirements.broker_count);
    const clusterCount = Number.isFinite(item.quantity) && item.quantity > 0 ? item.quantity : 1;
    return Number.isFinite(brokerCount) && brokerCount > 0
      ? `${clusterCount} 套集群 · ${brokerCount} 个 Broker 节点`
      : `${clusterCount} 套集群`;
  }
  return `数量 ${item.quantity}`;
}

function isTechnicalPricingIssue(item: ConfigurationItem): boolean {
  if (item.pricing_status === "ready") return false;
  if (item.pricing_issue_category) return item.pricing_issue_category === "retryable";
  // Compatibility with confirmation sessions created before issue categories
  // were persisted.  New sessions always use the structured category above.
  return /官方.*(?:接口|目录|规格).*(?:暂时|超时|未返回|不可用)|稍后重试/.test(
    item.pricing_notice ?? "",
  );
}

function isSystemPricingIssue(item: ConfigurationItem): boolean {
  if (item.pricing_status === "ready") return false;
  return isTechnicalPricingIssue(item) || [
    "compatibility",
    "catalog_mapping",
    "system_configuration",
    "unsupported",
  ].includes(item.pricing_issue_category ?? "");
}

function pricingNoticeClass(item: ConfigurationItem): string {
  if (isTechnicalPricingIssue(item)) return "technical-pricing-notice";
  if (item.pricing_issue_category === "compatibility") return "compatibility-pricing-notice";
  if (isSystemPricingIssue(item)) return "technical-pricing-notice";
  return "customer-pricing-notice";
}

function requiresCustomerConfiguration(item: ConfigurationItem): boolean {
  return item.pricing_status !== "ready"
    && !isSystemPricingIssue(item);
}

export default function CustomerConfirmationPage() {
  const token = useMemo(() => {
    if (typeof window === "undefined") return "";
    return window.location.pathname.split("/").filter(Boolean).at(-1) ?? "";
  }, []);
  const [session, setSession] = useState<Session | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [processorArchitecture, setProcessorArchitecture] = useState<ProcessorArchitecture>("arm64");
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [componentFeedback, setComponentFeedback] = useState<Record<string, string>>({});
  const [componentDrafts, setComponentDrafts] = useState<Record<string, ComponentDraft>>({});
  const [componentUpdates, setComponentUpdates] = useState<Record<string, ComponentUpdate>>({});
  const [additionalFields, setAdditionalFields] = useState<Record<string, string[]>>({});
  const [additionalFieldChoice, setAdditionalFieldChoice] = useState<Record<string, string>>({});
  const [fieldUnits, setFieldUnits] = useState<Record<string, "gib" | "tib">>({});
  const [liveFieldOptions, setLiveFieldOptions] = useState<Record<string, EditableValue[]>>({});
  const [liveAvailableShapes, setLiveAvailableShapes] = useState<Record<string, Array<{
    model?: string;
    vcpu: number;
    memory_gib: number;
  }>>>({});
  const [editingComponents, setEditingComponents] = useState<Record<string, boolean>>({});
  const [pendingEditorSwitch, setPendingEditorSwitch] = useState<{
    fromId: string;
    toId: string | null;
  } | null>(null);
  const [deletedComponents, setDeletedComponents] = useState<Record<string, boolean>>({});
  const [additionFeedback, setAdditionFeedback] = useState("");
  const [addingConfiguration, setAddingConfiguration] = useState(false);
  const [reviewSeconds, setReviewSeconds] = useState(0);
  const [approvalSubmitted, setApprovalSubmitted] = useState(false);
  const [refreshingComponentIds, setRefreshingComponentIds] = useState<string[]>([]);
  const [submittingComponentIds, setSubmittingComponentIds] = useState<string[]>([]);
  const [queuedComponentIds, setQueuedComponentIds] = useState<string[]>([]);
  const [submittedComponentSnapshots, setSubmittedComponentSnapshots] = useState<Record<string, string>>({});
  const [failedComponentIds, setFailedComponentIds] = useState<string[]>([]);
  const [componentEditorNotices, setComponentEditorNotices] = useState<Record<string, string>>({});
  const [transientNotice, setTransientNotice] = useState("");
  const [recentlyUpdatedComponentIds, setRecentlyUpdatedComponentIds] = useState<string[]>([]);
  const [pendingAddition, setPendingAddition] = useState<PendingAddition | null>(() => (
    readPendingAddition(token)
  ));
  const [addingConfigurationInProgress, setAddingConfigurationInProgress] = useState(() => {
    if (typeof window === "undefined" || !token) return false;
    return Boolean(window.sessionStorage.getItem(additionStorageKey(token)));
  });
  const [regionConfirmationInProgress, setRegionConfirmationInProgress] = useState(() => {
    if (typeof window === "undefined" || !token) return false;
    return window.sessionStorage.getItem(`astraquote:aws:region-confirmation:${token}`) === "active";
  });
  const regionalRegions = useMemo(() => Array.from(new Set(
    (session?.configuration_items ?? [])
      .filter((item) => !isGlobalService(item) && Boolean(item.region))
      .map((item) => String(item.region)),
  )), [session?.configuration_items]);
  const sharedRegion = regionalRegions.length === 1 ? regionalRegions[0] : null;
  const isAzureConfirmation = session?.cloud_provider === "azure";
  const hasPendingComponentFeedback = Object.values(componentFeedback).some(
    (value) => value.trim().length > 0,
  );
  const hasPendingStructuredUpdates = Object.keys(componentUpdates).length > 0;
  const hasPendingConfigurationChanges = hasPendingComponentFeedback
    || hasPendingStructuredUpdates
    || Object.values(deletedComponents).some(Boolean)
    || additionFeedback.trim().length > 0;
  const customerBlockingItems = (session?.configuration_items ?? []).filter(
    requiresCustomerConfiguration,
  );
  const isConfigurationRefreshActive = (
    refreshingComponentIds.length > 0 || addingConfigurationInProgress
  ) && ["reviewing", "submitted", "processing"].includes(session?.status ?? "");
  const hasStandaloneConfirmationQuestions = Boolean(
    session?.status === "pending"
    && session.confirmation_items.length > 0
  );
  const isSessionReviewing = ["reviewing", "submitted", "processing"].includes(session?.status ?? "");
  const customerAnswersReviewInProgress = isSessionReviewing
    && !regionConfirmationInProgress
    && !isConfigurationRefreshActive
    && Boolean(session?.confirmation_items.length);
  const showConfigurationReview = session?.status === "configuration_review"
    || ((isConfigurationRefreshActive || isSessionReviewing)
      && Boolean(session?.configuration_items.length))
    || (addingConfigurationInProgress
      && hasStandaloneConfirmationQuestions
      && Boolean(session?.configuration_items.length));

  const clearPendingAddition = useCallback(() => {
    setPendingAddition(null);
    setAddingConfigurationInProgress(false);
    if (typeof window !== "undefined") {
      window.sessionStorage.removeItem(additionStorageKey(token));
    }
  }, [token]);

  const finishPendingAddition = useCallback((payload: Session): boolean => {
    const pending = pendingAddition ?? readPendingAddition(token);
    if (!pending) {
      clearPendingAddition();
      return false;
    }
    const originalIds = new Set(pending.existingComponentIds);
    const addedItems = payload.configuration_items.filter(
      (item) => !originalIds.has(item.component_id),
    );
    if (addedItems.length === 0) {
      setAdditionFeedback(pending.sourceText);
      setAddingConfiguration(true);
      setError(payload.confirmation_text?.includes("没有完成")
        ? payload.confirmation_text
        : "这次没有生成新的配置项，原内容已保留，请补充服务名称、数量或主要规格后重试。");
      clearPendingAddition();
      return false;
    }
    const addedNumbers = addedItems.map(
      (item, index) => item.component_number ?? String(pending.expectedNumber + index),
    );
    setRecentlyUpdatedComponentIds(addedItems.map((item) => item.component_id));
    setTransientNotice(`已新增为第 ${addedNumbers.join("、")} 项，原有配置未改动。`);
    setAdditionFeedback("");
    setAddingConfiguration(false);
    clearPendingAddition();
    window.setTimeout(() => {
      document.getElementById(`configuration-${addedItems[0].component_id}`)?.scrollIntoView({
        behavior: "smooth",
        block: "center",
      });
    }, 80);
    return true;
  }, [clearPendingAddition, pendingAddition, token]);

  function discardComponentDraft(componentId: string) {
    setComponentDrafts((current) => {
      const next = { ...current };
      delete next[componentId];
      return next;
    });
    setComponentUpdates((current) => {
      const next = { ...current };
      delete next[componentId];
      return next;
    });
    setAdditionalFields((current) => {
      const next = { ...current };
      delete next[componentId];
      return next;
    });
    setAdditionalFieldChoice((current) => {
      const next = { ...current };
      delete next[componentId];
      return next;
    });
    setFieldUnits((current) => Object.fromEntries(
      Object.entries(current).filter(([key]) => !key.startsWith(`${componentId}:`)),
    ));
  }

  function openComponentEditor(item: ConfigurationItem) {
    const componentId = item.component_id;
    const isClosing = editingComponents[componentId] === true;
    if (isClosing) {
      if (componentUpdates[componentId]) {
        setPendingEditorSwitch({ fromId: componentId, toId: null });
        return;
      }
      setEditingComponents((current) => ({ ...current, [componentId]: false }));
      discardComponentDraft(componentId);
      setPendingEditorSwitch(null);
      return;
    }

    // AWS structured edits are local drafts. Moving to another component is
    // allowed only after the customer chooses whether to keep real changes.
    const openId = Object.entries(editingComponents).find(
      ([candidateId, isOpen]) => isOpen && candidateId !== componentId,
    )?.[0];
    if (openId && componentUpdates[openId]) {
      setPendingEditorSwitch({ fromId: openId, toId: componentId });
      return;
    }
    if (openId) discardComponentDraft(openId);
    setEditingComponents({ [componentId]: true });
    setComponentDrafts((current) => ({
      ...current,
      [componentId]: buildComponentDraft(item, isAzureConfirmation),
    }));
    void loadOfficialFieldOptions(item);
  }

  async function loadOfficialFieldOptions(
    item: ConfigurationItem,
    regionOverride?: string,
    requirementsOverride?: Record<string, EditableValue>,
  ) {
    const region = (regionOverride ?? item.region)?.trim();
    if (!region) return;
    if (regionOverride && regionOverride !== item.region) {
      setLiveFieldOptions((existing) => Object.fromEntries(
        Object.entries(existing).filter(([key]) => (
          !key.startsWith(`${item.component_id}:`)
          || key === `${item.component_id}:region`
        )),
      ));
      setLiveAvailableShapes((existing) => ({
        ...existing,
        [item.component_id]: [],
      }));
    }
    try {
      const provider = session?.cloud_provider === "azure" ? "azure" : "aws";
      const response = await fetch(`${API_BASE}/api/${provider}/configuration-field-options`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          service: item.service,
          region,
          requirements: requirementsOverride ?? item.requirements,
        }),
        cache: "no-store",
      });
      if (!response.ok) return;
      const rawPayload = await response.json() as {
        options?: Record<string, EditableValue[]>;
        shapes?: Array<{ model?: string; vcpu: number; memory_gib: number }>;
      } | Record<string, EditableValue[]>;
      const payload = "options" in rawPayload
        ? (rawPayload.options ?? {})
        : rawPayload as Record<string, EditableValue[]>;
      setLiveFieldOptions((existing) => ({
        ...Object.fromEntries(
          Object.entries(existing).filter(([key]) => !key.startsWith(`${item.component_id}:`)),
        ),
        ...Object.fromEntries(
          Object.entries(payload)
            .filter(([, values]) => Array.isArray(values) && values.length > 0)
            .map(([field, values]) => [`${item.component_id}:${field}`, values]),
        ),
      }));
      if ("shapes" in rawPayload && Array.isArray(rawPayload.shapes)) {
        setLiveAvailableShapes((existing) => ({
          ...existing,
          [item.component_id]: rawPayload.shapes ?? [],
        }));
      }
    } catch {
      // Existing provider-specific controls remain available while the local lookup retries.
    }
  }

  function completeEditorSwitch(saveCurrent: boolean) {
    if (!pendingEditorSwitch || !session) return;
    const { fromId, toId } = pendingEditorSwitch;
    if (toId === null) {
      if (saveCurrent) void submitConfigurationFeedback(fromId);
      else discardComponentDraft(fromId);
      setEditingComponents((current) => ({ ...current, [fromId]: false }));
      setPendingEditorSwitch(null);
      return;
    }
    const targetItem = session.configuration_items.find(
      (item) => item.component_id === toId,
    );
    if (!targetItem) {
      setPendingEditorSwitch(null);
      return;
    }
    if (saveCurrent) void submitConfigurationFeedback(fromId);
    else discardComponentDraft(fromId);
    setEditingComponents({ [toId]: true });
    setComponentDrafts((current) => ({
      ...current,
      [toId]: buildComponentDraft(targetItem, isAzureConfirmation),
    }));
    setPendingEditorSwitch(null);
  }

  function updateComponentField(
    item: ConfigurationItem,
    scope: "region" | "quantity" | "requirements",
    field: string,
    value: EditableValue,
  ) {
    const componentId = item.component_id;
    setComponentEditorNotices((current) => {
      if (!current[componentId]) return current;
      const next = { ...current };
      delete next[componentId];
      return next;
    });
    setComponentDrafts((current) => {
      const draft = current[componentId] ?? buildComponentDraft(item, isAzureConfirmation);
      if (scope === "requirements") {
        return {
          ...current,
          [componentId]: {
            ...draft,
            requirements: { ...draft.requirements, [field]: value },
          },
        };
      }
      return {
        ...current,
        [componentId]: {
          ...draft,
          [scope]: scope === "quantity" ? Number(value) : String(value ?? ""),
        },
      };
    });
    setComponentUpdates((current) => {
      const update = current[componentId] ?? {};
      if (scope === "requirements") {
        return {
          ...current,
          [componentId]: {
            ...update,
            requirements: { ...(update.requirements ?? {}), [field]: value },
          },
        };
      }
      return {
        ...current,
        [componentId]: {
          ...update,
          [scope]: scope === "quantity" ? Number(value) : String(value ?? ""),
        },
      };
    });
  }

  function updateTransientNumericField(
    item: ConfigurationItem,
    scope: "quantity" | "requirements",
    field: string,
    rawValue: string,
    multiplier = 1,
  ) {
    if (rawValue !== "") {
      updateComponentField(item, scope, field, Number(rawValue) * multiplier);
      return;
    }
    const componentId = item.component_id;
    setComponentEditorNotices((current) => {
      if (!current[componentId]) return current;
      const next = { ...current };
      delete next[componentId];
      return next;
    });
    // Empty is a temporary editing state, not a request to delete the field.
    // Keep the input mounted so the customer can type the replacement value,
    // and remove only the unfinished value from the pending server payload.
    setComponentDrafts((current) => {
      const draft = current[componentId] ?? buildComponentDraft(item, isAzureConfirmation);
      if (scope === "quantity") {
        return { ...current, [componentId]: { ...draft, quantity: "" } };
      }
      return {
        ...current,
        [componentId]: {
          ...draft,
          requirements: { ...draft.requirements, [field]: "" },
        },
      };
    });
    setComponentUpdates((current) => {
      const next = { ...current };
      const update = next[componentId];
      if (!update) return current;
      const revised: ComponentUpdate = { ...update };
      if (scope === "quantity") {
        delete revised.quantity;
      } else if (revised.requirements) {
        const requirements = { ...revised.requirements };
        delete requirements[field];
        if (Object.keys(requirements).length > 0) revised.requirements = requirements;
        else delete revised.requirements;
      }
      if (Object.keys(revised).length > 0) next[componentId] = revised;
      else delete next[componentId];
      return next;
    });
  }

  useEffect(() => {
    if (!token) return;
    fetch(`${API_BASE}/api/confirmation-sessions/${token}`, { cache: "no-store" })
      .then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.message ?? "确认单不存在或已失效");
        if (payload.cloud_provider !== EXPECTED_CLOUD_PROVIDER) {
          throw new Error("该链接不属于 AWS 报价系统，系统已阻止跨云读取。");
        }
        const expectedPrefix = payload.cloud_provider === "azure" ? "azure_" : "aws_";
        if (!token.startsWith(expectedPrefix)) {
          throw new Error("确认链接的云厂商标识不一致，系统已阻止打开。");
        }
        return payload as Session;
      })
      .then((payload) => {
        setSession(payload);
        setAnswers(payload.answers ?? {});
        const persistedArchitecture = payload.answers?.[PROCESSOR_ARCHITECTURE_ANSWER_KEY];
        if (persistedArchitecture === "arm64" || persistedArchitecture === "x86_64") {
          setProcessorArchitecture(persistedArchitecture);
        }
        if (
          ["reviewing", "submitted", "processing"].includes(payload.status)
          && payload.confirmation_items.length > 0
          && payload.confirmation_items.every(isRegionConfirmation)
        ) {
          setRegionConfirmationInProgress(true);
          window.sessionStorage.setItem(`astraquote:aws:region-confirmation:${token}`, "active");
        }
        if (!["reviewing", "submitted", "processing"].includes(payload.status)) {
          setRegionConfirmationInProgress(false);
          window.sessionStorage.removeItem(`astraquote:aws:region-confirmation:${token}`);
        }
        if (["configuration_review", "approved", "completed"].includes(payload.status)) {
          if (addingConfigurationInProgress) finishPendingAddition(payload);
          else clearPendingAddition();
          setRegionConfirmationInProgress(false);
          window.sessionStorage.removeItem(`astraquote:aws:region-confirmation:${token}`);
        }
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "无法读取确认单"))
      .finally(() => setLoading(false));
  }, [token, addingConfigurationInProgress, clearPendingAddition, finishPendingAddition]);

  useEffect(() => {
    if (!token || session?.status !== "configuration_review") return;
    const validate = () => {
      fetch(`${API_BASE}/api/confirmation-sessions/${token}`, { cache: "no-store" })
        .then(async (response) => {
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.message ?? "确认单不存在或已失效");
          if (payload.cloud_provider !== EXPECTED_CLOUD_PROVIDER) {
            throw new Error("该链接不属于 AWS 报价系统，系统已阻止跨云读取。");
          }
          const expectedPrefix = payload.cloud_provider === "azure" ? "azure_" : "aws_";
          if (!token.startsWith(expectedPrefix)) {
            throw new Error("确认链接的云厂商标识不一致，系统已阻止继续操作。");
          }
          return payload as Session;
        })
        .then((payload) => setSession(payload))
        .catch((reason) => {
          setSession(null);
          setError(
            reason instanceof Error
              ? reason.message
              : "旧版确认链接已经隔离，请让销售重新生成。",
          );
        });
    };
    const timer = window.setInterval(validate, 3000);
    return () => window.clearInterval(timer);
  }, [token, session?.status]);

  useEffect(() => {
    if (
      !token
      || !["reviewing", "submitted", "processing", "approved"].includes(session?.status ?? "")
    ) return;
    const refresh = () => {
      fetch(`${API_BASE}/api/confirmation-sessions/${token}`, { cache: "no-store" })
        .then((response) => response.json())
        .then((payload: Session) => {
          const persistedArchitecture = payload.answers?.[PROCESSOR_ARCHITECTURE_ANSWER_KEY];
          if (persistedArchitecture === "arm64" || persistedArchitecture === "x86_64") {
            setProcessorArchitecture(persistedArchitecture);
          }
          setSession((current) => ({
            ...payload,
            configuration_items: payload.configuration_items?.length
              ? payload.configuration_items
              : current?.configuration_items ?? [],
          }));
          if (payload.status === "configuration_review" && (
            refreshingComponentIds.length > 0 || addingConfigurationInProgress
          )) {
            const failedIds = refreshingComponentIds.filter((componentId) => {
              const updated = payload.configuration_items?.find(
                (item) => item.component_id === componentId,
              );
              if (!updated) return !deletedComponents[componentId];
              return Boolean(
                updated.pricing_status !== "ready"
                || (
                  submittedComponentSnapshots[componentId]
                  && JSON.stringify(updated) === submittedComponentSnapshots[componentId]
                ),
              );
            });
            const updatedIds = refreshingComponentIds.filter((id) => !failedIds.includes(id));
            setRecentlyUpdatedComponentIds(updatedIds);
            if (updatedIds.length > 0) {
              setComponentFeedback((current) => {
                const next = { ...current };
                updatedIds.forEach((id) => delete next[id]);
                return next;
              });
              setComponentUpdates((current) => {
                const next = { ...current };
                updatedIds.forEach((id) => delete next[id]);
                return next;
              });
              setComponentDrafts((current) => {
                const next = { ...current };
                updatedIds.forEach((id) => delete next[id]);
                return next;
              });
              setAdditionalFields((current) => {
                const next = { ...current };
                updatedIds.forEach((id) => delete next[id]);
                return next;
              });
              setEditingComponents((current) => {
                const next = { ...current };
                updatedIds.forEach((id) => { next[id] = false; });
                return next;
              });
              setDeletedComponents((current) => {
                const next = { ...current };
                updatedIds.forEach((id) => delete next[id]);
                return next;
              });
            }
            if (failedIds.length > 0) {
              setEditingComponents((current) => {
                const next = { ...current };
                failedIds.forEach((id) => { next[id] = true; });
                return next;
              });
              const invalidItem = payload.configuration_items?.find(
                (item) => failedIds.includes(item.component_id) && item.pricing_notice,
              );
              if (invalidItem?.pricing_notice) {
                setError("");
                setTransientNotice(invalidItem.pricing_notice);
              } else if (payload.confirmation_text?.includes("已保留原配置")) {
                setError("");
                setTransientNotice(payload.confirmation_text);
              } else {
                setError(isAzureConfirmation
                  ? "AI 没有完成这次修改，原内容已保留，请点击“重新尝试”。"
                  : "新配置未通过官方规格校验，原内容已保留，请调整后重试。");
              }
            }
            setFailedComponentIds(failedIds);
            setSubmittedComponentSnapshots((current) => {
              const next = { ...current };
              refreshingComponentIds.forEach((id) => delete next[id]);
              return next;
            });
            setRefreshingComponentIds([]);
            if (addingConfigurationInProgress) finishPendingAddition(payload);
            else clearPendingAddition();
          }
          if (payload.status === "pending") setAnswers({});
          if (!["reviewing", "submitted", "processing"].includes(payload.status)) {
            setRegionConfirmationInProgress(false);
            window.sessionStorage.removeItem(`astraquote:aws:region-confirmation:${token}`);
          }
        })
        .catch(() => undefined);
    };
    const timer = window.setInterval(refresh, 1800);
    return () => window.clearInterval(timer);
  }, [token, session?.status, refreshingComponentIds, addingConfigurationInProgress, submittedComponentSnapshots, deletedComponents, isAzureConfirmation, clearPendingAddition, finishPendingAddition]);

  useEffect(() => {
    if (recentlyUpdatedComponentIds.length === 0) return;
    const timer = window.setTimeout(() => setRecentlyUpdatedComponentIds([]), 1800);
    return () => window.clearTimeout(timer);
  }, [recentlyUpdatedComponentIds]);

  useEffect(() => {
    if (!transientNotice) return;
    const timer = window.setTimeout(() => setTransientNotice(""), 4200);
    return () => window.clearTimeout(timer);
  }, [transientNotice]);

  useEffect(() => {
    if (!isSessionReviewing) return;
    const timer = window.setInterval(() => setReviewSeconds((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [isSessionReviewing]);

  async function submit() {
    if (!session || session.confirmation_items.some(
      (item) => !confirmationComplete(item, answers[confirmationAnswerKey(item)]),
    )) return;
    const pendingSession = session;
    const isRegionOnlyRound = session.confirmation_items.length > 0
      && session.confirmation_items.every(isRegionConfirmation);
    setReviewSeconds(0);
    setSubmitting(true);
    setError("");
    if (isRegionOnlyRound) {
      setRegionConfirmationInProgress(true);
      window.sessionStorage.setItem(`astraquote:aws:region-confirmation:${token}`, "active");
    }
    // Close the question dialog immediately after a valid submission.  The
    // retained configuration table becomes the progress surface while the
    // server continues processing; if the request fails, restore the dialog
    // with every answer still present so the customer can retry.
    setSession((current) => current ? { ...current, status: "submitted" } : current);
    try {
      const response = await fetch(`${API_BASE}/api/confirmation-sessions/${token}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          answers,
          processor_architecture: processorArchitecture,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message ?? "提交失败");
      setSession((current) => ({
        ...(payload as Session),
        configuration_items: (payload as Session).configuration_items?.length
          ? (payload as Session).configuration_items
          : current?.configuration_items ?? [],
      }));
      if (!["reviewing", "submitted", "processing"].includes((payload as Session).status)) {
        setRegionConfirmationInProgress(false);
        window.sessionStorage.removeItem(`astraquote:aws:region-confirmation:${token}`);
      }
      if (["configuration_review", "approved", "completed"].includes((payload as Session).status)) {
        if (addingConfigurationInProgress) finishPendingAddition(payload as Session);
        else clearPendingAddition();
      }
    } catch (reason) {
      setSession(pendingSession);
      if (isRegionOnlyRound) {
        setRegionConfirmationInProgress(false);
        window.sessionStorage.removeItem(`astraquote:aws:region-confirmation:${token}`);
      }
      setError(reason instanceof Error ? reason.message : "提交失败，请重试");
    } finally {
      setSubmitting(false);
    }
  }

  async function approveConfiguration() {
    if (!session) return;
    setSubmitting(true);
    setError("");
    try {
      const response = await fetch(`${API_BASE}/api/confirmation-sessions/${token}/approve`, {
        method: "POST",
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message ?? "确认失败");
      setApprovalSubmitted(true);
      setSession(payload as Session);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "确认失败，请重试");
    } finally {
      setSubmitting(false);
    }
  }

  async function submitConfigurationFeedback(
    componentId?: string,
    fromQueue = false,
    additionOnly = false,
  ) {
    if (!session) return;
    if (componentId && !deletedComponents[componentId]) {
      const item = session.configuration_items.find(
        (candidate) => candidate.component_id === componentId,
      );
      const draft = item ? componentDrafts[componentId] : undefined;
      if (item && draft) {
        if (draft.quantity === "" || !Number.isFinite(draft.quantity) || draft.quantity < 1) {
          setComponentEditorNotices((current) => ({
            ...current,
            [componentId]: "请填写 1 到 10000 之间的完整数量后再保存。",
          }));
          return;
        }
        const visibleFields = editableRequirementFields(
          item,
          draft,
          additionalFields[componentId] ?? [],
          isAzureConfirmation,
        );
        const unfinishedField = visibleFields.find(
          (field) => field in draft.requirements
            && NUMERIC_CONFIGURATION_FIELDS.has(field)
            && (draft.requirements[field] === "" || draft.requirements[field] === null),
        );
        if (unfinishedField) {
          setComponentEditorNotices((current) => ({
            ...current,
            [componentId]: `请先填写完整的${editableFieldLabel(unfinishedField)}，再保存本项。`,
          }));
          return;
        }
      }
    }
    const componentChanges: Record<string, string> = {};
    const candidateIds = additionOnly
      ? []
      : componentId
      ? [componentId]
      : Array.from(new Set([
        ...Object.keys(componentFeedback),
        ...Object.keys(componentUpdates),
        ...Object.keys(deletedComponents),
      ]));
    candidateIds.forEach((candidateId) => {
      const feedback = componentFeedback[candidateId]?.trim() ?? "";
      if (deletedComponents[candidateId]) componentChanges[candidateId] = DELETE_COMPONENT_MARKER;
      else if (feedback) componentChanges[candidateId] = feedback;
    });
    const structuredUpdates: Record<string, ComponentUpdate> = {};
    candidateIds.forEach((candidateId) => {
      if (!deletedComponents[candidateId] && componentUpdates[candidateId]) {
        structuredUpdates[candidateId] = componentUpdates[candidateId];
      }
    });
    const addedConfiguration = componentId ? "" : additionFeedback.trim();
    if (Object.keys(componentChanges).length === 0
      && Object.keys(structuredUpdates).length === 0
      && !addedConfiguration) {
      if (componentId) {
        setComponentEditorNotices((current) => ({
          ...current,
          [componentId]: "当前配置没有实际变化。GiB/TiB 只切换显示单位；请修改数值或其他字段后再保存。",
        }));
      }
      return;
    }
    // Only one revision can own the server-side confirmation draft at a time.
    // Keep later row submissions locally and send them as soon as the current
    // component returns to configuration review. This avoids a 409 response
    // and, more importantly, prevents the customer's second edit being lost.
    if (componentId && isConfigurationRefreshActive && !fromQueue) {
      setQueuedComponentIds((current) => Array.from(new Set([...current, componentId])));
      setEditingComponents((current) => ({ ...current, [componentId]: false }));
      return;
    }
    if (componentId) {
      setFailedComponentIds((current) => current.filter((id) => id !== componentId));
      const currentItem = session.configuration_items.find(
        (item) => item.component_id === componentId,
      );
      if (currentItem) {
        setSubmittedComponentSnapshots((current) => ({
          ...current,
          [componentId]: JSON.stringify(currentItem),
        }));
      }
    }
    const affectedComponentIds = Array.from(new Set([
      ...Object.keys(componentChanges),
      ...Object.keys(structuredUpdates),
    ]));
    setRefreshingComponentIds((current) => Array.from(new Set([...current, ...affectedComponentIds])));
    setAddingConfigurationInProgress(Boolean(addedConfiguration));
    if (addedConfiguration) {
      const pending: PendingAddition = {
        sourceText: addedConfiguration,
        existingComponentIds: session.configuration_items.map((item) => item.component_id),
        expectedNumber: session.configuration_items.length + 1,
      };
      setPendingAddition(pending);
      window.sessionStorage.setItem(additionStorageKey(token), JSON.stringify(pending));
    }
    setReviewSeconds(0);
    if (componentId) {
      setSubmittingComponentIds((current) => Array.from(new Set([...current, componentId])));
    } else {
      setSubmitting(true);
    }
    setError("");
    try {
      const response = await fetch(`${API_BASE}/api/confirmation-sessions/${token}/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          feedback: addedConfiguration ? `请新增以下配置：\n${addedConfiguration}` : null,
          component_feedback: componentChanges,
          component_updates: structuredUpdates,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message ?? "提交修改失败");
      setSession((current) => ({
        ...(payload as Session),
        configuration_items: current?.configuration_items?.length
          ? current.configuration_items
          : (payload as Session).configuration_items,
      }));
      if (addedConfiguration
        && ["configuration_review", "approved", "completed"].includes((payload as Session).status)) {
        finishPendingAddition(payload as Session);
      }
      if (additionOnly) {
        // The inline add form owns only the new row. Do not discard another
        // component's unsaved local editor when the customer submits it.
        setAdditionFeedback("");
        setAddingConfiguration(false);
      } else if (!componentId) {
        setComponentFeedback({});
        setComponentUpdates({});
        setComponentDrafts({});
        setAdditionalFields({});
        setAdditionalFieldChoice({});
        setEditingComponents({});
        setDeletedComponents({});
        setAdditionFeedback("");
        setAddingConfiguration(false);
      }
    } catch (reason) {
      setRefreshingComponentIds((current) => current.filter((id) => !affectedComponentIds.includes(id)));
      if (addedConfiguration) {
        clearPendingAddition();
      }
      setError(reason instanceof Error ? reason.message : "提交修改失败，请重试");
    } finally {
      if (componentId) {
        setSubmittingComponentIds((current) => current.filter((id) => id !== componentId));
      } else {
        setSubmitting(false);
      }
    }
  }

  useEffect(() => {
    if (
      session?.status !== "configuration_review"
      || queuedComponentIds.length === 0
      || submittingComponentIds.length > 0
    ) return;
    const nextComponentId = queuedComponentIds[0];
    const timer = window.setTimeout(() => {
      setQueuedComponentIds((current) => current.filter((id) => id !== nextComponentId));
      void submitConfigurationFeedback(nextComponentId, true);
    }, 0);
    return () => window.clearTimeout(timer);
    // submitConfigurationFeedback intentionally reads the latest form state
    // when the queued turn starts; recreating this effect for its function
    // identity would submit the same queued edit more than once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session?.status, queuedComponentIds, submittingComponentIds]);

  function renderConfirmationItems(className = "") {
    if (!session) return null;
    const hasModelChoices = session.confirmation_items.some(itemHasModelChoices);
    const changeProcessorArchitecture = (next: ProcessorArchitecture) => {
      if (next === processorArchitecture) return;
      setProcessorArchitecture(next);
      setAnswers((current) => {
        const revised = { ...current };
        session.confirmation_items.filter(itemHasModelChoices).forEach((item) => {
          const answerKey = confirmationAnswerKey(item);
          const currentAnswer = revised[answerKey] ?? "";
          const baseValue = currentAnswer.split("；", 1)[0];
          if ((item.dependent_on_values ?? []).includes(baseValue)) {
            revised[answerKey] = baseValue;
          } else if (item.options.some((choice) => Boolean(choice.model))) {
            delete revised[answerKey];
          }
        });
        return revised;
      });
    };
    return (
      <div className={`customer-questions ${className}`.trim()}>
        {hasModelChoices && <div className="processor-architecture-choice" role="radiogroup" aria-label="选择整份报价的处理器架构">
          <div>
            <strong>处理器架构</strong>
            <span>默认使用价格通常更低的ARM；改选x86后，下面所有组件只显示x86型号。</span>
          </div>
          <div>
            <button type="button" role="radio" aria-checked={processorArchitecture === "arm64"} className={processorArchitecture === "arm64" ? "selected" : ""} onClick={() => changeProcessorArchitecture("arm64")}>
              ARM（默认）<small>价格通常更低，使用前确认软件兼容</small>
            </button>
            <button type="button" role="radio" aria-checked={processorArchitecture === "x86_64"} className={processorArchitecture === "x86_64" ? "selected" : ""} onClick={() => changeProcessorArchitecture("x86_64")}>
              x86<small>兼容范围更广</small>
            </button>
          </div>
        </div>}
        {session.confirmation_items.map((item, index) => {
          const answerKey = confirmationAnswerKey(item);
          const answer = answers[answerKey] ?? "";
          const context = customerQuestionContext(item, session.configuration_items);
          const relatedConfiguration = configurationForConfirmation(
            item,
            session.configuration_items,
          );
          const baseValue = answer.split("；", 1)[0];
          const automaticDependentChoice = preferredDependentChoice(
            item,
            relatedConfiguration,
            processorArchitecture,
          );
          const showDependentConfiguration = (
            item.dependent_on_values ?? []
          ).includes(baseValue)
            && (item.dependent_options?.length ?? 0) > 0
            && !automaticDependentChoice;
          return <article key={answerKey}>
            <label><b>{index + 1}</b><span>{item.question}</span></label>
            {context && <div className="customer-question-context">
              <strong>{context.title}</strong>
              <span>{context.source}</span>
            </div>}
            {item.options.length > 0 && <ConfigurationOptionPicker
              key={`${answerKey}-${processorArchitecture}-primary`}
              className="customer-options"
              options={item.options}
              value={answers[answerKey]}
              placeholder={isRegionConfirmation(item) ? "请选择 Azure 部署区域" : "请选择配置选项"}
              catalog={item.selection_mode === "catalog" || item.options.some((option) => Boolean(option.model))}
              requireMachineCount={/自建/.test(item.question) && item.options.some((option) => Boolean(option.model))}
              architecturePreference={item.options.some((option) => Boolean(option.model))
                ? processorArchitecture
                : undefined}
              initialMachineCount={relatedConfiguration?.quantity
                ?? Number(item.question.match(/当前\s*(\d+)\s*台/)?.[1] ?? 1)}
              initialVcpu={typeof relatedConfiguration?.requirements.vcpu === "number"
                ? relatedConfiguration.requirements.vcpu
                : undefined}
              initialMemoryGiB={typeof relatedConfiguration?.requirements.memory_gib === "number"
                ? relatedConfiguration.requirements.memory_gib
                : undefined}
              onChange={(selected) => setAnswers((current) => ({
                ...current,
                [answerKey]: dependentSelectionValue(
                  item,
                  selected,
                  relatedConfiguration,
                  processorArchitecture,
                ),
              }))}
            />}
            {showDependentConfiguration && <ConfigurationOptionPicker
              key={`${answerKey}-${processorArchitecture}-dependent`}
              className="customer-options dependent-configuration-picker"
              options={item.dependent_options ?? []}
              value={answer.includes("；") ? answer.slice(answer.indexOf("；") + 1) : ""}
              catalog
              requireMachineCount
              architecturePreference={processorArchitecture}
              initialMachineCount={relatedConfiguration?.quantity ?? 1}
              initialVcpu={typeof relatedConfiguration?.requirements.vcpu === "number"
                ? relatedConfiguration.requirements.vcpu
                : undefined}
              initialMemoryGiB={typeof relatedConfiguration?.requirements.memory_gib === "number"
                ? relatedConfiguration.requirements.memory_gib
                : undefined}
              onChange={(selected) => setAnswers((current) => ({
                ...current,
                [answerKey]: selected ? `${baseValue}；${selected}` : baseValue,
              }))}
            />}
            {item.selection_mode === "text" && item.options.length === 0 && <input
              value={answers[answerKey] ?? ""}
              onChange={(event) => setAnswers((current) => ({
                ...current,
                [answerKey]: event.target.value,
              }))}
              placeholder="填写您的选择"
            />}
            {item.selection_mode !== "text" && item.options.length === 0 && <div
              className="configuration-picker-empty"
              role="alert"
            >官方可选项尚未加载完成，系统已阻止手动填写，请刷新后重试。</div>}
          </article>;
        })}
      </div>
    );
  }

  return (
    <main className={`customer-confirm-page ${showConfigurationReview ? "configuration-review-page" : ""} ${hasStandaloneConfirmationQuestions ? "question-entry-page" : ""}`.trim()}>
      <header><span>A</span><strong>AstraQuote</strong></header>
      <section>
        {loading ? <div className="customer-confirm-state">正在读取确认单…</div> : error && !session ? (
          <div className="customer-confirm-state error">{error}</div>
        ) : approvalSubmitted || session?.status === "completed" || session?.status === "approved" ? (
          <div className="customer-completion-state" role="status" aria-labelledby="approval-title">
            <i>✓</i>
            <small>提交成功</small>
            <h1 id="approval-title">配置已确认</h1>
            <p>销售人员稍后会向您发送正式报价单，感谢您的配合。</p>
            <span>本页面无需继续操作，您可以直接关闭。</span>
          </div>
        ) : regionConfirmationInProgress && isSessionReviewing && session ? (
          <div className="customer-region-processing-state" role="status" aria-live="polite">
            <i aria-hidden="true" />
            <small>正在运行</small>
            <h1>正在按所选地区核验配置</h1>
            <p>系统正在查询该地区支持的官方产品、型号和价格，请稍候。</p>
            <div className="customer-review-progress" aria-hidden="true"><i /></div>
            <span>处理完成后会自动显示下一步，无需刷新页面</span>
          </div>
        ) : customerAnswersReviewInProgress && session ? (
          <div className="customer-answer-processing-state" role="status" aria-live="polite">
            <i aria-hidden="true" />
            <small>正在处理</small>
            <h1>正在处理客户回答</h1>
            <p className="customer-processing-elapsed">已用时 {String(Math.floor(reviewSeconds / 60)).padStart(2, "0")}:{String(reviewSeconds % 60).padStart(2, "0")}</p>
            <p className="customer-processing-estimate">预计需要 {estimatedReviewDuration(session.configuration_items.length)}，组件越多处理时间越长</p>
            <div className="customer-review-progress" aria-hidden="true"><i /></div>
            <span>完成后自动进入配置确认</span>
          </div>
        ) : showConfigurationReview && session ? (
          <>
            <div className="customer-confirm-title customer-review-heading">
              <h1>请核对配置信息</h1>
              <p>如有不符，请直接修改、添加或删除。</p>
            </div>
            {transientNotice && (
              <div className="customer-transient-toast" role="alert">
                {transientNotice}
              </div>
            )}
            {(isConfigurationRefreshActive || isSessionReviewing) && (
              <div className="configuration-refresh-status" role="status">
                <i />
                <span>{addingConfigurationInProgress && refreshingComponentIds.length === 0
                    ? "正在添加配置"
                    : refreshingComponentIds.length > 0
                      ? `正在更新 ${refreshingComponentIds.length} 项配置`
                      : "正在处理您刚才提交的选择"}</span>
                <small>未修改的配置保持不变</small>
                {addingConfigurationInProgress && (
                  <div className="customer-addition-progress" aria-label="新增配置处理进度">
                    <span className="done">1 识别需求</span>
                    <span className={isSessionReviewing ? "active" : "done"}>
                      2 补充与校验
                    </span>
                    <span>3 加入表格</span>
                  </div>
                )}
              </div>
            )}
            {isSessionReviewing && reviewSeconds >= 15 && (
              <div className="customer-ai-retry-notice" role="alert">
                <strong>{addingConfigurationInProgress
                  ? "正在检查新加的配置"
                  : isAzureConfirmation
                    ? "正在继续处理您的修改"
                    : "正在检查您刚修改的配置"}</strong>
                <span>{addingConfigurationInProgress
                  ? "这里只处理新加的内容，原来的配置不会重新运行。"
                  : "您填写的内容已经保存，不需要重复提交。"}</span>
              </div>
            )}
            <div className="customer-configuration-toolbar">
              <button
                type="button"
                onClick={() => setAddingConfiguration((current) => !current)}
              >{addingConfiguration ? "收起新增配置" : "＋ 新增配置"}</button>
            </div>
            {addingConfiguration && (
              <div className="customer-add-configuration">
                <label htmlFor="add-configuration"><strong>新增配置</strong><span>填写服务、区域、数量和规格；未明确的参数可留空。</span></label>
                <div className="customer-add-configuration-input">
                  <textarea
                    id="add-configuration"
                    value={additionFeedback}
                    onChange={(event) => setAdditionFeedback(event.target.value)}
                    placeholder={isAzureConfirmation
                      ? "例如：新增 Azure 虚拟机，新加坡区域，2台，Standard_D4s_v5，Linux。"
                      : "例如：新增 Amazon EC2，新加坡区域，2台，4核16GB，Linux。"}
                    rows={2}
                  />
                  <button
                    type="button"
                    disabled={!additionFeedback.trim() || submitting || isConfigurationRefreshActive}
                    onClick={() => void submitConfigurationFeedback(undefined, false, true)}
                  >{submitting && additionFeedback.trim() ? "提交中…" : "提交新增"}</button>
                </div>
              </div>
            )}
            <div className="customer-configuration-table customer-review-table">
              <table>
                <colgroup><col className="review-index-column" /><col className="review-service-column" /><col className="review-detail-column" /><col className="review-action-column" /></colgroup>
                <thead><tr><th>序号</th><th>{isAzureConfirmation ? "Azure 服务" : "AWS 服务"}</th><th>配置详情</th><th>操作</th></tr></thead>
                <tbody>
                  {hierarchyOrderedConfigurationItems(session.configuration_items)
                    .map(({ item }, index) => {
                    const feedback = componentFeedback[item.component_id] ?? "";
                    const structuredUpdate = componentUpdates[item.component_id];
                    const draft = componentDrafts[item.component_id]
                      ?? buildComponentDraft(item, isAzureConfirmation);
                    const componentAdditionalFields = additionalFields[item.component_id] ?? [];
                    const editableFields = editableRequirementFields(
                      item, draft, componentAdditionalFields, isAzureConfirmation,
                    );
                    const availableOptionalFields = (
                      item.available_billing_fields?.length
                        ? item.available_billing_fields
                        : (isAzureConfirmation
                          ? []
                          : (AWS_SERVICE_OPTIONAL_USAGE_FIELDS[item.service] ?? []))
                    ).filter(
                      (field) => !editableFields.includes(field),
                    );
                    const isEditing = editingComponents[item.component_id] === true;
                    const isDeleted = deletedComponents[item.component_id] === true;
                    const isRefreshing = refreshingComponentIds.includes(item.component_id);
                    const isSubmittingComponent = submittingComponentIds.includes(item.component_id);
                    const isQueuedComponent = queuedComponentIds.includes(item.component_id);
                    const wasUpdated = recentlyUpdatedComponentIds.includes(item.component_id);
                    const rowClassName = [
                      isDeleted ? "pending-delete" : (feedback.trim() || structuredUpdate) ? "needs-change" : "",
                      isRefreshing ? "row-refreshing" : "",
                      wasUpdated ? "row-updated" : "",
                    ].filter(Boolean).join(" ");
                    return (
                      <Fragment key={item.component_id}>
                        <tr id={`configuration-${item.component_id}`} className={`${rowClassName} ${item.parent_component_id ? "review-child-row" : ""}`.trim()}>
                          <td className="review-index-cell">{item.component_number ?? String(index + 1).padStart(2, "0")}</td>
                          <td className="review-service-cell"><strong>{item.parent_component_id ? "↳ " : ""}{displayServiceName(item)}</strong>{item.parent_component_number && <small className="component-parent-label">由 {item.parent_component_number} · {item.parent_display_name ?? "父组件"} 衍生</small>}</td>
                          <td className="review-detail-cell">
                            <div className="configuration-comparison">
                              <div className="configuration-comparison-source">
                                <small className="configuration-comparison-label">客户原话</small>
                                <p>{item.source_text?.trim() || "客户未提供单独说明"}</p>
                              </div>
                              <div className="configuration-comparison-result">
                                <small className="configuration-comparison-label">生成配置</small>
                                <span>{sharedRegion && !isGlobalService(item) ? "" : `${displayRegion(item)} · `}{displayPlan(item)} · {displayQuantity(item)}</span>
                                <small>{configurationText(item.requirements, item.service, item.official_specifications)}</small>
                                {item.pricing_status !== "ready" && !isSystemPricingIssue(item) && <small className={pricingNoticeClass(item)}>
                                  {item.pricing_notice ?? "此配置尚未完成官方核验。"}
                                </small>}
                              </div>
                            </div>
                          </td>
                          <td className="review-action-cell">
                            {isRefreshing ? <span className="row-refresh-state"><i />更新中</span> : isQueuedComponent ? <span className="row-refresh-state queued"><i />等待提交</span> : <div className="review-action-buttons">
                              {!isDeleted && <button
                                type="button"
                                className={(feedback.trim() || structuredUpdate) ? "has-feedback" : ""}
                                onClick={() => openComponentEditor(item)}
                              >{isEditing ? "收起" : (feedback.trim() || structuredUpdate) ? "已修改" : "修改"}</button>}
                              <button
                                type="button"
                                className={isDeleted ? "undo-delete" : "delete-configuration"}
                                onClick={() => {
                                  if (!isDeleted && !window.confirm(`确定删除“${displayServiceName(item)}”吗？`)) return;
                                  setDeletedComponents((current) => ({
                                    ...current,
                                    [item.component_id]: !isDeleted,
                                  }));
                                  if (!isDeleted) {
                                    setEditingComponents((current) => ({ ...current, [item.component_id]: false }));
                                    setPendingEditorSwitch(null);
                                    discardComponentDraft(item.component_id);
                                  }
                                }}
                              >{isDeleted ? "撤销" : "删除"}</button>
                              {isDeleted && <button
                                type="button"
                                className="submit-row-change"
                                disabled={isSubmittingComponent}
                                onClick={() => void submitConfigurationFeedback(item.component_id)}
                              >{isSubmittingComponent ? "提交中" : "提交"}</button>}
                            </div>}
                          </td>
                        </tr>
                        {isEditing && !isDeleted && (
                          <tr className="review-feedback-row">
                            <td colSpan={4}>
                              <div className="customer-component-feedback-box">
                                <>
                                  <div className="customer-structured-editor-heading">
                                    <strong>直接修改 {displayServiceName(item)} 的配置</strong>
                                    <span>{item.service === "eks"
                                      ? "这里只修改 AWS 托管控制面；工作节点请在对应的 EC2 组件中修改。"
                                      : "只改需要调整的字段，其他配置保持不变。"}</span>
                                  </div>
                                  <div className="customer-structured-editor-grid">
                                    <label>
                                      <span>{item.service === "eks" ? "集群数量" : "数量"}</span>
                                      <input
                                        type="number"
                                        min="1"
                                        max="10000"
                                        value={draft.quantity}
                                        onChange={(event) => updateTransientNumericField(
                                          item, "quantity", "quantity", event.target.value,
                                        )}
                                      />
                                    </label>
                                    {!isGlobalService(item) && <label>
                                      <span>区域</span>
                                      <select
                                        value={draft.region}
                                        onChange={(event) => {
                                          const nextRegion = event.target.value;
                                          updateComponentField(item, "region", "region", nextRegion);
                                          if (isAzureConfirmation) {
                                            updateComponentField(
                                              item,
                                              "requirements",
                                              "requested_sku",
                                              null,
                                            );
                                          }
                                          void loadOfficialFieldOptions(
                                            item,
                                            nextRegion,
                                            draft.requirements,
                                          );
                                        }}
                                      >
                                        {Array.from(new Set([
                                          draft.region,
                                          ...(liveFieldOptions[`${item.component_id}:region`] ?? [])
                                            .map(String),
                                          ...(isAzureConfirmation
                                            ? []
                                            : Object.keys(REGION_LABELS).filter(
                                                (region) => region !== "global" && region.includes("-"),
                                              )),
                                        ].filter(Boolean))).map((region) => <option key={region} value={region}>
                                          {REGION_LABELS[region] ?? region}
                                        </option>)}
                                      </select>
                                    </label>}
                                    {editableFields.map((field) => {
                                      const value = draft.requirements[field] ?? "";
                                      const isBoolean = typeof value === "boolean";
                                      const isNumeric = NUMERIC_CONFIGURATION_FIELDS.has(field);
                                      const isCpu = field === "vcpu" || field.endsWith("_vcpu");
                                      const isMemory = field === "memory_gib" || field.endsWith("_memory_gib");
                                      let selectOptions = [
                                        ...configuredFieldOptions(item, field, isAzureConfirmation),
                                        ...(liveFieldOptions[`${item.component_id}:${field}`] ?? []),
                                      ].filter((option, optionIndex, allOptions) => allOptions.findIndex(
                                        (candidate) => String(candidate).toLowerCase()
                                          === String(option).toLowerCase(),
                                      ) === optionIndex);
                                      const currentOperatingSystem = String(
                                        draft.requirements.operating_system ?? "",
                                      ).toLowerCase();
                                      const currentArchitecture = String(
                                        draft.requirements.architecture ?? "",
                                      ).toLowerCase();
                                      if (field === "architecture" && currentOperatingSystem.includes("windows")) {
                                        selectOptions = selectOptions.filter((option) => [
                                          "x86_64", "x86-64", "amd64",
                                        ].includes(String(option).toLowerCase()));
                                      }
                                      if (field === "operating_system" && [
                                        "arm", "arm64", "aarch64",
                                      ].includes(currentArchitecture)) {
                                        selectOptions = selectOptions.filter(
                                          (option) => !String(option).toLowerCase().includes("windows"),
                                        );
                                      }
                                      const useOptionSelect = selectOptions.length > 1
                                        && !(isNumeric && isUsageGibField(field));
                                      const isFixedOption = selectOptions.length === 1 && !isNumeric;
                                      const renderedSelectOptions = [
                                        ...(value !== "" && value !== null ? [value] : []),
                                        ...selectOptions,
                                      ].filter((option, optionIndex, allOptions) => allOptions.findIndex(
                                        (candidate) => String(candidate).toLowerCase()
                                          === String(option).toLowerCase(),
                                      ) === optionIndex);
                                      const unitKey = `${item.component_id}:${field}`;
                                      const selectedUnit = fieldUnits[unitKey]
                                        ?? (typeof value === "number" && value >= 1024 ? "tib" : "gib");
                                      const shapes = availableShapes(
                                        item,
                                        draft,
                                        liveAvailableShapes[item.component_id] ?? [],
                                      );
                                      const pairedCpuField = pairedShapeField(field, "cpu");
                                      const selectedCpu = isCpu
                                        ? value
                                        : draft.requirements[pairedCpuField];
                                      const numericOptions = uniqueNumericOptions([
                                        ...(isCpu
                                          ? shapes.map((shape) => shape.vcpu)
                                          : shapes
                                            .filter((shape) => shape.vcpu === Number(selectedCpu))
                                            .map((shape) => shape.memory_gib)),
                                        ...(typeof value === "number" ? [value] : []),
                                      ]);
                                      const isAdditionalField = componentAdditionalFields.includes(field);
                                      const billingFieldLabel = item.available_billing_labels?.[field]
                                        ?? FIELD_LABELS[field]
                                        ?? field;
                                      const fieldDisplayLabel = item.available_billing_labels?.[field]
                                        ?? editableFieldLabel(field);
                                      return <div
                                        className={`customer-structured-field${isAdditionalField ? " is-optional" : ""}`}
                                        key={field}
                                      >
                                        <label
                                          id={isAdditionalField
                                            ? `added-usage-${item.component_id}-${field}`
                                            : undefined}
                                        >
                                          <span>{fieldDisplayLabel}</span>
                                        {isBoolean ? <select
                                          value={value ? "true" : "false"}
                                          onChange={(event) => updateComponentField(
                                            item, "requirements", field, event.target.value === "true",
                                          )}
                                        >
                                          <option value="true">开启</option>
                                          <option value="false">关闭</option>
                                        </select> : (isCpu || isMemory) ? <select
                                          value={String(value)}
                                          disabled={isMemory && typeof selectedCpu !== "number"}
                                          onChange={(event) => {
                                            const nextValue = Number(event.target.value);
                                            updateComponentField(
                                              item, "requirements", field, nextValue,
                                            );
                                            if (isAzureConfirmation && (isCpu || isMemory)) {
                                              updateComponentField(
                                                item, "requirements", "requested_sku", null,
                                              );
                                            }
                                            if (isCpu) {
                                              const memoryField = pairedShapeField(field, "memory");
                                              const currentMemory = draft.requirements[memoryField];
                                              const validMemories = shapes
                                                .filter((shape) => shape.vcpu === nextValue)
                                                .map((shape) => shape.memory_gib);
                                              if (
                                                typeof currentMemory === "number"
                                                && !validMemories.includes(currentMemory)
                                              ) {
                                                updateComponentField(
                                                  item, "requirements", memoryField, null,
                                                );
                                              }
                                            }
                                          }}
                                        >
                                          <option value="" disabled>
                                            {isMemory && typeof selectedCpu !== "number"
                                              ? "请先选择处理器"
                                              : "请选择"}
                                          </option>
                                          {numericOptions.map((option) => <option key={option} value={option}>
                                            {option} {isCpu ? "vCPU" : "GiB"}
                                          </option>)}
                                        </select> : isFixedOption ? <div className="customer-fixed-field-value">
                                          {FIELD_OPTION_LABELS[field]?.[String(selectOptions[0]).toLowerCase()]
                                            ?? VALUE_LABELS[String(selectOptions[0]).toLowerCase()]
                                            ?? String(selectOptions[0])}
                                          <small>该服务当前仅支持此选项</small>
                                        </div> : useOptionSelect ? <select
                                          value={String(value)}
                                          onChange={(event) => {
                                            const selected = selectOptions.find(
                                              (option) => String(option) === event.target.value,
                                            );
                                            const nextValue = selected ?? event.target.value;
                                            updateComponentField(
                                              item, "requirements", field, nextValue,
                                            );
                                            if (isAzureConfirmation && field === "requested_sku") {
                                              const selectedShape = (
                                                liveAvailableShapes[item.component_id] ?? []
                                              ).find((shape) => shape.model === String(nextValue));
                                              if (selectedShape) {
                                                updateComponentField(
                                                  item,
                                                  "requirements",
                                                  "vcpu",
                                                  selectedShape.vcpu,
                                                );
                                                updateComponentField(
                                                  item,
                                                  "requirements",
                                                  "memory_gib",
                                                  selectedShape.memory_gib,
                                                );
                                              }
                                            }
                                            if (
                                              field === "operating_system"
                                              && String(nextValue).toLowerCase().includes("windows")
                                              && ["arm", "arm64", "aarch64"].includes(currentArchitecture)
                                            ) {
                                              updateComponentField(
                                                item, "requirements", "architecture", "x86_64",
                                              );
                                            }
                                            if (
                                              field === "architecture"
                                              && ["arm", "arm64", "aarch64"].includes(
                                                String(nextValue).toLowerCase(),
                                              )
                                              && currentOperatingSystem.includes("windows")
                                            ) {
                                              updateComponentField(
                                                item, "requirements", "operating_system", "linux",
                                              );
                                            }
                                          }}
                                        >
                                          <option value="" disabled>请选择</option>
                                          {renderedSelectOptions.map((option) => <option key={String(option)} value={String(option)}>
                                            {typeof option === "string"
                                              ? (FIELD_OPTION_LABELS[field]?.[option.toLowerCase()]
                                                ?? VALUE_LABELS[option.toLowerCase()]
                                                ?? option)
                                              : String(option)}
                                          </option>)}
                                        </select> : isNumeric && isUsageGibField(field) ? <div className="customer-value-with-unit">
                                          <input
                                            type="number"
                                            min="0"
                                            step="any"
                                            value={value === "" ? "" : Number(value) / (selectedUnit === "tib" ? 1024 : 1)}
                                            placeholder="请输入用量"
                                            onChange={(event) => updateTransientNumericField(
                                              item,
                                              "requirements",
                                              field,
                                              event.target.value,
                                              selectedUnit === "tib" ? 1024 : 1,
                                            )}
                                          />
                                          <select
                                            aria-label={`${billingFieldLabel}单位`}
                                            value={selectedUnit}
                                            onChange={(event) => setFieldUnits((current) => ({
                                              ...current,
                                              [unitKey]: event.target.value as "gib" | "tib",
                                            }))}
                                          >
                                            <option value="gib">GiB</option>
                                            <option value="tib">TiB</option>
                                          </select>
                                        </div> : isAzureConfirmation && !isNumeric
                                          ? <div className="customer-fixed-field-value">
                                              {value === "" ? "等待官方可选项" : String(value)}
                                              <small>该字段不支持手动填写，系统仅接受 Microsoft 官方选项</small>
                                            </div>
                                          : <input
                                          type={isNumeric ? "number" : "text"}
                                          min={isNumeric ? "0" : undefined}
                                          step={isNumeric ? "any" : undefined}
                                          value={String(value)}
                                          placeholder={isNumeric ? "请输入数字" : `请输入${billingFieldLabel}`}
                                          onChange={(event) => isNumeric
                                            ? updateTransientNumericField(
                                                item,
                                                "requirements",
                                                field,
                                                event.target.value,
                                              )
                                            : updateComponentField(
                                                item,
                                                "requirements",
                                                field,
                                                event.target.value,
                                              )}
                                        />}
                                        </label>
                                        {isAdditionalField && <button
                                          type="button"
                                          className="customer-remove-usage-field"
                                          aria-label={`删除${billingFieldLabel}`}
                                          title="删除这个计费项"
                                          onClick={() => {
                                            updateComponentField(
                                              item, "requirements", field, null,
                                            );
                                            setAdditionalFields((current) => ({
                                              ...current,
                                              [item.component_id]: (current[item.component_id] ?? [])
                                                .filter((candidate) => candidate !== field),
                                            }));
                                            setFieldUnits((current) => {
                                              const next = { ...current };
                                              delete next[unitKey];
                                              return next;
                                            });
                                          }}
                                        >×</button>}
                                      </div>;
                                    })}
                                  </div>
                                  {availableOptionalFields.length > 0 && <div className="customer-add-usage-field">
                                    <select
                                      aria-label="其他计费项"
                                      value={additionalFieldChoice[item.component_id] ?? ""}
                                      onChange={(event) => {
                                        const field = event.target.value;
                                        if (!field) return;
                                        updateComponentField(
                                          item, "requirements", field, 0,
                                        );
                                        setAdditionalFields((current) => ({
                                          ...current,
                                          [item.component_id]: Array.from(new Set([
                                            ...(current[item.component_id] ?? []), field,
                                          ])),
                                        }));
                                        setAdditionalFieldChoice((current) => ({
                                          ...current,
                                          [item.component_id]: "",
                                        }));
                                        window.setTimeout(() => {
                                          document.getElementById(
                                            `added-usage-${item.component_id}-${field}`,
                                          )?.scrollIntoView({ behavior: "smooth", block: "center" });
                                        }, 0);
                                      }}
                                    >
                                      <option value="" disabled>请选择要添加的计费项</option>
                                      {availableOptionalFields.map((field) => <option key={field} value={field}>
                                        {item.available_billing_labels?.[field] ?? FIELD_LABELS[field] ?? field}
                                      </option>)}
                                    </select>
                                  </div>}
                                  <div className="customer-structured-editor-actions">
                                    <button
                                      type="button"
                                      className={isRefreshing ? "is-refreshing" : ""}
                                      disabled={isSubmittingComponent || isQueuedComponent || isRefreshing}
                                      onClick={() => void submitConfigurationFeedback(item.component_id)}
                                    >{isRefreshing ? "更新中…" : isSubmittingComponent ? "提交中…" : isQueuedComponent ? "等待提交" : failedComponentIds.includes(item.component_id) ? "重新尝试" : "保存本项"}</button>
                                  </div>
                                  {componentEditorNotices[item.component_id] && (
                                    <p className="customer-component-editor-notice" role="status">
                                      {componentEditorNotices[item.component_id]}
                                    </p>
                                  )}
                                  {pendingEditorSwitch?.fromId === item.component_id && (
                                    <div className="customer-editor-switch-confirmation" role="alert">
                                      <span>{pendingEditorSwitch.toId
                                        ? "当前组件还有未保存的修改，是否保存后再打开其他组件？"
                                        : "当前组件还有未保存的修改，收起前是否保存？"}</span>
                                      <div>
                                        <button type="button" onClick={() => completeEditorSwitch(true)}>
                                          {pendingEditorSwitch.toId ? "保存并继续" : "保存并收起"}
                                        </button>
                                        <button type="button" onClick={() => completeEditorSwitch(false)}>
                                          {pendingEditorSwitch.toId ? "放弃并继续" : "放弃并收起"}
                                        </button>
                                        <button type="button" onClick={() => setPendingEditorSwitch(null)}>
                                          继续编辑
                                        </button>
                                      </div>
                                    </div>
                                  )}
                                </>
                              </div>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                  {addingConfigurationInProgress
                    && pendingAddition
                    && session.configuration_items.length <= pendingAddition.existingComponentIds.length
                    && (
                      <tr className="customer-pending-addition-row" aria-live="polite">
                        <td className="review-index-cell">{pendingAddition.expectedNumber}</td>
                        <td className="review-service-cell"><strong>正在识别新组件</strong></td>
                        <td className="review-detail-cell">
                          <div className="configuration-comparison">
                            <div className="configuration-comparison-source">
                              <small className="configuration-comparison-label">客户新加内容</small>
                              <p>{pendingAddition.sourceText}</p>
                            </div>
                            <div className="configuration-comparison-result customer-pending-addition-result">
                              <small className="configuration-comparison-label">独立处理窗口</small>
                              <span>只识别并核验第 {pendingAddition.expectedNumber} 项</span>
                              <small>原有 {pendingAddition.existingComponentIds.length} 项配置保持不变</small>
                            </div>
                          </div>
                        </td>
                        <td className="review-action-cell"><span className="row-refresh-state"><i />处理中</span></td>
                      </tr>
                    )}
                </tbody>
              </table>
            </div>
            <div className="customer-configuration-actions">
              {hasPendingConfigurationChanges ? (
                <button
                  className="customer-feedback-submit"
                  type="button"
                  disabled={submitting}
                  onClick={() => void submitConfigurationFeedback()}
                >{submitting
                    ? (additionFeedback.trim() ? "正在重新识别…" : "正在保存…")
                    : (additionFeedback.trim() ? "重新识别配置" : "保存全部修改")}</button>
              ) : (
                <button
                  className="customer-submit"
                  type="button"
                  disabled={submitting || isConfigurationRefreshActive || hasStandaloneConfirmationQuestions || customerBlockingItems.length > 0}
                  onClick={() => void approveConfiguration()}
                >{submitting
                    ? "正在确认…"
                    : customerBlockingItems.length > 0
                      ? `请先修改 ${customerBlockingItems.length} 项不可用配置`
                      : "最终确认并开始报价"}</button>
              )}
            </div>
            {hasPendingConfigurationChanges && (
              <p className="customer-pending-feedback">{isAzureConfirmation || additionFeedback.trim()
                ? "系统将重新生成新增、删除或修改后的配置清单。"
                : "系统将按您填写的字段重新校验配置，未修改内容保持不变。"}</p>
            )}
            {error && <p className="customer-submit-error">{error}</p>}
            {addingConfigurationInProgress && hasStandaloneConfirmationQuestions && (
              <div className="customer-addition-modal-backdrop" role="presentation">
                <div className="customer-addition-modal" role="dialog" aria-modal="true" aria-label="补充新增配置信息">
                  <header>
                    <div>
                      <small>新增配置 · 第 {pendingAddition?.expectedNumber ?? session.configuration_items.length} 项</small>
                      <h2>请补充这项配置</h2>
                      <p>这里只处理刚刚新增的内容，原来的配置不会重新运行。</p>
                    </div>
                  </header>
                  <div className="customer-addition-questions customer-questions">
                    {renderConfirmationItems()}
                  </div>
                  <footer>
                    <button
                      className="customer-submit"
                      type="button"
                      disabled={submitting || isSessionReviewing || session.confirmation_items.some((item) => !confirmationComplete(item, answers[confirmationAnswerKey(item)]))}
                      onClick={() => void submit()}
                    >{submitting || isSessionReviewing ? "正在处理…" : "确认并加入配置"}</button>
                  </footer>
                </div>
              </div>
            )}
          </>
        ) : hasStandaloneConfirmationQuestions && session ? (
          <div className="customer-question-page">
            <div className="customer-confirm-title customer-question-heading">
              <small>{addingConfigurationInProgress ? "新增配置 · 补充信息" : "配置校验 · 需要确认"}</small>
              <h1>{addingConfigurationInProgress ? "请补充新增配置信息" : "请确认以下配置选项"}</h1>
              <p>为确保报价准确，请根据实际业务需求完成以下配置选择，并统一提交。</p>
            </div>
            {isSessionReviewing && <div className="configuration-refresh-status customer-inline-review" role="status">
              <i />
              <span>正在处理您刚才提交的选择</span>
              <small>当前内容保留在本页，不需要重新填写</small>
            </div>}
            <div className="customer-question-scroll">
              {renderConfirmationItems()}
            </div>
            <div className="customer-question-footer">
              {error && <p className="customer-submit-error">{error}</p>}
              <button className="customer-submit" type="button" disabled={submitting || isSessionReviewing || session.confirmation_items.some((item) => !confirmationComplete(item, answers[confirmationAnswerKey(item)]))} onClick={() => void submit()}>{submitting || isSessionReviewing ? "正在处理…" : addingConfigurationInProgress ? "确认并继续添加" : "确认配置并提交"}</button>
            </div>
          </div>
        ) : session ? (
          <div className="customer-confirm-state">确认页面正在准备下一步…</div>
        ) : null}
      </section>
    </main>
  );
}
