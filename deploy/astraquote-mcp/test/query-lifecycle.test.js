'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { AstraQuoteV2Workflow } = require('../lib/v2-workflow');
const { V2QuoteStore } = require('../lib/v2-quote-store');
const { reusableQueryResult } = require('../lib/query-lifecycle');

function fixture(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'aq-query-lifecycle-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const calls = [];
  const backend = {
    async getPrices(input) {
      calls.push(input);
      return { results: input.queries.map((q) => ({
        query_id: q.query_id, provider: q.provider,
        status: q.filter === 'valid' ? 'exact' : 'not_found',
        official_item_ids: q.filter === 'valid' ? ['item-1'] : [],
        official_rate_candidates: q.filter === 'valid' ? [{
          rate_id: 'rate-1', official_item_id: 'item-1', unit_price: '1', currency: 'USD',
        }] : [],
      })) };
    },
  };
  const store = new V2QuoteStore({ directory });
  const workflow = new AstraQuoteV2Workflow({
    backend,
    store,
    deliverer: {},
    priceCache: { get: () => null, put: () => false },
  });
  return { workflow, store, calls, backend };
}

const query = (id, valid = false, provider = 'azure') => ({
  query_id: id, provider, filter: valid ? 'valid' : 'missing',
});
const context = (id, billing = 'compute', component = 'cmp_resource_0001') => ({
  query_id: id, purpose: 'pricing', component_key: component, billing_key: billing,
});

test('formal pricing does not complete on missing currency, zero placeholders, or partial modules', () => {
  const owned = context('price');
  const base = {
    query_id: 'price', provider: 'alibaba', status: 'ambiguous',
    official_item_ids: ['NodeAmount', 'Disk'],
  };
  assert.equal(reusableQueryResult({
    ...base,
    official_rate_candidates: [
      { rate_id: 'node', official_item_id: 'NodeAmount', unit_price: '1.72', currency: 'CNY' },
      { rate_id: 'disk', official_item_id: 'Disk', unit_price: '14', currency: 'None' },
    ],
  }, owned), false);
  assert.equal(reusableQueryResult({
    ...base,
    official_rate_candidates: [
      { rate_id: 'node', official_item_id: 'NodeAmount', unit_price: '0', currency: 'CNY' },
      { rate_id: 'disk', official_item_id: 'Disk', unit_price: '14', currency: 'CNY' },
    ],
  }, owned), false);
  assert.equal(reusableQueryResult({
    ...base,
    official_rate_candidates: [
      { rate_id: 'node', official_item_id: 'NodeAmount', unit_price: '1200', currency: 'CNY' },
      { rate_id: 'disk', official_item_id: 'Disk', unit_price: '14', currency: 'CNY' },
    ],
  }, owned), true);
  assert.equal(reusableQueryResult({
    ...base,
    official_item_ids: ['Enterprise_Instance_Postpaid'],
    official_rate_candidates: [{
      rate_id: 'opaque', official_item_id: 'Enterprise_Instance_Postpaid',
      unit_price: '0.000954866', currency: 'CNY',
      unit: 'CNY/usage-period-configured-module',
    }],
  }, owned), false);
});

test('one failed API attempt can be completed by persisted official-page evidence without requerying', async (t) => {
  const { workflow, store, calls } = fixture(t);
  const first = await workflow.getPrices({
    quote_mode: 'formal_quote',
    queries: [query('page-fallback')],
    query_contexts: [context('page-fallback')],
    quote_components: [{
      component_key: 'cmp_resource_0001',
      customer_owned_source: '云服务器数量：1。',
      billing_scopes: [{ billing_key: 'compute' }],
    }],
  });
  assert.equal(first.completed_component_count, 0);

  const second = await workflow.getPrices({
    quote_mode: 'formal_quote',
    price_batch_id: first.price_batch_id,
    queries: [query('page-fallback')],
    query_contexts: [context('page-fallback')],
    official_page_price_evidence: [{
      component_key: 'cmp_resource_0001',
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
      api_attempt_query_ids: ['page-fallback'],
    }],
  });

  assert.equal(calls.length, 1);
  assert.equal(second.completed_component_count, 1);
  assert.deepEqual(second.incomplete_query_ids, []);
  assert.equal(second.official_page_price_evidence.length, 1);
  assert.equal(
    store.getPriceBatch(first.price_batch_id).official_page_price_evidence[0].unit_price,
    '0.12',
  );
});

test('new successful attempt retires old failure in the same billing scope, not other costs', async (t) => {
  const { workflow, store, calls } = fixture(t);
  const first = await workflow.getPrices({
    queries: [query('explore'), query('old'), query('disk')],
    query_contexts: [
      { query_id: 'explore', purpose: 'discovery' }, context('old'), context('disk', 'disk'),
    ],
  });
  assert.deepEqual(first.incomplete_query_ids, ['old', 'disk']);
  const second = await workflow.getPrices({
    price_batch_id: first.price_batch_id, queries: [query('new', true)],
    query_contexts: [context('new')],
  });
  assert.deepEqual(second.incomplete_query_ids, ['disk']);
  assert.deepEqual(second.superseded_query_ids, ['old']);
  assert.equal(second.status, 'needs_refinement');
  assert.equal(second.quote_terminal, false);
  assert.equal(calls[1].query_contexts, undefined);
  const saved = store.getPriceBatch(first.price_batch_id);
  assert.equal(saved.result.results.find((r) => r.query_id === 'old').status, 'not_found');
  assert.equal(saved.query_lifecycle.find((r) => r.query_id === 'old').superseded_by, 'new');
  const restarted = new AstraQuoteV2Workflow({
    backend: workflow.backend,
    store,
    deliverer: {},
    priceCache: { get: () => null, put: () => false },
  });
  const third = await restarted.getPrices({
    price_batch_id: first.price_batch_id, queries: [query('disk-fixed', true)],
    query_contexts: [context('disk-fixed', 'disk')],
  });
  assert.equal(third.status, 'completed');
  assert.deepEqual(third.incomplete_query_ids, []);
  assert.equal(third.batch_result_count, 5);
});

