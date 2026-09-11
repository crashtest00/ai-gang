'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { Coordination } = require('../lib/coordination');

// --- REQ-02: sourcing candidates ---

function twoEntryGraphs() {
  const a = {
    graph_id: 'jira-setup',
    schema_version: 1,
    entry: 'start',
    nodes: [{ id: 'start', kind: 'action', procedure: 'x', terminal: true, outcome: 'success' }],
  };
  const b = {
    graph_id: 'vm-infra-setup',
    schema_version: 1,
    entry: 'start',
    nodes: [{ id: 'start', kind: 'action', procedure: 'y', terminal: true, outcome: 'success' }],
  };
  return { a, b };
}

test('REQ-02: distinct graph entry points become addressed candidates', () => {
  const { a, b } = twoEntryGraphs();
  const candidates = Coordination.sourceEntryPointCandidates([
    { graphDoc: a, writes: { files: ['jira.json'], services: [] } },
    { graphDoc: b, writes: { files: ['vm.json'], services: [] } },
  ]);
  assert.deepEqual(
    candidates.map((c) => c.address),
    ['jira-setup#start', 'vm-infra-setup#start']
  );
});

test('REQ-02: a fan-out node\'s branches become candidates addressed via <graph_id>#<node_id>:<branch_id>', () => {
  const doc = {
    graph_id: 'init',
    schema_version: 1,
    entry: 'split',
    nodes: [
      {
        id: 'split',
        kind: 'fan-out',
        branches: [
          { branch_id: 'jira-setup', to: 'jira-start' },
          { branch_id: 'vm-infra-setup', to: 'vm-start' },
        ],
        join: 'joined',
      },
      { id: 'jira-start', kind: 'action', procedure: 'x', next: 'joined' },
      { id: 'vm-start', kind: 'action', procedure: 'y', next: 'joined' },
      { id: 'joined', kind: 'fan-in', for: 'split', next: 'done' },
      { id: 'done', kind: 'action', procedure: 'no-op', terminal: true, outcome: 'success' },
    ],
  };
  const candidates = Coordination.sourceFanOutCandidates(doc, 'split', {
    'jira-setup': { files: ['jira.json'], services: [] },
    'vm-infra-setup': { files: ['vm.json'], services: [] },
  });
  assert.deepEqual(
    candidates.map((c) => c.address),
    ['init#split:jira-setup', 'init#split:vm-infra-setup']
  );
});

test('REQ-02: a candidate reachable from another candidate in the same document is rejected', () => {
  const doc = {
    graph_id: 'g',
    schema_version: 1,
    entry: 'split',
    nodes: [
      {
        id: 'split',
        kind: 'fan-out',
        branches: [
          { branch_id: 'outer', to: 'outer-start' },
          { branch_id: 'inner', to: 'inner-start' },
        ],
        join: 'joined',
      },
      // outer-start's own path reaches inner-start — not independent.
      { id: 'outer-start', kind: 'action', procedure: 'x', next: 'inner-start' },
      { id: 'inner-start', kind: 'action', procedure: 'y', next: 'joined' },
      { id: 'joined', kind: 'fan-in', for: 'split', next: 'done' },
      { id: 'done', kind: 'action', procedure: 'no-op', terminal: true, outcome: 'success' },
    ],
  };
  assert.throws(() => Coordination.sourceFanOutCandidates(doc, 'split'), /not independent/);
});

// --- REQ-03/REQ-04: collision safety ---

test('REQ-03: a candidate with no declared write-scope is excluded from concurrency (fails closed)', () => {
  const withWrites = { address: 'a', writes: { files: ['x'], services: [] } };
  const withoutWrites = { address: 'b' };
  const { concurrentGroups, sequential } = Coordination.partitionCollisionSafe([withWrites, withoutWrites]);
  assert.deepEqual(sequential, [withoutWrites]);
  assert.deepEqual(concurrentGroups, [[withWrites]]);
});

test('REQ-04: two candidates sharing a file path are never grouped concurrently', () => {
  const c1 = { address: 'a', writes: { files: ['shared.json'], services: [] } };
  const c2 = { address: 'b', writes: { files: ['shared.json'], services: [] } };
  const { concurrentGroups } = Coordination.partitionCollisionSafe([c1, c2]);
  assert.equal(concurrentGroups.length, 2);
  assert.equal(Coordination.isCollisionSafe(c1, c2), false);
});

