'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {
  officialApiBaseRoute,
  withOfficialApiBaseRoute,
} = require('../lib/official-api-base-routes');

test('basic official API routes replace a caller-guessed host without fixing request details', () => {
  const query = withOfficialApiBaseRoute({
    provider: 'ctyun', service: 'ecs', region: '200000001790',
    endpoint: 'ctapi.ctyun.cn', path: '/v4/order/new-query-price',
    body: { flavorId: 's7.large.4' },
  });

  assert.equal(query.endpoint, 'ctecs-global.ctapi.ctyun.cn');
  assert.equal(query.path, '/v4/order/new-query-price');
  assert.deepEqual(query.body, { flavorId: 's7.large.4' });
});

test('basic routes support fixed and region-scoped official hosts', () => {
  assert.equal(
    officialApiBaseRoute('tencent', 'redis', 'ap-shanghai').endpoint,
    'redis.tencentcloudapi.com',
  );
  assert.equal(
    officialApiBaseRoute('alibaba', 'ecs', 'cn-hangzhou').endpoint,
    'ecs.cn-hangzhou.aliyuncs.com',
  );
  assert.equal(
    officialApiBaseRoute('volcengine', 'ecs', 'cn-beijing').endpoint,
    'open.volcengineapi.com',
  );
});

test('unknown services keep a supplied official endpoint and do not invent one', () => {
  const supplied = withOfficialApiBaseRoute({
    provider: 'ctyun', service: 'future-service', region: '200000001790',
    endpoint: 'future-global.ctapi.ctyun.cn', path: '/v4/query',
  });
  const missing = withOfficialApiBaseRoute({
    provider: 'ctyun', service: 'future-service', region: '200000001790', path: '/v4/query',
  });

  assert.equal(supplied.endpoint, 'future-global.ctapi.ctyun.cn');
  assert.equal(missing.endpoint, undefined);
});

test('the base route catalog cannot store paths, parameters, response shapes, or prices', () => {
  const route = officialApiBaseRoute('ctyun', 'ecs', '200000001790');
  const serialized = JSON.stringify(route);
  const catalog = JSON.parse(fs.readFileSync(
    path.resolve(__dirname, '../../../policies/official-api-base-routes.json'), 'utf8',
  ));

  assert.doesNotMatch(serialized, /path|action|parameter|response|price/i);
  for (const provider of Object.values(catalog.providers)) {
    for (const entry of provider.routes || []) {
      assert.ok(Object.keys(entry).every((key) => [
        'services', 'endpoint', 'endpoint_template', 'official_source_url',
      ].includes(key)));
    }
  }
});
