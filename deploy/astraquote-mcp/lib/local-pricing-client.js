'use strict';

const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const { spawn } = require('node:child_process');

const { BackendError } = require('./backend-client');

const REPOSITORY_ROOT = path.resolve(__dirname, '../../..');
const DEFAULT_BRIDGE = path.join(REPOSITORY_ROOT, 'backend/scripts/local_mcp_bridge.py');
const DEFAULT_BACKEND_ROOT = path.join(REPOSITORY_ROOT, 'backend');

function configuredBackendRoot() {
  return path.resolve(process.env.ASTRAQUOTE_BACKEND_ROOT || DEFAULT_BACKEND_ROOT);
}

function defaultPythonExecutable() {
  if (process.env.ASTRAQUOTE_PYTHON) return process.env.ASTRAQUOTE_PYTHON;
  const virtualEnvironmentPython = path.join(configuredBackendRoot(), '.venv/bin/python');
  return fs.existsSync(virtualEnvironmentPython) ? virtualEnvironmentPython : 'python3';
}

class LocalPricingClient {
  constructor({
    python = defaultPythonExecutable(),
    bridgePath = process.env.ASTRAQUOTE_LOCAL_BRIDGE || DEFAULT_BRIDGE,
    backendRoot = configuredBackendRoot(),
    timeoutMs = Number(process.env.ASTRAQUOTE_BACKEND_TIMEOUT_MS || 120000),
    spawnImpl = spawn,
  } = {}) {
    this.python = python;
    this.bridgePath = path.resolve(bridgePath);
    this.backendRoot = path.resolve(backendRoot);
    this.timeoutMs = timeoutMs;
    this.spawnImpl = spawnImpl;
    this.sequence = 0;
    this.pending = new Map();
    this.child = null;
    this.stderr = '';
  }

  start() {
    if (this.child) return;
    if (!fs.existsSync(this.bridgePath)) {
      throw new BackendError('The local AstraQuote pricing bridge was not found.', {
        status: 503,
        code: 'local_pricing_bridge_missing',
        details: { bridge_path: this.bridgePath },
      });
    }
    const existingPythonPath = String(process.env.PYTHONPATH || '').trim();
    const pythonPath = existingPythonPath
      ? `${this.backendRoot}${path.delimiter}${existingPythonPath}`
      : this.backendRoot;
    this.child = this.spawnImpl(this.python, ['-u', this.bridgePath], {
      cwd: REPOSITORY_ROOT,
      env: { ...process.env, PYTHONPATH: pythonPath },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.child.stderr.on('data', (chunk) => {
      this.stderr = `${this.stderr}${chunk.toString('utf8')}`.slice(-8000);
      process.stderr.write(chunk);
    });
    this.child.once('error', (error) => this.failPending(error));
    this.child.once('exit', (code, signal) => {
      const error = new Error(`Local pricing bridge exited (${code ?? signal ?? 'unknown'}).`);
      this.child = null;
      this.failPending(error);
    });
    const lines = readline.createInterface({ input: this.child.stdout });
    lines.on('line', (line) => this.handleLine(line));
  }

  failPending(error) {
    for (const { reject, timer } of this.pending.values()) {
      clearTimeout(timer);
      reject(new BackendError('The local AstraQuote pricing bridge is unavailable.', {
        status: 503,
        code: 'local_pricing_bridge_unavailable',
        retryable: true,
        details: { error_type: error?.name || 'Error' },
      }));
    }
    this.pending.clear();
  }

  handleLine(line) {
    let payload;
    try {
      payload = JSON.parse(line);
    } catch {
      return;
    }
    const pending = this.pending.get(payload.id);
    if (!pending) return;
    this.pending.delete(payload.id);
    clearTimeout(pending.timer);
    if (payload.ok === true) {
      pending.resolve(payload.result);
      return;
    }
    const error = payload.error || {};
    pending.reject(new BackendError(error.message || 'Local pricing bridge rejected the request.', {
      status: Number(error.status || 500),
      code: error.code || 'local_pricing_bridge_rejected',
      details: error.details || {},
      retryable: error.retryable === true,
    }));
  }

  call(method, params = {}, { timeoutMs } = {}) {
    this.start();
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new BackendError('The local AstraQuote pricing request timed out.', {
          status: 503,
          code: 'local_pricing_bridge_timeout',
          retryable: true,
          details: { method },
        }));
      }, timeoutMs || this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(`${JSON.stringify({ id, method, params })}\n`, (error) => {
        if (!error) return;
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new BackendError('The local AstraQuote pricing request could not be sent.', {
          status: 503,
          code: 'local_pricing_bridge_write_failed',
          retryable: true,
        }));
      });
    });
  }

  request(requestPath, { timeoutMs } = {}) {
    if (requestPath !== '/api/mcp/v2/health') {
      throw new BackendError('Unsupported local compatibility request.', {
        status: 404,
        code: 'local_pricing_bridge_path_unsupported',
        details: { path: requestPath },
      });
    }
    return this.call('health', {}, { timeoutMs });
  }

  describeService(input) {
    return this.call('describe_service', input);
  }

  getAttributeValues(input) {
    return this.call('get_attribute_values', input);
  }

  searchProducts(input) {
    return this.call('search_products', input);
  }

  getPrices(input) {
    return this.call('get_prices', input);
  }

  close() {
    if (!this.child) return;
    this.child.kill('SIGTERM');
    this.child = null;
  }
}

module.exports = {
  DEFAULT_BACKEND_ROOT,
  DEFAULT_BRIDGE,
  LocalPricingClient,
  configuredBackendRoot,
  defaultPythonExecutable,
};
