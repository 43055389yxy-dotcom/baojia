'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const {
  DeleteObjectCommand,
  PutObjectCommand,
  S3Client,
} = require('@aws-sdk/client-s3');

const { buildQuoteWorkbook, simplifyCustomerText } = require('./quote-workbook');

const XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet';

class QuoteDeliveryError extends Error {
  constructor(message, { code = 'quote_delivery_failed', details = {} } = {}) {
    super(message);
    this.name = 'QuoteDeliveryError';
    this.code = code;
    this.details = details;
  }
}

function safeName(value) {
  const normalized = String(value || 'cloud-quote')
    .normalize('NFKC')
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, '-')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '')
    .slice(0, 80);
  return normalized || 'cloud-quote';
}

function safeSubmissionCode(value) {
  const normalized = String(value || '').trim();
  return /^[1-9]$/.test(normalized) ? normalized : '';
}

function shortQuoteFilename(record) {
  const provider = {
    aws: 'AWS',
    azure: 'Azure',
    oci: 'OCI',
    gcp: 'GCP',
  }[record.cloud_provider] || 'Cloud';
  const suffix = String(record.quote_id || '')
    .replace(/[^a-z0-9]/gi, '')
    .slice(-8) || crypto.randomBytes(4).toString('hex');
  return `${provider}报价-${suffix}.xlsx`;
}

const PROVIDER_SCENARIO_LABELS = Object.freeze({
  aws: {
    on_demand: '按需付费',
    one_year_commitment: '1 年预留实例全预付',
    three_year_commitment: '3 年预留实例全预付',
  },
  azure: {
    on_demand: '即用即付',
    one_year_commitment: '1 年预留',
    three_year_commitment: '3 年预留',
  },
  oci: { on_demand: 'OCI 公开按量价' },
  gcp: {
    on_demand: '按需付费',
    one_year_commitment: '1 年承诺使用',
    three_year_commitment: '3 年承诺使用',
  },
});

function scenarioLabel(record, scenario) {
  if (scenario.label) return String(scenario.label).slice(0, 40);
  return PROVIDER_SCENARIO_LABELS[record.cloud_provider]?.[scenario.scenario_key]
    || scenario.scenario_key;
}

function buildPageResult(record) {
  const quoteScenarios = record.pricing_scenarios || [];
  const components = [
    ...(record.resource_ir || []),
    ...(record.zero_cost_ir || []),
  ].map((component) => ({
    service_name: String(
      simplifyCustomerText(component.customer_facing?.service_name)
      || component.component_key,
    ).slice(0, 120),
    model_or_plan: simplifyCustomerText(component.customer_facing?.model_or_plan).slice(0, 160),
    quantity: simplifyCustomerText(component.customer_facing?.quantity).slice(0, 80),
    configuration_summary: String(
      simplifyCustomerText(component.customer_facing?.configuration_summary),
    ).slice(0, 1200),
    scenario_costs: (
      component.pricing_basis === 'official_no_additional_charge'
        ? quoteScenarios.map((scenario) => ({
          scenario_key: scenario.scenario_key,
          monthly_cost: '0',
          upfront_cost: '0',
        }))
        : component.scenario_costs || []
    ).map((cost) => ({
      scenario_key: cost.scenario_key,
      label: scenarioLabel(record, cost),
      monthly_cost: String(cost.monthly_cost),
      upfront_cost: String(cost.upfront_cost || '0'),
    })),
  }));
  const scenarios = quoteScenarios.map((scenario) => ({
    scenario_key: scenario.scenario_key,
    label: scenarioLabel(record, scenario),
    monthly_total: String(scenario.monthly_total),
    upfront_total: String(scenario.upfront_total || '0'),
  }));
  return {
    schema_version: 'astraquote-page-result/1',
    currency: record.currency || 'USD',
    region: record.default_region || '',
    components,
    scenarios,
  };
}

