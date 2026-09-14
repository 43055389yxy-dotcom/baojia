'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const {
  officialApiBaseRoute,
  withOfficialApiBaseRoute,
} = require('../lib/official-api-base-routes');

test('basic official API routes replace a caller-guessed host and preserve allowed request details', () => {
  const query = withOfficialApiBaseRoute({
    provider: 'ctyun', service: 'ecs', region: '200000001790',
    endpoint: 'ctapi.ctyun.cn', path: '/v4/order/new-query-price',
    body: { flavorId: 's7.large.4' },
  });

  assert.equal(query.endpoint, 'ctecs-global.ctapi.ctyun.cn');
  assert.equal(query.path, '/v4/order/new-query-price');
  assert.deepEqual(query.body, { flavorId: 's7.large.4' });
});

test('a single registered operation is filled when the caller omits it', () => {
  const query = withOfficialApiBaseRoute({
    provider: 'tencent', service: 'cvm', region: 'ap-shanghai',
    action: '', path: '/', body: { InstanceChargeType: 'POSTPAID_BY_HOUR' },
  });

  assert.equal(query.endpoint, 'cvm.tencentcloudapi.com');
  assert.equal(query.action, 'InquiryPriceRunInstances');
});

test('registered routes allow additional read-only official operations', () => {
  const query = withOfficialApiBaseRoute({
    provider: 'alibaba_intl', service: 'bssopenapi', region: 'ap-southeast-1',
    endpoint: 'caller-guessed.example.com', action: 'QuerySkuPriceList', path: '/',
  });

  assert.equal(query.endpoint, 'business.ap-southeast-1.aliyuncs.com');
  assert.equal(query.action, 'QuerySkuPriceList');
});

test('registered routes reject state-changing operations even when the caller supplies one', () => {
  for (const action of [
    'RunInstances', 'CreateInstance', 'ModifyInstance', 'DeleteInstance',
    'SetRenewal', 'RenewInstance', 'PayOrder', 'RefundInstance', 'BatchCreateInstance',
  ]) {
    assert.throws(
      () => withOfficialApiBaseRoute({
        provider: 'alibaba_intl', service: 'bssopenapi', region: 'ap-southeast-1',
        action, path: '/',
      }),
      (error) => error.code === 'official_api_mutating_operation_blocked',
      action,
    );
  }
});

test('basic routes support fixed and region-scoped official hosts', () => {
  assert.equal(
    officialApiBaseRoute('tencent', 'redis', 'ap-shanghai').endpoint,
    'redis.tencentcloudapi.com',
  );
  assert.equal(
    officialApiBaseRoute('alibaba', 'ecs', 'cn-hangzhou').endpoint,
    'business.aliyuncs.com',
  );
  assert.equal(
    officialApiBaseRoute('alibaba_intl', 'ecs', 'ap-southeast-1').endpoint,
    'business.ap-southeast-1.aliyuncs.com',
  );
  assert.equal(
    officialApiBaseRoute('huawei_intl', 'ecs', 'ap-southeast-3').endpoint,
    'bss-intl.myhuaweicloud.com',
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

test('official-page-only routes clear caller-guessed API hosts', () => {
  const route = officialApiBaseRoute('baidu', 'tsdb', 'bj');
  const query = withOfficialApiBaseRoute({
    provider: 'baidu', service: 'tsdb', region: 'bj', endpoint: 'guessed.example.com',
  });

  assert.equal(route.capability, 'official_page_only');
  assert.equal(route.endpoint, '');
  assert.equal(query.endpoint, undefined);
  assert.equal(query.official_source_url, 'https://cloud.baidu.com/product-price/tsdb.html');
});

test('the route catalog stores only stable capabilities and operation identities', () => {
  const route = officialApiBaseRoute('ctyun', 'ecs', '200000001790');
  const serialized = JSON.stringify(route);
  const catalog = JSON.parse(fs.readFileSync(
    path.resolve(__dirname, '../../../policies/official-api-base-routes.json'), 'utf8',
  ));

  assert.ok(serialized.length > 0);
  assert.ok(Object.keys(route).every((key) => [
    'provider', 'service', 'endpoint', 'capability', 'operations',
    'official_source_url', 'catalog_checked_at',
  ].includes(key)));
  for (const provider of Object.values(catalog.providers)) {
    const entries = [provider.default_route, ...(provider.routes || [])].filter(Boolean);
    for (const entry of entries) {
      assert.ok(Object.keys(entry).every((key) => [
        'services', 'capability', 'endpoint', 'endpoint_template', 'operations',
        'official_source_url',
      ].includes(key)));
    }
  }
});
