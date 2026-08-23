"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import { ConfigurationOptionPicker, type ConfigurationChoice } from "../../components/configuration-option-picker";

type Item = { question: string; options: ConfigurationChoice[]; selection_mode?: "buttons" | "catalog" };
type ConfigurationItem = {
  component_id: string;
  service: string;
  display_name: string;
  region?: string | null;
  quantity: number;
  selected_model?: string | null;
  official_specifications?: Record<string, unknown>;
  pricing_status: "ready" | "unpriced";
  pricing_notice?: string | null;
  requirements: Record<string, unknown>;
  source_text: string;
};
type Session = {
  token: string;
  status: "pending" | "submitted" | "reviewing" | "configuration_review" | "approved" | "completed";
  customer_summary: string;
  confirmation_items: Item[];
  answers: Record<string, string>;
  configuration_items: ConfigurationItem[];
};

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/backend";
const DELETE_COMPONENT_MARKER = "__DELETE_COMPONENT__";
const FIELD_LABELS: Record<string, string> = {
  vcpu: "处理器", memory_gib: "内存", operating_system: "操作系统",
  architecture: "处理器架构", tenancy: "租用方式", business_type: "实例用途",
  system_disk_gib: "系统盘", total_system_disk_gib: "系统盘总容量",
  total_worker_system_disk_gib: "工作节点系统盘总容量", storage_gib: "单项存储容量",
  total_storage_gib: "总存储容量", storage_gib_per_node: "每节点存储",
  storage_gib_per_broker: "每个 Broker 存储", engine: "数据库或缓存引擎", engine_version: "引擎版本",
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
  applications: "应用框架", deployment_type: "部署类型", nodes: "计算节点数",
  managed_storage_gib: "托管存储容量", snapshot_storage_gib: "快照存储容量",
  rpu: "计算容量（RPU）", hours_per_month: "每月运行时长",
  data_scanned_gib: "查询扫描数据量", queries: "查询次数",
  provisioned_dpu_hours: "预置容量（DPU 小时）",
  secret_count: "密钥数量", key_count: "KMS 密钥数量", vpc_count: "VPC 数量",
  public_subnets: "公有子网数量", private_subnets: "私有子网数量", availability_zones: "可用区数量",
  data_transfer_out_gib: "每月出站流量", data_processed_gib: "每月处理数据量",
  requests: "每月请求量", https_requests: "每月 HTTPS 请求量", listeners: "监听器数量",
  storage_iops: "存储 IOPS", storage_throughput_mbps: "存储吞吐量",
  backup_retention_days: "备份保留天数", read_replica_count: "只读副本数",
  detailed_monitoring: "详细监控", performance_insights: "性能分析",
  enhanced_monitoring: "增强监控", dedicated_master: "专用主节点",
};

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
  aurora_mysql: "Aurora MySQL", aurora_postgresql: "Aurora PostgreSQL",
};

const HIDDEN_CONFIGURATION_FIELDS = new Set([
  "requested_model", "system_default_assumption", "reference_unit_only",
  "reference_lcu_unit_only", "_review_selected_model", "_quote_skip_reason",
  "data_transfer_monitoring", "purchase_option", "reserved_term_years",
  "payment_option", "utilization_percent",
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
  "data_transfer_out_gib", "data_processed_gib", "requests", "https_requests", "listeners",
  "storage_iops", "storage_throughput_mbps", "backup_retention_days", "read_replica_count",
  "nodes", "managed_storage_gib", "snapshot_storage_gib", "rpu", "hours_per_month",
  "data_scanned_gib", "queries", "provisioned_dpu_hours",
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
    && key.includes("storage_gib")
    && value >= 1024
    && Number.isInteger(value / 1024)
  ) return `${value / 1024} TB`;
  const suffix = key.endsWith("_gib") || key.endsWith("_gib_per_node")
    ? " GiB"
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
    ([key, value]) => key in FIELD_LABELS
      && !HIDDEN_CONFIGURATION_FIELDS.has(key)
      && !(service === "elasticache" && ["shards", "replicas_per_shard"].includes(key))
      && value !== null
      && value !== ""
      && (!NUMERIC_CONFIGURATION_FIELDS.has(key) || typeof value === "number"),
  );
  const descriptions = entries.map(
    ([key, value]) => `${FIELD_LABELS[key]}：${formatConfigurationValue(key, value)}`,
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
  };
  if (/\baurora\b/i.test(item.display_name)) return item.display_name;
  return names[item.service] ?? item.display_name;
}