test('a failed replacement cannot close a missing price, and discovery alone is not a priced quote', async (t) => {
  const { workflow } = fixture(t);
  const first = await workflow.getPrices({
    queries: [query('catalog', true)],
    query_contexts: [{ query_id: 'catalog', purpose: 'discovery' }],
  });
  assert.equal(first.status, 'needs_refinement');
  assert.equal(first.next_action, 'query_component_prices');
  const second = await workflow.getPrices({
    price_batch_id: first.price_batch_id, queries: [query('old')],
    query_contexts: [context('old')],
  });
  const third = await workflow.getPrices({
    price_batch_id: first.price_batch_id, queries: [query('new')],
    query_contexts: [{ ...context('new'), supersedes_query_ids: ['old'] }],
  });
  assert.deepEqual(third.incomplete_query_ids, ['old', 'new']);
  assert.deepEqual(third.superseded_query_ids, []);
  assert.equal(second.quote_terminal, false);
});

test('legacy attempts can be explicitly linked to a saved successful replacement without requerying it', async (t) => {
  const { workflow, calls } = fixture(t);
  const first = await workflow.getPrices({ queries: [query('old'), query('new', true)] });
  const second = await workflow.getPrices({
    price_batch_id: first.price_batch_id, queries: [query('new', true)],
    query_contexts: [{ ...context('new'), supersedes_query_ids: ['old'] }],
  });
  assert.equal(calls.length, 1);
  assert.equal(second.status, 'completed');
  assert.deepEqual(second.superseded_query_ids, ['old']);
  assert.deepEqual(second.reused_query_ids, ['new']);
});

test('replacement scope cannot cross components, purchase options, or cloud providers', async (t) => {
  for (const variant of ['component', 'scenario', 'provider']) {
    const { workflow, calls } = fixture(t);
    const first = await workflow.getPrices({
      queries: [query('old')], query_contexts: [context('old')],
    });
    const nextContext = { ...context('new'), supersedes_query_ids: ['old'] };
    if (variant === 'component') nextContext.component_key = 'cmp_resource_0002';
    if (variant === 'scenario') nextContext.scenario_key = 'one_year_commitment';
    await assert.rejects(workflow.getPrices({
      price_batch_id: first.price_batch_id,
      queries: [query('new', true, variant === 'provider' ? 'oci' : 'azure')],
      query_contexts: [nextContext],
    }), (err) => err.code === 'price_query_context_invalid' && err.retryable === true);
    assert.equal(calls.length, 1);
  }
});

test('declared pricing requirements cannot be silently relabeled as discovery or another cost', async (t) => {
  const { workflow } = fixture(t);
  const first = await workflow.getPrices({ queries: [query('old')], query_contexts: [context('old')] });
  for (const change of [{ query_id: 'old', purpose: 'discovery' }, context('old', 'disk')]) {
    await assert.rejects(workflow.getPrices({
      price_batch_id: first.price_batch_id, queries: [query('new', true)], query_contexts: [change],
    }), (err) => err.code === 'price_query_context_invalid');
  }
});

test('an older success cannot hide a newly failed attempt, and a catalog envelope is not a rate', async (t) => {
  const { workflow, backend } = fixture(t);
  const first = await workflow.getPrices({ queries: [query('old', true)], query_contexts: [context('old')] });
  const next = await workflow.getPrices({
    price_batch_id: first.price_batch_id, queries: [query('new')], query_contexts: [context('new')],
  });
  assert.deepEqual(next.incomplete_query_ids, ['new']);
  backend.getPrices = async () => ({ results: [{
    query_id: 'envelope', provider: 'azure', status: 'exact', official_item_ids: ['wrapper-id'],
    official_rate_candidates: [],
  }] });
  const envelope = await workflow.getPrices({
    price_batch_id: first.price_batch_id, queries: [query('envelope', true)],
    query_contexts: [context('envelope')],
  });
  assert.deepEqual(envelope.incomplete_query_ids, ['new', 'envelope']);
});

test('unknown replacement IDs and replacement cycles are correctable without changing the saved batch', async (t) => {
  const { workflow, store, calls } = fixture(t);
  const first = await workflow.getPrices({ queries: [query('old')] });
  const snapshot = store.getPriceBatch(first.price_batch_id);
  for (const contexts of [
    [{ ...context('new'), supersedes_query_ids: ['unknown'] }],
    [{ ...context('new'), supersedes_query_ids: ['old'] },
      { ...context('old'), supersedes_query_ids: ['new'] }],
  ]) {
    await assert.rejects(workflow.getPrices({
      price_batch_id: first.price_batch_id, queries: [query('new', true)], query_contexts: contexts,
    }), (err) => err.code === 'price_query_context_invalid' && err.retryable);
    assert.deepEqual(store.getPriceBatch(first.price_batch_id), snapshot);
  }
  assert.equal(calls.length, 1);
});

