#!/usr/bin/env node
'use strict';

const express = require('express');
const fs = require('node:fs');
const path = require('node:path');
const { McpServer } = require('@modelcontextprotocol/sdk/server/mcp.js');
const {
  StreamableHTTPServerTransport,
} = require('@modelcontextprotocol/sdk/server/streamableHttp.js');
const { z } = require('zod');

const { AstraQuoteBackendClient, BackendError } = require('./lib/backend-client');
const { QuoteDeliveryError, QuoteDeliveryService } = require('./lib/quote-delivery');
const { QuoteStoreError, V2QuoteStore } = require('./lib/v2-quote-store');
const { AstraQuoteV2Workflow } = require('./lib/v2-workflow');

const VERSION = '3.1.0';
const PORT = Number(process.env.ASTRAQUOTE_MCP_PORT || process.env.PORT || 8200);
const HOST = process.env.ASTRAQUOTE_MCP_HOST || process.env.HOST || '127.0.0.1';

const INSTRUCTIONS = fs.readFileSync(
  path.join(__dirname, 'instructions.zh-CN.md'),
  'utf8',
).trim();

const jsonValue = z.union([
  z.string(), z.number(), z.boolean(), z.null(), z.array(z.unknown()), z.record(z.unknown()),
]);
const region = z.string().min(3).max(40);
const serviceCode = z.string().min(2).max(120);
const componentKey = z.string().regex(/^cmp_[A-Za-z0-9_-]{4,76}$/);
const factId = z.string().regex(/^[A-Za-z][A-Za-z0-9_-]{0,79}$/);
const cloudProvider = z.enum(['aws', 'azure', 'oci', 'gcp']);

const describeServiceInput = z.object({ service_code: serviceCode }).strict();

const attributeValuesInput = z.object({
  service_code: serviceCode,
  attribute_name: z.string().min(1).max(160),
  max_results: z.number().int().min(1).max(1000).default(1000),
}).strict();

const awsPriceQuery = z.object({
  provider: z.literal('aws'),
  query_id: z.string().min(1).max(100),
  service_code: serviceCode,
  region: region.default('global'),
  filters: z.record(z.string()).default({}),
  max_results: z.number().int().min(11).max(1000).default(100).describe(
    'Fetch cap. Minimum 11 lets the MCP prove whether a result set exceeds the 10-item return boundary.',
  ),
  pricing_model: z.enum(['on_demand', 'reserved']).default('on_demand'),
  term_years: z.union([z.literal(1), z.literal(3)]).optional(),
  payment_option: z.enum(['no_upfront', 'partial_upfront', 'all_upfront']).optional(),
  offering_class: z.enum(['standard', 'convertible']).optional(),
}).strict();

const azurePriceQuery = z.object({
  provider: z.literal('azure'),
  query_id: z.string().min(1).max(100),
  filter: z.string().max(4000).optional(),
  currency_code: z.string().regex(/^[A-Z]{3}$/).default('USD'),
  api_version: z.enum(['2021-10-01', '2023-01-01-preview']).default('2023-01-01-preview'),
  next_page_url: z.string().url().max(8000).optional(),
}).strict();

const ociPriceQuery = z.object({
  provider: z.literal('oci'),
  query_id: z.string().min(1).max(100),
  part_number: z.string().min(1).max(120).optional(),
  currency_code: z.string().regex(/^[A-Z]{3}$/).default('USD'),
}).strict();

const gcpPriceQuery = z.object({
  provider: z.literal('gcp'),
  query_id: z.string().min(1).max(100),
  operation: z.enum(['list_services', 'list_skus']),
  service_id: z.string().regex(/^[A-Za-z0-9._-]{1,240}$/).optional(),
  page_size: z.number().int().min(1).max(5000).default(5000),
  page_token: z.string().max(4000).optional(),
  currency_code: z.string().regex(/^[A-Z]{3}$/).default('USD'),
}).strict();

const priceQuery = z.discriminatedUnion('provider', [
  awsPriceQuery,
  azurePriceQuery,
  ociPriceQuery,
  gcpPriceQuery,
]);

const getPricesInput = z.object({
  queries: z.array(priceQuery).min(1).max(50),
  relay_job_id: z.string().regex(/^gpt-[a-f0-9]{32}$/).optional(),
  submission_code: z.string().regex(/^[1-9]$/).optional(),
  price_batch_id: z.string().regex(/^aqpb_[a-f0-9-]{36}$/).optional().describe(
    'Saved batch to extend during resume. Existing successful query_ids are reused.',
  ),
}).strict();

