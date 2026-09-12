'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { Client } = require('@modelcontextprotocol/sdk/client/index.js');
const { InMemoryTransport } = require('@modelcontextprotocol/sdk/inMemory.js');
const { buildServer, INSTRUCTIONS, ok } = require('../server');

function fakeWorkflow() {
  return {
    describeService: async (input) => ({ status: 'ok', input }),
    getAttributeValues: async (input) => ({ status: 'ok', input }),
    getPrices: async (input) => ({ status: 'completed', input }),
    getPriceResults: async (input) => ({ status: 'completed', input }),
    getQuoteJobStatus: async (input) => ({ status: 'created', input }),
    resumeQuoteJob: async (input) => ({ status: 'created', input }),
    buildEstimate: async (input) => ({ status: 'delivered', input }),
  };
}

async function connectedClient() {
  const server = buildServer(fakeWorkflow());
  const client = new Client({ name: 'astraquote-schema-test', version: '1.0.0' });
  const [clientTransport, serverTransport] = InMemoryTransport.createLinkedPair();
  await server.connect(serverTransport);
  await client.connect(clientTransport);
  return { client, server };
}

test('text-only MCP clients receive the exact official IDs and rates needed to quote', () => {
  const result = ok({
    status: 'completed',
    results: [{
      query_id: 'oci-compute',
      provider: 'oci',
      status: 'exact',
      official_item_ids: ['B12345'],
      items: [{
        partNumber: 'B12345',
        displayName: 'Compute Standard OCPU',
        metricName: 'OCPU Per Hour',
      }],
      official_rate_candidates: [{
        rate_id: 'oci:B12345:rate-1',
        official_item_id: 'B12345',
        unit_price: '0.0255',
        currency: 'USD',
        unit: 'OCPU Per Hour',
        is_zero_rate: false,
      }],
    }],
  });

  const textPayload = JSON.parse(result.content[0].text);
  assert.deepEqual(textPayload.results[0].official_item_ids, ['B12345']);
  assert.equal(textPayload.results[0].official_rate_candidates[0].unit_price, '0.0255');
  assert.equal(textPayload.results[0].official_rate_candidates[0].rate_id, 'oci:B12345:rate-1');
  assert.equal(textPayload.results[0].official_items[0].displayName, 'Compute Standard OCPU');
});

test('text-only MCP clients receive provider error details instead of a bare status', () => {
  const result = ok({
    status: 'needs_refinement',
    results: [{
      query_id: 'tencent-redis',
      provider: 'tencent',
      status: 'query_failed',
      terminal: true,
      retryable: false,
      error_category: 'authorization',
      code: 'tencent_auth_failure_unauthorized_operation',
      message: 'The caller lacks the required read-only pricing permission.',
      details: {
        provider_code: 'AuthFailure.UnauthorizedOperation',
        request_id: 'request-123',
      },
    }],
  });

  const textPayload = JSON.parse(result.content[0].text);
  assert.equal(textPayload.results[0].error_category, 'authorization');
  assert.equal(textPayload.results[0].provider_code, 'AuthFailure.UnauthorizedOperation');
  assert.match(textPayload.results[0].message, /pricing permission/);
});

test('text-only clients see every returned rate and product without an undisclosed preview limit', () => {
  const rates = Array.from({ length: 25 }, (_, index) => ({
    rate_id: `rate-${index}`, official_item_id: `sku-${index % 8}`,
    unit_price: String(index + 1), currency: 'USD', unit: 'GB-Mo',
  }));
  const items = Array.from({ length: 8 }, (_, index) => ({
    sku: `sku-${index}`, attributes: { instanceType: `model-${index}`, capacity: index },
  }));
  const result = ok({ results: [{ query_id: 'many-rates', status: 'exact',
    official_item_ids: items.map((item) => item.sku), official_rate_candidates: rates, items,
  }] });
  const payload = JSON.parse(result.content[0].text);
  assert.deepEqual(payload.results[0].official_rate_candidates.map((rate) => rate.rate_id),
    rates.map((rate) => rate.rate_id));
  assert.equal(payload.results[0].official_items.length, 8);
  assert.equal(payload.results[0].official_items[7].attributes.instanceType, 'model-7');
});

