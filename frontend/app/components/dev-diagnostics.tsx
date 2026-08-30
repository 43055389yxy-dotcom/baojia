"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/backend";
const STORAGE_KEY = "astraquote.dev-diagnostics.v1";
const CLEAR_DIAGNOSTICS_EVENT = "astraquote:clear-diagnostics";
const MAX_CLIENT_ENTRIES = 160;

type DiagnosticEntry = {
  diagnostic_id: string;
  timestamp: string;
  level: string;
  event: string;
  message?: string | null;
  request_id?: string | null;
  context?: unknown;
  source?: "browser" | "backend";
};

type BackendDiagnosticResponse = {
  enabled?: boolean;
  environment?: string;
  provider?: string;
  entries?: DiagnosticEntry[];
};

type FetchListener = (entry: DiagnosticEntry) => void;

let fetchInstrumentationInstalled = false;
const fetchListeners = new Set<FetchListener>();

const sensitiveKeyParts = [
  "authorization",
  "cookie",
  "credential",
  "password",
  "secret",
  "session_token",
  "access_key",
  "api_key",
  "apikey",
];

function redactText(value: string) {
  return value
    .replace(/\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+/gi, "$1 [REDACTED]")
    .replace(/\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g, "[REDACTED_AWS_ACCESS_KEY]")
    .replace(/\b(?:aws|azure)_[A-Za-z0-9_-]{12,}\b/g, "[REDACTED_CONFIRMATION_TOKEN]")
    .replace(/\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b/g, "[REDACTED_JWT]")
    .replace(/\bsk-[A-Za-z0-9_-]{12,}\b/g, "[REDACTED_API_KEY]")
    .replace(
      /(authorization|api[_-]?key|access[_-]?key|session[_-]?token|secret(?:[_-]?access)?[_-]?key|password|credential|token)(\s*[:=]\s*["']?)([^"'\s&,;}]+)/gi,
      "$1$2[REDACTED]",
    );
}

function redactValue(value: unknown, depth = 0): unknown {
  if (depth > 12) return "[MAX_DEPTH_REACHED]";
  if (value === null || value === undefined || typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  if (typeof value === "string") return redactText(value);
  if (Array.isArray(value)) return value.map((item) => redactValue(item, depth + 1));
  if (typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => {
        const normalized = key.toLowerCase().replaceAll("-", "_");
        const sensitive = normalized === "token"
          || sensitiveKeyParts.some((part) => normalized.includes(part));
        return [key, sensitive ? "[REDACTED]" : redactValue(item, depth + 1)];
      }),
    );
  }
  return redactText(String(value));
}

function compactSuccessfulPayload(payload: unknown) {
  if (!payload || typeof payload !== "object") return payload;
  const item = payload as Record<string, unknown>;
  const result = item.result && typeof item.result === "object"
    ? item.result as Record<string, unknown>
    : undefined;
  return redactValue({
    job_id: item.job_id,
    status: item.status,
    updated_at: item.updated_at,
    events: item.events,
    quote_id: result?.quote_id,
    quote_status: result?.status,
    total_cost: result?.total_cost,
    is_partial: result?.is_partial,
    incomplete_component_ids: result?.incomplete_component_ids,
    notices: result?.notices,
    audit_metadata: result?.audit_metadata,
    selection_pricing: Array.isArray(result?.selections)
      ? (result.selections as Array<Record<string, unknown>>).map((selection) => ({
          component_id: selection.component_id,
          service: selection.service,
          display_name: selection.display_name,
          pricing_status: selection.pricing_status,
          pricing_issue_code: selection.pricing_issue_code,
          pricing_notice: selection.pricing_notice,
        }))
      : undefined,
  });
}

function emitBrowserEntry(entry: Omit<DiagnosticEntry, "diagnostic_id" | "timestamp" | "source">) {
  const complete: DiagnosticEntry = {
    ...entry,
    diagnostic_id: `browser_${crypto.randomUUID()}`,
    timestamp: new Date().toISOString(),
    source: "browser",
    context: redactValue(entry.context),
  };
  fetchListeners.forEach((listener) => listener(complete));
}

function installFetchInstrumentation() {
  if (fetchInstrumentationInstalled || typeof window === "undefined") return;
  fetchInstrumentationInstalled = true;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? (input instanceof Request ? input.method : "GET")).toUpperCase();
    const rawUrl = input instanceof Request ? input.url : String(input);
    const url = redactText(rawUrl);
    const isDiagnosticRequest = rawUrl.includes("/api/debug/logs");
    const startedAt = performance.now();
    try {
      const response = await nativeFetch(input, init);
      if (!isDiagnosticRequest && rawUrl.includes("/api/")) {
        let payload: unknown;
        try {
          payload = await response.clone().json();
        } catch {
          payload = undefined;
        }
        const recordCompletedJob = method === "GET"
          && rawUrl.includes("/api/quote-jobs/")
          && payload !== null
          && typeof payload === "object"
          && ["completed", "failed"].includes(String((payload as Record<string, unknown>).status));
        const shouldRecord = !response.ok || method !== "GET" || recordCompletedJob;
        if (shouldRecord) {
          const responseRequestId = response.headers.get("x-diagnostic-request-id");
          emitBrowserEntry({
            level: response.ok ? "info" : "error",
            event: response.ok ? "browser_api_response" : "browser_api_error_response",
            message: `${method} ${url} -> ${response.status}`,
            request_id: responseRequestId,
            context: {
              method,
              url,
              status_code: response.status,
              duration_ms: Math.round((performance.now() - startedAt) * 100) / 100,
              response: response.ok ? compactSuccessfulPayload(payload) : redactValue(payload),
            },
          });
        }
      }
      return response;
    } catch (error) {
      if (!isDiagnosticRequest) {
        emitBrowserEntry({
          level: "error",
          event: "browser_fetch_exception",
          message: error instanceof Error ? error.message : String(error),
          context: {
            method,
            url,
            error_type: error instanceof Error ? error.name : typeof error,
            raw_error: error instanceof Error ? error.message : String(error),
            stack: error instanceof Error ? error.stack : undefined,
            duration_ms: Math.round((performance.now() - startedAt) * 100) / 100,
          },
        });
      }
      throw error;
    }
  };
}

