#!/usr/bin/env node
'use strict';

// Scheduled Docker image/layer cache pruning, invoked nightly by
// the jenkins-cache-retention-nightly job, and also callable directly by
// the threshold sweep (disk-usage-sweep.js).
//
// The in-progress-build guarantee for this surface is not something this
// script re-implements — see the comment on buildDockerPruneCommand() in
// lib/retention-policy.js: `docker system prune` without `-a` never
// removes an image or build-cache layer attached to an existing container
// (running or stopped), which is exactly what protects a currently-running
// build's image from a concurrent prune.
//
// Usage: node prune-docker-cache.js [--trigger=scheduled-docker|threshold-sweep]

const { execFileSync } = require('node:child_process');
const { buildDockerPruneCommand, parseDockerReclaimedBytes } = require('./lib/retention-policy');
const { appendPruneRecord } = require('./lib/retention-log');

/**
 * @param {object} [opts]
 * @param {typeof execFileSync} [opts.execFn] - injectable for tests
 * @param {string} [opts.trigger]
 * @returns {{stdout: string, reclaimedBytes: number|null}}
 */
function run(opts = {}) {
  const execFn = opts.execFn ?? execFileSync;
  const trigger = opts.trigger ?? 'scheduled-docker';
  const [cmd, ...cmdArgs] = buildDockerPruneCommand();

  const stdout = execFn(cmd, cmdArgs, { encoding: 'utf8' });
  const reclaimedBytes = parseDockerReclaimedBytes(stdout);

  console.log(stdout.trim());
  console.log(`prune-docker-cache: reclaimed ${reclaimedBytes ?? 'unknown'} bytes`);
  appendPruneRecord({ trigger, command: [cmd, ...cmdArgs].join(' '), reclaimedBytes, stdout });

  return { stdout, reclaimedBytes };
}

if (require.main === module) {
  const triggerArg = process.argv.find((a) => a.startsWith('--trigger='));
  try {
    run({ trigger: triggerArg ? triggerArg.split('=')[1] : undefined });
  } catch (err) {
    console.error(`prune-docker-cache: fatal: ${err.stack || err.message}`);
    process.exitCode = 1;
  }
}

module.exports = { run };
