import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../app/sales/presentation.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const { progressPercent, processingStatusDetail, queuedStatusDetail, unpricedRecoveryText, canRetryUnpriced, money } =
  await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

test("sales progress never invents completion or treats queue/login as running", () => {
  assert.equal(progressPercent({ status: "processing" }), null);
  assert.equal(progressPercent({ status: "queued", progress: { total_component_count: 40, completed_component_count: 3 } }), null);
  assert.equal(progressPercent({ status: "needs_login", progress: { total_component_count: 40, completed_component_count: 3 } }), null);
  assert.equal(progressPercent({ status: "processing", progress: { total_component_count: 40, completed_component_count: 0 } }), 0);
  assert.equal(progressPercent({ status: "processing", progress: { total_component_count: 40, completed_component_count: 38 } }), 95);
});

test("quote assembly remains visible after all prices are saved and batches do not imply unlimited parallelism", () => {
  const text = processingStatusDetail({ progress: { stage: "estimate_validated", total_component_count: 60, completed_component_count: 60, component_chat_count: 3 } });
  assert.match(text, /正在生成 Excel/);
  const large = processingStatusDetail({ progress: { total_component_count: 120, completed_component_count: 38, component_chat_count: 6 } });
  assert.match(large, /38\/120/);
  assert.match(large, /6 批/);
  assert.doesNotMatch(large, /6 批并行/);
});

test("queued sales see shared quote slots rather than a misleading salesperson count", () => {
  const text = queuedStatusDetail({ active_quote_count: 3, max_concurrent_quotes: 4, queued_ahead_count: 1, estimated_wait_minutes: 5 });
  assert.match(text, /3\/4 个报价名额/);
  assert.match(text, /1 个任务排在您前面/);
  assert.match(text, /预计等待约 5 分钟/);
});

test("partial recovery is actionable without echoing internal errors or offering impossible retries", () => {
  assert.equal(canRetryUnpriced({ is_partial: true, unpriced_components: [{ retryable: false }] }), false);
  assert.equal(canRetryUnpriced({ is_partial: true, unpriced_components: [{ retryable: false }, { retryable: true }] }), true);
  assert.match(unpricedRecoveryText({ failure_code: "official_query_failed", retryable: true }), /重试/);
  assert.match(unpricedRecoveryText({ failure_code: "unsupported_in_region", retryable: false }), /管理员/);
  assert.doesNotMatch(unpricedRecoveryText({ failure_code: "internal-secret-value", retryable: false }), /internal-secret-value/);
});

test("sales recovery text uses backend failure categories instead of asking the model to guess", () => {
  assert.match(
    unpricedRecoveryText({ failure_category: "authorization", retryable: false }),
    /询价权限/,
  );
  assert.match(
    unpricedRecoveryText({ failure_category: "provider_unavailable", retryable: true }),
    /官方价格服务暂时不可用/,
  );
  assert.match(
    unpricedRecoveryText({ failure_category: "invalid_request", retryable: true }),
    /参数或当地可售规格/,
  );
});

test("unknown amounts never become zero cost", () => {
  assert.equal(money(undefined, "USD"), "—");
  assert.equal(money("", "USD"), "—");
  assert.equal(money("invalid", "USD"), "—");
  assert.equal(money("0", "USD"), "0.00 USD");
  assert.equal(money("1234.50", "USD"), "1,234.50 USD");
});
