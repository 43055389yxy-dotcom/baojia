'use strict';

const fs = require('node:fs');
const path = require('node:path');

const POLICY_CANDIDATES = [
  process.env.ASTRAQUOTE_QUOTE_WORKFLOW_POLICY_PATH,
  path.resolve(__dirname, '../policies/quote-workflow-policy.json'),
  path.resolve(__dirname, '../../policies/quote-workflow-policy.json'),
  path.resolve(__dirname, '../../../policies/quote-workflow-policy.json'),
].filter(Boolean);

let cachedPolicy;

function validatePolicy(payload) {
  const positiveIntegerPaths = [
    ['batching', 'components_per_wave'],
    ['batching', 'waves_per_chat'],
    ['batching', 'max_active_chats_per_sales_job'],
    ['batching', 'global_active_chat_limit'],
    ['batching', 'max_numbered_components'],
    ['batching', 'max_deferred_components_per_retry'],
    ['timing', 'default_quote_seconds'],
    ['timing', 'max_continuations_without_progress'],
    ['pricing', 'official_api_network_attempt_limit_per_scope'],
    ['pricing', 'corrected_api_attempt_limit_per_scope'],
    ['pricing', 'official_page_min_api_attempts'],
    ['recovery', 'deferred_retry_rounds'],
  ];
  for (const [section, key] of positiveIntegerPaths) {
    if (!Number.isInteger(payload[section][key]) || payload[section][key] < 1) {
      throw new Error(`AstraQuote workflow policy integer is invalid: ${section}.${key}`);
    }
  }
  if (payload.pricing.official_api_network_attempt_limit_per_scope
      !== 1 + payload.pricing.corrected_api_attempt_limit_per_scope) {
    throw new Error('AstraQuote workflow policy API attempt budget is inconsistent.');
  }
  if (payload.batching.max_deferred_components_per_retry
      > payload.batching.components_per_wave) {
    throw new Error('AstraQuote workflow policy deferred batch exceeds one wave.');
  }
  if (payload.recovery.deferred_retry_rounds !== 1) {
    throw new Error('AstraQuote worker supports exactly one deferred retry round.');
  }
  if (payload.recovery.stalled_component_strategy !== 'defer_then_retry_once') {
    throw new Error('AstraQuote workflow policy recovery strategy is unsupported.');
  }
  if (payload.completion.authority !== 'sealed_component_fragment') {
    throw new Error('AstraQuote workflow policy completion authority is unsupported.');
  }
  if (payload.delivery.owner !== 'program') {
    throw new Error('AstraQuote workflow policy delivery owner is unsupported.');
  }
  const booleanPaths = [
    ['pricing', 'official_page_evidence_completes_scope'],
    ['pricing', 'reuse_successful_evidence_within_quote'],
    ['pricing', 'reuse_historical_prices_across_quotes'],
    ['pricing', 'use_on_demand_fallback_for_missing_long_term_price'],
    ['recovery', 'retry_in_original_conversation'],
    ['recovery', 'continue_other_components'],
    ['completion', 'ai_text_is_authoritative'],
    ['completion', 'official_page_evidence_is_authoritative'],
    ['delivery', 'allow_partial_sales_quote'],
    ['delivery', 'allow_sales_manual_price'],
    ['delivery', 'excel_after_all_component_batches'],
  ];
  for (const [section, key] of booleanPaths) {
    if (typeof payload[section][key] !== 'boolean') {
      throw new Error(`AstraQuote workflow policy boolean is invalid: ${section}.${key}`);
    }
  }
  for (const [sliceName, directiveKeys] of Object.entries(payload.consumer_slices)) {
    if (!Array.isArray(directiveKeys) || directiveKeys.length === 0
        || directiveKeys.some((key) => (
          typeof key !== 'string'
          || typeof payload.prompt_directives[key] !== 'string'
          || !payload.prompt_directives[key].trim()
        ))) {
      throw new Error(`AstraQuote workflow policy slice is invalid: ${sliceName}`);
    }
  }
}

function quoteWorkflowPolicy() {
  if (cachedPolicy !== undefined) return cachedPolicy;
  for (const candidatePath of POLICY_CANDIDATES) {
    if (!fs.existsSync(candidatePath)) continue;
    let payload;
    try {
      payload = JSON.parse(fs.readFileSync(candidatePath, 'utf8'));
    } catch (error) {
      throw new Error(`AstraQuote workflow policy is unreadable: ${candidatePath}`, {
        cause: error,
      });
    }
    if (payload?.schema_version !== 'astraquote-quote-workflow-policy/1') {
      throw new Error(`AstraQuote workflow policy schema is invalid: ${candidatePath}`);
    }
    if (!String(payload.policy_version || '').trim()) {
      throw new Error(`AstraQuote workflow policy version is missing: ${candidatePath}`);
    }
    const required = [
      'batching', 'timing', 'pricing', 'recovery', 'completion', 'delivery',
      'prompt_directives', 'consumer_slices',
    ];
    if (required.some((key) => !payload[key] || typeof payload[key] !== 'object')) {
      throw new Error(`AstraQuote workflow policy section is invalid: ${candidatePath}`);
    }
    validatePolicy(payload);
    cachedPolicy = payload;
    return cachedPolicy;
  }
  throw new Error('AstraQuote workflow policy could not be loaded.');
}

function workflowPolicyValue(...keys) {
  let value = quoteWorkflowPolicy();
  for (const key of keys) {
    if (!value || typeof value !== 'object' || !(key in value)) {
      throw new Error(`AstraQuote workflow policy value is missing: ${keys.join('.')}`);
    }
    value = value[key];
  }
  return value;
}

function workflowPolicyVersion() {
  return String(quoteWorkflowPolicy().policy_version);
}

function renderWorkflowPolicySlice(sliceName) {
  const policy = quoteWorkflowPolicy();
  const keys = policy.consumer_slices[sliceName];
  if (!Array.isArray(keys) || keys.length === 0) {
    throw new Error(`AstraQuote workflow policy slice is invalid: ${sliceName}`);
  }
  return keys.map((key) => {
    const text = policy.prompt_directives[key];
    if (typeof text !== 'string' || !text.trim()) {
      throw new Error(`AstraQuote workflow directive is invalid: ${key}`);
    }
    return text.trim();
  }).join('\n');
}

function workflowPolicySnapshot() {
  const policy = quoteWorkflowPolicy();
  return {
    schema_version: policy.schema_version,
    policy_version: policy.policy_version,
    batching: { ...policy.batching },
    timing: { ...policy.timing },
    pricing: { ...policy.pricing },
    recovery: { ...policy.recovery },
    completion: { ...policy.completion },
    delivery: { ...policy.delivery },
  };
}

module.exports = {
  quoteWorkflowPolicy,
  renderWorkflowPolicySlice,
  workflowPolicySnapshot,
  workflowPolicyValue,
  workflowPolicyVersion,
};
