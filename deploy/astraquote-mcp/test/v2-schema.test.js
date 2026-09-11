'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { Client } = require('@modelcontextprotocol/sdk/client/index.js');
const { InMemoryTransport } = require('@modelcontextprotocol/sdk/inMemory.js');
const { buildServer, INSTRUCTIONS } = require('../server');

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
  assert.ok(getPrices.inputSchema.required.includes('queries'));
  assert.equal(getPrices.inputSchema.properties.queries.type, 'array');
  assert.match(JSON.stringify(getPrices.inputSchema.properties.queries), /provider/);
  assert.match(JSON.stringify(getPrices.inputSchema.properties.queries), /query_id/);
  assert.match(INSTRUCTIONS, /GPT.*理解.*选择.*计算/s);
  assert.match(INSTRUCTIONS, /AWS.*Azure.*Oracle.*Google.*腾讯云.*阿里云.*华为云.*百度智能云.*火山引擎.*天翼云/s);
  assert.match(INSTRUCTIONS, /官方文档.*官方 SDK/s);
  assert.match(INSTRUCTIONS, /route_id.*连续失败.*隔离/s);
  assert.match(INSTRUCTIONS, /第三方网页.*绝不能作为价格证据/s);
  assert.match(INSTRUCTIONS, /工具入参校验.*可修正.*重试/s);
  assert.match(INSTRUCTIONS, /queries.*非空/s);
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

test('get_prices accepts a learned route id without repeating transport details', async (t) => {
  const { client, server } = await connectedClient();
  t.after(async () => {
    await client.close();
    await server.close();
  });

  const result = await client.callTool({
    name: 'get_prices',
    arguments: {
      queries: [{
        provider: 'alibaba', query_id: 'alibaba-reuse',
        route_id: 'aqr_aaaaaaaaaaaaaaaaaaaaaaaa',
        region: 'ap-southeast-1', query_parameters: { ProductCode: 'ecs' },
      }],
    },
  });

  assert.equal(result.isError, undefined);
  assert.equal(result.structuredContent.input.queries[0].route_id, 'aqr_aaaaaaaaaaaaaaaaaaaaaaaa');
});

test('get_prices returns cross-field omissions as a retryable tool result', async (t) => {
  const { client, server } = await connectedClient();
  t.after(async () => {
    await client.close();
    await server.close();
  });

  const result = await client.callTool({
    name: 'get_prices',
    arguments: {
      queries: [{ provider: 'alibaba', query_id: 'alibaba-missing-route-fields' }],
    },
  });
  const payload = JSON.parse(result.content[0].text);

  assert.equal(result.isError, true);
  assert.equal(payload.code, 'request_schema_invalid');
  assert.equal(payload.retryable, true);
  assert.equal(payload.terminal, false);
  assert.ok(payload.details.violations.some((item) => item.path === 'queries.0.endpoint'));
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