test('text-only clients can read discovery values, refinement guidance and delivered links', () => {
  for (const payload of [
    { status: 'exact', service_code: 'Example', services: [{ ServiceCode: 'Example', AttributeNames: ['usage'] }] },
    { status: 'found', attribute_name: 'usage', values: [{ Value: 'official-value' }] },
    { status: 'displayed_on_page', quote_id: 'quote-1', spreadsheet_url: 'https://example.test/quote.xlsx',
      page_result: { currency: 'USD', is_partial: true } },
  ]) {
    assert.deepEqual(JSON.parse(ok(payload).content[0].text), payload);
  }
  const text = JSON.parse(ok({ results: [{ query_id: 'refine', status: 'needs_refinement',
    refinement_fields: [{ field: 'usage', candidate_values: ['a', 'b'] }],
    recovery: { next_action: 'refine', candidate_values: ['a', 'b'] },
  }] }).content[0].text);
  assert.deepEqual(text.results[0].refinement_fields[0].candidate_values, ['a', 'b']);
  assert.deepEqual(text.results[0].recovery.candidate_values, ['a', 'b']);
});

test('raw official nested item fields remain readable on an explicit detail request', () => {
  const raw = { id: 'sku-1', providerSpecific: { limits: { cpu: 4 } } };
  const result = ok({ results: [{ query_id: 'raw', status: 'exact', include_raw_items: true, items: [raw] }] });
  assert.deepEqual(JSON.parse(result.content[0].text).results[0].official_items, [raw]);
});

test('MCP exposes only official catalog query and delivery tools', async (t) => {
  const { client, server } = await connectedClient();
  t.after(async () => {
    await client.close();
    await server.close();
  });

  const listed = await client.listTools();
  assert.deepEqual(listed.tools.map((tool) => tool.name), [
    'describe_service',
    'get_attribute_values',
    'get_prices',
    'get_price_results',
    'get_quote_job_status',
    'resume_quote_job',
    'build_estimate',
  ]);
  const getPrices = listed.tools.find((tool) => tool.name === 'get_prices');
  const buildEstimate = listed.tools.find((tool) => tool.name === 'build_estimate');
  assert.ok(getPrices.inputSchema.required.includes('queries'));
  assert.ok(getPrices.inputSchema.required.includes('quote_mode'));
  assert.deepEqual(getPrices.inputSchema.properties.quote_mode.enum, ['price_lookup', 'formal_quote']);
  assert.equal(getPrices.inputSchema.properties.queries.type, 'array');
  assert.equal(getPrices.inputSchema.properties.queries.maxItems, 50);
  assert.equal(getPrices.inputSchema.properties.relay_batch_index.type, 'integer');
  assert.equal(getPrices.inputSchema.properties.relay_batch_count.type, 'integer');
  assert.equal(buildEstimate.inputSchema.properties.services.maxItems, 200);
  assert.match(JSON.stringify(getPrices.inputSchema.properties.queries), /provider/);
  assert.match(JSON.stringify(getPrices.inputSchema.properties.queries), /query_id/);
  assert.match(INSTRUCTIONS, /GPT.*理解.*选择.*计算/s);
  assert.match(INSTRUCTIONS, /AWS.*Azure.*Oracle.*Google.*腾讯云.*阿里云.*华为云.*百度智能云.*火山引擎.*天翼云/s);
  assert.match(INSTRUCTIONS, /官方文档.*官方 SDK/s);
  assert.doesNotMatch(INSTRUCTIONS, /route_id|缓存道路|道路级错误/);
  assert.match(INSTRUCTIONS, /第三方网页.*绝不能作为价格证据/s);
  assert.match(INSTRUCTIONS, /工具入参校验.*可修正.*重试/s);
  assert.match(INSTRUCTIONS, /queries.*非空/s);
  assert.match(INSTRUCTIONS, /长报价.*动态.*小批/s);
  assert.match(INSTRUCTIONS, /response_compacted.*get_price_results/s);
  assert.match(INSTRUCTIONS, /同一个.*price_batch_id.*合并/s);
  assert.match(INSTRUCTIONS, /created.*立即执行.*不得只汇报/s);
  assert.match(getPrices.description, /formal quote.*relay_batch_index.*quote_components/is);
  assert.match(getPrices.description, /must_continue.*final answer/is);
  assert.match(getPrices.description, /three.*official API.*official pricing page/is);
  assert.match(buildEstimate.description, /official_page_price_evidence/i);
  assert.doesNotMatch(INSTRUCTIONS, /Calculator|import_estimate|模板映射/i);
});

