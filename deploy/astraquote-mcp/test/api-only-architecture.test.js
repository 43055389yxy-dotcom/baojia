'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { Client } = require('@modelcontextprotocol/sdk/client/index.js');
const { InMemoryTransport } = require('@modelcontextprotocol/sdk/inMemory.js');

const { buildServer, INSTRUCTIONS } = require('../server');

test('public MCP is an official-first twelve-site workflow with AWS-only calculator delivery', async (t) => {
  const workflow = {
    describeService: async (input) => ({ status: 'ok', input }),
    getAttributeValues: async (input) => ({ status: 'ok', input }),
    getPrices: async (input) => ({ status: 'ok', input }),
    getPriceResults: async (input) => ({ status: 'ok', input }),
    getQuoteJobStatus: async (input) => ({ status: 'created', input }),
    resumeQuoteJob: async (input) => ({ status: 'created', input }),
    buildEstimate: async (input) => ({ status: 'delivered', input }),
  };
  const server = buildServer(workflow);
  const client = new Client({ name: 'astraquote-api-only-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  t.after(async () => {
    await client.close();
    await server.close();
  });

  const listed = await client.listTools();
  assert.deepEqual(
    listed.tools.map((tool) => tool.name),
    ['get_prices', 'build_estimate'],
  );
  assert.match(INSTRUCTIONS, /AWS.*Excel.*aws_calculator_url.*AWS Pricing Calculator/s);
  assert.match(INSTRUCTIONS, /非 AWS.*只返回 Excel.*严禁返回/s);
  assert.doesNotMatch(INSTRUCTIONS, /模板指纹|模板搜索/i);
  assert.match(INSTRUCTIONS, /AWS.*Price List/s);
  assert.match(INSTRUCTIONS, /Azure.*Retail Prices API/s);
  assert.match(INSTRUCTIONS, /Oracle Cloud/);
  assert.match(INSTRUCTIONS, /GCP.*Cloud Billing Catalog API/s);
  assert.match(INSTRUCTIONS, /腾讯云/);
  assert.match(INSTRUCTIONS, /阿里云/);
  assert.match(INSTRUCTIONS, /阿里云国际站/);
  assert.match(INSTRUCTIONS, /华为云/);
  assert.match(INSTRUCTIONS, /华为云国际站/);
  assert.match(INSTRUCTIONS, /百度智能云/);
  assert.match(INSTRUCTIONS, /火山引擎/);
  assert.match(INSTRUCTIONS, /天翼云/);
  assert.match(INSTRUCTIONS, /ASTRAQUOTE_STOP_CODE: AQ-QUOTE-FAILED/);
  assert.match(INSTRUCTIONS, /price_lookup.*queries=\[\].*不再请求云厂商/s);
  assert.match(INSTRUCTIONS, /needs_refinement.*不是失败/s);
  assert.match(INSTRUCTIONS, /只允许一次.*修正请求.*官方价格页/s);
  assert.match(INSTRUCTIONS, /credentials.*authorization.*不得把权限拒绝说成无 SKU/s);
  assert.match(INSTRUCTIONS, /GPT 负责理解需求.*计算金额/s);
  assert.match(INSTRUCTIONS, /不需要销售前端.*远程桌面.*远程 GPT/s);
  assert.match(INSTRUCTIONS, /AWS.*直接提供.*service_code.*Price List Filters/s);
  assert.match(INSTRUCTIONS, /不调用路由工具/);
});

test('production and sales entry have no Calculator runtime or option', () => {
  const repository = path.resolve(__dirname, '..', '..', '..');
  const files = [
    'deploy/Dockerfile',
    'deploy/start-production.sh',
    'deploy/compose.production.yml',
    'frontend/app/sales/page.tsx',
    'backend/app/aws_main.py',
    'backend/pyproject.toml',
  ];
  for (const relative of files) {
    const source = fs.readFileSync(path.join(repository, relative), 'utf8');
    assert.doesNotMatch(
      source,
      /sample-aws-pricing-calculator-mcp|CALCULATOR_MCP|generate_calculator_link|playwright/i,
      relative,
    );
  }
  const dockerignore = fs.readFileSync(path.join(repository, '.dockerignore'), 'utf8');
  assert.match(dockerignore, /backend\/app\/integrations\/\*calculator\*\.py/);
  assert.match(dockerignore, /backend\/app\/integrations\/aws_component_templates/);
});
