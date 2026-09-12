'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  officialPricingPageUrlAllowed,
  pricingScenarios,
} = require('../lib/cloud-market-profiles');

test('official pricing page host validation is provider and market-profile scoped', () => {
  assert.equal(officialPricingPageUrlAllowed(
    'azure', 'https://azure.microsoft.com/en-us/pricing/details/virtual-machines/',
  ), true);
  assert.equal(officialPricingPageUrlAllowed(
    'azure', 'https://azure.microsoft.com.example.test/pricing',
  ), false);
  assert.equal(officialPricingPageUrlAllowed(
    'alibaba', 'https://help.aliyun.com/zh/ecs/product-overview/billing-overview',
    'alibaba-cn',
  ), true);
  assert.equal(officialPricingPageUrlAllowed(
    'alibaba', 'https://www.alibabacloud.com/help/en/ecs/product-overview/billing-overview',
    'alibaba-cn',
  ), false);
  assert.equal(officialPricingPageUrlAllowed(
    'tencent', 'http://cloud.tencent.com/document/product/213/2180',
  ), false);
});

test('pricing scenarios are provider and market-profile scoped', () => {
  assert.deepEqual(
    pricingScenarios('aws').map((scenario) => scenario.key),
    ['on_demand', 'one_year_commitment', 'three_year_commitment'],
  );
  assert.deepEqual(
    pricingScenarios('tencent').map((scenario) => scenario.key),
    ['on_demand', 'one_month_subscription', 'one_year_subscription'],
  );
  assert.deepEqual(pricingScenarios('oci'), [
    { key: 'on_demand', label: 'OCI 公开按量价', term_months: null },
  ]);
});
