#!/usr/bin/env node
'use strict';

// Disk-usage-triggered safety sweep. Invoked on a >=30-minute
// cadence by the jenkins-disk-usage-sweep job (jenkins/jenkins.yaml).
// Checks current disk usage on the jenkins-data mount; if it's at or above
// the high watermark (default 85%), repeatedly re-runs the same
// prune-workspaces / prune-docker-cache logic used on their nightly
// schedule, until usage drops back below the low watermark
// (default 70%) or a small iteration cap is hit.
//
// This is deliberately the SAME pruning logic as the nightly job, just
// invoked out of band — one policy, two triggers: this sweep is a safety
// net on top of the nightly workspace/Docker-cache pruning, not a separate
// policy.
//
// Usage: node disk-usage-sweep.js
// Env: JENKINS_DATA_MOUNT default /var/jenkins_home

const { getUsedPercent } = require('./lib/disk-usage');
const { shouldSweep, evaluateSweepIteration } = require('./lib/retention-policy');
const { appendPruneRecord } = require('./lib/retention-log');
const pruneWorkspaces = require('./prune-workspaces');
const pruneDockerCache = require('./prune-docker-cache');

// `overrides` exists purely for testability (see test/disk-usage-sweep.test.js)
// so the iteration/stop-condition loop can be exercised without a real
// Docker daemon, a real Jenkins controller, or real disk pressure.
async function main(overrides = {}) {
  const getUsedPercentFn = overrides.getUsedPercentFn || getUsedPercent;
  const pruneWorkspacesFn = overrides.pruneWorkspacesFn || pruneWorkspaces.main;
  const pruneDockerFn = overrides.pruneDockerFn || pruneDockerCache.run;

  const mount = process.env.JENKINS_DATA_MOUNT || '/var/jenkins_home';
  let usedPercent = getUsedPercentFn(mount);

  console.log(`disk-usage-sweep: ${mount} at ${usedPercent}% used`);

  if (!shouldSweep(usedPercent, false)) {
    appendPruneRecord({ trigger: 'threshold-sweep', swept: false, usedPercentAtCheck: usedPercent });
    console.log('disk-usage-sweep: below high watermark, no action taken');
    return { swept: false, usedPercent };
  }

  console.log(`disk-usage-sweep: ${usedPercent}% at or above high watermark, sweeping`);

  let iteration = 0;
  let outcome;
  for (;;) {
    outcome = evaluateSweepIteration(usedPercent, iteration);
    if (outcome.done) break;

    iteration += 1;
    console.log(`disk-usage-sweep: iteration ${iteration}`);
    // eslint-disable-next-line no-await-in-loop
    await pruneWorkspacesFn(['--trigger=threshold-sweep']);
    try {
      pruneDockerFn({ trigger: 'threshold-sweep' });
    } catch (err) {
      console.error(`disk-usage-sweep: docker prune failed on iteration ${iteration}: ${err.message}`);
    }

    usedPercent = getUsedPercentFn(mount);
    console.log(`disk-usage-sweep: ${mount} now at ${usedPercent}% used`);
  }

  appendPruneRecord({
    trigger: 'threshold-sweep',
    swept: true,
    iterations: iteration,
    finalUsedPercent: usedPercent,
    reachedTarget: outcome.reason === 'below-target',
  });

  if (outcome.reason === 'max-iterations') {
    console.error(
      `disk-usage-sweep: stopped after ${iteration} iterations still at ${usedPercent}% — not enough prunable ` +
        'content to reach the low watermark; operator attention needed',
    );
  } else {
    console.log(`disk-usage-sweep: usage back to ${usedPercent}%, below low watermark after ${iteration} iteration(s)`);
  }

  return { swept: true, iterations: iteration, usedPercent, reachedTarget: outcome.reason === 'below-target' };
}

// Setting process.exitCode is a CLI-entry-point concern, not something
// main() itself should do -- main() is also called as a library function
// (by prune-workspaces... no, by disk-usage-sweep's own test suite, and
// conceptually could be composed by another script), and a reusable
// function mutating the whole process's exit code as a side effect of a
// "didn't fully succeed" return value is a surprise for any caller that
// isn't this file's own CLI block.
if (require.main === module) {
  main()
    .then((result) => {
      if (result && result.swept && !result.reachedTarget) process.exitCode = 1;
    })
    .catch((err) => {
      console.error(`disk-usage-sweep: fatal: ${err.stack || err.message}`);
      process.exitCode = 1;
    });
}

module.exports = { main };