const fact = z.object({
  fact_id: factId,
  component_key: componentKey,
  field: z.string().min(1).max(160),
  value: jsonValue,
  unit: z.string().min(1).max(80),
  scope: z.enum(['total', 'per_instance', 'per_node', 'per_gateway', 'per_month']),
  cleaned_evidence: z.string().min(1).max(500),
  disposition: z.enum(['billable', 'selection', 'context', 'zero_cost']).default('billable'),
}).strict();

const scenarioKey = z.enum([
  'on_demand',
  'one_year_commitment',
  'three_year_commitment',
]);

const officialPriceEvidence = z.object({
  query_id: z.string().min(1).max(100),
  official_item_ids: z.array(z.string().min(1).max(500)).min(1).max(100),
}).strict();

const componentScenarioCost = z.object({
  scenario_key: scenarioKey,
  pricing_basis: z.enum(['on_demand', 'reserved', 'provider_commitment', 'on_demand_fallback']),
  monthly_cost: z.string().regex(/^\d+(?:\.\d{1,10})?$/),
  upfront_cost: z.string().regex(/^\d+(?:\.\d{1,10})?$/).default('0'),
  price_query_ids: z.array(z.string().min(1).max(100)).min(1).max(30).optional(),
  price_evidence: z.array(officialPriceEvidence).min(1).max(30).optional(),
}).strict();

const quoteScenarioTotal = z.object({
  scenario_key: scenarioKey,
  monthly_total: z.string().regex(/^\d+(?:\.\d{1,10})?$/),
  upfront_total: z.string().regex(/^\d+(?:\.\d{1,10})?$/).default('0'),
}).strict();

const customerFacingService = z.object({
  service_name: z.string().min(1).max(120).describe('简明中文服务名称，可保留 AWS 产品名。'),
  model_or_plan: z.string().min(1).max(160).optional().describe('客户可读的实例型号或计费方案。'),
  quantity: z.string().min(1).max(80).optional().describe('客户可读的资源数量。'),
  requirement_summary: z.string().min(1).max(800).describe('客户可直接阅读的中文需求摘要。'),
  configuration_summary: z.string().min(1).max(1000).describe('客户可直接阅读的中文最终配置摘要。'),
  reference_unit_price: z.string().min(1).max(120).optional().describe('有清晰单价时填写客户可读参考单价。'),
}).strict();

const pricedService = z.object({
  component_key: componentKey,
  region: region.optional(),
  instance: z.string().min(1).max(160).optional(),
  group: z.string().min(1).max(160).optional(),
  price_query_ids: z.array(z.string().min(1).max(100)).min(1).max(30).optional(),
  price_evidence: z.array(officialPriceEvidence).min(1).max(30).optional().describe(
    'GPT 从官方原始结果中选中的查询与 SKU/价格项身份。',
  ),
  fact_ids: z.array(factId).min(1).max(100).describe(
    '该组件在 ResourceIR、BillingUsageIR 和 PriceIR 中消费的客户事实 ID。',
  ),
  monthly_cost: z.string().regex(/^\d+(?:\.\d{1,10})?$/).optional().describe(
    'GPT 根据本批官方价格计算的组件月费。',
  ),
  expected_monthly_cost: z.string().regex(/^\d+(?:\.\d{1,10})?$/).optional().describe(
    '旧会话兼容字段；新调用请使用 monthly_cost。',
  ),
  scenario_costs: z.array(componentScenarioCost).min(1).max(3).optional().describe(
    '销售所选方案的逐组件官方费用。',
  ),
  customer_facing: customerFacingService,
}).strict();

const zeroCostEvidence = z.object({
  source: z.enum(['official_documentation', 'official_price_catalog']),
  reference: z.string().min(1).max(500).describe('GPT 提供的简短官方依据说明。'),
}).strict();

const zeroCostService = z.object({
  component_key: componentKey,
  region: region.optional(),
  fact_ids: z.array(factId).min(1).max(100),
  pricing_basis: z.literal('official_no_additional_charge'),
  official_evidence: zeroCostEvidence,
  customer_facing: customerFacingService,
}).strict();

