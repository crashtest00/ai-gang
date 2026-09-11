#!/usr/bin/env node
'use strict';

// Scheduled workspace pruning, invoked nightly by the
// jenkins-cache-retention-nightly job (jenkins/jenkins.yaml), and also
// callable directly by the threshold sweep (disk-usage-sweep.js) — the two
// triggers share this exact logic: one policy, two triggers.
//
// Impure orchestration only: all actual prune/keep decisions come from
// lib/retention-policy.js. This file's job is to gather real inputs
// (on-disk workspaces, which ones are currently building) and carry out
// the decision (delete, log).
//
// Usage:
//   node prune-workspaces.js [--dry-run] [--trigger=scheduled-workspace|threshold-sweep]
//
// Env:
//   WORKSPACE_ROOT           default /var/jenkins_home/workspace
//   JENKINS_INTERNAL_URL     default http://localhost:8080 -- deliberately
//     NOT the JENKINS_URL env var jenkins/docker-compose.yml already
//     defines (that's the public-facing URL used in Jira links/email
//     footers, e.g. behind a Cloudflare tunnel per setup/JenkinsConfig.md,
//     and may not even be reachable from inside this same container). This
//     script always wants to reach the controller's own local port.
//   JENKINS_ADMIN_USER       default admin
//   JENKINS_ADMIN_PASSWORD   (required to query the API with auth enabled)
//   RETENTION_LOG_PATH       see lib/retention-log.js

const { discoverWorkspaces, directorySizeBytes, removeWorkspace } = require('./lib/workspace-fs');
const { fetchJenkinsJobState } = require('./lib/jenkins-api');
const { selectWorkspacesToPrune } = require('./lib/retention-policy');
const { appendPruneRecord } = require('./lib/retention-log');

function workspaceKey(ws) {
  return ws.branch ? `${ws.job}/${ws.branch}` : ws.job;
}

// `overrides` exists purely for testability (see test/prune-workspaces.test.js):
// production callers never need it, since the default `fetchFn` (global
// fetch) and default `WORKSPACE_ROOT`/`JENKINS_INTERNAL_URL` env vars are
// correct for the real Jenkins container this runs in.
async function main(argv = process.argv.slice(2), overrides = {}) {
  const args = argv;
  const dryRun = args.includes('--dry-run');
  const triggerArg = args.find((a) => a.startsWith('--trigger='));
  const trigger = triggerArg ? triggerArg.split('=')[1] : 'scheduled-workspace';

  const root = overrides.workspaceRoot || process.env.WORKSPACE_ROOT || '/var/jenkins_home/workspace';
  const jenkinsUrl = process.env.JENKINS_INTERNAL_URL || 'http://localhost:8080';

  // Ask Jenkins first: which jobs are multibranch parents (needed to walk
  // the workspace tree correctly at all) and which job/branches are
  // currently building both come from the same API call.
  let buildingKeys, multibranchJobNames;
  try {
    ({ buildingKeys, multibranchJobNames } = await fetchJenkinsJobState({
      jenkinsUrl,
      user: process.env.JENKINS_ADMIN_USER || 'admin',
      token: process.env.JENKINS_ADMIN_PASSWORD,
      fetchFn: overrides.fetchFn,
    }));
  } catch (err) {
    // If we can't reliably learn what's building, the only safe behavior
    // (never pruning outranks staying on cadence) is to prune nothing this
    // run rather than guess.
    console.error(`prune-workspaces: could not query Jenkins for in-progress builds (${err.message}); skipping this run to avoid pruning an active build's workspace`);
    appendPruneRecord({ trigger, skipped: true, reason: 'jenkins-api-unreachable' });
    return;
  }

  const rawWorkspaces = discoverWorkspaces(root, { multibranchJobNames });
  const workspaces = rawWorkspaces.map((ws) => ({ ...ws, building: buildingKeys.has(workspaceKey(ws)) }));
  const { prune } = selectWorkspacesToPrune(workspaces);

  let reclaimedBytes = 0;
  const removed = [];
  for (const ws of prune) {
    const size = directorySizeBytes(ws.path);
    if (!dryRun) removeWorkspace(ws.path);
    reclaimedBytes += size;
    removed.push({ job: ws.job, branch: ws.branch, path: ws.path, reason: ws.reason, bytes: size });
    console.log(`${dryRun ? '[dry-run] would remove' : 'removed'} ${ws.path} (${ws.reason}, ${size} bytes)`);
  }

  console.log(`prune-workspaces: ${removed.length} workspace(s) removed, ${reclaimedBytes} bytes reclaimed`);
  appendPruneRecord({ trigger, dryRun, removed, reclaimedBytes });
}

if (require.main === module) {
  main().catch((err) => {
    console.error(`prune-workspaces: fatal: ${err.stack || err.message}`);
    process.exitCode = 1;
  });
}

module.exports = { main, workspaceKey };
