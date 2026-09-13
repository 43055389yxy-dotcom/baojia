'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { V2QuoteStore } = require('../lib/v2-quote-store');

test('price batches and checkpoints remain readable by the desktop relay owner', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-v2-owner-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const uid = typeof process.getuid === 'function' ? process.getuid() : undefined;
  const gid = typeof process.getgid === 'function' ? process.getgid() : undefined;
  const store = new V2QuoteStore({ directory, ownerUid: uid, ownerGid: gid });
  const priceBatchId = 'aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
  const relayJobId = 'gpt-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';

  store.putPriceBatch({ price_batch_id: priceBatchId, component_lifecycle: [] });
  store.putCheckpoint(relayJobId, { stage: 'pricing_partial' });

  for (const target of [store.priceBatchPath(priceBatchId), store.relayPath(relayJobId)]) {
    const stat = fs.statSync(target);
    if (uid !== undefined) assert.equal(stat.uid, uid);
    if (gid !== undefined) assert.equal(stat.gid, gid);
    assert.equal(stat.mode & 0o777, 0o600);
    assert.doesNotThrow(() => JSON.parse(fs.readFileSync(target, 'utf8')));
  }
});

test('startup repairs ownership of existing JSON state', (t) => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'astraquote-v2-repair-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const existing = path.join(directory, 'aqpb_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.json');
  fs.writeFileSync(existing, '{}\n', { mode: 0o600 });
  const uid = typeof process.getuid === 'function' ? process.getuid() : undefined;
  const gid = typeof process.getgid === 'function' ? process.getgid() : undefined;

  new V2QuoteStore({ directory, ownerUid: uid, ownerGid: gid });

  const stat = fs.statSync(existing);
  if (uid !== undefined) assert.equal(stat.uid, uid);
  if (gid !== undefined) assert.equal(stat.gid, gid);
});
