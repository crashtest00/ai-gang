'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { loadGraphFile, validateGraphDocument } = require('../lib/schema');
const { walkGraph } = require('../lib/walker');
const { detectBaseImageFamily } = require('../lib/checks/base-image-family');

const GRAPH_PATH = path.join(__dirname, '..', '..', 'base-image-family.graph.yaml');
const DOCKER_TEMPLATES_DIR = path.join(__dirname, '..', '..', '..', '..', 'Docker Templates');

test('the checked-in base-image-family graph is structurally valid', () => {
  const doc = loadGraphFile(GRAPH_PATH);
  const result = validateGraphDocument(doc);
  assert.deepEqual(result.errors, []);
  assert.equal(result.valid, true);
});

async function walkAgainst(templateName) {
  const doc = loadGraphFile(GRAPH_PATH);
  const content = fs.readFileSync(path.join(DOCKER_TEMPLATES_DIR, templateName), 'utf8');
  return walkGraph(doc, { evaluateCheck: async () => detectBaseImageFamily(content) });
}

// REQ-12 acceptance
test('Dockerfile-node.template (node:22-alpine) reaches the alpine-busybox branch', async () => {
  const result = await walkAgainst('Dockerfile-node.template');
  const refs = result.transcript.map((t) => t.ref);
  assert.ok(refs.includes('base-image-family#detect-base-image:alpine-busybox'));
  assert.equal(result.outcome, 'success');
});

test('Dockerfile-python.template (python:3.11-slim) reaches the debian-ubuntu branch', async () => {
  const result = await walkAgainst('Dockerfile-python.template');
  const refs = result.transcript.map((t) => t.ref);
  assert.ok(refs.includes('base-image-family#detect-base-image:debian-ubuntu'));
  assert.equal(result.outcome, 'success');
});

test('Dockerfile-tauri.template (REQ-14) reaches the debian-ubuntu branch', async () => {
  const result = await walkAgainst('Dockerfile-tauri.template');
  const refs = result.transcript.map((t) => t.ref);
  assert.ok(refs.includes('base-image-family#detect-base-image:debian-ubuntu'));
  assert.equal(result.outcome, 'success');
});

test('an unrecognized base image reaches its remediation node before looping back', async () => {
  const doc = loadGraphFile(GRAPH_PATH);
  let attempts = 0;
  const result = await walkGraph(
    doc,
    {
      evaluateCheck: async () => {
        attempts++;
        return attempts === 1 ? 'unrecognized' : 'debian-ubuntu';
      },
    },
    { maxSteps: 20 }
  );
  const refs = result.transcript.map((t) => t.ref);
  assert.ok(refs.includes('base-image-family#remediate-unrecognized-base-image:next'));
  assert.equal(result.outcome, 'success');
});
