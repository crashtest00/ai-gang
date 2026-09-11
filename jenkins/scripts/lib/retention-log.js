'use strict';

// Shared append-only prune-history log (REQ-05): every scheduled or
// threshold-triggered prune run — workspace pruning, Docker cache pruning,
// or a threshold sweep — appends one JSON line here, so the outcome of any
// run is on disk without SSHing in and inspecting the volume/Docker state
// by hand.
//
// Split from retention-policy.js because this module does real file I/O
// (impure); retention-policy.js stays pure and unit-testable in isolation.
// This module is exercised by integration tests against a temp file
// instead.

const fs = require('node:fs');
const path = require('node:path');

const DEFAULT_LOG_PATH = process.env.RETENTION_LOG_PATH || '/var/jenkins_home/retention/prune-history.jsonl';

/**
 * Append one prune-run record to the log. Never throws on a logging
 * failure by default (a log write must not fail the prune job itself);
 * pass `strict: true` to opt into propagating the error, which the test
 * suite uses to assert failure behavior.
 *
 * @param {object} record
 * @param {string} record.trigger - 'scheduled-workspace' | 'scheduled-docker' | 'threshold-sweep'
 * @param {string} [logPath]
 * @param {object} [opts]
 * @param {boolean} [opts.strict]
 */
function appendPruneRecord(record, logPath = DEFAULT_LOG_PATH, opts = {}) {
  const line = JSON.stringify({ timestamp: new Date().toISOString(), ...record }) + '\n';
  try {
    fs.mkdirSync(path.dirname(logPath), { recursive: true });
    fs.appendFileSync(logPath, line);
  } catch (err) {
    if (opts.strict) throw err;
    // eslint-disable-next-line no-console
    console.error(`retention-log: failed to write ${logPath}: ${err.message}`);
  }
}

module.exports = { DEFAULT_LOG_PATH, appendPruneRecord };
