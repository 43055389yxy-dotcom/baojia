import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html", host: "localhost" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the AstraQuote product shell", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /AstraQuote/);
  assert.match(html, /AWS 智能报价/);
  assert.doesNotMatch(html, /codex-preview|Your site is taking shape/);
  assert.match(html, /og\.png/);
});

test("client uses the live quote job API and keeps official-source copy", async () => {
  const [page, layout, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);
  assert.match(page, /\/api\/quotes\/preview/);
  assert.match(page, /\/api\/quote-jobs/);
  assert.match(page, /本次没有生成价格/);
  assert.match(page, /月均成本/);
  assert.doesNotMatch(page, /复制链接/);
  assert.match(page, /Enter 开始系统解析/);
  assert.doesNotMatch(page, /交给 AI 拆分/);
  assert.match(page, /组件配置清单/);
  assert.match(page, /配置确认/);
  assert.match(page, /导出 Excel/);
  assert.match(page, /报价单/);
  assert.match(page, /quote-table/);
  assert.match(page, /展开记录/);
  assert.doesNotMatch(page, /AI 浏览器正在工作/);
  assert.doesNotMatch(page, /报价记录|插件中心|查看 AWS 技术计费字段/);
  assert.match(layout, /AWS 智能报价/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
});
