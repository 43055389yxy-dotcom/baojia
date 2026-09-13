"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { canRetryUnpriced, money, processingStatusDetail, progressPercent, queuedStatusDetail, unpricedRecoveryText } from "./presentation";
import { validateNumberedComponentLines } from "./numbered-components";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/backend";
const ACTIVE_JOB_KEY = "astraquote.sales.active-job.v1";
const PENDING_SUBMISSION_KEY = "astraquote.sales.pending-submission.v1";

type ScenarioKey =
  | "on_demand"
  | "one_month_subscription"
  | "one_year_subscription"
  | "one_year_commitment"
  | "three_year_commitment";
type CloudProvider =
  | "aws" | "azure" | "oci" | "gcp"
  | "tencent" | "alibaba" | "huawei" | "baidu" | "volcengine" | "ctyun";
type QuoteEngine = "chatgpt";

type PageScenarioCost = {
  scenario_key: ScenarioKey;
  label: string;
  monthly_cost?: string;
  upfront_cost?: string;
  monthly_total?: string;
  upfront_total?: string;
  term_months?: number | null;
};

type QuickQuoteResult = {
  schema_version: "astraquote-page-result/1";
  is_partial?: boolean;
  currency: string;
  region: string;
  preferred_region?: string;
  region_adjustment_reason?: string;
  pricing_notice?: string;
  components: Array<{
    service_name: string;
    model_or_plan?: string;
    quantity?: string;
    configuration_summary?: string;
    scenario_costs: PageScenarioCost[];
  }>;
  unpriced_components?: Array<{
    service_name: string;
    model_or_plan?: string;
    quantity?: string;
    configuration_summary?: string;
    failure_code: string;
    failure_category?: string;
    provider_code?: string;
    retryable: boolean;
  }>;
  scenarios: PageScenarioCost[];
};

type RelayJob = {
  job_id: string;
  submission_code: string;
  status: "queued" | "processing" | "needs_login" | "completed" | "partial" | "failed" | "cancelled";
  created_at?: string;
  updated_at?: string;
  cloud_provider?: CloudProvider;
  preferred_region?: string;
  preferred_engine?: QuoteEngine;
  assigned_engine?: QuoteEngine | null;
  failure_code?: string | null;
  display_result_on_page?: boolean;
  quick_quote_result?: QuickQuoteResult | null;
  quote_download_url?: string | null;
  quote_download_filename?: string | null;
  max_concurrent_quotes?: number;
  active_quote_count?: number;
  queue_position?: number;
  queued_ahead_count?: number;
  jobs_ahead_count?: number;
  estimated_wait_minutes?: number;
  progress?: {
    stage: string;
    total_component_count?: number;
    top_level_component_count?: number;
    component_chat_count?: number;
    completed_component_count?: number;
    failed_component_count?: number;
    pending_component_count?: number;
    updated_at?: string;
  };
};

type RegionCatalog = {
  provider: CloudProvider;
  market_profile: string;
  site_label: string;
  regions: Array<{ code: string; label: string }>;
  pricing_scenarios: Array<{ key: ScenarioKey; label: string; term_months: number | null }>;
};

type RelayHealth = {
  status: "ready" | "offline";
  message?: string;
  provider_catalogs?: Partial<Record<CloudProvider, {
    available: boolean;
    message?: string;
  }>>;
  engines?: Partial<Record<QuoteEngine, {
    status: "ready" | "offline";
    max_concurrent_quotes: number;
  }>>;
};

const statusCopy: Record<RelayJob["status"], { title: string; detail?: string }> = {
  queued: { title: "报价正在排队", detail: "正在等待可用的报价名额。" },
  processing: { title: "报价申请已提交" },
  needs_login: { title: "报价等待登录", detail: "报价服务正在等待管理员恢复登录。" },
  completed: { title: "报价已完成", detail: "报价结果和 Excel 已生成。" },
  partial: { title: "部分报价已完成", detail: "已返回成功组件；未取得价格的组件没有计入合计。" },
  failed: { title: "报价失败", detail: "报价已经停止，请联系管理员处理。" },
  cancelled: { title: "报价已撤回", detail: "本次报价已停止处理。" },
};

const PROVIDER_META: Record<CloudProvider, { label: string; mark: string; detail: string }> = {
  aws: { label: "AWS", mark: "AWS", detail: "Amazon Web Services" },
  azure: { label: "微软 Azure", mark: "AZ", detail: "Microsoft Cloud" },
  oci: { label: "Oracle Cloud", mark: "OCI", detail: "Oracle Infrastructure" },
  gcp: { label: "Google Cloud", mark: "GCP", detail: "Google Cloud Platform" },
  tencent: { label: "腾讯云", mark: "TC", detail: "Tencent Cloud" },
  alibaba: { label: "阿里云", mark: "ALI", detail: "Alibaba Cloud" },
  huawei: { label: "华为云", mark: "HW", detail: "Huawei Cloud" },
  baidu: { label: "百度智能云", mark: "BD", detail: "Baidu AI Cloud" },
  volcengine: { label: "火山引擎", mark: "VE", detail: "Volcengine" },
  ctyun: { label: "天翼云", mark: "CT", detail: "CTyun" },
};

