'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { isDeepStrictEqual } = require('node:util');

const { V2QuoteStore } = require('./v2-quote-store');
const { QuoteDeliveryService } = require('./quote-delivery');
const { OfficialPriceCache } = require('./official-price-cache');
const {
  officialPricingPageUrlAllowed, pricingScenarios, providerRegionMismatch,
} = require('./cloud-market-profiles');
const { PricingRouteStore, PricingRouteStoreError } = require('./pricing-route-store');
const { canFinalizeRelayJob } = require('./relay-job-state');
const {
  PROGRESS_GUIDANCE, mergeQueryContexts, queryProgress, componentProgress,
  assertQueryIdentity, contextEvidenceViolations, pricingScopeKey, reusableQueryResult,
} = require('./query-lifecycle');

function uniqueComponentKeys(entries) {
  const seen = new Set();
  const duplicates = [];
  for (const entry of entries || []) {
    if (seen.has(entry.component_key)) duplicates.push(entry.component_key);
    seen.add(entry.component_key);
  }
  if (duplicates.length > 0) {
    const error = new Error('Component keys must be unique.');
    error.code = 'duplicate_component_key';
    error.details = { component_keys: [...new Set(duplicates)] };
    throw error;
  }
}

function sealedComponentPlan(existing, supplied) {
  const previous = Array.isArray(existing) ? existing : [];
  const incoming = Array.isArray(supplied) ? supplied : [];
  if (previous.length > 0) {
    if (incoming.length > 0 && !isDeepStrictEqual(previous, incoming)) {
      const error = new Error('The cleaned component plan is sealed and cannot be changed.');
      error.code = 'quote_component_plan_immutable';
      throw error;
    }
    return previous;
  }
  if (incoming.length === 0) return [];
  uniqueComponentKeys(incoming);
  const keys = new Set(incoming.map((item) => item.component_key));
  const byKey = new Map(incoming.map((item) => [item.component_key, item]));
  const violations = [];
  for (const component of incoming) {
    if (component.parent_component_key
      && (!keys.has(component.parent_component_key)
        || component.parent_component_key === component.component_key)) {
      violations.push(`invalid_parent:${component.component_key}`);
    }
    const ancestors = new Set([component.component_key]);
    let parent = component.parent_component_key;
    while (parent && keys.has(parent)) {
      if (ancestors.has(parent)) {
        violations.push(`parent_cycle:${component.component_key}`);
        break;
      }
      ancestors.add(parent);
      parent = byKey.get(parent).parent_component_key;
    }
    const scopes = (component.billing_scopes || []).map((billing) => JSON.stringify([
      billing.billing_key,
      billing.scenario_key || null,
    ]));
    if (new Set(scopes).size !== scopes.length) {
      violations.push(`duplicate_billing_scope:${component.component_key}`);
    }
  }
  if (violations.length > 0) {
    const error = new Error('The cleaned component plan is invalid.');
    error.code = 'quote_component_plan_invalid';
    error.details = { violations };
    throw error;
  }
  return incoming;
}

function assertFormalQuotePricingPlan({ quoteMode, relayJobId, quoteComponents, queries, queryContexts }) {
  if (relayJobId && quoteMode === 'price_lookup') {
    const error = new Error('A sales relay task is always a formal quote.');
    error.code = 'relay_quote_mode_invalid';
    error.retryable = true;
    error.details = { next_action: 'retry_get_prices_with_quote_mode_formal_quote' };
    throw error;
  }
  const formalQuote = quoteMode === 'formal_quote'
    || Boolean(relayJobId) || quoteComponents.length > 0;
  if (!formalQuote) return;
  if (quoteComponents.length === 0) {
    const error = new Error('Register the complete cleaned component plan before querying a sales quote.');
    error.code = 'formal_quote_component_plan_required';
    error.retryable = true;
    error.details = {
      next_action: 'retry_get_prices_with_complete_quote_components_and_query_contexts',
    };
    throw error;
  }

  const contextById = new Map(queryContexts.map((item) => [item.query_id, item]));
  const missingContextIds = queries
    .map((query) => query.query_id)
    .filter((queryId) => !contextById.has(queryId));
  if (missingContextIds.length > 0) {
    const error = new Error('Every formal quote query must declare whether it is discovery or owned pricing work.');
    error.code = 'formal_quote_query_context_required';
    error.retryable = true;
    error.details = {
      query_ids: missingContextIds,
      next_action: 'retry_get_prices_with_query_contexts_for_every_query',
    };
    throw error;
  }

  const plannedScopes = new Set(quoteComponents.flatMap((component) => (
    (component.billing_scopes || []).map((billing) => pricingScopeKey(
      component.component_key, billing.billing_key, billing.scenario_key || null,
    ))
  )));
  const unknownScopes = queries.flatMap((query) => {
    const context = contextById.get(query.query_id);
    if (context?.purpose !== 'pricing') return [];
    const contextScope = pricingScopeKey(
      context.component_key, context.billing_key, context.scenario_key || null,
    );
    return plannedScopes.has(contextScope) ? [] : [{
      query_id: query.query_id,
      component_key: context.component_key,
      billing_key: context.billing_key,
      scenario_key: context.scenario_key || null,
    }];
  });
  if (unknownScopes.length > 0) {
    const error = new Error('Formal quote pricing queries must belong to a declared component billing scope.');
    error.code = 'formal_quote_query_scope_unknown';
    error.retryable = true;
    error.details = {
      query_scopes: unknownScopes,
      next_action: 'correct_query_context_or_quote_component_plan',
    };
    throw error;
  }
}

function priceWorkflowGuard({ quoteMode, relayJobId, quoteComponents, componentLifecycle = [] }) {
  const formalQuote = quoteMode === 'formal_quote'
    || Boolean(relayJobId) || quoteComponents.length > 0;
  return {
    mode: formalQuote ? 'formal_quote' : 'price_lookup',
    quote_plan_registered: quoteComponents.length > 0,
    quote_coverage_known: quoteComponents.length > 0,
    formal_quote_final_response_allowed: false,
    ...(formalQuote ? {
      required_delivery_tool: 'build_estimate',
      instruction: 'Do not end with a prose-only failure. Continue to official page fallback or deliver a verified partial/complete quote with build_estimate.',
      completed_component_keys: componentLifecycle
        .filter((item) => item.state === 'completed').map((item) => item.component_key),
      failed_component_keys: componentLifecycle
        .filter((item) => item.state === 'failed').map((item) => item.component_key),
      pending_component_keys: componentLifecycle
        .filter((item) => item.state === 'pending').map((item) => item.component_key),
    } : {
      price_lookup_answer_allowed: true,
      formal_quote_required_action: 'Register the complete quote_components plan and query_contexts before treating this batch as a formal quote.',
      instruction: 'This batch is only a price lookup. If the user requested a formal quote or Excel, do not give a final quote answer because component coverage is unknown.',
    }),
    official_page_fallback: {
      supported: true,
      api_attempts_required: 3,
      same_component_billing_scenario_scope_required: true,
      disallowed_for: ['credentials', 'authorization'],
      build_estimate_field: 'official_page_price_evidence',
    },
  };
}

function validateCustomerDocumentMetadata(input) {
  const componentKeys = new Set([
    ...(input.services || []).map((entry) => entry.component_key),
    ...(input.zero_cost_services || []).map((entry) => entry.component_key),
    ...(input.unpriced_services || []).map((entry) => entry.component_key),
  ]);
  const violations = [];
  for (const adjustment of input.adjustments || []) {
    if (!componentKeys.has(adjustment.component_key)) {
      violations.push(`adjustment_unknown_component:${adjustment.component_key}`);
    }
  }
  if (violations.length > 0) {
    const error = new Error('Customer-facing quote notes reference an unknown component.');
    error.code = 'customer_document_metadata_invalid';
    error.details = { violations };
    throw error;
  }
}

function normalizeComponentCosts(input) {
  const expectedTotal = Number(input.expected_monthly_total);
  const services = (input.services || []).map((service) => ({ ...service }));
  const missing = services.filter((service) => (
    service.expected_monthly_cost === undefined || service.expected_monthly_cost === null
  ));

  // Compatibility is limited to the old one-component cost alias. The program
  // never invents a cost distribution for a multi-component quote.
  if (missing.length === 1 && services.length === 1) {
    services[0].expected_monthly_cost = String(input.expected_monthly_total);
  } else if (missing.length > 0) {
    const error = new Error('Every quote component must include its calculated monthly cost.');
    error.code = 'component_monthly_cost_required';
    error.details = { component_keys: missing.map((service) => service.component_key) };
    throw error;
  }

  const invalid = services.filter((service) => {
    const amount = Number(service.expected_monthly_cost);
    return !Number.isFinite(amount) || amount < 0;
  });
  if (!Number.isFinite(expectedTotal) || expectedTotal < 0 || invalid.length > 0) {
    const error = new Error('Component or total monthly cost is invalid.');
    error.code = 'component_monthly_cost_invalid';
    error.details = { component_keys: invalid.map((service) => service.component_key) };
    throw error;
  }

  const componentTotal = services.reduce(
    (sum, service) => sum + Number(service.expected_monthly_cost),
    0,
  );
  if (Math.round(componentTotal * 100) !== Math.round(expectedTotal * 100)) {
    const error = new Error('Component monthly costs do not add up to the quote total.');
    error.code = 'component_monthly_cost_mismatch';
    error.details = {
      component_total: componentTotal.toFixed(2),
      expected_monthly_total: expectedTotal.toFixed(2),
    };
    throw error;
  }

  return { ...input, services };
}

const SCENARIO_KEYS = Object.freeze([
  'on_demand',
  'one_month_subscription',
  'one_year_subscription',
  'one_year_commitment',
  'three_year_commitment',
]);

const CREATED_PRICE_NEXT_ACTION = [
  'Execute now: derive a non-empty queries array for the smallest useful current group',
  'from the normalized component configuration and current get_prices schema, then call get_prices immediately.',
  'For a long quote, choose groups dynamically from query breadth and expected response size,',
  'and merge every group into the same returned price_batch_id.',
  'Do not describe or list the remaining steps and do not wait for another turn.',
  'Correct any caller-input validation error and retry in this response.',
].join(' ');

const DEFAULT_RESULT_BYTE_BUDGET = 128 * 1024;

function routeResolutionErrorCategory(code) {
  if (['pricing_route_identity_conflict', 'pricing_route_scope_mismatch',
    'pricing_route_id_invalid'].includes(code)) return 'invalid_request';
  if (code === 'pricing_route_revalidation_required') return 'response_schema';
  return 'route_not_found';
}

