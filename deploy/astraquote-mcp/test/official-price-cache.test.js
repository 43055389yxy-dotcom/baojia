'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { OfficialPriceCache } = require('../lib/official-price-cache');


function temporaryCache(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-price-cache-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  let now = Date.parse('2026-09-12T00:00:00.000Z');
  return {
    directory,
    cache: new OfficialPriceCache({
      directory,
      freshTtlMs: 60_000,
      maximumStaleMs: 300_000,
      now: () => now,
    }),
    advance(milliseconds) { now += milliseconds; },
  };
}


function query(overrides = {}) {
  return {
    provider: 'oci', query_id: 'price-1', service: 'Compute', region: 'ap-singapore-1',
    currency: 'USD', filters: { partNumber: 'B93113' }, ...overrides,
  };
}


function exactResult(overrides = {}) {
  return {
    query_id: 'price-1', provider: 'oci', status: 'exact',
    official_item_ids: ['B93113'],
    official_rate_candidates: [{
      rate_id: 'B93113', official_item_id: 'B93113', unit_price: '0.0255', currency: 'USD',
    }],
    ...overrides,
  };
}


test('exact official prices are reused only for the identical market and query identity', (t) => {
  const { cache } = temporaryCache(t);
  assert.equal(cache.put(query(), exactResult(), { marketProfile: 'global' }), true);

  const hit = cache.get(query({ query_id: 'price-2' }), {
    marketProfile: 'global', allowStale: false,
  });
  assert.equal(hit.query_id, 'price-2');
  assert.equal(hit.cache_status, 'fresh');
  assert.equal(hit.official_item_ids[0], 'B93113');
  assert.equal(cache.get(query({ region: 'uk-london-1' }), {
    marketProfile: 'global', allowStale: true,
  }), null);
  assert.equal(cache.get(query(), {
    marketProfile: 'oracle-cn', allowStale: true,
  }), null);
});


test('an expired official price is available only as a bounded stale fallback', (t) => {
  const clock = temporaryCache(t);
  clock.cache.put(query(), exactResult(), { marketProfile: 'global' });
  clock.advance(90_000);

  assert.equal(clock.cache.get(query(), {
    marketProfile: 'global', allowStale: false,
  }), null);
  assert.equal(clock.cache.get(query(), {
    marketProfile: 'global', allowStale: true,
  }).cache_status, 'stale');

  clock.advance(300_000);
  assert.equal(clock.cache.get(query(), {
    marketProfile: 'global', allowStale: true,
  }), null);
  assert.deepEqual(fs.readdirSync(clock.directory), []);
});


test('catalog envelopes without a positive official rate are never cached', (t) => {
  const { cache } = temporaryCache(t);
  assert.equal(cache.put(query(), exactResult({ official_rate_candidates: [] }), {
    marketProfile: 'global',
  }), false);
  assert.equal(cache.put(query(), exactResult({
    official_rate_candidates: [{
      rate_id: 'free', official_item_id: 'B93113', unit_price: '0', currency: 'USD',
      is_zero_rate: true,
    }],
  }), { marketProfile: 'global' }), false);
});
