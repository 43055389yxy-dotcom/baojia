import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const source = await readFile(new URL("../app/sales/presentation.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
}).outputText;
const { progressPercent, estimatedQuoteWindow, unpricedRecoveryText, canRetryUnpriced, money } =
  await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

test("sales progress never invents completion or treats queue/login as running", () => {
  assert.equal(progressPercent({ status: "processing" }), null);
  assert.equal(progressPercent({ status: "queued", progress: { total_component_count: 40, completed_component_count: 3 } }), null);
  assert.equal(progressPercent({ status: "needs_login", progress: { total_component_count: 40, completed_component_count: 3 } }), null);
  assert.equal(progressPercent({ status: "processing", progress: { total_component_count: 40, completed_component_count: 0 } }), 0);
  assert.equal(progressPercent({ status: "processing", progress: { total_component_count: 40, completed_component_count: 38 } }), 95);
});

test("sales see a professional time window without internal component counters", () => {
  assert.equal(estimatedQuoteWindow({ progress: { top_level_component_count: 10 } }), "10～20 分钟");
  assert.equal(estimatedQuoteWindow({ progress: { top_level_component_count: 11 } }), "15～30 分钟");
  assert.equal(estimatedQuoteWindow({ progress: { top_level_component_count: 20 } }), "15～30 分钟");
  assert.equal(estimatedQuoteWindow({ progress: { total_component_count: 20 } }), "15～30 分钟");
  assert.equal(estimatedQuoteWindow({}), "10～20 分钟");
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