function resultByteBudget(value) {
  const configured = value ?? process.env.ASTRAQUOTE_MCP_RESULT_MAX_BYTES;
  if (configured === undefined || configured === null || configured === '') {
    return DEFAULT_RESULT_BYTE_BUDGET;
  }
  const parsed = Number(configured);
  if (!Number.isSafeInteger(parsed) || parsed < 1_024) {
    throw new Error('ASTRAQUOTE_MCP_RESULT_MAX_BYTES must be an integer of at least 1024.');
  }
  return parsed;
}

function jsonBytes(value) {
  return Buffer.byteLength(JSON.stringify(value), 'utf8');
}

function compactRecovery(recovery) {
  if (!recovery || typeof recovery !== 'object') return undefined;
  const compact = {};
  for (const key of ['next_action', 'field', 'parameter', 'reason']) {
    if (recovery[key] !== undefined) compact[key] = recovery[key];
  }
  if (Array.isArray(recovery.candidate_values)) {
    compact.candidate_values = recovery.candidate_values.slice(0, 10);
    compact.candidate_value_count = recovery.candidate_values.length;
  }
  return Object.keys(compact).length > 0 ? compact : undefined;
}

function compactRefinementFields(fields) {
  if (!Array.isArray(fields)) return undefined;
  return fields.slice(0, 12).map((field) => {
    if (!field || typeof field !== 'object') return field;
    const compact = {};
    for (const key of ['field', 'name', 'path', 'reason', 'description']) {
      if (field[key] !== undefined) compact[key] = field[key];
    }
    if (Array.isArray(field.candidate_values)) {
      compact.candidate_values = field.candidate_values.slice(0, 10);
      compact.candidate_value_count = field.candidate_values.length;
    }
    return compact;
  });
}

function compactPriceResult(item) {
  const compact = {
    query_id: item?.query_id,
    provider: item?.provider,
    status: item?.status,
    details_available: true,
  };
  for (const key of [
    'terminal', 'retryable', 'error_category', 'code', 'reused_route_id', 'matched_count',
    'not_found_reason', 'raw_item_count', 'filtered_item_count', 'cache_status',
    'official_price_observed_at', 'cache_age_seconds', 'source_request_skipped',
  ]) {
    if (item?.[key] !== undefined) compact[key] = item[key];
  }
  if (item?.details?.provider_code !== undefined) {
    compact.provider_code = item.details.provider_code;
  }
  const officialItemIds = Array.isArray(item?.official_item_ids) ? item.official_item_ids : [];
  if (officialItemIds.length > 0) {
    compact.official_item_ids = officialItemIds.slice(0, 10);
    compact.official_item_id_count = officialItemIds.length;
  }
  if (Array.isArray(item?.official_rate_candidates)) {
    compact.official_rate_candidate_count = item.official_rate_candidates.length;
  }
  if (Array.isArray(item?.items)) compact.official_item_count = item.items.length;
  const refinementFields = compactRefinementFields(item?.refinement_fields);
  if (refinementFields) compact.refinement_fields = refinementFields;
  const recovery = compactRecovery(item?.recovery);
  if (recovery) compact.recovery = recovery;
  if (Array.isArray(item?.pricing_knowledge)) {
    compact.pricing_knowledge = item.pricing_knowledge.slice(0, 3).map((knowledge) => ({
      error_category: knowledge?.error_category,
      provider_code: knowledge?.provider_code,
      next_action: knowledge?.next_action,
    }));
  }
  if (item?.pricing_route_health) {
    compact.pricing_route_health = {
      route_id: item.pricing_route_health.route_id,
      status: item.pricing_route_health.status,
      failure_count: item.pricing_route_health.failure_count,
      remaining_attempts: item.pricing_route_health.remaining_attempts,
    };
  }
  return compact;
}

function compactLearnedRoute(route) {
  if (!route || typeof route !== 'object') return route;
  const compact = {};
  for (const key of ['route_id', 'query_id', 'provider', 'service', 'status']) {
    if (route[key] !== undefined) compact[key] = route[key];
  }
  return compact;
}

function compactIncrementalPriceResponse(payload, byteBudget) {
  const before = jsonBytes(payload);
  if (before <= byteBudget) {
    return {
      ...payload,
      response_compacted: false,
      response_bytes: before,
      response_byte_budget: byteBudget,
    };
  }
  const detailQueryIds = (payload.results || [])
    .map((item) => item?.query_id)
    .filter(Boolean);
  const compacted = {
    ...payload,
    results: (payload.results || []).map(compactPriceResult),
    learned_routes: (payload.learned_routes || []).map(compactLearnedRoute),
    response_compacted: true,
    response_bytes_before_compaction: before,
    response_byte_budget: byteBudget,
    detail_query_ids: detailQueryIds,
    result_access: {
      tool: 'get_price_results',
      max_query_ids_per_call: 10,
      instruction: 'Read only the query_ids whose full official evidence is needed.',
    },
  };
  compacted.response_bytes = jsonBytes(compacted);
  return compacted;
}

const FREE_ALLOWANCE_REFERENCE = /(?:free\s*tier|always\s*free|free\s*trial|trial\s*credit|promotional\s*credit|account\s*credit|免费额度|免费试用|赠送额度|账户(?:信用|赠送)|零价区间)/i;

function relayScenarioKeys(options) {
  if (Array.isArray(options?.pricing_scenarios)) {
    return [...new Set(options.pricing_scenarios)];
  }
  // Read-only compatibility boundary for jobs submitted before schema v3.1.
  const keys = [];
  if (options?.include_on_demand_scenario !== false) keys.push('on_demand');
  if (options?.pricing_mode === 'reserved' && options?.payment_option === 'all_upfront') {
    for (const years of options.reserved_term_years || []) {
      if (Number(years) === 1) keys.push('one_year_commitment');
      if (Number(years) === 3) keys.push('three_year_commitment');
    }
  }
  return [...new Set(keys)];
}

function readRelayJob(relayJobId) {
  const relayDirectory = path.resolve(process.env.ASTRAQUOTE_GPT_RELAY_DIR || '/data/gpt-relay');
  const jobPath = path.join(relayDirectory, 'jobs', `${relayJobId}.json`);
  try {
    const job = JSON.parse(fs.readFileSync(jobPath, 'utf8'));
    if (job.job_id !== relayJobId) throw new Error('relay identity mismatch');
    return job;
  } catch {
    const error = new Error('The sales quote task could not be verified.');
    error.code = 'relay_job_context_missing';
    error.details = { relay_job_id: relayJobId };
    throw error;
  }
}

function assertRelayIdentity({ relay_job_id: relayJobId, submission_code: submissionCode }) {
  const job = readRelayJob(String(relayJobId || ''));
  if (String(job.submission_code || '') !== String(submissionCode || '')) {
    const error = new Error('The sales quote submission code does not match.');
    error.code = 'relay_submission_code_invalid';
    error.details = { relay_job_id: relayJobId };
    throw error;
  }
  return job;
}

function bindRelayJobContext(input) {
  const relayJobId = String(input.relay_job_id || '');
  if (!relayJobId) return input;
  const job = readRelayJob(relayJobId);
  if (!canFinalizeRelayJob(job)) {
    const error = new Error('The sales quote task is no longer active.');
    error.code = 'relay_job_not_processing';
    error.details = { relay_job_id: relayJobId, status: job.status || null };
    throw error;
  }
  const submissionCode = String(job.submission_code || '');
  if (!/^[1-9]$/.test(submissionCode)) {
    const error = new Error('The sales quote task has no valid submission code.');
    error.code = 'relay_submission_code_invalid';
    error.details = { relay_job_id: relayJobId };
    throw error;
  }

  const expected = relayScenarioKeys(job.quote_options || {}).sort();
  const received = (input.pricing_scenarios || []).map((item) => item.scenario_key).sort();
  if (expected.length === 0
    || expected.length !== received.length
    || expected.some((key, index) => key !== received[index])) {
    const error = new Error('The quote scenarios do not match the sales submission.');
    error.code = 'relay_pricing_scenarios_mismatch';
    error.details = { relay_job_id: relayJobId, expected, received };
    throw error;
  }
  const expectedProvider = String(job.quote_options?.cloud_provider || 'aws');
  if (input.cloud_provider !== expectedProvider) {
    const error = new Error('The cloud provider does not match the sales submission.');
    error.code = 'relay_cloud_provider_mismatch';
    error.details = {
      relay_job_id: relayJobId,
      expected: expectedProvider,
      received: input.cloud_provider,
    };
    throw error;
  }
  const preferredRegion = String(job.quote_options?.preferred_region || '');
  if (preferredRegion && input.default_region !== preferredRegion
    && !String(input.region_adjustment_reason || '').trim()) {
    const error = new Error('A changed quote region requires a customer-facing reason.');
    error.code = 'relay_region_adjustment_required';
    error.details = {
      relay_job_id: relayJobId,
      preferred_region: preferredRegion,
      actual_region: input.default_region,
    };
    throw error;
  }
  return {
    ...input,
    submission_code: submissionCode,
    preferred_region: preferredRegion || input.default_region,
    display_result_on_page: true,
  };
}

function roundedCents(value) {
  return Math.round(Number(value) * 100);
}

