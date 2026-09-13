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

const VERSION = '3.16.0';
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
  endpoint: z.string().min(4).max(255).describe(
    'Official provider API hostname for this live request. Protocol, path, credentials and authorization are forbidden.',
  ),
  service: z.string().regex(/^[A-Za-z0-9._-]{1,80}$/),
  action: z.string().regex(/^[A-Za-z0-9._-]{0,160}$/).optional(),
  version: z.string().min(1).max(40).optional(),
  region: z.string().min(2).max(80),
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
    'Official API or documentation URL used to verify this live request.',
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

const componentBillingScope = z.object({
  billing_key: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/),
  scenario_key: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/).optional(),
}).strict();

const quoteComponentPlan = z.object({
  component_key: componentKey,
  parent_component_key: componentKey.optional(),
  customer_owned_source: z.string().min(1).max(2000).describe(
    '第一遍清洗后仅属于本组件的标准化配置；不得放入整单原文、兄弟组件或清洗前文本。',
  ),
  billing_scopes: z.array(componentBillingScope).min(1).max(30).describe(
    '本组件正式报价必须完成的计费身份。它只用于进度与完整性核对，不发送给云厂商。',
  ),
}).strict();

const savedOfficialPagePriceEvidence = z.object({
  component_key: componentKey,
  billing_key: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/),
  scenario_key: z.enum([
    'on_demand',
    'one_month_subscription',
    'one_year_subscription',
    'one_year_commitment',
    'three_year_commitment',
  ]).optional(),
  source_url: z.string().url().max(2000),
  source_title: z.string().min(1).max(300),
  price_item: z.string().min(1).max(500),
  region,
  currency: z.string().regex(/^[A-Z]{3}$/),
  unit_price: z.string().regex(/^(?:0*[1-9]\d*(?:\.\d{1,12})?|0*\.\d*[1-9]\d*)$/),
  unit: z.string().min(1).max(120),
  observed_at: z.string().datetime({ offset: true }),
  source_excerpt: z.string().min(1).max(1000),
  api_attempt_query_ids: z.array(z.string().min(1).max(100)).min(1).max(30),
}).strict();

