'use strict';

const crypto = require('node:crypto');

const SAVE_ESTIMATE_URL = 'https://dnd5zrqcec4or.cloudfront.net/Prod/v2/saveAs';
const PUBLIC_ESTIMATE_BASE_URL = 'https://calculator.aws/#/estimate?id=';
const ONE_YEAR_MS = 365 * 24 * 60 * 60 * 1000;

class AwsCalculatorLinkError extends Error {
  constructor(message, { code = 'aws_calculator_link_failed', details = {}, retryable = true } = {}) {
    super(message);
    this.name = 'AwsCalculatorLinkError';
    this.code = code;
    this.details = details;
    this.retryable = retryable;
  }
}

function normalizedIdentity(component) {
  const customer = component?.customer_facing || {};
  return [
    component?.component_key,
    component?.instance,
    customer.service_name,
    customer.model_or_plan,
    customer.requirement_summary,
    customer.configuration_summary,
  ].filter(Boolean).join(' ').normalize('NFKC').toLowerCase();
}

function awsCalculatorServiceCode(component) {
  const value = normalizedIdentity(component);
  const mappings = [
    [/aurora.*postgres|postgres.*aurora/, 'amazonRDSAuroraPostgreSQLCompatibleDB'],
    [/aurora.*mysql|mysql.*aurora/, 'amazonAuroraMySQLCompatible'],
    [/(?:rds|relational database).*(?:mariadb)|mariadb.*(?:rds|database)/, 'amazonRDSMariaDB'],
    [/(?:rds|relational database).*(?:postgres|postgresql)|(?:postgres|postgresql).*(?:rds|database)/, 'amazonRDSPostgreSQLDB'],
    [/(?:rds|relational database).*(?:mysql)|mysql.*(?:rds|database)/, 'amazonRDSMySQLDB'],
    [/(?:elasticache|redis|memcached)/, 'amazonElastiCache'],
    [/(?:elastic load balanc|\balb\b|\bnlb\b|\belb\b)/, 'elasticLoadBalancing'],
    [/(?:simple storage service|amazon s3|\bs3\b)/, 'amazonSimpleStorageServiceGroup'],
    [/(?:elastic block store|amazon ebs|\bebs\b)/, 'amazonElasticBlockStore'],
    [/(?:amazon ec2|elastic compute cloud|\bec2\b)/, 'ec2Enhancement'],
    [/(?:amazon dynamodb|\bdynamodb\b)/, 'amazonDynamoDb'],
    [/(?:amazon cloudfront|\bcloudfront\b)/, 'amazonCloudFront'],
    [/(?:amazon cloudwatch|\bcloudwatch\b)/, 'amazonCloudWatch'],
    [/(?:amazon api gateway|\bapi gateway\b)/, 'amazonApiGateway'],
    [/(?:amazon route 53|\broute ?53\b)/, 'amazonRoute53'],
    [/(?:amazon eks|elastic kubernetes|\beks\b)/, 'awsEks'],
    [/(?:aws fargate|\bfargate\b)/, 'awsFargate'],
    [/(?:aws lambda|amazon lambda|\blambda\b)/, 'aWSLambda'],
    [/(?:amazon virtual private cloud|\bvpc\b|nat gateway)/, 'amazonVirtualPrivateCloud'],
    [/(?:aws backup|amazon backup)/, 'awsBackup'],
    [/(?:aws data transfer|amazon data transfer|\bdata transfer\b)/, 'aWSDataTransfer'],
    [/(?:amazon bedrock|aws bedrock|\bbedrock\b)/, 'amazonBedrock'],
  ];
  return mappings.find(([pattern]) => pattern.test(value))?.[1] || null;
}

