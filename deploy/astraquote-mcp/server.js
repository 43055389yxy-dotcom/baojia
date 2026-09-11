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

const VERSION = '3.11.0';
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
const cloudProvider = z.enum([
  'aws', 'azure', 'oci', 'gcp',
  'tencent', 'alibaba', 'huawei', 'baidu', 'volcengine', 'ctyun',
]);

const describeServiceInput = z.object({
  service_code: serviceCode.optional().describe(
    'Exact AWS Price List ServiceCode when already known.',
  ),
  search_text: z.string().min(2).max(120).optional().describe(
    'Short official-name fragment used to discover ServiceCode from AWS DescribeServices. Do not pass customer prose.',
  ),
  max_results: z.number().int().min(1).max(1000).default(1000),
}).strict();

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
  currency_code: z.string().regex(/^[A-Z]{3}$/).describe(
    'Official request currency selected by GPT for this quote. No currency is assumed.',
  ),
  api_version: z.enum(['2021-10-01', '2023-01-01-preview']).default('2023-01-01-preview'),
  next_page_url: z.string().url().max(8000).optional(),
}).strict();

const ociPriceQuery = z.object({
  provider: z.literal('oci'),
  query_id: z.string().min(1).max(100),
  part_number: z.string().min(1).max(120).optional(),
  catalog_search: z.string().min(2).max(240).optional().describe(
    'Caller-chosen official product words. AstraQuote mechanically searches Oracle catalog text and returns part-number candidates; it does not select one.',
  ),
  refresh_catalog: z.boolean().default(false).describe(
    'Normally false. Set true only after a cached Oracle identity no longer resolves through the live official API; prices are never read from this identity cache.',
  ),
  currency_code: z.string().regex(/^[A-Z]{3}$/).describe(
    'Official request currency selected by GPT for this quote. No currency is assumed.',
  ),
  response_filters: z.record(z.string().min(1).max(500)).refine(
    (value) => Object.keys(value).length <= 12,
    'At most 12 caller-supplied exact response filters are allowed.',
  ).default({}).describe('Exact dotted official JSON field matches supplied by GPT. The MCP does not choose values.'),
}).strict();

const gcpPriceQuery = z.object({
  provider: z.literal('gcp'),
  query_id: z.string().min(1).max(100),
  operation: z.enum(['list_services', 'list_skus']),
  service_id: z.string().regex(/^[A-Za-z0-9._-]{1,240}$/).optional(),
  page_size: z.number().int().min(1).max(5000).default(5000),
  page_token: z.string().max(4000).optional(),
  currency_code: z.string().regex(/^[A-Z]{3}$/).optional().describe(
    'Official request currency selected by GPT for this quote. No currency is assumed.',
  ),
  response_filters: z.record(z.string().min(1).max(500)).refine(
    (value) => Object.keys(value).length <= 12,
    'At most 12 caller-supplied exact response filters are allowed.',
  ).default({}).describe('Exact dotted official JSON field matches supplied by GPT. The MCP does not choose values.'),
  max_pages: z.number().int().min(1).max(20).default(8).describe(
    'Maximum official pages to scan when response_filters are supplied.',
  ),
}).strict();

function isResponsePath(value) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 360) return false;
  if (value.startsWith('/')) {
    const parts = value.split('/').slice(1);
    return parts.length <= 20 && parts.every((part) => (
      !/[\u0000-\u001f]/.test(part) && !/~(?:[^01]|$)/.test(part)
    ));
  }
  return /^(?:[A-Za-z0-9_-]{1,120})(?:\.[A-Za-z0-9_-]{1,120}){0,19}$/.test(value);
}

const responsePath = z.string().min(1).max(360).refine(
  isResponsePath,
  'Use a dotted field path or RFC 6901 JSON Pointer.',
);

const commercialRateField = z.object({
  unit_price_path: responsePath,
  item_id_path: responsePath.optional(),
  currency_code: z.string().regex(/^[A-Z]{3}$/).optional(),
  currency_path: responsePath.optional(),
  unit: z.string().min(1).max(120).optional(),
  unit_path: responsePath.optional(),
  description_path: responsePath.optional(),
  pricing_model_path: responsePath.optional(),
  tier_start_path: responsePath.optional(),
  tier_end_path: responsePath.optional(),
}).strict().superRefine((value, context) => {
  if (!value.currency_code && !value.currency_path) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: 'Declare currency_path or an official documented currency_code.',
      path: ['currency_code'],
    });
  }
});

