'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { AstraQuoteV2Workflow, enrichUnpricedServices } = require('../lib/v2-workflow');
const { V2QuoteStore } = require('../lib/v2-quote-store');
const { OfficialPriceCache } = require('../lib/official-price-cache');
const { providerRegionMismatch } = require('../lib/cloud-market-profiles');

function fixture({
  status = 'exact', provider = 'azure', itemIds = ['item-1'], rateCandidates = [],
  resultByteBudget, priceCache,
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
      resultByteBudget,
      priceCache,
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

async function failedPricingAttempts(workflow, count = 3, {
  provider = 'azure', errorCategory,
} = {}) {
  let priceBatchId;
  for (let index = 1; index <= count; index += 1) {
    const queryId = `page-attempt-${index}`;
    const result = await workflow.getPrices({
      ...(priceBatchId ? { price_batch_id: priceBatchId } : {}),
      queries: [{ provider, query_id: queryId, filter: `missing-${index}` }],
      query_contexts: [{
        query_id: queryId, purpose: 'pricing',
        component_key: 'cmp_compute_0001', billing_key: 'compute',
      }],
      ...(!priceBatchId ? {
        quote_components: [{
          component_key: 'cmp_compute_0001',
          customer_owned_source: '云服务器数量：1。',
          billing_scopes: [{ billing_key: 'compute' }],
        }],
      } : {}),
    });
    priceBatchId = result.price_batch_id;
  }
  if (errorCategory) {
    const batch = workflow.store.getPriceBatch(priceBatchId);
    batch.result.results = batch.result.results.map((result) => ({
      ...result,
      status: 'query_failed',
      terminal: errorCategory === 'authorization' || errorCategory === 'credentials',
      retryable: !['authorization', 'credentials'].includes(errorCategory),
      error_category: errorCategory,
      official_item_ids: [],
      official_rate_candidates: [],
    }));
    workflow.store.putPriceBatch(batch);
  }
  return priceBatchId;
}

function officialPageEvidence(attemptIds, overrides = {}) {
  return {
    billing_key: 'compute',
    source_url: 'https://azure.microsoft.com/en-us/pricing/details/virtual-machines/',
    source_title: 'Azure Virtual Machines pricing',
    price_item: 'Linux pay as you go',
    region: 'eastasia',
    currency: 'USD',
    unit_price: '0.12',
    unit: 'instance hour',
    observed_at: new Date().toISOString(),
    source_excerpt: 'East Asia Linux pay as you go: USD 0.12 per instance hour.',
    api_attempt_query_ids: attemptIds,
    ...overrides,
  };
}

test('unpriced components inherit the authoritative provider failure without leaking raw messages', () => {
  const enriched = enrichUnpricedServices({
    unpriced_services: [{
      component_key: 'cmp_redis_0001',
      failure_code: 'official_price_unavailable',
      retryable: true,
    }],
  }, {
    query_contexts: [{ query_id: 'q-redis', component_key: 'cmp_redis_0001' }],
    result: { results: [{
      query_id: 'q-redis', status: 'query_failed', error_category: 'authorization',
      retryable: false, code: 'tencent_official_api_error',
      message: 'secret upstream detail that must not reach sales',
      details: { provider_code: 'AuthFailure' },
    }] },
  });

  assert.equal(enriched[0].failure_category, 'authorization');
  assert.equal(enriched[0].provider_code, 'AuthFailure');
  assert.equal(enriched[0].retryable, false);
  assert.doesNotMatch(JSON.stringify(enriched[0]), /secret upstream detail/);
});

test('unpriced failure selection is generic and prefers a hard blocker over transient noise', () => {
  const enriched = enrichUnpricedServices({
    unpriced_services: [{ component_key: 'cmp_api_0001', retryable: true }],
  }, {
    query_contexts: [
      { query_id: 'q-timeout', component_key: 'cmp_api_0001' },
      { query_id: 'q-invalid', component_key: 'cmp_api_0001' },
    ],
    result: { results: [
      { query_id: 'q-timeout', status: 'query_failed', error_category: 'transport', retryable: true },
      { query_id: 'q-invalid', status: 'query_failed', error_category: 'invalid_request', retryable: true, details: { provider_code: 'InvalidParameter' } },
    ] },
  });

  assert.equal(enriched[0].failure_category, 'invalid_request');
  assert.equal(enriched[0].provider_code, 'InvalidParameter');
  assert.equal(enriched[0].retryable, true);
});

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

test('three failed official API attempts unlock GPT-selected official pricing page evidence', async (t) => {
  const { workflow, directory } = fixture({ status: 'not_found', itemIds: [] });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batchId = await failedPricingAttempts(workflow);
  const input = quoteInput(batchId, { evidence: [] });
  input.idempotency_key = 'official-page-after-three-api-failures';
  input.services[0].official_page_price_evidence = [officialPageEvidence([
    'page-attempt-1', 'page-attempt-2', 'page-attempt-3',
  ])];

  const delivered = await workflow.buildEstimate(input);
  const saved = workflow.store.get(delivered.quote_id);

  assert.equal(saved.verification.official_pricing_page_evidence.used, true);
  assert.equal(saved.verification.official_pricing_page_evidence.count, 1);
  assert.equal(saved.price_ir[0].source, 'official_pricing_page');
  assert.equal(saved.price_ir[0].billing_key, 'compute');
  assert.equal(saved.fact_coverage.F1[0].coverage_type, 'official_price_page');
  assert.equal(saved.billing_usage_ir[0].official_page_price_evidence.length, 1);
});

test('official pricing page evidence stays locked until three qualifying API failures', async (t) => {
  const { workflow, directory } = fixture({ status: 'not_found', itemIds: [] });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batchId = await failedPricingAttempts(workflow, 2);
  const input = quoteInput(batchId, { evidence: [] });
  input.services[0].official_page_price_evidence = [officialPageEvidence([
    'page-attempt-1', 'page-attempt-2', 'page-attempt-2',
  ])];

  await assert.rejects(
    workflow.buildEstimate(input),
    (error) => error.code === 'official_price_evidence_invalid'
      && error.details.violations.some(
        (violation) => violation.startsWith('official_page_requires_three_api_attempts:'),
      ),
  );
});

test('official pricing page fallback rejects third-party hosts and permission failures', async (t) => {
  const thirdParty = fixture({ status: 'not_found', itemIds: [] });
  t.after(() => fs.rmSync(thirdParty.directory, { recursive: true, force: true }));
  const thirdPartyBatch = await failedPricingAttempts(thirdParty.workflow);
  const thirdPartyInput = quoteInput(thirdPartyBatch, { evidence: [] });
  thirdPartyInput.services[0].official_page_price_evidence = [officialPageEvidence([
    'page-attempt-1', 'page-attempt-2', 'page-attempt-3',
  ], { source_url: 'https://azure.microsoft.com.example.test/prices' })];
  await assert.rejects(
    thirdParty.workflow.buildEstimate(thirdPartyInput),
    (error) => error.code === 'official_price_evidence_invalid'
      && error.details.violations.some(
        (violation) => violation.startsWith('official_page_host_not_allowed:'),
      ),
  );

  const denied = fixture({ status: 'not_found', itemIds: [] });
  t.after(() => fs.rmSync(denied.directory, { recursive: true, force: true }));
  const deniedBatch = await failedPricingAttempts(
    denied.workflow, 3, { errorCategory: 'authorization' },
  );
  const deniedInput = quoteInput(deniedBatch, { evidence: [] });
  deniedInput.services[0].official_page_price_evidence = [officialPageEvidence([
    'page-attempt-1', 'page-attempt-2', 'page-attempt-3',
  ])];
  await assert.rejects(
    denied.workflow.buildEstimate(deniedInput),
    (error) => error.code === 'official_price_evidence_invalid'
      && error.details.violations.some(
        (violation) => violation.startsWith('official_page_api_attempt_is_permission_blocker:'),
      ),
  );
});

test('a usable API rate remains preferred over an official pricing page fallback', async (t) => {
  const context = fixture({ status: 'not_found', itemIds: [] });
  t.after(() => fs.rmSync(context.directory, { recursive: true, force: true }));
  const batchId = await failedPricingAttempts(context.workflow);
  context.backend.getPrices = async (input) => ({
    status: 'completed',
    results: input.queries.map((query) => ({
      query_id: query.query_id, provider: query.provider, status: 'exact',
      official_item_ids: ['api-item'],
      official_rate_candidates: [{
        rate_id: 'api-rate', official_item_id: 'api-item', unit_price: '0.11', currency: 'USD',
      }],
    })),
  });
  await context.workflow.getPrices({
    price_batch_id: batchId,
    queries: [{ provider: 'azure', query_id: 'api-success', filter: 'valid' }],
    query_contexts: [{
      query_id: 'api-success', purpose: 'pricing',
      component_key: 'cmp_compute_0001', billing_key: 'compute',
    }],
  });
  const input = quoteInput(batchId, { evidence: [] });
  input.services[0].official_page_price_evidence = [officialPageEvidence([
    'page-attempt-1', 'page-attempt-2', 'page-attempt-3',
  ])];

  await assert.rejects(
    context.workflow.buildEstimate(input),
    (error) => error.code === 'official_price_evidence_invalid'
      && error.details.violations.some(
        (violation) => violation.startsWith('official_page_fallback_forbidden_when_api_price_exists:'),
      ),
  );
});


test('a second identical quote uses the shared exact official price cache', async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-price-cache-flow-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const priceCache = new OfficialPriceCache({ directory: path.join(directory, 'cache') });
  const context = fixture({
    rateCandidates: [{
      rate_id: 'rate-1', official_item_id: 'item-1', unit_price: '12.34', currency: 'USD',
    }],
    priceCache,
  });
  t.after(() => fs.rmSync(context.directory, { recursive: true, force: true }));
  let backendCalls = 0;
  const original = context.backend.getPrices;
  context.backend.getPrices = async (input) => {
    backendCalls += 1;
    return original(input);
  };

  await context.workflow.getPrices({
    queries: [{ provider: 'azure', query_id: 'first', filter: 'same-official-query' }],
  });
  const repeated = await context.workflow.getPrices({
    queries: [{ provider: 'azure', query_id: 'second', filter: 'same-official-query' }],
  });

  assert.equal(backendCalls, 1);
  assert.deepEqual(repeated.price_cache_hit_query_ids, ['second']);
  assert.equal(repeated.results[0].query_id, 'second');
  assert.equal(repeated.results[0].cache_status, 'fresh');
});


test('transient provider outage uses bounded stale official evidence but authorization never does', async (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-stale-flow-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  let now = Date.parse('2026-09-12T00:00:00.000Z');
  const priceCache = new OfficialPriceCache({
    directory: path.join(directory, 'cache'), freshTtlMs: 1_000,
    maximumStaleMs: 60_000, now: () => now,
  });
  const context = fixture({
    rateCandidates: [{
      rate_id: 'rate-1', official_item_id: 'item-1', unit_price: '12.34', currency: 'USD',
    }],
    priceCache,
  });
  t.after(() => fs.rmSync(context.directory, { recursive: true, force: true }));
  await context.workflow.getPrices({
    queries: [{ provider: 'azure', query_id: 'seed', filter: 'same-official-query' }],
  });
  now += 2_000;
  context.backend.getPrices = async (input) => ({ results: input.queries.map((item) => ({
    query_id: item.query_id, provider: item.provider, status: 'query_failed', terminal: false,
    retryable: true, error_category: 'provider_unavailable', code: 'catalog_maintenance',
    message: 'Official catalog is temporarily unavailable.', official_item_ids: [],
  })) });

  const fallback = await context.workflow.getPrices({
    queries: [{ provider: 'azure', query_id: 'outage', filter: 'same-official-query' }],
  });
  assert.equal(fallback.results[0].status, 'exact');
  assert.equal(fallback.results[0].cache_status, 'stale_fallback');
  assert.equal(fallback.results[0].live_error.error_category, 'provider_unavailable');
  const fallbackQuote = quoteInput(fallback.price_batch_id, {
    evidence: [{ query_id: 'outage', official_item_ids: ['item-1'] }],
  });
  fallbackQuote.idempotency_key = 'workflow-stale-official-cache-disclosure';
  const delivered = await context.workflow.buildEstimate(fallbackQuote);
  const saved = context.workflow.store.get(delivered.quote_id);
  assert.equal(saved.verification.cache_fallback.used, true);
  assert.deepEqual(saved.adjustments, []);
  assert.deepEqual(saved.verification.cache_fallback.query_ids, ['outage']);
  assert.doesNotMatch(JSON.stringify(saved.resource_ir), /维护|限流|连接中断|价格快照/);

  context.backend.getPrices = async (input) => ({ results: input.queries.map((item) => ({
    query_id: item.query_id, provider: item.provider, status: 'query_failed', terminal: true,
    retryable: false, error_category: 'authorization', code: 'forbidden',
    message: 'Permission denied.', official_item_ids: [],
  })) });
  const denied = await context.workflow.getPrices({
    queries: [{ provider: 'azure', query_id: 'denied', filter: 'same-official-query' }],
  });
  assert.equal(denied.results[0].status, 'query_failed');
  assert.equal(denied.results[0].cache_status, undefined);
});

test('a legacy partial batch can deliver with selected complete evidence and keeps unused attempts only in the batch', async (t) => {
  const { workflow, backend, directory, displayed } = fixture();
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow);
  backend.getPrices = async () => ({ results: [{
    query_id: 'unused-discovery', provider: 'azure', status: 'not_found', official_item_ids: [],
  }] });
  const partial = await workflow.getPrices({
    price_batch_id: batch.price_batch_id,
    queries: [{ query_id: 'unused-discovery', provider: 'azure', filter: 'missing' }],
  });
  assert.equal(partial.status, 'needs_refinement');
  const result = await workflow.buildEstimate(quoteInput(batch.price_batch_id));
  assert.equal(result.status, 'displayed_on_page');
  assert.equal(displayed.length, 1);
  assert.deepEqual(workflow.store.get(result.quote_id).price_ir.map((r) => r.query_id), ['price-1']);
  assert.equal(workflow.store.getPriceBatch(batch.price_batch_id).result.results.length, 2);
});