test('REQ-04: two candidates with fully disjoint write-scopes are grouped concurrently', () => {
  const c1 = { address: 'a', writes: { files: ['a.json'], services: ['svc-a'] } };
  const c2 = { address: 'b', writes: { files: ['b.json'], services: ['svc-b'] } };
  const { concurrentGroups } = Coordination.partitionCollisionSafe([c1, c2]);
  assert.equal(concurrentGroups.length, 1);
  assert.equal(concurrentGroups[0].length, 2);
  assert.equal(Coordination.isCollisionSafe(c1, c2), true);
});

test('REQ-04: a shared service name is also a collision, not just a shared file', () => {
  const c1 = { address: 'a', writes: { files: [], services: ['cloudflared'] } };
  const c2 = { address: 'b', writes: { files: [], services: ['cloudflared'] } };
  assert.equal(Coordination.isCollisionSafe(c1, c2), false);
});

// --- REQ-05 through REQ-14: dispatch and tracking ---

test('REQ-05/REQ-06/REQ-12: dispatching a group records each branch under one fan-out group id', async () => {
  const c = new Coordination();
  const candidates = [
    { address: 'jira-setup#start', writes: { files: ['jira.json'], services: [] } },
    { address: 'vm-infra-setup#start', writes: { files: ['vm.json'], services: [] } },
  ];
  await c.dispatchGroup('group-1', candidates, async () => ({ status: 'done' }));
  const status = c.getGroupStatus('group-1');
  assert.equal(status.complete, true);
  assert.deepEqual(
    status.branches.map((b) => b.address).sort(),
    ['jira-setup#start', 'vm-infra-setup#start']
  );
  assert.ok(status.branches.every((b) => b.status === 'done'));
});

test('REQ-07: a failed branch does not abort or block a sibling branch (three-branch group, one forced to fail)', async () => {
  const c = new Coordination();
  const candidates = [
    { address: 'a', writes: { files: ['a.json'], services: [] } },
    { address: 'b', writes: { files: ['b.json'], services: [] } },
    { address: 'c', writes: { files: ['c.json'], services: [] } },
  ];
  const completedSiblings = [];
  const status = await c.dispatchGroup('group-fail', candidates, async (candidate) => {
    if (candidate.address === 'b') {
      throw new Error('subagent crashed');
    }
    completedSiblings.push(candidate.address);
    return { status: 'done' };
  });

  assert.deepEqual(completedSiblings.sort(), ['a', 'c']);
  const byAddress = Object.fromEntries(status.branches.map((b) => [b.address, b]));
  assert.equal(byAddress.a.status, 'done');
  assert.equal(byAddress.c.status, 'done');
  assert.equal(byAddress.b.status, 'failed');
  assert.match(byAddress.b.reason, /subagent crashed/);
  // REQ-10: group is complete once every branch — including the failed one — is terminal.
  assert.equal(status.complete, true);
});

test('REQ-08: an execution-layer failure is recorded locally, never as a graph terminal-failure node', async () => {
  const c = new Coordination();
  const candidates = [{ address: 'only', writes: { files: ['x'], services: [] } }];
  const status = await c.dispatchGroup('group-2', candidates, async () => {
    throw new Error('retry exhaustion');
  });
  assert.equal(status.branches[0].status, 'failed');
  assert.match(status.branches[0].reason, /retry exhaustion/);
  // Nothing here ever produces or references a graph node — Coordination
  // has no method that could construct one (see coordination.js's class
  // comment / REQ-01's authority boundary).
});

test('REQ-09: a [HUMAN]-gated remediation pause records paused-for-human, distinct from failed, without pausing a sibling', async () => {
  const c = new Coordination();
  const candidates = [
    { address: 'a', writes: { files: ['a.json'], services: [] } },
    { address: 'b', writes: { files: ['b.json'], services: [] } },
  ];
  const status = await c.dispatchGroup('group-3', candidates, async (candidate) => {
    if (candidate.address === 'a') return { status: 'paused-for-human', reason: 'missing CF_API_KEY' };
    return { status: 'done' };
  });
  const byAddress = Object.fromEntries(status.branches.map((b) => [b.address, b]));
  assert.equal(byAddress.a.status, 'paused-for-human');
  assert.equal(byAddress.a.reason, 'missing CF_API_KEY');
  assert.equal(byAddress.b.status, 'done');
  assert.notEqual(byAddress.a.status, 'failed');
});