const PROVIDER_ORDER: CloudProvider[] = [
  "aws", "azure", "oci", "gcp", "tencent",
  "alibaba", "huawei", "baidu", "volcengine", "ctyun",
];

function estimateWindow() {
  return "5～10 分钟";
}

function providerLabel(provider: CloudProvider | undefined) {
  return PROVIDER_META[provider ?? "aws"].label;
}

function safeSubmissionError(status: number) {
  if (status === 422) return "报价选项与所选云厂商不一致，请检查后重新提交。";
  if (status === 429) return "当前提交较多，请稍候再试。";
  if (status >= 500) return "报价服务暂时不可用，请稍后重试。";
  return "报价提交失败，请检查填写内容后重试。";
}

function scenarioTermMonths(scenario: Pick<PageScenarioCost, "scenario_key" | "term_months">) {
  if (typeof scenario.term_months === "number") return scenario.term_months;
  if (["one_year_subscription", "one_year_commitment"].includes(scenario.scenario_key)) return 12;
  if (scenario.scenario_key === "three_year_commitment") return 36;
  if (scenario.scenario_key === "one_month_subscription") return 1;
  return null;
}

function scenarioMonthlyCaption(scenario: Pick<PageScenarioCost, "scenario_key" | "term_months">) {
  const months = scenarioTermMonths(scenario);
  return months && months > 1 ? `折合月费（总价÷${months}个月）` : "月费";
}

function quoteCopyText(job: RelayJob) {
  const result = job.quick_quote_result;
  if (!result) return "";
  const lines = [result.is_partial ? "部分报价（未完成组件未计入合计）" : "报价结果", `云厂商：${providerLabel(job.cloud_provider)}`, `区域：${result.region}`, ""];
  result.components.forEach((component, index) => {
    const identity = [component.service_name, component.model_or_plan, component.quantity]
      .filter(Boolean).join(" · ");
    lines.push(`${index + 1}. ${identity}`);
    if (component.configuration_summary) lines.push(`配置：${component.configuration_summary}`);
    component.scenario_costs.forEach((scenario) => {
      const upfront = Number(scenario.upfront_cost || 0) > 0
        ? `；预付总额 ${money(scenario.upfront_cost, result.currency)}`
        : "";
      lines.push(`${scenario.label}：${scenarioMonthlyCaption(scenario)} ${money(scenario.monthly_cost, result.currency)}${upfront}`);
    });
    lines.push("");
  });
  (result.unpriced_components || []).forEach((component, index) => {
    const identity = [component.service_name, component.model_or_plan, component.quantity]
      .filter(Boolean).join(" · ");
    lines.push(`${result.components.length + index + 1}. ${identity}`);
    if (component.configuration_summary) lines.push(`配置：${component.configuration_summary}`);
    lines.push("状态：尚未取得官方价格，未计入合计", "");
  });
  lines.push(result.is_partial ? "已核价部分小计" : "报价合计");
  result.scenarios.forEach((scenario) => {
    const upfront = Number(scenario.upfront_total || 0) > 0
      ? `；预付总额 ${money(scenario.upfront_total, result.currency)}`
      : "";
    lines.push(`${scenario.label}：${scenarioMonthlyCaption(scenario)} ${money(scenario.monthly_total, result.currency)}${upfront}`);
  });
  return lines.join("\n").trim();
}