test('a partial quote delivers verified components and preserves failed components without pricing them', async (t) => {
  const { workflow, directory, displayed } = fixture({ rateCandidates: [{
    rate_id: 'rate-1', official_item_id: 'item-1', unit_price: '12.34', currency: 'USD',
  }] });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await workflow.getPrices({
    queries: [{ provider: 'azure', query_id: 'price-1', filter: 'caller supplied query' }],
    query_contexts: [{
      query_id: 'price-1', purpose: 'pricing',
      component_key: 'cmp_compute_0001', billing_key: 'compute',
    }],
    quote_components: [
      {
        component_key: 'cmp_compute_0001',
        customer_owned_source: '云服务器：1 台。',
        billing_scopes: [{ billing_key: 'compute' }],
      },
      {
        component_key: 'cmp_storage_0002',
        customer_owned_source: '对象存储：2 TiB。',
        billing_scopes: [{ billing_key: 'storage' }],
      },
    ],
  });
  const input = quoteInput(batch.price_batch_id);
  input.is_partial = true;
  input.fact_ledger.push({
    fact_id: 'F2', component_key: 'cmp_storage_0002', field: 'storage_gib',
    value: 2048, unit: 'GiB', scope: 'total', cleaned_evidence: '对象存储：2 TiB。',
    disposition: 'billable',
  });
  input.unpriced_services = [{
    component_key: 'cmp_storage_0002', region: 'eastasia', fact_ids: ['F2'],
    failure_code: 'official_price_unavailable', retryable: true,
    customer_facing: {
      service_name: '对象存储', quantity: '2 TiB',
      requirement_summary: '对象存储 2 TiB。',
      configuration_summary: '对象存储 2 TiB，价格尚未取得。',
    },
  }];

  const result = await workflow.buildEstimate(input);

  assert.equal(result.status, 'displayed_on_page');
  assert.equal(displayed.length, 1);
  const saved = workflow.store.get(result.quote_id);
  assert.equal(saved.is_partial, true);
  assert.equal(saved.verification.status, 'official_price_partial');
  assert.equal(saved.unpriced_ir[0].component_key, 'cmp_storage_0002');
  assert.equal(saved.verification.costs.monthly, 12.34);
});

