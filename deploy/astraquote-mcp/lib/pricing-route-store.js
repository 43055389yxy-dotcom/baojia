'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');


class PricingRouteStoreError extends Error {
  constructor(message, { code = 'pricing_route_store_error', details = {} } = {}) {
    super(message);
    this.name = 'PricingRouteStoreError';
    this.code = code;
    this.details = details;
  }
}


const ROUTE_FIELDS = Object.freeze([
  'provider', 'endpoint', 'service', 'action', 'version', 'region',
  'region_parameter', 'method', 'path', 'response_items_path', 'item_id_paths',
  'rate_fields', 'next_page_path', 'official_source_url', 'sdk_version',
]);


function routeId(fingerprint) {
  return `aqr_${crypto.createHash('sha256').update(String(fingerprint)).digest('hex').slice(0, 24)}`;
}


function cleanRouteVerification(verification) {
  const clean = {};
  for (const field of ROUTE_FIELDS) {
    if (verification[field] !== undefined && verification[field] !== null) {
      clean[field] = verification[field];
    }
  }
  for (const field of [
    'route_fingerprint', 'auth_scheme', 'request_schema_hash', 'response_schema_hash',
    'last_verified_at', 'revalidate_after', 'expires_at',
  ]) {
    if (verification[field] !== undefined && verification[field] !== null) {
      clean[field] = verification[field];
    }
  }
  return clean;
}


class PricingRouteStore {
  constructor({ directory = path.join(
    process.env.ASTRAQUOTE_V2_STATE_DIR || '/data/v2-quotes',
    'pricing-routes',
  ) } = {}) {
    this.directory = directory;
    this.target = path.join(directory, 'pricing-routes.json');
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  }

  load() {
    if (!fs.existsSync(this.target)) {
      return { schema_version: 'astraquote-pricing-routes/1', routes: [] };
    }
    try {
      const payload = JSON.parse(fs.readFileSync(this.target, 'utf8'));
      return Array.isArray(payload.routes)
        ? payload
        : { schema_version: 'astraquote-pricing-routes/1', routes: [] };
    } catch (error) {
      throw new PricingRouteStoreError('Verified pricing routes could not be read.', {
        code: 'pricing_route_store_corrupt',
        details: { exception_type: error?.name || 'Error' },
      });
    }
  }

  save(payload) {
    const temporary = `${this.target}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`;
    fs.writeFileSync(temporary, `${JSON.stringify(payload)}\n`, { encoding: 'utf8', mode: 0o600 });
    fs.renameSync(temporary, this.target);
  }

  get(id) {
    if (!/^aqr_[a-f0-9]{24}$/.test(String(id || ''))) {
      throw new PricingRouteStoreError('Invalid pricing route ID.', {
        code: 'pricing_route_id_invalid', details: { route_id: id || null },
      });
    }
    const route = this.load().routes.find((item) => item.route_id === id);
    if (!route) {
      throw new PricingRouteStoreError('Verified pricing route was not found.', {
        code: 'pricing_route_not_found', details: { route_id: id },
      });
    }
    return route;
  }

  materialize(query) {
    if (!query.route_id) return { ...query };
    const route = this.get(query.route_id);
    const revalidationDue = Date.parse(route.revalidate_after) <= Date.now();
    const expired = Date.parse(route.expires_at) <= Date.now();
    if (route.status !== 'active' || revalidationDue || expired) {
      throw new PricingRouteStoreError('Verified pricing route must be revalidated.', {
        code: 'pricing_route_revalidation_required',
        details: {
          route_id: route.route_id,
          status: route.status,
          revalidation_due: revalidationDue,
          expired,
        },
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
      });
    }
    for (const field of ROUTE_FIELDS) {
      if (['provider', 'region'].includes(field)) continue;
      if (query[field] !== undefined
        && JSON.stringify(query[field]) !== JSON.stringify(route[field])) {
        throw new PricingRouteStoreError('Caller transport fields conflict with the verified route.', {
          code: 'pricing_route_identity_conflict',
          details: { route_id: route.route_id, field },
        });
      }
    }
    const { route_id: ignoredRouteId, ...dynamic } = query;
    const materialized = {};
    for (const field of ROUTE_FIELDS) {
      if (route[field] !== undefined) materialized[field] = route[field];
    }
    return {
      ...materialized,
      ...dynamic,
      provider: route.provider,
      region: route.region,
      endpoint: route.endpoint,
      service: route.service,
      action: route.action || '',
      method: route.method || 'POST',
      path: route.path || '/',
    };
  }

