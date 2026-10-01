'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');

process.env.AGENTS_CATALOG_PATH = path.join(__dirname, '../config/agents.json');
process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, '../config/projects.json');

const redis = require('./redis');
const streams = require('./streams');
const registry = require('./registry');
const jenkins = require('./jenkins');
const canonicalWorkItems = require('./canonicalWorkItems');
const taskStore = require('./a2a/taskStore');
const schema = require('./a2a/schema');
const { dispatchTask, handleDone, handleReleaseRequested, handleReleaseAbandoned } = require('./handlers');

// The shape dispatchConsumer.js's issueLikeFromCanonical builds, which from
// v5.1 is the only issue-like object any dispatch path passes in: `key` is the
// work item's canonical id in every mode.
const ISSUE = {
  key: 'wi-42-canonical',
  externalKey: null,
  jiraProjectKey: 'GANG',
  projectName: 'hello-world',
  parent: null,
};

function mockPublish(t) {
  const published = [];
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(streams, 'publish', async (_client, stream, envelope, opts) => {
    published.push({ stream, envelope, opts });
    return { deduped: false, entryId: '0-1', messageId: envelope.messageId };
  });
  return published;
}

test.beforeEach(() => taskStore._reset());

// dispatchTask is the sole place that builds and publishes an A2A Task
// dispatch/continuation envelope: one Task per work item for its whole
// lifecycle.

test('dispatchTask publishes a schema-valid Message payload for a fresh Task', async (t) => {
  const published = mockPublish(t);
  const agent = registry.getAgent('backend-agent');

  await dispatchTask(ISSUE, agent, {
    dispatchId: 'wh-1',
    promptFactory: (task, message) => `prompt referencing ${task.id}/${task.contextId}/${message.messageId}`,
  });

  assert.equal(published.length, 1);
  const { stream, envelope, opts } = published[0];
  assert.equal(stream, 'aigang:agent:hello-world:backend');
  assert.equal(envelope.kind, 'task');
  assert.equal(envelope.taskId, ISSUE.key);
  schema.validateMessage(envelope.payload);
  assert.equal(envelope.payload.role, 'client');
  assert.equal(envelope.payload.taskId, ISSUE.key);
  assert.equal(opts.dedupeKey, 'dispatch:wh-1');

  const record = taskStore.getTaskById(ISSUE.key);
  assert.equal(record.state, 'submitted');
});

// REQ-06 — the Task's id is the work item's canonical id, and its metadata
// carries the project's canonical name. The Task id IS the work item, so a
// second copy of the work item's identity under a tracker-specific name would
// be exactly the handle REQ-07 forbids keying on: the record and its metadata
// are asserted by their full key sets, so any such field reappearing fails
// here.
test('a registered Task is keyed by the canonical work item id and carries no tracker-named id', async (t) => {
  mockPublish(t);
  const agent = registry.getAgent('backend-agent');

  await dispatchTask(ISSUE, agent, { promptFactory: () => 'prompt' });

  const record = taskStore.getTaskById(ISSUE.key);
  assert.equal(record.id, 'wi-42-canonical');
  assert.equal(record.metadata.projectName, 'hello-world');
  assert.equal(record.metadata.jiraProjectKey, 'GANG');
  assert.deepEqual(Object.keys(record).sort(),
    ['artifacts', 'contextId', 'id', 'messages', 'metadata', 'state']);
  assert.deepEqual(Object.keys(record.metadata).sort(),
    ['agentId', 'jiraProjectKey', 'projectName']);
});

test('the prompt factory receives the Task id/contextId before publishing', async (t) => {
  mockPublish(t);
  const agent = registry.getAgent('backend-agent');

  let seen = null;
  await dispatchTask(ISSUE, agent, {
    promptFactory: (task, message) => {
      seen = { taskId: task.id, contextId: task.contextId, messageId: message.messageId };
      return 'prompt text';
    },
  });

  assert.ok(seen.taskId && seen.contextId && seen.messageId);
  assert.equal(seen.taskId, ISSUE.key);
});

// A continuation reuses the existing Task and context, with a new Message identity

test('dispatchTask resumes an interrupted Task with a new Message, same identity', async (t) => {
  mockPublish(t);
  const agent = registry.getAgent('backend-agent');

  await dispatchTask(ISSUE, agent, { promptFactory: () => 'initial prompt' });
  const firstMessageId = taskStore.lastMessage(ISSUE.key).messageId;
  taskStore.applyTransition(ISSUE.key, { state: 'input-required' });

  await dispatchTask(ISSUE, agent, { promptFactory: () => 'continuation prompt' });

  const record = taskStore.getTaskById(ISSUE.key);
  assert.equal(record.id, ISSUE.key, 'Task identity must not change');
  assert.equal(record.state, 'working');
  const secondMessageId = taskStore.lastMessage(ISSUE.key).messageId;
  assert.notEqual(secondMessageId, firstMessageId, 'a new Message identity is created for the continuation');
});

