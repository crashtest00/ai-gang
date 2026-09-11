'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { loadGraphFile, validateGraphDocument } = require('../lib/schema');
const { walkGraph } = require('../lib/walker');

const GRAPH_PATH = path.join(__dirname, '..', '..', 'deployment-target-boilerplate.graph.yaml');

test('the checked-in deployment-target-boilerplate graph is structurally valid', () => {
  const doc = loadGraphFile(GRAPH_PATH);
  const result = validateGraphDocument(doc);
  assert.deepEqual(result.errors, []);
  assert.equal(result.valid, true);
});

// deployment-target-boilerplate.md REQ-02
test('selecting "web" scaffolds the web boilerplate and terminates success', async () => {
  const doc = loadGraphFile(GRAPH_PATH);
  const actionsRun = [];
  const result = await walkGraph(doc, {
    evaluateCheck: async () => 'web',
    runAction: async (node) => actionsRun.push(node.id),
  });
  assert.ok(actionsRun.includes('scaffold-web-boilerplate'));
  assert.equal(result.outcome, 'success');
});

// deployment-target-boilerplate.md REQ-03
test('selecting an unsupported target reaches remediation naming the gap, not a dead end', async () => {
  const doc = loadGraphFile(GRAPH_PATH);
  let fixed = false;
  const result = await walkGraph(
    doc,
    {
      evaluateCheck: async () => (fixed ? 'web' : 'unsupported'),
      onRemediation: async (node) => {
        if (node.id === 'remediate-unsupported-target') fixed = true;
      },
    },
    { maxSteps: 20 }
  );
  const refs = result.transcript.map((t) => t.ref);
  assert.ok(refs.includes('deployment-target-boilerplate#remediate-unsupported-target:next'));
  const remediationNode = doc.nodes.find((n) => n.id === 'remediate-unsupported-target');
  assert.match(remediationNode.guidance, /web/);
  assert.equal(result.outcome, 'success');
});
