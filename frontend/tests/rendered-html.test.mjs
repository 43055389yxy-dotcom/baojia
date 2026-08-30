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
  assert.match(page, /\["elb", "alb", "nlb", "gwlb"\]\.includes\(selection\.service\)/);
  assert.match(page, /load\\s\*balancer\|负载均衡/);
  assert.match(page, /展开记录/);
  assert.doesNotMatch(page, /AI 浏览器正在工作/);
  assert.doesNotMatch(page, /报价记录|插件中心|查看 AWS 技术计费字段/);
  assert.match(layout, /AWS 智能报价/);
  assert.match(layout, /DevDiagnostics/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
});

test("local diagnostics are global, copyable, detailed and credential-safe", async () => {
  const diagnostics = await readFile(
    new URL("../app/components/dev-diagnostics.tsx", import.meta.url),
    "utf8",
  );
  assert.match(diagnostics, /调试日志/);
  assert.match(diagnostics, /一键复制异常/);
  assert.match(diagnostics, /exceptions_only/);
  assert.match(diagnostics, /normal_entries_omitted/);
  assert.match(diagnostics, /复制全部日志/);
  assert.match(diagnostics, /下载 JSON/);
  assert.match(diagnostics, /browser_unhandled_rejection/);
  assert.match(diagnostics, /browser_fetch_exception/);
  assert.match(diagnostics, /\/api\/debug\/logs\?limit=1000/);
  assert.match(diagnostics, /REDACTED_AWS_ACCESS_KEY/);
  assert.match(diagnostics, /REDACTED_CONFIRMATION_TOKEN/);
});

test("configuration selection shows ten nearby official models directly", async () => {
  const picker = await readFile(
    new URL("../app/components/configuration-option-picker.tsx", import.meta.url),
    "utf8",
  );
  assert.match(picker, /lowerRanked\.slice\(0, 5\)/);
  assert.match(picker, /upperRanked\.slice\(0, 5\)/);
  assert.match(picker, /selectedValues\.size >= 10/);
  assert.match(picker, /modelConfigurationLabel\(option\)/);
  assert.match(picker, /`\$\{vcpu\} vCPU`/);
  assert.match(picker, /`\$\{memory\} GiB`/);
  assert.match(picker, /left\.vcpu \?\? Number\.POSITIVE_INFINITY/);
  assert.doesNotMatch(picker, /等于或高于客户填写规格/);
  assert.doesNotMatch(picker, /低于客户填写规格（由客户决定）/);
  assert.doesNotMatch(picker, /型号、CPU和内存均来自同一条AWS官方记录/);
  assert.doesNotMatch(picker, /选择处理器|选择内存|请先选择处理器/);
  assert.doesNotMatch(picker, /搜索型号或配置|搜索可用项/);
  assert.doesNotMatch(picker, /configuration-picker-hint/);
});

test("processor architecture is one quote-wide filter with ARM as the default", async () => {
  const picker = await readFile(
    new URL("../app/components/configuration-option-picker.tsx", import.meta.url),
    "utf8",
  );
  const confirmationPage = await readFile(
    new URL("../app/confirm/[token]/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(confirmationPage, /useState<ProcessorArchitecture>\("arm64"\)/);
  assert.match(confirmationPage, /下面所有组件只显示x86型号/);
  assert.match(confirmationPage, /processorArchitecture === "x86_64"/);
  assert.match(confirmationPage, /processor_architecture: processorArchitecture/);
  assert.match(confirmationPage, /PROCESSOR_ARCHITECTURE_ANSWER_KEY/);
  assert.match(confirmationPage, /session\.confirmation_items\.filter\(itemHasModelChoices\)/);
  assert.match(picker, /architecturePreference === "arm64"/);
  assert.match(picker, /architectureMatches\.length === 0/);
  assert.match(picker, /richOptions\.filter\(\(item\) => item\.architecture === architecturePreference\)/);
  assert.match(picker, /const architectureCatalog = architectureFallback/);
  assert.match(picker, /ranked\.slice\(0, 10\)/);
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
  assert.match(confirmationPage, /\["elb", "alb", "nlb", "gwlb"\]\.includes\(item\.service\)/);
  assert.match(confirmationPage, /"Network Load Balancer": "网络型负载均衡器"/);
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
  assert.match(salesPage, /internalValidationBlocked/);
  assert.match(salesPage, /组件校验未通过/);
  assert.match(salesPage, /本轮校验已停止，未生成客户链接/);
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
  assert.match(salesPage, /previewSelectionNextAction/);
  assert.match(salesPage, /retry_component/);
  assert.match(salesPage, /internal_block/);
  assert.match(salesPage, /previewHasUnfinishedComponents/);
  assert.match(salesPage, /salesReview && !internalValidationPending/);
  assert.match(salesPage, /正在完成全部组件校验/);
  assert.match(salesPage, /salesReview\?\.confirmation_token && !internalValidationPending/);
  assert.doesNotMatch(salesPage, /重新执行内部核验/);
  assert.doesNotMatch(salesPage, /确认配置可用并生成客户链接/);
});

test("failed component details preserve names from old and new backend payloads", async () => {
  const salesPage = await readFile(
    new URL("../app/page.tsx", import.meta.url),
    "utf8",
  );

  assert.match(salesPage, /typeof rawComponent === "string"/);
  assert.match(salesPage, /\? \{ display_name: rawComponent \}/);
  assert.match(salesPage, /component\.source_text/);
});
