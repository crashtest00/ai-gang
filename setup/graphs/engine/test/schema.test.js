'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { validateGraphDocument } = require('../lib/schema');

function baseValidDoc() {
  return {
    graph_id: 'g',
    schema_version: 1,
    entry: 'start',
    nodes: [
      {
        id: 'start',
        kind: 'decision',
        check: { description: 'd', probe: 'p', error_when: 'probe-error' },
        branches: [
          { when: 'yes', to: 'do-it' },
          { when: 'no', to: 'done' },
          { when: 'probe-error', to: 'done' },
        ],
      },
      { id: 'do-it', kind: 'action', procedure: 'run it', next: 'done' },
      { id: 'done', kind: 'action', procedure: 'no-op', terminal: true, outcome: 'success' },
    ],
  };
}

test('a well-formed graph document is valid', () => {
  const result = validateGraphDocument(baseValidDoc());
  assert.deepEqual(result.errors, []);
  assert.equal(result.valid, true);
});

// --- required top-level fields ---

test('missing graph_id is rejected', () => {
  const doc = baseValidDoc();
  delete doc.graph_id;
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('graph_id')));
});

test('missing entry is rejected', () => {
  const doc = baseValidDoc();
  delete doc.entry;
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('entry')));
});

test('a node without a valid kind is rejected', () => {
  const doc = baseValidDoc();
  doc.nodes.push({ id: 'weird', kind: 'not-a-kind' });
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('invalid or missing kind')));
});

test('duplicate node id is rejected', () => {
  const doc = baseValidDoc();
  doc.nodes.push({ id: 'start', kind: 'action', procedure: 'x', terminal: true, outcome: 'success' });
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('duplicate node id')));
});

test('entry naming an unknown node is rejected', () => {
  const doc = baseValidDoc();
  doc.entry = 'nowhere';
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('does not name a node')));
});

// --- decision nodes ---

test('decision node missing check is rejected', () => {
  const doc = baseValidDoc();
  delete doc.nodes[0].check;
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('missing "check"')));
});

test('decision node without an error_when outcome is rejected', () => {
  const doc = baseValidDoc();
  delete doc.nodes[0].check.error_when;
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('error_when')));
});

test('duplicate "when" keys are rejected (branches must be mutually exclusive)', () => {
  const doc = baseValidDoc();
  doc.nodes[0].branches.push({ when: 'yes', to: 'done' });
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('duplicate "when: yes"')));
});

test('a branch targeting an unknown node is rejected', () => {
  const doc = baseValidDoc();
  doc.nodes[0].branches[0].to = 'nowhere';
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('unknown node "nowhere"')));
});

test('a decision node declaring writes is rejected (decision nodes MUST NOT mutate state)', () => {
  const doc = baseValidDoc();
  doc.nodes[0].writes = { files: [], services: [] };
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('MUST NOT declare writes')));
});

// --- action nodes ---

test('action node missing procedure is rejected', () => {
  const doc = baseValidDoc();
  delete doc.nodes[1].procedure;
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('missing "procedure"')));
});

test('action node missing next (and not terminal) is rejected', () => {
  const doc = baseValidDoc();
  delete doc.nodes[1].next;
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('missing "next"')));
});

test('writes with files but no services is rejected', () => {
  const doc = baseValidDoc();
  doc.nodes[1].writes = { files: ['a.txt'] };
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('writes.services')));
});

test('writes with both files and services (even empty) is valid', () => {
  const doc = baseValidDoc();
  doc.nodes[1].writes = { files: [], services: [] };
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, true, result.errors.join('; '));
});

// --- remediation nodes ---

test('remediation node missing guidance is rejected', () => {
  const doc = baseValidDoc();
  doc.nodes.push({ id: 'remediate', kind: 'remediation', next: 'start' });
  doc.nodes[0].branches[1].to = 'remediate';
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('missing "guidance"')));
});

