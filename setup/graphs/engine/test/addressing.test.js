'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const { formatNodeRef, formatEdgeRef, parseRef, resolveRef } = require('../lib/addressing');

test('formatNodeRef produces <graph_id>#<node_id>', () => {
  assert.equal(formatNodeRef('cloudflare-setup', 'cf-token-present'), 'cloudflare-setup#cf-token-present');
});

test('formatEdgeRef produces <graph_id>#<node_id>:<edgeKey>', () => {
  assert.equal(
    formatEdgeRef('cloudflare-setup', 'cf-token-present', 'absent'),
    'cloudflare-setup#cf-token-present:absent'
  );
  assert.equal(formatEdgeRef('g', 'n', 'next'), 'g#n:next');
});

test('parseRef parses a bare node reference', () => {
  assert.deepEqual(parseRef('cloudflare-setup#cf-token-present'), {
    graphId: 'cloudflare-setup',
    nodeId: 'cf-token-present',
    edgeKey: null,
  });
});

test('parseRef parses a when-branch edge reference', () => {
  assert.deepEqual(parseRef('cloudflare-setup#cf-token-present:absent'), {
    graphId: 'cloudflare-setup',
    nodeId: 'cf-token-present',
    edgeKey: 'absent',
  });
});

test('parseRef parses a fan-out branch_id edge reference', () => {
  assert.deepEqual(parseRef('init#init-parallel-phases:jira-setup'), {
    graphId: 'init',
    nodeId: 'init-parallel-phases',
    edgeKey: 'jira-setup',
  });
});

test('parseRef rejects malformed references', () => {
  assert.throws(() => parseRef('no-hash-here'), /malformed reference/);
  assert.throws(() => parseRef(''), /not a string reference/);
  assert.throws(() => parseRef(null), /not a string reference/);
});

const sampleDoc = {
  graph_id: 'sample',
  schema_version: 1,
  entry: 'a',
  nodes: [
    {
      id: 'a',
      kind: 'decision',
      check: { description: 'd', probe: 'p', error_when: 'probe-error' },
      branches: [
        { when: 'yes', to: 'b' },
        { when: 'no', to: 'c' },
        { when: 'probe-error', to: 'c' },
      ],
    },
    { id: 'b', kind: 'action', procedure: 'do it', next: 'c' },
    { id: 'c', kind: 'action', procedure: 'no-op', terminal: true, outcome: 'success' },
  ],
};

test('resolveRef resolves a bare node reference', () => {
  const { node, edgeKey, target } = resolveRef(sampleDoc, 'sample#a');
  assert.equal(node.id, 'a');
  assert.equal(edgeKey, null);
  assert.equal(target, undefined);
});

test('resolveRef resolves a decision node branch edge', () => {
  const { node, target } = resolveRef(sampleDoc, 'sample#a:yes');
  assert.equal(node.id, 'a');
  assert.equal(target, 'b');
});

test('resolveRef resolves an action node "next" edge', () => {
  const { target } = resolveRef(sampleDoc, 'sample#b:next');
  assert.equal(target, 'c');
});

test('resolveRef rejects a reference to a different graph document', () => {
  assert.throws(() => resolveRef(sampleDoc, 'other-graph#a'), /names graph "other-graph"/);
});

test('resolveRef rejects an unknown node', () => {
  assert.throws(() => resolveRef(sampleDoc, 'sample#zzz'), /no node "zzz"/);
});

test('resolveRef rejects an unknown branch key', () => {
  assert.throws(() => resolveRef(sampleDoc, 'sample#a:maybe'), /no branch "when: maybe"/);
});

test('resolveRef rejects a "next" lookup against a terminal node', () => {
  assert.throws(() => resolveRef(sampleDoc, 'sample#c:next'), /terminal/);
});

// --- escalation nodes ---

const escalationSampleDoc = {
  graph_id: 'sample-esc',
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

test('resolveRef resolves an escalation node branch edge', () => {
  const { node, target } = resolveRef(escalationSampleDoc, 'sample-esc#ask:right');
  assert.equal(node.id, 'ask');
  assert.equal(target, 'do-right');
});

test('resolveRef rejects an unknown escalation branch key', () => {
  assert.throws(() => resolveRef(escalationSampleDoc, 'sample-esc#ask:middle'), /no branch "when: middle"/);
});
