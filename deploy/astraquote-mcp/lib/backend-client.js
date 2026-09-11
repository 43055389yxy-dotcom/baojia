'use strict';

class BackendError extends Error {
  constructor(message, {
    status = 502, code = 'backend_error', details = {}, retryable = false,
  } = {}) {
    super(message);
    this.name = 'BackendError';
    this.status = status;
    this.code = code;
    this.details = details;
    this.retryable = retryable;
  }
}

class AstraQuoteBackendClient {
  constructor({
    baseUrl = process.env.ASTRAQUOTE_BACKEND_URL || 'http://astraquote:3000/api/backend',
    token = process.env.ASTRAQUOTE_INTERNAL_TOKEN || '',
    timeoutMs = Number(process.env.ASTRAQUOTE_BACKEND_TIMEOUT_MS || 120000),
    fetchImpl = globalThis.fetch,
  } = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, '');
    this.token = token;
    this.timeoutMs = timeoutMs;
    this.fetch = fetchImpl;
  }

  async request(path, { method = 'GET', body, timeoutMs } = {}) {
    if (!this.token) {
      throw new BackendError('AstraQuote MCP internal token is not configured.', {
        status: 503,
        code: 'internal_auth_not_configured',
      });
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs || this.timeoutMs);
    let response;
    try {
      response = await this.fetch(`${this.baseUrl}${path}`, {
        method,
        headers: {
          Accept: 'application/json',
          'Content-Type': 'application/json',
          'X-AstraQuote-MCP-Token': this.token,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
    } catch (error) {
      const timedOut = error && error.name === 'AbortError';
      throw new BackendError(
        timedOut ? 'AstraQuote backend request timed out.' : 'AstraQuote backend is unavailable.',
        {
          status: 503,
          code: timedOut ? 'backend_timeout' : 'backend_unavailable',
          details: { path },
          retryable: true,
        },
      );
    } finally {
      clearTimeout(timer);
    }

    let payload = {};
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (!response.ok) {
      const validationDetails = Array.isArray(payload.detail)
        ? { violations: payload.detail.map((item) => ({
          path: Array.isArray(item.loc) ? item.loc.join('.') : '',
          message: String(item.msg || 'Invalid request field.'),
          type: String(item.type || 'validation_error'),
        })) }
        : {};
      const isSchemaError = response.status === 422;
      throw new BackendError(
        payload.message || (isSchemaError
          ? 'AstraQuote rejected the request schema. Correct the listed fields and retry.'
          : `AstraQuote backend returned HTTP ${response.status}.`),
        {
          status: response.status,
          code: payload.code || (isSchemaError
            ? 'backend_request_schema_invalid'
            : 'backend_rejected_request'),
          details: payload.details || validationDetails,
          retryable: payload.retryable === true || isSchemaError,
        },
      );
    }
    return payload;
  }

  describeService(input) {
    return this.request('/api/mcp/v2/describe-service', { method: 'POST', body: input });
  }

  getAttributeValues(input) {
    return this.request('/api/mcp/v2/attribute-values', { method: 'POST', body: input });
  }

  searchProducts(input) {
    return this.request('/api/mcp/v2/search-products', { method: 'POST', body: input });
  }

  getPrices(input) {
    return this.request('/api/mcp/v2/prices', { method: 'POST', body: input });
  }
}

module.exports = { AstraQuoteBackendClient, BackendError };
