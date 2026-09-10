'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');

const { V2QuoteStore } = require('./v2-quote-store');
const { QuoteDeliveryService } = require('./quote-delivery');

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
  'one_year_all_upfront',
  'three_year_all_upfront',
]);

function relayScenarioKeys(options) {
  const keys = [];
  if (options?.include_on_demand_scenario !== false) keys.push('on_demand');
  if (options?.pricing_mode === 'reserved' && options?.payment_option === 'all_upfront') {
    for (const years of options.reserved_term_years || []) {
      if (Number(years) === 1) keys.push('one_year_all_upfront');
      if (Number(years) === 3) keys.push('three_year_all_upfront');
    }
  }
  return [...new Set(keys)];
}

function bindRelayJobContext(input) {
  const relayJobId = String(input.relay_job_id || '');
  if (!relayJobId) return input;
  const relayDirectory = path.resolve(process.env.ASTRAQUOTE_GPT_RELAY_DIR || '/data/gpt-relay');
  const jobPath = path.join(relayDirectory, 'jobs', `${relayJobId}.json`);
  let job;
  try {
    job = JSON.parse(fs.readFileSync(jobPath, 'utf8'));
  } catch {
    const error = new Error('The sales quote task could not be verified.');
    error.code = 'relay_job_context_missing';
    error.details = { relay_job_id: relayJobId };
    throw error;
  }
  if (job.job_id !== relayJobId || job.status !== 'processing') {
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
  const expectedPageResult = job.quote_options?.display_result_on_page === true;
  const receivedPageResult = input.display_result_on_page === undefined
    ? expectedPageResult
    : input.display_result_on_page === true;
  if (receivedPageResult !== expectedPageResult) {
    const error = new Error('The page result option does not match the sales submission.');
    error.code = 'relay_page_result_option_mismatch';
    error.details = {
      relay_job_id: relayJobId,
      expected: expectedPageResult,
      received: receivedPageResult,
    };
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
  return {
    ...input,
    submission_code: submissionCode,
    display_result_on_page: receivedPageResult,
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

function officialCosts(input) {
  const monthly = Number(input.expected_monthly_total);
  const services = input.services || [];
  const zeroCostServices = input.zero_cost_services || [];
  return {
    upfront: 0,
    monthly,
    total_12_months: monthly * 12,
    currency: input.currency || 'USD',
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
  }) {
    this.backend = backend;
    this.store = store;
    this.deliverer = deliverer;
  }

  describeService(input) {
    return this.backend.describeService(input);
  }

  getAttributeValues(input) {
    return this.backend.getAttributeValues(input);
  }

  getPrices(input) {
    return this.backend.getPrices(input).then((result) => {
      const priceBatchId = `aqpb_${randomUUID()}`;
      this.store.putPriceBatch({
        schema_version: 'astraquote-v3-price-batch/1',
        price_batch_id: priceBatchId,
        created_at: new Date().toISOString(),
        request: input,
        result,
      });
      return {
        ...result,
        price_batch_id: priceBatchId,
      };
    });
  }

  validatePriceEvidence(input, priceBatch) {
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
        if (result.status === 'ambiguous' && selected.length === 0) {
          violations.push(`official_item_selection_required:${component.component_key}:${ref.query_id}`);
        }
        for (const itemId of selected) {
          if (!available.has(itemId)) {
            violations.push(`unknown_official_item:${component.component_key}:${ref.query_id}:${itemId}`);
          }
        }
      }
      for (const scenario of component.scenario_costs || []) {
        const results = evidenceReferences(scenario)
          .map((ref) => priceResults.get(ref.query_id))
          .filter(Boolean);
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
          one_year_all_upfront: 1,
          three_year_all_upfront: 3,
        }[scenario.scenario_key];
        if (scenario.pricing_basis === 'reserved'
          && !results.some((result) => (
            result.pricing_model === 'reserved'
            && Number(result.term_years) === expectedTermYears
            && result.payment_option === 'all_upfront'
          ))) {
          violations.push(`scenario_commitment_terms_mismatch:${component.component_key}:${scenario.scenario_key}`);
        }
        if (scenario.pricing_basis === 'on_demand_fallback') {
          const onDemandCost = (component.scenario_costs || []).find(
            (cost) => cost.scenario_key === 'on_demand',
          );
          if (onDemandCost
            && roundedCents(onDemandCost.monthly_cost) !== roundedCents(scenario.monthly_cost)) {
            violations.push(`scenario_fallback_cost_changed:${component.component_key}:${scenario.scenario_key}`);
          }
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

  async deliverRecord(record) {
    const pageOnly = record.display_result_on_page === true;
    const deliveryResult = pageOnly
      ? await this.deliverer.deliverPageResult(record)
      : await this.deliverer.deliver(record);
    this.store.update(record.quote_id, {
      delivery: {
        status: 'delivered',
        delivered_at: new Date().toISOString(),
        result: deliveryResult,
      },
    });
    return {
      ...deliveryResult,
      status: pageOnly ? 'displayed_on_page' : 'delivered',
      quote_id: record.quote_id,
      next_step: pageOnly
        ? 'The structured quote is ready on the sales page.'
        : 'The Excel quote has been delivered to the configured WebHook.',
    };
  }

  async buildEstimate(input) {
    const relayBoundInput = bindRelayJobContext(input);
    const replay = this.store.findByIdempotencyKey(relayBoundInput.idempotency_key);
    if (replay) {
      if ((relayBoundInput.relay_job_id || null) !== (replay.relay_job_id || null)) {
        const error = new Error('The idempotency key belongs to a different sales quote task.');
        error.code = 'relay_idempotency_context_mismatch';
        throw error;
      }
      if (replay.delivery?.status === 'delivered') {
        return { ...replay.delivery.result, idempotent_replay: true };
      }
      return { ...(await this.deliverRecord(replay)), idempotent_replay: true };
    }

    uniqueComponentKeys([
      ...(relayBoundInput.services || []),
      ...(relayBoundInput.zero_cost_services || []),
    ]);
    const priceBatch = this.store.getPriceBatch(relayBoundInput.price_batch_id);
    const normalizedInput = normalizeScenarioCosts(normalizeComponentCosts(relayBoundInput));
    validateCustomerDocumentMetadata(normalizedInput);
    this.validatePriceEvidence(normalizedInput, priceBatch);
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
      default_region: normalizedInput.default_region,
      cloud_provider: normalizedInput.cloud_provider,
      currency: normalizedInput.currency,
      display_result_on_page: normalizedInput.display_result_on_page === true,
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
};