test('get_prices accepts all ten provider-specific raw query shapes', async (t) => {
  const { client, server } = await connectedClient();
  t.after(async () => {
    await client.close();
    await server.close();
  });

  const result = await client.callTool({
    name: 'get_prices',
    arguments: {
      quote_mode: 'price_lookup',
      queries: [
        {
          provider: 'aws', query_id: 'aws-1', service_code: 'AmazonEC2',
          region: 'ap-northeast-1', filters: { instanceType: 'm7g.large' },
        },
        {
          provider: 'azure', query_id: 'azure-1',
          filter: "serviceName eq 'Virtual Machines'", currency_code: 'USD',
        },
        { provider: 'oci', query_id: 'oci-1', part_number: 'B93113', currency_code: 'USD' },
        {
          provider: 'gcp', query_id: 'gcp-1', operation: 'list_services',
          currency_code: 'USD', response_filters: { displayName: 'Compute Engine' }, max_pages: 4,
        },
        {
          provider: 'tencent', query_id: 'tencent-1', endpoint: 'cvm.tencentcloudapi.com',
          service: 'cvm', action: 'InquiryPriceRunInstances', version: '2017-03-12',
          region: 'ap-guangzhou', response_items_path: 'Response.Price',
          item_id_paths: ['InstanceType'], rate_fields: [{ unit_price_path: 'InstancePrice.UnitPrice', currency_code: 'CNY' }],
        },
        {
          provider: 'alibaba', query_id: 'alibaba-1', endpoint: 'ecs.cn-hangzhou.aliyuncs.com',
          service: 'ecs', action: 'DescribePrice', version: '2014-05-26', region: 'cn-hangzhou',
          response_items_path: 'PriceInfo', item_id_paths: ['Price.TradePrice'],
          rate_fields: [{ unit_price_path: 'Price.TradePrice', currency_code: 'CNY' }],
        },
        {
          provider: 'huawei', query_id: 'huawei-1', endpoint: 'bss.myhuaweicloud.com',
          service: 'bss', region: 'cn-north-4', path: '/v2/inquiry/price',
          response_items_path: 'official.items', item_id_paths: ['id'],
          rate_fields: [{ unit_price_path: 'amount', currency_code: 'CNY' }],
        },
        {
          provider: 'baidu', query_id: 'baidu-1', endpoint: 'billing.baidubce.com',
          service: 'billing', region: 'bj', region_parameter: 'none', path: '/v1/price/query',
          response_items_path: '/result/items', item_id_paths: ['/id'],
          rate_fields: [{ unit_price_path: '/price', currency_path: '/currency' }],
        },
        {
          provider: 'volcengine', query_id: 'volcengine-1', endpoint: 'open.volcengineapi.com',
          service: 'billing', action: 'QueryPriceForPayAsYouGo', version: '2022-01-01',
          region: 'cn-beijing', response_items_path: 'Result.Items', item_id_paths: ['Id'],
          rate_fields: [{ unit_price_path: 'Price', currency_code: 'CNY' }],
        },
        {
          provider: 'ctyun', query_id: 'ctyun-1', endpoint: 'ctapi-global.ctapi.ctyun.cn',
          service: 'ecs', region: 'bb9fdb42056f11eda1610242ac110002',
          path: '/v4/order/new-query-price', response_items_path: 'returnObj',
          item_id_paths: ['masterOrderId'], rate_fields: [{ unit_price_path: 'totalPrice', currency_code: 'CNY' }],
        },
      ],
    },
  });
  assert.equal(result.isError, undefined);
  assert.equal(result.structuredContent.input.queries.length, 10);
  assert.equal(result.structuredContent.input.queries[3].response_filters.displayName, 'Compute Engine');
});

