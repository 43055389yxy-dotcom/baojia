'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { DatabaseSync } = require('node:sqlite');


class PricingRouteStoreError extends Error {
  constructor(message, {
    code = 'pricing_route_store_error', details = {}, retryable = false,
  } = {}) {
    super(message);
    this.name = 'PricingRouteStoreError';
    this.code = code;
    this.details = details;
    this.retryable = retryable;
  }
}


const ROUTE_FIELDS = Object.freeze([
  'provider', 'endpoint', 'service', 'action', 'version', 'region',
  'region_parameter', 'method', 'path', 'response_items_path', 'item_id_paths',
  'rate_fields', 'next_page_path', 'official_source_url', 'sdk_version',
]);
const ROUTE_SCHEMA_VERSION = 'astraquote-pricing-route/4';
const ROUTE_STORE_SCHEMA_VERSION = 'astraquote-pricing-routes-sqlite/1';
const KNOWLEDGE_SCHEMA_VERSION = 'astraquote-pricing-knowledge/1';
const ROUTE_FAILURE_CATEGORIES = new Set(['route_not_found', 'response_schema']);
const AUTHENTICATED_ROUTE_PROVIDERS = new Set([
  'tencent', 'alibaba', 'huawei', 'baidu', 'volcengine', 'ctyun',
]);
const SENSITIVE_REQUEST_FIELD = /(?:secret|password|credential|authorization|signature|access[_-]?key)/i;
const MARKET_PROFILE_CATALOG_CANDIDATES = [
  process.env.ASTRAQUOTE_MARKET_PROFILE_CATALOG_PATH,
  path.resolve(__dirname, '../policies/cloud-market-profiles.json'),
  path.resolve(__dirname, '../../policies/cloud-market-profiles.json'),
  path.resolve(__dirname, '../../../policies/cloud-market-profiles.json'),
].filter(Boolean);


function routeId(fingerprint) {
  return `aqr_${crypto.createHash('sha256').update(String(fingerprint)).digest('hex').slice(0, 24)}`;
}


function digestId(prefix, value) {
  return `${prefix}_${crypto.createHash('sha256').update(JSON.stringify(value)).digest('hex').slice(0, 32)}`;
}


