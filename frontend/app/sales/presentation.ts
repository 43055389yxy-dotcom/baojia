type ProgressJob = {
  status?: string;
  active_quote_count?: number;
  max_concurrent_quotes?: number;
  queued_ahead_count?: number;
  estimated_wait_minutes?: number;
  progress?: {
    stage?: string;
    total_component_count?: number;
    completed_component_count?: number;
    failed_component_count?: number;
    component_chat_count?: number;
  };
};

type UnpricedComponent = {
  failure_code?: string;
  failure_category?: string;
  provider_code?: string;
  retryable?: boolean;
};

export function queuedStatusDetail(job: ProgressJob) {
  const activeCount = job.active_quote_count ?? 0;
  const capacity = job.max_concurrent_quotes ?? 4;
  const queuedAhead = job.queued_ahead_count ?? 0;
  const waitMinutes = job.estimated_wait_minutes ?? 0;
  const parts = [`当前占用 ${activeCount}/${capacity} 个报价名额`];
  if (queuedAhead > 0) parts.push(`${queuedAhead} 个任务排在您前面`);
  const wait = waitMinutes > 0 ? `预计等待约 ${waitMinutes} 分钟` : "等待系统分配名额";
  return `${parts.join("，")}，${wait}。`;
}

export function processingStatusDetail(job: ProgressJob) {
  const progress = job.progress;
  const stage = progress?.stage;
  if (stage === "artifacts_generated") return "报价已核对，正在生成销售页面。";
  if (stage === "estimate_validated") return "报价已通过核对，正在生成 Excel。";
  const total = progress?.total_component_count;
  const completed = progress?.completed_component_count ?? 0;
  const failed = progress?.failed_component_count ?? 0;
  if (typeof total === "number" && total > 0) {
    const failedText = failed > 0 ? `，${failed} 个暂未完成，成功结果已保存` : "";
    const batches = progress?.component_chat_count ?? 1;
    const batchText = batches > 1 ? `，共 ${batches} 批按可用名额处理` : "";
    const retryText = stage === "pricing_request_rejected" ? "正在修正查价请求，重试次数有限。" : "";
    return `后台已确认 ${completed}/${total} 个组件完成${failedText}${batchText}。${retryText}`;
  }
  if (stage === "pricing_request_rejected") return "官方查价请求需要修正，系统正在进行有限重试。";
  return "正在整理需求并建立组件清单。";
}

export function progressPercent(job: ProgressJob): number | null {
  if (job.status !== "processing") return null;
  const total = job.progress?.total_component_count;
  const completed = job.progress?.completed_component_count;
  if (!Number.isFinite(total) || !total || total < 1 || !Number.isFinite(completed)) return null;
  return Math.max(0, Math.min(100, Math.round(((completed ?? 0) / total) * 100)));
}

export function unpricedRecoveryText(component: UnpricedComponent) {
  const categoryDescriptions: Record<string, string> = {
    credentials: "报价账号的询价凭证未配置或已失效",
    authorization: "报价账号缺少该产品的询价权限",
    provider_unavailable: "官方价格服务暂时不可用",
    transport: "连接官方价格服务时暂时中断",
    rate_limit: "官方价格接口暂时限流",
    invalid_request: "官方接口参数或当地可售规格需要调整",
    response_schema: "该产品的官方查价适配需要更新",
    official_api_error: "官方价格接口本次未能完成查询",
  };
  const descriptions: Record<string, string> = {
    official_price_unavailable: "暂未取得可用官方价格",
    official_query_failed: "本次官方价格查询未完成",
    unsupported_in_region: "当前地域暂未找到可购配置",
    retry_limit_reached: "已达到自动重试次数上限",
  };
  const reason = categoryDescriptions[component.failure_category ?? ""]
    ?? descriptions[component.failure_code ?? ""]
    ?? "本组件暂未完成报价";
  const action = component.retryable === false
    ? "请联系管理员处理后再重试"
    : "可选择只重试未完成组件";
  return `${reason}；${action}。`;
}

export function canRetryUnpriced(result: { is_partial?: boolean; unpriced_components?: UnpricedComponent[] } | null | undefined) {
  return Boolean(result?.is_partial && result.unpriced_components?.some((component) => component.retryable !== false));
}

export function money(value: string | undefined, currency: string) {
  if (value === undefined || value.trim() === "") return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return "—";
  const formatted = amount.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${formatted}${currency ? ` ${currency}` : ""}`;
}
