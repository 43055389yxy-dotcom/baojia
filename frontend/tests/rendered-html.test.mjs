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
  assert.match(page, /createPortal/);
  assert.match(page, /document\.body/);
  assert.match(page, /global-requote-button/);
  assert.doesNotMatch(page, />Microsoft Azure 报价</);
  assert.match(page, /请由销售确认客户部署地区/);
  assert.match(page, /不会把地区问题发给客户/);
  assert.match(page, /销售确认地区并开始整理/);
  assert.match(page, /SALES_REGION_CONTEXT_KEY/);
  assert.match(page, /astraquote\.aws\.current-sales-region\.v2/);
  assert.doesNotMatch(page, /astraquote\.azure\.current-sales-region\.v2/);
  assert.doesNotMatch(page, /\/api\/azure\/quotes\/region-preflight/);
  assert.match(page, /sales_region: currentSalesRegion/);
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
  const salesPage = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );
  const confirmationPage = await readFile(
    new URL("../app/confirm/[token]/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(confirmationPage, /请核对配置信息/);
  assert.match(confirmationPage, /如有不符，请直接修改、添加或删除/);
  assert.doesNotMatch(confirmationPage, /最终配置确认|请核对完整配置清单|配置概览/);
  assert.match(confirmationPage, /正在检查新加的配置/);
  assert.match(confirmationPage, /这里只处理新加的内容，原来的配置不会重新运行/);
  assert.doesNotMatch(confirmationPage, /AI 响应较慢，正在自动重试/);
  assert.match(confirmationPage, /原内容已保留，请点击“重新尝试”/);
  assert.match(confirmationPage, /queuedComponentIds/);
  assert.match(confirmationPage, /submittedComponentSnapshots/);
  assert.match(confirmationPage, /api\/\$\{provider\}\/configuration-field-options/);
  assert.match(confirmationPage, /provider = session\?\.cloud_provider === "azure"/);
  assert.match(confirmationPage, /configuredFieldOptions\(item, field, isAzureConfirmation\)/);
  assert.match(confirmationPage, /serviceOptions = isAzure \? \[\]/);
  assert.match(confirmationPage, /commonOptions = isAzure \? \[\]/);
  assert.match(confirmationPage, /loadOfficialFieldOptions/);
  assert.match(confirmationPage, /updateTransientNumericField/);
  assert.match(confirmationPage, /rawValue !== ""/);
  assert.match(confirmationPage, /hierarchyOrderedConfigurationItems/);
  assert.match(confirmationPage, /customer-transient-toast/);
  assert.match(confirmationPage, /最终确认并开始报价/);
  assert.doesNotMatch(confirmationPage, /系统已更新该服务的官方报价映射/);
  assert.doesNotMatch(confirmationPage, /确认配置并重新核验官方报价/);
  assert.doesNotMatch(confirmationPage, /global-requote-button/);
  assert.doesNotMatch(confirmationPage, />重新报价</);
  assert.match(confirmationPage, /isSystemPricingIssue/);
  assert.match(confirmationPage, /isSubmittingComponent \|\| isQueuedComponent \|\| isRefreshing/);
  assert.match(confirmationPage, /isRefreshing \? "更新中…"/);
  assert.doesNotMatch(confirmationPage, /请返回报价页面重新分析/);
  assert.match(salesPage, /component-processing-log/);
  assert.match(salesPage, /component-processing-log/);
  assert.doesNotMatch(salesPage, /组件处理日志/);
  assert.match(salesPage, /componentRetryStatus/);
  assert.match(salesPage, /本轮尚未通过/);
  assert.match(salesPage, /获取客户确认链接/);
  assert.match(salesPage, /openedCustomerLinkVersion/);
  assert.doesNotMatch(salesPage, /系统正在独立处理该组件/);
});

test("customer questions are collected on one concise page", async () => {
  const confirmationPage = await readFile(
    new URL("../app/confirm/[token]/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(confirmationPage, /请确认以下配置选项/);
  assert.match(confirmationPage, /为确保报价准确/);
  assert.match(confirmationPage, /确认配置并提交/);
  assert.doesNotMatch(confirmationPage, /仅填写需要您决定的项目|需求摘要/);
});

test("internal validation retries failed components automatically without exposing errors", async () => {
  const salesPage = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(salesPage, /sales_validation_required/);
  assert.match(salesPage, /系统正在自动完成组件核验/);
  assert.match(salesPage, /retry_component_ids/);
  assert.match(salesPage, /failedComponentIds/);
  assert.doesNotMatch(salesPage, /重新执行内部核验/);
  assert.doesNotMatch(salesPage, /确认配置可用并生成客户链接/);
});
