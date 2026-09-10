'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const path = require('node:path');
const {
  DeleteObjectCommand,
  PutObjectCommand,
  S3Client,
} = require('@aws-sdk/client-s3');

const { buildQuoteWorkbook } = require('./quote-workbook');

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

const SCENARIO_LABELS = Object.freeze({
  on_demand: '按需付费',
  one_year_all_upfront: '1 年全预付',
  three_year_all_upfront: '3 年全预付',
});

function buildPageResult(record) {
  const quoteScenarios = record.pricing_scenarios || [];
  const components = [
    ...(record.resource_ir || []),
    ...(record.zero_cost_ir || []),
  ].map((component) => ({
    service_name: String(
      component.customer_facing?.service_name
      || component.component_key,
    ).slice(0, 120),
    model_or_plan: String(component.customer_facing?.model_or_plan || '').slice(0, 160),
    quantity: String(component.customer_facing?.quantity || '').slice(0, 80),
    configuration_summary: String(
      component.customer_facing?.configuration_summary || '',
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
      label: SCENARIO_LABELS[cost.scenario_key] || cost.scenario_key,
      monthly_cost: String(cost.monthly_cost),
      upfront_cost: String(cost.upfront_cost || '0'),
    })),
  }));
  const scenarios = quoteScenarios.map((scenario) => ({
    scenario_key: scenario.scenario_key,
    label: SCENARIO_LABELS[scenario.scenario_key] || scenario.scenario_key,
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
    status: deliveryResult.page_result ? 'page_result_ready' : 'delivered',
    delivered_at: new Date().toISOString(),
    webhook_event_id: String(deliveryResult.webhook?.event_id || ''),
  };
  if (deliveryResult.page_result) receipt.page_result = deliveryResult.page_result;
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

function webhookMarkdown(record, artifact) {
  const submissionCode = safeSubmissionCode(record.submission_code);
  const lines = [];
  if (submissionCode) lines.push(`**提交码：${submissionCode}**`, '');
  lines.push(
    '**报价已完成**',
    '',
    `[Excel 报价单：点击下载](${artifact.spreadsheet_url})`,
  );
  return lines.join('\n');
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
  } finally {
    await fs.rm(temporary, { force: true }).catch(() => {});
  }
  return { token, path: target };
}

class QuoteDeliveryService {
  constructor({
    bucket = process.env.ASTRAQUOTE_XLSX_BUCKET || process.env.ASTRAQUOTE_DOCX_BUCKET,
    region = process.env.ASTRAQUOTE_XLSX_REGION || process.env.ASTRAQUOTE_DOCX_REGION || process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION,
    prefix = process.env.ASTRAQUOTE_XLSX_PREFIX || process.env.ASTRAQUOTE_DOCX_PREFIX || 'quotes',
    urlTtlSeconds = Number(process.env.ASTRAQUOTE_XLSX_URL_TTL_SECONDS || process.env.ASTRAQUOTE_DOCX_URL_TTL_SECONDS || 604800),
    webhookUrl = process.env.ASTRAQUOTE_WEBHOOK_URL,
    publicBaseUrl = process.env.ASTRAQUOTE_PUBLIC_BASE_URL,
    artifactDirectory = process.env.ASTRAQUOTE_ARTIFACT_DIR
      || path.join(process.env.ASTRAQUOTE_V2_STATE_DIR || '/data/v2-quotes', 'artifacts'),
    s3Client,
    fetchImpl = globalThis.fetch,
    documentBuilder = buildQuoteWorkbook,
    deliveryGuard = defaultDeliveryGuard,
    completionWriter = writeRelayCompletionReceipt,
  } = {}) {
    this.bucket = bucket;
    this.region = region;
    this.prefix = prefix.replace(/^\/+|\/+$/g, '');
    this.urlTtlSeconds = urlTtlSeconds;
    this.webhookUrl = webhookUrl;
    this.publicBaseUrl = String(publicBaseUrl || '').replace(/\/+$/g, '');
    this.artifactDirectory = path.resolve(artifactDirectory);
    this.s3 = s3Client || (region ? new S3Client({ region }) : null);
    this.fetchImpl = fetchImpl;
    this.documentBuilder = documentBuilder;
    this.deliveryGuard = deliveryGuard;
    this.completionWriter = completionWriter;
  }

  validateConfiguration() {
    const missing = [];
    if (!this.bucket) missing.push('ASTRAQUOTE_XLSX_BUCKET');
    if (!this.region) missing.push('ASTRAQUOTE_XLSX_REGION/AWS_REGION');
    if (!this.webhookUrl) missing.push('ASTRAQUOTE_WEBHOOK_URL');
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
    await this.assertDeliveryAllowed(record);
    const result = {
      status: 'displayed_on_page',
      quote_id: record.quote_id,
      page_result: buildPageResult(record),
      webhook: { status: 'not_requested' },
      artifact_type: null,
    };
    await this.assertDeliveryAllowed(record);
    await this.completionWriter(record, result);
    return result;
  }

  async deliver(record) {
    this.validateConfiguration();
    await this.assertDeliveryAllowed(record);
    const buffer = await this.documentBuilder(record);
    const date = new Date(record.verification.verified_at || Date.now());
    const year = String(date.getUTCFullYear());
    const month = String(date.getUTCMonth() + 1).padStart(2, '0');
    const filename = `${safeName(record.quote_name)}-${record.quote_id}.xlsx`;
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
        // The object remains private and no WebHook is sent if cleanup fails.
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
    const documentUrl = `${this.publicBaseUrl}/api/backend/api/quote-artifacts/${artifactManifest.token}`;
    const eventId = `aqevt_${crypto.createHash('sha256').update(record.quote_id).digest('hex').slice(0, 24)}`;
    const artifact = {
      s3_bucket: this.bucket,
      s3_key: key,
      spreadsheet_url: documentUrl,
      spreadsheet_url_expires_at: expiresAt,
      // Temporary response aliases keep already-open ChatGPT conversations working.
      document_url: documentUrl,
      document_url_expires_at: expiresAt,
      event_id: eventId,
    };

    let response;
    try {
      await this.assertDeliveryAllowed(record);
      response = await this.fetchImpl(this.webhookUrl, {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-astraquote-event-id': eventId },
        body: JSON.stringify({
          msgtype: 'markdown',
          markdown: { content: webhookMarkdown(record, artifact) },
        }),
        signal: AbortSignal.timeout(10000),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.errcode !== 0) {
        throw new Error(`webhook_status_${response.status}_code_${payload.errcode ?? 'unknown'}`);
      }
    } catch (error) {
      await fs.rm(artifactManifest.path, { force: true }).catch(() => {});
      await this.s3.send(new DeleteObjectCommand({ Bucket: this.bucket, Key: key })).catch(() => {});
      if (error?.code === 'quote_delivery_cancelled') throw error;
      throw new QuoteDeliveryError('The quote package was uploaded, but the WebHook notification failed.', {
        code: 'quote_webhook_failed',
        details: { s3_key: key, event_id: eventId, error_type: error?.name || 'Error' },
      });
    }

    const result = {
      status: 'delivered',
      quote_id: record.quote_id,
      spreadsheet_url: documentUrl,
      spreadsheet_url_expires_at: expiresAt,
      artifact_type: 'xlsx',
      document_url: documentUrl,
      document_url_expires_at: expiresAt,
      s3_key: key,
      webhook: { status: 'sent', event_id: eventId },
    };
    try {
      await this.completionWriter(record, result);
    } catch (error) {
      // External delivery has already succeeded. Keep the ChatGPT structured
      // completion marker as a fallback instead of reporting a false delivery
      // failure or sending the same WebHook twice on retry.
      console.error(
        'AstraQuote relay completion receipt could not be written:',
        error?.code || error?.name || 'Error',
      );
    }
    return result;
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
  webhookMarkdown,
  writeArtifactManifest,
  writeRelayCompletionReceipt,
};
