'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { once } = require('node:events');

const {
  AwsCalculatorLinkService,
  awsCalculatorServiceCode,
  buildSavePayload,
} = require('../lib/aws-calculator-link');
const { QuoteDeliveryService } = require('../lib/quote-delivery');
const { createApp } = require('../server');

function quote(provider = 'aws') {
  return {
    quote_id: 'aqv2_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    quote_name: 'AstraQuote test',
    cloud_provider: provider,
    currency: 'USD',
    default_region: 'ap-northeast-1',
    pricing_scenarios: [{
      scenario_key: 'on_demand', monthly_total: '123.45', upfront_total: '0',
    }],
    resource_ir: [{
      component_key: 'cmp_ec2_0001',
      region: 'ap-northeast-1',
      customer_facing: {
        service_name: provider === 'aws' ? 'Amazon EC2' : 'Azure Virtual Machines',
        model_or_plan: 'm7g.large',
        quantity: '2 台',
        requirement_summary: '两台云主机',
        configuration_summary: '2 台 m7g.large，按需运行。',
      },
      scenario_costs: [{
        scenario_key: 'on_demand', monthly_cost: '123.45', upfront_cost: '0',
      }],
    }],
    zero_cost_ir: [],
    unpriced_ir: [],
    verification: {
      status: 'official_price_verified',
      verified_at: '2026-09-24T00:00:00.000Z',
      costs: { monthly: 123.45, total_12_months: 1481.4 },
    },
  };
}

test('AWS public Calculator payload mirrors verified component and scenario totals', () => {
  const payload = buildSavePayload(quote(), { now: new Date('2026-09-24T00:00:00.000Z') });
  const service = Object.values(payload.services)[0];

  assert.equal(service.serviceCode, 'ec2Enhancement');
  assert.equal(service.serviceCost.monthly, 123.45);
  assert.equal(service.region, 'ap-northeast-1');
  assert.match(service.configSummary, /m7g\.large/);
  assert.deepEqual(payload.totalCost, { monthly: 123.45, upfront: 0 });
  assert.equal(payload.metaData.currency, 'USD');
});

test('AWS service identity mapping is explicit and fails closed for unknown products', () => {
  assert.equal(awsCalculatorServiceCode({
    component_key: 'cmp_rds_0001',
    customer_facing: { service_name: 'Amazon RDS for PostgreSQL' },
  }), 'amazonRDSPostgreSQLDB');
  assert.equal(awsCalculatorServiceCode({
    component_key: 'cmp_unknown_0001',
    customer_facing: { service_name: 'Unknown private appliance' },
  }), null);

  const unsupported = quote();
  unsupported.resource_ir[0].component_key = 'cmp_unknown_0001';
  unsupported.resource_ir[0].customer_facing.service_name = 'Unknown private appliance';
  assert.throws(
    () => buildSavePayload(unsupported),
    (error) => error.code === 'aws_calculator_service_unsupported',
  );
});

test('AWS quote receives the official public share URL without browser automation', async () => {
  let request;
  const service = new AwsCalculatorLinkService({
    now: () => new Date('2026-09-24T00:00:00.000Z'),
    fetchImpl: async (url, init) => {
      request = { url, init };
      return {
        ok: true,
        status: 201,
        json: async () => ({
          statusCode: 201,
          body: JSON.stringify({ savedKey: 'a'.repeat(40) }),
        }),
      };
    },
  });

  const result = await service.create(quote());

  assert.equal(
    result.aws_calculator_url,
    `https://calculator.aws/#/estimate?id=${'a'.repeat(40)}`,
  );
  assert.equal(result.aws_calculator_url_expires_at, '2027-09-24T00:00:00.000Z');
  assert.equal(request.init.method, 'POST');
  assert.equal(request.init.headers.origin, 'https://calculator.aws');
  assert.ok(JSON.parse(request.init.body).services);
});