test('a complete quote cannot hide an unpriced component', async (t) => {
  const { workflow, directory } = fixture();
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow);
  const input = quoteInput(batch.price_batch_id);
  input.unpriced_services = [{
    component_key: 'cmp_storage_0002', fact_ids: ['F2'],
    failure_code: 'retry_limit_reached', retryable: true,
    customer_facing: {
      service_name: '对象存储', requirement_summary: '对象存储 2 TiB。',
      configuration_summary: '对象存储 2 TiB，价格尚未取得。',
    },
  }];

  await assert.rejects(
    workflow.buildEstimate(input),
    (error) => error.code === 'partial_quote_contract_invalid',
  );
});

test('retrying a delivered partial quote creates a newer complete quote instead of replaying it', async (t) => {
  const { workflow, directory } = fixture({ rateCandidates: [{
    rate_id: 'rate-1', official_item_id: 'item-1', unit_price: '12.34', currency: 'USD',
  }] });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const relayDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'aq-partial-retry-relay-'));
  const previousRelayDirectory = process.env.ASTRAQUOTE_GPT_RELAY_DIR;
  process.env.ASTRAQUOTE_GPT_RELAY_DIR = relayDirectory;
  const relayJobId = 'gpt-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
  const relayJobPath = path.join(relayDirectory, 'jobs', `${relayJobId}.json`);
  fs.mkdirSync(path.dirname(relayJobPath), { recursive: true });
  const relayJob = {
    job_id: relayJobId, submission_code: '2', status: 'processing',
    partial_retry_generation: 0,
    quote_options: {
      cloud_provider: 'azure', preferred_region: 'eastasia',
      pricing_scenarios: ['on_demand'],
    },
  };
  fs.writeFileSync(relayJobPath, JSON.stringify(relayJob));
  t.after(() => {
    fs.rmSync(relayDirectory, { recursive: true, force: true });
    if (previousRelayDirectory === undefined) delete process.env.ASTRAQUOTE_GPT_RELAY_DIR;
    else process.env.ASTRAQUOTE_GPT_RELAY_DIR = previousRelayDirectory;
  });

  const plan = [
    {
      component_key: 'cmp_compute_0001', customer_owned_source: '云服务器：1 台。',
      billing_scopes: [{ billing_key: 'compute' }],
    },
    {
      component_key: 'cmp_storage_0002', customer_owned_source: '对象存储：2 TiB。',
      billing_scopes: [{ billing_key: 'storage' }],
    },
  ];
  const batch = await workflow.getPrices({
    relay_job_id: relayJobId, submission_code: '2', quote_components: plan,
    queries: [{ provider: 'azure', query_id: 'price-1', filter: 'valid' }],
    query_contexts: [{
      query_id: 'price-1', purpose: 'pricing',
      component_key: 'cmp_compute_0001', billing_key: 'compute',
    }],
  });
  const partial = quoteInput(batch.price_batch_id);
  Object.assign(partial, {
    relay_job_id: relayJobId,
    is_partial: true,
    pricing_scenarios: [{
      scenario_key: 'on_demand', monthly_total: '12.34', upfront_total: '0',
    }],
  });
  partial.services[0].scenario_costs = [{
    scenario_key: 'on_demand', pricing_basis: 'on_demand',
    monthly_cost: '12.34', upfront_cost: '0',
  }];
  partial.fact_ledger.push({
    fact_id: 'F2', component_key: 'cmp_storage_0002', field: 'storage_gib', value: 2048,
    unit: 'GiB', scope: 'total', cleaned_evidence: '对象存储：2 TiB。', disposition: 'billable',
  });
  partial.unpriced_services = [{
    component_key: 'cmp_storage_0002', fact_ids: ['F2'],
    failure_code: 'retry_limit_reached', retryable: true,
    customer_facing: {
      service_name: '对象存储', requirement_summary: '对象存储 2 TiB。',
      configuration_summary: '对象存储 2 TiB，价格尚未取得。',
    },
  }];
  const firstDelivery = await workflow.buildEstimate(partial);

  fs.writeFileSync(relayJobPath, JSON.stringify({
    ...relayJob, partial_retry_generation: 1,
  }));
  await workflow.getPrices({
    relay_job_id: relayJobId, submission_code: '2', price_batch_id: batch.price_batch_id,
    queries: [{ provider: 'azure', query_id: 'price-2', filter: 'valid' }],
    query_contexts: [{
      query_id: 'price-2', purpose: 'pricing',
      component_key: 'cmp_storage_0002', billing_key: 'storage',
    }],
  });
  const complete = quoteInput(batch.price_batch_id, { monthly: '17.34' });
  Object.assign(complete, {
    relay_job_id: relayJobId,
    pricing_scenarios: [{
      scenario_key: 'on_demand', monthly_total: '17.34', upfront_total: '0',
    }],
    idempotency_key: 'complete-after-partial-retry',
  });
  complete.fact_ledger.push(partial.fact_ledger[1]);
  complete.services[0].scenario_costs = [{
    scenario_key: 'on_demand', pricing_basis: 'on_demand',
    monthly_cost: '12.34', upfront_cost: '0',
  }];
  complete.services[0].expected_monthly_cost = '12.34';
  complete.services.push({
    component_key: 'cmp_storage_0002', region: 'eastasia', fact_ids: ['F2'],
    price_evidence: [{ query_id: 'price-2', official_item_ids: ['item-1'] }],
    expected_monthly_cost: '5.00',
    scenario_costs: [{
      scenario_key: 'on_demand', pricing_basis: 'on_demand',
      monthly_cost: '5.00', upfront_cost: '0',
    }],
    customer_facing: {
      service_name: '对象存储', quantity: '2 TiB', requirement_summary: '对象存储 2 TiB。',
      configuration_summary: '对象存储 2 TiB。',
    },
  });

  const secondDelivery = await workflow.buildEstimate(complete);

  assert.notEqual(secondDelivery.quote_id, firstDelivery.quote_id);
  assert.equal(workflow.store.get(secondDelivery.quote_id).is_partial, false);
  assert.equal(workflow.store.findByRelayJobId(relayJobId).quote_id, secondDelivery.quote_id);
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
  assert.deepEqual(replayed.results, []);
  const saved = workflow.getPriceResults({
    price_batch_id: first.price_batch_id,
    query_ids: ['price-1'],
  });
  assert.equal(saved.results[0].status, 'exact');
});