test('GPT may promote discovered rates to a billing item without repeating the official request', async (t) => {
  const { workflow, calls } = fixture(t);
  const first = await workflow.getPrices({
    queries: [query('found', true)], query_contexts: [{ query_id: 'found', purpose: 'discovery' }],
  });
  const second = await workflow.getPrices({
    price_batch_id: first.price_batch_id, queries: [query('found', true)],
    query_contexts: [context('found')],
  });
  assert.equal(calls.length, 1);
  assert.equal(second.status, 'completed');
});

test('unclassified legacy failures do not declare the whole quote permanently blocked', async (t) => {
  const { workflow, backend } = fixture(t);
  backend.getPrices = async () => ({ results: [{
    query_id: 'legacy', provider: 'azure', status: 'query_failed', terminal: true,
    retryable: false, official_item_ids: [],
  }] });
  const result = await workflow.getPrices({ queries: [query('legacy')] });
  assert.equal(result.quote_terminal, false);
  assert.equal(result.must_continue, true);
});

test('an explicitly selected on-demand fallback covers the corresponding commitment billing item', async (t) => {
  const { workflow, store } = fixture(t);
  const batch = await workflow.getPrices({
    queries: [query('payg', true), query('year')], query_contexts: [
      { ...context('payg'), scenario_key: 'on_demand' },
      { ...context('year'), scenario_key: 'one_year_commitment' },
    ],
  });
  const evidence = [{ query_id: 'payg', official_item_ids: ['item-1'], official_rate_ids: ['rate-1'] }];
  const input = { cloud_provider: 'azure', currency: 'USD', services: [{
    component_key: 'cmp_resource_0001', price_evidence: evidence,
    scenario_costs: [
      { scenario_key: 'on_demand', monthly_cost: '10', price_evidence: evidence },
      { scenario_key: 'one_year_commitment', pricing_basis: 'on_demand_fallback',
        monthly_cost: '10', price_evidence: evidence },
    ],
  }] };
  assert.doesNotThrow(() => workflow.validatePriceEvidence(input, store.getPriceBatch(batch.price_batch_id)));
  input.services[0].scenario_costs[1].monthly_cost = '0';
  assert.throws(() => workflow.validatePriceEvidence(input, store.getPriceBatch(batch.price_batch_id)),
    (err) => err.details.violations.some((v) => v.startsWith('scenario_fallback_cost_changed:')));
});

test('final evidence can ignore failed exploration but cannot omit a declared billing item or change its component', async (t) => {
  const { workflow, store } = fixture(t);
  const first = await workflow.getPrices({
    queries: [query('catalog'), query('price', true), query('disk')],
    query_contexts: [{ query_id: 'catalog', purpose: 'discovery' }, context('price'), context('disk', 'disk')],
  });
  const input = { cloud_provider: 'azure', currency: 'USD', services: [{
    component_key: 'cmp_resource_0001',
    price_evidence: [{ query_id: 'price', official_item_ids: ['item-1'], official_rate_ids: ['rate-1'] }],
  }] };
  assert.throws(() => workflow.validatePriceEvidence(input, store.getPriceBatch(first.price_batch_id)),
    (err) => err.details.violations.some((v) => v.startsWith('billing_query_evidence_missing:')));
  const next = await workflow.getPrices({
    price_batch_id: first.price_batch_id, queries: [query('disk-fixed', true)],
    query_contexts: [context('disk-fixed', 'disk')],
  });
  input.services[0].price_evidence.push({ query_id: 'disk-fixed', official_item_ids: ['item-1'], official_rate_ids: ['rate-1'] });
  assert.doesNotThrow(() => workflow.validatePriceEvidence(input, store.getPriceBatch(next.price_batch_id)));
  input.services[0].component_key = 'cmp_resource_0002';
  assert.throws(() => workflow.validatePriceEvidence(input, store.getPriceBatch(next.price_batch_id)),
    (err) => err.details.violations.some((v) => v.startsWith('price_query_component_mismatch:')));
});

