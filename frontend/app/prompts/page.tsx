"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

type PromptItem = {
  key: string;
  title: string;
  category: string;
  order: number;
  content: string;
  is_overridden: boolean;
  is_generated?: boolean;
  is_editable?: boolean;
  status?: string;
};

type PromptLibrary = { items: PromptItem[]; usage: string };
const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api/backend";

export default function PromptLibraryPage() {
  const [provider, setProvider] = useState<"aws" | "azure">("aws");
  const [library, setLibrary] = useState<PromptLibrary | null>(null);
  const [query, setQuery] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState<string | null>(null);
  const [message, setMessage] = useState("");

  useEffect(() => {
    const selected = new URLSearchParams(window.location.search).get("provider") === "azure" ? "azure" : "aws";
    queueMicrotask(() => setProvider(selected));
  }, []);

  useEffect(() => {
    void fetch(`${API_BASE}/api/prompt-library?provider=${provider}`, { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error("无法读取提示词规则库");
        return (await response.json()) as PromptLibrary;
      })
      .then((payload) => {
        setLibrary(payload);
        setDrafts(Object.fromEntries(payload.items.map((item) => [item.key, item.content])));
      })
      .catch((error: unknown) => setMessage(error instanceof Error ? error.message : "读取失败"));
  }, [provider]);

  const filtered = useMemo(() => {
    const keyword = query.trim().toLocaleLowerCase();
    if (!library) return [];
    return library.items.filter((item) =>
      !keyword || `${item.title} ${item.category} ${item.key} ${drafts[item.key] ?? item.content}`
        .toLocaleLowerCase()
        .includes(keyword)
    );
  }, [drafts, library, query]);

  const groups = useMemo(() => {
    const result = new Map<string, PromptItem[]>();
    for (const item of filtered) {
      const entries = result.get(item.category) ?? [];
      entries.push(item);
      result.set(item.category, entries);
    }
    return Array.from(result.entries());
  }, [filtered]);

  async function save(item: PromptItem) {
    if (item.is_editable === false) return;
    setSaving(item.key);
    setMessage("");
    try {
      const response = await fetch(`${API_BASE}/api/prompt-library/${item.key}?provider=${provider}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: drafts[item.key] }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.message ?? "保存失败");
      setLibrary(payload as PromptLibrary);
      setDrafts(Object.fromEntries((payload as PromptLibrary).items.map((entry) => [entry.key, entry.content])));
      setMessage(`${item.title} 已保存，下次系统解析立即生效。`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "保存失败");
    } finally {
      setSaving(null);
    }
  }

  function switchProvider(nextProvider: "aws" | "azure") {
    if (nextProvider === provider) return;
    setLibrary(null);
    setMessage("");
    setProvider(nextProvider);
  }

  return (
    <main className="prompt-app">
      <header className="site-header prompt-header">
        <Link className="brand" href="/" aria-label="返回智能报价">
          <span>A</span><strong>AstraQuote</strong>
        </Link>
        <div className="product-title"><strong>提示词管理</strong><span>开发调试</span></div>
        <Link className="prompt-nav-link global-requote-button" href="/">重新报价</Link>
      </header>

      <section className="prompt-page-title">
        <div>
          <p className="kicker">PROMPT RULE LIBRARY</p>
          <h1>组件提示词规则库</h1>
          <p>{library?.usage ?? "正在读取后端当前使用的规则……"}</p>
        </div>
        <label>
          <span>搜索规则</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="EC2、Redis、区域、默认值……" />
        </label>
      </section>

      <nav className="cloud-provider-switch prompt-provider-switch" aria-label="提示词云平台">
        <button className={provider === "aws" ? "selected" : ""} onClick={() => switchProvider("aws")}>AWS 提示词</button>
        <button className={provider === "azure" ? "selected" : ""} onClick={() => switchProvider("azure")}>Microsoft Azure 提示词</button>
      </nav>

      {message && <div className="prompt-message">{message}</div>}

      <section className="prompt-library-scroll" aria-label="提示词卡片列表">
        {groups.map(([category, items]) => (
          <div className="prompt-group" key={category}>
            <div className="prompt-group-title"><h2>{category}</h2><span>{items.length} 个模块</span></div>
            <div className="prompt-card-grid">
              {items.map((item) => (
                <details className="prompt-card" key={item.key}>
                  <summary>
                    <span><small>{item.key}</small><strong>{item.title}</strong></span>
                    <i>{item.is_generated ? item.status ?? "自动生成" : item.is_overridden ? "已调整" : "源码默认"}</i>
                  </summary>
                  <div>
                    <textarea
                      aria-label={`${item.title}提示词`}
                      value={drafts[item.key] ?? item.content}
                      readOnly={item.is_editable === false}
                      onChange={(event) => setDrafts((current) => ({ ...current, [item.key]: event.target.value }))}
                    />
                    <footer>
                      <span>{(drafts[item.key] ?? item.content).length.toLocaleString()} 字符</span>
                      {item.is_editable === false ? <span>官方目录自动维护</span> : (
                        <button disabled={saving === item.key} onClick={() => void save(item)}>
                          {saving === item.key ? "保存中" : "保存并生效"}
                        </button>
                      )}
                    </footer>
                  </div>
                </details>
              ))}
            </div>
          </div>
        ))}
        {!library && !message && <p className="prompt-empty">正在加载……</p>}
        {library && filtered.length === 0 && <p className="prompt-empty">没有匹配的规则</p>}
      </section>
    </main>
  );
}
