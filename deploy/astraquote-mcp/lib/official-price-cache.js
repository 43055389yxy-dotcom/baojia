'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');


const DEFAULT_FRESH_TTL_MS = 6 * 60 * 60 * 1_000;
const DEFAULT_MAXIMUM_STALE_MS = 72 * 60 * 60 * 1_000;


function positiveInteger(value, fallback) {
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}


function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(
    Object.keys(value).sort().map((key) => [key, canonical(value[key])]),
  );
}


function queryIdentity(query, marketProfile) {
  const { query_id: ignoredQueryId, ...officialQuery } = query || {};
  return canonical({ market_profile: marketProfile || 'unknown', query: officialQuery });
}


function hasCommercialRate(result) {
  if (!['exact', 'ambiguous'].includes(result?.status)) return false;
  if (!Array.isArray(result.official_item_ids) || result.official_item_ids.length === 0) {
    return false;
  }
  return (result.official_rate_candidates || []).some((rate) => (
    rate?.is_zero_rate !== true
      && Number.isFinite(Number(rate?.unit_price))
      && Number(rate.unit_price) > 0
      && String(rate?.currency || '').length === 3
  ));
}


class OfficialPriceCache {
  constructor({
    directory = path.join(
      process.env.ASTRAQUOTE_V2_STATE_DIR || '/data/v2-quotes',
      'official-price-cache',
    ),
    freshTtlMs = positiveInteger(
      process.env.ASTRAQUOTE_OFFICIAL_PRICE_FRESH_SECONDS,
      DEFAULT_FRESH_TTL_MS / 1_000,
    ) * 1_000,
    maximumStaleMs = positiveInteger(
      process.env.ASTRAQUOTE_OFFICIAL_PRICE_MAX_STALE_SECONDS,
      DEFAULT_MAXIMUM_STALE_MS / 1_000,
    ) * 1_000,
    now = () => Date.now(),
  } = {}) {
    this.directory = directory;
    this.freshTtlMs = positiveInteger(freshTtlMs, DEFAULT_FRESH_TTL_MS);
    this.maximumStaleMs = Math.max(
      this.freshTtlMs,
      positiveInteger(maximumStaleMs, DEFAULT_MAXIMUM_STALE_MS),
    );
    this.now = now;
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  }

  _identity(query, marketProfile) {
    return queryIdentity(query, marketProfile);
  }

  _path(identity) {
    const digest = crypto.createHash('sha256')
      .update(JSON.stringify(identity)).digest('hex');
    return path.join(this.directory, `${digest}.json`);
  }

  put(query, result, { marketProfile } = {}) {
    if (!hasCommercialRate(result)) return false;
    const identity = this._identity(query, marketProfile);
    const observedAt = new Date(this.now()).toISOString();
    const record = {
      schema_version: 'astraquote-official-price-cache/1',
      identity,
      observed_at: observedAt,
      result: {
        ...result,
        query_id: undefined,
        cache_status: undefined,
        cache_age_seconds: undefined,
        live_error: undefined,
      },
    };
    const target = this._path(identity);
    const temporary = `${target}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`;
    fs.writeFileSync(temporary, `${JSON.stringify(record)}\n`, {
      encoding: 'utf8', mode: 0o600,
    });
    fs.renameSync(temporary, target);
    return true;
  }

  get(query, { marketProfile, allowStale = false } = {}) {
    const identity = this._identity(query, marketProfile);
    const target = this._path(identity);
    let record;
    try {
      record = JSON.parse(fs.readFileSync(target, 'utf8'));
    } catch {
      return null;
    }
    if (record?.schema_version !== 'astraquote-official-price-cache/1'
      || JSON.stringify(record.identity) !== JSON.stringify(identity)
      || !hasCommercialRate(record.result)) return null;
    const observed = Date.parse(String(record.observed_at || ''));
    if (!Number.isFinite(observed)) return null;
    const ageMs = Math.max(0, this.now() - observed);
    if (ageMs > this.maximumStaleMs) {
      try {
        fs.unlinkSync(target);
      } catch {
        // Another worker may already have removed the same expired entry.
      }
      return null;
    }
    if (!allowStale && ageMs > this.freshTtlMs) return null;
    return {
      ...record.result,
      query_id: query.query_id,
      cache_status: ageMs <= this.freshTtlMs ? 'fresh' : 'stale',
      official_price_observed_at: record.observed_at,
      cache_age_seconds: Math.floor(ageMs / 1_000),
      source_request_skipped: ageMs <= this.freshTtlMs,
    };
  }
}


module.exports = { OfficialPriceCache, hasCommercialRate, queryIdentity };
