'use strict';

const PROGRESS_GUIDANCE = 'Query progress is not quote coverage. Discovery and superseded attempts are not missing components. Once every required component and purchase option has verified evidence, call build_estimate with the selected query IDs even if unused legacy attempts remain incomplete.';

function invalid(reason, queryId) {
  const error = new Error('Correct the query context or replacement relationship and retry.');
  error.code = 'price_query_context_invalid';
  error.retryable = true;
  error.details = { reason, query_id: queryId };
  throw error;
}

function scope(context) {
  return context?.purpose === 'pricing' && context.component_key && context.billing_key
    ? JSON.stringify([context.component_key, context.billing_key, context.scenario_key || null])
    : null;
}

function pricingScopeKey(componentKey, billingKey, scenarioKey = null) {
  return componentKey && billingKey
    ? JSON.stringify([componentKey, billingKey, scenarioKey || null])
    : null;
}

function usable(result) {
  return ['exact', 'ambiguous'].includes(result?.status)
    && Array.isArray(result.official_item_ids) && result.official_item_ids.length > 0
    && !(Array.isArray(result.items) ? result.items : []).some((item) => {
      const error = item?.Error || item?.error || item?.Response?.Error;
      return error && typeof error === 'object' && (error.Code || error.code)
        && (error.Message || error.message);
    });
}

function usableCommercialRates(result) {
  const candidates = Array.isArray(result?.official_rate_candidates)
    ? result.official_rate_candidates
    : [];
  const itemIds = new Set(candidates.map((rate) => String(rate?.official_item_id || ''))
    .filter(Boolean));
  const positiveItems = new Set(candidates.filter((rate) => (
    Number(rate?.unit_price) > 0
      && /^[A-Z]{3}$/.test(String(rate?.currency || '').trim())
      && String(rate?.rate_id || '').trim()
      && String(rate?.official_item_id || '').trim()
      && !/(?:configured[- ]?module|usage[- ]?period)/i.test(String(rate?.unit || ''))
  )).map((rate) => String(rate.official_item_id)));
  // Signed billing APIs often return zero-valued placeholder modules beside
  // the real charge.  Treating that mixed envelope as complete caused quotes
  // to omit compute while keeping only disk.  Every returned billing identity
  // must therefore expose a positive commercial rate before the scope closes.
  return positiveItems.size > 0
    && [...itemIds].every((itemId) => positiveItems.has(itemId));
}

function reusableQueryResult(result, context) {
  // Legacy public clients did not submit query contexts or flattened rates.
  // Preserve their valid catalog results, but never a known API error envelope.
  return usable(result) && (!scope(context)
    || usableCommercialRates(result));
}

// Context is task metadata and never an official API parameter.
// GPT supplies ownership and purpose; no inference from query IDs/product names.
function mergeQueryContexts(existing, updates, queries) {
  const contexts = new Map((existing || []).map((item) => [item.query_id, { ...item }]));
  const seen = new Set();
  for (const update of updates || []) {
    const id = update.query_id;
    if (seen.has(id) || !queries.has(id)) invalid('duplicate_or_unknown_query', id);
    seen.add(id);
    if (!['discovery', 'pricing'].includes(update.purpose)) invalid('invalid_purpose', id);
    if (update.purpose === 'pricing' && !scope(update)) invalid('pricing_scope_required', id);
    if (update.purpose === 'discovery'
      && (update.component_key || update.billing_key || update.scenario_key
        || update.supersedes_query_ids?.length)) invalid('discovery_has_no_billing_scope', id);
    const prior = contexts.get(id);
    if (prior?.purpose === 'pricing'
      && (prior.purpose !== update.purpose || scope(prior) !== scope(update))) {
      invalid('query_scope_is_immutable', id);
    }
    contexts.set(id, {
      ...update,
      supersedes_query_ids: [...new Set([
        ...(prior?.supersedes_query_ids || []), ...(update.supersedes_query_ids || []),
      ])],
    });
  }
  for (const current of contexts.values()) {
    for (const oldId of current.supersedes_query_ids || []) {
      if (oldId === current.query_id || !queries.has(oldId)) invalid('invalid_predecessor', current.query_id);
      const old = contexts.get(oldId);
      if (queries.get(oldId).provider !== queries.get(current.query_id).provider
        || (old && scope(old) !== scope(current))) invalid('replacement_scope_mismatch', current.query_id);
      // Explicitly adopt an unscoped legacy attempt. Never guess its owner.
      if (!old) contexts.set(oldId, {
        query_id: oldId, purpose: 'pricing', component_key: current.component_key,
        billing_key: current.billing_key, ...(current.scenario_key ? { scenario_key: current.scenario_key } : {}),
        supersedes_query_ids: [],
      });
    }
  }
  const visiting = new Set();
  const visited = new Set();
  function visit(id) {
    if (visiting.has(id)) invalid('replacement_cycle', id);
    if (visited.has(id)) return;
    visiting.add(id);
    for (const old of contexts.get(id)?.supersedes_query_ids || []) visit(old);
    visiting.delete(id);
    visited.add(id);
  }
  for (const id of contexts.keys()) visit(id);
  return [...contexts.values()];
}