function displayRegion(item: ConfigurationItem): string {
  if (["cloudfront", "route53", "global_accelerator"].includes(item.service)) return "全球";
  return item.region ? REGION_LABELS[item.region] ?? item.region : "使用本次报价区域";
}

function isGlobalService(item: ConfigurationItem): boolean {
  return ["cloudfront", "route53", "global_accelerator"].includes(item.service)
    || ["global", "全球"].includes(String(item.region ?? "").toLowerCase());
}

function displayPlan(item: ConfigurationItem): string {
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
    "CloudFront Pay-as-you-go": "按量付费",
    "S3 Standard": "标准存储",
  };
  return labels[raw] ?? raw;
}

function displayQuantity(item: ConfigurationItem): string {
  if (item.service === "msk") {
    const brokerCount = Number(item.requirements.broker_count);
    const clusterCount = Number.isFinite(item.quantity) && item.quantity > 0 ? item.quantity : 1;
    return Number.isFinite(brokerCount) && brokerCount > 0
      ? `${clusterCount} 套集群 · ${brokerCount} 个 Broker 节点`
      : `${clusterCount} 套集群`;
  }
  return `数量 ${item.quantity}`;
}

export default function CustomerConfirmationPage() {
  const token = useMemo(() => {
    if (typeof window === "undefined") return "";
    return window.location.pathname.split("/").filter(Boolean).at(-1) ?? "";
  }, []);
  const [session, setSession] = useState<Session | null>(null);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [componentFeedback, setComponentFeedback] = useState<Record<string, string>>({});
  const [editingComponents, setEditingComponents] = useState<Record<string, boolean>>({});
  const [deletedComponents, setDeletedComponents] = useState<Record<string, boolean>>({});
  const [additionFeedback, setAdditionFeedback] = useState("");
  const [addingConfiguration, setAddingConfiguration] = useState(false);
  const [reviewSeconds, setReviewSeconds] = useState(0);
  const [approvalSubmitted, setApprovalSubmitted] = useState(false);
  const [refreshingComponentIds, setRefreshingComponentIds] = useState<string[]>([]);
  const [submittingComponentIds, setSubmittingComponentIds] = useState<string[]>([]);
  const [recentlyUpdatedComponentIds, setRecentlyUpdatedComponentIds] = useState<string[]>([]);
  const [addingConfigurationInProgress, setAddingConfigurationInProgress] = useState(false);
  const regionalRegions = useMemo(() => Array.from(new Set(
    (session?.configuration_items ?? [])
      .filter((item) => !isGlobalService(item) && Boolean(item.region))
      .map((item) => String(item.region)),
  )), [session?.configuration_items]);
  const sharedRegion = regionalRegions.length === 1 ? regionalRegions[0] : null;
  const hasPendingComponentFeedback = Object.values(componentFeedback).some(
    (value) => value.trim().length > 0,
  );
  const hasPendingConfigurationChanges = hasPendingComponentFeedback
    || Object.values(deletedComponents).some(Boolean)
    || additionFeedback.trim().length > 0;
  const isConfigurationRefreshActive = (
    refreshingComponentIds.length > 0 || addingConfigurationInProgress
  ) && ["reviewing", "submitted"].includes(session?.status ?? "");
  const showConfigurationReview = session?.status === "configuration_review"
    || (isConfigurationRefreshActive && Boolean(session?.configuration_items.length));
  const isSessionReviewing = ["reviewing", "submitted"].includes(session?.status ?? "");

  useEffect(() => {
    if (!token) return;
    fetch(`${API_BASE}/api/confirmation-sessions/${token}`, { cache: "no-store" })
      .then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.message ?? "确认单不存在或已失效");
        return payload as Session;
      })
      .then((payload) => {
        setSession(payload);
        setAnswers(payload.answers ?? {});
      })
      .catch((reason) => setError(reason instanceof Error ? reason.message : "无法读取确认单"))
      .finally(() => setLoading(false));
  }, [token]);

  useEffect(() => {
    if (
      !token
      || !["reviewing", "submitted", "approved"].includes(session?.status ?? "")
    ) return;
    const refresh = () => {
      fetch(`${API_BASE}/api/confirmation-sessions/${token}`, { cache: "no-store" })
        .then((response) => response.json())
        .then((payload: Session) => {
          setSession((current) => ({
            ...payload,
            configuration_items: payload.configuration_items?.length
              ? payload.configuration_items
              : current?.configuration_items ?? [],
          }));
          if (payload.status === "configuration_review" && (
            refreshingComponentIds.length > 0 || addingConfigurationInProgress
          )) {
            setRecentlyUpdatedComponentIds(refreshingComponentIds);
            setRefreshingComponentIds([]);
            setAddingConfigurationInProgress(false);
          }
          if (payload.status === "pending") setAnswers({});
        })
        .catch(() => undefined);
    };
    const timer = window.setInterval(refresh, 1800);
    return () => window.clearInterval(timer);
  }, [token, session?.status, refreshingComponentIds, addingConfigurationInProgress]);

  useEffect(() => {
    if (recentlyUpdatedComponentIds.length === 0) return;
    const timer = window.setTimeout(() => setRecentlyUpdatedComponentIds([]), 1800);
    return () => window.clearTimeout(timer);
  }, [recentlyUpdatedComponentIds]);

  useEffect(() => {
    if (!isSessionReviewing) return;
    const timer = window.setInterval(() => setReviewSeconds((value) => value + 1), 1000);
    return () => window.clearInterval(timer);
  }, [isSessionReviewing]);

  async function submit() {
    if (!session || session.confirmation_items.some((item) => !answers[item.question]?.trim())) return;
    setReviewSeconds(0);
    setSubmitting(true);
    setError("");
    try {
      const response = await fetch(`${API_BASE}/api/confirmation-sessions/${token}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ answers }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message ?? "提交失败");
      setSession(payload as Session);
    } catch (reason) {
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

  async function submitConfigurationFeedback(componentId?: string) {
    if (!session) return;
    const componentChanges: Record<string, string> = {};
    const candidateIds = componentId
      ? [componentId]
      : Array.from(new Set([...Object.keys(componentFeedback), ...Object.keys(deletedComponents)]));
    candidateIds.forEach((candidateId) => {
      const feedback = componentFeedback[candidateId]?.trim() ?? "";
      if (deletedComponents[candidateId]) componentChanges[candidateId] = DELETE_COMPONENT_MARKER;
      else if (feedback) componentChanges[candidateId] = feedback;
    });
    const addedConfiguration = componentId ? "" : additionFeedback.trim();
    if (Object.keys(componentChanges).length === 0 && !addedConfiguration) return;
    const affectedComponentIds = Object.keys(componentChanges);
    setRefreshingComponentIds((current) => Array.from(new Set([...current, ...affectedComponentIds])));
    setAddingConfigurationInProgress(Boolean(addedConfiguration));
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
      if (componentId) {
        setComponentFeedback((current) => {
          const next = { ...current };
          delete next[componentId];
          return next;
        });
        setEditingComponents((current) => ({ ...current, [componentId]: false }));
        setDeletedComponents((current) => {
          const next = { ...current };
          delete next[componentId];
          return next;
        });
      } else {
        setComponentFeedback({});
        setEditingComponents({});
        setDeletedComponents({});
        setAdditionFeedback("");
        setAddingConfiguration(false);
      }
    } catch (reason) {
      setRefreshingComponentIds((current) => current.filter((id) => !affectedComponentIds.includes(id)));
      if (addedConfiguration) setAddingConfigurationInProgress(false);
      setError(reason instanceof Error ? reason.message : "提交修改失败，请重试");
    } finally {
      if (componentId) {
        setSubmittingComponentIds((current) => current.filter((id) => id !== componentId));
      } else {
        setSubmitting(false);
      }
    }
  }

  return (
    <main className={`customer-confirm-page ${showConfigurationReview ? "configuration-review-page" : ""}`}>
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
        ) : (session?.status === "reviewing" || session?.status === "submitted") && !isConfigurationRefreshActive ? (
          <div className="customer-confirm-state">
            <h1>正在复核，请稍等</h1>
            <div className="customer-review-progress" role="progressbar" aria-label="配置复核处理中"><i /></div>
            <p>系统正在只处理您填写了修改意见的组件，其他配置保持不变。</p>
            <small>已等待 {reviewSeconds} 秒，请不要关闭本页面</small>
          </div>
        ) : showConfigurationReview && session ? (
          <>
            <div className="customer-confirm-title customer-review-heading">
              <h1>请核对配置信息</h1>
              <p>如有不符，请直接修改、添加或删除。</p>
            </div>
            {isConfigurationRefreshActive && (
              <div className="configuration-refresh-status" role="status">
                <i />
                <span>
                  {addingConfigurationInProgress && refreshingComponentIds.length === 0
                    ? "正在添加配置"
                    : `正在更新 ${refreshingComponentIds.length} 项配置`}
                </span>
                <small>未修改的配置保持不变</small>
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
                <textarea
                  id="add-configuration"
                  value={additionFeedback}
                  onChange={(event) => setAdditionFeedback(event.target.value)}
                  placeholder="例如：新增 Amazon EC2，新加坡区域，2 台，4 核 16GB，Linux。"
                  rows={2}
                />
              </div>
            )}
            <div className="customer-configuration-table customer-review-table">
              <table>
                <colgroup><col className="review-index-column" /><col className="review-service-column" /><col className="review-detail-column" /><col className="review-action-column" /></colgroup>
                <thead><tr><th>序号</th><th>AWS 服务</th><th>配置详情</th><th>操作</th></tr></thead>
                <tbody>
                  {session.configuration_items.map((item, index) => {
                    const feedback = componentFeedback[item.component_id] ?? "";
                    const isEditing = editingComponents[item.component_id] === true;
                    const isDeleted = deletedComponents[item.component_id] === true;
                    const isRefreshing = refreshingComponentIds.includes(item.component_id);
                    const isSubmittingComponent = submittingComponentIds.includes(item.component_id);
                    const wasUpdated = recentlyUpdatedComponentIds.includes(item.component_id);
                    const rowClassName = [
                      isDeleted ? "pending-delete" : feedback.trim() ? "needs-change" : "",
                      isRefreshing ? "row-refreshing" : "",
                      wasUpdated ? "row-updated" : "",
                    ].filter(Boolean).join(" ");
                    return (
                      <Fragment key={item.component_id}>
                        <tr className={rowClassName}>
                          <td className="review-index-cell">{String(index + 1).padStart(2, "0")}</td>
                          <td className="review-service-cell"><strong>{displayServiceName(item)}</strong></td>
                          <td className="review-detail-cell">
                            <span>{sharedRegion && !isGlobalService(item) ? "" : `${displayRegion(item)} · `}{displayPlan(item)} · {displayQuantity(item)}</span>
                            <small>{configurationText(item.requirements, item.service, item.official_specifications)}</small>
                          </td>
                          <td className="review-action-cell">
                            {isRefreshing ? <span className="row-refresh-state"><i />更新中</span> : <div className="review-action-buttons">
                              {!isDeleted && <button
                                type="button"
                                className={feedback.trim() ? "has-feedback" : ""}
                                onClick={() => setEditingComponents((current) => ({
                                  ...current,
                                  [item.component_id]: !isEditing,
                                }))}
                              >{isEditing ? "收起" : feedback.trim() ? "已修改" : "修改"}</button>}
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
                                <label htmlFor={`component-feedback-${item.component_id}`}><strong>{displayServiceName(item)}</strong>：请说明要修改的字段和新值</label>
                                <div className="customer-component-feedback-input">
                                  <textarea
                                    id={`component-feedback-${item.component_id}`}
                                    value={feedback}
                                    onChange={(event) => setComponentFeedback((current) => ({
                                      ...current,
                                      [item.component_id]: event.target.value,
                                    }))}
                                    placeholder="例如：数量改为 3，存储改为 500GB。"
                                    rows={2}
                                  />
                                  <button
                                    type="button"
                                    disabled={!feedback.trim() || isSubmittingComponent}
                                    onClick={() => void submitConfigurationFeedback(item.component_id)}
                                  >{isSubmittingComponent ? "提交中…" : "提交本项"}</button>
                                </div>
                              </div>
                            </td>
                          </tr>
                        )}
                      </Fragment>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {session.configuration_items.some((item) => item.pricing_status !== "ready") && (
              <p className="customer-submit-error">
                这份确认单仍有组件未完成官方预检，不能提交报价。请返回报价页面重新分析，系统会先集中询问不确定项。
              </p>
            )}
            <div className="customer-configuration-actions">
              {hasPendingConfigurationChanges ? (
                <button
                  className="customer-feedback-submit"
                  type="button"
                  disabled={submitting}
                  onClick={() => void submitConfigurationFeedback()}
                >{submitting ? "正在重新识别…" : "重新识别配置"}</button>
              ) : (
                <button
                  className="customer-submit"
                  type="button"
                  disabled={submitting || isConfigurationRefreshActive || session.configuration_items.some((item) => item.pricing_status !== "ready")}
                  onClick={() => void approveConfiguration()}
                >{submitting ? "正在确认…" : session.configuration_items.some((item) => item.pricing_status !== "ready") ? "等待规格核验完成" : "下一步：报价确认"}</button>
              )}
            </div>
            {hasPendingConfigurationChanges && (
              <p className="customer-pending-feedback">系统将重新生成新增、删除或修改后的配置清单。</p>
            )}
            {error && <p className="customer-submit-error">{error}</p>}
          </>
        ) : session ? (
          <>
            <div className="customer-confirm-title"><small>配置确认</small><h1>请确认以下配置</h1><p>仅填写需要您决定的项目；未提供用量的项目按最小单位单价计算。</p></div>
            <div className="customer-summary"><strong>需求摘要</strong><p>{session.customer_summary}</p></div>
            <div className="customer-questions">
              {session.confirmation_items.map((item, index) => (
                <article key={item.question}>
                  <label><b>{index + 1}</b><span>{item.question}</span></label>
                  {item.options.length > 0 && <ConfigurationOptionPicker
                    className="customer-options"
                    options={item.options}
                    value={answers[item.question]}
                    catalog={item.selection_mode === "catalog" || item.options.some((option) => Boolean(option.model))}
                    onChange={(selected) => setAnswers((current) => ({ ...current, [item.question]: selected }))}
                  />}
                  {item.options.length === 0 && <input value={answers[item.question] ?? ""} onChange={(event) => setAnswers((current) => ({ ...current, [item.question]: event.target.value }))} placeholder="填写您的选择" />}
                </article>
              ))}
            </div>
            {error && <p className="customer-submit-error">{error}</p>}
            <button className="customer-submit" type="button" disabled={submitting || session.confirmation_items.some((item) => !answers[item.question]?.trim())} onClick={() => void submit()}>{submitting ? "正在提交…" : "全部填写完成，统一提交"}</button>
          </>
        ) : null}
      </section>
    </main>
  );
}