async function writeRelayCompletionReceipt(
  record,
  deliveryResult,
  { directory = process.env.ASTRAQUOTE_GPT_RELAY_DIR || '/data/gpt-relay' } = {},
) {
  const relayJobId = String(record.relay_job_id || '');
  if (!relayJobId) return null;
  if (!/^gpt-[a-f0-9]{32}$/.test(relayJobId)) {
    throw new QuoteDeliveryError('The relay job id is invalid.', {
      code: 'relay_job_id_invalid',
      details: { relay_job_id: relayJobId },
    });
  }
  const submissionCode = safeSubmissionCode(record.submission_code);
  if (!submissionCode) {
    throw new QuoteDeliveryError('The relay submission code is invalid.', {
      code: 'relay_submission_code_invalid',
      details: { relay_job_id: relayJobId },
    });
  }
  const completionDirectory = path.join(path.resolve(directory), 'completions');
  await fs.mkdir(completionDirectory, { recursive: true, mode: 0o700 });
  const receipt = {
    schema_version: 'astraquote-relay-completion/1',
    job_id: relayJobId,
    submission_code: submissionCode,
    quote_id: String(record.quote_id || ''),
    status: deliveryResult.status === 'displayed_on_page' ? 'page_result_ready' : 'delivered',
    delivered_at: new Date().toISOString(),
  };
  if (deliveryResult.page_result) receipt.page_result = deliveryResult.page_result;
  if (deliveryResult.spreadsheet_url) {
    receipt.spreadsheet_url = deliveryResult.spreadsheet_url;
    receipt.spreadsheet_filename = deliveryResult.spreadsheet_filename || '';
  }
  const receiptPath = path.join(completionDirectory, `${relayJobId}.json`);
  const temporaryPath = path.join(
    completionDirectory,
    `.${relayJobId}.${process.pid}.${crypto.randomUUID()}.tmp`,
  );
  try {
    await fs.writeFile(temporaryPath, `${JSON.stringify(receipt)}\n`, { mode: 0o600 });
    await fs.rename(temporaryPath, receiptPath);
  } finally {
    await fs.rm(temporaryPath, { force: true }).catch(() => {});
  }
  return receipt;
}

async function defaultDeliveryGuard(record) {
  const relayJobId = String(record.relay_job_id || '');
  if (!relayJobId) return true;
  if (!/^gpt-[a-f0-9]{32}$/.test(relayJobId)) return false;
  const relayDirectory = path.resolve(process.env.ASTRAQUOTE_GPT_RELAY_DIR || '/data/gpt-relay');
  const jobPath = path.join(relayDirectory, 'jobs', `${relayJobId}.json`);
  try {
    const job = JSON.parse(await fs.readFile(jobPath, 'utf8'));
    return job.job_id === relayJobId && job.status === 'processing';
  } catch {
    return false;
  }
}

async function writeArtifactManifest(record, artifact, { directory }) {
  const token = `aqdl_${crypto.randomBytes(24).toString('hex')}`;
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  const target = path.join(directory, `${token}.json`);
  const temporary = path.join(directory, `.${token}.${process.pid}.${crypto.randomUUID()}.tmp`);
  const manifest = {
    schema_version: 'astraquote-artifact/1',
    token,
    quote_id: record.quote_id,
    bucket: artifact.bucket,
    region: artifact.region,
    key: artifact.key,
    filename: artifact.filename,
    content_type: XLSX_MIME,
    expires_at: artifact.expires_at,
  };
  try {
    await fs.writeFile(temporary, `${JSON.stringify(manifest)}\n`, { mode: 0o600 });
    await fs.rename(temporary, target);
    const indexTarget = path.join(directory, `quote-${record.quote_id}.json`);
    const indexTemporary = path.join(
      directory,
      `.quote-${record.quote_id}.${process.pid}.${crypto.randomUUID()}.tmp`,
    );
    await fs.writeFile(indexTemporary, `${JSON.stringify(manifest)}\n`, { mode: 0o600 });
    await fs.rename(indexTemporary, indexTarget);
  } finally {
    await fs.rm(temporary, { force: true }).catch(() => {});
  }
  return { token, path: target };
}

