type ProgressJob = {
  status?: string;
  progress?: {
    stage?: string;
    total_component_count?: number;
    top_level_component_count?: number;
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

export function estimatedQuoteWindow(job: ProgressJob) {
  const componentCount = job.progress?.top_level_component_count
    ?? job.progress?.total_component_count
    ?? 0;
  return componentCount > 10 ? "15～30 分钟" : "10～20 分钟";
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