const quoteAdjustment = z.object({
  component_key: componentKey,
  customer_requirement: z.string().min(1).max(500).describe('客户能看懂的原需求简述。'),
  quoted_configuration: z.string().min(1).max(500).describe('客户能看懂的最终配置简述。'),
  reason: z.string().min(1).max(800).describe('用中文大白话说明调整原因，不得出现程序内部术语。'),
  price_impact: z.string().min(1).max(300).optional().describe('用中文说明费用是否已计入以及计入哪一项。'),
}).strict();

const buildEstimateInput = z.object({
  quote_name: z.string().min(1).max(160),
  cloud_provider: cloudProvider,
  relay_job_id: z.string().regex(/^gpt-[a-f0-9]{32}$/).optional().describe('销售前端提供的内部任务编号，用于撤回后的交付保护。'),
  default_region: region,
  currency: z.string().regex(/^[A-Z]{3}$/).default('USD'),
  price_batch_id: z.string().regex(/^aqpb_[a-f0-9-]{36}$/),
  display_result_on_page: z.boolean().optional().describe(
    '兼容字段；当前所有报价均生成 Excel 并返回销售页面，不发送 WebHook。',
  ),
  expected_monthly_total: z.string().regex(/^\d+(?:\.\d{1,10})?$/),
  pricing_scenarios: z.array(quoteScenarioTotal).min(1).max(3).optional().describe(
    '销售所选方案的整单合计；每一项必须等于全部 services[].scenario_costs 的机械加总。',
  ),
  fact_ledger: z.array(fact).max(500),
  services: z.array(pricedService).min(1).max(50),
  zero_cost_services: z.array(zeroCostService).max(50).default([]).describe(
    '不产生额外云费用的结构化资源。只能消费 disposition=zero_cost 的客户事实。',
  ),
  assumptions: z.array(z.string().min(1).max(500).describe('客户可直接阅读的中文报价假设。')).max(100).default([]),
  adjustments: z.array(quoteAdjustment).max(200).default([]),
  idempotency_key: z.string().min(12).max(160),
}).strict();

const quoteJobInput = z.object({
  relay_job_id: z.string().regex(/^gpt-[a-f0-9]{32}$/),
  submission_code: z.string().regex(/^[1-9]$/),
}).strict();

function ok(payload) {
  return {
    content: [{ type: 'text', text: JSON.stringify(payload) }],
    structuredContent: payload,
  };
}

function fail(error) {
  const payload = {
    status: 'failed',
    code: error.code || 'astraquote_v3_tool_failed',
    message: error.message || 'AstraQuote tool failed.',
    details: error.details || {},
    retryable: ['backend_timeout', 'backend_unavailable'].includes(error.code),
  };
  return { isError: true, content: [{ type: 'text', text: JSON.stringify(payload) }] };
}

function guarded(handler) {
  return async (args) => {
    try {
      return ok(await handler(args));
    } catch (error) {
      if (!(error instanceof BackendError)
        && !(error instanceof QuoteDeliveryError)
        && !(error instanceof QuoteStoreError)) {
        error.code = error.code || 'astraquote_v3_workflow_failed';
      }
      return fail(error);
    }
  };
}

function normalizeBuildEstimateInput(input) {
  return {
    ...input,
    services: input.services.map(({
      monthly_cost: monthlyCost,
      expected_monthly_cost: legacyMonthlyCost,
      ...service
    }) => {
      if (monthlyCost !== undefined
        && legacyMonthlyCost !== undefined
        && monthlyCost !== legacyMonthlyCost) {
        const error = new Error('The current and legacy component monthly cost fields disagree.');
        error.code = 'component_monthly_cost_alias_conflict';
        error.details = { component_key: service.component_key };
        throw error;
      }
      return {
        ...service,
        expected_monthly_cost: monthlyCost ?? legacyMonthlyCost,
      };
    }),
  };
}

