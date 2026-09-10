"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/backend";
const ACTIVE_JOB_KEY = "astraquote.sales.active-job.v1";
const PENDING_SUBMISSION_KEY = "astraquote.sales.pending-submission.v1";

type ScenarioKey = "on_demand" | "one_year_commitment" | "three_year_commitment";
type CloudProvider = "aws" | "azure" | "oci" | "gcp";

type PageScenarioCost = {
  scenario_key: ScenarioKey;
  label: string;
  monthly_cost?: string;
  upfront_cost?: string;
  monthly_total?: string;
  upfront_total?: string;
};

type QuickQuoteResult = {
  schema_version: "astraquote-page-result/1";
  currency: string;
  region: string;
  components: Array<{
    service_name: string;
    model_or_plan?: string;
    quantity?: string;
    configuration_summary?: string;
    scenario_costs: PageScenarioCost[];
  }>;
  scenarios: PageScenarioCost[];
};

type RelayJob = {
  job_id: string;
  submission_code: string;
  status: "queued" | "processing" | "needs_login" | "completed" | "failed" | "cancelled";
  created_at?: string;
  updated_at?: string;
  cloud_provider?: CloudProvider;
  display_result_on_page?: boolean;
  quick_quote_result?: QuickQuoteResult | null;
  quote_download_url?: string | null;
  quote_download_filename?: string | null;
};

type RelayHealth = {
  status: "ready" | "offline";
  message?: string;
};

const statusCopy: Record<RelayJob["status"], { title: string; detail?: string }> = {
  queued: { title: "报价申请已提交" },
  processing: { title: "报价申请已提交" },
  needs_login: { title: "报价申请已提交" },
  completed: { title: "报价已完成", detail: "报价结果和 Excel 已生成。" },
  failed: { title: "报价未完成", detail: "请联系管理员处理。" },
  cancelled: { title: "报价已撤回", detail: "本次报价已停止处理。" },
};

const PROVIDER_SCENARIOS: Record<CloudProvider, Array<{ key: ScenarioKey; label: string }>> = {
  aws: [
    { key: "on_demand", label: "按需付费" },
    { key: "one_year_commitment", label: "1 年预留实例全预付" },
    { key: "three_year_commitment", label: "3 年预留实例全预付" },
  ],
  azure: [
    { key: "on_demand", label: "即用即付" },
    { key: "one_year_commitment", label: "1 年预留" },
    { key: "three_year_commitment", label: "3 年预留" },
  ],
  oci: [{ key: "on_demand", label: "OCI 公开按量价" }],
  gcp: [
    { key: "on_demand", label: "按需付费" },
    { key: "one_year_commitment", label: "1 年承诺使用" },
    { key: "three_year_commitment", label: "3 年承诺使用" },
  ],
};

function approximateProgress(job: RelayJob) {
  if (job.status === "completed") return 100;
  if (["failed", "cancelled"].includes(job.status)) return 0;
  const createdAt = job.created_at ? new Date(job.created_at).valueOf() : Date.now();
  const elapsedSeconds = Math.max(0, (Date.now() - createdAt) / 1000);
  return Math.min(92, Math.round(14 + (elapsedSeconds / (10 * 60)) * 78));
}

function estimateWindow() {
  return "5～10 分钟";
}

function money(value: string | undefined, currency = "USD") {
  const amount = Number(value ?? 0);
  const formatted = Number.isFinite(amount)
    ? amount.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : "0.00";
  return `${formatted} ${currency}`;
}

