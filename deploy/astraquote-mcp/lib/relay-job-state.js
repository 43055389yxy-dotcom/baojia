'use strict';

const LEGACY_RECOVERABLE_FAILURE_CODES = new Set([
  'gpt_quote_worker_stale',
]);

function canFinalizeRelayJob(job) {
  if (job?.status === 'processing') return true;
  // Compatibility for jobs that an older sales poller permanently failed
  // after a short browser-worker heartbeat gap. No other failure is revived.
  return job?.status === 'failed'
    && LEGACY_RECOVERABLE_FAILURE_CODES.has(String(job?.error?.code || ''));
}

module.exports = {
  canFinalizeRelayJob,
};
