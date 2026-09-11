'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { AstraQuoteV2Workflow } = require('../lib/v2-workflow');
const { V2QuoteStore } = require('../lib/v2-quote-store');

function fixture({
  status = 'exact', provider = 'azure', itemIds = ['item-1'], rateCandidates = [],
} = {}) {
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
        official_rate_candidates: rateCandidates,
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
    backend,
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
  currency = 'USD',
} = {}) {
  return {
    quote_name: 'Official API quote',
    cloud_provider: provider,
    default_region: provider === 'azure' ? 'eastasia' : 'global',
    currency,
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

test('resuming a price batch queries only unfinished ids and reuses successful results', async (t) => {
  const { workflow, directory, backend } = fixture({ status: 'needs_refinement', itemIds: [] });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const query = { provider: 'azure', query_id: 'price-1', filter: 'caller supplied query' };
  let calls = 1;
  const first = await workflow.getPrices({ queries: [query] });
  assert.equal(first.status, 'needs_refinement');
  assert.equal(first.terminal, false);
  assert.equal(first.next_action, 'refine_incomplete_queries');

  backend.getPrices = async (input) => {
    calls += 1;
    return {
      status: 'completed',
      result_count: 1,
      results: [{
        query_id: input.queries[0].query_id,
        provider: 'azure',
        status: 'exact',
        official_item_ids: ['item-1'],
        items: [{ id: 'item-1' }],
      }],
    };
  };
  const resumed = await workflow.getPrices({
    price_batch_id: first.price_batch_id,
    queries: [query],
  });
  assert.equal(resumed.price_batch_id, first.price_batch_id);
  assert.deepEqual(resumed.queried_query_ids, ['price-1']);
  assert.equal(resumed.results[0].status, 'exact');

  const replayed = await workflow.getPrices({
    price_batch_id: first.price_batch_id,
    queries: [query],
  });
  assert.equal(calls, 2);
  assert.deepEqual(replayed.reused_query_ids, ['price-1']);
  assert.deepEqual(replayed.queried_query_ids, []);
});

test('get_prices persists learned official routes and can reuse a route id', async (t) => {
  const { workflow, directory, backend } = fixture({ provider: 'alibaba' });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  let received;
  backend.getPrices = async (input) => {
    received = input.queries[0];
    return {
      status: 'completed', result_count: 1, results: [{
        query_id: received.query_id, provider: 'alibaba', status: 'exact',
        official_item_ids: ['item-1'], items: [{ id: 'item-1' }],
        route_verification: {
          route_contract_version: 2,
          route_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          provider: 'alibaba', endpoint: 'business.aliyuncs.com', service: 'bssopenapi',
          action: 'QueryPrice', version: '2017-12-14', region: 'ap-southeast-1',
          region_parameter: 'Region', method: 'POST', path: '/',
          response_items_path: 'Data.Items', item_id_paths: ['Id'], rate_fields: [],
          auth_scheme: 'alibaba_rpc_hmac_sha1', request_schema_hash: 'sha256:req',
          response_schema_hash: 'sha256:res', sdk_version: 'astraquote-direct-signer/1',
          official_source_url: 'https://help.aliyun.com/document_detail/87913.html',
          last_verified_at: '2026-09-11T00:00:00.000Z',
          revalidate_after: '2026-09-18T00:00:00.000Z',
          expires_at: '2026-10-11T00:00:00.000Z', confidence: 0.75, failure_count: 0,
        },
      }],
    };
  };

  const first = await workflow.getPrices({ queries: [{
    provider: 'alibaba', query_id: 'route-first', endpoint: 'business.aliyuncs.com',
    service: 'bssopenapi', action: 'QueryPrice', version: '2017-12-14',
    region: 'ap-southeast-1', region_parameter: 'Region', method: 'POST', path: '/',
    query_parameters: { ProductCode: 'ecs' }, body: {}, response_filters: {},
  }] });
  const routeId = first.learned_routes[0].route_id;

  await workflow.getPrices({ queries: [{
    provider: 'alibaba', query_id: 'route-reused', route_id: routeId,
    region: 'ap-southeast-1', query_parameters: { ProductCode: 'rds' },
    body: {}, response_filters: {},
  }] });

  assert.equal(received.route_id, undefined);
  assert.equal(received.endpoint, 'business.aliyuncs.com');
  assert.equal(received.action, 'QueryPrice');
  assert.deepEqual(received.query_parameters, { ProductCode: 'rds' });
});

test('build validates selected official evidence then delivers', async (t) => {
  const { workflow, directory, delivered } = fixture();
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow);

  const result = await workflow.buildEstimate(quoteInput(batch.price_batch_id));

  assert.equal(result.status, 'displayed_on_page');
  assert.equal(delivered.length, 0);
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

  assert.equal(result.status, 'displayed_on_page');
  assert.equal(delivered.length, 0);
});

test('a priced service cannot use a free allowance tier from an official catalog item', async (t) => {
  const rateCandidates = [
    {
      rate_id: 'oci:B93297:free', official_item_id: 'item-1',
      unit_price: '0', currency: 'USD', is_zero_rate: true,
    },
    {
      rate_id: 'oci:B93297:paid', official_item_id: 'item-1',
      unit_price: '0.01', currency: 'USD', is_zero_rate: false,
    },
  ];
  const { workflow, directory } = fixture({ provider: 'oci', rateCandidates });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow, 'oci');

  await assert.rejects(
    workflow.buildEstimate(quoteInput(batch.price_batch_id, {
      provider: 'oci', monthly: '0',
      evidence: [{
        query_id: 'price-1',
        official_item_ids: ['item-1'],
        official_rate_ids: ['oci:B93297:free'],
      }],
    })),
    (error) => error.code === 'official_price_evidence_invalid'
      && error.details.violations.includes(
        'free_or_zero_rate_forbidden:cmp_compute_0001:price-1:oci:B93297:free',
      ),
  );
});

