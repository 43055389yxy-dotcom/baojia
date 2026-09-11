'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { AstraQuoteBackendClient } = require('../lib/backend-client');

test('preserves field-level backend validation errors as retryable guidance', async () => {
  const client = new AstraQuoteBackendClient({
    token: 'test-token',
    fetchImpl: async () => new Response(JSON.stringify({
      code: 'request_schema_invalid',
      message: 'Correct the request fields.',
      details: {
        violations: [{
          path: 'body.queries.0.currency_code',
          message: 'Field required',
          type: 'missing',
        }],
      },
      retryable: true,
    }), {
      status: 422,
      headers: { 'Content-Type': 'application/json' },
    }),
  });

  await assert.rejects(
    client.getPrices({ queries: [] }),
    (error) => error.code === 'request_schema_invalid'
      && error.retryable === true
      && error.details.violations[0].path.endsWith('currency_code'),
  );
});

test('converts FastAPI validation detail into actionable field violations', async () => {
  const client = new AstraQuoteBackendClient({
    token: 'test-token',
    fetchImpl: async () => new Response(JSON.stringify({
      detail: [{ loc: ['body', 'queries', 0, 'region_parameter'], msg: 'Invalid value', type: 'value_error' }],
    }), {
      status: 422,
      headers: { 'Content-Type': 'application/json' },
    }),
  });

  await assert.rejects(
    client.getPrices({ queries: [] }),
    (error) => error.code === 'backend_request_schema_invalid'
      && error.retryable === true
      && error.details.violations[0].path === 'body.queries.0.region_parameter',
  );
});