const limitedRecord = (maximum, description) => z.record(jsonValue).refine(
  (value) => Object.keys(value).length <= maximum,
  description,
);

const authenticatedCloudPriceQueryShape = {
  query_id: z.string().min(1).max(100),
  route_id: z.string().regex(/^aqr_[a-f0-9]{24}$/).optional().describe(
    'Previously verified official read-only route. Supply current quote parameters; saved quote-specific values are never reused.',
  ),
  endpoint: z.string().min(4).max(255).optional().describe(
    'Official provider API hostname only. Omit it to look up a previously verified route by provider, service and region. Protocol, path, credentials and authorization are forbidden.',
  ),
  service: z.string().regex(/^[A-Za-z0-9._-]{1,80}$/).optional(),
  action: z.string().regex(/^[A-Za-z0-9._-]{0,160}$/).optional(),
  version: z.string().min(1).max(40).optional(),
  region: z.string().min(2).max(80).optional(),
  method: z.enum(['GET', 'POST']).optional(),
  path: z.string().min(1).max(1000).optional(),
  region_parameter: z.string().regex(/^(?:none|[A-Za-z][A-Za-z0-9_.-]{0,119})$/).optional().describe(
    'Optional official region parameter name. Prefer placing exact provider parameters in query_parameters or body.',
  ),
  query_parameters: limitedRecord(100, 'At most 100 official query parameters are allowed.').default({}),
  body: limitedRecord(200, 'At most 200 official request fields are allowed.').default({}),
  response_items_path: responsePath.optional(),
  response_filters: z.record(z.string().min(1).max(500)).refine(
    (value) => Object.keys(value).length <= 12,
    'At most 12 exact official response filters are allowed.',
  ).default({}),
  item_id_paths: z.array(responsePath).max(12).default([]),
  rate_fields: z.array(commercialRateField).max(24).default([]).describe(
    'GPT-declared official response paths. The MCP dereferences them mechanically and never chooses a rate.',
  ),
  next_page_path: responsePath.optional(),
  official_source_url: z.string().url().max(2000).optional().describe(
    'Official API or documentation URL used to verify a newly discovered route.',
  ),
  sdk_version: z.string().min(1).max(120).optional(),
};

const authenticatedCloudPriceQuery = (provider) => z.object({
  provider: z.literal(provider),
  ...authenticatedCloudPriceQueryShape,
}).strict();

const tencentPriceQuery = authenticatedCloudPriceQuery('tencent');
const alibabaPriceQuery = authenticatedCloudPriceQuery('alibaba');
const huaweiPriceQuery = authenticatedCloudPriceQuery('huawei');
const baiduPriceQuery = authenticatedCloudPriceQuery('baidu');
const volcenginePriceQuery = authenticatedCloudPriceQuery('volcengine');
const ctyunPriceQuery = authenticatedCloudPriceQuery('ctyun');

const priceQuery = z.discriminatedUnion('provider', [
  awsPriceQuery,
  azurePriceQuery,
  ociPriceQuery,
  gcpPriceQuery,
  tencentPriceQuery,
  alibabaPriceQuery,
  huaweiPriceQuery,
  baiduPriceQuery,
  volcenginePriceQuery,
  ctyunPriceQuery,
]);

const queryContext = z.object({
  query_id: z.string().min(1).max(100),
  purpose: z.enum(['discovery', 'pricing']).describe(
    'GPT labels catalog exploration as discovery. Pricing attempts belong to a component and billing item.',
  ),
  component_key: componentKey.optional(),
  billing_key: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/).optional().describe(
    'Stable billing item chosen by GPT, such as compute or storage. Different costs must use different keys.',
  ),
  scenario_key: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/).optional(),
  supersedes_query_ids: z.array(z.string().min(1).max(100)).max(100).optional().describe(
    'Explicit legacy attempts for the same billing item. They retire only after a replacement returns usable official rates.',
  ),
}).strict();

const getPricesInputSchema = z.object({
  queries: z.array(priceQuery).max(50).default([]).describe(
    'Non-empty current incremental query group. If omitted accidentally, the tool returns a compact recovery guide instead of a protocol error; retry immediately with real queries. For a long quote, GPT chooses a suitably small group from response complexity and continues the same price_batch_id.',
  ),
  query_contexts: z.array(queryContext).max(500).optional().describe(
    'Task bookkeeping only, never sent to a cloud API. May annotate queries in this call or the saved batch. Same component/billing/scenario scope shares a requirement; successful replacement rates retire old failures without deleting history. Omit for legacy clients.',
  ),
  relay_job_id: z.string().regex(/^gpt-[a-f0-9]{32}$/).optional(),
  submission_code: z.string().regex(/^[1-9]$/).optional(),
  price_batch_id: z.string().regex(/^aqpb_[a-f0-9-]{36}$/).optional().describe(
    'Saved batch to extend during resume. Existing successful query_ids are reused.',
  ),
}).strict();

