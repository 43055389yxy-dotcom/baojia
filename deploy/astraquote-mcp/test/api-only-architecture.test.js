'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { Client } = require('@modelcontextprotocol/sdk/client/index.js');
const { InMemoryTransport } = require('@modelcontextprotocol/sdk/inMemory.js');

const { buildServer, INSTRUCTIONS } = require('../server');

test('public MCP is an official ten-cloud catalog API-only workflow', async (t) => {
  const workflow = {
    describeService: async (input) => ({ status: 'ok', input }),
    getAttributeValues: async (input) => ({ status: 'ok', input }),
    getPrices: async (input) => ({ status: 'ok', input }),
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
    ['describe_service', 'get_attribute_values', 'get_prices', 'get_quote_job_status', 'resume_quote_job', 'build_estimate'],
  );
  assert.doesNotMatch(INSTRUCTIONS, /Calculator|官方报价链接|模板指纹|模板搜索/i);
  assert.match(INSTRUCTIONS, /AWS Price List API/);
  assert.match(INSTRUCTIONS, /Azure Retail Prices API/);
  assert.match(INSTRUCTIONS, /Oracle Cloud Price List API/);
  assert.match(INSTRUCTIONS, /Google Cloud Billing Catalog API/);
  assert.match(INSTRUCTIONS, /腾讯云/);
  assert.match(INSTRUCTIONS, /阿里云/);
  assert.match(INSTRUCTIONS, /华为云/);
  assert.match(INSTRUCTIONS, /百度智能云/);
  assert.match(INSTRUCTIONS, /火山引擎/);
  assert.match(INSTRUCTIONS, /天翼云/);
  assert.match(INSTRUCTIONS, /ASTRAQUOTE_STOP_CODE: AQ-QUOTE-BLOCKED/);
  assert.match(INSTRUCTIONS, /不得.*needs_refinement.*终止码/s);
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