function loadStoredEntries(): DiagnosticEntry[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "[]") as unknown;
    return Array.isArray(parsed) ? parsed.slice(-MAX_CLIENT_ENTRIES) : [];
  } catch {
    return [];
  }
}

function entryTitle(entry: DiagnosticEntry) {
  const prefix = entry.source === "browser" ? "浏览器" : "后端";
  return `${prefix} · ${entry.event}`;
}

function isExceptionEntry(entry: DiagnosticEntry) {
  const eventSignalsFailure = /(failed|error|exception|unhandled)/i.test(entry.event);
  const context = entry.context && typeof entry.context === "object"
    ? entry.context as Record<string, unknown>
    : {};
  const hasOriginalError = ["error_type", "raw_error", "raw_message", "traceback"]
    .some((field) => context[field] !== undefined);
  return eventSignalsFailure
    || hasOriginalError
    || ["error", "critical"].includes(entry.level);
}

async function writeClipboardText(value: string) {
  try {
    await navigator.clipboard.writeText(value);
    return true;
  } catch {
    const textarea = document.createElement("textarea");
    textarea.value = value;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.select();
    const copied = document.execCommand("copy");
    textarea.remove();
    return copied;
  }
}

export function DevDiagnostics() {
  const [enabled, setEnabled] = useState(false);
  const [open, setOpen] = useState(false);
  const [clientEntries, setClientEntries] = useState<DiagnosticEntry[]>([]);
  const [backendEntries, setBackendEntries] = useState<DiagnosticEntry[]>([]);
  const [environment, setEnvironment] = useState("local");
  const [copyState, setCopyState] = useState("复制全部日志");
  const [exceptionCopyState, setExceptionCopyState] = useState("一键复制异常");
  const [loading, setLoading] = useState(false);

  const appendClientEntry = useCallback((entry: DiagnosticEntry) => {
    setClientEntries((current) => {
      const next = [...current, entry].slice(-MAX_CLIENT_ENTRIES);
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      } catch {
        // Diagnostics must never interfere with the quoting workflow.
      }
      return next;
    });
  }, []);

  const loadBackendEntries = useCallback(async () => {
    if (!enabled) return;
    setLoading(true);
    try {
      const response = await fetch(`${API_BASE}/api/debug/logs?limit=1000`, {
        cache: "no-store",
      });
      if (!response.ok) return;
      const payload = await response.json() as BackendDiagnosticResponse;
      setEnvironment(payload.environment ?? "development");
      setBackendEntries(
        (payload.entries ?? []).map((entry) => ({ ...entry, source: "backend" })),
      );
    } catch (error) {
      appendClientEntry({
        diagnostic_id: `browser_${crypto.randomUUID()}`,
        timestamp: new Date().toISOString(),
        level: "error",
        event: "diagnostic_log_fetch_failed",
        message: error instanceof Error ? error.message : String(error),
        source: "browser",
        context: redactValue({
          error_type: error instanceof Error ? error.name : typeof error,
          raw_error: error instanceof Error ? error.message : String(error),
          stack: error instanceof Error ? error.stack : undefined,
        }),
      });
    } finally {
      setLoading(false);
    }
  }, [appendClientEntry, enabled]);

  const clearLocal = useCallback(() => {
    setClientEntries([]);
    setBackendEntries([]);
    localStorage.removeItem(STORAGE_KEY);
  }, []);

  const clearAll = useCallback(async () => {
    clearLocal();
    try {
      await fetch(`${API_BASE}/api/debug/logs/clear`, { method: "POST" });
    } catch {
      // Clearing logs must never block the application.
    }
  }, [clearLocal]);

  useEffect(() => {
    const isLocal = ["localhost", "127.0.0.1", "::1"].includes(window.location.hostname);
    const shouldEnable = process.env.NODE_ENV !== "production"
      || process.env.NEXT_PUBLIC_ENABLE_DEBUG_LOGS === "1"
      || isLocal;
    if (!shouldEnable) return;

    const initialLoad = window.setTimeout(() => {
      setEnabled(true);
      // A full browser refresh starts a new test run. Do not mix exceptions
      // from the previous run into the next quote's one-click export.
      void clearAll();
    }, 0);
    installFetchInstrumentation();
    fetchListeners.add(appendClientEntry);

    const handleWindowError = (event: ErrorEvent) => {
      appendClientEntry({
        diagnostic_id: `browser_${crypto.randomUUID()}`,
        timestamp: new Date().toISOString(),
        level: "error",
        event: "browser_window_error",
        message: event.message,
        source: "browser",
        context: redactValue({
          filename: event.filename,
          line: event.lineno,
          column: event.colno,
          error_type: event.error instanceof Error ? event.error.name : typeof event.error,
          raw_error: event.error instanceof Error ? event.error.message : String(event.error ?? ""),
          stack: event.error instanceof Error ? event.error.stack : undefined,
        }),
      });
    };
    const handleRejection = (event: PromiseRejectionEvent) => {
      const reason = event.reason;
      appendClientEntry({
        diagnostic_id: `browser_${crypto.randomUUID()}`,
        timestamp: new Date().toISOString(),
        level: "error",
        event: "browser_unhandled_rejection",
        message: reason instanceof Error ? reason.message : String(reason),
        source: "browser",
        context: redactValue({
          error_type: reason instanceof Error ? reason.name : typeof reason,
          raw_error: reason instanceof Error ? reason.message : String(reason),
          stack: reason instanceof Error ? reason.stack : undefined,
        }),
      });
    };
    const handleStorage = (event: StorageEvent) => {
      if (event.key === STORAGE_KEY) setClientEntries(loadStoredEntries());
    };
    const handleQuoteReset = () => {
      setOpen(false);
      clearLocal();
    };
    window.addEventListener("error", handleWindowError);
    window.addEventListener("unhandledrejection", handleRejection);
    window.addEventListener("storage", handleStorage);
    window.addEventListener(CLEAR_DIAGNOSTICS_EVENT, handleQuoteReset);
    return () => {
      window.clearTimeout(initialLoad);
      fetchListeners.delete(appendClientEntry);
      window.removeEventListener("error", handleWindowError);
      window.removeEventListener("unhandledrejection", handleRejection);
      window.removeEventListener("storage", handleStorage);
      window.removeEventListener(CLEAR_DIAGNOSTICS_EVENT, handleQuoteReset);
    };
  }, [appendClientEntry, clearAll, clearLocal]);

  useEffect(() => {
    if (!open || !enabled) return;
    const initialRefresh = window.setTimeout(() => void loadBackendEntries(), 0);
    const interval = window.setInterval(() => void loadBackendEntries(), 4000);
    return () => {
      window.clearTimeout(initialRefresh);
      window.clearInterval(interval);
    };
  }, [enabled, loadBackendEntries, open]);

  const entries = useMemo(
    () => [...backendEntries, ...clientEntries].sort((left, right) =>
      right.timestamp.localeCompare(left.timestamp)),
    [backendEntries, clientEntries],
  );
  const issueCount = entries.filter((entry) => entry.level !== "info").length;
  const exceptionEntries = useMemo(() => entries.filter(isExceptionEntry), [entries]);

  const diagnosticPackage = useCallback(() => redactValue({
    exported_at: new Date().toISOString(),
    application: "AstraQuote",
    environment,
    page_url: window.location.href,
    user_agent: navigator.userAgent,
    backend_entries: backendEntries,
    browser_entries: clientEntries,
  }), [backendEntries, clientEntries, environment]);

  const copyAll = useCallback(async () => {
    const output = JSON.stringify(diagnosticPackage(), null, 2);
    if (await writeClipboardText(output)) {
      setCopyState("已复制，可直接发给开发人员");
    } else {
      setCopyState("复制失败，请下载 JSON");
    }
    window.setTimeout(() => setCopyState("复制全部日志"), 2400);
  }, [diagnosticPackage]);

  const copyExceptions = useCallback(async () => {
    if (exceptionEntries.length === 0) {
      setExceptionCopyState("暂无异常");
      window.setTimeout(() => setExceptionCopyState("一键复制异常"), 1800);
      return;
    }
    const output = JSON.stringify(redactValue({
      exported_at: new Date().toISOString(),
      application: "AstraQuote",
      environment,
      export_type: "exceptions_only",
      exception_count: exceptionEntries.length,
      normal_entries_omitted: entries.length - exceptionEntries.length,
      page_url: window.location.href,
      exceptions: exceptionEntries,
    }), null, 2);
    if (await writeClipboardText(output)) {
      setExceptionCopyState(`已复制 ${exceptionEntries.length} 条异常`);
    } else {
      setExceptionCopyState("复制失败，请复制全部日志");
    }
    window.setTimeout(() => setExceptionCopyState("一键复制异常"), 2400);
  }, [entries.length, environment, exceptionEntries]);

  const downloadAll = useCallback(() => {
    const blob = new Blob([JSON.stringify(diagnosticPackage(), null, 2)], {
      type: "application/json;charset=utf-8",
    });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `astraquote-diagnostics-${new Date().toISOString().replaceAll(":", "-")}.json`;
    link.click();
    URL.revokeObjectURL(link.href);
  }, [diagnosticPackage]);

  if (!enabled) return null;

  return (
    <aside className="dev-diagnostics" data-testid="dev-diagnostics">
      <button
        className={`dev-diagnostics-trigger${issueCount ? " has-error" : ""}`}
        type="button"
        onClick={() => setOpen(true)}
        aria-label="打开调试日志"
      >
        调试日志
        {issueCount > 0 && <b>{issueCount}</b>}
      </button>
      {open && (
        <div className="dev-diagnostics-layer" role="dialog" aria-modal="true" aria-label="调试日志">
          <button className="dev-diagnostics-backdrop" type="button" onClick={() => setOpen(false)} aria-label="关闭调试日志" />
          <section className="dev-diagnostics-panel">
            <header>
              <div>
                <small>测试环境 · {environment}</small>
                <h2>调试日志</h2>
                <p>包含原始异常、完整调用栈、任务进度和 AWS 返回详情；敏感凭据已自动遮盖。</p>
              </div>
              <button type="button" onClick={() => setOpen(false)} aria-label="关闭">×</button>
            </header>
            <div className="dev-diagnostics-actions">
              <button type="button" onClick={() => void loadBackendEntries()} disabled={loading}>
                {loading ? "正在刷新" : "刷新"}
              </button>
              <button className="primary-action" type="button" onClick={() => void copyExceptions()}>
                {exceptionCopyState}
              </button>
              <button type="button" onClick={() => void copyAll()}>{copyState}</button>
              <button type="button" onClick={downloadAll}>下载 JSON</button>
              <button type="button" onClick={() => void clearAll()}>清空</button>
            </div>
            <div className="dev-diagnostics-summary">
              <span>总记录 {entries.length}</span>
              <span className={issueCount ? "error-count" : ""}>异常 {issueCount}</span>
              <span>浏览器 {clientEntries.length}</span>
              <span>后端 {backendEntries.length}</span>
            </div>
            <div className="dev-diagnostics-list">
              {entries.length === 0 ? (
                <p className="dev-diagnostics-empty">暂时没有日志。开始核验或报价后，这里会自动记录。</p>
              ) : entries.map((entry) => (
                <details className={`dev-diagnostic-entry level-${entry.level}`} key={entry.diagnostic_id}>
                  <summary>
                    <span>{new Date(entry.timestamp).toLocaleTimeString("zh-CN", { hour12: false })}</span>
                    <strong>{entryTitle(entry)}</strong>
                    <em>{entry.message || entry.level}</em>
                  </summary>
                  <pre>{JSON.stringify(entry, null, 2)}</pre>
                </details>
              ))}
            </div>
          </section>
        </div>
      )}
    </aside>
  );
}
