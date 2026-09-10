'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  QuoteDeliveryService,
  defaultDeliveryGuard,
  writeRelayCompletionReceipt,
} = require('../lib/quote-delivery');

function record() {
  return {
    quote_id: 'aqv2_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    quote_name: '东京 AWS 报价',
    submission_code: '7',
    currency: 'USD',
    adjustments: [],
    verification: {
      status: 'official_price_verified',
      verified_at: '2026-09-09T00:00:00.000Z',
      costs: { monthly: 100, total_12_months: 1200 },
    },
  };
}

test('uploads the Excel file privately and sends the stable download link to the configured WebHook', async () => {
  const artifactDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-artifacts-'));
  const commands = [];
  const webhookCalls = [];
  const service = new QuoteDeliveryService({
    bucket: 'private-quote-bucket',
    region: 'ap-east-1',
    webhookUrl: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret',
    publicBaseUrl: 'https://baojia.tontiancloud.com',
    artifactDirectory,
    s3Client: { send: async (command) => { commands.push(command); return {}; } },
    fetchImpl: async (url, options) => {
      webhookCalls.push({ url, options });
      return { ok: true, status: 200, json: async () => ({ errcode: 0 }) };
    },
    documentBuilder: async () => Buffer.from('excel-package'),
  });

  const result = await service.deliver(record());
  assert.equal(result.status, 'delivered');
  assert.equal(commands.length, 1);
  assert.equal(commands[0].input.Bucket, 'private-quote-bucket');
  assert.equal(commands[0].input.ServerSideEncryption, 'AES256');
  assert.match(commands[0].input.Key, /\.xlsx$/);
  assert.equal(commands[0].input.ContentType, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
  assert.equal(webhookCalls.length, 1);
  const payload = JSON.parse(webhookCalls[0].options.body);
  assert.match(payload.markdown.content, /提交码：7/);
  assert.doesNotMatch(payload.markdown.content, /销售姓名|@郭瑞龙/);
  assert.match(payload.markdown.content, /报价已完成/);
  assert.doesNotMatch(payload.markdown.content, /Calculator|官方报价链接/i);
  assert.match(payload.markdown.content, /baojia\.tontiancloud\.com\/api\/backend\/api\/quote-artifacts\/aqdl_/);
  assert.doesNotMatch(payload.markdown.content, /amazonaws\.com|X-Amz-Signature/);
  assert.match(payload.markdown.content, /Excel 报价单/);
  assert.doesNotMatch(payload.markdown.content, /报价名称|报价编号|每月费用|12 个月估算|配置调整|链接有效至/);
  const manifests = fs.readdirSync(artifactDirectory);
  assert.equal(manifests.length, 1);
  const manifest = JSON.parse(fs.readFileSync(path.join(artifactDirectory, manifests[0]), 'utf8'));
  assert.equal(manifest.bucket, 'private-quote-bucket');
  assert.equal(manifest.key, commands[0].input.Key);
  assert.match(result.spreadsheet_url, /\/api\/backend\/api\/quote-artifacts\/aqdl_[a-f0-9]{48}$/);
  fs.rmSync(artifactDirectory, { recursive: true, force: true });
});

test('page-only delivery writes a structured receipt without rendering, uploading, or notifying', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-page-result-'));
  const pageRecord = record();
  pageRecord.relay_job_id = `gpt-${'d'.repeat(32)}`;
  pageRecord.default_region = 'ap-northeast-1';
  pageRecord.pricing_scenarios = [{
    scenario_key: 'on_demand', monthly_total: '100', upfront_total: '0',
  }];
  pageRecord.resource_ir = [{
    component_key: 'cmp_ec2_0001',
    customer_facing: {
      service_name: 'Amazon EC2',
      model_or_plan: 'm7g.large',
      quantity: '1 台',
      configuration_summary: '2 核 8 GiB，按需运行。',
    },
    scenario_costs: [{
      scenario_key: 'on_demand', monthly_cost: '100', upfront_cost: '0',
    }],
  }];
  const service = new QuoteDeliveryService({
    deliveryGuard: async () => true,
    completionWriter: (quote, result) => writeRelayCompletionReceipt(
      quote,
      result,
      { directory },
    ),
    s3Client: { send: async () => assert.fail('must not upload') },
    fetchImpl: async () => assert.fail('must not notify'),
    documentBuilder: async () => assert.fail('must not render'),
  });

  const result = await service.deliverPageResult(pageRecord);
  const receipt = JSON.parse(fs.readFileSync(
    path.join(directory, 'completions', `${pageRecord.relay_job_id}.json`),
    'utf8',
  ));

  assert.equal(result.status, 'displayed_on_page');
  assert.equal(result.webhook.status, 'not_requested');
  assert.equal(receipt.status, 'page_result_ready');
  assert.equal(receipt.page_result.components[0].service_name, 'Amazon EC2');
  assert.equal(receipt.page_result.scenarios[0].monthly_total, '100');
  fs.rmSync(directory, { recursive: true, force: true });
});