test('an item containing free and paid tiers requires GPT to bind the paid rate', async (t) => {
  const rateCandidates = [
    {
      rate_id: 'oci:B93297:free', official_item_id: 'item-1',
      unit_price: '0', currency: 'USD', is_zero_rate: true,
    },
    {
      rate_id: 'oci:B93297:paid', official_item_id: 'item-1',
      unit_price: '0.01', currency: 'USD', is_zero_rate: false,
    },
  ];
  const { workflow, directory } = fixture({ provider: 'oci', rateCandidates });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow, 'oci');

  await assert.rejects(
    workflow.buildEstimate(quoteInput(batch.price_batch_id, { provider: 'oci' })),
    (error) => error.code === 'official_price_evidence_invalid'
      && error.details.violations.includes(
        'paid_rate_selection_required:cmp_compute_0001:price-1:item-1',
      ),
  );

  const result = await workflow.buildEstimate(quoteInput(batch.price_batch_id, {
    provider: 'oci', monthly: '18.98',
    evidence: [{
      query_id: 'price-1',
      official_item_ids: ['item-1'],
      official_rate_ids: ['oci:B93297:paid'],
    }],
  }));
  assert.equal(result.status, 'displayed_on_page');
});

test('a mixed free-tier catalog SKU cannot be disguised as a zero-cost service', async (t) => {
  const rateCandidates = [
    {
      rate_id: 'oci:B93297:free', official_item_id: 'item-1',
      unit_price: '0', currency: 'USD', is_zero_rate: true,
    },
    {
      rate_id: 'oci:B93297:paid', official_item_id: 'item-1',
      unit_price: '0.01', currency: 'USD', is_zero_rate: false,
    },
  ];
  const { workflow, directory } = fixture({ provider: 'oci', rateCandidates });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow, 'oci');
  const input = quoteInput(batch.price_batch_id, { provider: 'oci' });
  input.services[0].price_evidence = [{
    query_id: 'price-1',
    official_item_ids: ['item-1'],
    official_rate_ids: ['oci:B93297:paid'],
  }];
  input.fact_ledger.push({
    fact_id: 'F2',
    component_key: 'cmp_oci_a1_0002',
    field: 'quantity',
    value: 1,
    unit: 'count',
    scope: 'total',
    cleaned_evidence: 'A1 云服务器数量：1。',
    disposition: 'zero_cost',
  });
  input.zero_cost_services = [{
    component_key: 'cmp_oci_a1_0002',
    region: 'eu-dublin-1',
    fact_ids: ['F2'],
    pricing_basis: 'official_no_additional_charge',
    official_evidence: {
      source: 'official_price_catalog',
      reference: 'Oracle A1 Always Free 零价额度。',
    },
    price_evidence: [{
      query_id: 'price-1',
      official_item_ids: ['item-1'],
      official_rate_ids: ['oci:B93297:free'],
    }],
    customer_facing: {
      service_name: 'OCI Compute',
      model_or_plan: 'VM.Standard.A1.Flex',
      quantity: '1 台',
      requirement_summary: 'A1 云服务器 1 台。',
      configuration_summary: 'A1 云服务器。',
    },
  }];

  await assert.rejects(
    workflow.buildEstimate(input),
    (error) => error.code === 'official_zero_cost_evidence_invalid'
      && error.details.violations.includes(
        'free_allowance_not_zero_cost:cmp_oci_a1_0002:price-1:item-1',
      ),
  );
});