test('get_prices requires every authenticated cloud call to declare its live API route', async (t) => {
  const { client, server } = await connectedClient();
  t.after(async () => {
    await client.close();
    await server.close();
  });

  const result = await client.callTool({
    name: 'get_prices',
    arguments: {
      quote_mode: 'price_lookup',
      queries: [{
        provider: 'alibaba', query_id: 'alibaba-auto-reuse',
        service: 'bssopenapi', region: 'ap-southeast-1',
        query_parameters: { ProductCode: 'ecs' },
      }],
    },
  });

  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /endpoint/);

  const tool = (await client.listTools()).tools.find((item) => item.name === 'get_prices');
  assert.doesNotMatch(JSON.stringify(tool.inputSchema.properties.queries), /route_id/);
});

test('query lifecycle metadata is visible in the MCP schema and survives tool validation', async (t) => {
  const { client, server } = await connectedClient();
  t.after(async () => { await client.close(); await server.close(); });
  const tool = (await client.listTools()).tools.find((item) => item.name === 'get_prices');
  assert.equal(tool.inputSchema.properties.query_contexts.type, 'array');
  assert.ok(!tool.inputSchema.required.includes('query_contexts'));
  const queryContexts = [{
    query_id: 'replacement', purpose: 'pricing', component_key: 'cmp_example_0001',
    billing_key: 'storage', scenario_key: 'on_demand', supersedes_query_ids: ['legacy-attempt'],
  }];
  const result = await client.callTool({
    name: 'get_prices', arguments: {
      quote_mode: 'price_lookup',
      queries: [{ provider: 'azure', query_id: 'replacement', currency_code: 'USD', filter: 'valid' }],
      query_contexts: queryContexts,
    },
  });
  assert.equal(result.isError, undefined);
  assert.deepEqual(result.structuredContent.input.query_contexts, queryContexts);
});

test('get_prices schema rejects an authenticated query without its live endpoint', async (t) => {
  const { client, server } = await connectedClient();
  t.after(async () => {
    await client.close();
    await server.close();
  });

  const result = await client.callTool({
    name: 'get_prices',
    arguments: {
      quote_mode: 'price_lookup',
      queries: [{ provider: 'alibaba', query_id: 'alibaba-missing-route-fields' }],
    },
  });
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /endpoint|service/);
});