test('a sealed cleaned component plan produces machine-readable component progress', async (t) => {
  const { workflow, store } = fixture(t);
  const relayJobId = 'gpt-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
  const relayDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'aq-component-progress-relay-'));
  const previousRelayDirectory = process.env.ASTRAQUOTE_GPT_RELAY_DIR;
  process.env.ASTRAQUOTE_GPT_RELAY_DIR = relayDirectory;
  fs.mkdirSync(path.join(relayDirectory, 'jobs'), { recursive: true });
  fs.writeFileSync(path.join(relayDirectory, 'jobs', `${relayJobId}.json`), JSON.stringify({
    job_id: relayJobId, submission_code: '1', status: 'processing',
  }));
  t.after(() => {
    fs.rmSync(relayDirectory, { recursive: true, force: true });
    if (previousRelayDirectory === undefined) delete process.env.ASTRAQUOTE_GPT_RELAY_DIR;
    else process.env.ASTRAQUOTE_GPT_RELAY_DIR = previousRelayDirectory;
  });
  const result = await workflow.getPrices({
    relay_job_id: relayJobId,
    submission_code: '1',
    queries: [query('compute-found', true)],
    query_contexts: [context('compute-found', 'compute', 'cmp_resource_0001')],
    quote_components: [
      {
        component_key: 'cmp_resource_0001',
        customer_owned_source: '云服务器：1 台，4 核 16 GiB。',
        billing_scopes: [{ billing_key: 'compute' }],
      },
      {
        component_key: 'cmp_resource_0002',
        customer_owned_source: '对象存储：2 TiB。',
        billing_scopes: [{ billing_key: 'storage' }],
      },
    ],
  });

  assert.equal(result.total_component_count, 2);
  assert.equal(result.top_level_component_count, 2);
  assert.equal(result.component_chat_count, 1);
  assert.equal(result.completed_component_count, 1);
  assert.equal(result.failed_component_count, 0);
  assert.equal(result.pending_component_count, 1);
  const checkpoint = store.getCheckpoint(relayJobId);
  assert.equal(checkpoint.total_component_count, 2);
  assert.equal(checkpoint.top_level_component_count, 2);
  assert.equal(checkpoint.component_chat_count, 1);
  assert.equal(checkpoint.completed_component_count, 1);
  assert.equal(checkpoint.pending_component_count, 1);
  assert.equal(checkpoint.quote_components, undefined);
  assert.deepEqual(
    store.getPriceBatch(result.price_batch_id).quote_components.map((item) => item.component_key),
    ['cmp_resource_0001', 'cmp_resource_0002'],
  );
});

test('component chat count uses top-level groups and keeps child components in the parent slot', async (t) => {
  const { workflow } = fixture(t);
  const quoteComponents = [
    ...Array.from({ length: 21 }, (_, index) => ({
      component_key: `cmp_root_${String(index).padStart(4, '0')}`,
      customer_owned_source: `清洗组件 ${index}。`,
      billing_scopes: [{ billing_key: 'base' }],
    })),
    {
      component_key: 'cmp_child_0001',
      parent_component_key: 'cmp_root_0000',
      customer_owned_source: '清洗后的子组件。',
      billing_scopes: [{ billing_key: 'storage' }],
    },
  ];

  const result = await workflow.getPrices({
    queries: [query('compute-found', true)],
    query_contexts: [context('compute-found', 'base', 'cmp_root_0000')],
    quote_components: quoteComponents,
  });

  assert.equal(result.total_component_count, 22);
  assert.equal(result.top_level_component_count, 21);
  assert.equal(result.component_chat_count, 3);
});

