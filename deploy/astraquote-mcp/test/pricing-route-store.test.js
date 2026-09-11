'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { PricingRouteStore } = require('../lib/pricing-route-store');


function verifiedResult(overrides = {}, queryId = 'price-1') {
  return {
    query_id: queryId,
    provider: 'alibaba',
    status: 'exact',
    official_item_ids: ['ecs.g8i.xlarge'],
    official_rate_candidates: [{ unit_price: '1.25', is_zero_rate: false }],
    route_verification: {
      route_contract_version: 3,
      route_fingerprint: 'sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      provider: 'alibaba',
      market_profile: 'alibaba-cn',
      credential_scope: 'alibaba-cn',
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
      rate_fields: [{ unit_price_path: 'CostAfterDiscount', currency_code: 'CNY' }],
      auth_scheme: 'alibaba_rpc_hmac_sha1',
      request_schema_hash: 'sha256:request',
      response_schema_hash: 'sha256:response',
      sdk_version: 'astraquote-direct-signer/1',
      official_source_url: 'https://help.aliyun.com/document_detail/87913.html',
      last_verified_at: '2026-09-11T00:00:00.000Z',
      confidence: 0.75,
      failure_count: 0,
      ...overrides,
    },
  };
}


function quoteQuery(overrides = {}) {
  return {
    provider: 'alibaba', query_id: 'price-1', endpoint: 'business.aliyuncs.com',
    service: 'bssopenapi', action: 'QueryPrice', version: '2017-12-14',
    region: 'ap-southeast-1', region_parameter: 'Region', method: 'POST', path: '/',
    query_parameters: { ProductCode: 'ecs', CustomerSecretValue: 'must-not-persist' },
    body: { ModuleList: [{ Config: 'customer-specific-sku' }] },
    ...overrides,
  };
}


function temporaryStore(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-routes-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return { directory, store: new PricingRouteStore({ directory }) };
}


test('verified official route is learned without persisting quote-specific request values', (t) => {
  const { directory, store } = temporaryStore(t);
  const learned = store.recordBatch([quoteQuery()], [verifiedResult()]);

  assert.equal(learned.length, 1);
  assert.match(learned[0].route_id, /^aqr_[a-f0-9]{24}$/);
  assert.ok(fs.existsSync(path.join(directory, 'pricing-routes.sqlite3')));
  const persistedKnowledge = JSON.stringify({
    routes: store.load().routes,
    experiences: store.knowledgeForQuery(quoteQuery()),
  });
  assert.doesNotMatch(
    persistedKnowledge,
    /must-not-persist|customer-specific-sku|CustomerSecretValue/,
  );
  assert.match(persistedKnowledge, /query_parameters\.ProductCode/);
  assert.match(persistedKnowledge, /request_schema_hash/);
  assert.match(persistedKnowledge, /official_source_url/);
});


test('a route without current learning contract metadata is not learned', (t) => {
  const { store } = temporaryStore(t);
  const legacy = verifiedResult();
  delete legacy.route_verification.route_contract_version;

  assert.deepEqual(store.recordBatch([], [legacy]), []);
  assert.equal(store.load().routes.length, 0);
});


test('a learned route is automatically reused by exact provider, site, service and region', (t) => {
  const { store } = temporaryStore(t);
  store.recordBatch([quoteQuery()], [verifiedResult()]);

  const materialized = store.materialize({
    provider: 'alibaba', query_id: 'next-price', service: 'bssopenapi',
    region: 'ap-southeast-1', query_parameters: { ProductCode: 'rds' },
    body: {}, response_filters: {},
  });

  assert.equal(materialized.route_id, undefined);
  assert.equal(materialized.endpoint, 'business.aliyuncs.com');
  assert.equal(materialized.action, 'QueryPrice');
  assert.equal(materialized.region_parameter, 'Region');
  assert.equal(materialized.response_items_path, 'Data.ModuleDetails.ModuleDetail');
  assert.deepEqual(materialized.query_parameters, { ProductCode: 'rds' });
});


test('route reuse rejects provider or region drift instead of silently mispricing', (t) => {
  const { store } = temporaryStore(t);
  const [learned] = store.recordBatch([quoteQuery()], [verifiedResult()]);

  assert.throws(
    () => store.materialize({
      provider: 'alibaba', query_id: 'wrong-region', route_id: learned.route_id,
      region: 'cn-hangzhou', query_parameters: {}, body: {}, response_filters: {},
    }),
    (error) => error.code === 'pricing_route_scope_mismatch',
  );
});


test('a successful route remains reusable regardless of old revalidation dates', (t) => {
  const { store } = temporaryStore(t);
  const [learned] = store.recordBatch([quoteQuery()], [verifiedResult({
    last_verified_at: '2020-01-01T00:00:00.000Z',
    revalidate_after: '2020-01-08T00:00:00.000Z',
    expires_at: '2020-02-01T00:00:00.000Z',
  })]);

  const materialized = store.materialize({
    provider: 'alibaba', query_id: 'old-route', route_id: learned.route_id,
    region: 'ap-southeast-1', query_parameters: {}, body: {}, response_filters: {},
  });
  assert.equal(materialized.endpoint, 'business.aliyuncs.com');
  assert.equal(store.get(learned.route_id).status, 'active');
});


test('schema-incompatible route records require rediscovery', (t) => {
  const { store } = temporaryStore(t);
  const [learned] = store.recordBatch([quoteQuery()], [verifiedResult()]);
  const payload = store.load();
  payload.routes[0].schema_version = 'astraquote-pricing-route/1';
  store.save(payload);

  assert.throws(
    () => store.materialize({
      provider: 'alibaba', query_id: 'old-contract', route_id: learned.route_id,
      region: 'ap-southeast-1', query_parameters: {}, body: {}, response_filters: {},
    }),
    (error) => error.code === 'pricing_route_revalidation_required'
      && error.details.reason === 'route_schema_changed',
  );
});