const getPricesInputSchema = z.object({
  quote_mode: z.enum(['price_lookup', 'formal_quote']).describe(
    'Required intent. Use formal_quote whenever the user asks for a formal quote, Excel, sales-page delivery, or a multi-component customer quote. Use price_lookup only when the user wants price facts without formal delivery. Never downgrade a formal quote because some prices are missing.',
  ),
  queries: z.array(priceQuery).min(1).max(50).describe(
    'Current incremental query group. For a long quote, GPT chooses a suitably small group from response complexity and continues the same price_batch_id; this is a transport ceiling, not a required batch size.',
  ),
  query_contexts: z.array(queryContext).max(500).optional().describe(
    'Task bookkeeping only, never sent to a cloud API. For a formal quote, provide one context for every query: discovery is explicit; pricing must bind component_key, billing_key and any scenario_key. Same scope shares a requirement, and successful replacement rates retire old failures without deleting history. Omit only for a one-off legacy price lookup.',
  ),
  relay_job_id: z.string().regex(/^gpt-[a-f0-9]{32}$/).optional(),
  submission_code: z.string().regex(/^[1-9]$/).optional(),
  relay_batch_index: z.number().int().min(0).max(199).optional().describe(
    'Program-assigned zero-based component batch index for a pre-split sales relay quote.',
  ),
  relay_batch_count: z.number().int().min(1).max(200).optional().describe(
    'Program-assigned total component chat count for the same pre-split sales relay quote.',
  ),
  quote_components: z.array(quoteComponentPlan).min(1).max(200).optional().describe(
    '正式报价必须提交清洗组件计划。程序预拆分的销售任务只提交当前 relay_batch_index 独占的组件，后台按批次追加并封存；旧任务第一次仍提交整单计划。不得放入其他批次组件。单项查价可以省略。',
  ),
  price_batch_id: z.string().regex(/^aqpb_[a-f0-9-]{36}$/).optional().describe(
    'Saved batch to extend during resume. Existing successful query_ids are reused.',
  ),
  force_capability_recheck: z.boolean().optional().describe(
    'Use only after an administrator has repaired cloud API permissions. It bypasses a recent scoped authorization-denial memory once; it never bypasses provider authorization.',
  ),
  official_page_price_evidence: z.array(savedOfficialPagePriceEvidence).min(1).max(100).optional().describe(
    'When one official API attempt for the same component billing scope returned no complete usable commercial rate, save the price selected from the same provider and account site official pricing page. Resubmit the existing failed query identity; it will not be called again.',
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

const getPriceResultsInput = z.object({
  price_batch_id: z.string().regex(/^aqpb_[a-f0-9-]{36}$/),
  query_ids: z.array(z.string().min(1).max(100)).min(1).max(10),
  detail_offset: z.number().int().min(0).optional().describe(
    'Saved evidence page offset, initially 0. Use each result.detail_page.next_offset to read remaining rates and items.',
  ),
  detail_limit: z.number().int().min(1).max(50).optional().describe(
    'Maximum rates/items per query in this response; defaults to 20. Pagination never changes the saved evidence.',
  ),
  include_raw_items: z.boolean().optional().describe(
    'Set true only when nested official item fields are needed in text-only clients. Normally the text includes identities, scalar attributes and every rate in the current page.',
  ),
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
  'one_month_subscription',
  'one_year_subscription',
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

const officialPagePriceEvidence = z.object({
  billing_key: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/).describe(
    '与 query_contexts 中相同的稳定计费项。程序只核对归属，不替 GPT 选择价格。',
  ),
  scenario_key: scenarioKey.optional(),
  source_url: z.string().url().max(2000).describe(
    'GPT 在官方 API 对同一计费项一次未取得可用费率后选择的云厂商官方 HTTPS 价格页。',
  ),
  source_title: z.string().min(1).max(300),
  price_item: z.string().min(1).max(500).describe('官方页面中的具体计费项目或价格档位。'),
  region,
  currency: z.string().regex(/^[A-Z]{3}$/),
  unit_price: z.string().regex(/^(?:0*[1-9]\d*(?:\.\d{1,12})?|0*\.\d*[1-9]\d*)$/).describe(
    'GPT 从官方价格页选择的正数单位价格；MCP 不据此替 GPT 计算金额。',
  ),
  unit: z.string().min(1).max(120),
  observed_at: z.string().datetime({ offset: true }).describe('GPT 读取官方价格页的时间。'),
  source_excerpt: z.string().min(1).max(1000).describe('足以核对价格、币种、单位和适用范围的简短官方页面摘录。'),
  api_attempt_query_ids: z.array(z.string().min(1).max(100)).min(1).max(30).describe(
    '同一组件、计费项和方案下未取得可用费率的官方 API 查询 ID；一次有效失败即可转官网。',
  ),
}).strict();

const componentScenarioCost = z.object({
  scenario_key: scenarioKey,
  label: z.string().min(1).max(40).optional().describe('GPT 根据本次官方方案给出的客户可读名称。'),
  pricing_basis: z.enum(['on_demand', 'reserved', 'provider_commitment', 'on_demand_fallback']),
  monthly_cost: z.string().regex(/^\d+(?:\.\d{1,10})?$/).describe(
    '该组件按客户要求的全部数量计算后的月度展示金额，不是单台价格。按量和包月填月费；多月方案填整批合同总价除以合同月数后的折合月费，并加上该方案未覆盖的持续月费。',
  ),
  upfront_cost: z.string().regex(/^\d+(?:\.\d{1,10})?$/).default('0').describe(
    '该组件按客户要求的全部数量计算的一次性预付总额；没有预付款时填 0。',
  ),
  price_query_ids: z.array(z.string().min(1).max(100)).min(1).max(30).optional(),
  price_evidence: z.array(officialPriceEvidence).min(1).max(30).optional(),
  official_page_price_evidence: z.array(officialPagePriceEvidence).min(1).max(30).optional().describe(
    '同一计费项的官方 API 一次未取得可用费率后即可使用；仍由 GPT 选价格和计算。',
  ),
}).strict();

const quoteScenarioTotal = z.object({
  scenario_key: scenarioKey,
  label: z.string().min(1).max(40).optional().describe('GPT 根据本次官方方案给出的客户可读名称。'),
  monthly_total: z.string().regex(/^\d+(?:\.\d{1,10})?$/).describe(
    '整张报价在该方案下的月度展示金额，必须等于所有组件同方案月费或折合月费之和。',
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
  official_page_price_evidence: z.array(officialPagePriceEvidence).min(1).max(30).optional().describe(
    'API 优先；同一计费项一次未取得可用费率后，GPT 可提交对应账号站点的云厂商官方价格页证据。',
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

const unpricedService = z.object({
  component_key: componentKey,
  region: region.optional(),
  fact_ids: z.array(factId).min(1).max(100),
  failure_code: z.enum([
    'official_price_unavailable',
    'official_query_failed',
    'unsupported_in_region',
    'retry_limit_reached',
  ]),
  retryable: z.boolean().default(true),
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
    '仅当实际报价地域不同于销售首选地域时填写。实际地域必须是当前云厂商和账号站点的官方地域代码，不得复制其他云厂商同名代码的含义；说明首选地域不能承载整套产品以及所选同站点相邻地域。',
  ),
  currency: z.string().regex(/^[A-Z]{3}$/).describe(
    '整张报价使用的官方币种，必须与所选官方费率证据一致；不得静默换汇。',
  ),
  price_batch_id: z.string().regex(/^aqpb_[a-f0-9-]{36}$/),
  display_result_on_page: z.boolean().optional().describe(
    '兼容字段；当前所有报价均生成 Excel 并返回销售页面，不发送 WebHook。',
  ),
  is_partial: z.boolean().default(false).describe(
    '仅在有限重试后仍有组件无法取得官方价格时设为 true；已成功组件照常交付，未核价组件必须全部列入 unpriced_services。',
  ),
  expected_monthly_total: z.string().regex(/^\d+(?:\.\d{1,10})?$/),
  pricing_scenarios: z.array(quoteScenarioTotal).min(1).max(3).optional().describe(
    '销售所选方案的整单合计；每一项必须等于全部 services[].scenario_costs 的机械加总。',
  ),
  fact_ledger: z.array(fact).max(500),
  services: z.array(pricedService).min(1).max(200),
  zero_cost_services: z.array(zeroCostService).max(200).default([]).describe(
    '不产生额外云费用的结构化资源。只能消费 disposition=zero_cost 的客户事实。',
  ),
  unpriced_services: z.array(unpricedService).max(200).default([]).describe(
    '部分报价中仍未取得官方价格的组件。它们不参与金额合计，禁止按 0 元处理。完整报价必须为空。',
  ),
  assumptions: z.array(z.string().min(1).max(500).describe(
    '仅填写不补就无法正式查价、且 GPT 已从官方允许值中采用最小或最低价取值的必要参数；可省略的参数不得形成假设。',
  )).max(100).default([]),
  adjustments: z.array(quoteAdjustment).max(200).default([]),
  idempotency_key: z.string().min(12).max(160),
}).strict();

const quoteJobInput = z.object({
  relay_job_id: z.string().regex(/^gpt-[a-f0-9]{32}$/),
  submission_code: z.string().regex(/^[1-9]$/),
}).strict();

const RATE_SUMMARY_FIELDS = [
  'rate_id', 'official_item_id', 'unit_price', 'currency', 'unit', 'description',
  'pricing_model', 'purchase_option', 'term_years', 'payment_option',
  'offering_class', 'tier_start', 'tier_end', 'tier_unit', 'is_zero_rate',
];

function compactRateForText(rate) {
  if (!rate || typeof rate !== 'object') return undefined;
  const compact = {};
  for (const key of RATE_SUMMARY_FIELDS) {
    if (rate[key] !== undefined) compact[key] = rate[key];
  }
  return Object.keys(compact).length > 0 ? compact : undefined;
}

function compactOfficialItemForText(item) {
  if (!item || typeof item !== 'object') return undefined;
  const compact = {};
  for (const [key, value] of Object.entries(item)) {
    if (value === null || ['number', 'boolean'].includes(typeof value)
      || (typeof value === 'string' && value.length <= 1000)) compact[key] = value;
  }
  if (item.attributes && typeof item.attributes === 'object' && !Array.isArray(item.attributes)) {
    compact.attributes = Object.fromEntries(
      Object.entries(item.attributes)
        .filter(([, value]) => ['string', 'number', 'boolean'].includes(typeof value)),
    );
  }
  return Object.keys(compact).length > 0 ? compact : undefined;
}

function ok(payload) {
  // Some MCP clients expose content text only. Keep all public non-price
  // fields (discovery values, recovery, progress and delivery URLs) available.
  // Raw official price evidence may need a concise rendering for text-only clients.
  const summary = { ...payload };
  if (Array.isArray(payload?.results)) {
    summary.results = payload.results.map((item) => {
      const result = {
        query_id: item?.query_id,
        provider: item?.provider,
        status: item?.status,
      };
      for (const key of [
        'terminal', 'retryable', 'error_category', 'code',
        'not_found_reason', 'raw_item_count', 'filtered_item_count',
        'detail_page', 'details_available', 'refinement_fields', 'matched_count',
        'official_item_count', 'official_item_id_count', 'official_rate_candidate_count',
        'provider_code', 'cache_status', 'official_price_observed_at',
        'cache_age_seconds', 'source_request_skipped', 'capability_preflight',
      ]) {
        if (item?.[key] !== undefined) result[key] = item[key];
      }
      if (item?.details?.provider_code) {
        result.provider_code = item.details.provider_code;
      }
      if (item?.details?.request_id) result.request_id = item.details.request_id;
      if (typeof item?.message === 'string' && item.message) {
        result.message = item.message.slice(0, 800);
      }
      if (Array.isArray(item?.official_item_ids)) {
        result.official_item_ids = item.official_item_ids;
        result.official_item_id_count = item.official_item_ids.length;
      }
      if (Array.isArray(item?.official_rate_candidates)) {
        result.official_rate_candidates = item.official_rate_candidates
          .map(compactRateForText).filter(Boolean);
        result.official_rate_candidate_count = item.official_rate_candidates.length;
      }
      const officialItems = [item?.items, item?.products, item?.skus, item?.services]
        .find((candidate) => Array.isArray(candidate));
      if (officialItems) {
        result.official_items = item.include_raw_items === true
          ? officialItems : officialItems.map(compactOfficialItemForText).filter(Boolean);
        result.official_item_count = officialItems.length;
        if (item.include_raw_items !== true) {
          result.raw_item_access = { tool: 'get_price_results', include_raw_items: true };
        }
      }
      if (item?.recovery) {
        result.recovery = item.recovery;
      }
      if (Array.isArray(item?.pricing_knowledge)) {
        result.pricing_knowledge_count = item.pricing_knowledge.length;
      }
      return result;
    });
  }
  if (Array.isArray(payload?.detail_query_ids)) {
    summary.detail_query_ids = payload.detail_query_ids;
  }
  if (payload?.result_access) summary.result_access = payload.result_access;
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
    description: 'Always set quote_mode: a request for a formal quote, Excel or sales-page delivery MUST use formal_quote; never downgrade it to price_lookup because some prices are missing. Requires a non-empty incremental queries array. For a formal quote, every query MUST have a query_contexts entry. A pre-split sales relay call MUST preserve relay_batch_index, relay_batch_count and the reserved price_batch_id from its prompt, and quote_components MUST contain only that batch; the backend appends and seals each batch. A legacy formal quote registers the complete plan on its first call. Submit as many prepared scopes as fit this call so independent official requests can run in parallel. Each authenticated-cloud query must contain the complete official endpoint, service, region and current response contract chosen by GPT for that live call; the MCP does not learn or reuse product, country or region API routes. Full official results are persisted. If response_compacted=true, read only required details with get_price_results. Invalid or unsupported parameter values are correctable: repair only the rejected fields and retry. needs_refinement, terminal=false, or must_continue=true means do not give the user a final answer. After one incomplete official API result for the same component/billing/scenario scope, read the provider official pricing page and call get_prices again with the saved query plus top-level official_page_price_evidence; the saved API is not called twice and the fallback becomes available to final merge. GPT chooses products, required minimum parameter values and quote totals; program-assigned sales batches are immutable.',
    // Keep the JSON Schema visible to MCP clients. ZodEffects produced by
    // superRefine serializes as an empty object in the MCP SDK, so cross-field
    // checks run inside the guarded handler instead.
    inputSchema: getPricesInputSchema,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: true },
  }, guarded((args) => workflow.getPrices(parseGetPricesInput(args))));

  server.registerTool('get_price_results', {
    title: 'Read selected saved official price results',
    description: 'Reads explicitly requested query IDs from a saved batch, with complete official evidence paged by detail_offset/detail_limit (default 20). Continue using each result.detail_page.next_offset until null. The text response includes every returned rate and official identity even in clients that hide structuredContent. Do not replay price queries or download a full cloud catalog to recover these details.',
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
    title: 'Validate and deliver an official quote',
    description: 'Checks selected official catalog evidence, fact coverage and GPT-calculated totals, then creates one Excel link and returns it with the quote to the sales page. After one effective official API failure in one declared scope, official_page_price_evidence is accepted from the same provider and account site. If a component still cannot be priced, submit it through the verified partial-quote contract instead of ending with prose only. Unused discovery and replaced attempts need not succeed.',
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