test('parallel component chats merge into one price batch without overwriting each other', async (t) => {
  const { workflow, store, backend } = fixture(t);
  const first = await workflow.getPrices({
    queries: [query('first', true)],
    query_contexts: [context('first', 'base', 'cmp_resource_0001')],
  });
  const original = backend.getPrices.bind(backend);
  backend.getPrices = async (input) => {
    if (input.queries[0].query_id === 'second') {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    return original(input);
  };

  await Promise.all([
    workflow.getPrices({
      price_batch_id: first.price_batch_id,
      queries: [query('second', true)],
      query_contexts: [context('second', 'storage', 'cmp_resource_0002')],
    }),
    workflow.getPrices({
      price_batch_id: first.price_batch_id,
      queries: [query('third', true)],
      query_contexts: [context('third', 'requests', 'cmp_resource_0003')],
    }),
  ]);

  const saved = store.getPriceBatch(first.price_batch_id);
  assert.deepEqual(
    saved.result.results.map((item) => item.query_id).sort(),
    ['first', 'second', 'third'],
  );
});

test('the cleaned component plan is immutable after the first saved price batch', async (t) => {
  const { workflow, calls } = fixture(t);
  const plan = [{
    component_key: 'cmp_resource_0001',
    customer_owned_source: '云服务器：1 台，4 核 16 GiB。',
    billing_scopes: [{ billing_key: 'compute' }],
  }];
  const first = await workflow.getPrices({
    queries: [query('compute-found', true)],
    query_contexts: [context('compute-found')],
    quote_components: plan,
  });

  await assert.rejects(workflow.getPrices({
    price_batch_id: first.price_batch_id,
    queries: [query('disk-found', true)],
    query_contexts: [context('disk-found', 'disk')],
    quote_components: [{
      ...plan[0],
      customer_owned_source: '被修改后的组件。',
    }],
  }), (err) => err.code === 'quote_component_plan_immutable');
  assert.equal(calls.length, 1);
});

test('pre-split relay chats append only their own component plan into one reserved batch', async (t) => {
  const { workflow, store } = fixture(t);
  const relayJobId = `gpt-${'9'.repeat(32)}`;
  const reservedBatchId = 'aqpb_aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
  const relayDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'aq-pre-split-relay-'));
  const previousRelayDirectory = process.env.ASTRAQUOTE_GPT_RELAY_DIR;
  process.env.ASTRAQUOTE_GPT_RELAY_DIR = relayDirectory;
  fs.mkdirSync(path.join(relayDirectory, 'jobs'), { recursive: true });
  fs.writeFileSync(path.join(relayDirectory, 'jobs', `${relayJobId}.json`), JSON.stringify({
    job_id: relayJobId,
    submission_code: '3',
    status: 'processing',
    reserved_price_batch_id: reservedBatchId,
    intake_component_count: 2,
    intake_batch_count: 2,
    intake_batches: [
      { batch_index: 0, batch_count: 2, component_keys: ['cmp_intake_0001'], source_lines: [] },
      { batch_index: 1, batch_count: 2, component_keys: ['cmp_intake_0002'], source_lines: [] },
    ],
    quote_options: { cloud_provider: 'azure', pricing_scenarios: ['on_demand'] },
  }));
  t.after(() => {
    fs.rmSync(relayDirectory, { recursive: true, force: true });
    if (previousRelayDirectory === undefined) delete process.env.ASTRAQUOTE_GPT_RELAY_DIR;
    else process.env.ASTRAQUOTE_GPT_RELAY_DIR = previousRelayDirectory;
  });

  const first = await workflow.getPrices({
    quote_mode: 'formal_quote', relay_job_id: relayJobId, submission_code: '3',
    relay_batch_index: 0, relay_batch_count: 2, price_batch_id: reservedBatchId,
    queries: [query('batch-0', true)],
    query_contexts: [context('batch-0', 'compute', 'cmp_intake_0001')],
    quote_components: [{
      component_key: 'cmp_intake_0001', customer_owned_source: '云服务器 1 台。',
      billing_scopes: [{ billing_key: 'compute' }],
    }],
  });
  assert.equal(first.price_batch_id, reservedBatchId);
  assert.equal(first.total_component_count, 2);
  assert.equal(first.completed_component_count, 1);
  assert.equal(first.must_continue, true);
  await assert.rejects(workflow.buildEstimate({
    relay_job_id: relayJobId,
    pricing_scenarios: [{ scenario_key: 'on_demand' }],
    cloud_provider: 'azure', default_region: 'eastasia',
    price_batch_id: reservedBatchId,
    idempotency_key: 'pre-split-before-all-batches',
  }), (error) => error.code === 'relay_component_batches_incomplete');

  await assert.rejects(workflow.getPrices({
    quote_mode: 'formal_quote', relay_job_id: relayJobId, submission_code: '3',
    relay_batch_index: 1, relay_batch_count: 2, price_batch_id: reservedBatchId,
    queries: [query('wrong-batch', true)],
    query_contexts: [context('wrong-batch', 'compute', 'cmp_intake_0001')],
    quote_components: [{
      component_key: 'cmp_intake_0001', customer_owned_source: '越界组件。',
      billing_scopes: [{ billing_key: 'compute' }],
    }],
  }), (error) => error.code === 'relay_component_batch_mismatch');

  const second = await workflow.getPrices({
    quote_mode: 'formal_quote', relay_job_id: relayJobId, submission_code: '3',
    relay_batch_index: 1, relay_batch_count: 2, price_batch_id: reservedBatchId,
    queries: [query('batch-1', true)],
    query_contexts: [context('batch-1', 'storage', 'cmp_intake_0002')],
    quote_components: [{
      component_key: 'cmp_intake_0002', customer_owned_source: '对象存储 1 TiB。',
      billing_scopes: [{ billing_key: 'storage' }],
    }],
  });
  const saved = store.getPriceBatch(reservedBatchId);
  assert.equal(second.completed_component_count, 2);
  assert.deepEqual(saved.registered_relay_batches, [0, 1]);
  assert.deepEqual(saved.quote_components.map((item) => item.component_key), [
    'cmp_intake_0001', 'cmp_intake_0002',
  ]);
  await assert.rejects(workflow.buildEstimate({
    relay_job_id: relayJobId,
    pricing_scenarios: [{ scenario_key: 'on_demand' }],
    cloud_provider: 'azure', default_region: 'eastasia',
    price_batch_id: reservedBatchId,
    idempotency_key: 'pre-split-before-merge',
  }), (error) => error.code === 'relay_merge_not_authorized');
});

