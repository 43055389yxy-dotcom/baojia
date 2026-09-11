'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { AstraQuoteV2Workflow } = require('../lib/v2-workflow');
const { V2QuoteStore } = require('../lib/v2-quote-store');

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
  const workflow = new AstraQuoteV2Workflow({ backend, store, deliverer: {} });
  return { workflow, store, calls, backend };
}

const query = (id, valid = false, provider = 'azure') => ({
  query_id: id, provider, filter: valid ? 'valid' : 'missing',
});
const context = (id, billing = 'compute', component = 'cmp_resource_0001') => ({
  query_id: id, purpose: 'pricing', component_key: component, billing_key: billing,
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
  const restarted = new AstraQuoteV2Workflow({ backend: workflow.backend, store, deliverer: {} });
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
