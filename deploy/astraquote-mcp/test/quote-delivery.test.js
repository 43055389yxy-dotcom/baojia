'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const {
  QuoteDeliveryService,
  buildPageResult,
  defaultDeliveryGuard,
  writeRelayCompletionReceipt,
} = require('../lib/quote-delivery');

test('sales-page result removes internal cheapest-candidate wording', () => {
  const pageRecord = record();
  pageRecord.resource_ir[0].customer_facing.configuration_summary =
    'Premium P3，26 GiB，3 个物理节点；在候选型号中月费最低。';

  const result = buildPageResult(pageRecord);

  assert.equal(
    result.components[0].configuration_summary,
    'Premium P3，26 GiB，3 个物理节点。',
  );
});

function record() {
  return {
    quote_id: 'aqv2_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    quote_name: '东京 AWS 报价',
    submission_code: '7',
    cloud_provider: 'aws',
    currency: 'USD',
    default_region: 'ap-northeast-1',
    pricing_scenarios: [{
      scenario_key: 'on_demand', monthly_total: '100', upfront_total: '0',
    }],
    resource_ir: [{
      component_key: 'cmp_ec2_0001',
      customer_facing: {
        service_name: 'Amazon EC2', model_or_plan: 'm7g.large', quantity: '1 台',
        configuration_summary: '2 核 8 GiB，按需运行。',
      },
      scenario_costs: [{
        scenario_key: 'on_demand', monthly_cost: '100', upfront_cost: '0',
      }],
    }],
    adjustments: [],
    verification: {
      status: 'official_price_verified',
      verified_at: '2026-09-09T00:00:00.000Z',
      costs: { monthly: 100, total_12_months: 1200 },
    },
  };
}

test('uploads the Excel file privately and returns one stable sales-page download link', async () => {
  const artifactDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-artifacts-'));
  const commands = [];
  const service = new QuoteDeliveryService({
    bucket: 'private-quote-bucket',
    region: 'ap-east-1',
    publicBaseUrl: 'https://baojia.tontiancloud.com',
    artifactDirectory,
    s3Client: { send: async (command) => { commands.push(command); return {}; } },
    documentBuilder: async () => Buffer.from('excel-package'),
  });

  const result = await service.deliver(record());
  assert.equal(result.status, 'delivered');
  assert.equal(commands.length, 1);
  assert.equal(commands[0].input.Bucket, 'private-quote-bucket');
  assert.equal(commands[0].input.ServerSideEncryption, 'AES256');
  assert.match(commands[0].input.Key, /\.xlsx$/);
  assert.equal(commands[0].input.ContentType, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
  const manifests = fs.readdirSync(artifactDirectory);
  assert.equal(manifests.length, 2);
  const tokenManifestName = manifests.find((name) => name.startsWith('aqdl_'));
  assert.ok(tokenManifestName);
  const manifest = JSON.parse(fs.readFileSync(path.join(artifactDirectory, tokenManifestName), 'utf8'));
  assert.equal(manifest.bucket, 'private-quote-bucket');
  assert.equal(manifest.key, commands[0].input.Key);
  assert.match(result.spreadsheet_url, /\/api\/backend\/api\/quote-artifacts\/aqdl_[a-f0-9]{48}$/);
  fs.rmSync(artifactDirectory, { recursive: true, force: true });
});

test('every page delivery renders Excel and writes result plus download link to the receipt', async () => {
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
    bucket: 'private-quote-bucket',
    region: 'ap-east-1',
    publicBaseUrl: 'https://baojia.tontiancloud.com',
    artifactDirectory: path.join(directory, 'artifacts'),
    deliveryGuard: async () => true,
    completionWriter: (quote, result) => writeRelayCompletionReceipt(
      quote,
      result,
      { directory },
    ),
    s3Client: { send: async () => ({}) },
    documentBuilder: async () => Buffer.from('excel-package'),
  });

  const result = await service.deliverPageResult(pageRecord);
  const receipt = JSON.parse(fs.readFileSync(
    path.join(directory, 'completions', `${pageRecord.relay_job_id}.json`),
    'utf8',
  ));

  assert.equal(result.status, 'displayed_on_page');
  assert.match(result.spreadsheet_url, /\/api\/backend\/api\/quote-artifacts\/aqdl_/);
  assert.equal(receipt.status, 'page_result_ready');
  assert.equal(receipt.page_result.components[0].service_name, 'Amazon EC2');
  assert.equal(receipt.page_result.scenarios[0].monthly_total, '100');
  assert.equal(receipt.spreadsheet_url, result.spreadsheet_url);
  fs.rmSync(directory, { recursive: true, force: true });
});

test('does not upload when delivery configuration is incomplete', async () => {
  const service = new QuoteDeliveryService({
    bucket: '',
    region: 'ap-east-1',
    s3Client: { send: async () => assert.fail('must not upload') },
    documentBuilder: async () => assert.fail('must not render'),
  });
  await assert.rejects(
    service.deliver(record()),
    (error) => error.code === 'quote_delivery_configuration_missing',
  );
});

test('a cancelled relay job cannot upload or expose a result', async () => {
  const cancelled = record();
  cancelled.relay_job_id = 'gpt-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
  const service = new QuoteDeliveryService({
    bucket: 'private-quote-bucket',
    region: 'ap-east-1',
    publicBaseUrl: 'https://baojia.tontiancloud.com',
    deliveryGuard: async () => false,
    s3Client: { send: async () => assert.fail('must not upload') },
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

test('writes an authoritative relay completion receipt with page result and download link', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-delivery-receipt-'));
  const delivered = record();
  delivered.relay_job_id = `gpt-${'b'.repeat(32)}`;
  try {
    const receipt = await writeRelayCompletionReceipt(
      delivered,
      {
        status: 'delivered',
        quote_id: delivered.quote_id,
        page_result: { schema_version: 'astraquote-page-result/1' },
        spreadsheet_url: 'https://baojia.tontiancloud.com/api/backend/api/quote-artifacts/aqdl_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
        spreadsheet_filename: 'quote.xlsx',
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
    assert.equal(JSON.parse(fs.readFileSync(receiptPath, 'utf8')).spreadsheet_filename, 'quote.xlsx');
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('replaying the same quote reuses its Excel artifact and does not upload twice', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-no-receipt-'));
  const failed = record();
  failed.relay_job_id = `gpt-${'c'.repeat(32)}`;
  const previous = process.env.ASTRAQUOTE_GPT_RELAY_DIR;
  process.env.ASTRAQUOTE_GPT_RELAY_DIR = directory;
  let uploads = 0;
  let renders = 0;
  try {
    const service = new QuoteDeliveryService({
      bucket: 'private-quote-bucket',
      region: 'ap-east-1',
      publicBaseUrl: 'https://baojia.tontiancloud.com',
      artifactDirectory: path.join(directory, 'artifacts'),
      deliveryGuard: async () => true,
      s3Client: { send: async () => { uploads += 1; return {}; } },
      documentBuilder: async () => { renders += 1; return Buffer.from('excel-package'); },
    });
    const first = await service.deliver(failed);
    const second = await service.deliver(failed);
    assert.equal(first.spreadsheet_url, second.spreadsheet_url);
    assert.equal(uploads, 1);
    assert.equal(renders, 1);
  } finally {
    if (previous === undefined) delete process.env.ASTRAQUOTE_GPT_RELAY_DIR;
    else process.env.ASTRAQUOTE_GPT_RELAY_DIR = previous;
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