const getPricesInput = getPricesInputSchema.superRefine((value, context) => {
  value.queries.forEach((query, index) => {
    if (query.provider === 'gcp' && query.operation === 'list_skus' && !query.currency_code) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'GCP SKU price queries require the official request currency.',
        path: ['queries', index, 'currency_code'],
      });
    }
    if (['tencent', 'alibaba', 'huawei', 'baidu', 'volcengine', 'ctyun'].includes(query.provider)
      && !query.route_id) {
      for (const field of ['service', 'region']) {
        if (!query[field]) {
          context.addIssue({
            code: z.ZodIssueCode.custom,
            message: `A new official route requires GPT to provide ${field}; a learned route may use route_id.`,
            path: ['queries', index, field],
          });
        }
      }
    }
  });
});

function parseGetPricesInput(args) {
  const parsed = getPricesInput.safeParse(args);
  if (parsed.success) return parsed.data;
  const error = new Error('Correct the get_prices input fields and retry.');
  error.code = 'request_schema_invalid';
  error.retryable = true;
  error.details = {
    violations: parsed.error.issues.map((issue) => ({
      path: issue.path.join('.'),
      message: issue.message,
    })),
  };
  throw error;
}

function emptyPriceQueryGuide() {
  return {
    status: 'needs_query_plan',
    terminal: false,
    quote_terminal: false,
    must_continue: true,
    code: 'price_queries_required',
    message: 'No official price query was supplied. Build the smallest useful query group and retry now.',
    next_tool: 'get_prices',
    required_argument: 'queries',
    supported_fast_paths: {
      aws: ['describe_service', 'get_attribute_values', 'get_prices'],
      oci: ['get_prices'],
    },
    provider_guidance: {
      aws: 'If ServiceCode is unknown, call describe_service with search_text. Use returned AttributeNames with get_attribute_values, then call get_prices.',
      oci: 'If partNumber is unknown, call get_prices with provider=oci, currency_code, query_id and catalog_search. Reuse the returned exact partNumber.',
    },
  };
}

const getPriceResultsInput = z.object({
  price_batch_id: z.string().regex(/^aqpb_[a-f0-9-]{36}$/),
  query_ids: z.array(z.string().min(1).max(100)).min(1).max(10),
  relay_job_id: z.string().regex(/^gpt-[a-f0-9]{32}$/).optional(),
  submission_code: z.string().regex(/^[1-9]$/).optional(),
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
  official_rate_ids: z.array(z.string().min(1).max(800)).min(1).max(100).optional().describe(
    'GPT 选中的具体官方费率身份。当同一 SKU 同时含免费额度和正常商业费率时必须填写，并且只能选择正常商业费率。',
  ),
}).strict();

const componentScenarioCost = z.object({
  scenario_key: scenarioKey,
  label: z.string().min(1).max(40).optional().describe('GPT 根据本次官方方案给出的客户可读名称。'),
  pricing_basis: z.enum(['on_demand', 'reserved', 'provider_commitment', 'on_demand_fallback']),
  monthly_cost: z.string().regex(/^\d+(?:\.\d{1,10})?$/).describe(
    '该组件按客户要求的全部数量计算后的折合月费，不是单台价格。全预付方案须把整批预付额除以合同月数，并加上该方案未覆盖的持续月费。',
  ),
  upfront_cost: z.string().regex(/^\d+(?:\.\d{1,10})?$/).default('0').describe(
    '该组件按客户要求的全部数量计算的一次性预付总额；没有预付款时填 0。',
  ),
  price_query_ids: z.array(z.string().min(1).max(100)).min(1).max(30).optional(),
  price_evidence: z.array(officialPriceEvidence).min(1).max(30).optional(),
}).strict();

const quoteScenarioTotal = z.object({
  scenario_key: scenarioKey,
  label: z.string().min(1).max(40).optional().describe('GPT 根据本次官方方案给出的客户可读名称。'),
  monthly_total: z.string().regex(/^\d+(?:\.\d{1,10})?$/).describe(
    '整张报价在该方案下的折合月费，必须等于所有组件整批折合月费之和。',
  ),
  upfront_total: z.string().regex(/^\d+(?:\.\d{1,10})?$/).default('0').describe(
    '整张报价在该方案下的一次性预付总额，必须等于所有组件预付总额之和。',
  ),
}).strict();