test('pre-split relay batches save isolated quote fragments and the last fragment delivers once', async (t) => {
  const { workflow, store } = fixture(t);
  const displayed = [];
  workflow.deliverer = {
    async deliverPageResult(record) {
      displayed.push(record);
      return { status: 'displayed_on_page', page_result: { currency: record.currency } };
    },
  };
  const relayJobId = `gpt-${'8'.repeat(32)}`;
  const reservedBatchId = 'aqpb_bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee';
  const relayDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'aq-auto-merge-relay-'));
  const previousRelayDirectory = process.env.ASTRAQUOTE_GPT_RELAY_DIR;
  process.env.ASTRAQUOTE_GPT_RELAY_DIR = relayDirectory;
  fs.mkdirSync(path.join(relayDirectory, 'jobs'), { recursive: true });
  fs.writeFileSync(path.join(relayDirectory, 'jobs', `${relayJobId}.json`), JSON.stringify({
    job_id: relayJobId,
    submission_code: '4',
    status: 'processing',
    reserved_price_batch_id: reservedBatchId,
    intake_component_count: 2,
    intake_batch_count: 2,
    intake_batches: [
      { batch_index: 0, batch_count: 2, component_keys: ['cmp_intake_0001'], source_lines: [] },
      { batch_index: 1, batch_count: 2, component_keys: ['cmp_intake_0002'], source_lines: [] },
    ],
    quote_options: { cloud_provider: 'azure', pricing_scenarios: ['on_demand'] },
  }));
  t.after(() => {
    fs.rmSync(relayDirectory, { recursive: true, force: true });
    if (previousRelayDirectory === undefined) delete process.env.ASTRAQUOTE_GPT_RELAY_DIR;
    else process.env.ASTRAQUOTE_GPT_RELAY_DIR = previousRelayDirectory;
  });

  for (const [index, componentKey] of ['cmp_intake_0001', 'cmp_intake_0002'].entries()) {
    await workflow.getPrices({
      quote_mode: 'formal_quote', relay_job_id: relayJobId, submission_code: '4',
      relay_batch_index: index, relay_batch_count: 2, price_batch_id: reservedBatchId,
      queries: [query(`batch-${index}`, true)],
      query_contexts: [context(`batch-${index}`, 'compute', componentKey)],
      quote_components: [{
        component_key: componentKey,
        customer_owned_source: `组件数量：${index + 1}。`,
        billing_scopes: [{ billing_key: 'compute' }],
      }],
    });
  }

  const fragmentInput = (index, componentKey, monthly) => ({
    delivery_mode: 'save_component_batch',
    relay_job_id: relayJobId,
    relay_batch_index: index,
    relay_batch_count: 2,
    quote_name: `第 ${index + 1} 批`,
    cloud_provider: 'azure',
    default_region: 'eastasia',
    currency: 'USD',
    price_batch_id: reservedBatchId,
    expected_monthly_total: monthly,
    pricing_scenarios: [{
      scenario_key: 'on_demand', monthly_total: monthly, upfront_total: '0',
    }],
    fact_ledger: [{
      fact_id: 'F1', component_key: componentKey, field: 'quantity',
      value: index + 1, unit: 'count', scope: 'total',
      cleaned_evidence: `组件数量：${index + 1}。`, disposition: 'billable',
    }],
    services: [{
      component_key: componentKey, region: 'eastasia',
      price_evidence: [{ query_id: `batch-${index}`, official_item_ids: ['item-1'] }],
      fact_ids: ['F1'], expected_monthly_cost: monthly,
      scenario_costs: [{
        scenario_key: 'on_demand', pricing_basis: 'on_demand',
        monthly_cost: monthly, upfront_cost: '0',
        price_evidence: [{ query_id: `batch-${index}`, official_item_ids: ['item-1'] }],
      }],
      customer_facing: {
        service_name: `服务 ${index + 1}`, model_or_plan: '正式规格',
        quantity: `${index + 1}`, requirement_summary: `组件数量：${index + 1}。`,
        configuration_summary: `组件数量：${index + 1}。`,
      },
    }],
    zero_cost_services: [], unpriced_services: [], assumptions: [], adjustments: [],
    idempotency_key: `relay-fragment-${index}-first-attempt`,
  });

  const first = await workflow.buildEstimate(
    fragmentInput(0, 'cmp_intake_0001', '12.345'),
  );
  assert.equal(first.status, 'component_batch_saved');
  assert.deepEqual(first.pending_batch_indexes, [1]);
  assert.equal(displayed.length, 0);
  await assert.rejects(workflow.buildEstimate({
    ...fragmentInput(0, 'cmp_intake_0001', '13.00'),
    idempotency_key: 'relay-fragment-0-conflicting-attempt',
  }), (error) => error.code === 'relay_component_result_immutable');

  const final = await workflow.buildEstimate(
    fragmentInput(1, 'cmp_intake_0002', '0.105'),
  );
  assert.equal(final.status, 'displayed_on_page');
  assert.equal(displayed.length, 1);
  assert.equal(displayed[0].expected_monthly_total, '12.45');
  assert.deepEqual(displayed[0].resource_ir.map((item) => item.component_key), [
    'cmp_intake_0001', 'cmp_intake_0002',
  ]);
  assert.equal(new Set(displayed[0].fact_ledger.map((fact) => fact.fact_id)).size, 2);

  const replay = await workflow.buildEstimate({
    ...fragmentInput(1, 'cmp_intake_0002', '0.105'),
    idempotency_key: 'relay-fragment-1-second-attempt',
  });
  assert.equal(replay.idempotent_replay, true);
  assert.equal(displayed.length, 1);
  assert.equal(store.getPriceBatch(reservedBatchId).component_result_fragments.length, 2);
});

