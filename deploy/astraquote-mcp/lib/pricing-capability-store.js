'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');


const MARKET_PROFILE_CATALOG_CANDIDATES = [
  process.env.ASTRAQUOTE_MARKET_PROFILE_CATALOG_PATH,
  path.resolve(__dirname, '../policies/cloud-market-profiles.json'),
  path.resolve(__dirname, '../../policies/cloud-market-profiles.json'),
  path.resolve(__dirname, '../../../policies/cloud-market-profiles.json'),
].filter(Boolean);


function digestId(prefix, value) {
  return `${prefix}_${crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex').slice(0, 32)}`;
}


function configuredMarketProfile(provider) {
  const environmentKey = `ASTRAQUOTE_${String(provider).toUpperCase()}_MARKET_PROFILE`;
  if (process.env[environmentKey]) return process.env[environmentKey];
  for (const catalogPath of MARKET_PROFILE_CATALOG_CANDIDATES) {
    try {
      const catalog = JSON.parse(fs.readFileSync(catalogPath, 'utf8'));
      return catalog?.providers?.[provider]?.default_profile || null;
    } catch {
      // Try the next known packaging layout. No business decision is made here.
    }
  }
  return null;
}


function operationOf(value) {
  return String(value.action || value.path || '').trim();
}


class PricingCapabilityStore {
  constructor({ directory = path.join(
    process.env.ASTRAQUOTE_V2_STATE_DIR || '/data/v2-quotes',
    'pricing-capabilities',
  ) } = {}) {
    this.directory = directory;
    this.target = path.join(directory, 'pricing-capabilities.sqlite3');
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    this.database = new DatabaseSync(this.target);
    this.database.exec('PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;');
    this.database.exec(`
      CREATE TABLE IF NOT EXISTS capability_events (
        event_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        market_profile TEXT NOT NULL,
        service TEXT NOT NULL,
        region TEXT NOT NULL,
        operation TEXT NOT NULL,
        outcome TEXT NOT NULL,
        provider_code TEXT,
        error_category TEXT,
        recovery_action TEXT,
        last_seen_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS capability_events_scope_idx
        ON capability_events(
          provider, market_profile, service, region, operation, last_seen_at DESC
        );
    `);
    fs.chmodSync(this.target, 0o600);
  }

  _transaction(callback) {
    this.database.exec('BEGIN IMMEDIATE');
    try {
      const result = callback();
      this.database.exec('COMMIT');
      return result;
    } catch (error) {
      this.database.exec('ROLLBACK');
      throw error;
    }
  }

  _recordCapabilityEvent(query, result, now) {
    if (!query?.provider || !query?.service || !query?.region) return;
    const failed = result?.status === 'query_failed';
    if (!failed && !['exact', 'ambiguous'].includes(result?.status)) return;
    if (failed && !['credentials', 'authorization'].includes(result?.error_category)) return;
    const marketProfile = configuredMarketProfile(query.provider) || 'unknown';
    const providerCode = failed
      ? String(result?.details?.provider_code || result?.code || '') || null
      : null;
    const scope = {
      provider: query.provider,
      market_profile: marketProfile,
      service: query.service,
      region: query.region,
      operation: operationOf(query),
      outcome: failed ? 'rejected' : 'accepted',
      provider_code: providerCode,
      error_category: failed ? String(result.error_category) : null,
    };
    const id = digestId('aqc', scope);
    this.database.prepare(`
      INSERT INTO capability_events (
        event_id, provider, market_profile, service, region, operation, outcome,
        provider_code, error_category, recovery_action, last_seen_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(event_id) DO UPDATE SET
        last_seen_at=excluded.last_seen_at,
        recovery_action=excluded.recovery_action
    `).run(
      id,
      scope.provider,
      scope.market_profile,
      scope.service,
      scope.region,
      scope.operation,
      scope.outcome,
      scope.provider_code,
      scope.error_category,
      failed ? String(result?.recovery?.next_action || 'inspect_official_error_contract') : null,
      now,
    );
  }

  recordCapabilityBatch(queries, results) {
    const byQueryId = new Map((queries || []).map((query) => [query.query_id, query]));
    const now = new Date().toISOString();
    this._transaction(() => {
      for (const result of results || []) {
        const query = byQueryId.get(result?.query_id);
        if (query) this._recordCapabilityEvent(query, result, now);
      }
    });
  }

  marketProfile(provider) {
    return configuredMarketProfile(provider) || 'unknown';
  }

  capabilityBlocker(query, { maximumAgeMs = 5 * 60 * 1_000, now = Date.now() } = {}) {
    if (!query?.provider || !query?.service || !query?.region || !operationOf(query)) return null;
    const profile = configuredMarketProfile(query.provider) || 'unknown';
    const providerRows = this.database.prepare(`
      SELECT outcome, provider_code, error_category, recovery_action, last_seen_at
      FROM capability_events
      WHERE provider = ? AND market_profile = ?
        AND (outcome = 'accepted' OR error_category = 'credentials')
      ORDER BY last_seen_at DESC, rowid DESC LIMIT 50
    `).all(query.provider, profile);
    const providerAcceptedAt = providerRows
      .filter((row) => row.outcome === 'accepted')
      .map((row) => Date.parse(row.last_seen_at))
      .filter(Number.isFinite).reduce((latest, value) => Math.max(latest, value), 0);
    const credentialFailure = providerRows.find((row) => row.outcome === 'rejected'
      && row.error_category === 'credentials');
    const credentialFailureAt = Date.parse(credentialFailure?.last_seen_at || '');
    if (credentialFailure
      && Number.isFinite(credentialFailureAt)
      && credentialFailureAt > providerAcceptedAt
      && now - credentialFailureAt <= maximumAgeMs) {
      return {
        error_category: 'credentials',
        provider_code: credentialFailure.provider_code || null,
        next_action: credentialFailure.recovery_action || 'configure_official_api_credentials',
        observed_at: credentialFailure.last_seen_at,
        recheck_after: new Date(credentialFailureAt + maximumAgeMs).toISOString(),
      };
    }
    const rows = this.database.prepare(`
      SELECT outcome, provider_code, error_category, recovery_action, last_seen_at
      FROM capability_events
      WHERE provider = ? AND market_profile = ? AND service = ? AND region = ?
        AND operation = ?
      ORDER BY last_seen_at DESC, rowid DESC LIMIT 20
    `).all(query.provider, profile, query.service, query.region, operationOf(query));
    const acceptedAt = rows
      .filter((row) => row.outcome === 'accepted')
      .map((row) => Date.parse(row.last_seen_at))
      .filter(Number.isFinite).reduce((latest, value) => Math.max(latest, value), 0);
    const denied = rows.find((row) => row.outcome === 'rejected'
      && row.error_category === 'authorization');
    if (!denied) return null;
    const deniedAt = Date.parse(denied.last_seen_at);
    if (!Number.isFinite(deniedAt) || deniedAt <= acceptedAt || now - deniedAt > maximumAgeMs) {
      return null;
    }
    return {
      error_category: 'authorization',
      provider_code: denied.provider_code || null,
      next_action: denied.recovery_action || 'verify_cloud_read_and_billing_access',
      observed_at: denied.last_seen_at,
      recheck_after: new Date(deniedAt + maximumAgeMs).toISOString(),
    };
  }
}


module.exports = { PricingCapabilityStore };