function quoteCopyText(job: RelayJob) {
  const result = job.quick_quote_result;
  if (!result) return "";
  const lines = [`提交码：${job.submission_code}`, `区域：${result.region}`, ""];
  result.components.forEach((component, index) => {
    const identity = [component.service_name, component.model_or_plan, component.quantity]
      .filter(Boolean).join(" · ");
    lines.push(`${index + 1}. ${identity}`);
    if (component.configuration_summary) lines.push(`配置：${component.configuration_summary}`);
    component.scenario_costs.forEach((scenario) => {
      const upfront = Number(scenario.upfront_cost || 0) > 0
        ? `；预付总额 ${money(scenario.upfront_cost, result.currency)}`
        : "";
      lines.push(`${scenario.label}：折合月费 ${money(scenario.monthly_cost, result.currency)}${upfront}`);
    });
    lines.push("");
  });
  lines.push("报价合计");
  result.scenarios.forEach((scenario) => {
    const upfront = Number(scenario.upfront_total || 0) > 0
      ? `；预付总额 ${money(scenario.upfront_total, result.currency)}`
      : "";
    lines.push(`${scenario.label}：折合月费 ${money(scenario.monthly_total, result.currency)}${upfront}`);
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
    () => new Set<ScenarioKey>(PROVIDER_SCENARIOS.aws.map((scenario) => scenario.key)),
  );
  const [utilization, setUtilization] = useState(100);
  const [cloudProvider, setCloudProvider] = useState<CloudProvider>("aws");
  const [health, setHealth] = useState<RelayHealth | null>(null);
  const [job, setJob] = useState<RelayJob | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [pageError, setPageError] = useState("");
  const [trackedJobId, setTrackedJobId] = useState("");
  const [resultOpen, setResultOpen] = useState(false);
  const [copied, setCopied] = useState<"" | "quote" | "link">("");
  const [, refreshProgress] = useState(0);

  const active = Boolean(job && ["queued", "processing", "needs_login"].includes(job.status));

  const loadJob = useCallback(async (jobId: string) => {
    const response = await fetch(`${API_BASE}/api/quote-relay/jobs/${encodeURIComponent(jobId)}`, {
      cache: "no-store",
    });
    if (!response.ok) throw new Error("暂时无法获取报价状态，请稍后重试。");
    const payload = await response.json() as RelayJob;
    setJob(payload);
    if (payload.status === "completed" && payload.quick_quote_result) setResultOpen(true);
    setPageError("");
    if (["completed", "failed", "cancelled"].includes(payload.status)) {
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
    const savedJobId = window.sessionStorage.getItem(ACTIVE_JOB_KEY);
    if (!savedJobId) return;
    const timer = window.setTimeout(() => setTrackedJobId(savedJobId), 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!trackedJobId) return;
    let stopped = false;
    async function poll() {
      try {
        await loadJob(trackedJobId);
      } catch {
        if (!stopped) setPageError("报价状态正在同步，请稍候。");
      }
    }
    void poll();
    const timer = window.setInterval(poll, 3000);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [trackedJobId, loadJob]);

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => refreshProgress((value) => value + 1), 15000);
    return () => window.clearInterval(timer);
  }, [active]);

  const workflowLabel = useMemo(
    () => `已选 ${selectedScenarios.size} 种报价方案`,
    [selectedScenarios],
  );

  function toggleScenario(scenario: ScenarioKey) {
    setSelectedScenarios((current) => {
      const next = new Set(current);
      if (next.has(scenario)) next.delete(scenario);
      else next.add(scenario);
      return next;
    });
  }

  function chooseProvider(provider: CloudProvider) {
    setCloudProvider(provider);
    setSelectedScenarios(new Set(PROVIDER_SCENARIOS[provider].map((scenario) => scenario.key)));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting || active || selectedScenarios.size < 1 || requirement.trim().length < 3) return;
    setSubmitting(true);
    setPageError("");
    try {
      const requestDetails = {
        customer_request: requirement.trim(),
        cloud_provider: cloudProvider,
        pricing_scenarios: PROVIDER_SCENARIOS[cloudProvider]
          .map((scenario) => scenario.key)
          .filter((scenario) => selectedScenarios.has(scenario)),
        utilization_percent: utilization,
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
      const payload = await response.json() as RelayJob;
      if (!response.ok || !payload.job_id) throw new Error("报价提交失败，请稍后重试。");
      window.sessionStorage.setItem(ACTIVE_JOB_KEY, payload.job_id);
      window.sessionStorage.removeItem(PENDING_SUBMISSION_KEY);
      setTrackedJobId(payload.job_id);
      setJob(payload);
      setRequirement("");
    } catch {
      setPageError("报价提交失败，请稍后重试。");
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
  const progress = job ? approximateProgress(job) : 0;

  return (
    <main className="sales-portal">
      <header className="sales-portal-header">
        <a href="/sales" className="sales-portal-brand" aria-label="AstraQuote 云成本报价">
          <span aria-hidden="true">A</span>
          <div><strong>AstraQuote</strong><small>云成本报价</small></div>
        </a>
        <div className={`sales-portal-health ${ready ? "ready" : "waiting"}`}>
          <i aria-hidden="true" />
          <span>{ready ? "服务正常" : health ? "服务维护中" : "状态检测中"}</span>
        </div>
      </header>

      {!job ? (
        <section className="sales-quote-workspace">
          <form className="sales-quote-form" onSubmit={submit}>
            <div className="sales-form-heading">
              <p>MULTI-CLOUD PRICING</p>
              <h1>新建报价</h1>
            </div>

            <fieldset className="sales-pricing-mode">
              <legend>云厂商</legend>
              <div className="sales-choice-row sales-provider-row">
                {([
                  ["aws", "AWS"],
                  ["azure", "微软 Azure"],
                  ["oci", "Oracle Cloud"],
                  ["gcp", "Google Cloud"],
                ] as const).map(([value, label]) => (
                  <label className={cloudProvider === value ? "selected" : ""} key={value}>
                    <input
                      type="radio"
                      name="cloud-provider"
                      value={value}
                      checked={cloudProvider === value}
                      onChange={() => chooseProvider(value)}
                    />
                    <span>{label}</span>
                  </label>
                ))}
              </div>
            </fieldset>

            <label htmlFor="sales-requirement">需求内容</label>
            <textarea
              id="sales-requirement"
              value={requirement}
              maxLength={12000}
              onChange={(event) => setRequirement(event.target.value)}
              placeholder="区域、服务、数量、规格、存储、流量及购买方式"
            />
            <div className="sales-field-foot"><b>{requirement.length.toLocaleString()} / 12,000</b></div>

            <fieldset className="sales-pricing-mode">
              <legend>报价方案</legend>
              <div className="sales-choice-row">
                {PROVIDER_SCENARIOS[cloudProvider].map(({ key, label }) => (
                  <label className={selectedScenarios.has(key) ? "selected" : ""} key={key}>
                    <input
                      type="checkbox"
                      name="pricing-scenario"
                      value={key}
                      checked={selectedScenarios.has(key)}
                      onChange={() => toggleScenario(key)}
                    />
                    <span>{label}</span>
                  </label>
                ))}
              </div>
            </fieldset>

            <div className="sales-utilization-row">
              <label htmlFor="sales-utilization">预计使用率</label>
              <input id="sales-utilization" type="number" min={1} max={100} value={utilization} onChange={(event) => setUtilization(Math.min(100, Math.max(1, Number(event.target.value) || 100)))} />
              <span>%</span>
              <small>{workflowLabel}</small>
            </div>

            {pageError && <p className="sales-form-error" role="alert">{pageError}</p>}
            <button className="sales-submit" type="submit" disabled={submitting || selectedScenarios.size < 1 || requirement.trim().length < 3 || health?.status === "offline"}>
              {submitting ? "正在提交…" : "提交报价"}
              <span aria-hidden="true">↗</span>
            </button>
          </form>
        </section>
      ) : (
        <section className={`sales-job-card status-${job.status}`} aria-live="polite">
          <div className="sales-job-status-icon" aria-hidden="true">
            {job.status === "completed" ? "✓" : job.status === "failed" ? "!" : job.status === "cancelled" ? "×" : <i />}
          </div>
          <div className="sales-job-copy">
            <p>提交码</p>
            <strong className="sales-submission-code">{job.submission_code}</strong>
            <h1>{statusCopy[job.status].title}</h1>
            <span>{job.status === "completed" && job.quick_quote_result
              ? "报价结果和 Excel 已生成，可查看、复制或下载。"
              : statusCopy[job.status].detail ?? `预计 ${estimateWindow()}完成，结果将在当前页面显示。`}</span>
          </div>

          {active && (
            <div className="sales-job-progress" aria-label={`处理进度约 ${progress}%`}>
              <div><span>处理中</span><b>{progress}%</b></div>
              <i><span style={{ width: `${progress}%` }} /></i>
              <small>预计 {estimateWindow()}</small>
            </div>
          )}

          <div className="sales-job-actions">
            {active
              ? <button type="button" className="sales-secondary" onClick={() => void cancelJob()}>撤回报价</button>
              : job.quick_quote_result
                ? <><button type="button" className="sales-submit" onClick={() => setResultOpen(true)}>查看报价结果</button><button type="button" className="sales-secondary" onClick={reset}>新建报价</button></>
                : <button type="button" className="sales-submit" onClick={reset}>新建报价 <span aria-hidden="true">↗</span></button>}
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
              <div><small>提交码 {job.submission_code}</small><h2 id="sales-result-title">报价结果</h2></div>
              <button type="button" aria-label="关闭报价结果" onClick={() => setResultOpen(false)}>×</button>
            </header>
            <div className="sales-result-body">
              {job.quick_quote_result.components.map((component, index) => (
                <article key={`${component.service_name}-${index}`}>
                  <div><b>{index + 1}</b><strong>{component.service_name}</strong><span>{[component.model_or_plan, component.quantity].filter(Boolean).join(" · ")}</span></div>
                  {component.configuration_summary && <p>{component.configuration_summary}</p>}
                  <dl>{component.scenario_costs.map((scenario) => (
                    <div key={scenario.scenario_key}>
                      <dt>{scenario.label}</dt>
                      <dd>{money(scenario.monthly_cost, job.quick_quote_result.currency)} / 月{Number(scenario.upfront_cost || 0) > 0 && <small>预付 {money(scenario.upfront_cost, job.quick_quote_result.currency)}</small>}</dd>
                    </div>
                  ))}</dl>
                </article>
              ))}
              <section className="sales-result-totals">
                <h3>报价合计</h3>
                {job.quick_quote_result.scenarios.map((scenario) => (
                  <div key={scenario.scenario_key}>
                    <span>{scenario.label}</span>
                    <strong>{money(scenario.monthly_total, job.quick_quote_result.currency)} / 月</strong>
                    {Number(scenario.upfront_total || 0) > 0 && <small>预付总额 {money(scenario.upfront_total, job.quick_quote_result.currency)}</small>}
                  </div>
                ))}
              </section>
            </div>
            <footer>
              <button type="button" className="sales-secondary" onClick={() => setResultOpen(false)}>关闭</button>
              <button type="button" className="sales-secondary" onClick={() => void copyDownloadLink()} disabled={!job.quote_download_url}>{copied === "link" ? "链接已复制" : "复制下载链接"}</button>
              {job.quote_download_url && <a className="sales-secondary sales-download" href={job.quote_download_url} download={job.quote_download_filename || undefined}>下载 Excel</a>}
              <button type="button" className="sales-submit" onClick={() => void copyQuoteResult()}>{copied === "quote" ? "报价已复制" : "复制报价"}</button>
            </footer>
          </section>
        </div>
      )}
    </main>
  );
}