test('free-tier documentation cannot justify a commercial zero-cost line', async (t) => {
  const { workflow, directory } = fixture({ provider: 'oci' });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow, 'oci');
  const input = quoteInput(batch.price_batch_id, { provider: 'oci' });
  input.fact_ledger.push({
    fact_id: 'F2', component_key: 'cmp_free_0002', field: 'quantity', value: 1,
    unit: 'count', scope: 'total', cleaned_evidence: '云服务器数量：1。',
    disposition: 'zero_cost',
  });
  input.zero_cost_services = [{
    component_key: 'cmp_free_0002',
    fact_ids: ['F2'],
    pricing_basis: 'official_no_additional_charge',
    official_evidence: {
      source: 'official_documentation',
      reference: '该资源使用 Free Tier 免费额度。',
    },
    customer_facing: {
      service_name: '云服务器', requirement_summary: '云服务器 1 台。',
      configuration_summary: '云服务器 1 台。',
    },
  }];

  await assert.rejects(
    workflow.buildEstimate(input),
    (error) => error.code === 'official_zero_cost_evidence_invalid'
      && error.details.violations.includes(
        'free_allowance_documentation_forbidden:cmp_free_0002',
      ),
  );
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

test('provider commitment labels and basis are supplied by GPT instead of hardcoded by provider', async (t) => {
  const { workflow, directory } = fixture();
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow);
  const input = quoteInput(batch.price_batch_id);
  input.pricing_scenarios = [{
    scenario_key: 'one_year_commitment', label: '1 年官方承诺方案',
    monthly_total: '12.34', upfront_total: '148.08',
  }];
  input.services[0].scenario_costs = [{
    scenario_key: 'one_year_commitment', pricing_basis: 'reserved',
    monthly_cost: '12.34', upfront_cost: '148.08',
    price_evidence: [{ query_id: 'price-1', official_item_ids: ['item-1'] }],
  }];

  const result = await workflow.buildEstimate(input);
  assert.equal(result.status, 'displayed_on_page');
});

test('a signed cloud quote must bind the positive official commercial rate selected by GPT', async (t) => {
  const positiveRate = {
    rate_id: 'tencent:item-1:paid',
    official_item_id: 'item-1',
    unit_price: '0.25',
    currency: 'CNY',
    unit: 'hour',
    is_zero_rate: false,
  };
  const { workflow, directory } = fixture({
    provider: 'tencent',
    rateCandidates: [positiveRate],
  });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow, 'tencent');

  await assert.rejects(
    workflow.buildEstimate(quoteInput(batch.price_batch_id, { provider: 'tencent' })),
    (error) => error.code === 'official_price_evidence_invalid'
      && error.details.violations.some((item) => item.includes('commercial_rate_selection_required')),
  );

  const input = quoteInput(batch.price_batch_id, {
    provider: 'tencent',
    currency: 'CNY',
    evidence: [{
      query_id: 'price-1',
      official_item_ids: ['item-1'],
      official_rate_ids: [positiveRate.rate_id],
    }],
  });
  const result = await workflow.buildEstimate(input);
  assert.equal(result.status, 'displayed_on_page');
});

