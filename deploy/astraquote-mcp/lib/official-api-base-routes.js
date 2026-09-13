'use strict';

const fs = require('node:fs');
const path = require('node:path');


const CATALOG_CANDIDATES = [
  process.env.ASTRAQUOTE_OFFICIAL_API_BASE_ROUTES_PATH,
  path.resolve(__dirname, '../policies/official-api-base-routes.json'),
  path.resolve(__dirname, '../../policies/official-api-base-routes.json'),
  path.resolve(__dirname, '../../../policies/official-api-base-routes.json'),
].filter(Boolean);
const SAFE_REGION = /^[A-Za-z0-9][A-Za-z0-9.-]{0,79}$/;

let cachedCatalog;


function catalog() {
  if (cachedCatalog !== undefined) return cachedCatalog;
  for (const candidatePath of CATALOG_CANDIDATES) {
    try {
      const payload = JSON.parse(fs.readFileSync(candidatePath, 'utf8'));
      if (payload?.schema_version === 'astraquote-official-api-base-routes/2') {
        cachedCatalog = payload;
        return payload;
      }
    } catch {
      // Try the next supported source/deployment layout.
    }
  }
  cachedCatalog = null;
  return null;
}


function serviceKey(value) {
  return String(value || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '');
}


function officialApiBaseRoute(provider, service, region) {
  const providerKey = String(provider || '').trim().toLowerCase();
  const normalizedService = serviceKey(service);
  const regionValue = String(region || '').trim();
  if (!normalizedService || !SAFE_REGION.test(regionValue)) return null;
  const providerRoutes = catalog()?.providers?.[providerKey];
  if (!providerRoutes || typeof providerRoutes !== 'object') return null;
  const selected = (providerRoutes.routes || []).find((candidate) => (
    (candidate.services || []).some((alias) => serviceKey(alias) === normalizedService)
  )) || (
    providerRoutes.default_route && typeof providerRoutes.default_route === 'object'
      ? providerRoutes.default_route
      : null
  );
  if (!selected) return null;
  const capability = String(selected.capability || 'quote_api').trim();
  if (!['quote_api', 'official_page_only'].includes(capability)) return null;
  const endpoint = String(selected.endpoint || selected.endpoint_template || '')
    .trim().toLowerCase().replace('{region}', regionValue.toLowerCase());
  if (capability === 'quote_api' && !endpoint) return null;
  return {
    provider: providerKey,
    service: normalizedService,
    endpoint,
    capability,
    operations: (selected.operations || [])
      .map((operation) => String(operation).trim())
      .filter(Boolean),
    official_source_url: String(
      selected.official_source_url || providerRoutes.official_source_url || '',
    ).trim(),
    catalog_checked_at: String(catalog()?.catalog_checked_at || ''),
  };
}


function withOfficialApiBaseRoute(query) {
  const route = officialApiBaseRoute(query?.provider, query?.service, query?.region);
  if (!route) return { ...query };
  const materialized = { ...query };
  if (route.capability === 'quote_api') materialized.endpoint = route.endpoint;
  else delete materialized.endpoint;
  if (!materialized.official_source_url && route.official_source_url) {
    materialized.official_source_url = route.official_source_url;
  }
  if (route.capability === 'quote_api' && route.operations.length > 0) {
    if (!materialized.action && (!materialized.path || materialized.path === '/')
      && route.operations.length === 1) {
      const operation = route.operations[0];
      if (operation.startsWith('/')) materialized.path = operation;
      else materialized.action = operation;
    }
    const supplied = String(materialized.action || materialized.path || '/').toLowerCase()
      .replace(/\/$/u, '');
    const allowed = route.operations.some((operation) => {
      const expected = operation.toLowerCase().replace(/\/$/u, '');
      return supplied === expected
        || (!materialized.action && !operation.startsWith('/') && supplied.endsWith(expected));
    });
    if (!allowed) {
      const error = new Error('The request must use a registered official pricing operation.');
      error.code = 'official_api_operation_not_registered';
      error.retryable = true;
      error.details = {
        provider: route.provider,
        service: route.service,
        allowed_operations: route.operations,
      };
      throw error;
    }
  }
  return {
    ...materialized,
  };
}


module.exports = { officialApiBaseRoute, withOfficialApiBaseRoute };