test('resumed get_prices returns only this call delta instead of the whole stored batch', async (t) => {
  const { workflow, directory, backend } = fixture();
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const first = await workflow.getPrices({
    queries: [{ provider: 'azure', query_id: 'old', filter: 'old' }],
  });
  backend.getPrices = async () => ({
    status: 'completed', result_count: 1,
    results: [{ query_id: 'new', provider: 'azure', status: 'exact', official_item_ids: ['new-1'] }],
  });

  const resumed = await workflow.getPrices({
    price_batch_id: first.price_batch_id,
    queries: [{ provider: 'azure', query_id: 'new', filter: 'new' }],
  });

  assert.deepEqual(resumed.results.map((item) => item.query_id), ['new']);
  assert.equal(resumed.batch_result_count, 2);
  assert.equal(resumed.response_compacted, false);
});

test('large get_prices deltas are compacted while complete official evidence stays retrievable', async (t) => {
  const { workflow, directory, backend } = fixture({ resultByteBudget: 1_400 });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  backend.getPrices = async (input) => ({
    status: 'completed',
    result_count: input.queries.length,
    results: input.queries.map((query, index) => ({
      query_id: query.query_id,
      provider: 'azure',
      status: 'exact',
      official_item_ids: [`item-${index}`],
      official_rate_candidates: [{
        rate_id: `rate-${index}`,
        official_item_id: `item-${index}`,
        unit_price: '1.23',
        currency: 'USD',
      }],
      items: [{
        id: `item-${index}`,
        official_payload: 'x'.repeat(4_000),
      }],
    })),
  });

  const result = await workflow.getPrices({
    queries: [
      { provider: 'azure', query_id: 'large-1', filter: 'first' },
      { provider: 'azure', query_id: 'large-2', filter: 'second' },
    ],
  });

  assert.equal(result.response_compacted, true);
  assert.ok(result.response_bytes_before_compaction > result.response_byte_budget);
  assert.deepEqual(result.detail_query_ids, ['large-1', 'large-2']);
  assert.equal(result.results[0].details_available, true);
  assert.equal(result.results[0].items, undefined);
  assert.equal(result.results[0].official_rate_candidates, undefined);

  const saved = workflow.getPriceResults({
    price_batch_id: result.price_batch_id,
    query_ids: ['large-1'],
  });
  assert.equal(saved.results[0].items[0].official_payload.length, 4_000);
  assert.equal(saved.results[0].official_rate_candidates[0].rate_id, 'rate-0');
});