function normalizeScenarioCosts(input) {
  const hasComponentScenarios = (input.services || []).some(
    (service) => Array.isArray(service.scenario_costs) && service.scenario_costs.length > 0,
  );
  if (!Array.isArray(input.pricing_scenarios)) {
    if (hasComponentScenarios) {
      const error = new Error('Component pricing scenarios require quote-level scenario totals.');
      error.code = 'pricing_scenario_cost_mismatch';
      error.details = { violations: ['quote_scenarios_missing'] };
      throw error;
    }
    return input;
  }

  const scenarioKeys = input.pricing_scenarios.map((scenario) => scenario.scenario_key);
  const uniqueKeys = new Set(scenarioKeys);
  const violations = [];
  if (uniqueKeys.size !== scenarioKeys.length) violations.push('duplicate_quote_scenario');
  if (scenarioKeys.some((key) => !SCENARIO_KEYS.includes(key))) {
    violations.push('unknown_quote_scenario');
  }

  const primaryKey = uniqueKeys.has('on_demand') ? 'on_demand' : scenarioKeys[0];
  const primaryTotal = input.pricing_scenarios.find(
    (scenario) => scenario.scenario_key === primaryKey,
  );
  if (!primaryTotal
    || roundedCents(primaryTotal.monthly_total) !== roundedCents(input.expected_monthly_total)) {
    violations.push('primary_scenario_total_mismatch');
  }

  for (const scenario of input.pricing_scenarios) {
    if (!Number.isFinite(Number(scenario.monthly_total)) || Number(scenario.monthly_total) < 0
      || !Number.isFinite(Number(scenario.upfront_total)) || Number(scenario.upfront_total) < 0) {
      violations.push(`invalid_quote_scenario_cost:${scenario.scenario_key}`);
      continue;
    }
    let monthly = 0;
    let upfront = 0;
    for (const service of input.services || []) {
      const matching = (service.scenario_costs || []).filter(
        (cost) => cost.scenario_key === scenario.scenario_key,
      );
      if (matching.length !== 1) {
        violations.push(
          `component_scenario_missing_or_duplicate:${service.component_key}:${scenario.scenario_key}`,
        );
        continue;
      }
      const cost = matching[0];
      if (!Number.isFinite(Number(cost.monthly_cost)) || Number(cost.monthly_cost) < 0
        || !Number.isFinite(Number(cost.upfront_cost)) || Number(cost.upfront_cost) < 0) {
        violations.push(`invalid_component_scenario_cost:${service.component_key}:${scenario.scenario_key}`);
        continue;
      }
      monthly += Number(cost.monthly_cost);
      upfront += Number(cost.upfront_cost);
      if (scenario.scenario_key === primaryKey
        && roundedCents(cost.monthly_cost) !== roundedCents(service.expected_monthly_cost)) {
        violations.push(`primary_component_cost_mismatch:${service.component_key}`);
      }
    }
    if (roundedCents(monthly) !== roundedCents(scenario.monthly_total)) {
      violations.push(`scenario_monthly_total_mismatch:${scenario.scenario_key}`);
    }
    if (roundedCents(upfront) !== roundedCents(scenario.upfront_total)) {
      violations.push(`scenario_upfront_total_mismatch:${scenario.scenario_key}`);
    }
  }

  for (const service of input.services || []) {
    const componentKeys = (service.scenario_costs || []).map((cost) => cost.scenario_key);
    if (componentKeys.length !== uniqueKeys.size
      || componentKeys.some((key) => !uniqueKeys.has(key))
      || new Set(componentKeys).size !== componentKeys.length) {
      violations.push(`component_scenario_set_mismatch:${service.component_key}`);
    }
  }

  if (violations.length > 0) {
    const error = new Error('Pricing scenario costs do not reconcile.');
    error.code = 'pricing_scenario_cost_mismatch';
    error.details = { violations };
    throw error;
  }
  return input;
}

function validateScenarioSemantics(input) {
  const violations = [];
  const supported = new Set(
    pricingScenarios(input.cloud_provider).map((scenario) => scenario.key),
  );
  for (const scenario of input.pricing_scenarios || []) {
    if (!supported.has(scenario.scenario_key)) {
      violations.push(`provider_scenario_not_supported:${input.cloud_provider}:${scenario.scenario_key}`);
    }
  }
  for (const service of input.services || []) {
    for (const scenario of service.scenario_costs || []) {
      if (scenario.scenario_key === 'on_demand' && scenario.pricing_basis !== 'on_demand') {
        violations.push(`on_demand_basis_invalid:${service.component_key}`);
      }
      if (scenario.scenario_key !== 'on_demand' && scenario.pricing_basis === 'on_demand') {
        violations.push(
          `commitment_basis_invalid:${service.component_key}:${scenario.scenario_key}`,
        );
      }
    }
  }
  if (violations.length > 0) {
    const error = new Error('Pricing scenario keys and pricing bases are structurally inconsistent.');
    error.code = 'pricing_scenario_semantics_invalid';
    error.details = { violations };
    throw error;
  }
  return input;
}

function officialCosts(input) {
  const monthly = Number(input.expected_monthly_total);
  const services = input.services || [];
  const zeroCostServices = input.zero_cost_services || [];
  return {
    upfront: 0,
    monthly,
    total_12_months: monthly * 12,
    currency: input.currency,
    pricing_scenarios: input.pricing_scenarios || [],
    line_items: services.map((service) => ({
      component_key: service.component_key,
      service_name: service.customer_facing?.service_name || service.component_key,
      monthly: Number(service.expected_monthly_cost),
      scenario_costs: service.scenario_costs || [],
    })).concat(zeroCostServices.map((service) => ({
      component_key: service.component_key,
      service_name: service.customer_facing?.service_name || service.component_key,
      monthly: 0,
      scenario_costs: [],
    }))),
    source: allOfficialPageEvidence(input).length > 0
      ? 'Official cloud price API and pricing pages'
      : 'Official cloud price catalog',
  };
}

function validatePartialQuoteContract(input, priceBatch) {
  const unpriced = input.unpriced_services || [];
  const violations = [];
  if (input.is_partial === true && unpriced.length === 0) {
    violations.push('partial_quote_has_no_unpriced_components');
  }
  if (input.is_partial !== true && unpriced.length > 0) {
    violations.push('complete_quote_contains_unpriced_components');
  }
  const planned = new Set(
    (priceBatch.quote_components || []).map((item) => item.component_key),
  );
  if (planned.size > 0) {
    const submitted = new Set([
      ...(input.services || []).map((item) => item.component_key),
      ...(input.zero_cost_services || []).map((item) => item.component_key),
      ...unpriced.map((item) => item.component_key),
    ]);
    for (const componentKey of planned) {
      if (!submitted.has(componentKey)) violations.push(`planned_component_missing:${componentKey}`);
    }
    for (const componentKey of submitted) {
      if (!planned.has(componentKey)) violations.push(`unknown_planned_component:${componentKey}`);
    }
  }
  if (violations.length > 0) {
    const error = new Error('Partial quote components do not match the sealed quote plan.');
    error.code = 'partial_quote_contract_invalid';
    error.details = { violations };
    throw error;
  }
}

function evidenceReferences(holder) {
  if (Array.isArray(holder?.price_evidence) && holder.price_evidence.length > 0) {
    return holder.price_evidence;
  }
  return (holder?.price_query_ids || []).map((queryId) => ({
    query_id: queryId,
    official_item_ids: [],
  }));
}

function officialPageEvidenceReferences(holder) {
  return Array.isArray(holder?.official_page_price_evidence)
    ? holder.official_page_price_evidence
    : [];
}

function componentEvidenceGroups(component) {
  return [
    { component, holder: component, scenario_key: null },
    ...(component.scenario_costs || []).map((scenario) => ({
      component, holder: scenario, scenario_key: scenario.scenario_key,
    })),
  ];
}

function allOfficialPageEvidence(input) {
  return (input.services || []).flatMap((component) => (
    componentEvidenceGroups(component).flatMap((group) => (
      officialPageEvidenceReferences(group.holder).map((evidence) => ({
        ...evidence,
        component_key: component.component_key,
        selected_scenario_key: group.scenario_key,
      }))
    ))
  ));
}


function cachedOfficialPriceDisclosure(input, priceBatch) {
  const results = new Map(
    (priceBatch.result?.results || []).map((result) => [result.query_id, result]),
  );
  const observedAt = new Set();
  const queryIds = new Set();
  for (const component of input.services || []) {
    const references = [
      ...evidenceReferences(component),
      ...(component.scenario_costs || []).flatMap(evidenceReferences),
    ];
    const stale = references.map((reference) => results.get(reference.query_id)).filter(
      (result) => result?.cache_status === 'stale_fallback',
    );
    if (stale.length === 0) continue;
    for (const result of stale) {
      queryIds.add(result.query_id);
      if (result.official_price_observed_at) observedAt.add(result.official_price_observed_at);
    }
  }
  return {
    metadata: {
      used: queryIds.size > 0,
      query_ids: [...queryIds],
      official_price_observed_at: [...observedAt].sort(),
    },
  };
}

const FAILURE_CATEGORY_PRIORITY = Object.freeze({
  credentials: 100,
  authorization: 95,
  invalid_request: 80,
  response_schema: 70,
  official_api_error: 60,
  provider_unavailable: 50,
  rate_limit: 40,
  transport: 30,
});

function safeProviderCode(value) {
  const code = String(value || '');
  return /^[A-Za-z0-9_.:-]{1,120}$/.test(code) ? code : '';
}

function enrichUnpricedServices(input, priceBatch) {
  const failuresByComponent = new Map();
  const componentByQuery = new Map(
    (priceBatch?.query_contexts || []).map((context) => [
      context.query_id, context.component_key,
    ]),
  );
  for (const result of priceBatch?.result?.results || []) {
    if (result.status !== 'query_failed') continue;
    const componentKey = componentByQuery.get(result.query_id);
    if (!componentKey) continue;
    const failures = failuresByComponent.get(componentKey) || [];
    failures.push(result);
    failuresByComponent.set(componentKey, failures);
  }
  return (input.unpriced_services || []).map((component) => {
    const selected = (failuresByComponent.get(component.component_key) || [])
      .sort((left, right) => (
        (FAILURE_CATEGORY_PRIORITY[right.error_category] || 0)
        - (FAILURE_CATEGORY_PRIORITY[left.error_category] || 0)
      ))[0];
    if (!selected) return component;
    const providerCode = safeProviderCode(selected.details?.provider_code || selected.code);
    return {
      ...component,
      failure_category: selected.error_category || 'official_api_error',
      ...(providerCode ? { provider_code: providerCode } : {}),
      retryable: selected.retryable === true,
    };
  });
}