test('the quote currency must match the selected official commercial rate', async (t) => {
  const positiveRate = {
    rate_id: 'tencent:item-1:paid', official_item_id: 'item-1',
    unit_price: '0.25', currency: 'CNY', unit: 'hour', is_zero_rate: false,
  };
  const { workflow, directory } = fixture({ provider: 'tencent', rateCandidates: [positiveRate] });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow, 'tencent');
  await assert.rejects(
    workflow.buildEstimate(quoteInput(batch.price_batch_id, {
      provider: 'tencent', currency: 'USD', evidence: [{
        query_id: 'price-1', official_item_ids: ['item-1'],
        official_rate_ids: [positiveRate.rate_id],
      }],
    })),
    (error) => error.code === 'official_price_evidence_invalid'
      && error.details.violations.some((item) => item.includes('price_currency_mismatch')),
  );
});

test('MCP accepts a GPT-evidenced provider scenario without a provider-specific denylist', async (t) => {
  const { workflow, directory } = fixture({ provider: 'oci' });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow, 'oci');
  const input = quoteInput(batch.price_batch_id, { provider: 'oci' });
  input.pricing_scenarios = [{
    scenario_key: 'one_year_commitment', monthly_total: '12.34', upfront_total: '0',
  }];
  input.services[0].scenario_costs = [{
    scenario_key: 'one_year_commitment', pricing_basis: 'provider_commitment',
    monthly_cost: '12.34', upfront_cost: '0',
    price_evidence: [{ query_id: 'price-1', official_item_ids: ['item-1'] }],
  }];

  const result = await workflow.buildEstimate(input);
  assert.equal(result.status, 'displayed_on_page');
});