const customerFacingService = z.object({
  service_name: z.string().min(1).max(120).describe('简明中文服务名称，可保留 AWS 产品名。'),
  model_or_plan: z.string().min(1).max(160).optional().describe('客户可读的实例型号或计费方案。'),
  quantity: z.string().min(1).max(80).optional().describe('客户可读的资源数量。'),
  requirement_summary: z.string().min(1).max(800).describe('客户可直接阅读的中文需求摘要。'),
  configuration_summary: z.string().min(1).max(1000).describe(
    '客户可直接阅读的精简最终配置。只写型号、规格、数量、拓扑、高可用、运行时长和必要计费口径；禁止写 API 查询过程、候选比较、价格高低、未查到某价格后的回退过程、不超配规则或其他内部操作说明。',
  ),
  reference_unit_price: z.string().min(1).max(120).optional().describe(
    '仅填写用于核对的官方单位价格。它不能代替 scenario_costs 中按客户全部数量计算的方案折合月费。',
  ),
}).strict();

const pricedService = z.object({
  component_key: componentKey,
  region: region.optional(),
  instance: z.string().min(1).max(160).optional(),
  group: z.string().min(1).max(160).optional(),
  price_query_ids: z.array(z.string().min(1).max(100)).min(1).max(30).optional(),
  price_evidence: z.array(officialPriceEvidence).min(1).max(30).optional().describe(
    'GPT 从官方原始结果中选中的查询、SKU/价格项及具体费率身份。正式商业报价不得选择 Free Tier、Always Free、免费试用或账户赠送额度。',
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
  price_evidence: z.array(officialPriceEvidence).min(1).max(30).optional().describe(
    '仅当零费用依据来自官方价目目录时填写。必须绑定只含零价、且不存在同 SKU 正常商业费率的官方费率身份。',
  ),
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
  region_adjustment_reason: z.string().min(1).max(500).optional().describe(
    '仅当实际报价地域不同于销售首选地域时填写，说明该地域不能承载整套产品以及 GPT 选择的同站点相邻地域。',
  ),
  currency: z.string().regex(/^[A-Z]{3}$/).describe(
    '整张报价使用的官方币种，必须与所选官方费率证据一致；不得静默换汇。',
  ),
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
  const summary = {};
  for (const key of [
    'status', 'code', 'price_batch_id', 'quote_id', 'next_action',
    'result_count', 'batch_result_count', 'relay_job_id', 'stage',
    'terminal', 'quote_terminal', 'must_continue', 'response_compacted',
    'response_bytes', 'response_bytes_before_compaction', 'response_byte_budget', 'batch_query_count',
    'completed_query_count', 'incomplete_query_count',
    'progress_guidance', 'discovery_query_count', 'superseded_query_ids',
  ]) {
    if (payload?.[key] !== undefined) summary[key] = payload[key];
  }
  if (Array.isArray(payload?.results)) {
    summary.results = payload.results.map((item) => {
      const result = {
        query_id: item?.query_id,
        status: item?.status,
      };
      for (const key of [
        'terminal', 'retryable', 'error_category', 'code', 'reused_route_id',
        'not_found_reason', 'raw_item_count', 'filtered_item_count',
      ]) {
        if (item?.[key] !== undefined) result[key] = item[key];
      }
      if (item?.details?.provider_code) {
        result.provider_code = item.details.provider_code;
      }
      if (item?.recovery) {
        result.recovery = {
          next_action: item.recovery.next_action,
          field: item.recovery.field,
          parameter: item.recovery.parameter,
        };
      }
      if (Array.isArray(item?.pricing_knowledge)) {
        result.pricing_knowledge_count = item.pricing_knowledge.length;
      }
      if (item?.pricing_route_health) {
        result.pricing_route_health = item.pricing_route_health;
      }
      return result;
    });
  }
  if (Array.isArray(payload?.detail_query_ids)) {
    summary.detail_query_ids = payload.detail_query_ids;
  }
  if (payload?.result_access) summary.result_access = payload.result_access;
  if (Array.isArray(payload?.learned_routes)) {
    summary.learned_route_count = payload.learned_routes.length;
    summary.learned_route_ids = payload.learned_routes
      .map((route) => route?.route_id)
      .filter(Boolean);
  }
  return {
    content: [{ type: 'text', text: JSON.stringify(summary) }],
    structuredContent: payload,
  };
}

function fail(error) {
  const retryable = error.retryable === true
    || ['backend_timeout', 'backend_unavailable'].includes(error.code);
  const payload = {
    status: 'failed',
    code: error.code || 'astraquote_v3_tool_failed',
    message: error.message || 'AstraQuote tool failed.',
    details: error.details || {},
    retryable,
    terminal: !retryable,
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
  const learnedRouteInstructions = typeof workflow.routeInstructions === 'function'
    ? workflow.routeInstructions()
    : '';
  const server = new McpServer(
    { name: 'astraquote-official-pricing', version: VERSION },
    { instructions: [INSTRUCTIONS, learnedRouteInstructions].filter(Boolean).join('\n\n') },
  );

  server.registerTool('describe_service', {
    title: 'Discover or describe an AWS Price List service',
    description: 'AWS official catalog discovery. Supply exact service_code when known; otherwise supply a short search_text such as EC2 or RDS. The result gives official ServiceCode candidates, AttributeNames and the exact next tools. AstraQuote never chooses a service for GPT.',
    inputSchema: describeServiceInput,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, guarded((args) => workflow.describeService(args)));

  server.registerTool('get_attribute_values', {
    title: 'Get official AWS attribute values',
    description: 'AWS official catalog discovery after describe_service. Supply one returned service_code and one returned AttributeName. Use the official values to form narrow get_prices filters; AstraQuote never chooses a value for GPT.',
    inputSchema: attributeValuesInput,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, guarded((args) => workflow.getAttributeValues(args)));

  server.registerTool('get_prices', {
    title: 'Batch query official cloud prices',
    description: 'Queries official prices and persists every result. AWS fast path: use service_code, region and narrow filters; if service_code or filter values are unknown, first use describe_service and get_attribute_values. Oracle fast path: use part_number and currency_code; if part_number is unknown, send catalog_search here, choose from the returned official candidates, then retry with the exact part_number. Oracle discovery caches only official identity fields, never prices; set refresh_catalog=true only when a cached identity fails against the live API. Always send a non-empty incremental queries array; an accidental empty call returns a recovery guide and is not terminal. Continue long quotes in the same price_batch_id. If response_compacted=true, fetch only needed query IDs with get_price_results. Authenticated-cloud routes are reused by provider, service and region; discover an official read-only route only when none matches. needs_refinement and correctable failures must be repaired in the current task. GPT alone chooses products, parameters, grouping and totals.',
    // Keep the JSON Schema visible to MCP clients. ZodEffects produced by
    // superRefine serializes as an empty object in the MCP SDK, so cross-field
    // checks run inside the guarded handler instead.
    inputSchema: getPricesInputSchema,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, guarded((args) => {
    const input = parseGetPricesInput(args);
    return input.queries.length > 0 ? workflow.getPrices(input) : emptyPriceQueryGuide();
  }));

  server.registerTool('get_price_results', {
    title: 'Read selected saved official price results',
    description: 'Reads only explicitly requested query IDs from a saved batch. Use it instead of replaying the whole historical batch.',
    inputSchema: getPriceResultsInput,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  }, guarded((args) => workflow.getPriceResults(args)));

  server.registerTool('get_quote_job_status', {
    title: 'Read a resumable quote job checkpoint',
    description: 'Returns only persisted stage, price batch and delivery state. It never reruns a completed step. If the saved state proves the job is permanently unrecoverable, follow the server final-state protocol and emit AQ-QUOTE-FAILED.',
    inputSchema: quoteJobInput,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  }, guarded((args) => workflow.getQuoteJobStatus(args)));

  server.registerTool('resume_quote_job', {
    title: 'Resume a quote job from its saved stage',
    description: 'Returns the next missing action and saved identifiers. It does not restart price queries, files or delivery. If recovery is definitively impossible, follow the server final-state protocol and emit AQ-QUOTE-FAILED.',
    inputSchema: quoteJobInput,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  }, guarded((args) => workflow.resumeQuoteJob(args)));

  server.registerTool('build_estimate', {
    title: 'Validate and deliver an official API quote',
    description: 'Checks selected official catalog evidence, fact coverage and GPT-calculated totals, then creates one Excel link and returns it with the quote to the sales page. A pricing_partial batch is allowed when every required component has usable selected evidence; unused discovery and replaced attempts need not succeed.',
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
  getPricesInputSchema,
  getPriceResultsInput,
  guarded,
  normalizeBuildEstimateInput,
  ok,
  priceQuery,
  cloudProvider,
  officialPriceEvidence,
  pricedService,
};
