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
  assert.match(page, /previewPollFailures/);
  assert.match(page, /failures <= 12/);
  assert.match(page, /response\.status === 404/);
  assert.match(page, /previewRestartedJobs/);
  assert.match(page, /本次没有生成价格/);
  assert.match(page, /月均成本/);
  assert.doesNotMatch(page, /复制链接/);
  assert.match(page, /event\.key === "Enter" && !event\.shiftKey/);
  assert.match(page, /void submitRequirement\(\)/);
  assert.doesNotMatch(page, /交给 AI 拆分/);
  assert.match(page, /组件配置清单/);
  assert.match(page, /配置确认/);
  assert.match(page, /导出 Excel/);
  assert.match(page, /报价单/);
  assert.match(page, /quote-table/);
  assert.match(page, /line\.key === "rdsstg" \|\| line\.group === "rds-storage"/);
  assert.match(page, /展开记录/);
  assert.doesNotMatch(page, /AI 浏览器正在工作/);
  assert.doesNotMatch(page, /报价记录|插件中心|查看 AWS 技术计费字段/);
  assert.match(layout, /AWS 智能报价/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
});

test("configuration selection follows processor then memory without search", async () => {
  const picker = await readFile(
    new URL("../app/components/configuration-option-picker.tsx", import.meta.url),
    "utf8",
  );
  const memoryHandler = picker.slice(
    picker.indexOf("const handleMemoryChange"),
    picker.indexOf("if (!catalog)"),
  );

  assert.match(picker, /处理器/);
  assert.match(picker, /内存/);
  assert.match(picker, /请先选择处理器/);
  assert.match(picker, /disabled={!vcpu \|\| memoryValues\.length === 0}/);
  assert.doesNotMatch(picker, /vcpu && memoryValues\.length > 0 && <label>/);
  assert.match(memoryHandler, /monthly_catalog_cost/);
  assert.match(memoryHandler, /emitSelection\(cheapest\?\.option\.value \?\? ""\)/);
  assert.doesNotMatch(picker, /搜索型号或配置|搜索可用项/);
  assert.doesNotMatch(picker, /configuration-picker-hint/);
  assert.doesNotMatch(memoryHandler, /setVcpu/);
});

test("final customer review keeps the instruction concise", async () => {
  const confirmationPage = await readFile(
    new URL("../app/confirm/[token]/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(confirmationPage, /请核对配置信息/);
  assert.match(confirmationPage, /如有不符，请直接修改、添加或删除/);
  assert.doesNotMatch(confirmationPage, /最终配置确认|请核对完整配置清单|配置概览/);
  assert.match(confirmationPage, /AI 响应较慢，正在自动重试/);
  assert.match(confirmationPage, /原内容已保留，请点击“重新尝试”/);
  assert.match(confirmationPage, /queuedComponentIds/);
  assert.match(confirmationPage, /submittedComponentSnapshots/);
  assert.match(confirmationPage, /\/api\/aws\/configuration-field-options/);
  assert.match(confirmationPage, /loadOfficialFieldOptions/);
  assert.match(confirmationPage, /updateTransientNumericField/);
  assert.match(confirmationPage, /rawValue !== ""/);
  assert.match(confirmationPage, /hierarchyOrderedConfigurationItems/);
  assert.match(confirmationPage, /customer-transient-toast/);
  assert.match(confirmationPage, /请先修改.*项不可用配置/);
  assert.match(confirmationPage, /isSubmittingComponent \|\| isQueuedComponent \|\| isRefreshing/);
  assert.match(confirmationPage, /isRefreshing \? "更新中…"/);
  assert.doesNotMatch(confirmationPage, /请返回报价页面重新分析/);
});

test("customer questions are collected on one concise page", async () => {
  const confirmationPage = await readFile(
    new URL("../app/confirm/[token]/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(confirmationPage, /请一次确认全部问题/);
  assert.match(confirmationPage, /所有待确认项都集中在本页/);
  assert.match(confirmationPage, /全部填写完成，统一提交/);
  assert.doesNotMatch(confirmationPage, /仅填写需要您决定的项目|需求摘要/);
});