function queryProgress(queries, results, contexts = [], officialPageEvidence = []) {
  const byContext = new Map(contexts.map((item) => [item.query_id, item]));
  const byResult = new Map(results.map((item) => [item.query_id, item]));
  const byQuery = new Map(queries.map((item) => [item.query_id, item]));
  const positions = new Map(queries.map((item, index) => [item.query_id, index]));
  const officialPageScopes = new Set((officialPageEvidence || []).map((item) => (
    pricingScopeKey(item.component_key, item.billing_key, item.scenario_key || null)
  )).filter(Boolean));
  const successes = new Map();
  function key(id) {
    const ownedScope = scope(byContext.get(id));
    return ownedScope ? JSON.stringify([byQuery.get(id)?.provider, ownedScope]) : null;
  }
  for (const q of queries) {
    // A directory envelope with an item ID but no rate must not close pricing.
    if (key(q.query_id) && reusableQueryResult(
      byResult.get(q.query_id), byContext.get(q.query_id),
    )) {
      successes.set(key(q.query_id), q.query_id);
    }
  }
  const lifecycle = queries.map((q) => {
    const context = byContext.get(q.query_id);
    const result = byResult.get(q.query_id);
    const purpose = context?.purpose || 'pricing';
    const completed = reusableQueryResult(result, context);
    const completedByOfficialPage = !completed && officialPageScopes.has(scope(context));
    const candidate = !completed && key(q.query_id) ? successes.get(key(q.query_id)) : null;
    const successor = candidate && (
      positions.get(candidate) > positions.get(q.query_id)
      || byContext.get(candidate)?.supersedes_query_ids?.includes(q.query_id)
    ) ? candidate : null;
    return {
      query_id: q.query_id, purpose,
      ...(context || {}),
      state: purpose === 'discovery'
        ? 'discovery'
        : successor
          ? 'superseded'
          : completed
            ? 'completed'
            : completedByOfficialPage
              ? 'completed_by_official_page'
              : 'pending',
      ...(successor ? { superseded_by: successor } : {}),
    };
  });
  const pending = lifecycle.filter((item) => item.state === 'pending');
  const priced = lifecycle.filter((item) => (
    item.state === 'completed' || item.state === 'completed_by_official_page'
  ));
  const completed = priced.length > 0 && pending.length === 0;
  return {
    status: completed ? 'completed' : 'needs_refinement',
    terminal: completed,
    quote_terminal: pending.length > 0 && pending.every((item) => {
      const result = byResult.get(item.query_id);
      return Boolean(scope(byContext.get(item.query_id)))
        && result?.status === 'query_failed' && result.terminal === true && result.retryable !== true;
    }),
    next_action: completed ? 'build_estimate' : pending.length ? 'refine_incomplete_queries' : 'query_component_prices',
    progress_guidance: PROGRESS_GUIDANCE,
    incomplete_query_ids: pending.map((item) => item.query_id),
    superseded_query_ids: lifecycle.filter((item) => item.state === 'superseded').map((item) => item.query_id),
    discovery_query_count: lifecycle.filter((item) => item.state === 'discovery').length,
    batch_query_count: queries.length,
    completed_query_count: priced.length,
    incomplete_query_count: pending.length,
    query_lifecycle: lifecycle,
  };
}