async function readExistingArtifact(record, { directory, publicBaseUrl }) {
  const indexPath = path.join(directory, `quote-${record.quote_id}.json`);
  try {
    const manifest = JSON.parse(await fs.readFile(indexPath, 'utf8'));
    if (
      manifest.schema_version !== 'astraquote-artifact/1'
      || manifest.quote_id !== record.quote_id
      || !/^aqdl_[a-f0-9]{48}$/.test(String(manifest.token || ''))
      || new Date(manifest.expires_at).getTime() <= Date.now()
    ) return null;
    return {
      s3_bucket: manifest.bucket,
      s3_key: manifest.key,
      spreadsheet_url: `${publicBaseUrl}/api/backend/api/quote-artifacts/${manifest.token}`,
      spreadsheet_url_expires_at: manifest.expires_at,
      spreadsheet_filename: manifest.filename,
      manifest_path: path.join(directory, `${manifest.token}.json`),
      reused: true,
    };
  } catch {
    return null;
  }
}

class QuoteDeliveryService {
  constructor({
    bucket = process.env.ASTRAQUOTE_XLSX_BUCKET || process.env.ASTRAQUOTE_DOCX_BUCKET,
    region = process.env.ASTRAQUOTE_XLSX_REGION || process.env.ASTRAQUOTE_DOCX_REGION || process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION,
    prefix = process.env.ASTRAQUOTE_XLSX_PREFIX || process.env.ASTRAQUOTE_DOCX_PREFIX || 'quotes',
    urlTtlSeconds = Number(process.env.ASTRAQUOTE_XLSX_URL_TTL_SECONDS || process.env.ASTRAQUOTE_DOCX_URL_TTL_SECONDS || 604800),
    publicBaseUrl = process.env.ASTRAQUOTE_PUBLIC_BASE_URL,
    artifactDirectory = process.env.ASTRAQUOTE_ARTIFACT_DIR
      || path.join(process.env.ASTRAQUOTE_V2_STATE_DIR || '/data/v2-quotes', 'artifacts'),
    s3Client,
    documentBuilder = buildQuoteWorkbook,
    deliveryGuard = defaultDeliveryGuard,
    completionWriter = writeRelayCompletionReceipt,
  } = {}) {
    this.bucket = bucket;
    this.region = region;
    this.prefix = prefix.replace(/^\/+|\/+$/g, '');
    this.urlTtlSeconds = urlTtlSeconds;
    this.publicBaseUrl = String(publicBaseUrl || '').replace(/\/+$/g, '');
    this.artifactDirectory = path.resolve(artifactDirectory);
    this.s3 = s3Client || (region ? new S3Client({ region }) : null);
    this.documentBuilder = documentBuilder;
    this.deliveryGuard = deliveryGuard;
    this.completionWriter = completionWriter;
  }