test('does not upload or notify when delivery configuration is incomplete', async () => {
  const service = new QuoteDeliveryService({
    bucket: '',
    region: 'ap-east-1',
    webhookUrl: '',
    s3Client: { send: async () => assert.fail('must not upload') },
    documentBuilder: async () => assert.fail('must not render'),
  });
  await assert.rejects(
    service.deliver(record()),
    (error) => error.code === 'quote_delivery_configuration_missing',
  );
});

test('a cancelled relay job cannot upload or notify the group', async () => {
  const cancelled = record();
  cancelled.relay_job_id = 'gpt-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
  const service = new QuoteDeliveryService({
    bucket: 'private-quote-bucket',
    region: 'ap-east-1',
    webhookUrl: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret',
    publicBaseUrl: 'https://baojia.tontiancloud.com',
    deliveryGuard: async () => false,
    s3Client: { send: async () => assert.fail('must not upload') },
    fetchImpl: async () => assert.fail('must not notify'),
    documentBuilder: async () => assert.fail('must not render'),
  });
  await assert.rejects(
    service.deliver(cancelled),
    (error) => error.code === 'quote_delivery_cancelled',
  );
});

test('the default delivery guard permits only an actively processing relay job', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-delivery-guard-'));
  const jobsDirectory = path.join(directory, 'jobs');
  fs.mkdirSync(jobsDirectory);
  const relayJobId = `gpt-${'a'.repeat(32)}`;
  const jobPath = path.join(jobsDirectory, `${relayJobId}.json`);
  const previous = process.env.ASTRAQUOTE_GPT_RELAY_DIR;
  process.env.ASTRAQUOTE_GPT_RELAY_DIR = directory;
  try {
    fs.writeFileSync(jobPath, JSON.stringify({ job_id: relayJobId, status: 'processing' }));
    assert.equal(await defaultDeliveryGuard({ relay_job_id: relayJobId }), true);
    fs.writeFileSync(jobPath, JSON.stringify({ job_id: relayJobId, status: 'cancelled' }));
    assert.equal(await defaultDeliveryGuard({ relay_job_id: relayJobId }), false);
  } finally {
    if (previous === undefined) delete process.env.ASTRAQUOTE_GPT_RELAY_DIR;
    else process.env.ASTRAQUOTE_GPT_RELAY_DIR = previous;
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('writes an authoritative relay completion receipt only after delivery succeeds', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-delivery-receipt-'));
  const delivered = record();
  delivered.relay_job_id = `gpt-${'b'.repeat(32)}`;
  try {
    const receipt = await writeRelayCompletionReceipt(
      delivered,
      {
        status: 'delivered',
        quote_id: delivered.quote_id,
        webhook: { status: 'sent', event_id: 'aqevt_receipt' },
      },
      { directory },
    );
    const receiptPath = path.join(
      directory,
      'completions',
      `${delivered.relay_job_id}.json`,
    );
    assert.equal(receipt.job_id, delivered.relay_job_id);
    assert.equal(receipt.status, 'delivered');
    assert.equal(receipt.submission_code, '7');
    assert.equal(receipt.quote_id, delivered.quote_id);
    assert.equal(JSON.parse(fs.readFileSync(receiptPath, 'utf8')).webhook_event_id, 'aqevt_receipt');
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('does not write a relay completion receipt when WebHook delivery fails', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-no-receipt-'));
  const failed = record();
  failed.relay_job_id = `gpt-${'c'.repeat(32)}`;
  const previous = process.env.ASTRAQUOTE_GPT_RELAY_DIR;
  process.env.ASTRAQUOTE_GPT_RELAY_DIR = directory;
  try {
    const service = new QuoteDeliveryService({
      bucket: 'private-quote-bucket',
      region: 'ap-east-1',
      webhookUrl: 'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret',
      publicBaseUrl: 'https://baojia.tontiancloud.com',
      artifactDirectory: path.join(directory, 'artifacts'),
      deliveryGuard: async () => true,
      s3Client: { send: async () => ({}) },
      fetchImpl: async () => ({
        ok: false,
        status: 500,
        json: async () => ({ errcode: 1 }),
      }),
      documentBuilder: async () => Buffer.from('excel-package'),
    });
    await assert.rejects(
      service.deliver(failed),
      (error) => error.code === 'quote_webhook_failed',
    );
    assert.equal(
      fs.existsSync(path.join(directory, 'completions', `${failed.relay_job_id}.json`)),
      false,
    );
  } finally {
    if (previous === undefined) delete process.env.ASTRAQUOTE_GPT_RELAY_DIR;
    else process.env.ASTRAQUOTE_GPT_RELAY_DIR = previous;
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
