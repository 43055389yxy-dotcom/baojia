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
      if (payload?.schema_version === 'astraquote-official-api-base-routes/1') {
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
  )) || (providerRoutes.default_endpoint ? providerRoutes : null);
  if (!selected) return null;
  const endpoint = String(
    selected.endpoint || selected.default_endpoint || selected.endpoint_template || '',
  )
    .trim().toLowerCase().replace('{region}', regionValue.toLowerCase());
  if (!endpoint) return null;
  return {
    provider: providerKey,
    service: normalizedService,
    endpoint,
    official_source_url: String(
      selected.official_source_url || providerRoutes.official_source_url || '',
    ).trim(),
    catalog_checked_at: String(catalog()?.catalog_checked_at || ''),
  };
}


function withOfficialApiBaseRoute(query) {
  const route = officialApiBaseRoute(query?.provider, query?.service, query?.region);
  if (!route) return { ...query };
  return {
    ...query,
    endpoint: route.endpoint,
    ...(!query.official_source_url && route.official_source_url
      ? { official_source_url: route.official_source_url }
      : {}),
  };
}


module.exports = { officialApiBaseRoute, withOfficialApiBaseRoute };
