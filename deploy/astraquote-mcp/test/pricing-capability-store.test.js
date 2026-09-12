'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { PricingCapabilityStore } = require('../lib/pricing-capability-store');


function quoteQuery(overrides = {}) {
  return {
    provider: 'alibaba', query_id: 'price-1', endpoint: 'business.aliyuncs.com',
    service: 'bssopenapi', action: 'QueryPrice', version: '2017-12-14',
    region: 'ap-southeast-1', query_parameters: { ProductCode: 'ecs' }, body: {},
    ...overrides,
  };
}


function temporaryStore(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-capabilities-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return { directory, store: new PricingCapabilityStore({ directory }) };
}


test('capability memory never exposes or recreates fine-grained route reuse', (t) => {
  const { directory, store } = temporaryStore(t);
  store.recordCapabilityBatch([quoteQuery()], [{
    query_id: 'price-1', provider: 'alibaba', status: 'exact',
    official_item_ids: ['ecs.g8i.xlarge'],
  }]);

  assert.equal(typeof store.resolve, 'undefined');
  assert.equal(typeof store.materialize, 'undefined');
  assert.equal(typeof store.recordBatch, 'undefined');
  assert.ok(fs.existsSync(path.join(directory, 'pricing-capabilities.sqlite3')));
  const tables = store.database.prepare(
    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
  ).all().map((row) => row.name);
  assert.deepEqual(tables, ['capability_events']);
  assert.equal(typeof store.knowledgeForQuery, 'undefined');
});


test('a recent authorization denial blocks only the same live operation briefly', (t) => {
  const { store } = temporaryStore(t);
  const query = quoteQuery();
  store.recordCapabilityBatch([query], [{
    query_id: query.query_id, provider: query.provider, status: 'query_failed',
    error_category: 'authorization', code: 'alibaba_forbidden',
    details: { provider_code: 'Forbidden.RAM' },
    recovery: { next_action: 'verify_cloud_read_and_billing_access' },
  }]);

  assert.equal(store.capabilityBlocker(query).provider_code, 'Forbidden.RAM');
  assert.equal(store.capabilityBlocker(quoteQuery({ action: 'DescribePricingModule' })), null);
  assert.equal(store.capabilityBlocker(quoteQuery({ region: 'cn-hangzhou' })), null);
});


test('missing credentials block provider calls until a later official success', (t) => {
  const { store } = temporaryStore(t);
  const query = quoteQuery();
  store.recordCapabilityBatch([query], [{
    query_id: query.query_id, provider: query.provider, status: 'query_failed',
    error_category: 'credentials', code: 'alibaba_credentials_missing',
    recovery: { next_action: 'configure_official_api_credentials' },
  }]);
  const otherService = quoteQuery({
    query_id: 'another-service', service: 'r-kvstore', action: 'DescribePrice',
  });
  assert.equal(store.capabilityBlocker(otherService).error_category, 'credentials');

  store.recordCapabilityBatch([otherService], [{
    query_id: otherService.query_id, provider: 'alibaba', status: 'exact',
    official_item_ids: ['redis.master.small.default'],
  }]);
  assert.equal(store.capabilityBlocker(query), null);
});