async function submissionFingerprint(value: unknown) {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export default function SalesQuotePage() {
  const [requirement, setRequirement] = useState("");
  const [selectedScenarios, setSelectedScenarios] = useState(
    () => new Set<ScenarioKey>(["on_demand"]),
  );
  const [utilization, setUtilization] = useState(100);
  const [cloudProvider, setCloudProvider] = useState<CloudProvider>("aws");
  const [providerOpen, setProviderOpen] = useState(false);
  const [regionCatalog, setRegionCatalog] = useState<RegionCatalog | null>(null);
  const [preferredRegion, setPreferredRegion] = useState("");
  const [regionLoading, setRegionLoading] = useState(true);
  const [regionOpen, setRegionOpen] = useState(false);
  const providerPickerRef = useRef<HTMLDivElement>(null);
  const regionPickerRef = useRef<HTMLDivElement>(null);
  const [health, setHealth] = useState<RelayHealth | null>(null);
  const [job, setJob] = useState<RelayJob | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [pageError, setPageError] = useState("");
  const [trackedJobId, setTrackedJobId] = useState("");
  const [resultOpen, setResultOpen] = useState(false);
  const [copied, setCopied] = useState<"" | "quote" | "link">("");
  const active = Boolean(job && ["queued", "processing", "needs_login"].includes(job.status));
  const retryAvailable = job?.status === "partial" && canRetryUnpriced(job.quick_quote_result);
  const completedPercent = job ? progressPercent(job) : null;
  const resultReady = Boolean(job?.quick_quote_result);
  const inventoryReady = Boolean(job?.progress?.total_component_count || resultReady);
  const pricesReady = completedPercent === 100 || Boolean(job?.status === "completed" && resultReady);

  const loadJob = useCallback(async (jobId: string, signal: AbortSignal) => {
    const response = await fetch(`${API_BASE}/api/quote-relay/jobs/${encodeURIComponent(jobId)}`, {
      cache: "no-store",
      signal,
    });
    if (!response.ok) throw new Error("暂时无法获取报价状态，请稍后重试。");
    const payload = await response.json() as RelayJob;
    if (signal.aborted) return payload;
    setJob(payload);
    if (["completed", "partial"].includes(payload.status) && payload.quick_quote_result) setResultOpen(true);
    setPageError("");
    if (["completed", "partial", "failed", "cancelled"].includes(payload.status)) {
      window.sessionStorage.removeItem(ACTIVE_JOB_KEY);
      setTrackedJobId("");
    }
    return payload;
  }, []);

  useEffect(() => {
    let stopped = false;
    async function updateHealth() {
      try {
        const response = await fetch(`${API_BASE}/api/quote-relay/health`, { cache: "no-store" });
        const payload = await response.json() as RelayHealth;
        if (!stopped) setHealth(response.ok ? payload : { status: "offline" });
      } catch {
        if (!stopped) setHealth({ status: "offline" });
      }
    }
    void updateHealth();
    const timer = window.setInterval(updateHealth, 15000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    let stopped = false;
    async function loadRegions() {
      try {
        const response = await fetch(
          `${API_BASE}/api/quote-relay/providers/${cloudProvider}/regions`,
          { cache: "no-store" },
        );
        if (!response.ok) throw new Error();
        const payload = await response.json() as RegionCatalog;
        if (!stopped) {
          setRegionCatalog(payload);
          const allowed = new Set(payload.pricing_scenarios.map((scenario) => scenario.key));
          setSelectedScenarios((current) => {
            const kept = new Set([...current].filter((scenario) => allowed.has(scenario)));
            if (kept.size > 0) return kept;
            const fallback = allowed.has("on_demand")
              ? "on_demand"
              : payload.pricing_scenarios[0]?.key;
            return fallback ? new Set<ScenarioKey>([fallback]) : new Set<ScenarioKey>();
          });
        }
      } catch {
        if (!stopped) {
          setRegionCatalog(null);
          setPageError("暂时无法读取所选云厂商的地域目录，请稍后重试。");
        }
      } finally {
        if (!stopped) setRegionLoading(false);
      }
    }
    void loadRegions();
    return () => { stopped = true; };
  }, [cloudProvider]);

  useEffect(() => {
    function closePickers(event: PointerEvent) {
      if (!providerPickerRef.current?.contains(event.target as Node)) setProviderOpen(false);
      if (!regionPickerRef.current?.contains(event.target as Node)) setRegionOpen(false);
    }
    document.addEventListener("pointerdown", closePickers);
    return () => document.removeEventListener("pointerdown", closePickers);
  }, []);

  useEffect(() => {
    const savedJobId = window.sessionStorage.getItem(ACTIVE_JOB_KEY);
    if (!savedJobId) return;
    const timer = window.setTimeout(() => setTrackedJobId(savedJobId), 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!trackedJobId) return;
    let stopped = false;
    let inFlight = false;
    let requestController: AbortController | null = null;
    async function poll() {
      if (stopped || inFlight) return;
      inFlight = true;
      requestController = new AbortController();
      const controller = requestController;
      const timeout = window.setTimeout(() => controller.abort(), 12000);
      try {
        await loadJob(trackedJobId, controller.signal);
      } catch {
        if (!stopped) setPageError("报价状态正在同步，请稍候。");
      } finally {
        window.clearTimeout(timeout);
        inFlight = false;
      }
    }
    void poll();
    const timer = window.setInterval(poll, 3000);
    return () => {
      stopped = true;
      requestController?.abort();
      window.clearInterval(timer);
    };
  }, [trackedJobId, loadJob]);

  useEffect(() => {
    if (!resultOpen) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setResultOpen(false);
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [resultOpen]);

  const workflowLabel = useMemo(
    () => `已选 ${selectedScenarios.size} 种报价方案`,
    [selectedScenarios],
  );
  const providerScenarios = regionCatalog?.pricing_scenarios ?? [];
  const filteredRegions = useMemo(() => {
    const query = preferredRegion.trim().toLocaleLowerCase();
    const regions = regionCatalog?.regions ?? [];
    if (!query || regions.some((region) => region.code.toLocaleLowerCase() === query)) {
      return regions;
    }
    return regions.filter((region) => `${region.label} ${region.code}`.toLocaleLowerCase().includes(query));
  }, [preferredRegion, regionCatalog]);

  function toggleScenario(scenario: ScenarioKey) {
    setSelectedScenarios((current) => {
      const next = new Set(current);
      if (next.has(scenario)) next.delete(scenario);
      else next.add(scenario);
      return next;
    });
  }

  function chooseProvider(provider: CloudProvider) {
    if (provider === cloudProvider) {
      setProviderOpen(false);
      return;
    }
    setRegionLoading(true);
    setRegionCatalog(null);
    setPreferredRegion("");
    setRegionOpen(false);
    setCloudProvider(provider);
    setProviderOpen(false);
    setSelectedScenarios(new Set<ScenarioKey>(["on_demand"]));
    setPageError("");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting || active || providerScenarios.length < 1 || selectedScenarios.size < 1 || requirement.trim().length < 3) return;
    const numberedLineError = validateNumberedComponentLines(requirement);
    if (numberedLineError) {
      setPageError(numberedLineError);
      return;
    }
    setSubmitting(true);
    setPageError("");
    try {
      const requestDetails = {
        customer_request: requirement.trim(),
        cloud_provider: cloudProvider,
        pricing_scenarios: providerScenarios
          .map((scenario) => scenario.key)
          .filter((scenario) => selectedScenarios.has(scenario)),
        utilization_percent: utilization,
        preferred_region: preferredRegion,
        preferred_engine: "chatgpt",
      };
      const fingerprint = await submissionFingerprint(requestDetails);
      let pending: { fingerprint?: string; client_request_id?: string } = {};
      try {
        pending = JSON.parse(window.sessionStorage.getItem(PENDING_SUBMISSION_KEY) || "{}");
      } catch {
        pending = {};
      }
      const clientRequestId = pending.fingerprint === fingerprint && pending.client_request_id
        ? pending.client_request_id
        : crypto.randomUUID();
      window.sessionStorage.setItem(PENDING_SUBMISSION_KEY, JSON.stringify({
        fingerprint,
        client_request_id: clientRequestId,
      }));
      const body = JSON.stringify({ ...requestDetails, client_request_id: clientRequestId });
      let response: Response | null = null;
      for (let attempt = 0; attempt < 2; attempt += 1) {
        try {
          response = await fetch(`${API_BASE}/api/quote-relay/jobs`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body,
          });
          if (response.ok || response.status < 500) break;
        } catch {
          if (attempt === 1) throw new Error("network_error");
        }
      }
      if (!response) throw new Error("network_error");
      let payload: RelayJob | null = null;
      try {
        payload = await response.json() as RelayJob;
      } catch {
        throw new Error(safeSubmissionError(response.status));
      }
      if (!response.ok || !payload?.job_id) throw new Error(safeSubmissionError(response.status));
      window.sessionStorage.setItem(ACTIVE_JOB_KEY, payload.job_id);
      window.sessionStorage.removeItem(PENDING_SUBMISSION_KEY);
      setTrackedJobId(payload.job_id);
      setJob(payload);
      setRequirement("");
    } catch (error) {
      setPageError(
        error instanceof Error && error.message !== "network_error"
          ? error.message
          : "网络连接不稳定，请检查网络后重新提交。",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function cancelJob() {
    if (!active || !job) return;
    setPageError("");
    try {
      const response = await fetch(`${API_BASE}/api/quote-relay/jobs/${encodeURIComponent(job.job_id)}/cancel`, {
        method: "POST",
      });
      if (!response.ok) throw new Error();
      const payload = await response.json() as RelayJob;
      window.sessionStorage.removeItem(ACTIVE_JOB_KEY);
      setTrackedJobId("");
      setJob(payload);
    } catch {
      setPageError("暂时无法撤回，请稍后重试。");
    }
  }

  async function retryFailedComponents() {
    if (!job || !retryAvailable || retrying) return;
    setRetrying(true);
    setPageError("");
    try {
      const response = await fetch(
        `${API_BASE}/api/quote-relay/jobs/${encodeURIComponent(job.job_id)}/retry-failed`,
        { method: "POST" },
      );
      if (!response.ok) throw new Error();
      const payload = await response.json() as RelayJob;
      window.sessionStorage.setItem(ACTIVE_JOB_KEY, payload.job_id);
      setTrackedJobId(payload.job_id);
      setJob(payload);
      setResultOpen(false);
    } catch {
      setPageError("未完成组件暂时无法重试，请稍后再试。");
    } finally {
      setRetrying(false);
    }
  }

  function reset() {
    window.sessionStorage.removeItem(ACTIVE_JOB_KEY);
    window.sessionStorage.removeItem(PENDING_SUBMISSION_KEY);
    setTrackedJobId("");
    setJob(null);
    setPageError("");
    setResultOpen(false);
    setCopied("");
  }

  async function copyQuoteResult() {
    if (!job?.quick_quote_result) return;
    try {
      await navigator.clipboard.writeText(quoteCopyText(job));
      setCopied("quote");
      window.setTimeout(() => setCopied(""), 1800);
    } catch {
      setPageError("复制失败，请选中报价内容后复制。");
    }
  }

  async function copyDownloadLink() {
    if (!job?.quote_download_url) return;
    try {
      await navigator.clipboard.writeText(job.quote_download_url);
      setCopied("link");
      window.setTimeout(() => setCopied(""), 1800);
    } catch {
      setPageError("下载链接复制失败，请直接点击下载。");
    }
  }

  const ready = health?.status === "ready";
  const selectedCatalog = health?.provider_catalogs?.[cloudProvider];
  const selectedCatalogUnavailable = selectedCatalog?.available === false;

  return (
    <main className="sales-portal">
      <div className="sales-ambient" aria-hidden="true">
        <i className="sales-ambient-orb sales-ambient-orb-one" />
        <i className="sales-ambient-orb sales-ambient-orb-two" />
        <i className="sales-ambient-line sales-ambient-line-one" />
        <i className="sales-ambient-line sales-ambient-line-two" />
      </div>
      <header className="sales-portal-header">
        <a href="/sales" className="sales-portal-brand" aria-label="AstraQuote 云成本报价">
          <span className="sales-brand-mark" aria-hidden="true"><i>A</i></span>
          <div><strong>AstraQuote</strong><small>Multi-cloud pricing workspace</small></div>
        </a>
        <div className={`sales-portal-health ${ready ? "ready" : "waiting"}`}>
          <i aria-hidden="true" />
          <span>{ready ? "服务正常" : health ? "服务维护中" : "状态检测中"}</span>
        </div>
      </header>

      {!job ? (
        <section className="sales-quote-workspace">
          <form className="sales-quote-form" onSubmit={submit}>
            <fieldset className="sales-pricing-mode sales-provider-section">
              <div className="sales-provider-heading">
                <legend>选择云厂商</legend>
                <span>01 / 04</span>
              </div>
              <div
                className="sales-provider-select-wrap"
                ref={providerPickerRef}
                onMouseLeave={() => setProviderOpen(false)}
              >
                <button
                  className="sales-provider-trigger"
                  type="button"
                  aria-controls="sales-provider-options"
                  aria-expanded={providerOpen}
                  aria-label={providerOpen ? "收起云厂商" : "展开云厂商"}
                  onClick={() => setProviderOpen((current) => !current)}
                >
                  <b className="sales-provider-mark" aria-hidden="true">{PROVIDER_META[cloudProvider].mark}</b>
                  <span><strong>{PROVIDER_META[cloudProvider].label}</strong><small>{PROVIDER_META[cloudProvider].detail}</small></span>
                  <i aria-hidden="true">⌄</i>
                </button>
                {providerOpen && <div className="sales-choice-row sales-provider-row" id="sales-provider-options" role="radiogroup">
                  {PROVIDER_ORDER.map((value) => {
                    const catalog = health?.provider_catalogs?.[value];
                    const provider = PROVIDER_META[value];
                    return (
                      <label
                        className={`${cloudProvider === value ? "selected" : ""} ${catalog?.available === false ? "unavailable" : ""}`}
                        key={value}
                      >
                        <input
                          type="radio"
                          name="cloud-provider"
                          value={value}
                          checked={cloudProvider === value}
                          onChange={() => chooseProvider(value)}
                        />
                        <b className="sales-provider-mark" aria-hidden="true">{provider.mark}</b>
                        <span><strong>{provider.label}</strong><small>{provider.detail}</small></span>
                        <i className="sales-choice-indicator" aria-hidden="true" />
                        {catalog?.available === false && <em>待配置</em>}
                      </label>
                    );
                  })}
                </div>}
              </div>
            </fieldset>

            {selectedCatalogUnavailable && (
              <p className="sales-catalog-note" role="status">
                {selectedCatalog?.message ?? "所选云厂商的官方价格接口待配置。"}
              </p>
            )}

            <section className="sales-region-section">
              <div className="sales-section-heading">
                <div>
                  <label htmlFor="sales-region">选择首选地域</label>
                  <p>{regionCatalog?.site_label ?? "正在读取账号站点"} · 地域代码按当前云厂商解释，最终可购性以每个产品的官方响应为准</p>
                </div>
                <span>02 / 04</span>
              </div>
              <div className="sales-region-select-wrap" ref={regionPickerRef}>
                <input
                  id="sales-region"
                  value={preferredRegion}
                  readOnly
                  onFocus={() => setRegionOpen(true)}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") setRegionOpen(false);
                    if (event.key === "ArrowDown") setRegionOpen(true);
                  }}
                  disabled={regionLoading}
                  placeholder={regionLoading ? "正在加载官方地域…" : "请选择官方地域"}
                  role="combobox"
                  aria-autocomplete="none"
                  aria-expanded={regionOpen}
                  aria-controls="sales-region-options"
                  required
                />
                <button
                  className="sales-region-toggle"
                  type="button"
                  aria-label={regionOpen ? "收起地域" : "展开地域"}
                  aria-expanded={regionOpen}
                  onClick={() => setRegionOpen((current) => !current)}
                  disabled={regionLoading}
                >⌄</button>
                {regionOpen && !regionLoading && (
                  <div className="sales-region-options-panel" id="sales-region-options" role="listbox">
                    {filteredRegions.length > 0 ? filteredRegions.map((item) => (
                      <button
                        type="button"
                        role="option"
                        aria-selected={preferredRegion === item.code}
                        className={preferredRegion === item.code ? "selected" : ""}
                        key={item.code}
                        onClick={() => {
                          setPreferredRegion(item.code);
                          setRegionOpen(false);
                        }}
                      >
                        <strong>{item.label}</strong>
                        <small>{item.code}</small>
                      </button>
                    )) : (
                      <p>当前云厂商暂无可选官方地域，请联系管理员更新地域目录。</p>
                    )}
                  </div>
                )}
              </div>
            </section>

            <div className="sales-form-grid">
              <section className="sales-requirement-panel">
                <div className="sales-section-heading">
                  <div><label htmlFor="sales-requirement">填写客户需求</label><p>规格、数量、存储及流量</p></div>
                  <span>03 / 04</span>
                </div>
                <div className="sales-textarea-shell">
                  <textarea
                    id="sales-requirement"
                    value={requirement}
                    maxLength={12000}
                    onChange={(event) => {
                      setRequirement(event.target.value);
                      if (pageError) setPageError("");
                    }}
                    placeholder={"1. Linux 云服务器 1 台，2 核 4GB\n2. 数据库 1 套，4 核 16GB"}
                  />
                  <div className="sales-field-foot">
                    <span>每行一个组件，序号必须连续；每个对话最多 10 个，按 5 个一轮处理</span>
                    <b>{requirement.length.toLocaleString()} / 12,000</b>
                  </div>
                </div>
              </section>

              <aside className="sales-options-panel">
                <div className="sales-section-heading">
                  <div><strong>设置报价方案</strong><p>采用所选云厂商的计价方式</p></div>
                  <span>04 / 04</span>
                </div>
                <fieldset className="sales-pricing-mode sales-scenario-list">
                  <legend className="sales-visually-hidden">报价方案</legend>
                  <div className="sales-choice-row">
                    {providerScenarios.map(({ key, label }) => (
                      <label className={selectedScenarios.has(key) ? "selected" : ""} key={key}>
                        <input
                          type="checkbox"
                          name="pricing-scenario"
                          value={key}
                          checked={selectedScenarios.has(key)}
                          onChange={() => toggleScenario(key)}
                        />
                        <i aria-hidden="true" />
                        <span>{label}</span>
                      </label>
                    ))}
                  </div>
                </fieldset>

                <div className="sales-utilization-row">
                  <div><label htmlFor="sales-utilization">预计使用率</label><small>{workflowLabel}</small></div>
                  <div className="sales-utilization-control">
                    <input id="sales-utilization" type="number" min={1} max={100} value={utilization} onChange={(event) => setUtilization(Math.min(100, Math.max(1, Number(event.target.value) || 100)))} />
                    <span>%</span>
                  </div>
                </div>

                <div className="sales-commercial-note">
                  <i aria-hidden="true">✓</i>
                  <span><strong>商业价格口径</strong><small>不抵扣免费额度、试用额度或账户赠送额度</small></span>
                </div>
              </aside>
            </div>

            {pageError && <p className="sales-form-error" role="alert">{pageError}</p>}
            <div className="sales-form-submit-row">
              <span><i aria-hidden="true" /> 数据来自所选云厂商官方价格目录</span>
              <button className="sales-button sales-button-primary sales-submit" type="submit" disabled={submitting || regionLoading || providerScenarios.length < 1 || !preferredRegion || selectedScenarios.size < 1 || requirement.trim().length < 3 || health?.status === "offline" || selectedCatalogUnavailable}>
                {submitting ? "正在提交…" : "提交报价"}
                <i aria-hidden="true">→</i>
              </button>
            </div>
          </form>
        </section>
      ) : (
        <section className={`sales-job-card status-${job.status}`} aria-live="polite">
          <div className="sales-job-visual" aria-hidden="true">
            <span className="sales-job-orbit sales-job-orbit-one" />
            <span className="sales-job-orbit sales-job-orbit-two" />
            <span className="sales-job-node sales-job-node-one" />
            <span className="sales-job-node sales-job-node-two" />
            <div className="sales-job-status-icon">
              {["completed", "partial"].includes(job.status) ? "✓" : job.status === "failed" ? "!" : job.status === "cancelled" ? "×" : <i />}
            </div>
          </div>
          <div className="sales-job-copy">
            <p>LIVE QUOTE WORKFLOW · {providerLabel(job.cloud_provider)} · ChatGPT</p>
            <h1>{statusCopy[job.status].title}</h1>
            <span>{["completed", "partial"].includes(job.status) && job.quick_quote_result
              ? job.status === "partial"
                ? "已生成部分报价和 Excel，成功组件可立即使用，未报价组件没有计入合计。"
                : "报价结果和 Excel 已生成，可查看、复制或下载。"
              : job.status === "queued"
                ? queuedStatusDetail(job)
              : job.status === "processing"
                ? processingStatusDetail(job)
                : statusCopy[job.status].detail ?? `预计 ${estimateWindow()}完成，结果将在当前页面显示。`}</span>
            {job.status === "failed" && (
              <small className="sales-failure-reference">
                错误码 {job.failure_code || "AQ-QUOTE-FAILED"}
              </small>
            )}
          </div>

          {active && (
            <div className="sales-job-progress" aria-label="报价处理状态">
              <div><span>{job.status === "queued" ? "等待启动" : job.status === "needs_login" ? "等待服务恢复" : "组件核价进度"}</span><b>{job.status === "processing" && <i />} {job.status === "queued" ? "排队中" : job.status === "needs_login" ? "等待管理员" : "正在运行"}</b></div>
              {completedPercent !== null && <i role="progressbar" aria-label="已完成核价的组件比例" aria-valuemin={0} aria-valuemax={100} aria-valuenow={completedPercent}><span style={{ width: `${completedPercent}%` }} /></i>}
              <small>{job.status === "queued" ? queuedStatusDetail(job) : job.status === "needs_login" ? "请联系管理员恢复服务，恢复后将继续处理本次报价。" : processingStatusDetail(job)}</small>
            </div>
          )}

          <div className="sales-job-stages" aria-hidden="true">
            <span className={inventoryReady ? "complete" : job.status === "processing" ? "current" : ""}><i />需求识别</span>
            <b />
            <span className={pricesReady ? "complete" : job.status === "processing" && inventoryReady ? "current" : ""}><i />官方核价</span>
            <b />
            <span className={["completed", "partial"].includes(job.status) && resultReady ? "complete" : job.status === "processing" && pricesReady ? "current" : ""}><i />生成结果</span>
          </div>

          <div className="sales-job-actions">
            {active
              ? <>{job.quick_quote_result && <button type="button" className="sales-button sales-button-secondary" onClick={() => setResultOpen(true)}>查看已保存的部分报价</button>}<button type="button" className="sales-button sales-button-ghost" onClick={() => void cancelJob()}>撤回报价</button></>
              : job.status === "partial" && job.quick_quote_result
                ? <><button type="button" className="sales-button sales-button-primary" onClick={() => setResultOpen(true)}>查看部分报价 <i aria-hidden="true">→</i></button>{retryAvailable && <button type="button" className="sales-button sales-button-secondary" disabled={retrying} onClick={() => void retryFailedComponents()}>{retrying ? "正在提交重试…" : "只重试未完成组件"}</button>}<button type="button" className="sales-button sales-button-ghost" onClick={reset}>新建报价</button></>
              : job.quick_quote_result
                ? <><button type="button" className="sales-button sales-button-primary" onClick={() => setResultOpen(true)}>查看报价结果 <i aria-hidden="true">→</i></button><button type="button" className="sales-button sales-button-secondary" onClick={reset}>新建报价</button></>
                : <button type="button" className="sales-button sales-button-primary" onClick={reset}>新建报价 <i aria-hidden="true">→</i></button>}
          </div>
          {pageError && <p className="sales-form-error" role="alert">{pageError}</p>}
        </section>
      )}

      {resultOpen && job?.quick_quote_result && (
        <div className="sales-result-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setResultOpen(false);
        }}>
          <section className="sales-result-dialog" role="dialog" aria-modal="true" aria-labelledby="sales-result-title">
            <header>
              <div className="sales-result-title">
                <span className="sales-result-status" aria-hidden="true">✓</span>
                <div><small>{providerLabel(job.cloud_provider)} · {job.quick_quote_result.region}</small><h2 id="sales-result-title">{job.quick_quote_result.is_partial ? "部分报价已生成" : "报价已生成"}</h2><p>{job.quick_quote_result.is_partial ? "成功组件已核价；未完成组件未计入合计" : "官方价格已核对，Excel 文件已就绪"}</p></div>
              </div>
              <button className="sales-result-close" type="button" aria-label="关闭报价结果" onClick={() => setResultOpen(false)}>×</button>
            </header>
            <div className="sales-result-layout">
              <section className="sales-result-summary">
                <div className="sales-result-summary-heading"><small>QUOTE SUMMARY</small><h3>{job.quick_quote_result.is_partial ? "已核价部分小计" : "报价合计"}</h3></div>
                <div className="sales-result-totals">
                  {job.quick_quote_result.scenarios.map((scenario) => (
                    <div key={scenario.scenario_key}>
                      <span>{scenario.label}</span>
                      <strong>{money(scenario.monthly_total, job.quick_quote_result!.currency)}</strong>
                      <small>{scenarioMonthlyCaption(scenario)}</small>
                      {Number(scenario.upfront_total || 0) > 0 && <em>预付总额 {money(scenario.upfront_total, job.quick_quote_result!.currency)}</em>}
                    </div>
                  ))}
                </div>
                <div className="sales-result-file"><i aria-hidden="true">X</i><span><strong>Excel 报价文件</strong><small>{job.quote_download_filename || "正式报价单.xlsx"}</small></span><b aria-hidden="true">✓</b></div>
                <p className="sales-result-commercial"><i aria-hidden="true" /> 正常商业价格，不抵扣免费或试用额度</p>
                {job.quick_quote_result.preferred_region && job.quick_quote_result.preferred_region !== job.quick_quote_result.region && (
                  <p className="sales-result-region-note">
                    首选地域 {job.quick_quote_result.preferred_region} → 实际地域 {job.quick_quote_result.region}<br />
                    {job.quick_quote_result.region_adjustment_reason || "首选地域不能承载整套产品，已选择同站点最近可用地域。"}
                  </p>
                )}
              </section>
              <div className="sales-result-body">
                <div className="sales-result-section-label"><span>服务明细</span><b>{job.quick_quote_result.components.length} 项</b></div>
                <div className="sales-result-table-wrap">
                  <table className="sales-result-table">
                    <thead>
                      <tr>
                        <th>服务</th>
                        <th>型号 / 数量</th>
                        <th>配置</th>
                        {job.quick_quote_result.scenarios.map((scenario) => (
                          <th key={scenario.scenario_key}>{scenario.label}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {job.quick_quote_result.components.map((component, index) => (
                        <tr key={`${component.service_name}-${index}`} style={{ animationDelay: `${index * 35}ms` }}>
                          <td>
                            <span className="sales-result-row-index">{String(index + 1).padStart(2, "0")}</span>
                            <strong>{component.service_name}</strong>
                          </td>
                          <td><strong>{component.model_or_plan || "—"}</strong><small>{component.quantity || "—"}</small></td>
                          <td>
                            {component.configuration_summary
                              ? <details className="sales-result-config"><summary>{component.configuration_summary}</summary><p>{component.configuration_summary}</p></details>
                              : <span className="sales-result-empty">—</span>}
                          </td>
                          {job.quick_quote_result!.scenarios.map((scenario) => {
                            const cost = component.scenario_costs.find((item) => item.scenario_key === scenario.scenario_key);
                            return (
                              <td className="sales-result-price" key={scenario.scenario_key}>
                                {cost
                                  ? <><strong>{money(cost.monthly_cost, job.quick_quote_result!.currency)}</strong><small>{scenarioMonthlyCaption(scenario)}</small>{Number(cost.upfront_cost || 0) > 0 && <em>预付 {money(cost.upfront_cost, job.quick_quote_result!.currency)}</em>}</>
                                  : <span className="sales-result-empty">—</span>}
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {job.quick_quote_result.pricing_notice && (
                  <div className="sales-result-notice" role="status">
                    {job.quick_quote_result.pricing_notice}
                  </div>
                )}
                {(job.quick_quote_result.unpriced_components || []).length > 0 && (
                  <div className="sales-result-unpriced" role="status">
                    <strong>以下 {(job.quick_quote_result.unpriced_components || []).length} 个组件尚未报价，未计入合计</strong>
                    {(job.quick_quote_result.unpriced_components || []).map((component, index) => (
                      <p key={`${component.service_name}-${index}`}>
                        <b>{component.service_name}</b>
                        <span>{[component.model_or_plan, component.quantity, component.configuration_summary].filter(Boolean).join(" · ")}<small>{unpricedRecoveryText(component)}</small></span>
                      </p>
                    ))}
                  </div>
                )}
              </div>
            </div>
            <footer className="sales-result-actions">
              <span className="sales-result-action-note">{job.quick_quote_result.is_partial ? "这是部分报价，发送客户前请确认未完成项" : "报价和文件均可直接发送给客户"}{pageError && <small role="alert">{pageError}</small>}</span>
              <div>
                {retryAvailable && <button type="button" className="sales-button sales-button-primary" disabled={retrying} onClick={() => void retryFailedComponents()}>{retrying ? "正在提交重试…" : "只重试未完成组件"}</button>}
                <button type="button" className="sales-button sales-button-ghost" onClick={() => void copyDownloadLink()} disabled={!job.quote_download_url}><i aria-hidden="true">↗</i>{copied === "link" ? "链接已复制" : "复制下载链接"}</button>
                {job.quote_download_url && <a className="sales-button sales-button-secondary sales-download" href={job.quote_download_url} download={job.quote_download_filename || undefined}><i aria-hidden="true">↓</i>下载 Excel</a>}
                <button type="button" className="sales-button sales-button-primary" onClick={() => void copyQuoteResult()}><i aria-hidden="true">□</i>{copied === "quote" ? "报价已复制" : "复制报价"}</button>
              </div>
            </footer>
          </section>
        </div>
      )}
    </main>
  );
}