function cleanRouteVerification(verification) {
  const clean = {};
  for (const field of ROUTE_FIELDS) {
    if (verification[field] !== undefined && verification[field] !== null) {
      clean[field] = verification[field];
    }
  }
  for (const field of [
    'route_contract_version', 'route_fingerprint', 'auth_scheme',
    'market_profile', 'credential_scope',
    'request_schema_hash', 'response_schema_hash',
    'last_verified_at', 'revalidate_after', 'expires_at',
  ]) {
    if (verification[field] !== undefined && verification[field] !== null) {
      clean[field] = verification[field];
    }
  }
  return clean;
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


function valueKind(value) {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  if (typeof value === 'object') return 'object';
  if (typeof value === 'number') return Number.isInteger(value) ? 'integer' : 'number';
  return typeof value;
}


function collectRequestFields(value, prefix, fields) {
  const kind = valueKind(value);
  if (prefix) fields.set(`${prefix}:${kind}`, { field: prefix, kind });
  if (Array.isArray(value)) {
    for (const item of value.slice(0, 3)) {
      collectRequestFields(item, `${prefix}[]`, fields);
    }
    return;
  }
  if (!value || typeof value !== 'object') return;
  for (const key of Object.keys(value).sort()) {
    if (SENSITIVE_REQUEST_FIELD.test(key)) continue;
    collectRequestFields(value[key], prefix ? `${prefix}.${key}` : key, fields);
  }
}


function requestFieldShape(query) {
  const fields = new Map();
  collectRequestFields(query.query_parameters || {}, 'query_parameters', fields);
  collectRequestFields(query.body || {}, 'body', fields);
  return [...fields.values()].sort((left, right) => left.field.localeCompare(right.field));
}


function mergeRequestFields(left, right) {
  const fields = new Map();
  for (const item of [...(left || []), ...(right || [])]) {
    if (item?.field && item?.kind) fields.set(`${item.field}:${item.kind}`, item);
  }
  return [...fields.values()].sort((a, b) => a.field.localeCompare(b.field));
}


function routeContractKey(route) {
  return JSON.stringify({
    provider: route.provider,
    endpoint: route.endpoint,
    service: route.service,
    action: route.action || '',
    version: route.version || '',
    region: route.region,
    region_parameter: route.region_parameter || '',
    method: route.method || 'POST',
    path: route.path || '/',
    response_items_path: route.response_items_path || '',
    item_id_paths: route.item_id_paths || [],
    rate_fields: route.rate_fields || [],
    next_page_path: route.next_page_path || '',
  });
}


function operationOf(value) {
  return String(value.action || value.path || '').trim();
}


class PricingRouteStore {
  constructor({ directory = path.join(
    process.env.ASTRAQUOTE_V2_STATE_DIR || '/data/v2-quotes',
    'pricing-routes',
  ) } = {}) {
    this.directory = directory;
    this.target = path.join(directory, 'pricing-routes.sqlite3');
    this.legacyTarget = path.join(directory, 'pricing-routes.json');
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    this.database = new DatabaseSync(this.target);
    this.database.exec('PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;');
    this.database.exec(`
      CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS routes (
        route_id TEXT PRIMARY KEY,
        route_fingerprint TEXT NOT NULL UNIQUE,
        provider TEXT NOT NULL,
        market_profile TEXT NOT NULL,
        credential_scope TEXT NOT NULL,
        service TEXT NOT NULL,
        region TEXT NOT NULL,
        operation TEXT NOT NULL,
        status TEXT NOT NULL,
        confidence REAL NOT NULL,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
      );
      CREATE INDEX IF NOT EXISTS routes_scope_idx
        ON routes(provider, market_profile, service, region, status, confidence DESC);
      CREATE TABLE IF NOT EXISTS parameter_experiences (
        knowledge_id TEXT PRIMARY KEY,
        schema_version TEXT NOT NULL,
        provider TEXT NOT NULL,
        market_profile TEXT NOT NULL,
        credential_scope TEXT NOT NULL,
        service TEXT NOT NULL,
        region TEXT NOT NULL,
        operation TEXT NOT NULL,
        api_version TEXT NOT NULL,
        outcome TEXT NOT NULL,
        provider_code TEXT,
        error_category TEXT,
        request_fields_json TEXT NOT NULL,
        recovery_action TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        seen_count INTEGER NOT NULL
      );
      CREATE INDEX IF NOT EXISTS parameter_experiences_scope_idx
        ON parameter_experiences(
          provider, market_profile, service, region, operation, last_seen_at DESC
        );
    `);
    fs.chmodSync(this.target, 0o600);
    this._migrateLegacyJson();
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

  _routeFromRow(row) {
    if (!row) return null;
    try {
      return JSON.parse(row.payload_json);
    } catch (error) {
      throw new PricingRouteStoreError('Verified pricing route could not be decoded.', {
        code: 'pricing_route_store_corrupt',
        details: { route_id: row.route_id, exception_type: error?.name || 'Error' },
      });
    }
  }

  _putRoute(record) {
    this.database.prepare(`
      INSERT INTO routes (
        route_id, route_fingerprint, provider, market_profile, credential_scope,
        service, region, operation, status, confidence, payload_json, updated_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(route_id) DO UPDATE SET
        route_fingerprint=excluded.route_fingerprint,
        provider=excluded.provider,
        market_profile=excluded.market_profile,
        credential_scope=excluded.credential_scope,
        service=excluded.service,
        region=excluded.region,
        operation=excluded.operation,
        status=excluded.status,
        confidence=excluded.confidence,
        payload_json=excluded.payload_json,
        updated_at=excluded.updated_at
    `).run(
      record.route_id,
      record.route_fingerprint,
      record.provider,
      record.market_profile || configuredMarketProfile(record.provider) || 'unknown',
      record.credential_scope || record.market_profile || 'unknown',
      record.service,
      record.region,
      operationOf(record),
      record.status,
      Number(record.confidence || 0),
      JSON.stringify(record),
      record.last_verified_at || record.last_failure_at || new Date().toISOString(),
    );
  }

  _migrateLegacyJson() {
    const migrated = this.database.prepare(
      'SELECT value FROM metadata WHERE key = ?',
    ).get('legacy_json_migrated');
    if (migrated || !fs.existsSync(this.legacyTarget)) return;
    let payload;
    try {
      payload = JSON.parse(fs.readFileSync(this.legacyTarget, 'utf8'));
    } catch (error) {
      throw new PricingRouteStoreError('Legacy verified pricing routes could not be read.', {
        code: 'pricing_route_store_corrupt',
        details: { exception_type: error?.name || 'Error' },
      });
    }
    this._transaction(() => {
      for (const legacy of payload.routes || []) {
        if (!legacy?.route_id || !legacy?.route_fingerprint
          || !legacy?.provider || !legacy?.service || !legacy?.region) continue;
        const compatible = legacy.schema_version === 'astraquote-pricing-route/3'
          || legacy.schema_version === ROUTE_SCHEMA_VERSION;
        this._putRoute(compatible
          ? { ...legacy, schema_version: ROUTE_SCHEMA_VERSION }
          : { ...legacy, status: 'quarantined' });
      }
      this.database.prepare(
        'INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)',
      ).run('legacy_json_migrated', new Date().toISOString());
    });
  }

  load() {
    const rows = this.database.prepare(
      'SELECT route_id, payload_json FROM routes ORDER BY updated_at, route_id',
    ).all();
    return {
      schema_version: ROUTE_STORE_SCHEMA_VERSION,
      routes: rows.map((row) => this._routeFromRow(row)),
    };
  }

  save(payload) {
    this._transaction(() => {
      this.database.exec('DELETE FROM routes');
      for (const route of payload.routes || []) this._putRoute(route);
    });
  }

  get(id) {
    if (!/^aqr_[a-f0-9]{24}$/.test(String(id || ''))) {
      throw new PricingRouteStoreError('Invalid pricing route ID.', {
        code: 'pricing_route_id_invalid', details: { route_id: id || null },
      });
    }
    const route = this._routeFromRow(this.database.prepare(
      'SELECT route_id, payload_json FROM routes WHERE route_id = ?',
    ).get(id));
    if (!route) {
      throw new PricingRouteStoreError('Verified pricing route was not found.', {
        code: 'pricing_route_not_found', details: { route_id: id }, retryable: true,
      });
    }
    if (route.schema_version !== ROUTE_SCHEMA_VERSION) {
      throw new PricingRouteStoreError('Verified pricing route must be rediscovered.', {
        code: 'pricing_route_revalidation_required',
        details: { route_id: route.route_id, reason: 'route_schema_changed' },
        retryable: true,
      });
    }
    return route;
  }

  _usableRoutes(query) {
    const profile = configuredMarketProfile(query.provider);
    if (!query.provider || !query.service || !query.region || !profile) return [];
    const rows = this.database.prepare(`
      SELECT route_id, payload_json FROM routes
      WHERE provider = ? AND market_profile = ? AND service = ? AND region = ?
        AND status = 'active'
      ORDER BY confidence DESC, updated_at DESC
    `).all(query.provider, profile, query.service, query.region);
    let routes = rows.map((row) => this._routeFromRow(row));
    if (query.action) routes = routes.filter((route) => route.action === query.action);
    if (query.path && query.path !== '/') routes = routes.filter((route) => route.path === query.path);
    if (query.version) routes = routes.filter((route) => route.version === query.version);
    return routes;
  }

  _selectCachedRoute(query) {
    const candidates = this._usableRoutes(query);
    const groups = new Map();
    for (const route of candidates) {
      const key = routeContractKey(route);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(route);
    }
    if (groups.size === 0) {
      throw new PricingRouteStoreError('No verified pricing route matches this scope.', {
        code: 'pricing_route_discovery_required',
        retryable: true,
        details: {
          provider: query.provider || null,
          market_profile: configuredMarketProfile(query.provider),
          service: query.service || null,
          region: query.region || null,
          operation: operationOf(query) || null,
          next_action: 'discover_and_verify_official_read_only_route',
        },
      });
    }
    if (groups.size > 1) {
      const choices = [...groups.values()].map((routes) => {
        const route = routes[0];
        return {
          route_id: route.route_id,
          operation: operationOf(route),
          version: route.version || null,
          endpoint: route.endpoint,
          confidence: route.confidence,
          observed_request_fields: route.observed_request_fields || [],
        };
      }).slice(0, 12);
      throw new PricingRouteStoreError('More than one verified pricing route matches this scope.', {
        code: 'pricing_route_selection_required',
        retryable: true,
        details: { candidates: choices, next_action: 'choose_route_id_and_retry' },
      });
    }
    return [...groups.values()][0][0];
  }

  resolve(query) {
    let route = null;
    if (query.route_id) route = this.get(query.route_id);
    else if (AUTHENTICATED_ROUTE_PROVIDERS.has(query.provider)
      && !query.endpoint && query.service && query.region) {
      route = this._selectCachedRoute(query);
    }
    if (!route) return { query: { ...query }, route: null };

    if (route.status !== 'active') {
      throw new PricingRouteStoreError('Verified pricing route has been quarantined.', {
        code: 'pricing_route_revalidation_required',
        details: { route_id: route.route_id, status: route.status },
        retryable: true,
      });
    }
    const activeProfile = configuredMarketProfile(route.provider);
    if (activeProfile && route.market_profile !== activeProfile) {
      throw new PricingRouteStoreError('Verified pricing route belongs to a different account site.', {
        code: 'pricing_route_revalidation_required',
        details: { route_id: route.route_id, reason: 'market_profile_changed' },
        retryable: true,
      });
    }
    if (query.provider !== route.provider || (query.region && query.region !== route.region)) {
      throw new PricingRouteStoreError('Verified pricing route has a different provider or region.', {
        code: 'pricing_route_scope_mismatch',
        details: {
          route_id: route.route_id,
          expected_provider: route.provider,
          expected_region: route.region,
          received_provider: query.provider,
          received_region: query.region || null,
        },
        retryable: true,
      });
    }
    for (const field of ROUTE_FIELDS) {
      if (['provider', 'region'].includes(field)) continue;
      if (query[field] !== undefined
        && JSON.stringify(query[field]) !== JSON.stringify(route[field])) {
        throw new PricingRouteStoreError('Caller transport fields conflict with the verified route.', {
          code: 'pricing_route_identity_conflict',
          details: { route_id: route.route_id, field },
          retryable: true,
        });
      }
    }
    const { route_id: ignoredRouteId, ...dynamic } = query;
    const materialized = {};
    for (const field of ROUTE_FIELDS) {
      if (route[field] !== undefined) materialized[field] = route[field];
    }
    return {
      route,
      query: {
        ...materialized,
        ...dynamic,
        provider: route.provider,
        region: route.region,
        endpoint: route.endpoint,
        service: route.service,
        action: route.action || '',
        method: route.method || 'POST',
        path: route.path || '/',
      },
    };
  }

  materialize(query) {
    return this.resolve(query).query;
  }

  _recordExperience(query, result, now) {
    if (!query?.provider || !query?.service || !query?.region) return;
    const failed = result?.status === 'query_failed';
    if (!failed && !['exact', 'ambiguous'].includes(result?.status)) return;
    const marketProfile = configuredMarketProfile(query.provider) || 'unknown';
    const credentialScope = result?.route_verification?.credential_scope || marketProfile;
    const requestFields = requestFieldShape(query);
    const providerCode = failed
      ? String(result?.details?.provider_code || result?.code || '') || null
      : null;
    const outcome = failed ? 'rejected' : 'accepted';
    const scope = {
      provider: query.provider,
      market_profile: marketProfile,
      credential_scope: credentialScope,
      service: query.service,
      region: query.region,
      operation: operationOf(query),
      api_version: String(query.version || ''),
      outcome,
      provider_code: providerCode,
      error_category: failed ? String(result.error_category || 'official_api_error') : null,
      request_fields: requestFields,
    };
    const id = digestId('aqk', scope);
    const previous = this.database.prepare(
      'SELECT first_seen_at, seen_count FROM parameter_experiences WHERE knowledge_id = ?',
    ).get(id);
    this.database.prepare(`
      INSERT INTO parameter_experiences (
        knowledge_id, schema_version, provider, market_profile, credential_scope,
        service, region, operation, api_version, outcome, provider_code,
        error_category, request_fields_json, recovery_action, first_seen_at,
        last_seen_at, seen_count
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(knowledge_id) DO UPDATE SET
        last_seen_at=excluded.last_seen_at,
        seen_count=parameter_experiences.seen_count + 1,
        recovery_action=excluded.recovery_action
    `).run(
      id,
      KNOWLEDGE_SCHEMA_VERSION,
      scope.provider,
      scope.market_profile,
      scope.credential_scope,
      scope.service,
      scope.region,
      scope.operation,
      scope.api_version,
      scope.outcome,
      scope.provider_code,
      scope.error_category,
      JSON.stringify(scope.request_fields),
      failed ? String(result?.recovery?.next_action || 'inspect_official_error_contract') : null,
      previous?.first_seen_at || now,
      now,
      Number(previous?.seen_count || 0) + 1,
    );
  }

  knowledgeForQuery(query) {
    if (!query?.provider || !query?.service || !query?.region) return [];
    const profile = configuredMarketProfile(query.provider) || 'unknown';
    const rows = this.database.prepare(`
      SELECT * FROM parameter_experiences
      WHERE provider = ? AND market_profile = ? AND service = ? AND region = ?
      ORDER BY last_seen_at DESC LIMIT 12
    `).all(query.provider, profile, query.service, query.region);
    return rows
      .filter((row) => !query.action || !row.operation || row.operation === query.action)
      .map((row) => ({
        knowledge_id: row.knowledge_id,
        outcome: row.outcome,
        operation: row.operation,
        api_version: row.api_version || null,
        provider_code: row.provider_code || null,
        error_category: row.error_category || null,
        observed_request_fields: JSON.parse(row.request_fields_json),
        recovery_action: row.recovery_action || null,
        last_seen_at: row.last_seen_at,
        seen_count: Number(row.seen_count),
      }));
  }

  marketProfile(provider) {
    return configuredMarketProfile(provider) || 'unknown';
  }

  capabilityBlocker(query, { maximumAgeMs = 5 * 60 * 1_000, now = Date.now() } = {}) {
    if (!query?.provider || !query?.service || !query?.region || !operationOf(query)) return null;
    const profile = configuredMarketProfile(query.provider) || 'unknown';
    const providerRows = this.database.prepare(`
      SELECT outcome, provider_code, error_category, recovery_action, last_seen_at
      FROM parameter_experiences
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
      FROM parameter_experiences
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

  healthForRoute(id) {
    if (!id) return null;
    const route = this.get(id);
    const failureCount = Number(route.failure_count || 0);
    return {
      route_id: route.route_id,
      status: route.status,
      failure_count: failureCount,
      remaining_attempts: Math.max(0, 3 - failureCount),
      next_action: route.status === 'quarantined'
        ? 'discover_and_verify_official_read_only_route'
        : 'retry_cached_route_with_current_quote_parameters',
    };
  }

  recordBatch(queries, results) {
    const byQueryId = new Map((queries || []).map((query) => [query.query_id, query]));
    const now = new Date().toISOString();
    const learned = [];
    this._transaction(() => {
      for (const result of results || []) {
        const query = byQueryId.get(result?.query_id);
        if (query) this._recordExperience(query, result, now);
        if (result?.route_verification
          && Number(result.route_verification.route_contract_version) === 3
          && ['exact', 'ambiguous'].includes(result.status)
          && Array.isArray(result.official_item_ids)
          && result.official_item_ids.length > 0) {
          const verification = cleanRouteVerification(result.route_verification);
          const id = routeId(verification.route_fingerprint);
          const previous = this._routeFromRow(this.database.prepare(
            'SELECT route_id, payload_json FROM routes WHERE route_id = ?',
          ).get(id));
          const verifiedCount = Number(previous?.verified_count || 0) + 1;
          const positiveRate = (result.official_rate_candidates || []).some(
            (rate) => rate.is_zero_rate !== true && Number(rate.unit_price) > 0,
          );
          const baseConfidence = Math.max(
            Number(verification.confidence || 0), positiveRate ? 0.75 : 0.65,
          );
          const confidence = Math.min(
            0.98, baseConfidence + Math.min(0.2, (verifiedCount - 1) * 0.05),
          );
          const record = {
            ...(previous || {}),
            ...verification,
            schema_version: ROUTE_SCHEMA_VERSION,
            route_id: id,
            status: 'active',
            confidence,
            verified_count: verifiedCount,
            failure_count: 0,
            observed_request_fields: mergeRequestFields(
              previous?.observed_request_fields,
              query ? requestFieldShape(query) : [],
            ),
            first_verified_at: previous?.first_verified_at || verification.last_verified_at || now,
            last_verified_at: verification.last_verified_at || now,
            history: [
              ...(previous?.history || []),
              { at: now, event: previous ? 'reverified' : 'verified' },
            ].slice(-20),
          };
          this._putRoute(record);
          learned.push({
            route_id: id,
            provider: record.provider,
            service: record.service,
            region: record.region,
            confidence: record.confidence,
            retained_until_failure: true,
          });
          continue;
        }
        if (result?.status !== 'query_failed' || !result.route_fingerprint) continue;
        if (!ROUTE_FAILURE_CATEGORIES.has(result.error_category)) continue;
        const previous = this._routeFromRow(
          result.reused_route_id
            ? this.database.prepare(
              'SELECT route_id, payload_json FROM routes WHERE route_id = ?',
            ).get(result.reused_route_id)
            : this.database.prepare(
              'SELECT route_id, payload_json FROM routes WHERE route_fingerprint = ?',
            ).get(result.route_fingerprint),
        );
        if (!previous) continue;
        const failureCount = Number(previous.failure_count || 0) + 1;
        this._putRoute({
          ...previous,
          status: failureCount >= 3 ? 'quarantined' : previous.status,
          failure_count: failureCount,
          confidence: Math.max(0, Number(previous.confidence || 0) - 0.15),
          last_failure_at: now,
          last_failure_category: result.error_category,
          history: [
            ...(previous.history || []),
            { at: now, event: 'route_failure', category: result.error_category },
          ].slice(-20),
        });
      }
    });
    return learned;
  }

  instructions() {
    const row = this.database.prepare(
      "SELECT COUNT(*) AS count FROM routes WHERE status = 'active'",
    ).get();
    if (!Number(row?.count || 0)) return '';
    return [
      '## 已验证的动态查价道路',
      `服务器现有 ${Number(row.count)} 条有效官方只读道路，不把全部道路载入上下文。`,
      '调用 get_prices 时先提供 provider、service、region，并省略 endpoint；AstraQuote 会按当前账号站点和精确作用域检索缓存。唯一命中时自动复用；多条命中时返回候选 route_id 由 GPT 选择；没有命中时才查询官方资料并提交新道路。成功道路不按时间删除，只有道路级错误连续 3 次才隔离；参数值错误只记为参数经验，不伤害道路。缓存不保存价格、客户参数值或凭据。',
    ].join('\n\n');
  }
}


module.exports = { PricingRouteStore, PricingRouteStoreError };