test('saved details page all official rates with complete counts and never modify stored evidence', async (t) => {
  const { workflow, directory, backend } = fixture();
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const rates = Array.from({ length: 45 }, (_, index) => ({
    rate_id: `rate-${index}`, official_item_id: `item-${index}`, unit_price: String(index), currency: 'USD',
  }));
  backend.getPrices = async () => ({ results: [{ query_id: 'many', provider: 'azure', status: 'exact',
    official_item_ids: rates.map((rate) => rate.official_item_id), official_rate_candidates: rates,
    items: rates.map((rate) => ({ id: rate.official_item_id, raw_details: { meter: rate.rate_id } })),
  }] });
  const batch = await workflow.getPrices({ queries: [{ provider: 'azure', query_id: 'many', filter: 'official' }] });
  const observed = [];
  let offset = 0;
  do {
    const page = workflow.getPriceResults({ price_batch_id: batch.price_batch_id, query_ids: ['many'], detail_offset: offset });
    const result = page.results[0];
    assert.equal(result.detail_page.total_rates, 45);
    assert.ok(result.official_rate_candidates.length <= 20);
    assert.equal(result.items[0].raw_details.meter, `rate-${offset}`);
    observed.push(...result.official_rate_candidates.map((rate) => rate.rate_id));
    offset = result.detail_page.next_offset;
  } while (offset !== null);
  assert.deepEqual(observed, rates.map((rate) => rate.rate_id));
  assert.equal(workflow.store.getPriceBatch(batch.price_batch_id).result.results[0].official_rate_candidates.length, 45);
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
          route_contract_version: 3,
          route_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
          provider: 'alibaba', endpoint: 'business.aliyuncs.com', service: 'bssopenapi',
          market_profile: 'alibaba-cn', credential_scope: 'alibaba-cn',
          action: 'QueryPrice', version: '2017-12-14', region: 'ap-southeast-1',
          region_parameter: 'Region', method: 'POST', path: '/',
          response_items_path: 'Data.Items', item_id_paths: ['Id'], rate_fields: [],
          auth_scheme: 'alibaba_rpc_hmac_sha1', request_schema_hash: 'sha256:req',
          response_schema_hash: 'sha256:res', sdk_version: 'astraquote-direct-signer/1',
          official_source_url: 'https://help.aliyun.com/document_detail/87913.html',
          last_verified_at: '2026-09-11T00:00:00.000Z',
          confidence: 0.75, failure_count: 0,
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

  await workflow.getPrices({ queries: [{
    provider: 'alibaba', query_id: 'route-auto-reused', service: 'bssopenapi',
    region: 'ap-southeast-1', query_parameters: { ProductCode: 'oss' },
    body: {}, response_filters: {},
  }] });
  assert.equal(received.endpoint, 'business.aliyuncs.com');
  assert.equal(received.action, 'QueryPrice');
  assert.deepEqual(received.query_parameters, { ProductCode: 'oss' });
});