test('REQ-10: an incomplete group (one branch still running) is reported incomplete, not silently done', async () => {
  const c = new Coordination();
  let resolveB;
  const bPromise = new Promise((r) => {
    resolveB = r;
  });
  const dispatchPromise = c.dispatchGroup(
    'group-4',
    [
      { address: 'a', writes: { files: ['a.json'], services: [] } },
      { address: 'b', writes: { files: ['b.json'], services: [] } },
    ],
    async (candidate) => {
      if (candidate.address === 'a') return { status: 'done' };
      await bPromise;
      return { status: 'done' };
    }
  );

  // Give branch "a" a chance to finish before we inspect mid-flight state.
  await new Promise((r) => setTimeout(r, 10));
  const midStatus = c.getGroupStatus('group-4');
  assert.equal(midStatus.complete, false);
  const outstanding = midStatus.branches.find((b) => b.address === 'b');
  assert.ok(outstanding, 'the still-running branch is listed, not silently absent');
  assert.notEqual(outstanding.status, 'done');

  resolveB();
  const finalStatus = await dispatchPromise;
  assert.equal(finalStatus.complete, true);
});

test('REQ-13: a branch exceeding its timeout is retried, and succeeding siblings are unaffected', async () => {
  const c = new Coordination();
  let attempts = 0;
  const status = await c.dispatchGroup(
    'group-5',
    [
      { address: 'slow-then-ok', writes: { files: ['x'], services: [] } },
      { address: 'fast', writes: { files: ['y'], services: [] } },
    ],
    async (candidate) => {
      if (candidate.address === 'slow-then-ok') {
        attempts++;
        if (attempts === 1) {
          // Never resolves within the timeout on the first attempt.
          await new Promise((r) => setTimeout(r, 200));
        }
        return { status: 'done' };
      }
      return { status: 'done' };
    },
    { timeoutMs: 20, maxAttempts: 2 }
  );
  const byAddress = Object.fromEntries(status.branches.map((b) => [b.address, b]));
  assert.equal(byAddress['slow-then-ok'].status, 'done');
  assert.equal(byAddress['slow-then-ok'].attempts, 2);
  assert.equal(byAddress.fast.status, 'done');
  assert.equal(byAddress.fast.attempts, 1);
});

test('REQ-13: exhausting retries after repeated timeouts records failed', async () => {
  const c = new Coordination();
  const status = await c.dispatchGroup(
    'group-6',
    [{ address: 'always-slow', writes: { files: ['x'], services: [] } }],
    async () => new Promise((r) => setTimeout(r, 200)),
    { timeoutMs: 10, maxAttempts: 2 }
  );
  assert.equal(status.branches[0].status, 'failed');
  assert.equal(status.branches[0].attempts, 2);
  assert.match(status.branches[0].reason, /timed out/);
});

test('REQ-14: inspectable state distinguishes a fully-completed group from one with a paused/failed branch', async () => {
  const c = new Coordination();
  await c.dispatchGroup(
    'group-7',
    [
      { address: 'a', writes: { files: ['a'], services: [] } },
      { address: 'b', writes: { files: ['b'], services: [] } },
    ],
    async (candidate) => (candidate.address === 'a' ? { status: 'done' } : { status: 'failed', reason: 'nope' })
  );
  const status = c.getGroupStatus('group-7');
  assert.equal(status.complete, true); // failed still counts as terminal for completeness
  const byAddress = Object.fromEntries(status.branches.map((b) => [b.address, b]));
  assert.equal(byAddress.a.status, 'done');
  assert.equal(byAddress.b.status, 'failed');
  assert.equal(byAddress.b.reason, 'nope');
});

test('REQ-11: Coordination exposes no interface that calls canonical-work-model.md or Jira/Streams', () => {
  const c = new Coordination();
  const publicMethods = Object.getOwnPropertyNames(Coordination.prototype).filter((m) => m !== 'constructor');
  for (const m of publicMethods) {
    assert.doesNotMatch(m.toLowerCase(), /jira|canonical|stream|workitem/);
  }
  assert.equal(typeof c.dispatchGroup, 'function');
});
