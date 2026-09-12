'use strict';

const fs = require('node:fs');
const path = require('node:path');


const CATALOG_CANDIDATES = [
  process.env.ASTRAQUOTE_MARKET_PROFILE_CATALOG_PATH,
  path.resolve(__dirname, '../policies/cloud-market-profiles.json'),
  path.resolve(__dirname, '../../policies/cloud-market-profiles.json'),
  path.resolve(__dirname, '../../../policies/cloud-market-profiles.json'),
].filter(Boolean);

let cachedCatalog;


function catalog() {
  if (cachedCatalog !== undefined) return cachedCatalog;
  for (const candidatePath of CATALOG_CANDIDATES) {
    try {
      const payload = JSON.parse(fs.readFileSync(candidatePath, 'utf8'));
      if (payload?.schema_version === 'astraquote-cloud-market-profiles/1') {
        cachedCatalog = payload;
        return payload;
      }
    } catch {
      // Try the next supported packaging layout.
    }
  }
  cachedCatalog = null;
  return null;
}


function configuredProfileId(provider, explicitProfile) {
  const providerConfig = catalog()?.providers?.[provider];
  return explicitProfile
    || process.env[`ASTRAQUOTE_${String(provider).toUpperCase()}_MARKET_PROFILE`]
    || providerConfig?.default_profile
    || null;
}


function profile(provider, explicitProfile) {
  const providerConfig = catalog()?.providers?.[provider];
  const profileId = configuredProfileId(provider, explicitProfile);
  const profileConfig = providerConfig?.profiles?.[profileId];
  return profileConfig ? { profile_id: profileId, ...profileConfig } : null;
}


function regionLabels(provider, explicitProfile) {
  const regions = profile(provider, explicitProfile)?.regions;
  if (!Array.isArray(regions)) return new Map();
  return new Map(regions.map(([code, label]) => [String(code), String(label)]));
}


function regionOwners(regionCode) {
  const code = String(regionCode || '').trim();
  const owners = [];
  for (const [provider, providerConfig] of Object.entries(catalog()?.providers || {})) {
    for (const [profileId, profileConfig] of Object.entries(providerConfig.profiles || {})) {
      if ((profileConfig.regions || []).some(([candidate]) => String(candidate) === code)) {
        owners.push({ provider, market_profile: profileId });
      }
    }
  }
  return owners;
}


function providerRegionMismatch(provider, regionCode, explicitProfile) {
  const code = String(regionCode || '').trim();
  if (!code || code === 'global' || regionLabels(provider, explicitProfile).has(code)) return null;
  const owners = regionOwners(code);
  if (owners.some((owner) => owner.provider === provider)) return null;
  return { provider, region: code, known_owners: owners };
}


function officialPricingPageUrlAllowed(provider, value, explicitProfile) {
  const allowedHosts = profile(provider, explicitProfile)?.official_price_page_hosts;
  if (!Array.isArray(allowedHosts) || allowedHosts.length === 0) return false;
  let parsed;
  try {
    parsed = new URL(String(value || ''));
  } catch {
    return false;
  }
  if (parsed.protocol !== 'https:' || parsed.username || parsed.password
    || !['', '443'].includes(parsed.port)) return false;
  const hostname = parsed.hostname.toLowerCase().replace(/\.$/, '');
  return allowedHosts.some((candidate) => {
    const officialHost = String(candidate || '').toLowerCase().replace(/\.$/, '');
    return officialHost && (hostname === officialHost || hostname.endsWith(`.${officialHost}`));
  });
}


module.exports = {
  catalog,
  configuredProfileId,
  officialPricingPageUrlAllowed,
  profile,
  providerRegionMismatch,
  regionLabels,
  regionOwners,
};