function buildServer(workflow) {
  const server = new McpServer(
    { name: 'astraquote-official-pricing', version: VERSION },
    { instructions: INSTRUCTIONS },
  );

  server.registerTool('describe_service', {
    title: 'Describe an AWS Price List service',
    description: 'AWS-only auxiliary discovery. It returns official attributes and never chooses a service for GPT.',
    inputSchema: describeServiceInput,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, guarded((args) => workflow.describeService(args)));

  server.registerTool('get_attribute_values', {
    title: 'Get official AWS attribute values',
    description: 'Returns official values of one Price List attribute. It never chooses a value for GPT.',
    inputSchema: attributeValuesInput,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, guarded((args) => workflow.getAttributeValues(args)));

  server.registerTool('get_prices', {
    title: 'Batch query official cloud prices',
    description: 'Dispatches caller-supplied parameters to AWS, Azure, OCI or GCP official price catalogs and returns raw candidates. needs_refinement is non-terminal: GPT must refine the unfinished queries and continue. GPT chooses and calculates.',
    inputSchema: getPricesInput,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, guarded((args) => workflow.getPrices(args)));

  server.registerTool('get_quote_job_status', {
    title: 'Read a resumable quote job checkpoint',
    description: 'Returns only persisted stage, price batch and delivery state. It never reruns a completed step.',
    inputSchema: quoteJobInput,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  }, guarded((args) => workflow.getQuoteJobStatus(args)));

  server.registerTool('resume_quote_job', {
    title: 'Resume a quote job from its saved stage',
    description: 'Returns the next missing action and saved identifiers. It does not restart price queries, files or delivery.',
    inputSchema: quoteJobInput,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  }, guarded((args) => workflow.resumeQuoteJob(args)));

  server.registerTool('build_estimate', {
    title: 'Validate and deliver an official API quote',
    description: 'Checks selected official catalog evidence, fact coverage and GPT-calculated totals, then creates one Excel link and returns it with the quote to the sales page.',
    inputSchema: buildEstimateInput,
    annotations: { readOnlyHint: false, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, guarded((args) => workflow.buildEstimate(normalizeBuildEstimateInput(args))));

  return server;
}

function createApp({ backend, store, deliverer } = {}) {
  const backendClient = backend || new AstraQuoteBackendClient();
  const quoteStore = store || new V2QuoteStore();
  const quoteDeliverer = deliverer || new QuoteDeliveryService();
  const workflow = new AstraQuoteV2Workflow({
    backend: backendClient,
    store: quoteStore,
    deliverer: quoteDeliverer,
  });
  const app = express();
  app.disable('x-powered-by');
  app.use((_req, res, next) => {
    res.set('Cache-Control', 'no-store, no-cache, must-revalidate, proxy-revalidate');
    res.set('Pragma', 'no-cache');
    res.set('Expires', '0');
    next();
  });
  app.use(express.json({ limit: '2mb' }));

  app.get('/healthz', (_req, res) => res.json({ status: 'ok', service: 'astraquote-mcp', version: VERSION }));
  app.get('/readyz', async (_req, res) => {
    try {
      await backendClient.request('/api/mcp/v2/health', { timeoutMs: 5000 });
      res.json({ status: 'ready', version: VERSION, dependencies: ['astraquote-backend'] });
    } catch (error) {
      res.status(503).json({ status: 'not_ready', code: error.code || 'dependency_unavailable' });
    }
  });

  const mcpHandler = async (req, res) => {
    const server = buildServer(workflow);
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    let closed = false;
    const closeRequest = async () => {
      if (closed) return;
      closed = true;
      await server.close().catch(() => transport.close().catch(() => {}));
    };
    const closeOnDisconnect = () => { void closeRequest(); };
    res.once('close', closeOnDisconnect);
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body);
    } catch {
      if (!res.headersSent) res.status(500).json({ error: 'mcp_transport_failure' });
    } finally {
      res.off('close', closeOnDisconnect);
      await closeRequest();
    }
  };
  app.post('/v2/mcp', mcpHandler);
  app.get('/v2/mcp', (_req, res) => res.status(405).send('Method Not Allowed'));
  app.delete('/v2/mcp', (_req, res) => res.status(405).send('Method Not Allowed'));
  return app;
}

if (require.main === module) {
  createApp().listen(PORT, HOST, () => {
    process.stderr.write(`AstraQuote MCP ${VERSION} listening on ${HOST}:${PORT}\n`);
  });
}

module.exports = {
  INSTRUCTIONS,
  VERSION,
  attributeValuesInput,
  buildEstimateInput,
  buildServer,
  createApp,
  customerFacingService,
  describeServiceInput,
  fail,
  getPricesInput,
  guarded,
  normalizeBuildEstimateInput,
  ok,
  priceQuery,
  cloudProvider,
  officialPriceEvidence,
  pricedService,
};
