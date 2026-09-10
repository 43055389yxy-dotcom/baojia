'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { AstraQuoteV2Workflow } = require('../lib/v2-workflow');
const { V2QuoteStore } = require('../lib/v2-quote-store');

function fixture({ status = 'exact', provider = 'azure', itemIds = ['item-1'] } = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-api-workflow-'));
  const delivered = [];
  const displayed = [];
  const backend = {
    getPrices: async (input) => ({
      status: 'completed',
      result_count: 1,
      results: [{
        query_id: input.queries[0].query_id,
        provider,
        status,
        official_item_ids: itemIds,
        items: itemIds.map((id) => ({ id })),
      }],
    }),
    describeService: async (input) => ({ status: 'exact', input }),
    getAttributeValues: async (input) => ({ status: 'found', input }),
  };
  const deliverer = {
    deliver: async (record) => {
      delivered.push(record);
      return { status: 'delivered', spreadsheet_url: 'https://example.test/quote.xlsx' };
    },
    deliverPageResult: async (record) => {
      displayed.push(record);
      return { status: 'displayed_on_page', page_result: { currency: record.currency } };
    },
  };
  return {
    directory,
    delivered,
    displayed,
    workflow: new AstraQuoteV2Workflow({
      backend,
      store: new V2QuoteStore({ directory }),
      deliverer,
    }),
  };
}

function quoteInput(priceBatchId, {
  provider = 'azure',
  evidence = [{ query_id: 'price-1', official_item_ids: ['item-1'] }],
  monthly = '12.34',
  display = false,
} = {}) {
  return {
    quote_name: 'Official API quote',
    cloud_provider: provider,
    default_region: provider === 'azure' ? 'eastasia' : 'global',
    currency: 'USD',
    price_batch_id: priceBatchId,
    display_result_on_page: display,
    expected_monthly_total: monthly,
    fact_ledger: [{
      fact_id: 'F1',
      component_key: 'cmp_compute_0001',
      field: 'quantity',
      value: 1,
      unit: 'count',
      scope: 'total',
      cleaned_evidence: '云服务器数量：1。',
      disposition: 'billable',
    }],
    services: [{
      component_key: 'cmp_compute_0001',
      region: provider === 'azure' ? 'eastasia' : 'global',
      price_evidence: evidence,
      fact_ids: ['F1'],
      expected_monthly_cost: monthly,
      customer_facing: {
        service_name: '云服务器',
        model_or_plan: '官方型号',
        quantity: '1 台',
        requirement_summary: '云服务器 1 台。',
        configuration_summary: '官方型号 1 台。',
      },
    }],
    zero_cost_services: [],
    assumptions: [],
    adjustments: [],
    idempotency_key: `workflow-test-${provider}-${monthly}-${display}`,
  };
}

async function priceBatch(workflow, provider = 'azure') {
  return workflow.getPrices({
    queries: [{ provider, query_id: 'price-1', filter: 'caller supplied query' }],
  });
}

test('get_prices stores raw official evidence without choosing or calculating', async (t) => {
  const { workflow, directory } = fixture();
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));

  const result = await priceBatch(workflow);

  assert.match(result.price_batch_id, /^aqpb_[a-f0-9-]{36}$/);
  assert.deepEqual(result.results[0].official_item_ids, ['item-1']);
  assert.equal(result.selection_policy, undefined);
  assert.equal(result.selected_item, undefined);
  assert.equal(result.monthly_total, undefined);
});

test('build validates selected official evidence then delivers', async (t) => {
  const { workflow, directory, delivered } = fixture();
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow);

  const result = await workflow.buildEstimate(quoteInput(batch.price_batch_id));

  assert.equal(result.status, 'delivered');
  assert.equal(delivered.length, 1);
  assert.equal(delivered[0].cloud_provider, 'azure');
  assert.equal(delivered[0].verification.status, 'official_price_verified');
  assert.deepEqual(delivered[0].resource_ir[0].price_evidence[0].official_item_ids, ['item-1']);
});

test('an ambiguous official query requires GPT to identify the chosen item', async (t) => {
  const { workflow, directory } = fixture({ status: 'ambiguous', itemIds: ['item-1', 'item-2'] });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow);
  const input = quoteInput(batch.price_batch_id, { evidence: [] });
  input.services[0].price_query_ids = ['price-1'];

  await assert.rejects(
    workflow.buildEstimate(input),
    (error) => error.code === 'official_price_evidence_invalid'
      && error.details.violations.includes(
        'official_item_selection_required:cmp_compute_0001:price-1',
      ),
  );
});

test('GPT may select one official identity from an ambiguous result', async (t) => {
  const { workflow, directory, delivered } = fixture({
    status: 'ambiguous', itemIds: ['item-1', 'item-2'],
  });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow);

  const result = await workflow.buildEstimate(quoteInput(batch.price_batch_id, {
    evidence: [{ query_id: 'price-1', official_item_ids: ['item-2'] }],
  }));

  assert.equal(result.status, 'delivered');
  assert.equal(delivered.length, 1);
});

test('MCP never silently substitutes an item that GPT did not select', async (t) => {
  const { workflow, directory } = fixture({ status: 'ambiguous', itemIds: ['item-1', 'item-2'] });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow);

  await assert.rejects(
    workflow.buildEstimate(quoteInput(batch.price_batch_id, {
      evidence: [{ query_id: 'price-1', official_item_ids: ['item-not-returned'] }],
    })),
    (error) => error.code === 'official_price_evidence_invalid'
      && error.details.violations.some((item) => item.includes('unknown_official_item')),
  );
});

test('official evidence cannot cross cloud providers', async (t) => {
  const { workflow, directory } = fixture({ provider: 'oci' });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow, 'oci');

  await assert.rejects(
    workflow.buildEstimate(quoteInput(batch.price_batch_id, { provider: 'azure' })),
    (error) => error.code === 'official_price_evidence_invalid'
      && error.details.violations.some((item) => item.includes('price_provider_mismatch')),
  );
});

test('GPT component totals are checked but never redistributed by the MCP', async (t) => {
  const { workflow, directory } = fixture();
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow);
  const input = quoteInput(batch.price_batch_id, { monthly: '20.00' });
  input.services[0].expected_monthly_cost = '19.00';

  await assert.rejects(
    workflow.buildEstimate(input),
    (error) => error.code === 'component_monthly_cost_mismatch',
  );
  assert.equal(input.services[0].expected_monthly_cost, '19.00');
});

test('page result mode skips document and webhook delivery', async (t) => {
  const { workflow, directory, delivered, displayed } = fixture();
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow);

  const result = await workflow.buildEstimate(quoteInput(batch.price_batch_id, { display: true }));

  assert.equal(result.status, 'displayed_on_page');
  assert.equal(displayed.length, 1);
  assert.equal(delivered.length, 0);
});

test('a structured zero-cost fact cannot be smuggled into a priced component', async (t) => {
  const { workflow, directory } = fixture();
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow);
  const input = quoteInput(batch.price_batch_id);
  input.fact_ledger[0].disposition = 'zero_cost';

  await assert.rejects(
    workflow.buildEstimate(input),
    (error) => error.code === 'official_api_fact_mapping_invalid'
      && error.details.violations.some((item) => item.includes('zero_cost_fact_in_priced_service')),
  );
});