function prepareOfficialApiSubmission(input) {
  const facts = new Map();
  const violations = [];
  for (const fact of input.fact_ledger || []) {
    if (facts.has(fact.fact_id)) violations.push(`duplicate_fact_id:${fact.fact_id}`);
    facts.set(fact.fact_id, fact);
  }
  const coverage = {};
  const consumed = new Set();
  const register = (
    factId, component, coverageType, priceQueryIds = [], officialPageUrls = [],
  ) => {
    const fact = facts.get(factId);
    if (!fact) {
      violations.push(`unknown_fact:${component.component_key}:${factId}`);
      return;
    }
    if (fact.component_key !== component.component_key) {
      violations.push(`cross_component_fact:${component.component_key}:${factId}`);
      return;
    }
    if (consumed.has(factId)) {
      violations.push(`duplicate_fact_consumption:${factId}`);
      return;
    }
    consumed.add(factId);
    coverage[factId] = [{
      component_key: component.component_key,
      coverage_type: coverageType,
      price_query_ids: priceQueryIds,
      official_page_urls: officialPageUrls,
    }];
  };

  for (const component of input.services || []) {
    const componentQueryIds = componentEvidenceGroups(component)
      .flatMap((group) => evidenceReferences(group.holder)).map((ref) => ref.query_id);
    const componentPageEvidence = componentEvidenceGroups(component)
      .flatMap((group) => officialPageEvidenceReferences(group.holder));
    const componentPageUrls = componentPageEvidence.map((item) => item.source_url);
    const coverageType = componentQueryIds.length > 0
      ? (componentPageUrls.length > 0 ? 'official_price_api_and_page' : 'official_price')
      : 'official_price_page';
    for (const factId of component.fact_ids || []) {
      const fact = facts.get(factId);
      if (fact?.disposition === 'zero_cost') {
        violations.push(`zero_cost_fact_in_priced_service:${component.component_key}:${factId}`);
        continue;
      }
      register(factId, component, coverageType, componentQueryIds, componentPageUrls);
    }
  }
  for (const component of input.zero_cost_services || []) {
    for (const factId of component.fact_ids || []) {
      const fact = facts.get(factId);
      if (fact && fact.disposition !== 'zero_cost') {
        violations.push(`non_zero_fact_in_zero_cost_service:${component.component_key}:${factId}`);
        continue;
      }
      register(factId, component, 'zero_cost');
    }
  }
  for (const component of input.unpriced_services || []) {
    for (const factId of component.fact_ids || []) {
      const fact = facts.get(factId);
      if (fact?.disposition === 'zero_cost') {
        violations.push(`zero_cost_fact_in_unpriced_service:${component.component_key}:${factId}`);
        continue;
      }
      register(factId, component, 'unpriced');
    }
  }
  for (const fact of facts.values()) {
    if (fact.disposition !== 'context' && !consumed.has(fact.fact_id)) {
      violations.push(`unconsumed_fact:${fact.fact_id}`);
    }
  }

  const resourceComponents = new Set([
    ...(input.services || []).map((item) => item.component_key),
    ...(input.zero_cost_services || []).map((item) => item.component_key),
    ...(input.unpriced_services || []).map((item) => item.component_key),
  ]);
  const requirementComponents = new Set(
    (input.fact_ledger || []).map((item) => item.component_key),
  );
  for (const componentKey of requirementComponents) {
    if (!resourceComponents.has(componentKey)) violations.push(`missing_resource:${componentKey}`);
  }
  for (const componentKey of resourceComponents) {
    if (!requirementComponents.has(componentKey)) violations.push(`unknown_resource:${componentKey}`);
  }
  if (violations.length > 0) {
    const error = new Error('Official price evidence does not map cleanly to priced resources.');
    error.code = 'official_api_fact_mapping_invalid';
    error.details = { violations };
    throw error;
  }

  return {
    fact_coverage: coverage,
    transformation_trace: (input.services || []).map((component) => {
      const groups = componentEvidenceGroups(component);
      const apiEvidence = groups.flatMap((group) => evidenceReferences(group.holder));
      const pageEvidence = groups.flatMap(
        (group) => officialPageEvidenceReferences(group.holder),
      );
      return {
        component_key: component.component_key,
        source_fact_ids: component.fact_ids || [],
        price_evidence: apiEvidence,
        official_page_price_evidence: pageEvidence,
        transformation: apiEvidence.length > 0
          ? (pageEvidence.length > 0
            ? 'gpt_selected_official_api_and_page_price_identity'
            : 'gpt_selected_official_api_price_identity')
          : 'gpt_selected_official_page_price_identity',
      };
    }).concat((input.unpriced_services || []).map((component) => ({
      component_key: component.component_key,
      source_fact_ids: component.fact_ids || [],
      price_evidence: [],
      transformation: 'unpriced_after_bounded_retries',
    }))),
    billing_usage_ir: (input.services || []).map((component) => {
      const groups = componentEvidenceGroups(component);
      return {
        component_key: component.component_key,
        source_fact_ids: component.fact_ids || [],
        price_evidence: groups.flatMap((group) => evidenceReferences(group.holder)),
        official_page_price_evidence: groups.flatMap(
          (group) => officialPageEvidenceReferences(group.holder),
        ),
        expected_monthly_cost: component.expected_monthly_cost,
        scenario_costs: component.scenario_costs || [],
      };
    }),
  };
}

class AstraQuoteV2Workflow {
  constructor({
    backend,
    store = new V2QuoteStore(),
    deliverer = new QuoteDeliveryService(),
    routeStore,
    priceCache,
    resultByteBudget: configuredResultByteBudget,
  }) {
    this.backend = backend;
    this.store = store;
    this.deliverer = deliverer;
    this.routeStore = routeStore || new PricingRouteStore({
      directory: path.join(this.store.directory, 'pricing-routes'),
    });
    this.priceCache = priceCache || new OfficialPriceCache({
      directory: path.join(this.store.directory, 'official-price-cache'),
    });
    this.resultByteBudget = resultByteBudget(configuredResultByteBudget);
  }

  routeInstructions() {
    return this.routeStore.instructions();
  }

  describeService(input) {
    return this.backend.describeService(input);
  }

  getAttributeValues(input) {
    return this.backend.getAttributeValues(input);
  }

  getPriceResults(input) {
    const priceBatch = this.store.getPriceBatch(input.price_batch_id);
    if (priceBatch.relay_job_id) {
      const relayJob = assertRelayIdentity(input);
      if (relayJob.job_id !== priceBatch.relay_job_id) {
        const error = new Error('The saved price batch belongs to a different sales quote task.');
        error.code = 'price_batch_relay_context_mismatch';
        throw error;
      }
    }
    const byId = new Map(
      (priceBatch.result?.results || []).map((item) => [item.query_id, item]),
    );
    const missing = input.query_ids.filter((queryId) => !byId.has(queryId));
    if (missing.length > 0) {
      const error = new Error('One or more price query results were not found in the batch.');
      error.code = 'price_query_result_not_found';
      error.details = { query_ids: missing };
      throw error;
    }
    const offset = input.detail_offset ?? 0;
    const limit = input.detail_limit ?? 20;
    if (!Number.isSafeInteger(offset) || offset < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 50) {
      const error = new Error('Detail offset must be non-negative and detail limit must be between 1 and 50.');
      error.code = 'price_result_page_invalid';
      error.retryable = true;
      throw error;
    }
    const results = input.query_ids.map((queryId) => {
      const saved = byId.get(queryId);
      const paged = { ...saved, include_raw_items: input.include_raw_items === true };
      const lengths = {};
      for (const field of ['official_item_ids', 'official_rate_candidates', 'items', 'products', 'skus', 'services']) {
        if (!Array.isArray(saved[field])) continue;
        lengths[field] = saved[field].length;
        paged[field] = saved[field].slice(offset, offset + limit);
      }
      const total = Math.max(0, ...Object.values(lengths));
      paged.detail_page = {
        offset, limit,
        total_rates: lengths.official_rate_candidates || 0,
        total_item_ids: lengths.official_item_ids || 0,
        total_items: lengths.items || lengths.products || lengths.skus || lengths.services || 0,
        next_offset: offset + limit < total ? offset + limit : null,
      };
      return paged;
    });
    const componentStatus = componentProgress(
      priceBatch.quote_components || [],
      priceBatch.query_lifecycle || [],
      priceBatch.result?.results || [],
    );
    return {
      status: priceBatch.result?.status || 'needs_refinement',
      price_batch_id: priceBatch.price_batch_id,
      result_count: input.query_ids.length,
      batch_result_count: byId.size,
      results,
      result_access: {
        tool: 'get_price_results',
        instruction: 'Read next_offset with the same price_batch_id and query_ids until it is null. Saved official evidence is never truncated.',
      },
      query_lifecycle: (priceBatch.query_lifecycle || [])
        .filter((item) => input.query_ids.includes(item.query_id)),
      progress_guidance: PROGRESS_GUIDANCE,
      workflow_guard: priceWorkflowGuard({
        quoteMode: priceBatch.quote_mode,
        relayJobId: priceBatch.relay_job_id || null,
        quoteComponents: priceBatch.quote_components || [],
        componentLifecycle: componentStatus.component_lifecycle,
      }),
    };
  }

