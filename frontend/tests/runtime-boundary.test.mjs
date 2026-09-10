import assert from "node:assert/strict";
import { access, readFile } from "node:fs/promises";
import test from "node:test";

test("the public root uses the formal sales portal", async () => {
  const root = await readFile(new URL("../app/page.tsx", import.meta.url), "utf8");
  assert.match(root, /\.\/sales\/page/);
  assert.doesNotMatch(root, /confirmation|prompt|contract|quote-jobs/i);
});

test("legacy quote pages are physically absent from the frontend runtime", async () => {
  for (const target of [
    "../app/confirm/[token]/page.tsx",
    "../app/contracts/page.tsx",
    "../app/prompts/page.tsx",
  ]) {
    await assert.rejects(access(new URL(target, import.meta.url)));
  }
});

test("backend proxy preserves an optional public base path", async () => {
  const route = await readFile(
    new URL("../app/api/backend/[...path]/route.ts", import.meta.url),
    "utf8",
  );
  assert.match(route, /const backendPrefix = target\.pathname/);
  assert.match(route, /`\$\{backendPrefix\}\/\$\{joinedPath\}`/);
  assert.doesNotMatch(route, /new URL\(`\/\$\{joinedPath\}`, BACKEND_URL\)/);
});

test("the shared sales proxy forwards every selected cloud provider", async () => {
  const route = await readFile(
    new URL("../app/api/backend/[...path]/route.ts", import.meta.url),
    "utf8",
  );

  assert.doesNotMatch(route, /payload\.cloud_provider\s*!==\s*["']aws["']/);
  assert.doesNotMatch(route, /AWS 报价系统只接受 AWS 报价任务/);
  assert.doesNotMatch(route, /AWS 报价系统禁止访问 Azure 数据/);
  assert.match(route, /await fetch\(target/);
});