test('non-AWS quote never calls or returns a provider calculator link', async () => {
  let calls = 0;
  const calculatorLinkService = {
    create: async () => { calls += 1; throw new Error('must not be called'); },
  };
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-local-delivery-'));
  try {
    const service = new QuoteDeliveryService({
      mode: 'local',
      publicBaseUrl: 'http://127.0.0.1:8200',
      artifactDirectory: path.join(directory, 'artifacts'),
      downloadDirectory: path.join(directory, 'downloads'),
      documentBuilder: async () => Buffer.from('excel-package'),
      deliveryGuard: async () => true,
      completionWriter: async () => null,
      calculatorLinkService,
    });
    const azure = quote('azure');
    const artifact = await service.createArtifact(azure);
    const result = await service.completeMcpDelivery(azure, artifact);
    const resolved = await service.resolveLocalDownload(
      artifact.spreadsheet_url.split('/').at(-2),
      decodeURIComponent(artifact.spreadsheet_url.split('/').at(-1)),
    );

    assert.equal(result.status, 'quote_ready');
    assert.match(result.spreadsheet_url, /^http:\/\/127\.0\.0\.1:8200\/downloads\/aqdl_/);
    assert.equal('aws_calculator_url' in result, false);
    assert.equal(calls, 0);
    assert.equal(fs.readFileSync(resolved.path, 'utf8'), 'excel-package');
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('AWS MCP delivery returns Excel and Calculator links together', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-aws-delivery-'));
  let calls = 0;
  try {
    const service = new QuoteDeliveryService({
      mode: 'local',
      publicBaseUrl: 'http://127.0.0.1:8200',
      artifactDirectory: path.join(directory, 'artifacts'),
      downloadDirectory: path.join(directory, 'downloads'),
      documentBuilder: async () => Buffer.from('excel-package'),
      deliveryGuard: async () => true,
      completionWriter: async () => null,
      calculatorLinkService: {
        create: async () => {
          calls += 1;
          return {
            aws_calculator_url: `https://calculator.aws/#/estimate?id=${'b'.repeat(40)}`,
            aws_calculator_url_expires_at: '2027-09-24T00:00:00.000Z',
          };
        },
      },
    });
    const aws = quote();
    const artifact = await service.createArtifact(aws);
    const result = await service.completeMcpDelivery(aws, artifact);

    assert.equal(calls, 1);
    assert.match(result.spreadsheet_url, /\/downloads\/aqdl_/);
    assert.match(result.aws_calculator_url, /^https:\/\/calculator\.aws\/#\/estimate\?id=/);
    assert.equal(result.quote_result.currency, 'USD');
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('local MCP download route serves the generated Excel file', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-download-route-'));
  let httpServer;
  try {
    const deliverer = new QuoteDeliveryService({
      mode: 'local',
      publicBaseUrl: 'http://127.0.0.1:8200',
      artifactDirectory: path.join(directory, 'artifacts'),
      downloadDirectory: path.join(directory, 'downloads'),
      documentBuilder: async () => Buffer.from('route-download'),
      deliveryGuard: async () => true,
      completionWriter: async () => null,
      calculatorLinkService: { create: async () => null },
    });
    const artifact = await deliverer.createArtifact(quote('azure'));
    const app = createApp({
      backend: { request: async () => ({ status: 'ready' }) },
      store: { directory: path.join(directory, 'state') },
      deliverer,
    });
    httpServer = app.listen(0, '127.0.0.1');
    await once(httpServer, 'listening');
    const port = httpServer.address().port;
    const url = artifact.spreadsheet_url.replace('127.0.0.1:8200', `127.0.0.1:${port}`);
    const response = await fetch(url);

    assert.equal(response.status, 200);
    assert.equal(await response.text(), 'route-download');
    assert.match(response.headers.get('content-disposition'), /^attachment;/);
  } finally {
    if (httpServer) await new Promise((resolve) => httpServer.close(resolve));
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