  validateConfiguration() {
    const missing = [];
    if (!this.bucket) missing.push('ASTRAQUOTE_XLSX_BUCKET');
    if (!this.region) missing.push('ASTRAQUOTE_XLSX_REGION/AWS_REGION');
    if (!/^https?:\/\//.test(this.publicBaseUrl)) missing.push('ASTRAQUOTE_PUBLIC_BASE_URL');
    if (!Number.isInteger(this.urlTtlSeconds) || this.urlTtlSeconds < 60 || this.urlTtlSeconds > 604800) {
      missing.push('ASTRAQUOTE_XLSX_URL_TTL_SECONDS(60..604800)');
    }
    if (missing.length > 0) {
      throw new QuoteDeliveryError('Quote delivery is not fully configured.', {
        code: 'quote_delivery_configuration_missing', details: { missing },
      });
    }
  }

  async assertDeliveryAllowed(record) {
    if (!await this.deliveryGuard(record)) {
      throw new QuoteDeliveryError('The quote was cancelled before delivery.', {
        code: 'quote_delivery_cancelled',
        details: { relay_job_id: record.relay_job_id || null },
      });
    }
  }

  async deliverPageResult(record) {
    return this._deliverToSalesPage(record, 'displayed_on_page');
  }

  async deliver(record) {
    return this._deliverToSalesPage(record, 'delivered');
  }

  async createArtifact(record) {
    return this._createOrReuseArtifact(record);
  }

  async completeSalesPageDelivery(record, artifact, status = 'displayed_on_page') {
    await this.assertDeliveryAllowed(record);
    const result = {
      status,
      quote_id: record.quote_id,
      page_result: buildPageResult(record),
      spreadsheet_url: artifact.spreadsheet_url,
      spreadsheet_url_expires_at: artifact.spreadsheet_url_expires_at,
      spreadsheet_filename: artifact.spreadsheet_filename,
      artifact_type: 'xlsx',
      document_url: artifact.spreadsheet_url,
      document_url_expires_at: artifact.spreadsheet_url_expires_at,
    };
    try {
      await this.completionWriter(record, result);
    } catch (error) {
      throw new QuoteDeliveryError('The quote file exists, but its completion receipt could not be saved.', {
        code: 'quote_completion_receipt_failed',
        details: { quote_id: record.quote_id, error_type: error?.name || 'Error' },
      });
    }
    return result;
  }

  async _createOrReuseArtifact(record) {
    this.validateConfiguration();
    await this.assertDeliveryAllowed(record);
    const existing = await readExistingArtifact(record, {
      directory: this.artifactDirectory,
      publicBaseUrl: this.publicBaseUrl,
    });
    if (existing) return existing;

    const buffer = await this.documentBuilder(record);
    const date = new Date(record.verification.verified_at || Date.now());
    const year = String(date.getUTCFullYear());
    const month = String(date.getUTCMonth() + 1).padStart(2, '0');
    const filename = shortQuoteFilename(record);
    const key = `${this.prefix}/${year}/${month}/${record.quote_id}/${filename}`;
    await this.assertDeliveryAllowed(record);
    try {
      await this.s3.send(new PutObjectCommand({
        Bucket: this.bucket,
        Key: key,
        Body: buffer,
        ContentType: XLSX_MIME,
        ContentDisposition: `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`,
        ServerSideEncryption: 'AES256',
        Metadata: { 'quote-id': record.quote_id },
      }));
    } catch (error) {
      throw new QuoteDeliveryError('The Excel quote could not be uploaded to S3.', {
        code: 'quote_document_upload_failed', details: { error_type: error?.name || 'Error' },
      });
    }

    try {
      await this.assertDeliveryAllowed(record);
    } catch (error) {
      try {
        await this.s3.send(new DeleteObjectCommand({ Bucket: this.bucket, Key: key }));
      } catch {
        // The object remains private and no sales-page link is exposed if cleanup fails.
      }
      throw error;
    }

    const expiresAt = new Date(Date.now() + this.urlTtlSeconds * 1000).toISOString();
    let artifactManifest;
    try {
      artifactManifest = await writeArtifactManifest(record, {
        bucket: this.bucket,
        region: this.region,
        key,
        filename,
        expires_at: expiresAt,
      }, { directory: this.artifactDirectory });
    } catch (error) {
      throw new QuoteDeliveryError('The stable quote download link could not be created.', {
        code: 'quote_artifact_manifest_failed',
        details: { s3_key: key, error_type: error?.name || 'Error' },
      });
    }
    return {
      s3_bucket: this.bucket,
      s3_key: key,
      spreadsheet_url: `${this.publicBaseUrl}/api/backend/api/quote-artifacts/${artifactManifest.token}`,
      spreadsheet_url_expires_at: expiresAt,
      spreadsheet_filename: filename,
      manifest_path: artifactManifest.path,
      reused: false,
    };
  }

  async _deliverToSalesPage(record, status) {
    const artifact = await this._createOrReuseArtifact(record);
    return this.completeSalesPageDelivery(record, artifact, status);
  }
}

module.exports = {
  XLSX_MIME,
  QuoteDeliveryError,
  QuoteDeliveryService,
  buildPageResult,
  defaultDeliveryGuard,
  safeName,
  safeSubmissionCode,
  shortQuoteFilename,
  readExistingArtifact,
  writeArtifactManifest,
  writeRelayCompletionReceipt,
};
