'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

test('production runs one single-MCP container without a sales or backend server', () => {
  const compose = fs.readFileSync(path.resolve(__dirname, '..', '..', 'compose.production.yml'), 'utf8');
  const serviceNames = [...compose.matchAll(/^  ([a-zA-Z0-9_-]+):$/gm)]
    .map((match) => match[1])
    .filter((name) => name !== 'caddy-net');
  assert.deepEqual(serviceNames, ['astraquote']);
  assert.match(compose, /ASTRAQUOTE_MCP_PORT:\s*"8200"/);
  assert.match(compose, /OAUTH_PORT:\s*"8001"/);
  assert.match(compose, /ASTRAQUOTE_MCP_RESULT_MAX_BYTES:\s*"131072"/);
  assert.match(compose, /ASTRAQUOTE_LOCAL_MODE:\s*"1"/);
  assert.match(compose, /ASTRAQUOTE_LOCAL_BRIDGE:\s*\/app\/backend\/scripts\/local_mcp_bridge\.py/);
  assert.match(compose, /ASTRAQUOTE_DELIVERY_MODE:\s*local/);
  assert.match(compose, /ASTRAQUOTE_DOWNLOAD_DIR:\s*\/data\/downloads/);
  assert.match(compose, /PUBLIC_ORIGIN:\s*https:\/\/baojia\.tontianit\.com/);
  assert.match(compose, /DB_PATH:\s*\/data\/oauth\/oauth\.db/);
  assert.match(compose, /\/home\/ec2-user\/astraquote\/data:\/data/);
  assert.doesNotMatch(compose, /CALCULATOR|generate_calculator_link/i);
  assert.doesNotMatch(compose, /ASTRAQUOTE_BACKEND_URL|BACKEND_API_URL|GPT_RELAY/);

  const start = fs.readFileSync(path.resolve(__dirname, '..', '..', 'start-production.sh'), 'utf8');
  assert.match(start, /MCP_PID/);
  assert.match(start, /OAUTH_PID/);
  assert.doesNotMatch(start, /BACKEND_PID|FRONTEND_PID/);
  assert.doesNotMatch(start, /CALCULATOR_PID|pricing-calculator/i);

  const dockerfile = fs.readFileSync(path.resolve(__dirname, '..', '..', 'Dockerfile'), 'utf8');
  assert.match(dockerfile, /local_mcp_bridge\.py/);
  assert.doesNotMatch(dockerfile, /frontend-builder|npm run build/);
  assert.doesNotMatch(dockerfile, /chromium|playwright|pricing-calculator|calculator-client/i);
  const mcpPackage = fs.readFileSync(path.resolve(__dirname, '..', 'package.json'), 'utf8');
  assert.doesNotMatch(mcpPackage, /playwright/);
});
