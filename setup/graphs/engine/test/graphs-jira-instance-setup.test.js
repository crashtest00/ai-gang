'use strict';

// Structural validation and end-to-end walks of the checked-in
// jira-instance-setup pilot graph,
// converting docs/ClaudeInstructions.md Phase 1 (1.0 Jira Service Account,
// 1.1 Jira Custom Fields). Unlike every other converted graph so far, this
// one's fork is a human's declared preference (whether this AI Gang
// deployment wants Jira integration at all), not a probe over observable
// environment state — so the entry node is `resolveEscalation`-driven, not
// `evaluateCheck`-driven.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { loadGraphFile, validateGraphDocument } = require('../lib/schema');
const { walkGraph } = require('../lib/walker');

const GRAPH_PATH = path.join(__dirname, '..', '..', 'jira-instance-setup.graph.yaml');

function loadGraph() {
  return loadGraphFile(GRAPH_PATH);
}

test('the checked-in jira-instance-setup graph is structurally valid', () => {
  const doc = loadGraph();
  const result = validateGraphDocument(doc);
  assert.deepEqual(result.errors, []);
  assert.equal(result.valid, true);
});

test('answering "no" skips straight to the skipped terminal, touching no Jira instance-setup step', async () => {
  const doc = loadGraph();
  // Terminal nodes (any kind) are recorded via the walker's own 'terminal'
  // event and never dispatched to a kind-specific handler like runAction
  // (walker.js checks node.terminal before the kind switch) — so
  // 'jira-integration-skipped', itself terminal, never invokes runAction.
  // What matters for "touched no instance-setup step" is that
  // create-jira-service-account/create-jira-custom-fields — the two
  // non-terminal steps this fork is meant to skip — never run.
  const actionsRun = [];
  const result = await walkGraph(doc, {
    resolveEscalation: async () => 'no',
    runAction: async (node) => actionsRun.push(node.id),
  });
  assert.equal(result.outcome, 'skipped');
  const refs = result.transcript.map((t) => t.ref);
  assert.deepEqual(refs, ['jira-instance-setup#want-jira-integration:no', 'jira-instance-setup#jira-integration-skipped']);
  assert.deepEqual(actionsRun, []);
});

test('answering "yes" walks both instance-setup steps in order (1.0 then 1.1) to success', async () => {
  const doc = loadGraph();
  const actionsRun = [];
  const result = await walkGraph(doc, {
    resolveEscalation: async () => 'yes',
    runAction: async (node) => actionsRun.push(node.id),
  });
  assert.equal(result.outcome, 'success');
  // create-jira-custom-fields is terminal, so (like every other terminal
  // action node in this engine, e.g. cloudflare-setup's cf-setup-done)
  // the walker records it via the 'terminal' event rather than calling
  // runAction — only the non-terminal 1.0 step does.
  assert.deepEqual(actionsRun, ['create-jira-service-account']);
  const refs = result.transcript.map((t) => t.ref);
  assert.deepEqual(refs, [
    'jira-instance-setup#want-jira-integration:yes',
    'jira-instance-setup#create-jira-service-account:next',
    'jira-instance-setup#create-jira-custom-fields',
  ]);
});

// Resuming with any answer other than a declared `when` key is rejected.
test('an answer other than "yes"/"no" is rejected, not silently defaulted', async () => {
  const doc = loadGraph();
  await assert.rejects(
    () => walkGraph(doc, { resolveEscalation: async () => 'maybe-later' }),
    /not one of its declared "when" keys/
  );
});

// Walking to any escalation node halts the walk pending a human's choice.
test('with no resolveEscalation handler at all, the walk halts rather than guessing', async () => {
  const doc = loadGraph();
  await assert.rejects(() => walkGraph(doc, {}), /halts the walk pending a human choice/);
});

// No escalation node declares a check, procedure, or
// writes field — asserted directly against the checked-in document, not
// just the generic schema-level test in schema.test.js.
test('the entry escalation node declares no check, procedure, or writes', () => {
  const doc = loadGraph();
  const entry = doc.nodes.find((n) => n.id === 'want-jira-integration');
  assert.equal(entry.kind, 'escalation');
  assert.equal(entry.check, undefined);
  assert.equal(entry.procedure, undefined);
  assert.equal(entry.writes, undefined);
  assert.equal(typeof entry.prompt, 'string');
});

// canonical-delivery-state.md REQ-10, "The field ids reach `core`" — the
// 1.1 step runs BOTH provisioning scripts, writes only to the platform
// .env, and verifies that all fourteen JIRA_FIELD_ID_VARS ids are there.
// Up to v5.1 it ran only create-jira-fields.sh and told a human to copy
// nine ids into the platform .env by hand; the five Release ids were
// nobody's, and connect_jira refuses until all fourteen are set.
test('the custom-fields step runs both provisioning scripts and verifies all fourteen ids', () => {
  const doc = loadGraph();
  const step = doc.nodes.find((n) => n.id === 'create-jira-custom-fields');

  assert.match(step.procedure, /create-jira-fields\.sh/);
  assert.match(step.procedure, /create-release-fields\.sh/);
  assert.match(step.procedure, /14/, 'the verify names the full JIRA_FIELD_ID_VARS count');
  assert.deepEqual(step.writes.files, ['~/ai-gang/.env'],
    'the ids are written straight into the platform .env, so there is nothing to copy');
  assert.ok(!/copy the IDs/i.test(step.procedure),
    'the copy-the-ids instruction is dropped: derive-env.sh reads this file itself');
  assert.ok(!/scrummaster/.test(step.procedure),
    'nothing Jira-facing has read services/scrummaster/.env since v5.1');
});

// The two scripts the step names are the ones that exist, with the ids the
// loop that reads them declares (gate 4: an enumerating doc matches the
// config that defines it).
test('the step names scripts that exist, and the fourteen ids derive-env.sh reads', () => {
  const fs = require('node:fs');
  const repoRoot = path.join(__dirname, '..', '..', '..', '..');

  for (const script of ['create-jira-fields.sh', 'create-release-fields.sh']) {
    const scriptPath = path.join(repoRoot, 'scripts', script);
    assert.ok(fs.existsSync(scriptPath), `${script} is named by the graph and must exist`);
    const text = fs.readFileSync(scriptPath, 'utf8');
    assert.match(text, /FIELD_ID_ENV="\$HQ_ENV"/,
      `${script} writes its ids into the platform .env`);
  }

  const deriveEnv = fs.readFileSync(path.join(repoRoot, 'scripts', 'startup', 'derive-env.sh'), 'utf8');
  const block = deriveEnv.match(/JIRA_FIELD_ID_VARS=\(([\s\S]*?)\)/);
  assert.ok(block, 'derive-env.sh declares JIRA_FIELD_ID_VARS as an array literal');
  const ids = block[1].split('\n').map((line) => line.trim()).filter(Boolean);
  assert.equal(ids.length, 14, 'the count the graph step verifies');
});