  async getPrices(input) {
    const relayJobId = input.relay_job_id || null;
    const relayJob = relayJobId ? assertRelayIdentity(input) : null;
    const existing = input.price_batch_id
      ? this.store.getPriceBatch(input.price_batch_id)
      : null;
    if (existing && (existing.relay_job_id || null) !== relayJobId) {
      const error = new Error('The saved price batch belongs to a different sales quote task.');
      error.code = 'price_batch_relay_context_mismatch';
      throw error;
    }
    const quoteComponents = sealedComponentPlan(
      existing?.quote_components,
      input.quote_components,
    );
    const inferredQuoteMode = relayJobId || quoteComponents.length > 0
      ? 'formal_quote'
      : 'price_lookup';
    const quoteMode = input.quote_mode || existing?.quote_mode || inferredQuoteMode;
    if (existing?.quote_mode && existing.quote_mode !== quoteMode) {
      const error = new Error('The saved price batch quote mode is immutable.');
      error.code = 'price_batch_quote_mode_mismatch';
      error.retryable = true;
      error.details = { expected: existing.quote_mode, received: quoteMode };
      throw error;
    }
    const preliminaryQueries = new Map(
      (existing?.request?.queries || []).map((item) => [item.query_id, item]),
    );
    assertQueryIdentity(preliminaryQueries, input.queries);
    for (const query of input.queries) preliminaryQueries.set(query.query_id, query);
    const preliminaryContexts = mergeQueryContexts(
      existing?.query_contexts, input.query_contexts, preliminaryQueries,
    );
    assertFormalQuotePricingPlan({
      quoteMode,
      relayJobId,
      quoteComponents,
      queries: input.queries,
      queryContexts: preliminaryContexts,
    });
    const routeResolutionFailures = [];
    const resolvedQueries = input.queries.map((query) => {
      try {
        return this.routeStore.resolve(query);
      } catch (error) {
        if (!(error instanceof PricingRouteStoreError)) throw error;
        const errorCategory = routeResolutionErrorCategory(error.code);
        routeResolutionFailures.push({
          query_id: query.query_id,
          provider: query.provider,
          status: 'query_failed',
          terminal: error.retryable !== true,
          retryable: error.retryable === true,
          error_category: errorCategory,
          code: error.code || 'pricing_route_resolution_failed',
          message: error.message,
          details: error.details || {},
          recovery: {
            retryable: error.retryable === true,
            next_action: error.details?.next_action
              || (errorCategory === 'invalid_request'
                ? 'correct_pricing_route_input'
                : 'discover_and_verify_official_read_only_route'),
          },
          official_item_ids: [],
        });
        return { query: { ...query }, route: null };
      }
    });
    const materializedQueries = resolvedQueries.map((resolved) => resolved.query);
    const resolvedByQueryId = new Map(
      resolvedQueries.map((resolved) => [resolved.query.query_id, resolved]),
    );
    const queryIds = materializedQueries.map((query) => query.query_id);
    if (new Set(queryIds).size !== queryIds.length) {
      const error = new Error('Every official price query must have a unique query_id.');
      error.code = 'duplicate_price_query_id';
      throw error;
    }
    const existingResults = new Map(
      (existing?.result?.results || []).map((item) => [item.query_id, item]),
    );
    const existingQueries = new Map(
      (existing?.request?.queries || []).map((item) => [item.query_id, item]),
    );
    assertQueryIdentity(existingQueries, materializedQueries);
    const mergedQueries = new Map(existingQueries);
    for (const query of materializedQueries) mergedQueries.set(query.query_id, query);
    const queryContexts = mergeQueryContexts(
      existing?.query_contexts, input.query_contexts, mergedQueries,
    );
    const pendingQueries = [];
    const freshCacheResults = [];
    const capabilityPreflightResults = [];
    const routeResolutionFailureById = new Map(
      routeResolutionFailures.map((item) => [item.query_id, item]),
    );
    const contextById = new Map(queryContexts.map((context) => [context.query_id, context]));
    const forceCapabilityRecheck = input.force_capability_recheck === true
      || Number(relayJob?.partial_retry_generation || 0) > 0;
    for (const query of materializedQueries) {
      if (routeResolutionFailureById.has(query.query_id)) continue;
      const priorResult = existingResults.get(query.query_id);
      if (reusableQueryResult(priorResult, contextById.get(query.query_id))) {
        if (!isDeepStrictEqual(existingQueries.get(query.query_id), query)) {
          const error = new Error('A completed query_id cannot be reused for different filters.');
          error.code = 'price_query_identity_conflict';
          error.details = { query_id: query.query_id };
          throw error;
        }
        continue;
      }
      const marketProfile = this.routeStore.marketProfile(query.provider);
      const cached = this.priceCache.get(query, { marketProfile, allowStale: false });
      if (cached) {
        freshCacheResults.push(cached);
        continue;
      }
      const blocker = forceCapabilityRecheck
        ? null
        : this.routeStore.capabilityBlocker(query);
      if (blocker) {
        capabilityPreflightResults.push({
          query_id: query.query_id,
          provider: query.provider,
          status: 'query_failed',
          terminal: true,
          retryable: false,
          error_category: blocker.error_category,
          code: `${query.provider}_capability_preflight_denied`,
          message: blocker.error_category === 'credentials'
            ? 'A recent official API call proved that provider pricing credentials are unavailable.'
            : 'A recent official API call proved that this pricing operation is not authorized.',
          details: {
            provider_code: blocker.provider_code,
            observed_at: blocker.observed_at,
            recheck_after: blocker.recheck_after,
          },
          recovery: {
            retryable: false,
            next_action: blocker.next_action,
          },
          capability_preflight: true,
          official_item_ids: [],
        });
        continue;
      }
      pendingQueries.push(query);
    }

    let result;
    try {
      result = pendingQueries.length > 0
        ? await this.backend.getPrices({ queries: pendingQueries })
        : { status: 'completed', result_count: 0, results: [] };
    } catch (error) {
      if (relayJobId) {
        this.store.putCheckpoint(relayJobId, {
          stage: 'pricing_request_rejected',
          price_batch_id: existing?.price_batch_id || null,
          incomplete_query_ids: pendingQueries.map((query) => query.query_id),
          error: {
            code: error?.code || 'official_price_request_failed',
            message: error?.message || 'Official price request failed.',
            details: error?.details || {},
            retryable: error?.retryable === true,
          },
        });
      }
      throw error;
    }
    const liveResults = (result.results || []).map((item) => {
      const resolved = resolvedByQueryId.get(item.query_id);
      return resolved?.route
        ? { ...item, reused_route_id: resolved.route.route_id }
        : item;
    });
    const transientCategories = new Set(['transport', 'rate_limit', 'provider_unavailable']);
    const completedLiveResults = liveResults.map((item) => {
      const query = resolvedByQueryId.get(item.query_id)?.query;
      if (!query) return item;
      const marketProfile = this.routeStore.marketProfile(query.provider);
      if (item.status === 'query_failed' && transientCategories.has(item.error_category)) {
        const stale = this.priceCache.get(query, { marketProfile, allowStale: true });
        if (stale?.cache_status === 'stale') {
          return {
            ...stale,
            cache_status: 'stale_fallback',
            source_request_skipped: false,
            live_error: {
              error_category: item.error_category,
              code: item.code,
              message: item.message,
            },
          };
        }
      }
      this.priceCache.put(query, item, { marketProfile });
      return item;
    });
    const responseResults = new Map([
      ...freshCacheResults,
      ...capabilityPreflightResults,
      ...routeResolutionFailures,
      ...completedLiveResults,
    ].map((item) => [item.query_id, item]));
    result = {
      ...result,
      result_count: responseResults.size,
      results: materializedQueries
        .map((query) => responseResults.get(query.query_id)).filter(Boolean),
    };
    const learnedRoutes = this.routeStore.recordBatch(pendingQueries, liveResults);
    result = {
      ...result,
      results: (result.results || []).map((item) => {
        const resolved = resolvedByQueryId.get(item.query_id);
        const query = resolved?.query;
        const pricingKnowledge = query ? this.routeStore.knowledgeForQuery(query) : [];
        const routeHealth = resolved?.route
          ? this.routeStore.healthForRoute(resolved.route.route_id)
          : null;
        return {
          ...item,
          ...(pricingKnowledge.length > 0 ? { pricing_knowledge: pricingKnowledge } : {}),
          ...(routeHealth ? { pricing_route_health: routeHealth } : {}),
        };
      }),
    };
    const priceBatchId = existing?.price_batch_id || `aqpb_${randomUUID()}`;
    // Parallel component chats may finish their official API calls in either
    // order. Re-read the batch after the await and perform the final merge in
    // one synchronous event-loop section so a later response cannot overwrite
    // evidence already saved by another chat.
    const latest = existing ? this.store.getPriceBatch(priceBatchId) : existing;
    const latestQueries = new Map(
      (latest?.request?.queries || []).map((item) => [item.query_id, item]),
    );
    assertQueryIdentity(latestQueries, materializedQueries);
    for (const query of materializedQueries) {
      const latestQuery = latestQueries.get(query.query_id);
      if (latestQuery && !isDeepStrictEqual(latestQuery, existingQueries.get(query.query_id))
        && !isDeepStrictEqual(latestQuery, query)) {
        const error = new Error('Another concurrent request changed this query identity; retry with a new query_id.');
        error.code = 'price_query_identity_conflict';
        error.retryable = true;
        error.details = { query_id: query.query_id };
        throw error;
      }
    }
    const finalQueries = new Map(latestQueries);
    for (const query of materializedQueries) finalQueries.set(query.query_id, query);
    const finalQueryContexts = mergeQueryContexts(
      latest?.query_contexts, input.query_contexts, finalQueries,
    );
    const finalQuoteComponents = sealedComponentPlan(
      latest?.quote_components || quoteComponents,
      input.quote_components,
    );
    const mergedResults = new Map(
      (latest?.result?.results || []).map((item) => [item.query_id, item]),
    );
    const finalContextById = new Map(finalQueryContexts.map((context) => [context.query_id, context]));
    result = { ...result, results: (result.results || []).map((item) => {
      const alreadySaved = mergedResults.get(item.query_id);
      // A slower retry must not undo evidence another chat has already saved.
      if (reusableQueryResult(alreadySaved, finalContextById.get(item.query_id))
        && isDeepStrictEqual(latestQueries.get(item.query_id), finalQueries.get(item.query_id))) {
        return alreadySaved;
      }
      mergedResults.set(item.query_id, item);
      return item;
    }) };
    const progress = queryProgress(
      [...finalQueries.values()], [...mergedResults.values()], finalQueryContexts,
    );
    const componentStatus = componentProgress(
      finalQuoteComponents,
      progress.query_lifecycle,
      [...mergedResults.values()],
    );
    const incompleteQueryIds = progress.incomplete_query_ids;
    const completed = quoteComponents.length > 0
      ? componentStatus.completed_component_count === componentStatus.total_component_count
      : progress.status === 'completed';
    const quoteTerminal = progress.quote_terminal
      && (finalQuoteComponents.length === 0 || componentStatus.pending_component_count === 0);
    const mergedResult = {
      status: completed ? 'completed' : 'needs_refinement',
      terminal: completed,
      next_action: completed ? 'build_estimate' : progress.next_action,
      result_count: mergedResults.size,
      results: [...mergedResults.values()],
    };
    this.store.putPriceBatch({
      schema_version: 'astraquote-v3-price-batch/1',
      price_batch_id: priceBatchId,
      created_at: latest?.created_at || new Date().toISOString(),
      updated_at: new Date().toISOString(),
      relay_job_id: relayJobId,
      quote_mode: quoteMode,
      request: { queries: [...finalQueries.values()] },
      quote_components: finalQuoteComponents,
      query_contexts: finalQueryContexts,
      query_lifecycle: progress.query_lifecycle,
      component_lifecycle: componentStatus.component_lifecycle,
      result: mergedResult,
    });
    if (relayJobId) {
      this.store.putCheckpoint(relayJobId, {
        stage: completed ? 'pricing_completed' : 'pricing_partial',
        price_batch_id: priceBatchId,
        incomplete_query_ids: incompleteQueryIds,
        batch_query_count: finalQueries.size,
        completed_query_count: progress.completed_query_count,
        incomplete_query_count: incompleteQueryIds.length,
        superseded_query_ids: progress.superseded_query_ids,
        discovery_query_count: progress.discovery_query_count,
        total_component_count: componentStatus.total_component_count,
        top_level_component_count: componentStatus.top_level_component_count,
        component_chat_count: componentStatus.component_chat_count,
        completed_component_count: componentStatus.completed_component_count,
        failed_component_count: componentStatus.failed_component_count,
        pending_component_count: componentStatus.pending_component_count,
        completed_component_keys: componentStatus.component_lifecycle
          .filter((item) => item.state === 'completed').map((item) => item.component_key),
        failed_component_keys: componentStatus.component_lifecycle
          .filter((item) => item.state === 'failed').map((item) => item.component_key),
        partial_retry_generation: Number(relayJob?.partial_retry_generation || 0),
        progress_guidance: PROGRESS_GUIDANCE,
        quote_terminal: quoteTerminal,
      });
    }
    return compactIncrementalPriceResponse({
      status: mergedResult.status,
      terminal: mergedResult.terminal,
      quote_terminal: quoteTerminal,
      must_continue: !quoteTerminal,
      next_action: mergedResult.next_action,
      result_count: (result.results || []).length,
      batch_result_count: mergedResults.size,
      results: result.results || [],
      incomplete_query_ids: incompleteQueryIds,
      superseded_query_ids: progress.superseded_query_ids,
      discovery_query_count: progress.discovery_query_count,
      progress_guidance: PROGRESS_GUIDANCE,
      query_lifecycle: progress.query_lifecycle.filter((item) => (
        queryIds.includes(item.query_id) || item.state === 'superseded'
      )),
      price_batch_id: priceBatchId,
      learned_routes: learnedRoutes,
      resumed_batch: Boolean(existing),
      reused_query_ids: materializedQueries
        .filter((query) => !pendingQueries.includes(query)
          && !freshCacheResults.some((item) => item.query_id === query.query_id)
          && !capabilityPreflightResults.some((item) => item.query_id === query.query_id))
        .map((query) => query.query_id),
      queried_query_ids: pendingQueries.map((query) => query.query_id),
      price_cache_hit_query_ids: freshCacheResults.map((item) => item.query_id),
      stale_price_fallback_query_ids: completedLiveResults
        .filter((item) => item.cache_status === 'stale_fallback')
        .map((item) => item.query_id),
      capability_preflight_query_ids: capabilityPreflightResults
        .map((item) => item.query_id),
      total_component_count: componentStatus.total_component_count,
      top_level_component_count: componentStatus.top_level_component_count,
      component_chat_count: componentStatus.component_chat_count,
      completed_component_count: componentStatus.completed_component_count,
      failed_component_count: componentStatus.failed_component_count,
      pending_component_count: componentStatus.pending_component_count,
      workflow_guard: priceWorkflowGuard({
        quoteMode,
        relayJobId,
        quoteComponents: finalQuoteComponents,
        componentLifecycle: componentStatus.component_lifecycle,
      }),
    }, this.resultByteBudget);
  }

