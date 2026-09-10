'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

class QuoteStoreError extends Error {
  constructor(message, { code = 'quote_store_error', details = {} } = {}) {
    super(message);
    this.name = 'QuoteStoreError';
    this.code = code;
    this.details = details;
  }
}

class V2QuoteStore {
  constructor({ directory = process.env.ASTRAQUOTE_V2_STATE_DIR || '/data/v2-quotes' } = {}) {
    this.directory = directory;
    fs.mkdirSync(this.directory, { recursive: true, mode: 0o700 });
  }

  idempotencyPath(key) {
    const digest = crypto.createHash('sha256').update(key).digest('hex');
    return path.join(this.directory, `idempotency-${digest}.json`);
  }

  quotePath(quoteId) {
    if (!/^aqv2_[a-f0-9-]{36}$/.test(quoteId)) {
      throw new QuoteStoreError('Invalid quote ID.', { code: 'quote_id_invalid' });
    }
    return path.join(this.directory, `${quoteId}.json`);
  }

  priceBatchPath(batchId) {
    if (!/^aqpb_[a-f0-9-]{36}$/.test(batchId)) {
      throw new QuoteStoreError('Invalid price batch ID.', { code: 'price_batch_id_invalid' });
    }
    return path.join(this.directory, `${batchId}.json`);
  }

  putPriceBatch(record) {
    this.writeAtomic(this.priceBatchPath(record.price_batch_id), record);
  }

  getPriceBatch(batchId) {
    const target = this.priceBatchPath(batchId);
    if (!fs.existsSync(target)) {
      throw new QuoteStoreError('Official price batch was not found.', {
        code: 'price_batch_not_found', details: { price_batch_id: batchId },
      });
    }
    return JSON.parse(fs.readFileSync(target, 'utf8'));
  }

  findByIdempotencyKey(key) {
    const indexPath = this.idempotencyPath(key);
    if (!fs.existsSync(indexPath)) return null;
    const index = JSON.parse(fs.readFileSync(indexPath, 'utf8'));
    return this.get(index.quote_id);
  }

  put(record) {
    const target = this.quotePath(record.quote_id);
    this.writeAtomic(target, record);
    this.writeAtomic(this.idempotencyPath(record.idempotency_key), {
      quote_id: record.quote_id,
      created_at: record.created_at,
    });
  }

  get(quoteId) {
    const target = this.quotePath(quoteId);
    if (!fs.existsSync(target)) {
      throw new QuoteStoreError('Quote was not found.', {
        code: 'quote_not_found', details: { quote_id: quoteId },
      });
    }
    return JSON.parse(fs.readFileSync(target, 'utf8'));
  }

  update(quoteId, changes) {
    const current = this.get(quoteId);
    const updated = { ...current, ...changes, quote_id: current.quote_id };
    this.writeAtomic(this.quotePath(quoteId), updated);
    return updated;
  }

  writeAtomic(target, value) {
    const temporary = `${target}.${process.pid}.${crypto.randomBytes(6).toString('hex')}.tmp`;
    fs.writeFileSync(temporary, `${JSON.stringify(value)}\n`, { encoding: 'utf8', mode: 0o600 });
    fs.renameSync(temporary, target);
  }
}

module.exports = { QuoteStoreError, V2QuoteStore };