test('dispatchTask does not create a second Task for the same work item', async (t) => {
  mockPublish(t);
  const agent = registry.getAgent('backend-agent');

  await dispatchTask(ISSUE, agent, { promptFactory: () => 'initial prompt' });
  taskStore.applyTransition(ISSUE.key, { state: 'input-required' });
  await dispatchTask(ISSUE, agent, { promptFactory: () => 'continuation prompt' });

  assert.equal(taskStore.getTaskById(ISSUE.key).id, ISSUE.key);
});

test('dispatchTask reopens a completed Task under the same identity and retains its history', async (t) => {
  const published = mockPublish(t);
  const agent = registry.getAgent('backend-agent');

  await dispatchTask(ISSUE, agent, { promptFactory: () => 'initial prompt' });
  const firstMessageId = taskStore.lastMessage(ISSUE.key).messageId;
  taskStore.applyTransition(ISSUE.key, { state: 'completed' });

  await dispatchTask(ISSUE, agent, { dispatchId: 'canonical-ready-2', promptFactory: () => 'redispatch prompt' });

  const record = taskStore.getTaskById(ISSUE.key);
  assert.equal(record.id, ISSUE.key);
  assert.equal(record.state, 'working');
  assert.equal(record.messages[0].messageId, firstMessageId);
  assert.equal(record.messages.length, 2);
  assert.equal(published.length, 2);
  assert.equal(published[1].envelope.taskId, ISSUE.key);
  assert.equal(published[1].envelope.payload.referenceMessageId, firstMessageId);
});

test('a subtask under a story shares its contextId with the parent', async (t) => {
  mockPublish(t);
  const agent = registry.getAgent('backend-agent');
  const subtask = { ...ISSUE, key: 'wi-43-canonical', parent: 'wi-42-canonical' };

  await dispatchTask(subtask, agent, { promptFactory: () => 'prompt' });

  assert.equal(taskStore.getTaskById('wi-43-canonical').contextId, 'wi-42-canonical');
});

// Handler 5 — handleReleaseRequested triggers Jenkins for a release event
// dispatchConsumer.js has already resolved to a canonical work item. The
// beta-queue check ran in Django before the event was published.

test('handleReleaseRequested triggers release-candidate with the canonical work item id', async (t) => {
  let triggered = null;
  t.mock.method(jenkins, 'triggerReleaseCandidate', async (ref, projectName) => {
    triggered = { ref, projectName };
  });

  await handleReleaseRequested({ workItemId: 'wi-release-1', project: 'hello-world' });

  assert.deepEqual(triggered, { ref: { workItemId: 'wi-release-1' }, projectName: 'hello-world' });
});

// Handler 4 — handleDone promotes to production using the release work item's
// own recorded Candidate SHA.

test('handleDone promotes using the release work item\'s recorded Candidate SHA', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-release-1', releaseDetail: { candidate_sha: 'def5678' },
  }));
  let triggered = null;
  t.mock.method(jenkins, 'triggerProductionPromote', async (ref, projectName, candidateSha) => {
    triggered = { ref, projectName, candidateSha };
  });

  await handleDone({ workItemId: 'wi-release-1', project: 'hello-world' });

  assert.deepEqual(triggered, { ref: { workItemId: 'wi-release-1' }, projectName: 'hello-world', candidateSha: 'def5678' });
});

test('handleDone does not promote when no Candidate SHA is recorded', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({ id: 'wi-release-1', releaseDetail: null }));
  let called = false;
  t.mock.method(jenkins, 'triggerProductionPromote', async () => { called = true; });

  await handleDone({ workItemId: 'wi-release-1', project: 'hello-world' });

  assert.equal(called, false);
});

// Handler 6 — handleReleaseAbandoned tears down the preview container so it
// doesn't outlive an abandoned release.

test('handleReleaseAbandoned tears down the preview container', async (t) => {
  let triggered = null;
  t.mock.method(jenkins, 'triggerPreviewTeardown', async (ref, projectName) => {
    triggered = { ref, projectName };
  });

  await handleReleaseAbandoned({ workItemId: 'wi-release-1', project: 'hello-world' });

  assert.deepEqual(triggered, { ref: { workItemId: 'wi-release-1' }, projectName: 'hello-world' });
});

// REQ-07 — `findBlockedMarker` greps for `BLOCKED <work item id>`, keyed on the
// canonical id, and the agent role documents are where an agent learns to write
// that marker. The two have to agree or a resumed dispatch silently finds
// nothing, so the documents are read here rather than trusted.
test('every agent role document teaches the marker format findBlockedMarker searches for', () => {
  const rolesDir = path.join(__dirname, '..', '..', '..', 'setup', 'agents');
  for (const name of ['backend-agent.md', 'frontend-agent.md', 'devops-agent.md']) {
    const doc = fs.readFileSync(path.join(rolesDir, name), 'utf8');
    assert.match(doc, /BLOCKED <work item id>/,
      `${name} must teach the marker keyed on the canonical work item id`);
    assert.doesNotMatch(doc, /BLOCKED GANG-/,
      `${name} must not teach a marker keyed on a tracker key`);
  }
});
