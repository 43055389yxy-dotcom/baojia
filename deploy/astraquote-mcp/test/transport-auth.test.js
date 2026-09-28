'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { validMcpBearer } = require('../server');

test('internal MCP transport requires the exact bearer token', () => {
  assert.equal(validMcpBearer({}, 'secret'), false);
  assert.equal(validMcpBearer({ authorization: 'Bearer wrong' }, 'secret'), false);
  assert.equal(validMcpBearer({ authorization: 'Basic secret' }, 'secret'), false);
  assert.equal(validMcpBearer({ authorization: 'Bearer secret' }, 'secret'), true);
  assert.equal(validMcpBearer({ authorization: 'Bearer secret' }, ''), false);
});
