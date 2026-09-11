'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { PricingRouteStore } = require('../lib/pricing-route-store');


function verifiedResult(overrides = {}) {
  return {
    query_id: 'price-1',
    provider: 'alibaba',
    status: 'exact',
    official_item_ids: ['ecs.g8i.xlarge'],
    official_rate_candidates: [{ unit_price: '1.25', is_zero_rate: false }],
    route_verification: {
      route_fingerprint: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      provider: 'alibaba',
      endpoint: 'business.aliyuncs.com',
      service: 'bssopenapi',
      action: 'QueryPrice',
      version: '2017-12-14',
      region: 'ap-southeast-1',
      region_parameter: 'Region',
      method: 'POST',
      path: '/',
      response_items_path: 'Data.ModuleDetails.ModuleDetail',
      item_id_paths: ['ModuleCode'],
      rate_fields: [{ unit_price_path: 'CostAfterDiscount' }],
      auth_scheme: 'alibaba_rpc_hmac_sha1',
      request_schema_hash: 'sha256:request',
      response_schema_hash: 'sha256:response',
      sdk_version: 'astraquote-direct-signer/1',
      official_source_url: 'https://help.aliyun.com/document_detail/87913.html',
      last_verified_at: '2026-09-11T00:00:00.000Z',
      revalidate_after: '2026-09-18T00:00:00.000Z',
      expires_at: '2026-10-11T00:00:00.000Z',
      confidence: 0.75,
      failure_count: 0,
      ...overrides,
    },
  };
}


test('verified official route is learned without persisting quote-specific request values', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-routes-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const store = new PricingRouteStore({ directory });
  const query = {
    provider: 'alibaba', query_id: 'price-1', endpoint: 'business.aliyuncs.com',
    service: 'bssopenapi', action: 'QueryPrice', version: '2017-12-14',
    region: 'ap-southeast-1', region_parameter: 'Region', method: 'POST', path: '/',
    query_parameters: { ProductCode: 'ecs', CustomerSecretValue: 'must-not-persist' },
    body: { ModuleList: [{ Config: 'customer-specific-sku' }] },
  };

  const learned = store.recordBatch([query], [verifiedResult()]);

  assert.equal(learned.length, 1);
  assert.match(learned[0].route_id, /^aqr_[a-f0-9]{24}$/);
  const persisted = fs.readFileSync(path.join(directory, 'pricing-routes.json'), 'utf8');
  assert.doesNotMatch(persisted, /must-not-persist|customer-specific-sku|CustomerSecretValue/);
  assert.match(persisted, /request_schema_hash/);
  assert.match(persisted, /official_source_url/);
});


test('a learned route materializes transport and response contract for later quotes', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-routes-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const store = new PricingRouteStore({ directory });
  const [learned] = store.recordBatch([], [verifiedResult()]);

  const materialized = store.materialize({
    provider: 'alibaba',
    query_id: 'next-price',
    route_id: learned.route_id,
    region: 'ap-southeast-1',
    query_parameters: { ProductCode: 'rds' },
    body: {},
    response_filters: {},
  });

  assert.equal(materialized.route_id, undefined);
  assert.equal(materialized.endpoint, 'business.aliyuncs.com');
  assert.equal(materialized.service, 'bssopenapi');
  assert.equal(materialized.action, 'QueryPrice');
  assert.equal(materialized.region_parameter, 'Region');
  assert.equal(materialized.response_items_path, 'Data.ModuleDetails.ModuleDetail');
  assert.deepEqual(materialized.query_parameters, { ProductCode: 'rds' });
});


test('route reuse rejects provider or region drift instead of silently mispricing', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-routes-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const store = new PricingRouteStore({ directory });
  const [learned] = store.recordBatch([], [verifiedResult()]);

  assert.throws(
    () => store.materialize({
      provider: 'alibaba', query_id: 'wrong-region', route_id: learned.route_id,
      region: 'cn-hangzhou', query_parameters: {}, body: {}, response_filters: {},
    }),
    (error) => error.code === 'pricing_route_scope_mismatch',
  );
});


test('a learned route is revalidated on schedule instead of being trusted forever', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-routes-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const store = new PricingRouteStore({ directory });
  const [learned] = store.recordBatch([], [verifiedResult({
    last_verified_at: new Date(Date.now() - 8 * 86400 * 1000).toISOString(),
    revalidate_after: new Date(Date.now() - 86400 * 1000).toISOString(),
    expires_at: new Date(Date.now() + 22 * 86400 * 1000).toISOString(),
  })]);

  assert.throws(
    () => store.materialize({
      provider: 'alibaba', query_id: 'stale-route', route_id: learned.route_id,
      region: 'ap-southeast-1', query_parameters: {}, body: {}, response_filters: {},
    }),
    (error) => error.code === 'pricing_route_revalidation_required'
      && error.details.revalidation_due === true
      && error.details.expired === false,
  );
});


test('repeated schema failures quarantine one route revision without deleting history', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-routes-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const store = new PricingRouteStore({ directory });
  const [learned] = store.recordBatch([], [verifiedResult()]);
  const failed = {
    provider: 'alibaba', query_id: 'price-1', status: 'query_failed',
    error_category: 'invalid_request', retryable: true,
    route_fingerprint: verifiedResult().route_verification.route_fingerprint,
  };

  store.recordBatch([], [failed]);
  store.recordBatch([], [failed]);
  store.recordBatch([], [failed]);

  const route = store.get(learned.route_id);
  assert.equal(route.failure_count, 3);
  assert.equal(route.status, 'quarantined');
  assert.ok(route.history.length >= 1);
});
