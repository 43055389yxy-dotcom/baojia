'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  quoteWorkflowPolicy,
  renderWorkflowPolicySlice,
  workflowPolicySnapshot,
  workflowPolicyValue,
  workflowPolicyVersion,
} = require('../lib/quote-workflow-policy');

test('workflow policy is one versioned machine-readable source', () => {
  const policy = quoteWorkflowPolicy();
  const snapshot = workflowPolicySnapshot();

  assert.equal(policy.schema_version, 'astraquote-quote-workflow-policy/1');
  assert.equal(workflowPolicyVersion(), '2026-09-14-aws-local-routes-v1');
  assert.equal(snapshot.policy_version, workflowPolicyVersion());
  assert.equal(snapshot.prompt_directives, undefined);
  assert.equal(snapshot.consumer_slices, undefined);
});

test('workflow policy projects a small stage-specific prompt instead of the whole library', () => {
  const quoteContext = renderWorkflowPolicySlice('quote_context');
  const componentBatch = renderWorkflowPolicySlice('component_batch');

  assert.match(quoteContext, /官方价格 API/);
  assert.match(quoteContext, /on_demand_fallback/);
  assert.doesNotMatch(quoteContext, /回到原对话/);
  assert.match(componentBatch, /save_component_batch/);
  assert.match(componentBatch, /逐个组件/);
  assert.doesNotMatch(componentBatch, /官方价格 API/);
});

test('workflow policy shares batch and recovery limits with MCP', () => {
  assert.equal(workflowPolicyValue('batching', 'components_per_wave'), 5);
  assert.equal(workflowPolicyValue('batching', 'waves_per_chat'), 2);
  assert.equal(workflowPolicyValue('batching', 'max_active_chats_per_sales_job'), 3);
  assert.equal(workflowPolicyValue('recovery', 'deferred_retry_rounds'), 1);
  assert.equal(workflowPolicyValue(
    'pricing', 'official_api_network_attempt_limit_per_scope',
  ), 2);
  assert.equal(workflowPolicyValue('pricing', 'prefer_verified_local_aws_pricing_routes'), true);
  assert.equal(workflowPolicyValue('pricing', 'local_aws_route_failure_is_component_scoped'), true);
  assert.equal(workflowPolicyValue('pricing', 'local_aws_route_fallback_to_official_page'), true);
  assert.equal(
    workflowPolicyValue('pricing', 'official_api_network_attempt_limit_per_scope'),
    1 + workflowPolicyValue('pricing', 'corrected_api_attempt_limit_per_scope'),
  );
  assert.ok(
    workflowPolicyValue('batching', 'max_deferred_components_per_retry')
      <= workflowPolicyValue('batching', 'components_per_wave')
        * workflowPolicyValue('batching', 'waves_per_chat'),
  );
});