test('one missing cached route does not block other queries in the same batch', async (t) => {
  const { workflow, directory, backend } = fixture({ provider: 'alibaba' });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  backend.getPrices = async (input) => ({
    status: 'completed',
    result_count: input.queries.length,
    results: input.queries.map((query) => ({
      query_id: query.query_id,
      provider: query.provider,
      status: 'exact',
      official_item_ids: [`item-${query.query_id}`],
      official_rate_candidates: [],
      items: [{ id: `item-${query.query_id}` }],
      ...(query.query_id === 'learn-known-route' ? {
        route_verification: {
          route_contract_version: 3,
          route_fingerprint: 'sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc',
          provider: 'alibaba', endpoint: 'business.aliyuncs.com', service: 'known-service',
          market_profile: 'alibaba-cn', credential_scope: 'alibaba-cn',
          action: 'QueryPrice', version: '2017-12-14', region: 'cn-hangzhou',
          region_parameter: 'Region', method: 'POST', path: '/',
          response_items_path: 'Data.Items', item_id_paths: ['Id'], rate_fields: [],
          auth_scheme: 'alibaba_rpc_hmac_sha1', request_schema_hash: 'sha256:req',
          response_schema_hash: 'sha256:res', sdk_version: 'astraquote-direct-signer/1',
          official_source_url: 'https://help.aliyun.com/document_detail/87913.html',
          last_verified_at: '2026-09-12T00:00:00.000Z', confidence: 0.75, failure_count: 0,
        },
      } : {}),
    })),
  });

  await workflow.getPrices({ queries: [{
    provider: 'alibaba', query_id: 'learn-known-route', endpoint: 'business.aliyuncs.com',
    service: 'known-service', action: 'QueryPrice', version: '2017-12-14',
    region: 'cn-hangzhou', region_parameter: 'Region', method: 'POST', path: '/',
    query_parameters: { ProductCode: 'ecs' }, body: {}, response_filters: {},
  }] });

  let receivedQueryIds = [];
  backend.getPrices = async (input) => {
    receivedQueryIds = input.queries.map((query) => query.query_id);
    return {
      status: 'completed', result_count: input.queries.length,
      results: input.queries.map((query) => ({
        query_id: query.query_id, provider: query.provider, status: 'exact',
        official_item_ids: [`item-${query.query_id}`], items: [{ id: `item-${query.query_id}` }],
      })),
    };
  };
  const batch = await workflow.getPrices({ queries: [
    {
      provider: 'alibaba', query_id: 'known-query', service: 'known-service',
      region: 'cn-hangzhou', query_parameters: { ProductCode: 'rds' },
      body: {}, response_filters: {},
    },
    {
      provider: 'alibaba', query_id: 'missing-query', service: 'missing-service',
      region: 'cn-hangzhou', query_parameters: { ProductCode: 'mq' },
      body: {}, response_filters: {},
    },
  ] });

  assert.deepEqual(receivedQueryIds, ['known-query']);
  assert.equal(batch.results.find((item) => item.query_id === 'known-query').status, 'exact');
  const missing = batch.results.find((item) => item.query_id === 'missing-query');
  assert.equal(missing.status, 'query_failed');
  assert.equal(missing.code, 'pricing_route_discovery_required');
  assert.equal(missing.recovery.next_action, 'discover_and_verify_official_read_only_route');
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
    region: 'eu-frankfurt-1',
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

test('a region code owned by another provider cannot be published under the selected cloud', async (t) => {
  const positiveRate = {
    rate_id: 'tencent:item-1:paid', official_item_id: 'item-1',
    unit_price: '0.25', currency: 'CNY', unit: 'hour', is_zero_rate: false,
  };
  const { workflow, directory } = fixture({ provider: 'tencent', rateCandidates: [positiveRate] });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow, 'tencent');
  const input = quoteInput(batch.price_batch_id, {
    provider: 'tencent', currency: 'CNY', evidence: [{
      query_id: 'price-1', official_item_ids: ['item-1'], official_rate_ids: [positiveRate.rate_id],
    }],
  });
  input.default_region = 'ap-singapore-1';
  input.services[0].region = 'ap-singapore-1';

  await assert.rejects(
    workflow.buildEstimate(input),
    (error) => error.code === 'quote_region_provider_mismatch'
      && error.details.known_owners.some((owner) => owner.provider === 'oci'),
  );
});

test('provider region validation covers component regions and keeps shared codes provider-scoped', async (t) => {
  assert.equal(providerRegionMismatch('aws', 'ap-southeast-1', 'aws-global'), null);
  assert.equal(providerRegionMismatch('aws', 'eu-west-1', 'aws-global'), null);
  assert.deepEqual(
    providerRegionMismatch('aws', 'not-a-real-region', 'aws-global'),
    { provider: 'aws', region: 'not-a-real-region', known_owners: [] },
  );

  const positiveRate = {
    rate_id: 'tencent:item-1:paid', official_item_id: 'item-1',
    unit_price: '0.25', currency: 'CNY', unit: 'hour', is_zero_rate: false,
  };
  const { workflow, directory } = fixture({ provider: 'tencent', rateCandidates: [positiveRate] });
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const batch = await priceBatch(workflow, 'tencent');
  const input = quoteInput(batch.price_batch_id, {
    provider: 'tencent', currency: 'CNY', evidence: [{
      query_id: 'price-1', official_item_ids: ['item-1'], official_rate_ids: [positiveRate.rate_id],
    }],
  });
  input.default_region = 'ap-singapore';
  input.services[0].region = 'ap-singapore-1';

  await assert.rejects(
    workflow.buildEstimate(input),
    (error) => error.code === 'quote_region_provider_mismatch'
      && error.details.component_key === input.services[0].component_key
      && error.details.known_owners.some((owner) => owner.provider === 'oci'),
  );
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

test('MCP rejects a pricing scenario that the selected provider does not offer', async (t) => {
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

  await assert.rejects(
    workflow.buildEstimate(input),
    (error) => error.code === 'pricing_scenario_semantics_invalid'
      && error.details.violations.includes(
        'provider_scenario_not_supported:oci:one_year_commitment',
      ),
  );
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
    query_contexts: [{
      query_id: 'bcc-price', purpose: 'pricing',
      component_key: 'cmp_compute_0001', billing_key: 'compute',
    }],
    quote_components: [{
      component_key: 'cmp_compute_0001', customer_owned_source: '云服务器 1 台。',
      billing_scopes: [{ billing_key: 'compute' }],
    }],
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

test('a sales relay quote must register its complete component plan before any official call', async (t) => {
  const { workflow, backend, directory } = fixture();
  const relayDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-relay-plan-required-'));
  fs.mkdirSync(path.join(relayDirectory, 'jobs'));
  const relayJobId = `gpt-${'e'.repeat(32)}`;
  fs.writeFileSync(path.join(relayDirectory, 'jobs', `${relayJobId}.json`), JSON.stringify({
    job_id: relayJobId, submission_code: '5', status: 'processing',
    quote_options: { cloud_provider: 'tencent', pricing_scenarios: ['on_demand'] },
  }));
  const previous = process.env.ASTRAQUOTE_GPT_RELAY_DIR;
  process.env.ASTRAQUOTE_GPT_RELAY_DIR = relayDirectory;
  let officialCalls = 0;
  backend.getPrices = async () => { officialCalls += 1; return { results: [] }; };
  t.after(() => {
    if (previous === undefined) delete process.env.ASTRAQUOTE_GPT_RELAY_DIR;
    else process.env.ASTRAQUOTE_GPT_RELAY_DIR = previous;
    fs.rmSync(directory, { recursive: true, force: true });
    fs.rmSync(relayDirectory, { recursive: true, force: true });
  });

  await assert.rejects(workflow.getPrices({
    relay_job_id: relayJobId, submission_code: '5',
    queries: [{ provider: 'tencent', query_id: 'redis-price', route_id: 'aqr_aaaaaaaaaaaaaaaaaaaaaaaa' }],
  }), (error) => error.code === 'formal_quote_component_plan_required'
    && error.retryable === true);
  assert.equal(officialCalls, 0);
});

test('a formal quote rejects unowned price queries before calling a provider', async (t) => {
  const { workflow, backend, directory } = fixture();
  let officialCalls = 0;
  backend.getPrices = async () => { officialCalls += 1; return { results: [] }; };
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));

  await assert.rejects(workflow.getPrices({
    queries: [{ provider: 'tencent', query_id: 'redis-price', route_id: 'aqr_aaaaaaaaaaaaaaaaaaaaaaaa' }],
    quote_components: [{
      component_key: 'cmp_redis_0001', customer_owned_source: 'Redis 16 GB。',
      billing_scopes: [{ billing_key: 'instance' }],
    }],
  }), (error) => error.code === 'formal_quote_query_context_required'
    && error.details.query_ids.includes('redis-price'));
  assert.equal(officialCalls, 0);
});

test('a direct formal quote cannot silently fall back to untracked price lookup mode', async (t) => {
  const { workflow, backend, directory } = fixture();
  let officialCalls = 0;
  backend.getPrices = async () => { officialCalls += 1; return { results: [] }; };
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));

  await assert.rejects(workflow.getPrices({
    quote_mode: 'formal_quote',
    queries: [{ provider: 'azure', query_id: 'formal-price', filter: 'valid' }],
  }), (error) => error.code === 'formal_quote_component_plan_required');
  assert.equal(officialCalls, 0);
});

test('price lookup responses tell weaker clients that quote coverage is unknown and page fallback exists', async (t) => {
  const { workflow, directory } = fixture();
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));

  const result = await workflow.getPrices({
    queries: [{ provider: 'azure', query_id: 'one-price', filter: 'caller supplied query' }],
  });

  assert.equal(result.workflow_guard.mode, 'price_lookup');
  assert.equal(result.workflow_guard.quote_plan_registered, false);
  assert.equal(result.workflow_guard.quote_coverage_known, false);
  assert.equal(result.workflow_guard.formal_quote_final_response_allowed, false);
  assert.match(result.workflow_guard.formal_quote_required_action, /quote_components/);
  assert.equal(result.workflow_guard.official_page_fallback.supported, true);
  assert.equal(result.workflow_guard.official_page_fallback.api_attempts_required, 3);
});

