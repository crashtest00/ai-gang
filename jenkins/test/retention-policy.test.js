'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
  DAY_MS,
  selectWorkspacesToPrune,
  buildDockerPruneCommand,
  parseDockerReclaimedBytes,
  shouldSweep,
  evaluateSweepIteration,
} = require('../scripts/lib/retention-policy');

const NOW = Date.parse('2026-09-07T12:00:00Z');

function ws({ job, branch = null, mtimeDaysAgo, building = false }) {
  return {
    job,
    branch,
    path: `/workspace/${job}${branch ? '/' + branch : ''}`,
    mtimeMs: NOW - mtimeDaysAgo * DAY_MS,
    building,
  };
}

// ---- selectWorkspacesToPrune ------------------------------------------

test('selectWorkspacesToPrune: nothing to prune when everything is recent and under the count cap', () => {
  const workspaces = [
    ws({ job: 'hello-world-pipeline', branch: 'dev', mtimeDaysAgo: 1 }),
    ws({ job: 'hello-world-pipeline', branch: 'PR-3', mtimeDaysAgo: 2 }),
  ];
  const { keep, prune } = selectWorkspacesToPrune(workspaces, { now: NOW });
  assert.equal(prune.length, 0);
  assert.equal(keep.length, 2);
});

test('selectWorkspacesToPrune: prunes nothing when every candidate is an in-progress build', () => {
  // Both are well past the age and count thresholds, but both are building.
  const workspaces = [
    ws({ job: 'hello-world-pipeline', branch: 'dev', mtimeDaysAgo: 30, building: true }),
    ws({ job: 'hello-world-pipeline', branch: 'PR-3', mtimeDaysAgo: 30, building: true }),
    ws({ job: 'hello-world-pipeline', branch: 'PR-4', mtimeDaysAgo: 30, building: true }),
    ws({ job: 'hello-world-pipeline', branch: 'PR-5', mtimeDaysAgo: 30, building: true }),
    ws({ job: 'hello-world-pipeline', branch: 'PR-6', mtimeDaysAgo: 30, building: true }),
    ws({ job: 'hello-world-pipeline', branch: 'PR-7', mtimeDaysAgo: 30, building: true }),
  ];
  const { keep, prune } = selectWorkspacesToPrune(workspaces, { now: NOW });
  assert.equal(prune.length, 0);
  assert.equal(keep.length, 6);
  assert.ok(keep.every((k) => k.reason === 'active-build'));
});

test('selectWorkspacesToPrune: age threshold is exactly at the 14-day boundary (not yet eligible)', () => {
  const workspaces = [ws({ job: 'hello-world-pipeline', branch: 'dev', mtimeDaysAgo: 14 })];
  const { keep, prune } = selectWorkspacesToPrune(workspaces, { now: NOW, maxAgeDays: 14 });
  assert.equal(prune.length, 0, 'exactly 14 days old must not be pruned — only strictly older than 14 days');
  assert.equal(keep.length, 1);
});

test('selectWorkspacesToPrune: one tick past the age boundary is eligible', () => {
  const workspaces = [ws({ job: 'hello-world-pipeline', branch: 'dev', mtimeDaysAgo: 14.01 })];
  const { prune } = selectWorkspacesToPrune(workspaces, { now: NOW, maxAgeDays: 14 });
  assert.equal(prune.length, 1);
  assert.equal(prune[0].reason, 'over-age');
});

test('selectWorkspacesToPrune: count threshold is exactly at the 5-kept boundary (nothing pruned by count)', () => {
  const workspaces = [1, 2, 3, 4, 5].map((n) => ws({ job: 'hello-world-pipeline', branch: `PR-${n}`, mtimeDaysAgo: n }));
  const { prune } = selectWorkspacesToPrune(workspaces, { now: NOW, maxKeepPerJob: 5 });
  assert.equal(prune.length, 0, 'exactly 5 workspaces for one job must all be kept');
});

test('selectWorkspacesToPrune: the 6th most-recent workspace for a job is pruned by count', () => {
  const workspaces = [1, 2, 3, 4, 5, 6].map((n) => ws({ job: 'hello-world-pipeline', branch: `PR-${n}`, mtimeDaysAgo: n }));
  const { keep, prune } = selectWorkspacesToPrune(workspaces, { now: NOW, maxKeepPerJob: 5 });
  assert.equal(prune.length, 1);
  assert.equal(prune[0].branch, 'PR-6', 'the oldest (6th most-recent) branch workspace is the one over the count cap');
  assert.equal(prune[0].reason, 'over-count');
  assert.equal(keep.length, 5);
});