  getQuoteJobStatus(input) {
    const job = assertRelayIdentity(input);
    const checkpoint = this.store.getCheckpoint(input.relay_job_id);
    if (!checkpoint) {
      return {
        relay_job_id: input.relay_job_id,
        relay_status: job.status,
        stage: 'created',
        next_action: CREATED_PRICE_NEXT_ACTION,
        terminal: false,
        must_continue: true,
      };
    }
    const requestedRetryGeneration = Number(job.partial_retry_generation || 0);
    const checkpointRetryGeneration = Number(checkpoint.partial_retry_generation || 0);
    if (requestedRetryGeneration > checkpointRetryGeneration) {
      return {
        relay_job_id: input.relay_job_id,
        relay_status: job.status,
        stage: 'partial_retry_requested',
        price_batch_id: checkpoint.price_batch_id || null,
        failed_component_keys: checkpoint.failed_component_keys || [],
        total_component_count: checkpoint.total_component_count,
        top_level_component_count: checkpoint.top_level_component_count,
        component_chat_count: checkpoint.component_chat_count,
        completed_component_count: checkpoint.completed_component_count,
        failed_component_count: checkpoint.failed_component_count,
        pending_component_count: checkpoint.pending_component_count,
        terminal: false,
        must_continue: true,
      };
    }
    return {
      relay_job_id: input.relay_job_id,
      relay_status: job.status,
      stage: checkpoint.stage,
      price_batch_id: checkpoint.price_batch_id || null,
      incomplete_query_ids: checkpoint.incomplete_query_ids || [],
      batch_query_count: checkpoint.batch_query_count,
      completed_query_count: checkpoint.completed_query_count,
      incomplete_query_count: checkpoint.incomplete_query_count,
      total_component_count: checkpoint.total_component_count,
      top_level_component_count: checkpoint.top_level_component_count,
      component_chat_count: checkpoint.component_chat_count,
      completed_component_count: checkpoint.completed_component_count,
      failed_component_count: checkpoint.failed_component_count,
      pending_component_count: checkpoint.pending_component_count,
      failed_component_keys: checkpoint.failed_component_keys || [],
      quote_terminal: checkpoint.quote_terminal === true,
      superseded_query_ids: checkpoint.superseded_query_ids || [],
      discovery_query_count: checkpoint.discovery_query_count,
      progress_guidance: PROGRESS_GUIDANCE,
      quote_id: checkpoint.quote_id || null,
      failed_stage: checkpoint.failed_stage || null,
      error: checkpoint.error || undefined,
      result: checkpoint.stage === 'delivery_completed' ? checkpoint.result : undefined,
    };
  }

  resumeQuoteJob(input) {
    const status = this.getQuoteJobStatus(input);
    const nextActions = {
      created: CREATED_PRICE_NEXT_ACTION,
      pricing_request_rejected: 'Correct the rejected request using the saved field-level details, then retry get_prices.',
      pricing_partial: `Continue the next missing component price in the same price_batch_id. ${PROGRESS_GUIDANCE}`,
      pricing_completed: 'Reuse price_batch_id and continue with build_estimate.',
      partial_retry_requested: 'Reuse successful evidence and retry only the saved failed component keys.',
      estimate_validated: 'Resume artifact generation and sales-page delivery only.',
      artifacts_generated: 'Reuse the existing Excel artifact and finish the completion receipt.',
      delivery_completed: 'Return result exactly as saved; do not query, generate or deliver again.',
      failed: 'Inspect the saved failure and retry only its missing stage.',
    };
    const terminal = status.stage === 'delivery_completed'
      || (status.stage === 'failed' && status.error?.retryable !== true);
    return {
      ...status,
      resumed_from: status.stage,
      next_action: nextActions[status.stage],
      terminal,
      must_continue: !terminal,
    };
  }

  validateOfficialPagePriceEvidence(input, priceBatch) {
    const results = new Map(
      (priceBatch.result?.results || []).map((result) => [result.query_id, result]),
    );
    const contexts = new Map(
      (priceBatch.query_contexts || []).map((context) => [context.query_id, context]),
    );
    const contextsByScope = new Map();
    for (const context of contexts.values()) {
      const key = pricingScopeKey(
        context.component_key, context.billing_key, context.scenario_key || null,
      );
      if (context.purpose !== 'pricing' || !key) continue;
      const entries = contextsByScope.get(key) || [];
      entries.push(context);
      contextsByScope.set(key, entries);
    }
    const marketProfile = this.routeStore.marketProfile(input.cloud_provider);
    const coveredScopes = new Set();
    const acceptedEvidence = [];
    const violations = [];
    const disallowedFailureCategories = new Set(['credentials', 'authorization']);

    for (const component of input.services || []) {
      for (const group of componentEvidenceGroups(component)) {
        for (const evidence of officialPageEvidenceReferences(group.holder)) {
          const before = violations.length;
          const selectedScenario = evidence.scenario_key || null;
          if (selectedScenario !== group.scenario_key) {
            violations.push(
              `official_page_scenario_mismatch:${component.component_key}:${evidence.billing_key}`,
            );
          }
          const evidenceScope = pricingScopeKey(
            component.component_key, evidence.billing_key, selectedScenario,
          );
          const expectedRegion = component.region || input.default_region;
          if (evidence.region !== expectedRegion) {
            violations.push(
              `official_page_region_mismatch:${component.component_key}:${evidence.billing_key}:${evidence.region}:${expectedRegion}`,
            );
          }
          if (evidence.currency !== input.currency) {
            violations.push(
              `official_page_currency_mismatch:${component.component_key}:${evidence.billing_key}:${evidence.currency}:${input.currency}`,
            );
          }
          if (!(Number(evidence.unit_price) > 0)) {
            violations.push(
              `official_page_positive_rate_required:${component.component_key}:${evidence.billing_key}`,
            );
          }
          if (!officialPricingPageUrlAllowed(
            input.cloud_provider, evidence.source_url, marketProfile,
          )) {
            violations.push(
              `official_page_host_not_allowed:${component.component_key}:${evidence.billing_key}`,
            );
          }
          const observedAt = Date.parse(evidence.observed_at);
          if (!Number.isFinite(observedAt) || observedAt > Date.now() + 5 * 60 * 1000) {
            violations.push(
              `official_page_observed_at_invalid:${component.component_key}:${evidence.billing_key}`,
            );
          }

          const attemptIds = [...new Set(evidence.api_attempt_query_ids || [])];
          if (attemptIds.length < 3) {
            violations.push(
              `official_page_requires_three_api_attempts:${component.component_key}:${evidence.billing_key}`,
            );
          }
          let qualifyingAttempts = 0;
          for (const queryId of attemptIds) {
            const context = contexts.get(queryId);
            const result = results.get(queryId);
            const attemptScope = context && pricingScopeKey(
              context.component_key, context.billing_key, context.scenario_key || null,
            );
            if (!context || context.purpose !== 'pricing' || attemptScope !== evidenceScope) {
              violations.push(
                `official_page_api_attempt_scope_mismatch:${component.component_key}:${evidence.billing_key}:${queryId}`,
              );
              continue;
            }
            if (!result || result.provider !== input.cloud_provider) {
              violations.push(
                `official_page_api_attempt_missing_or_wrong_provider:${component.component_key}:${evidence.billing_key}:${queryId}`,
              );
              continue;
            }
            if (disallowedFailureCategories.has(result.error_category)
              || result.capability_preflight === true) {
              violations.push(
                `official_page_api_attempt_is_permission_blocker:${component.component_key}:${evidence.billing_key}:${queryId}`,
              );
              continue;
            }
            if (!['not_found', 'query_failed'].includes(result.status)) {
              violations.push(
                `official_page_api_attempt_not_failed:${component.component_key}:${evidence.billing_key}:${queryId}:${result.status}`,
              );
              continue;
            }
            qualifyingAttempts += 1;
          }
          if (qualifyingAttempts < 3) {
            violations.push(
              `official_page_has_fewer_than_three_qualifying_failures:${component.component_key}:${evidence.billing_key}`,
            );
          }

          const usableApiExists = (contextsByScope.get(evidenceScope) || []).some((context) => (
            reusableQueryResult(results.get(context.query_id), context)
          ));
          if (usableApiExists) {
            violations.push(
              `official_page_fallback_forbidden_when_api_price_exists:${component.component_key}:${evidence.billing_key}`,
            );
          }

          if (violations.length === before) {
            coveredScopes.add(evidenceScope);
            acceptedEvidence.push({
              source: 'official_pricing_page',
              component_key: component.component_key,
              scenario_key: selectedScenario,
              billing_key: evidence.billing_key,
              source_url: evidence.source_url,
              source_title: evidence.source_title,
              price_item: evidence.price_item,
              region: evidence.region,
              currency: evidence.currency,
              unit_price: evidence.unit_price,
              unit: evidence.unit,
              observed_at: evidence.observed_at,
              source_excerpt: evidence.source_excerpt,
              api_attempt_query_ids: attemptIds,
            });
          }
        }
      }
    }
    return { acceptedEvidence, coveredScopes, violations };
  }

