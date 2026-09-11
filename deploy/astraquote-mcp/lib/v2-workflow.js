'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { isDeepStrictEqual } = require('node:util');

const { V2QuoteStore } = require('./v2-quote-store');
const { QuoteDeliveryService } = require('./quote-delivery');
const { PricingRouteStore } = require('./pricing-route-store');
const { canFinalizeRelayJob } = require('./relay-job-state');

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

function validateCustomerDocumentMetadata(input) {
  const componentKeys = new Set([
    ...(input.services || []).map((entry) => entry.component_key),
    ...(input.zero_cost_services || []).map((entry) => entry.component_key),
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
  'one_year_commitment',
  'three_year_commitment',
]);

const CREATED_PRICE_NEXT_ACTION = [
  'Execute now: derive a non-empty queries array from the normalized component configuration',
  'and current get_prices schema, then call get_prices immediately.',
  'Do not describe or list the remaining steps and do not wait for another turn.',
  'Correct any caller-input validation error and retry in this response.',
].join(' ');

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
    source: 'Official cloud price catalog',
  };
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

function prepareOfficialApiSubmission(input) {
  const facts = new Map();
  const violations = [];
  for (const fact of input.fact_ledger || []) {
    if (facts.has(fact.fact_id)) violations.push(`duplicate_fact_id:${fact.fact_id}`);
    facts.set(fact.fact_id, fact);
  }
  const coverage = {};
  const consumed = new Set();
  const register = (factId, component, coverageType, priceQueryIds = []) => {
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
    }];
  };

  for (const component of input.services || []) {
    const componentQueryIds = evidenceReferences(component).map((ref) => ref.query_id);
    for (const factId of component.fact_ids || []) {
      const fact = facts.get(factId);
      if (fact?.disposition === 'zero_cost') {
        violations.push(`zero_cost_fact_in_priced_service:${component.component_key}:${factId}`);
        continue;
      }
      register(factId, component, 'official_price', componentQueryIds);
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
  for (const fact of facts.values()) {
    if (fact.disposition !== 'context' && !consumed.has(fact.fact_id)) {
      violations.push(`unconsumed_fact:${fact.fact_id}`);
    }
  }

  const resourceComponents = new Set([
    ...(input.services || []).map((item) => item.component_key),
    ...(input.zero_cost_services || []).map((item) => item.component_key),
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
    const error = new Error('Official API quote facts do not map cleanly to priced resources.');
    error.code = 'official_api_fact_mapping_invalid';
    error.details = { violations };
    throw error;
  }

  return {
    fact_coverage: coverage,
    transformation_trace: (input.services || []).map((component) => ({
      component_key: component.component_key,
      source_fact_ids: component.fact_ids || [],
      price_evidence: evidenceReferences(component),
      transformation: 'gpt_selected_official_price_identity',
    })),
    billing_usage_ir: (input.services || []).map((component) => ({
      component_key: component.component_key,
      source_fact_ids: component.fact_ids || [],
      price_evidence: evidenceReferences(component),
      expected_monthly_cost: component.expected_monthly_cost,
      scenario_costs: component.scenario_costs || [],
    })),
  };
}

class AstraQuoteV2Workflow {
  constructor({
    backend,
    store = new V2QuoteStore(),
    deliverer = new QuoteDeliveryService(),
    routeStore,
  }) {
    this.backend = backend;
    this.store = store;
    this.deliverer = deliverer;
    this.routeStore = routeStore || new PricingRouteStore({
      directory: path.join(this.store.directory, 'pricing-routes'),
    });
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
    return {
      status: priceBatch.result?.status || 'needs_refinement',
      price_batch_id: priceBatch.price_batch_id,
      result_count: input.query_ids.length,
      batch_result_count: byId.size,
      results: input.query_ids.map((queryId) => byId.get(queryId)),
    };
  }

  async getPrices(input) {
    const relayJobId = input.relay_job_id || null;
    if (relayJobId) assertRelayIdentity(input);
    const materializedQueries = input.queries.map((query) => this.routeStore.materialize(query));
    const queryIds = materializedQueries.map((query) => query.query_id);
    if (new Set(queryIds).size !== queryIds.length) {
      const error = new Error('Every official price query must have a unique query_id.');
      error.code = 'duplicate_price_query_id';
      throw error;
    }
    const existing = input.price_batch_id
      ? this.store.getPriceBatch(input.price_batch_id)
      : null;
    if (existing && (existing.relay_job_id || null) !== relayJobId) {
      const error = new Error('The saved price batch belongs to a different sales quote task.');
      error.code = 'price_batch_relay_context_mismatch';
      throw error;
    }

    const existingResults = new Map(
      (existing?.result?.results || []).map((item) => [item.query_id, item]),
    );
    const existingQueries = new Map(
      (existing?.request?.queries || []).map((item) => [item.query_id, item]),
    );
    const pendingQueries = [];
    for (const query of materializedQueries) {
      const priorResult = existingResults.get(query.query_id);
      if (priorResult && ['exact', 'ambiguous'].includes(priorResult.status)) {
        if (!isDeepStrictEqual(existingQueries.get(query.query_id), query)) {
          const error = new Error('A completed query_id cannot be reused for different filters.');
          error.code = 'price_query_identity_conflict';
          error.details = { query_id: query.query_id };
          throw error;
        }
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
    const learnedRoutes = this.routeStore.recordBatch(pendingQueries, result.results || []);
    const priceBatchId = existing?.price_batch_id || `aqpb_${randomUUID()}`;
    const mergedResults = new Map(existingResults);
    for (const item of result.results || []) mergedResults.set(item.query_id, item);
    const mergedQueries = new Map(existingQueries);
    for (const query of materializedQueries) mergedQueries.set(query.query_id, query);
    const incompleteQueryIds = [...mergedQueries.keys()].filter((queryId) => {
      const item = mergedResults.get(queryId);
      return !item || !['exact', 'ambiguous'].includes(item.status);
    });
    const completed = incompleteQueryIds.length === 0;
    const mergedResult = {
      status: completed ? 'completed' : 'needs_refinement',
      terminal: completed,
      next_action: completed ? 'build_estimate' : 'refine_incomplete_queries',
      result_count: mergedResults.size,
      results: [...mergedResults.values()],
    };
    this.store.putPriceBatch({
      schema_version: 'astraquote-v3-price-batch/1',
      price_batch_id: priceBatchId,
      created_at: existing?.created_at || new Date().toISOString(),
      updated_at: new Date().toISOString(),
      relay_job_id: relayJobId,
      request: { queries: [...mergedQueries.values()] },
      result: mergedResult,
    });
    if (relayJobId) {
      this.store.putCheckpoint(relayJobId, {
        stage: completed ? 'pricing_completed' : 'pricing_partial',
        price_batch_id: priceBatchId,
        incomplete_query_ids: incompleteQueryIds,
      });
    }
    return {
      status: mergedResult.status,
      terminal: mergedResult.terminal,
      next_action: mergedResult.next_action,
      result_count: (result.results || []).length,
      batch_result_count: mergedResults.size,
      results: result.results || [],
      incomplete_query_ids: incompleteQueryIds,
      price_batch_id: priceBatchId,
      learned_routes: learnedRoutes,
      resumed_batch: Boolean(existing),
      reused_query_ids: materializedQueries
        .filter((query) => !pendingQueries.includes(query))
        .map((query) => query.query_id),
      queried_query_ids: pendingQueries.map((query) => query.query_id),
    };
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
    return {
      relay_job_id: input.relay_job_id,
      relay_status: job.status,
      stage: checkpoint.stage,
      price_batch_id: checkpoint.price_batch_id || null,
      incomplete_query_ids: checkpoint.incomplete_query_ids || [],
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
      pricing_partial: 'Query only incomplete_query_ids; keep the saved price batch.',
      pricing_completed: 'Reuse price_batch_id and continue with build_estimate.',
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

  validatePriceEvidence(input, priceBatch) {
    const signedCatalogProviders = new Set([
      'tencent', 'alibaba', 'huawei', 'baidu', 'volcengine', 'ctyun',
    ]);
    const priceResults = new Map(
      (priceBatch.result.results || []).map((result) => [result.query_id, result]),
    );
    const violations = [];
    for (const component of input.services) {
      const refs = [
        ...evidenceReferences(component),
        ...(component.scenario_costs || []).flatMap((scenario) => evidenceReferences(scenario)),
      ];
      if (refs.length === 0) violations.push(`price_evidence_missing:${component.component_key}`);
      for (const ref of refs) {
        const result = priceResults.get(ref.query_id);
        if (!result) {
          violations.push(`unknown_price_query:${component.component_key}:${ref.query_id}`);
          continue;
        }
        if (result.provider !== input.cloud_provider) {
          violations.push(`price_provider_mismatch:${component.component_key}:${ref.query_id}`);
        }
        if (!['exact', 'ambiguous'].includes(result.status)) {
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
          && !results.some((result) => result.pricing_model === 'reserved')) {
          violations.push(`scenario_committed_evidence_missing:${component.component_key}:${scenario.scenario_key}`);
        }
        const expectedTermYears = {
          one_year_commitment: 1,
          three_year_commitment: 3,
        }[scenario.scenario_key];
        if (scenario.pricing_basis === 'reserved'
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
      error.details = { violations };
      throw error;
    }
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
    const replay = (
      (input.relay_job_id && this.store.findByRelayJobId(input.relay_job_id))
      || this.store.findByIdempotencyKey(input.idempotency_key)
    );
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
    validateCustomerDocumentMetadata(normalizedInput);
    this.validatePriceEvidence(normalizedInput, priceBatch);
    this.validateZeroCostEvidence(normalizedInput, priceBatch);
    const compiled = prepareOfficialApiSubmission(normalizedInput);
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
      currency: normalizedInput.currency,
      display_result_on_page: true,
      fact_ledger: normalizedInput.fact_ledger,
      fact_coverage: compiled.fact_coverage,
      transformation_trace: compiled.transformation_trace,
      requirement_ir: normalizedInput.fact_ledger,
      resource_ir: normalizedInput.services,
      zero_cost_ir: normalizedInput.zero_cost_services || [],
      billing_usage_ir: compiled.billing_usage_ir,
      price_ir: priceBatch.result.results,
      expected_monthly_total: normalizedInput.expected_monthly_total,
      pricing_scenarios: normalizedInput.pricing_scenarios || [],
      assumptions: normalizedInput.assumptions || [],
      adjustments: normalizedInput.adjustments || [],
      verification: {
        status: 'official_price_verified',
        verified_at: new Date().toISOString(),
        costs: officialCosts(normalizedInput),
        official_price_evidence_status: 'exact',
        source: 'Official cloud price catalog',
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
  prepareOfficialApiSubmission,
  validateCustomerDocumentMetadata,
  validateScenarioSemantics,
};