test('a saved relay quote fragment cannot contain or overwrite another batch', async (t) => {
  const { workflow } = fixture(t);
  const relayJobId = `gpt-${'7'.repeat(32)}`;
  const reservedBatchId = 'aqpb_cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee';
  const relayDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'aq-fragment-boundary-'));
  const previousRelayDirectory = process.env.ASTRAQUOTE_GPT_RELAY_DIR;
  process.env.ASTRAQUOTE_GPT_RELAY_DIR = relayDirectory;
  fs.mkdirSync(path.join(relayDirectory, 'jobs'), { recursive: true });
  fs.writeFileSync(path.join(relayDirectory, 'jobs', `${relayJobId}.json`), JSON.stringify({
    job_id: relayJobId, submission_code: '5', status: 'processing',
    reserved_price_batch_id: reservedBatchId, intake_component_count: 2, intake_batch_count: 2,
    intake_batches: [
      { batch_index: 0, batch_count: 2, component_keys: ['cmp_intake_0001'], source_lines: [] },
      { batch_index: 1, batch_count: 2, component_keys: ['cmp_intake_0002'], source_lines: [] },
    ],
    quote_options: { cloud_provider: 'azure', pricing_scenarios: ['on_demand'] },
  }));
  t.after(() => {
    fs.rmSync(relayDirectory, { recursive: true, force: true });
    if (previousRelayDirectory === undefined) delete process.env.ASTRAQUOTE_GPT_RELAY_DIR;
    else process.env.ASTRAQUOTE_GPT_RELAY_DIR = previousRelayDirectory;
  });
  for (const [index, componentKey] of ['cmp_intake_0001', 'cmp_intake_0002'].entries()) {
    await workflow.getPrices({
      quote_mode: 'formal_quote', relay_job_id: relayJobId, submission_code: '5',
      relay_batch_index: index, relay_batch_count: 2, price_batch_id: reservedBatchId,
      queries: [query(`owned-${index}`, true)],
      query_contexts: [context(`owned-${index}`, 'compute', componentKey)],
      quote_components: [{ component_key: componentKey,
        customer_owned_source: `组件数量：${index + 1}。`, billing_scopes: [{ billing_key: 'compute' }] }],
    });
  }

  await assert.rejects(workflow.buildEstimate({
    delivery_mode: 'save_component_batch', relay_job_id: relayJobId,
    relay_batch_index: 0, relay_batch_count: 2, quote_name: '越界批次',
    cloud_provider: 'azure', default_region: 'eastasia', currency: 'USD',
    price_batch_id: reservedBatchId, expected_monthly_total: '1',
    pricing_scenarios: [{ scenario_key: 'on_demand', monthly_total: '1', upfront_total: '0' }],
    fact_ledger: [{ fact_id: 'F1', component_key: 'cmp_intake_0002', field: 'quantity',
      value: 2, unit: 'count', scope: 'total', cleaned_evidence: '组件数量：2。', disposition: 'billable' }],
    services: [{ component_key: 'cmp_intake_0002', region: 'eastasia',
      price_evidence: [{ query_id: 'owned-1', official_item_ids: ['item-1'] }], fact_ids: ['F1'],
      expected_monthly_cost: '1', scenario_costs: [{ scenario_key: 'on_demand',
        pricing_basis: 'on_demand', monthly_cost: '1', upfront_cost: '0',
        price_evidence: [{ query_id: 'owned-1', official_item_ids: ['item-1'] }] }],
      customer_facing: { service_name: '服务 2', model_or_plan: '规格', quantity: '2',
        requirement_summary: '组件数量：2。', configuration_summary: '组件数量：2。' } }],
    zero_cost_services: [], unpriced_services: [], assumptions: [], adjustments: [],
    idempotency_key: 'cross-batch-fragment-attempt',
  }), (error) => error.code === 'relay_component_result_batch_mismatch');
});

test('an old exact error envelope is re-queried and cannot count as completed evidence', async (t) => {
  const { workflow, store, calls } = fixture(t);
  const first = await workflow.getPrices({ queries: [query('price', true)] });
  const saved = store.getPriceBatch(first.price_batch_id);
  saved.result.results[0] = {
    query_id: 'price', provider: 'azure', status: 'exact', official_item_ids: ['hash-of-error'],
    items: [{ Error: { Code: 'UnauthorizedOperation', Message: 'Permission missing' } }],
    official_rate_candidates: [],
  };
  store.putPriceBatch(saved);
  const result = await workflow.getPrices({ price_batch_id: first.price_batch_id, queries: [query('price', true)] });
  assert.equal(calls.length, 2);
  assert.deepEqual(result.queried_query_ids, ['price']);
  assert.deepEqual(result.results[0].official_item_ids, ['item-1']);
});

test('explicit pricing without rates is retried while legacy unscoped successful results remain reusable', async (t) => {
  const { workflow, store, calls } = fixture(t);
  const first = await workflow.getPrices({ queries: [query('price', true)] });
  const saved = store.getPriceBatch(first.price_batch_id);
  saved.result.results[0].official_rate_candidates = [];
  store.putPriceBatch(saved);
  const legacy = await workflow.getPrices({ price_batch_id: first.price_batch_id, queries: [query('price', true)] });
  assert.deepEqual(legacy.reused_query_ids, ['price']);
  assert.equal(calls.length, 1);
  const scoped = await workflow.getPrices({ price_batch_id: first.price_batch_id, queries: [query('price', true)],
    query_contexts: [context('price')],
  });
  assert.deepEqual(scoped.queried_query_ids, ['price']);
  assert.equal(calls.length, 2);
});