  validatePriceEvidence(input, priceBatch) {
    const signedCatalogProviders = new Set([
      'tencent', 'alibaba', 'huawei', 'baidu', 'volcengine', 'ctyun',
    ]);
    const priceResults = new Map(
      (priceBatch.result.results || []).map((result) => [result.query_id, result]),
    );
    const queryContexts = new Map((priceBatch.query_contexts || []).map((item) => [item.query_id, item]));
    const pageEvidence = this.validateOfficialPagePriceEvidence(input, priceBatch);
    const violations = [
      ...pageEvidence.violations,
      ...contextEvidenceViolations(input, priceBatch, evidenceReferences, {
        additionalCoveredScopes: pageEvidence.coveredScopes,
      }),
    ];
    for (const component of input.services) {
      const refs = [
        ...evidenceReferences(component),
        ...(component.scenario_costs || []).flatMap((scenario) => evidenceReferences(scenario)),
      ];
      const pageRefs = componentEvidenceGroups(component).flatMap(
        (group) => officialPageEvidenceReferences(group.holder),
      );
      if (refs.length === 0 && pageRefs.length === 0) {
        violations.push(`price_evidence_missing:${component.component_key}`);
      }
      for (const ref of refs) {
        const result = priceResults.get(ref.query_id);
        if (!result) {
          violations.push(`unknown_price_query:${component.component_key}:${ref.query_id}`);
          continue;
        }
        if (result.provider !== input.cloud_provider) {
          violations.push(`price_provider_mismatch:${component.component_key}:${ref.query_id}`);
        }
        if (!reusableQueryResult(result, queryContexts.get(ref.query_id))) {
          violations.push(`price_query_unusable:${component.component_key}:${ref.query_id}:${result.status}`);
          continue;
        }
        const available = new Set(result.official_item_ids || []);
        const selected = ref.official_item_ids || [];
        const rateCandidates = Array.isArray(result.official_rate_candidates)
          ? result.official_rate_candidates
          : [];
        const availableRates = new Map(
          rateCandidates.map((rate) => [rate.rate_id, rate]),
        );
        const selectedRates = ref.official_rate_ids || [];
        const selectedRateCurrencies = new Set();
        if (signedCatalogProviders.has(input.cloud_provider) && rateCandidates.length === 0) {
          violations.push(`commercial_rate_evidence_missing:${component.component_key}:${ref.query_id}`);
        }
        if (signedCatalogProviders.has(input.cloud_provider) && selectedRates.length === 0) {
          violations.push(`commercial_rate_selection_required:${component.component_key}:${ref.query_id}`);
        }
        if (result.status === 'ambiguous' && selected.length === 0) {
          violations.push(`official_item_selection_required:${component.component_key}:${ref.query_id}`);
        }
        for (const itemId of selected) {
          if (!available.has(itemId)) {
            violations.push(`unknown_official_item:${component.component_key}:${ref.query_id}:${itemId}`);
          }
        }
        for (const rateId of selectedRates) {
          const rate = availableRates.get(rateId);
          if (!rate) {
            violations.push(`unknown_official_rate:${component.component_key}:${ref.query_id}:${rateId}`);
            continue;
          }
          if (!selected.includes(rate.official_item_id)) {
            violations.push(`official_rate_item_mismatch:${component.component_key}:${ref.query_id}:${rateId}`);
          }
          if (rate.is_zero_rate === true || Number(rate.unit_price) === 0) {
            violations.push(`free_or_zero_rate_forbidden:${component.component_key}:${ref.query_id}:${rateId}`);
          }
          if (String(rate.currency || '').toUpperCase() !== input.currency) {
            violations.push(
              `price_currency_mismatch:${component.component_key}:${ref.query_id}:${rateId}:${rate.currency || 'missing'}:${input.currency}`,
            );
          }
          selectedRateCurrencies.add(String(rate.currency || '').toUpperCase());
        }
        if (selectedRateCurrencies.size > 1) {
          violations.push(
            `mixed_price_currencies:${component.component_key}:${ref.query_id}:${[...selectedRateCurrencies].join(',')}`,
          );
        }
        if (selectedRates.length === 0 && rateCandidates.length > 0) {
          const selectedItemRates = rateCandidates.filter(
            (rate) => selected.includes(rate.official_item_id),
          );
          const itemCurrencies = new Set(
            selectedItemRates.map((rate) => String(rate.currency || '').toUpperCase()),
          );
          if (itemCurrencies.size > 0 && !itemCurrencies.has(input.currency)) {
            violations.push(
              `price_currency_mismatch:${component.component_key}:${ref.query_id}:selected_items:${[...itemCurrencies].join(',')}:${input.currency}`,
            );
          }
          if (itemCurrencies.size > 1) {
            violations.push(
              `mixed_price_currencies:${component.component_key}:${ref.query_id}:${[...itemCurrencies].join(',')}`,
            );
          }
        }
        for (const itemId of selected) {
          const itemRates = rateCandidates.filter((rate) => rate.official_item_id === itemId);
          if (!itemRates.some((rate) => rate.is_zero_rate === true || Number(rate.unit_price) === 0)) {
            continue;
          }
          const selectedItemRates = selectedRates
            .map((rateId) => availableRates.get(rateId))
            .filter((rate) => rate?.official_item_id === itemId);
          if (!selectedItemRates.some(
            (rate) => rate.is_zero_rate !== true && Number(rate.unit_price) > 0,
          )) {
            violations.push(`paid_rate_selection_required:${component.component_key}:${ref.query_id}:${itemId}`);
          }
        }
      }
      for (const scenario of component.scenario_costs || []) {
        const scenarioHasOfficialPageEvidence = officialPageEvidenceReferences(scenario).length > 0;
        const results = evidenceReferences(scenario)
          .map((ref) => priceResults.get(ref.query_id))
          .filter(Boolean);
        if (scenario.pricing_basis === 'on_demand_fallback') {
          const onDemandCost = (component.scenario_costs || []).find(
            (cost) => cost.scenario_key === 'on_demand',
          );
          if (onDemandCost
            && roundedCents(onDemandCost.monthly_cost) !== roundedCents(scenario.monthly_cost)) {
            violations.push(`scenario_fallback_cost_changed:${component.component_key}:${scenario.scenario_key}`);
          }
        }
        if (input.cloud_provider !== 'aws') continue;
        const expectedBasis = scenario.pricing_basis === 'on_demand_fallback'
          ? 'on_demand'
          : scenario.pricing_basis;
        const allowedModels = new Set([expectedBasis, 'on_demand']);
        if (results.some((result) => result.pricing_model && !allowedModels.has(result.pricing_model))) {
          violations.push(`scenario_price_basis_mismatch:${component.component_key}:${scenario.scenario_key}`);
        }
        if (scenario.pricing_basis === 'on_demand_fallback'
          && results.some((result) => result.pricing_model && result.pricing_model !== 'on_demand')) {
          violations.push(`scenario_fallback_not_on_demand:${component.component_key}:${scenario.scenario_key}`);
        }
        if (scenario.pricing_basis === 'reserved'
          && !scenarioHasOfficialPageEvidence
          && !results.some((result) => result.pricing_model === 'reserved')) {
          violations.push(`scenario_committed_evidence_missing:${component.component_key}:${scenario.scenario_key}`);
        }
        const expectedTermYears = {
          one_year_commitment: 1,
          three_year_commitment: 3,
        }[scenario.scenario_key];
        if (scenario.pricing_basis === 'reserved'
          && !scenarioHasOfficialPageEvidence
          && !results.some((result) => (
            result.pricing_model === 'reserved'
            && Number(result.term_years) === expectedTermYears
            && result.payment_option === 'all_upfront'
          ))) {
          violations.push(`scenario_commitment_terms_mismatch:${component.component_key}:${scenario.scenario_key}`);
        }
      }
    }
    if (violations.length > 0) {
      const error = new Error('Official price evidence is missing or ambiguous.');
      error.code = 'official_price_evidence_invalid';
      error.retryable = true;
      error.details = { violations, next_action: 'repair_selected_component_price_evidence' };
      throw error;
    }
    return pageEvidence;
  }

  validateZeroCostEvidence(input, priceBatch) {
    const priceResults = new Map(
      (priceBatch.result.results || []).map((result) => [result.query_id, result]),
    );
    const violations = [];
    for (const component of input.zero_cost_services || []) {
      const source = component.official_evidence?.source;
      const reference = String(component.official_evidence?.reference || '');
      if (FREE_ALLOWANCE_REFERENCE.test(reference)) {
        violations.push(`free_allowance_documentation_forbidden:${component.component_key}`);
      }
      if (source !== 'official_price_catalog') continue;

      const refs = evidenceReferences(component);
      if (refs.length === 0) {
        violations.push(`zero_cost_catalog_evidence_missing:${component.component_key}`);
        continue;
      }
      for (const ref of refs) {
        const result = priceResults.get(ref.query_id);
        if (!result) {
          violations.push(`unknown_zero_cost_price_query:${component.component_key}:${ref.query_id}`);
          continue;
        }
        if (result.provider !== input.cloud_provider) {
          violations.push(`zero_cost_price_provider_mismatch:${component.component_key}:${ref.query_id}`);
        }
        const availableItems = new Set(result.official_item_ids || []);
        const selectedItems = ref.official_item_ids || [];
        const rateCandidates = Array.isArray(result.official_rate_candidates)
          ? result.official_rate_candidates
          : [];
        const availableRates = new Map(rateCandidates.map((rate) => [rate.rate_id, rate]));
        const selectedRates = ref.official_rate_ids || [];
        if (selectedItems.length === 0 || selectedRates.length === 0) {
          violations.push(`zero_cost_rate_selection_required:${component.component_key}:${ref.query_id}`);
        }
        for (const itemId of selectedItems) {
          if (!availableItems.has(itemId)) {
            violations.push(`unknown_zero_cost_item:${component.component_key}:${ref.query_id}:${itemId}`);
            continue;
          }
          const itemRates = rateCandidates.filter((rate) => rate.official_item_id === itemId);
          if (itemRates.some((rate) => Number(rate.unit_price) > 0)) {
            violations.push(`free_allowance_not_zero_cost:${component.component_key}:${ref.query_id}:${itemId}`);
          }
          if (!itemRates.some((rate) => rate.is_zero_rate === true || Number(rate.unit_price) === 0)) {
            violations.push(`official_zero_rate_missing:${component.component_key}:${ref.query_id}:${itemId}`);
          }
        }
        for (const rateId of selectedRates) {
          const rate = availableRates.get(rateId);
          if (!rate) {
            violations.push(`unknown_zero_cost_rate:${component.component_key}:${ref.query_id}:${rateId}`);
            continue;
          }
          if (!selectedItems.includes(rate.official_item_id)) {
            violations.push(`zero_cost_rate_item_mismatch:${component.component_key}:${ref.query_id}:${rateId}`);
          }
          if (rate.is_zero_rate !== true && Number(rate.unit_price) !== 0) {
            violations.push(`positive_rate_in_zero_cost_service:${component.component_key}:${ref.query_id}:${rateId}`);
          }
        }
      }
    }
    if (violations.length > 0) {
      const error = new Error('Official zero-cost evidence is invalid for a commercial quote.');
      error.code = 'official_zero_cost_evidence_invalid';
      error.details = { violations };
      throw error;
    }
  }

