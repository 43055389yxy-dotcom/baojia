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
    'build_estimate',
  ]);
  assert.match(INSTRUCTIONS, /GPT.*理解.*选择.*计算/s);
  assert.match(INSTRUCTIONS, /AWS.*Azure.*Oracle.*Google/s);
  assert.doesNotMatch(INSTRUCTIONS, /Calculator|import_estimate|模板映射/i);
});

test('get_prices accepts four provider-specific raw query shapes', async (t) => {
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
        { provider: 'gcp', query_id: 'gcp-1', operation: 'list_services' },
      ],
    },
  });
  assert.equal(result.isError, undefined);
  assert.equal(result.structuredContent.input.queries.length, 4);
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
      quote_name: 'conflict', cloud_provider: 'aws', default_region: 'us-east-1',
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
