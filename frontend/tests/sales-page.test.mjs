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
  assert.match(page, /服务启动中/);
  assert.match(page, /health\?\.status === "starting"/);
  assert.match(page, /ACTIVE_JOB_KEY/);
  assert.match(page, /window\.sessionStorage\.setItem/);
  assert.match(page, /pricing_scenarios/);
  assert.match(page, /regionCatalog\?\.pricing_scenarios/);
  assert.match(page, /cloud_provider: cloudProvider/);
  assert.match(page, /preferred_engine: "chatgpt"/);
  assert.match(page, /ChatGPT/);
  assert.doesNotMatch(page, /Gemini|选择报价引擎|备用报价引擎/);
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
  assert.match(page, /type="radio"/);
  assert.match(page, /provider_catalogs/);
  assert.match(page, /价格接口待配置/);
  assert.doesNotMatch(page, /get_prices|build_estimate|Fact Ledger/);
  assert.doesNotMatch(page, /chat_url|管理员查看对话/);
  assert.doesNotMatch(page, /\/api\/gpt-relay/);
});

test("sales portal reads provider-native purchase scenarios from the backend catalog", async () => {
  const page = await readFile(new URL("../app/sales/page.tsx", import.meta.url), "utf8");

  assert.doesNotMatch(page, /const PROVIDER_SCENARIOS/);
  assert.match(page, /providerScenarios\.map/);
  assert.match(page, /one_month_subscription/);
  assert.match(page, /scenarioMonthlyCaption/);
  assert.match(page, /providerScenarios\.length < 1/);
  assert.match(page, /new Set<ScenarioKey>\(\["on_demand"\]\)/);
  assert.match(page, /setSelectedScenarios\(new Set<ScenarioKey>\(\["on_demand"\]\)\)/);
});

test("sales portal requires consecutive numbered lines before submission", async () => {
  const page = await readFile(new URL("../app/sales/page.tsx", import.meta.url), "utf8");
  const validator = await readFile(
    new URL("../app/sales/numbered-components.ts", import.meta.url),
    "utf8",
  );

  assert.match(page, /validateNumberedComponentLines/);
  assert.match(page, /每行一个组件，序号必须连续/);
  assert.match(page, /每个对话最多 10 个，按 5 个一轮处理/);
  assert.match(page, /1\. Linux 云服务器/);
  assert.match(validator, /split\("\\n"\)/);
  assert.match(validator, /第 \$\{lineNumber\} 行必须以连续序号/);
  assert.match(validator, /actual !== expected/);
});

test("sales portal exposes only formal progress copy and no internal implementation", async () => {
  const page = await readFile(new URL("../app/sales/page.tsx", import.meta.url), "utf8");
  const presentation = await readFile(new URL("../app/sales/presentation.ts", import.meta.url), "utf8");

  assert.match(page, /报价正在生成/);
  assert.match(page, /需求解析、官方价格核验与一致性校验等多层验证/);
  assert.match(page, /10～20 分钟/);
  assert.match(presentation, /15～30 分钟/);
  assert.match(page, /sales-job-progress/);
  assert.match(page, /role="progressbar"/);
  assert.match(page, /报价结果和 Excel 已生成/);
  assert.doesNotMatch(page, /组件核价进度|后台已确认|个组件完成|sales-job-stages/);
  assert.doesNotMatch(page, /企业微信群/);
  assert.doesNotMatch(page, /管理员查看对话|清洗|客户原始需求|events\?\.length/);
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
  assert.match(page, /报价任务已进入处理队列/);
  assert.match(page, /estimatedQuoteWindow/);
  assert.doesNotMatch(page, /queuedStatusDetail|processingStatusDetail/);
  assert.match(css, /color-scheme:\s*light/);
  assert.match(css, /--page:\s*#eef8ff/);
  assert.match(css, /backdrop-filter:\s*blur\(28px\) saturate\(145%\)/);
  assert.match(css, /\.sales-result-backdrop[^{]*\{[^}]*rgba\(213, 235, 251, \.72\)/s);
  assert.match(page, /role="combobox"/);
  assert.match(page, /sales-region-options-panel/);
  assert.doesNotMatch(page, /地域代码按当前云厂商解释/);
  assert.match(page, /id="sales-region"[\s\S]*?readOnly/);
  assert.match(page, /selectedRegionLabel/);
  assert.match(page, /value=\{selectedRegionLabel\}/);
  assert.doesNotMatch(page, /<small>\{item\.code\}<\/small>/);
  assert.match(page, /系统会自动使用对应的官方地域编号/);
  assert.doesNotMatch(page, /可保留当前输入/);
  assert.doesNotMatch(page, /<datalist/);
  assert.match(css, /grid-template-columns:\s*repeat\(4, minmax\(0, 1fr\)\)/);
  assert.match(css, /\.sales-provider-row\s*\{[^}]*grid-template-columns:\s*repeat\(3, minmax\(0, 1fr\)\)/s);
  assert.match(css, /background:\s*rgba\(239, 249, 255, \.985\)/);
  assert.match(css, /\.sales-region-options-panel\s*\{[^}]*position:\s*relative/s);
  assert.match(css, /@keyframes sales-progress-indeterminate/);
  assert.match(css, /@keyframes sales-visual-breathe/);
  assert.match(css, /\.sales-job-progress-track\.is-indeterminate/);
});