test('a late failure cannot overwrite a concurrently completed identical query', async (t) => {
  const { workflow, store, backend } = fixture(t);
  const batch = await workflow.getPrices({ queries: [query('seed', true)] });
  const requests = [];
  backend.getPrices = (input) => new Promise((resolve) => requests.push({ input, resolve }));
  const args = { price_batch_id: batch.price_batch_id, queries: [query('concurrent', true)], query_contexts: [context('concurrent')] };
  const slow = workflow.getPrices(args);
  const fast = workflow.getPrices(args);
  requests[1].resolve({ results: [{ query_id: 'concurrent', provider: 'azure', status: 'exact',
    official_item_ids: ['item-2'], official_rate_candidates: [{ rate_id: 'rate-2', official_item_id: 'item-2', unit_price: '2', currency: 'USD' }],
  }] });
  await fast;
  requests[0].resolve({ results: [{ query_id: 'concurrent', provider: 'azure', status: 'query_failed', retryable: true }] });
  const late = await slow;
  const saved = store.getPriceBatch(batch.price_batch_id);
  assert.equal(saved.result.results.find((result) => result.query_id === 'concurrent').status, 'exact');
  assert.equal(late.results[0].status, 'exact');
});

test('a component plan with a parent cycle is rejected before official calls', async (t) => {
  const { workflow, calls } = fixture(t);
  await assert.rejects(workflow.getPrices({ queries: [query('first', true)], quote_components: [
    { component_key: 'cmp_first_0001', parent_component_key: 'cmp_other_0002', customer_owned_source: '独立清洗组件一。', billing_scopes: [{ billing_key: 'base' }] },
    { component_key: 'cmp_other_0002', parent_component_key: 'cmp_first_0001', customer_owned_source: '独立清洗组件二。', billing_scopes: [{ billing_key: 'base' }] },
  ] }), (error) => error.code === 'quote_component_plan_invalid');
  assert.equal(calls.length, 0);
});

test('publishing a component requires every sealed billing scope including scopes never queried', async (t) => {
  const { workflow, store } = fixture(t);
  const batch = await workflow.getPrices({ queries: [query('base', true)], query_contexts: [context('base', 'base')],
    quote_components: [{ component_key: 'cmp_resource_0001', customer_owned_source: '服务及容量配置。',
      billing_scopes: [{ billing_key: 'base' }, { billing_key: 'capacity' }],
    }],
  });
  assert.equal(batch.completed_component_count, 0);
  const input = { cloud_provider: 'azure', currency: 'USD', services: [{
    component_key: 'cmp_resource_0001', price_evidence: [{ query_id: 'base', official_item_ids: ['item-1'], official_rate_ids: ['rate-1'] }],
  }] };
  assert.throws(() => workflow.validatePriceEvidence(input, store.getPriceBatch(batch.price_batch_id)),
    (error) => error.details.violations.includes('billing_query_evidence_missing:cmp_resource_0001:capacity:base'));
});

test('explicitly unpriced components may keep failed attempts without blocking verified partial delivery', async (t) => {
  const { workflow, store } = fixture(t);
  const batch = await workflow.getPrices({ queries: [query('good', true), query('failed')],
    query_contexts: [context('good'), context('failed', 'capacity', 'cmp_resource_0002')],
  });
  const input = { is_partial: true, cloud_provider: 'azure', currency: 'USD', services: [{
    component_key: 'cmp_resource_0001', price_evidence: [{ query_id: 'good', official_item_ids: ['item-1'], official_rate_ids: ['rate-1'] }],
  }], unpriced_services: [{ component_key: 'cmp_resource_0002' }] };
  assert.doesNotThrow(() => workflow.validatePriceEvidence(input, store.getPriceBatch(batch.price_batch_id)));
  input.is_partial = false;
  assert.throws(() => workflow.validatePriceEvidence(input, store.getPriceBatch(batch.price_batch_id)),
    (error) => error.details.violations.some((violation) => violation.startsWith('billing_query_evidence_missing:')));
});

test('a terminal failure leaves other unqueried planned components running', async (t) => {
  const { workflow, backend } = fixture(t);
  backend.getPrices = async () => ({ results: [{ query_id: 'blocked', provider: 'azure', status: 'query_failed', terminal: true, retryable: false }] });
  const result = await workflow.getPrices({ queries: [query('blocked')], query_contexts: [context('blocked')],
    quote_components: [
      { component_key: 'cmp_resource_0001', customer_owned_source: '清洗后的组件一。', billing_scopes: [{ billing_key: 'compute' }] },
      { component_key: 'cmp_resource_0002', customer_owned_source: '清洗后的组件二。', billing_scopes: [{ billing_key: 'storage' }] },
    ],
  });
  assert.equal(result.failed_component_count, 1);
  assert.equal(result.pending_component_count, 1);
  assert.equal(result.quote_terminal, false);
  assert.equal(result.must_continue, true);
});

test('direct publication cannot bypass a saved exact error envelope', async (t) => {
  const { workflow, store } = fixture(t);
  const batch = await workflow.getPrices({ queries: [query('price', true)] });
  const saved = store.getPriceBatch(batch.price_batch_id);
  saved.result.results[0] = { query_id: 'price', provider: 'azure', status: 'exact',
    official_item_ids: ['hash-of-error'], items: [{ Error: { Code: 'Denied', Message: 'Access denied' } }],
  };
  const input = { cloud_provider: 'azure', currency: 'USD', services: [{
    component_key: 'cmp_resource_0001', price_evidence: [{ query_id: 'price', official_item_ids: ['hash-of-error'] }],
  }] };
  assert.throws(() => workflow.validatePriceEvidence(input, saved),
    (error) => error.details.violations.some((violation) => violation.startsWith('price_query_unusable:')));
});