  recordBatch(_queries, results) {
    const payload = this.load();
    const now = new Date().toISOString();
    const learned = [];
    for (const result of results || []) {
      if (result?.route_verification
        && ['exact', 'ambiguous'].includes(result.status)
        && Array.isArray(result.official_item_ids)
        && result.official_item_ids.length > 0) {
        const verification = cleanRouteVerification(result.route_verification);
        const id = routeId(verification.route_fingerprint);
        const index = payload.routes.findIndex((item) => item.route_id === id);
        const previous = index >= 0 ? payload.routes[index] : null;
        const verifiedCount = Number(previous?.verified_count || 0) + 1;
        const positiveRate = (result.official_rate_candidates || []).some(
          (rate) => rate.is_zero_rate !== true && Number(rate.unit_price) > 0,
        );
        const baseConfidence = Math.max(Number(verification.confidence || 0), positiveRate ? 0.75 : 0.65);
        const confidence = Math.min(0.98, baseConfidence + Math.min(0.2, (verifiedCount - 1) * 0.05));
        const record = {
          ...(previous || {}),
          ...verification,
          schema_version: 'astraquote-pricing-route/1',
          route_id: id,
          status: 'active',
          confidence,
          verified_count: verifiedCount,
          failure_count: 0,
          first_verified_at: previous?.first_verified_at || verification.last_verified_at || now,
          last_verified_at: verification.last_verified_at || now,
          history: [
            ...(previous?.history || []),
            { at: now, event: previous ? 'reverified' : 'verified' },
          ].slice(-20),
        };
        if (index >= 0) payload.routes[index] = record;
        else payload.routes.push(record);
        learned.push({
          route_id: id,
          provider: record.provider,
          service: record.service,
          region: record.region,
          confidence: record.confidence,
          revalidate_after: record.revalidate_after,
          expires_at: record.expires_at,
        });
        continue;
      }
      if (result?.status !== 'query_failed' || !result.route_fingerprint) continue;
      if (!['invalid_request', 'route_not_found', 'response_schema'].includes(
        result.error_category,
      )) continue;
      const index = payload.routes.findIndex(
        (item) => item.route_fingerprint === result.route_fingerprint,
      );
      if (index < 0) continue;
      const previous = payload.routes[index];
      const failureCount = Number(previous.failure_count || 0) + 1;
      payload.routes[index] = {
        ...previous,
        status: failureCount >= 3 ? 'quarantined' : previous.status,
        failure_count: failureCount,
        confidence: Math.max(0, Number(previous.confidence || 0) - 0.15),
        last_failure_at: now,
        last_failure_category: result.error_category || 'official_api_error',
        history: [
          ...(previous.history || []),
          { at: now, event: 'failure', category: result.error_category || 'official_api_error' },
        ].slice(-20),
      };
    }
    this.save({
      schema_version: 'astraquote-pricing-routes/1',
      updated_at: now,
      routes: payload.routes,
    });
    return learned;
  }

  instructions() {
    const routes = this.load().routes
      .filter((route) => route.status === 'active' && Date.parse(route.expires_at) > Date.now())
      .sort((left, right) => Number(right.confidence) - Number(left.confidence))
      .slice(0, 40)
      .map((route) => ({
        route_id: route.route_id,
        provider: route.provider,
        service: route.service,
        region: route.region,
        operation: route.action || route.path,
        confidence: route.confidence,
        revalidate_after: route.revalidate_after,
        official_source_url: route.official_source_url,
      }));
    if (routes.length === 0) return '';
    return [
      '## 已验证的动态查价道路',
      '以下道路来自历史成功的官方只读调用。相同云、服务和区域可在 get_prices 中只传 route_id 与本次业务参数复用；不得跨区域或跨云复用。到期或隔离道路必须重新查官方资料验证。',
      JSON.stringify(routes),
    ].join('\n\n');
  }
}


module.exports = { PricingRouteStore, PricingRouteStoreError };
