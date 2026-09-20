'use strict';

// disk-usage-sweep's own job is orchestrating the loop: check
// usage, sweep repeatedly while above the high/low watermark hysteresis,
// stop when below target or out of iterations. The hysteresis itself is
// unit tested directly in retention-policy.test.js; here the loop is
// exercised end to end with fake usage readings and fake prune calls that
// simulate reclaiming space, to prove the orchestration actually stops at
// the right time and calls the right things.

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

async function withTempLog(fn) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'jenkins-disk-sweep-log-'));
  const logPath = path.join(dir, 'prune-history.jsonl');
  process.env.RETENTION_LOG_PATH = logPath;
  delete require.cache[require.resolve('../scripts/lib/retention-log')];
  delete require.cache[require.resolve('../scripts/disk-usage-sweep')];
  try {
    return await fn(logPath);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
    delete process.env.RETENTION_LOG_PATH;
  }
}

// Reads the last appended record straight off disk -- retention-log.js only
// exposes the write side (appendPruneRecord); these tests verify what it
// wrote by reading the .jsonl file directly rather than through any
// library read-back helper.
function readLastRecord(logPath) {
  const lines = fs.readFileSync(logPath, 'utf8').split('\n').filter(Boolean);
  return JSON.parse(lines[lines.length - 1]);
}

test('disk-usage-sweep main(): below the high watermark takes no action and calls no prune functions', async () => {
  await withTempLog(async (logPath) => {
    const { main } = require('../scripts/disk-usage-sweep');
    let pruneCalls = 0;

    const result = await main({
      getUsedPercentFn: () => 50,
      pruneWorkspacesFn: () => {
        pruneCalls += 1;
      },
      pruneDockerFn: () => {
        pruneCalls += 1;
      },
    });

    assert.equal(result.swept, false);
    assert.equal(pruneCalls, 0);

    const record = readLastRecord(logPath);
    assert.equal(record.swept, false);
  });
});

test('disk-usage-sweep main(): at/above the high watermark sweeps and stops once usage drops below the low watermark', async () => {
  await withTempLog(async (logPath) => {
    const { main } = require('../scripts/disk-usage-sweep');
    // Usage readings: 90% (triggers), 78% (still sweeping, >= 70), 65% (done).
    const readings = [90, 78, 65];
    let call = 0;
    const usedPercentFn = () => readings[Math.min(call, readings.length - 1)];
    let pruneWorkspaceCalls = 0;
    let pruneDockerCalls = 0;

    const result = await main({
      getUsedPercentFn: () => {
        const v = usedPercentFn();
        call += 1;
        return v;
      },
      pruneWorkspacesFn: () => {
        pruneWorkspaceCalls += 1;
      },
      pruneDockerFn: () => {
        pruneDockerCalls += 1;
      },
    });

    assert.equal(result.swept, true);
    assert.equal(result.reachedTarget, true);
    assert.equal(result.iterations, 2, 'two sweep iterations: 90->78 (still sweeping), 78->65 (below target)');
    assert.equal(pruneWorkspaceCalls, 2);
    assert.equal(pruneDockerCalls, 2);

    const record = readLastRecord(logPath);
    assert.equal(record.swept, true);
    assert.equal(record.reachedTarget, true);
  });
});

test('disk-usage-sweep main(): gives up after the iteration cap when usage never drops, and reports failure', async () => {
  await withTempLog(async (logPath) => {
    const { main } = require('../scripts/disk-usage-sweep');
    const result = await main({
      getUsedPercentFn: () => 95, // never improves -- simulates "nothing left that's safe to prune"
      pruneWorkspacesFn: () => {},
      pruneDockerFn: () => {},
    });

    assert.equal(result.swept, true);
    assert.equal(result.reachedTarget, false);
    assert.ok(result.iterations >= 1);

    const record = readLastRecord(logPath);
    assert.equal(record.reachedTarget, false);
  });
});

test('disk-usage-sweep main(): a failing docker prune during a sweep iteration does not abort the whole sweep', async () => {
  await withTempLog(async () => {
    const { main } = require('../scripts/disk-usage-sweep');
    const readings = [90, 60];
    let call = 0;

    const result = await main({
      getUsedPercentFn: () => {
        const v = readings[Math.min(call, readings.length - 1)];
        call += 1;
        return v;
      },
      pruneWorkspacesFn: () => {},
      pruneDockerFn: () => {
        throw new Error('Cannot connect to the Docker daemon');
      },
    });

    assert.equal(result.swept, true);
    assert.equal(result.reachedTarget, true, 'workspace pruning alone still got usage under target even though docker prune errored');
  });
});