function numberValue(value) {
  const parsed = Number(value || 0);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

function selectedScenario(record) {
  const scenarios = Array.isArray(record?.pricing_scenarios) ? record.pricing_scenarios : [];
  return scenarios.find((scenario) => scenario.scenario_key === 'on_demand') || scenarios[0] || null;
}

function componentCost(component, scenarioKey) {
  if (component?.pricing_basis === 'official_no_additional_charge') {
    return { monthly: 0, upfront: 0 };
  }
  const costs = Array.isArray(component?.scenario_costs) ? component.scenario_costs : [];
  const selected = costs.find((cost) => cost.scenario_key === scenarioKey) || costs[0];
  return {
    monthly: numberValue(selected?.monthly_cost ?? component?.expected_monthly_cost),
    upfront: numberValue(selected?.upfront_cost),
  };
}

function calculatorComponents() {
  // The public Calculator save contract requires a valid component object.
  // Costs and customer-facing configuration are supplied from the already
  // verified AstraQuote record; this object is not used as a price oracle.
  return {
    tenancy: { value: 'shared' },
    selectedOS: { value: 'linux' },
    workloadSelection: { value: 'consistent' },
    storageType: { value: 'Storage General Purpose GB Mo' },
    dataTransferForEC2: {
      value: [
        { entryType: 'INBOUND', value: '', unit: 'tb_month', fromRegion: '' },
        { entryType: 'OUTBOUND', value: '', unit: 'tb_month', toRegion: '' },
        { entryType: 'INTRA_REGION', value: '', unit: 'tb_month' },
      ],
    },
    workload: { value: { workloadType: 'consistent', data: '1' } },
  };
}

function buildSavePayload(record, { now = new Date() } = {}) {
  if (record?.cloud_provider !== 'aws') {
    throw new AwsCalculatorLinkError('AWS Calculator links are available only for AWS quotes.', {
      code: 'aws_calculator_provider_required', retryable: false,
    });
  }
  const scenario = selectedScenario(record);
  if (!scenario) {
    throw new AwsCalculatorLinkError('The AWS quote has no pricing scenario to publish.', {
      code: 'aws_calculator_scenario_missing', retryable: false,
    });
  }
  const components = [
    ...(record.resource_ir || []),
    ...(record.zero_cost_ir || []),
  ];
  const unsupported = components.filter((component) => !awsCalculatorServiceCode(component));
  if (unsupported.length > 0) {
    throw new AwsCalculatorLinkError('One or more AWS services cannot be represented safely in the public Calculator link.', {
      code: 'aws_calculator_service_unsupported',
      retryable: false,
      details: { component_keys: unsupported.map((component) => component.component_key) },
    });
  }
  const services = {};
  for (const component of components) {
    const serviceCode = awsCalculatorServiceCode(component);
    const customer = component.customer_facing || {};
    const cost = componentCost(component, scenario.scenario_key);
    const identifier = `${serviceCode}-${crypto.randomUUID()}`;
    services[identifier] = {
      calculationComponents: calculatorComponents(),
      serviceCode,
      region: component.region || record.default_region || 'us-east-1',
      estimateFor: 'template',
      version: '0.0.68',
      description: customer.requirement_summary || null,
      serviceCost: { monthly: cost.monthly, upfront: cost.upfront },
      serviceName: customer.service_name || component.component_key,
      regionName: component.region || record.default_region || 'us-east-1',
      configSummary: [
        customer.model_or_plan,
        customer.quantity,
        customer.configuration_summary,
      ].filter(Boolean).join(' | ').slice(0, 4000),
    };
  }
  return {
    name: String(record.quote_name || `AstraQuote ${record.quote_id || ''}`).slice(0, 160),
    services,
    groups: {},
    groupSubtotal: { monthly: numberValue(scenario.monthly_total) },
    totalCost: {
      monthly: numberValue(scenario.monthly_total),
      upfront: numberValue(scenario.upfront_total),
    },
    support: {},
    metaData: {
      locale: 'zh_CN',
      currency: record.currency || 'USD',
      createdOn: now.toISOString(),
      source: 'calculator-platform',
    },
  };
}

function savedKeyFromResponse(payload) {
  let body = payload?.body;
  if (typeof body === 'string') {
    try { body = JSON.parse(body); } catch { body = null; }
  }
  const key = String(body?.savedKey || payload?.savedKey || '');
  return /^[a-f0-9]{40}$/.test(key) ? key : null;
}

class AwsCalculatorLinkService {
  constructor({
    fetchImpl = globalThis.fetch,
    endpoint = process.env.ASTRAQUOTE_AWS_CALCULATOR_SAVE_URL || SAVE_ESTIMATE_URL,
    timeoutMs = Number(process.env.ASTRAQUOTE_AWS_CALCULATOR_TIMEOUT_MS || 30000),
    now = () => new Date(),
  } = {}) {
    this.fetchImpl = fetchImpl;
    this.endpoint = endpoint;
    this.timeoutMs = timeoutMs;
    this.now = now;
  }

  async create(record) {
    if (record?.cloud_provider !== 'aws') return null;
    if (typeof this.fetchImpl !== 'function') {
      throw new AwsCalculatorLinkError('This Node.js runtime cannot call the AWS Calculator share service.', {
        code: 'aws_calculator_fetch_unavailable', retryable: false,
      });
    }
    const createdAt = this.now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let response;
    try {
      response = await this.fetchImpl(this.endpoint, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          origin: 'https://calculator.aws',
          referer: 'https://calculator.aws/',
        },
        body: JSON.stringify(buildSavePayload(record, { now: createdAt })),
        signal: controller.signal,
      });
    } catch (error) {
      throw new AwsCalculatorLinkError('The AWS Calculator public link could not be created.', {
        code: error?.name === 'AbortError'
          ? 'aws_calculator_link_timeout' : 'aws_calculator_link_unavailable',
        details: { error_type: error?.name || 'Error' },
      });
    } finally {
      clearTimeout(timer);
    }
    let payload;
    try { payload = await response.json(); } catch { payload = null; }
    const savedKey = response.ok ? savedKeyFromResponse(payload) : null;
    if (!savedKey) {
      throw new AwsCalculatorLinkError('AWS Calculator rejected the public estimate link.', {
        code: 'aws_calculator_link_rejected',
        details: { http_status: response.status },
      });
    }
    const expiresAt = new Date(createdAt.getTime() + ONE_YEAR_MS).toISOString();
    return {
      aws_calculator_url: `${PUBLIC_ESTIMATE_BASE_URL}${savedKey}`,
      aws_calculator_url_expires_at: expiresAt,
      aws_calculator_scenario_key: selectedScenario(record).scenario_key,
      aws_calculator_notice: '该 AWS 官方计算器公开链接是 AstraQuote 已校验金额和配置的交付副本，有效期 1 年；价格依据仍以本次官方价格查询记录为准。',
    };
  }
}

module.exports = {
  AwsCalculatorLinkError,
  AwsCalculatorLinkService,
  ONE_YEAR_MS,
  PUBLIC_ESTIMATE_BASE_URL,
  SAVE_ESTIMATE_URL,
  awsCalculatorServiceCode,
  buildSavePayload,
  savedKeyFromResponse,
  selectedScenario,
};
