'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { checkBranchTracking } = require('../lib/checks/branch-tracking');

function fakeExec(responses) {
  return async (cmd) => {
    for (const [pattern, output] of responses) {
      if (cmd.includes(pattern)) {
        if (output instanceof Error) throw output;
        return output;
      }
    }
    throw new Error(`fakeExec: no stub for "${cmd}"`);
  };
}

test('checkBranchTracking: "matches" when upstream equals the remote default branch', async () => {
  const exec = fakeExec([
    ['@{u}', 'origin/main\n'],
    ['refs/remotes/origin/HEAD', 'refs/remotes/origin/main\n'],
  ]);
  assert.equal(await checkBranchTracking(exec), 'matches');
});

test('checkBranchTracking: "stale" after a server-side default-branch rename leaves the tracking ref mismatched', async () => {
  // Local checkout still tracks "origin/master"; the remote's default
  // branch was renamed to "main" — this is exactly the V1 E2E finding
  // graph-process-engine.md's Objective describes.
  const exec = fakeExec([
    ['@{u}', 'origin/master\n'],
    ['refs/remotes/origin/HEAD', 'refs/remotes/origin/main\n'],
  ]);
  assert.equal(await checkBranchTracking(exec), 'stale');
});

test('checkBranchTracking: "probe-error" when git itself fails (e.g. no upstream configured)', async () => {
  const exec = fakeExec([['@{u}', new Error('fatal: no upstream configured for branch')]]);
  assert.equal(await checkBranchTracking(exec), 'probe-error');
});