function componentProgress(components = [], lifecycle = [], results = []) {
  const resultById = new Map(results.map((item) => [item.query_id, item]));
  const lifecycleByScope = new Map();
  for (const item of lifecycle) {
    const ownedScope = scope(item);
    if (!ownedScope) continue;
    const key = JSON.stringify([
      item.component_key,
      item.billing_key,
      item.scenario_key || null,
    ]);
    const entries = lifecycleByScope.get(key) || [];
    entries.push(item);
    lifecycleByScope.set(key, entries);
  }

  const states = components.map((component) => {
    const scopeStates = (component.billing_scopes || []).map((billing) => {
      const key = JSON.stringify([
        component.component_key,
        billing.billing_key,
        billing.scenario_key || null,
      ]);
      const attempts = lifecycleByScope.get(key) || [];
      if (attempts.some((item) => (
        item.state === 'completed' || item.state === 'completed_by_official_page'
      ))) return 'completed';
      const terminalFailures = attempts.filter((item) => {
        const result = resultById.get(item.query_id);
        return item.state === 'pending'
          && result?.status === 'query_failed'
          && result.terminal === true
          && result.retryable !== true;
      });
      if (attempts.length > 0 && terminalFailures.length === attempts.length) return 'failed';
      return 'pending';
    });
    const state = scopeStates.length > 0 && scopeStates.every((item) => item === 'completed')
      ? 'completed'
      : scopeStates.some((item) => item === 'failed')
        ? 'failed'
        : 'pending';
    return { component_key: component.component_key, state };
  });

  const topLevelComponentCount = components.filter(
    (component) => !component.parent_component_key,
  ).length;
  return {
    top_level_component_count: topLevelComponentCount,
    component_chat_count: Math.ceil(topLevelComponentCount / 10),
    total_component_count: states.length,
    completed_component_count: states.filter((item) => item.state === 'completed').length,
    failed_component_count: states.filter((item) => item.state === 'failed').length,
    pending_component_count: states.filter((item) => item.state === 'pending').length,
    component_lifecycle: states,
  };
}

function assertQueryIdentity(existingQueries, queries) {
  for (const query of queries) {
    const prior = existingQueries.get(query.query_id);
    if (prior && prior.provider !== query.provider) invalid('query_provider_is_immutable', query.query_id);
  }
}

function contextEvidenceViolations(input, batch, refsOf, { additionalCoveredScopes = [] } = {}) {
  const contexts = batch.query_contexts || [];
  const contextMap = new Map(contexts.map((item) => [item.query_id, item]));
  const violations = [];
  const covered = new Set(additionalCoveredScopes);
  const zeroKeys = new Set((input.zero_cost_services || []).map((item) => item.component_key));
  const unpricedKeys = new Set(input.is_partial === true
    ? (input.unpriced_services || []).map((item) => item.component_key) : []);
  for (const component of input.services || []) {
    const groups = [
      { scenario_key: null, refs: refsOf(component) },
      ...(component.scenario_costs || []).map((scenario) => ({
        scenario_key: scenario.scenario_key, pricing_basis: scenario.pricing_basis, refs: refsOf(scenario),
      })),
    ];
    for (const group of groups) {
      for (const ref of group.refs) {
        const context = contextMap.get(ref.query_id);
        if (!scope(context)) continue;
        if (context.component_key !== component.component_key) {
          violations.push(`price_query_component_mismatch:${component.component_key}:${ref.query_id}`);
        } else if (!context.scenario_key || context.scenario_key === group.scenario_key) {
          covered.add(scope(context));
        }
        if (context.component_key === component.component_key
          && group.pricing_basis === 'on_demand_fallback'
          && (!context.scenario_key || context.scenario_key === 'on_demand')) {
          covered.add(scope({ ...context, scenario_key: group.scenario_key }));
        }
      }
    }
  }
  const missing = new Map();
  const requiredScopes = contexts.concat((batch.quote_components || []).flatMap((component) => (
    (component.billing_scopes || []).map((billing) => ({
      purpose: 'pricing', component_key: component.component_key, ...billing,
    }))
  )));
  for (const context of requiredScopes) {
    if (scope(context) && !covered.has(scope(context)) && !zeroKeys.has(context.component_key)
      && !unpricedKeys.has(context.component_key)) {
      missing.set(scope(context), context);
    }
  }
  for (const context of missing.values()) {
    violations.push(`billing_query_evidence_missing:${context.component_key}:${context.billing_key}:${context.scenario_key || 'base'}`);
  }
  return violations;
}

module.exports = {
  PROGRESS_GUIDANCE, mergeQueryContexts, queryProgress, componentProgress,
  assertQueryIdentity, contextEvidenceViolations, pricingScopeKey, reusableQueryResult,
  usableCommercialRates,
};
