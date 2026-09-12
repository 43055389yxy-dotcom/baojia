import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("sales portal keeps its internal job identity private and recovers active jobs", async () => {
  const page = await readFile(new URL("../app/sales/page.tsx", import.meta.url), "utf8");

  assert.doesNotMatch(page, /销售姓名|salesName|sales_name/);
  assert.doesNotMatch(page, /提交码/);
  assert.match(page, /submission_code/);
  assert.match(page, /提交报价/);
  assert.match(page, /\/api\/quote-relay\/jobs/);
  assert.match(page, /\/api\/quote-relay\/health/);
  assert.match(page, /ACTIVE_JOB_KEY/);
  assert.match(page, /window\.sessionStorage\.setItem/);
  assert.match(page, /pricing_scenarios/);
  assert.match(page, /cloud_provider: cloudProvider/);
  assert.match(page, /Microsoft Azure|微软 Azure/);
  assert.match(page, /Oracle Cloud/);
  assert.match(page, /Google Cloud/);
  assert.match(page, /腾讯云/);
  assert.match(page, /阿里云/);
  assert.match(page, /华为云/);
  assert.match(page, /百度智能云/);
  assert.match(page, /火山引擎/);
  assert.match(page, /天翼云/);
  assert.match(page, /client_request_id/);
  assert.doesNotMatch(page, /generate_calculator_link|官方报价链接|Calculator/);
  assert.match(page, /查看报价结果/);
  assert.match(page, /复制报价/);
  assert.match(page, /复制下载链接/);
  assert.match(page, /下载 Excel/);
  assert.match(page, /quote_download_url/);
  assert.match(page, /navigator\.clipboard\.writeText/);
  assert.match(page, /即用即付/);
  assert.match(page, /1 年预留/);
  assert.match(page, /1 年承诺使用/);
  assert.match(page, /OCI 公开按量价/);
  assert.match(page, /type="radio"/);
  assert.match(page, /provider_catalogs/);
  assert.match(page, /价格接口待配置/);
  assert.doesNotMatch(page, /get_prices|build_estimate|Fact Ledger/);
  assert.doesNotMatch(page, /chat_url|管理员查看对话/);
  assert.doesNotMatch(page, /\/api\/gpt-relay/);
});

test("sales portal uses provider-native purchase labels instead of one AWS-only model", async () => {
  const page = await readFile(new URL("../app/sales/page.tsx", import.meta.url), "utf8");

  assert.match(page, /azure:\s*\[[\s\S]*即用即付[\s\S]*1 年预留[\s\S]*3 年预留[\s\S]*\]/);
  assert.match(page, /oci:\s*\[\{[^\n]*OCI 公开按量价/);
  assert.match(page, /gcp:\s*\[[\s\S]*1 年承诺使用[\s\S]*3 年承诺使用[\s\S]*\]/);
  assert.match(page, /new Set<ScenarioKey>\(\["on_demand"\]\)/);
  assert.match(page, /setSelectedScenarios\(new Set<ScenarioKey>\(\["on_demand"\]\)\)/);
});

test("sales portal exposes only formal progress copy and no internal implementation", async () => {
  const page = await readFile(new URL("../app/sales/page.tsx", import.meta.url), "utf8");

  assert.match(page, /报价申请已提交/);
  assert.match(page, /5～10 分钟/);
  assert.doesNotMatch(page, /5～20 分钟/);
  assert.match(page, /sales-job-progress/);
  assert.match(page, /报价结果和 Excel 已生成/);
  assert.doesNotMatch(page, /企业微信群/);
  assert.doesNotMatch(page, /ChatGPT|对话|清洗|客户原始需求|events\?\.length/);
});

test("sales portal keeps polling after a transient status failure", async () => {
  const page = await readFile(new URL("../app/sales/page.tsx", import.meta.url), "utf8");

  assert.match(page, /window\.setInterval\(poll/);
  assert.match(page, /报价状态正在同步，请稍候/);
  assert.match(page, /setPageError\(""\)/);
  assert.doesNotMatch(page, /loadJob\(savedJobId\)\.catch\(\(\) => window\.localStorage\.removeItem/);
});

test("sales portal uses a structured workspace and grouped result actions", async () => {
  const page = await readFile(new URL("../app/sales/page.tsx", import.meta.url), "utf8");
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");

  assert.match(page, /sales-form-grid/);
  assert.match(page, /sales-result-layout/);
  assert.match(page, /<table className="sales-result-table">/);
  assert.match(page, /sales-result-config/);
  assert.doesNotMatch(page, /<dl>/);
  assert.match(page, /sales-result-actions/);
  assert.match(page, /sales-provider-mark/);
  assert.match(page, /providerOpen/);
  assert.match(page, /sales-provider-trigger/);
  assert.match(page, /onMouseLeave=\{\(\) => setProviderOpen\(false\)\}/);
  assert.match(page, /onClick=\{\(\) => setProviderOpen\(\(current\) => !current\)\}/);
  assert.doesNotMatch(page, /onPointerEnter/);
  assert.match(page, /aria-label=\{providerOpen \? "收起云厂商" : "展开云厂商"\}/);
  assert.doesNotMatch(page, /更换云厂商/);
  assert.match(page, /setProviderOpen\(false\)/);
  assert.doesNotMatch(page, /创建云成本报价/);
  assert.match(css, /prefers-reduced-motion/);
  assert.match(css, /sales-result-summary/);
  assert.match(css, /\.sales-result-table\s*\{/);
  assert.match(css, /grid-template-columns:\s*repeat\(auto-fit, minmax\(150px, 1fr\)\)/);
});

test("sales portal uses a pale-blue glass theme and distinguishes queued work", async () => {
  const page = await readFile(new URL("../app/sales/page.tsx", import.meta.url), "utf8");
  const css = await readFile(new URL("../app/globals.css", import.meta.url), "utf8");

  assert.match(page, /queued:\s*\{ title: "报价正在排队"/);
  assert.match(page, /job\.status === "queued" \? "等待启动"/);
  assert.match(page, /active_quote_count\?: number/);
  assert.match(page, /queued_ahead_count\?: number/);
  assert.match(page, /estimated_wait_minutes\?: number/);
  assert.match(page, /queuedStatusDetail/);
  assert.match(css, /color-scheme:\s*light/);
  assert.match(css, /--page:\s*#eef8ff/);
  assert.match(css, /backdrop-filter:\s*blur\(28px\) saturate\(145%\)/);
  assert.match(css, /\.sales-result-backdrop[^{]*\{[^}]*rgba\(213, 235, 251, \.72\)/s);
  assert.match(page, /role="combobox"/);
  assert.match(page, /sales-region-options-panel/);
  assert.match(page, /地域代码按当前云厂商解释/);
  assert.match(page, /最终可购性以每个产品的官方响应为准/);
  assert.match(page, /id="sales-region"[\s\S]*?readOnly/);
  assert.doesNotMatch(page, /可保留当前输入/);
  assert.doesNotMatch(page, /<datalist/);
  assert.match(css, /grid-template-columns:\s*repeat\(4, minmax\(0, 1fr\)\)/);
  assert.match(css, /\.sales-provider-row\s*\{[^}]*grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\)/s);
  assert.match(css, /background:\s*rgba\(239, 249, 255, \.985\)/);
  assert.match(css, /\.sales-region-options-panel\s*\{[^}]*position:\s*relative/s);
});