test('a created relay job tells GPT to build non-empty queries before price lookup', (t) => {
  const { workflow, directory } = fixture();
  const relayDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-relay-created-'));
  const jobsDirectory = path.join(relayDirectory, 'jobs');
  fs.mkdirSync(jobsDirectory);
  const relayJobId = `gpt-${'d'.repeat(32)}`;
  fs.writeFileSync(path.join(jobsDirectory, `${relayJobId}.json`), JSON.stringify({
    job_id: relayJobId,
    submission_code: '9',
    status: 'processing',
    quote_options: { cloud_provider: 'alibaba', pricing_scenarios: ['on_demand'] },
  }));
  const previous = process.env.ASTRAQUOTE_GPT_RELAY_DIR;
  process.env.ASTRAQUOTE_GPT_RELAY_DIR = relayDirectory;
  t.after(() => {
    if (previous === undefined) delete process.env.ASTRAQUOTE_GPT_RELAY_DIR;
    else process.env.ASTRAQUOTE_GPT_RELAY_DIR = previous;
    fs.rmSync(directory, { recursive: true, force: true });
    fs.rmSync(relayDirectory, { recursive: true, force: true });
  });

  const status = workflow.getQuoteJobStatus({
    relay_job_id: relayJobId, submission_code: '9',
  });
  const resumed = workflow.resumeQuoteJob({
    relay_job_id: relayJobId, submission_code: '9',
  });

  assert.equal(status.stage, 'created');
  assert.match(status.next_action, /non-empty queries array/);
  assert.match(resumed.next_action, /non-empty queries array/);
  assert.match(resumed.next_action, /Execute now/);
  assert.match(resumed.next_action, /Do not describe or list the remaining steps/);
  assert.equal(status.terminal, false);
  assert.equal(status.must_continue, true);
  assert.equal(resumed.terminal, false);
  assert.equal(resumed.must_continue, true);
});