  async deliverRecord(record) {
    let deliveryResult;
    if (typeof this.deliverer.createArtifact === 'function'
      && typeof this.deliverer.completeSalesPageDelivery === 'function') {
      const artifact = await this.deliverer.createArtifact(record);
      if (record.relay_job_id) {
        this.store.putCheckpoint(record.relay_job_id, {
          stage: 'artifacts_generated',
          quote_id: record.quote_id,
          price_batch_id: record.price_batch_id,
          spreadsheet_url: artifact.spreadsheet_url,
          spreadsheet_filename: artifact.spreadsheet_filename,
          partial_retry_generation: Number(record.partial_retry_generation || 0),
        });
      }
      deliveryResult = await this.deliverer.completeSalesPageDelivery(
        record,
        artifact,
        'displayed_on_page',
      );
    } else {
      deliveryResult = await this.deliverer.deliverPageResult(record);
    }
    this.store.update(record.quote_id, {
      delivery: {
        status: 'delivered',
        delivered_at: new Date().toISOString(),
        result: deliveryResult,
        partial_retry_generation: Number(record.partial_retry_generation || 0),
      },
    });
    if (record.relay_job_id) {
      this.store.putCheckpoint(record.relay_job_id, {
        stage: 'delivery_completed',
        quote_id: record.quote_id,
        price_batch_id: record.price_batch_id,
        result: deliveryResult,
      });
    }
    return {
      ...deliveryResult,
      status: 'displayed_on_page',
      quote_id: record.quote_id,
      next_step: 'The structured quote and Excel download link are ready on the sales page.',
    };
  }

  async buildEstimate(input) {
    let replay = (
      (input.relay_job_id && this.store.findByRelayJobId(input.relay_job_id))
      || this.store.findByIdempotencyKey(input.idempotency_key)
    );
    const relayRetryGeneration = input.relay_job_id
      ? Number(readRelayJob(input.relay_job_id).partial_retry_generation || 0)
      : 0;
    if (replay?.is_partial === true
      && relayRetryGeneration > Number(replay.partial_retry_generation || 0)) {
      replay = null;
    }
    if (replay) {
      if ((input.relay_job_id || null) !== (replay.relay_job_id || null)) {
        const error = new Error('The idempotency key belongs to a different sales quote task.');
        error.code = 'relay_idempotency_context_mismatch';
        throw error;
      }
      if (replay.delivery?.status === 'delivered') {
        return { ...replay.delivery.result, idempotent_replay: true };
      }
      return { ...(await this.deliverRecord(replay)), idempotent_replay: true };
    }

    const relayBoundInput = bindRelayJobContext(input);

    uniqueComponentKeys([
      ...(relayBoundInput.services || []),
      ...(relayBoundInput.zero_cost_services || []),
      ...(relayBoundInput.unpriced_services || []),
    ]);
    const priceBatch = this.store.getPriceBatch(relayBoundInput.price_batch_id);
    if (priceBatch.relay_job_id
      && priceBatch.relay_job_id !== (relayBoundInput.relay_job_id || null)) {
      const error = new Error('The saved price batch belongs to a different sales quote task.');
      error.code = 'price_batch_relay_context_mismatch';
      throw error;
    }
    const normalizedInput = validateScenarioSemantics(
      normalizeScenarioCosts(normalizeComponentCosts(relayBoundInput)),
    );
    const marketProfile = this.routeStore.marketProfile(normalizedInput.cloud_provider);
    const regionScopes = [
      { region: normalizedInput.default_region, scope: 'quote' },
      ...(normalizedInput.services || []).map((component) => ({
        region: component.region, scope: 'priced_component', component_key: component.component_key,
      })),
      ...(normalizedInput.zero_cost_services || []).map((component) => ({
        region: component.region, scope: 'zero_cost_component', component_key: component.component_key,
      })),
      ...(normalizedInput.unpriced_services || []).map((component) => ({
        region: component.region, scope: 'unpriced_component', component_key: component.component_key,
      })),
    ].filter((item) => item.region);
    const mismatchedScope = regionScopes.find((item) => providerRegionMismatch(
      normalizedInput.cloud_provider, item.region, marketProfile,
    ));
    const regionMismatch = mismatchedScope
      ? providerRegionMismatch(
        normalizedInput.cloud_provider, mismatchedScope.region, marketProfile,
      )
      : null;
    if (regionMismatch) {
      const error = new Error('The quote region code does not belong to the selected cloud provider.');
      error.code = 'quote_region_provider_mismatch';
      error.retryable = true;
      error.details = { ...regionMismatch, ...mismatchedScope };
      throw error;
    }
    validatePartialQuoteContract(normalizedInput, priceBatch);
    normalizedInput.unpriced_services = enrichUnpricedServices(normalizedInput, priceBatch);
    validateCustomerDocumentMetadata(normalizedInput);
    const pageEvidence = this.validatePriceEvidence(normalizedInput, priceBatch);
    this.validateZeroCostEvidence(normalizedInput, priceBatch);
    const compiled = prepareOfficialApiSubmission(normalizedInput);
    const cacheFallback = cachedOfficialPriceDisclosure(normalizedInput, priceBatch);
    const selectedQueryIds = new Set([
      ...(normalizedInput.services || []), ...(normalizedInput.zero_cost_services || []),
    ].flatMap((component) => [
      ...evidenceReferences(component),
      ...(component.scenario_costs || []).flatMap(evidenceReferences),
    ]).map((ref) => ref.query_id));
    const quoteId = `aqv2_${randomUUID()}`;
    const record = {
      schema_version: 'astraquote-v3-quote/1',
      quote_id: quoteId,
      idempotency_key: normalizedInput.idempotency_key,
      created_at: new Date().toISOString(),
      quote_name: normalizedInput.quote_name,
      submission_code: normalizedInput.submission_code || null,
      relay_job_id: normalizedInput.relay_job_id || null,
      price_batch_id: normalizedInput.price_batch_id,
      default_region: normalizedInput.default_region,
      preferred_region: normalizedInput.preferred_region || normalizedInput.default_region,
      region_adjustment_reason: normalizedInput.region_adjustment_reason || '',
      cloud_provider: normalizedInput.cloud_provider,
      market_profile: marketProfile,
      currency: normalizedInput.currency,
      display_result_on_page: true,
      is_partial: normalizedInput.is_partial === true,
      partial_retry_generation: relayRetryGeneration,
      fact_ledger: normalizedInput.fact_ledger,
      fact_coverage: compiled.fact_coverage,
      transformation_trace: compiled.transformation_trace,
      requirement_ir: normalizedInput.fact_ledger,
      resource_ir: normalizedInput.services,
      zero_cost_ir: normalizedInput.zero_cost_services || [],
      unpriced_ir: normalizedInput.unpriced_services || [],
      billing_usage_ir: compiled.billing_usage_ir,
      price_ir: [
        ...priceBatch.result.results.filter((result) => selectedQueryIds.has(result.query_id)),
        ...pageEvidence.acceptedEvidence,
      ],
      official_page_price_ir: pageEvidence.acceptedEvidence,
      expected_monthly_total: normalizedInput.expected_monthly_total,
      pricing_scenarios: normalizedInput.pricing_scenarios || [],
      assumptions: normalizedInput.assumptions || [],
      adjustments: normalizedInput.adjustments || [],
      verification: {
        status: normalizedInput.is_partial === true
          ? 'official_price_partial'
          : 'official_price_verified',
        verified_at: new Date().toISOString(),
        costs: officialCosts(normalizedInput),
        official_price_evidence_status: normalizedInput.is_partial === true
          ? 'partial'
          : pageEvidence.acceptedEvidence.length > 0
            ? 'official_page_fallback'
            : 'exact',
        source: pageEvidence.acceptedEvidence.length > 0
          ? 'Official cloud price API and pricing pages'
          : 'Official cloud price catalog',
        cache_fallback: cacheFallback.metadata,
        official_pricing_page_evidence: {
          used: pageEvidence.acceptedEvidence.length > 0,
          count: pageEvidence.acceptedEvidence.length,
          source_urls: [...new Set(
            pageEvidence.acceptedEvidence.map((evidence) => evidence.source_url),
          )],
        },
      },
      delivery: { status: 'pending' },
    };
    this.store.put(record);

    try {
      return await this.deliverRecord(record);
    } catch (deliveryError) {
      this.store.update(quoteId, {
        delivery: {
          status: 'failed',
          failed_at: new Date().toISOString(),
          error: {
            code: deliveryError?.code || 'quote_delivery_failed',
            message: deliveryError?.message || 'Quote delivery failed.',
          },
        },
      });
      if (record.relay_job_id) {
        const checkpoint = this.store.getCheckpoint(record.relay_job_id);
        this.store.putCheckpoint(record.relay_job_id, {
          stage: 'failed',
          failed_stage: checkpoint?.stage || 'estimate_validated',
          quote_id: record.quote_id,
          price_batch_id: record.price_batch_id,
          error: {
            code: deliveryError?.code || 'quote_delivery_failed',
            message: deliveryError?.message || 'Quote delivery failed.',
          },
        });
      }
      throw deliveryError;
    }
  }
}

module.exports = {
  AstraQuoteV2Workflow,
  bindRelayJobContext,
  normalizeComponentCosts,
  normalizeScenarioCosts,
  officialCosts,
  enrichUnpricedServices,
  prepareOfficialApiSubmission,
  validateCustomerDocumentMetadata,
  validateScenarioSemantics,
};
