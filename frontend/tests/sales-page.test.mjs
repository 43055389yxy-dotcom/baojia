import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("sales portal returns a server-issued submission code and recovers active jobs", async () => {
  const page = await readFile(new URL("../app/sales/page.tsx", import.meta.url), "utf8");

  assert.doesNotMatch(page, /销售姓名|salesName|sales_name/);
  assert.match(page, /提交码/);
  assert.match(page, /submission_code/);
  assert.match(page, /提交报价/);
  assert.match(page, /\/api\/quote-relay\/jobs/);
  assert.match(page, /\/api\/quote-relay\/health/);
  assert.match(page, /ACTIVE_JOB_KEY/);
  assert.match(page, /window\.localStorage\.setItem/);
  assert.match(page, /pricing_mode: includesCommitment \? "reserved" : "on_demand"/);
  assert.match(page, /reserved_term_years/);
  assert.match(page, /payment_option/);
  assert.match(page, /include_on_demand_scenario/);
  assert.match(page, /cloud_provider: cloudProvider/);
  assert.match(page, /Microsoft Azure|微软 Azure/);
  assert.match(page, /Oracle Cloud/);
  assert.match(page, /Google Cloud/);
  assert.match(page, /display_result_on_page/);
  assert.match(page, /useState\(false\)/);
  assert.doesNotMatch(page, /generate_calculator_link|官方报价链接|Calculator/);
  assert.match(page, /在页面显示报价结果/);
  assert.match(page, /不生成文件，也不发送到企业微信群/);
  assert.match(page, /查看报价结果/);
  assert.match(page, /复制报价/);
  assert.match(page, /navigator\.clipboard\.writeText/);
  assert.match(page, /new Set<ScenarioKey>\(\["on_demand", "reserved_1yr_all_upfront", "reserved_3yr_all_upfront"\]\)/);
  assert.match(page, /1 年全预付/);
  assert.match(page, /3 年全预付/);
  assert.match(page, /type="radio"/);
  assert.doesNotMatch(page, /get_prices|build_estimate|Fact Ledger/);
  assert.doesNotMatch(page, /chat_url|管理员查看对话/);
  assert.doesNotMatch(page, /\/api\/gpt-relay/);
});

test("sales portal exposes only formal progress copy and no internal implementation", async () => {
  const page = await readFile(new URL("../app/sales/page.tsx", import.meta.url), "utf8");

  assert.match(page, /报价申请已提交/);
  assert.match(page, /5～10 分钟/);
  assert.doesNotMatch(page, /5～20 分钟/);
  assert.match(page, /sales-job-progress/);
  assert.match(page, /报价结果已发送至企业微信群/);
  assert.doesNotMatch(page, /ChatGPT|对话|清洗|客户原始需求|events\?\.length/);
});

test("sales portal keeps polling after a transient status failure", async () => {
  const page = await readFile(new URL("../app/sales/page.tsx", import.meta.url), "utf8");

  assert.match(page, /window\.setInterval\(poll/);
  assert.match(page, /报价状态正在同步，请稍候/);
  assert.match(page, /setPageError\(""\)/);
  assert.doesNotMatch(page, /loadJob\(savedJobId\)\.catch\(\(\) => window\.localStorage\.removeItem/);
});