test('completed relay jobs replay before the processing guard and expose stage-level resume', async (t) => {
  const { workflow, directory, displayed } = fixture({ rateCandidates: [{
    rate_id: 'rate-1', official_item_id: 'item-1', unit_price: '12.34', currency: 'USD',
  }] });
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
    query_contexts: [{
      query_id: 'price-1', purpose: 'pricing',
      component_key: 'cmp_compute_0001', billing_key: 'compute',
    }],
    quote_components: [{
      component_key: 'cmp_compute_0001', customer_owned_source: '云服务器数量：1。',
      billing_scopes: [{ billing_key: 'compute' }],
    }],
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

test('a legacy stale-worker failure can finish from its saved complete price batch', async (t) => {
  const { workflow, directory, displayed } = fixture({ rateCandidates: [{
    rate_id: 'rate-1', official_item_id: 'item-1', unit_price: '12.34', currency: 'USD',
  }] });
  const relayDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-relay-stale-'));
  const jobsDirectory = path.join(relayDirectory, 'jobs');
  fs.mkdirSync(jobsDirectory);
  const relayJobId = `gpt-${'c'.repeat(32)}`;
  const relayPath = path.join(jobsDirectory, `${relayJobId}.json`);
  const relay = {
    job_id: relayJobId,
    submission_code: '7',
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
    submission_code: '7',
    queries: [{ provider: 'azure', query_id: 'price-1', filter: 'caller supplied query' }],
    query_contexts: [{
      query_id: 'price-1', purpose: 'pricing',
      component_key: 'cmp_compute_0001', billing_key: 'compute',
    }],
    quote_components: [{
      component_key: 'cmp_compute_0001', customer_owned_source: '云服务器数量：1。',
      billing_scopes: [{ billing_key: 'compute' }],
    }],
  });
  relay.status = 'failed';
  relay.error = { code: 'gpt_quote_worker_stale', message: 'legacy false failure' };
  fs.writeFileSync(relayPath, JSON.stringify(relay));

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

  const result = await workflow.buildEstimate(input);

  assert.equal(result.status, 'displayed_on_page');
  assert.equal(displayed.length, 1);
});

test('an explicit quote stop remains terminal and cannot be revived', async (t) => {
  const { workflow, directory } = fixture();
  const relayDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-relay-blocked-'));
  const jobsDirectory = path.join(relayDirectory, 'jobs');
  fs.mkdirSync(jobsDirectory);
  const relayJobId = `gpt-${'d'.repeat(32)}`;
  fs.writeFileSync(path.join(jobsDirectory, `${relayJobId}.json`), JSON.stringify({
    job_id: relayJobId,
    submission_code: '8',
    status: 'failed',
    error: { code: 'gpt_quote_blocked' },
    quote_options: { cloud_provider: 'azure', pricing_scenarios: ['on_demand'] },
  }));
  const previous = process.env.ASTRAQUOTE_GPT_RELAY_DIR;
  process.env.ASTRAQUOTE_GPT_RELAY_DIR = relayDirectory;
  t.after(() => {
    if (previous === undefined) delete process.env.ASTRAQUOTE_GPT_RELAY_DIR;
    else process.env.ASTRAQUOTE_GPT_RELAY_DIR = previous;
    fs.rmSync(directory, { recursive: true, force: true });
    fs.rmSync(relayDirectory, { recursive: true, force: true });
  });

  await assert.rejects(
    workflow.buildEstimate({
      ...quoteInput('aqpb-not-needed'),
      relay_job_id: relayJobId,
      pricing_scenarios: [{
        scenario_key: 'on_demand', monthly_total: '12.34', upfront_total: '0',
      }],
    }),
    (error) => error.code === 'relay_job_not_processing',
  );
});