test('selectWorkspacesToPrune: a mix of in-progress and stale entries prunes only the stale, non-building ones', () => {
  const workspaces = [
    ws({ job: 'hello-world-pipeline', branch: 'dev', mtimeDaysAgo: 1 }), // recent -> kept
    ws({ job: 'hello-world-pipeline', branch: 'PR-1', mtimeDaysAgo: 20 }), // stale, not building -> pruned
    ws({ job: 'hello-world-pipeline', branch: 'PR-2', mtimeDaysAgo: 20, building: true }), // stale but building -> kept
    ws({ job: 'release-candidate', mtimeDaysAgo: 20 }), // singleton job, stale -> pruned
  ];

  const { keep, prune } = selectWorkspacesToPrune(workspaces, { now: NOW });
  const prunedKeys = prune.map((p) => p.branch || p.job).sort();
  assert.deepEqual(prunedKeys, ['PR-1', 'release-candidate'].sort());

  const keptBuilding = keep.find((k) => k.branch === 'PR-2');
  assert.ok(keptBuilding, 'the building workspace must be kept even though it is stale');
  assert.equal(keptBuilding.reason, 'active-build');
});

test('selectWorkspacesToPrune: building always wins even when both over-age and over-count', () => {
  // Every entry is past the 14-day age threshold AND the oldest (PR-6) is
  // also past the 5-kept count threshold -- it would be pruned twice over
  // if it weren't building.
  const workspaces = [1, 2, 3, 4, 5, 6].map((n) =>
    ws({ job: 'hello-world-pipeline', branch: `PR-${n}`, mtimeDaysAgo: 20 + n, building: n === 6 }),
  );
  const { keep, prune } = selectWorkspacesToPrune(workspaces, { now: NOW, maxKeepPerJob: 5 });
  assert.ok(!prune.some((p) => p.branch === 'PR-6'), 'the building workspace is excluded even though it is both oldest and over the count cap');
  assert.ok(keep.some((k) => k.branch === 'PR-6' && k.reason === 'active-build'));
  assert.equal(prune.length, 5, 'the other five are all over-age and get pruned');
  assert.ok(prune.every((p) => p.reason === 'over-age' || p.reason === 'over-age-and-over-count'));
});

test('selectWorkspacesToPrune: keeps job groups independent — one job over its cap does not affect another', () => {
  const workspaces = [
    ...[1, 2, 3, 4, 5, 6].map((n) => ws({ job: 'job-a', branch: `PR-${n}`, mtimeDaysAgo: n })),
    ws({ job: 'job-b', branch: 'dev', mtimeDaysAgo: 1 }),
  ];
  const { prune } = selectWorkspacesToPrune(workspaces, { now: NOW, maxKeepPerJob: 5 });
  assert.equal(prune.length, 1);
  assert.equal(prune[0].job, 'job-a');
});

// ---- buildDockerPruneCommand / parseDockerReclaimedBytes ---------------

test('buildDockerPruneCommand: default is docker system prune -f --filter until=72h', () => {
  assert.deepEqual(buildDockerPruneCommand(), ['docker', 'system', 'prune', '-f', '--filter', 'until=72h']);
});

test('buildDockerPruneCommand: honors a custom boundary', () => {
  assert.deepEqual(buildDockerPruneCommand({ untilHours: 24 }), ['docker', 'system', 'prune', '-f', '--filter', 'until=24h']);
});

test('buildDockerPruneCommand: rejects a non-positive boundary', () => {
  assert.throws(() => buildDockerPruneCommand({ untilHours: 0 }));
  assert.throws(() => buildDockerPruneCommand({ untilHours: -5 }));
});

test('parseDockerReclaimedBytes: parses GB/MB/KB and bare bytes', () => {
  assert.equal(parseDockerReclaimedBytes('Total reclaimed space: 1.5GB'), 1_500_000_000);
  assert.equal(parseDockerReclaimedBytes('Total reclaimed space: 512MB'), 512_000_000);
  assert.equal(parseDockerReclaimedBytes('Total reclaimed space: 0B'), 0);
});

test('parseDockerReclaimedBytes: returns null when the line is absent', () => {
  assert.equal(parseDockerReclaimedBytes('nothing here'), null);
});

// ---- shouldSweep / evaluateSweepIteration -------------------------------

test('shouldSweep: starts a sweep once usage reaches the high watermark (boundary inclusive)', () => {
  assert.equal(shouldSweep(85, false), true);
  assert.equal(shouldSweep(84, false), false);
});

test('shouldSweep: continues an in-progress sweep until strictly below the low watermark', () => {
  assert.equal(shouldSweep(70, true), true, '70% while sweeping must still count as sweeping (< 70 is the exit, not <=)');
  assert.equal(shouldSweep(69, true), false);
});

test('shouldSweep: usage between watermarks with no sweep in progress takes no action', () => {
  assert.equal(shouldSweep(75, false), false);
});

test('evaluateSweepIteration: stops once usage is below the low watermark', () => {
  const result = evaluateSweepIteration(69, 1);
  assert.deepEqual(result, { done: true, reason: 'below-target' });
});

test('evaluateSweepIteration: keeps going while above target and under the iteration cap', () => {
  const result = evaluateSweepIteration(80, 1, { maxIterations: 5 });
  assert.deepEqual(result, { done: false, reason: 'continue' });
});

test('evaluateSweepIteration: stops at the iteration cap even if still above target', () => {
  const result = evaluateSweepIteration(90, 5, { maxIterations: 5 });
  assert.deepEqual(result, { done: true, reason: 'max-iterations' });
});
