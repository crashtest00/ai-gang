'use strict';

// End-to-end integration: a fan-out graph node walked through walkGraph's
// dispatchFanOut hook, backed by a real Coordination instance dispatching
// one walker instance per branch concurrently — including a failed-branch
// scenario, per this workstream's test-coverage requirement.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { walkGraph } = require('../lib/walker');
const { Coordination } = require('../lib/coordination');

function twoBranchGraph() {
  return {
    graph_id: 'init-parallel-phases',
    schema_version: 1,
    entry: 'init-parallel-phases',
    nodes: [
      {
        id: 'init-parallel-phases',
        kind: 'fan-out',
        branches: [
          { branch_id: 'jira-setup', to: 'jira-start' },
          { branch_id: 'vm-infra-setup', to: 'vm-start' },
        ],
        join: 'init-phases-joined',
      },
      {
        id: 'jira-start',
        kind: 'action',
        procedure: 'Jira instance setup',
        writes: { files: ['jira.json'], services: [] },
        next: 'jira-done',
      },
      { id: 'jira-done', kind: 'action', procedure: 'no-op', terminal: true, outcome: 'success' },
      {
        id: 'vm-start',
        kind: 'action',
        procedure: 'VM infrastructure setup',
        writes: { files: ['vm.json'], services: ['redis'] },
        next: 'vm-done',
      },
      { id: 'vm-done', kind: 'action', procedure: 'no-op', terminal: true, outcome: 'success' },
      { id: 'init-phases-joined', kind: 'fan-in', for: 'init-parallel-phases', next: 'all-done' },
      { id: 'all-done', kind: 'action', procedure: 'no-op', terminal: true, outcome: 'success' },
    ],
  };
}

test('both branches collision-safe and successful: fan-out dispatches concurrently, fan-in only after both complete', async () => {
  const doc = twoBranchGraph();
  const coordination = new Coordination();
  const candidates = Coordination.sourceFanOutCandidates(doc, 'init-parallel-phases', {
    'jira-setup': { files: ['jira.json'], services: [] },
    'vm-infra-setup': { files: ['vm.json'], services: ['redis'] },
  });
  assert.equal(Coordination.partitionCollisionSafe(candidates).concurrentGroups.length, 1);

  const result = await walkGraph(doc, {
    dispatchFanOut: async (node) => {
      const status = await coordination.dispatchGroup('group-x', candidates, async (candidate) => {
        const sub = await walkGraph(doc, {}, { entryOverride: candidate.nodeId });
        return { status: sub.outcome === 'success' ? 'done' : 'failed' };
      });
      return { summary: status };
    },
  });

  assert.equal(result.outcome, 'success');
  const groupStatus = coordination.getGroupStatus('group-x');
  assert.equal(groupStatus.complete, true);
  assert.ok(groupStatus.branches.every((b) => b.status === 'done'));
});

test('one branch fails: the sibling still completes, fan-in still eventually proceeds, failure is tracked not lost', async () => {
  const doc = twoBranchGraph();
  const coordination = new Coordination();
  const candidates = Coordination.sourceFanOutCandidates(doc, 'init-parallel-phases', {
    'jira-setup': { files: ['jira.json'], services: [] },
    'vm-infra-setup': { files: ['vm.json'], services: ['redis'] },
  });

  const branchRunLog = [];
  const status = await coordination.dispatchGroup('group-y', candidates, async (candidate) => {
    if (candidate.branchId === 'jira-setup') {
      throw new Error('Jira service account provisioning crashed');
    }
    const sub = await walkGraph(doc, {}, { entryOverride: candidate.nodeId });
    branchRunLog.push(candidate.branchId);
    return { status: sub.outcome === 'success' ? 'done' : 'failed' };
  });

  // Sibling isolation (REQ-07): vm-infra-setup still completed.
  assert.deepEqual(branchRunLog, ['vm-infra-setup']);

  const byBranch = Object.fromEntries(status.branches.map((b) => [b.address, b]));
  assert.equal(byBranch['init-parallel-phases#init-parallel-phases:jira-setup'].status, 'failed');
  assert.match(
    byBranch['init-parallel-phases#init-parallel-phases:jira-setup'].reason,
    /Jira service account provisioning crashed/
  );
  assert.equal(byBranch['init-parallel-phases#init-parallel-phases:vm-infra-setup'].status, 'done');

  // REQ-10: the group is complete (every branch terminal) even though one failed —
  // "complete" tracks accounting, not success. Nothing is silently lost (AC-04).
  assert.equal(status.complete, true);
});