test('a component without a commitment discount keeps its on-demand monthly cost', async (t) => {
  const { workflow, directory } = fixture();
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow);
  const input = quoteInput(batch.price_batch_id, { monthly: '230.40' });
  input.pricing_scenarios = [
    { scenario_key: 'on_demand', monthly_total: '230.40', upfront_total: '0' },
    { scenario_key: 'one_year_commitment', monthly_total: '0', upfront_total: '0' },
  ];
  input.services[0].scenario_costs = [
    {
      scenario_key: 'on_demand', pricing_basis: 'on_demand',
      monthly_cost: '230.40', upfront_cost: '0',
      price_evidence: [{ query_id: 'price-1', official_item_ids: ['item-1'] }],
    },
    {
      scenario_key: 'one_year_commitment', pricing_basis: 'on_demand_fallback',
      monthly_cost: '0', upfront_cost: '0',
      price_evidence: [{ query_id: 'price-1', official_item_ids: ['item-1'] }],
    },
  ];

  await assert.rejects(
    workflow.buildEstimate(input),
    (error) => error.code === 'official_price_evidence_invalid'
      && error.details.violations.includes(
        'scenario_fallback_cost_changed:cmp_compute_0001:one_year_commitment',
      ),
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

test('every quote uses the sales-page delivery path', async (t) => {
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

test('a rejected pricing request is checkpointed as correctable instead of terminal', async (t) => {
  const { workflow, backend, directory } = fixture();
  const relayDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-relay-rejected-'));
  const jobsDirectory = path.join(relayDirectory, 'jobs');
  fs.mkdirSync(jobsDirectory);
  const relayJobId = `gpt-${'b'.repeat(32)}`;
  fs.writeFileSync(path.join(jobsDirectory, `${relayJobId}.json`), JSON.stringify({
    job_id: relayJobId,
    submission_code: '4',
    status: 'processing',
    quote_options: { cloud_provider: 'baidu', pricing_scenarios: ['on_demand'] },
  }));
  const previous = process.env.ASTRAQUOTE_GPT_RELAY_DIR;
  process.env.ASTRAQUOTE_GPT_RELAY_DIR = relayDirectory;
  backend.getPrices = async () => {
    const error = new Error('Correct the request fields.');
    error.code = 'request_schema_invalid';
    error.details = {
      violations: [{ path: 'body.queries.0.region_parameter', message: 'Invalid value' }],
    };
    error.retryable = true;
    throw error;
  };
  t.after(() => {
    if (previous === undefined) delete process.env.ASTRAQUOTE_GPT_RELAY_DIR;
    else process.env.ASTRAQUOTE_GPT_RELAY_DIR = previous;
    fs.rmSync(directory, { recursive: true, force: true });
    fs.rmSync(relayDirectory, { recursive: true, force: true });
  });

  await assert.rejects(workflow.getPrices({
    relay_job_id: relayJobId,
    submission_code: '4',
    queries: [{ provider: 'baidu', query_id: 'bcc-price', endpoint: 'bcc.sin.baidubce.com' }],
  }), (error) => error.code === 'request_schema_invalid');

  const status = workflow.getQuoteJobStatus({
    relay_job_id: relayJobId, submission_code: '4',
  });
  assert.equal(status.stage, 'pricing_request_rejected');
  assert.equal(status.error.retryable, true);
  assert.equal(status.error.details.violations[0].path, 'body.queries.0.region_parameter');
  assert.match(workflow.resumeQuoteJob({
    relay_job_id: relayJobId, submission_code: '4',
  }).next_action, /Correct the rejected request/);
});

test('completed relay jobs replay before the processing guard and expose stage-level resume', async (t) => {
  const { workflow, directory, displayed } = fixture();
  const relayDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-relay-resume-'));
  const jobsDirectory = path.join(relayDirectory, 'jobs');
  fs.mkdirSync(jobsDirectory);
  const relayJobId = `gpt-${'a'.repeat(32)}`;
  const relayPath = path.join(jobsDirectory, `${relayJobId}.json`);
  const relay = {
    job_id: relayJobId,
    submission_code: '6',
    status: 'processing',
    quote_options: {
      cloud_provider: 'azure',
      pricing_scenarios: ['on_demand'],
    },
  };
  fs.writeFileSync(relayPath, JSON.stringify(relay));
  const previous = process.env.ASTRAQUOTE_GPT_RELAY_DIR;
  process.env.ASTRAQUOTE_GPT_RELAY_DIR = relayDirectory;
  t.after(() => {
    if (previous === undefined) delete process.env.ASTRAQUOTE_GPT_RELAY_DIR;
    else process.env.ASTRAQUOTE_GPT_RELAY_DIR = previous;
    fs.rmSync(directory, { recursive: true, force: true });
    fs.rmSync(relayDirectory, { recursive: true, force: true });
  });

  const batch = await workflow.getPrices({
    relay_job_id: relayJobId,
    submission_code: '6',
    queries: [{ provider: 'azure', query_id: 'price-1', filter: 'caller supplied query' }],
  });
  assert.equal(workflow.getQuoteJobStatus({
    relay_job_id: relayJobId, submission_code: '6',
  }).stage, 'pricing_completed');

  const input = quoteInput(batch.price_batch_id);
  input.relay_job_id = relayJobId;
  input.pricing_scenarios = [{
    scenario_key: 'on_demand', monthly_total: '12.34', upfront_total: '0',
  }];
  input.services[0].scenario_costs = [{
    scenario_key: 'on_demand', pricing_basis: 'on_demand',
    monthly_cost: '12.34', upfront_cost: '0',
    price_evidence: [{ query_id: 'price-1', official_item_ids: ['item-1'] }],
  }];
  const first = await workflow.buildEstimate(input);
  assert.equal(first.status, 'displayed_on_page');
  relay.status = 'completed';
  fs.writeFileSync(relayPath, JSON.stringify(relay));

  const replay = await workflow.buildEstimate({ ...input, idempotency_key: 'new-key-after-timeout' });
  assert.equal(replay.idempotent_replay, true);
  assert.equal(displayed.length, 1);
  const resumed = workflow.resumeQuoteJob({ relay_job_id: relayJobId, submission_code: '6' });
  assert.equal(resumed.stage, 'delivery_completed');
  assert.match(resumed.next_action, /do not query, generate or deliver again/);
});
