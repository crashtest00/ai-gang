'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { loadGraphFile, validateGraphDocument } = require('../lib/schema');
const { walkGraph } = require('../lib/walker');
const { checkBranchTracking } = require('../lib/checks/branch-tracking');

const GRAPH_PATH = path.join(__dirname, '..', '..', 'branch-tracking-stale-ref.graph.yaml');

test('the checked-in branch-tracking-stale-ref graph is structurally valid', () => {
  const doc = loadGraphFile(GRAPH_PATH);
  const result = validateGraphDocument(doc);
  assert.deepEqual(result.errors, []);
  assert.equal(result.valid, true);
});

test('a matching tracking ref reaches success directly', async () => {
  const doc = loadGraphFile(GRAPH_PATH);
  const exec = async (cmd) => {
    if (cmd.includes('@{u}')) return 'origin/main\n';
    if (cmd.includes('refs/remotes/origin/HEAD')) return 'refs/remotes/origin/main\n';
    throw new Error(`unexpected: ${cmd}`);
  };
  const result = await walkGraph(doc, { evaluateCheck: async () => checkBranchTracking(exec) });
  assert.equal(result.outcome, 'success');
  assert.deepEqual(
    result.transcript.map((t) => t.ref),
    ['branch-tracking-stale-ref#check-tracking-ref:matches', 'branch-tracking-stale-ref#tracking-ref-ok']
  );
});

// REQ-13 acceptance: given a stale ref after a server-side rename, reaches
// the mismatch branch, and the remediation's declared procedure is a
// fetch --prune resync followed by re-evaluation of the same check.
test('a stale tracking ref after a server-side rename reaches remediation, whose guidance is a fetch --prune resync, then re-verifies', async () => {
  const doc = loadGraphFile(GRAPH_PATH);
  let renamed = false;
  const exec = async (cmd) => {
    if (cmd.includes('@{u}')) return 'origin/master\n'; // stale local tracking ref
    if (cmd.includes('refs/remotes/origin/HEAD')) {
      return renamed ? 'refs/remotes/origin/master\n' : 'refs/remotes/origin/main\n';
    }
    throw new Error(`unexpected: ${cmd}`);
  };
  const remediationNode = doc.nodes.find((n) => n.id === 'remediate-stale-tracking-ref');
  assert.match(remediationNode.guidance, /git fetch --prune/);

  const result = await walkGraph(
    doc,
    {
      evaluateCheck: async () => checkBranchTracking(exec),
      onRemediation: async (node) => {
        if (node.id === 'remediate-stale-tracking-ref') {
          // Simulate the guidance's own `git fetch --prune` / set-head
          // actually resyncing the local view of the remote default branch.
          renamed = true;
        }
      },
    },
    { maxSteps: 20 }
  );
  const refs = result.transcript.map((t) => t.ref);
  assert.ok(refs.includes('branch-tracking-stale-ref#check-tracking-ref:stale'));
  assert.ok(refs.includes('branch-tracking-stale-ref#remediate-stale-tracking-ref:next'));
  assert.equal(result.outcome, 'success');
});

test('a probe error (e.g. no upstream configured) reaches its own remediation node', async () => {
  const doc = loadGraphFile(GRAPH_PATH);
  let fixed = false;
  const exec = async (cmd) => {
    if (!fixed && cmd.includes('@{u}')) throw new Error('fatal: no upstream configured');
    if (cmd.includes('@{u}')) return 'origin/main\n';
    if (cmd.includes('refs/remotes/origin/HEAD')) return 'refs/remotes/origin/main\n';
    throw new Error(`unexpected: ${cmd}`);
  };
  const result = await walkGraph(
    doc,
    {
      evaluateCheck: async () => checkBranchTracking(exec),
      onRemediation: async () => {
        fixed = true;
      },
    },
    { maxSteps: 20 }
  );
  const refs = result.transcript.map((t) => t.ref);
  assert.ok(refs.includes('branch-tracking-stale-ref#remediate-tracking-ref-probe-error:next'));
  assert.equal(result.outcome, 'success');
});