test('a terminal remediation node is rejected (no dead ends / must loop back)', () => {
  const doc = baseValidDoc();
  doc.nodes.push({
    id: 'remediate',
    kind: 'remediation',
    guidance: '[HUMAN] fix it',
    terminal: true,
    outcome: 'success',
  });
  doc.nodes[0].branches[1].to = 'remediate';
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('MUST NOT be terminal')));
});

test('a remediation node with outcome: failure is rejected', () => {
  const doc = baseValidDoc();
  doc.nodes.push({ id: 'remediate', kind: 'remediation', guidance: 'x', outcome: 'failure', next: 'start' });
  doc.nodes[0].branches[1].to = 'remediate';
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('MUST NOT have outcome: failure')));
});

// --- dead-end / reachability checks ---

test('a reachable non-terminal node with no outgoing edges is a dead end', () => {
  const doc = baseValidDoc();
  doc.nodes.push({ id: 'orphan-action', kind: 'action', procedure: 'stuck' });
  doc.nodes[0].branches[1].to = 'orphan-action';
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('dead end')));
});

test('a terminal node with outcome: failure is rejected', () => {
  const doc = baseValidDoc();
  doc.nodes[2].outcome = 'failure';
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('not "success" or "skipped"')));
});

test('the "(dead end)" check itself is scoped to nodes reachable from entry', () => {
  // Every node kind's own shape check already requires a "next"/"branches"
  // regardless of reachability (e.g. for action nodes), so an
  // unreachable, malformed node is still rejected — just not by the
  // dead-end message specifically. This asserts that scoping: a reachable
  // edge-less action node gets flagged as a "(dead end)"; an unreachable
  // one with the same shape does not additionally get that label (it's
  // still invalid overall, via the action node's own "missing next" check).
  const reachableDoc = baseValidDoc();
  reachableDoc.nodes.push({ id: 'stuck', kind: 'action', procedure: 'never finishes' });
  reachableDoc.nodes[0].branches[1].to = 'stuck'; // make it reachable
  const reachableResult = validateGraphDocument(reachableDoc);
  assert.ok(reachableResult.errors.some((e) => e.includes('dead end')));

  const unreachableDoc = baseValidDoc();
  unreachableDoc.nodes.push({ id: 'stuck', kind: 'action', procedure: 'never visited' });
  const unreachableResult = validateGraphDocument(unreachableDoc);
  assert.ok(unreachableResult.errors.some((e) => e.includes('missing "next"')));
  assert.ok(!unreachableResult.errors.some((e) => e.includes('dead end')));
});

// --- fan-out / fan-in nodes ---

function fanOutDoc() {
  return {
    graph_id: 'g',
    schema_version: 1,
    entry: 'split',
    nodes: [
      {
        id: 'split',
        kind: 'fan-out',
        branches: [
          { branch_id: 'left', to: 'left-work' },
          { branch_id: 'right', to: 'right-work' },
        ],
        join: 'joined',
      },
      { id: 'left-work', kind: 'action', procedure: 'left', next: 'joined-entry-left' },
      { id: 'joined-entry-left', kind: 'action', procedure: 'converge', next: 'joined' },
      { id: 'right-work', kind: 'action', procedure: 'right', next: 'joined' },
      { id: 'joined', kind: 'fan-in', for: 'split', next: 'done' },
      { id: 'done', kind: 'action', procedure: 'no-op', terminal: true, outcome: 'success' },
    ],
  };
}

test('a well-formed fan-out/fan-in pair is valid', () => {
  const result = validateGraphDocument(fanOutDoc());
  assert.deepEqual(result.errors, []);
  assert.equal(result.valid, true);
});

test('fan-out join must resolve to a fan-in node', () => {
  const doc = fanOutDoc();
  doc.nodes[0].join = 'left-work'; // not a fan-in node
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('must name a fan-in node')));
});