test('build_estimate carries provider, official item evidence and no calculator fields', async (t) => {
  const { client, server } = await connectedClient();
  t.after(async () => {
    await client.close();
    await server.close();
  });

  const listed = await client.listTools();
  const build = listed.tools.find((tool) => tool.name === 'build_estimate').inputSchema;
  const schemaText = JSON.stringify(build);
  assert.match(schemaText, /cloud_provider/);
  assert.match(schemaText, /official_item_ids/);
  assert.match(schemaText, /official_page_price_evidence/);
  assert.match(schemaText, /api_attempt_query_ids/);
  assert.match(schemaText, /price_batch_id/);
  assert.doesNotMatch(schemaText, /calculator|template_batch|share_url/i);

  const result = await client.callTool({
    name: 'build_estimate',
    arguments: {
      quote_name: 'Azure test quote',
      cloud_provider: 'azure',
      default_region: 'eastasia',
      currency: 'USD',
      price_batch_id: 'aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
      expected_monthly_total: '12.34',
      fact_ledger: [{
        fact_id: 'F1', component_key: 'cmp_vm_0001', field: 'quantity', value: 1,
        unit: 'count', scope: 'total', cleaned_evidence: '云服务器数量：1。',
      }],
      services: [{
        component_key: 'cmp_vm_0001',
        price_evidence: [{ query_id: 'azure-vm', official_item_ids: ['azure:item:1'] }],
        fact_ids: ['F1'],
        monthly_cost: '12.34',
        customer_facing: {
          service_name: 'Azure Virtual Machines',
          requirement_summary: '云服务器 1 台。',
          configuration_summary: '官方型号 1 台。',
        },
      }],
      idempotency_key: 'schema-test-quote-0001',
    },
  });
  assert.equal(result.isError, undefined);
  assert.equal(result.structuredContent.input.services[0].expected_monthly_cost, '12.34');
  assert.equal(result.structuredContent.input.services[0].monthly_cost, undefined);
});

test('build_estimate accepts structured official pricing page evidence for workflow validation', async (t) => {
  const { client, server } = await connectedClient();
  t.after(async () => {
    await client.close();
    await server.close();
  });

  const result = await client.callTool({
    name: 'build_estimate',
    arguments: {
      quote_name: 'Azure official page fallback quote',
      cloud_provider: 'azure',
      default_region: 'eastasia',
      currency: 'USD',
      price_batch_id: 'aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
      expected_monthly_total: '12.34',
      fact_ledger: [{
        fact_id: 'F1', component_key: 'cmp_vm_0001', field: 'quantity', value: 1,
        unit: 'count', scope: 'total', cleaned_evidence: '云服务器数量：1。',
      }],
      services: [{
        component_key: 'cmp_vm_0001',
        official_page_price_evidence: [{
          billing_key: 'compute',
          source_url: 'https://azure.microsoft.com/en-us/pricing/details/virtual-machines/',
          source_title: 'Azure Virtual Machines pricing',
          price_item: 'Linux pay as you go',
          region: 'eastasia',
          currency: 'USD',
          unit_price: '0.12',
          unit: 'instance hour',
          observed_at: '2026-09-12T00:00:00+00:00',
          source_excerpt: 'East Asia Linux pay as you go: USD 0.12 per instance hour.',
          api_attempt_query_ids: ['attempt-1', 'attempt-2', 'attempt-3'],
        }],
        fact_ids: ['F1'],
        monthly_cost: '12.34',
        customer_facing: {
          service_name: 'Azure Virtual Machines',
          requirement_summary: '云服务器 1 台。',
          configuration_summary: '官方型号 1 台。',
        },
      }],
      idempotency_key: 'schema-test-official-page-0001',
    },
  });

  assert.equal(result.isError, undefined);
  assert.equal(
    result.structuredContent.input.services[0].official_page_price_evidence[0].billing_key,
    'compute',
  );
});

test('build_estimate rejects conflicting current and legacy component cost aliases', async (t) => {
  const { client, server } = await connectedClient();
  t.after(async () => {
    await client.close();
    await server.close();
  });

  const result = await client.callTool({
    name: 'build_estimate',
    arguments: {
      quote_name: 'conflict', cloud_provider: 'aws', default_region: 'us-east-1', currency: 'USD',
      price_batch_id: 'aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
      expected_monthly_total: '2', fact_ledger: [],
      services: [{
        component_key: 'cmp_ec2_0001', price_query_ids: ['q1'], fact_ids: ['F1'],
        monthly_cost: '1', expected_monthly_cost: '2',
        customer_facing: {
          service_name: 'EC2', requirement_summary: '1 台。', configuration_summary: '1 台。',
        },
      }],
      idempotency_key: 'schema-test-conflict',
    },
  });
  assert.equal(result.isError, true);
  assert.match(result.content[0].text, /component_monthly_cost_alias_conflict/);
});