test('parameter errors are remembered but never damage a valid route', (t) => {
  const { store } = temporaryStore(t);
  const query = quoteQuery();
  const [learned] = store.recordBatch([query], [verifiedResult()]);
  const rejected = {
    provider: 'alibaba', query_id: 'price-1', status: 'query_failed',
    error_category: 'invalid_request', retryable: true,
    code: 'alibaba_invalid_system_disk_category_value_not_supported',
    details: { provider_code: 'InvalidSystemDiskCategory.ValueNotSupported' },
    recovery: { next_action: 'repair_official_request_schema' },
    route_fingerprint: verifiedResult().route_verification.route_fingerprint,
    reused_route_id: learned.route_id,
  };

  store.recordBatch([query], [rejected]);
  store.recordBatch([query], [rejected]);
  store.recordBatch([query], [rejected]);

  const route = store.get(learned.route_id);
  const knowledge = store.knowledgeForQuery(query);
  assert.equal(route.failure_count, 0);
  assert.equal(route.status, 'active');
  assert.ok(knowledge.some((item) => (
    item.provider_code === 'InvalidSystemDiskCategory.ValueNotSupported'
      && item.outcome === 'rejected'
      && item.seen_count === 3
  )));
  assert.doesNotMatch(JSON.stringify(knowledge), /customer-specific-sku|must-not-persist/);
});


test('three consecutive route-level failures quarantine only that route revision', (t) => {
  const { store } = temporaryStore(t);
  const query = quoteQuery();
  const [learned] = store.recordBatch([query], [verifiedResult()]);
  const failed = {
    provider: 'alibaba', query_id: 'price-1', status: 'query_failed',
    error_category: 'response_schema', retryable: true,
    route_fingerprint: verifiedResult().route_verification.route_fingerprint,
    reused_route_id: learned.route_id,
  };

  store.recordBatch([query], [failed]);
  store.recordBatch([query], [failed]);
  assert.equal(store.get(learned.route_id).status, 'active');
  assert.deepEqual(store.healthForRoute(learned.route_id), {
    route_id: learned.route_id,
    status: 'active',
    failure_count: 2,
    remaining_attempts: 1,
    next_action: 'retry_cached_route_with_current_quote_parameters',
  });
  store.recordBatch([query], [failed]);

  const route = store.get(learned.route_id);
  assert.equal(route.failure_count, 3);
  assert.equal(route.status, 'quarantined');
  assert.equal(store.healthForRoute(learned.route_id).remaining_attempts, 0);
  assert.equal(
    store.healthForRoute(learned.route_id).next_action,
    'discover_and_verify_official_read_only_route',
  );
  assert.ok(route.history.some((item) => item.event === 'verified'));
});


test('transactional SQLite updates retain routes learned by concurrent store instances', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-routes-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const firstStore = new PricingRouteStore({ directory });
  const secondStore = new PricingRouteStore({ directory });

  firstStore.recordBatch([quoteQuery()], [verifiedResult()]);
  secondStore.recordBatch(
    [quoteQuery({ query_id: 'price-2', service: 'ecs', action: 'DescribePrice' })],
    [verifiedResult({
      route_fingerprint: 'sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      service: 'ecs',
      action: 'DescribePrice',
      endpoint: 'ecs.ap-southeast-1.aliyuncs.com',
    }, 'price-2')],
  );

  assert.equal(firstStore.load().routes.length, 2);
  assert.equal(secondStore.load().routes.length, 2);
});


test('legacy JSON routes migrate once and remain reusable without date expiry', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-routes-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const verification = verifiedResult({
    revalidate_after: '2020-01-08T00:00:00.000Z',
    expires_at: '2020-02-01T00:00:00.000Z',
  }).route_verification;
  const id = `aqr_${crypto.createHash('sha256')
    .update(verification.route_fingerprint).digest('hex').slice(0, 24)}`;
  fs.writeFileSync(path.join(directory, 'pricing-routes.json'), JSON.stringify({
    routes: [{
      ...verification,
      route_id: id,
      schema_version: 'astraquote-pricing-route/3',
      status: 'active',
      confidence: 0.75,
    }],
  }));

  const store = new PricingRouteStore({ directory });
  assert.equal(store.get(id).schema_version, 'astraquote-pricing-route/4');
  assert.equal(store.materialize({
    provider: 'alibaba', query_id: 'migrated', route_id: id,
    region: 'ap-southeast-1', query_parameters: {}, body: {}, response_filters: {},
  }).endpoint, 'business.aliyuncs.com');
});


test('legacy routes from an incompatible contract are retained but never trusted', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-routes-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const verification = verifiedResult().route_verification;
  const id = `aqr_${crypto.createHash('sha256')
    .update(verification.route_fingerprint).digest('hex').slice(0, 24)}`;
  fs.writeFileSync(path.join(directory, 'pricing-routes.json'), JSON.stringify({
    routes: [{
      ...verification,
      route_id: id,
      schema_version: 'astraquote-pricing-route/1',
      status: 'active',
      confidence: 0.75,
    }],
  }));

  const store = new PricingRouteStore({ directory });
  assert.throws(
    () => store.get(id),
    (error) => error.code === 'pricing_route_revalidation_required',
  );
  assert.equal(store.load().routes[0].status, 'quarantined');
});