test('fan-in "for" must point back to the fan-out whose join names it', () => {
  const doc = fanOutDoc();
  doc.nodes.find((n) => n.id === 'joined').for = 'left-work'; // not the matching fan-out
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('must name a fan-out node')));
});

test('a branch that bypasses the join and reaches a terminal node directly is rejected', () => {
  const doc = fanOutDoc();
  // right-work now terminates on its own instead of converging at "joined"
  doc.nodes.find((n) => n.id === 'right-work').next = 'done';
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('without first passing through join')));
});

test('a terminal fan-out node is rejected', () => {
  const doc = fanOutDoc();
  doc.nodes[0].terminal = true;
  doc.nodes[0].outcome = 'success';
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('fan-out node "split" MUST NOT be terminal')));
});

test('a fan-out node declaring "check" is rejected', () => {
  const doc = fanOutDoc();
  doc.nodes[0].check = { description: 'x', probe: 'y' };
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('MUST NOT declare "check"')));
});

// --- escalation nodes ---

function escalationDoc() {
  return {
    graph_id: 'g',
    schema_version: 1,
    entry: 'ask',
    nodes: [
      {
        id: 'ask',
        kind: 'escalation',
        prompt: 'Pick a lane.',
        branches: [
          { when: 'left', to: 'do-left' },
          { when: 'right', to: 'do-right' },
        ],
      },
      { id: 'do-left', kind: 'action', procedure: 'go left', terminal: true, outcome: 'success' },
      { id: 'do-right', kind: 'action', procedure: 'go right', terminal: true, outcome: 'success' },
    ],
  };
}

test('a well-formed escalation node is valid', () => {
  const result = validateGraphDocument(escalationDoc());
  assert.deepEqual(result.errors, []);
  assert.equal(result.valid, true);
});

test('escalation node missing prompt is rejected', () => {
  const doc = escalationDoc();
  delete doc.nodes[0].prompt;
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('missing "prompt"')));
});

test('escalation node declaring "check" is rejected (outcome is never probe-derived)', () => {
  const doc = escalationDoc();
  doc.nodes[0].check = { description: 'x', probe: 'y' };
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('MUST NOT declare "check"')));
});

test('escalation node declaring "procedure" is rejected', () => {
  const doc = escalationDoc();
  doc.nodes[0].procedure = 'do something';
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('MUST NOT declare "procedure"')));
});

test('escalation node declaring "writes" is rejected', () => {
  const doc = escalationDoc();
  doc.nodes[0].writes = { files: [], services: [] };
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('MUST NOT declare "writes"')));
});

test('duplicate "when" keys on an escalation node are rejected', () => {
  const doc = escalationDoc();
  doc.nodes[0].branches.push({ when: 'left', to: 'do-right' });
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('duplicate "when: left"')));
});

test('an escalation branch targeting an unknown node is rejected', () => {
  const doc = escalationDoc();
  doc.nodes[0].branches[0].to = 'nowhere';
  const result = validateGraphDocument(doc);
  assert.equal(result.valid, false);
  assert.ok(result.errors.some((e) => e.includes('unknown node "nowhere"')));
});

test('a remediation loop back to its own decision node is not flagged as a dead end or a cycle violation', () => {
  const doc = {
    graph_id: 'g',
    schema_version: 1,
    entry: 'check',
    nodes: [
      {
        id: 'check',
        kind: 'decision',
        check: { description: 'd', probe: 'p', error_when: 'err' },
        branches: [
          { when: 'ok', to: 'done' },
          { when: 'missing', to: 'remediate' },
          { when: 'err', to: 'remediate' },
        ],
      },
      { id: 'remediate', kind: 'remediation', guidance: '[HUMAN] fix it', next: 'check' },
      { id: 'done', kind: 'action', procedure: 'no-op', terminal: true, outcome: 'success' },
    ],
  };
  const result = validateGraphDocument(doc);
  assert.deepEqual(result.errors, []);
  assert.equal(result.valid, true);
});
