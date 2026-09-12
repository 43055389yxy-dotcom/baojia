'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  officialPricingPageUrlAllowed,
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
