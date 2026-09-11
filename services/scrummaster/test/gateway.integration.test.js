'use strict';

// End-to-end exercise of the gateway stream consumer against a real Redis
// instance, using gateway.js's actual public entry point
// (startGatewaySubscriber/stopGatewaySubscriber), with jira.js's network
// calls monkey-patched to an in-memory fake. Node caches CommonJS modules by
// reference, so mutating the already-required `jira` module's exports here
// also affects gateway.js/handlers.js, which `require('./jira')` the same
// cached object — no production code changes needed to make this testable.
//
// Gateway submissions use the canonical A2A payload shape ({ state, message,
// artifacts? } — the a2a-messaging design), not the legacy
// `{ type, ... }` contract that shape replaced (REQ-09). A real submission
// only ever arrives for a Task ScrumMaster has already dispatched, so each
// test registers its Task in the in-memory a2a/taskStore first, mirroring
// what handlers.js's dispatchTask does in production.

const path = require('node:path');
const { test, before, after, beforeEach } = require('node:test');
const assert = require('node:assert/strict');

process.env.AGENTS_CATALOG_PATH = path.join(__dirname, '..', 'config', 'agents.json');
process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, '..', 'config', 'projects.json');
process.env.REDIS_HOST = process.env.REDIS_TEST_HOST || 'localhost';
process.env.REDIS_PORT = process.env.REDIS_TEST_PORT || '16399';

const jira = require('../src/jira');
const registry = require('../src/registry');
const streams = require('../src/streams');
const dependencies = require('../src/dependencies');
const canonicalWorkItems = require('../src/canonicalWorkItems');
const taskStore = require('../src/a2a/taskStore');
const { buildTextPart, buildDataPart, buildMessage, buildTask } = require('../src/a2a/parts');
const { buildEnvelope, KIND, fromStreamFields } = require('../src/envelope');
const gateway = require('../src/gateway');
const redisModule = require('../src/redis');

let client;

const fakeJira = {
  issues: new Map(),
  comments: [],
  blocked: new Map(),
  agentField: new Map(),
  transitions: [],
  nextSubtaskSeq: 1,
};

function resetFakeJira() {
  fakeJira.issues.clear();
  fakeJira.comments = [];
  fakeJira.blocked.clear();
  fakeJira.agentField.clear();
  fakeJira.transitions = [];
  fakeJira.nextSubtaskSeq = 1;
  fakeJira.issues.set('HW-1', {
    key: 'HW-1', summary: 'Parent story', project: 'HW', projectName: 'hello-world',
    comments: [], parent: null,
  });
}

// Register a Task in the in-memory store the way handlers.js's dispatchTask
// would have when ScrumMaster originally assigned it — a gateway submission
// is only ever meaningful for a Task that already exists.
function registerTask(taskId, { contextId = taskId, agentId = 'backend-agent' } = {}) {
  const messageId = `msg-seed-${taskId}`;
  const message = buildMessage({
    messageId, taskId, contextId, role: 'client', parts: [buildTextPart('seed dispatch')],
  });
  taskStore.register(buildTask({
    id: taskId,
    contextId,
    status: { state: 'submitted', timestamp: new Date().toISOString(), message },
    metadata: { jiraIssueKey: taskId, jiraProjectKey: 'HW', jiraProjectName: 'hello-world', agentId },
  }));
  return messageId;
}

before(async () => {
  await redisModule.connect();
  client = redisModule.getClient();

  jira.postComment = async (key, body) => { fakeJira.comments.push({ key, body }); };
  jira.setBlockedField = async (key, val) => { fakeJira.blocked.set(key, val); };
  jira.setAgentField = async (key, val) => { fakeJira.agentField.set(key, val); };
  jira.transitionIssue = async (key, status) => { fakeJira.transitions.push({ key, status }); };
  jira.getIssue = async (key) => {
    const issue = fakeJira.issues.get(key);
    if (!issue) throw new Error(`fake jira: no such issue ${key}`);
    return { ...issue, comments: issue.comments || [] };
  };
  jira.createSubtask = async (parentKey, projectKey, summary, _description, agentFieldValue) => {
    const subtaskKey = `HW-${100 + fakeJira.nextSubtaskSeq++}`;
    fakeJira.issues.set(subtaskKey, {
      key: subtaskKey, summary, project: projectKey, projectName: 'hello-world',
      parent: parentKey, comments: [], agent: agentFieldValue,
    });
    return subtaskKey;
  };
});

after(async () => {
  await client.quit();
});

beforeEach(async () => {
  await client.flushDb();
  resetFakeJira();
  taskStore._reset();
  // These fixtures exercise Jira projection; local-mode cases override it.
  // Keep the mode lookup at the same mocked network boundary as Jira.
  canonicalWorkItems.getMode = async () => ({ mode: 'jira' });
});

function gatewayStream() { return registry.gatewayStreamName('hello-world'); }
function agentStream(suffix) { return registry.agentStreamName('hello-world', suffix); }

// Build the canonical A2A submission payload ({ state, message, artifacts? })
// for a gateway envelope, given the Task/context it belongs to.
function a2aPayload({ taskId, contextId, referenceMessageId, state, parts, artifacts }) {
  return {
    state,
    message: buildMessage({
      messageId: `msg-${taskId}-${Math.random().toString(36).slice(2, 8)}`,
      taskId,
      contextId,
      role: 'agent',
      parts,
      referenceMessageId,
    }),
    ...(artifacts ? { artifacts } : {}),
  };
}

async function publishGatewayOp(payload, { messageId } = {}) {
  const envelope = buildEnvelope({
    kind: KIND.JIRA_OPERATION,
    project: 'hello-world',
    taskId: payload.message?.taskId || payload.parentJiraIssueKey || null,
    contextId: payload.message?.contextId || null,
    payload,
    messageId,
  });
  await streams.publish(client, gatewayStream(), envelope);
  return envelope;
}

async function waitForXLen(stream, expected, timeoutMs = 3000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if ((await client.xLen(stream)) >= expected) return;
    await new Promise(r => setTimeout(r, 25));
  }
  throw new Error(`timed out waiting for ${stream} to reach length ${expected}`);
}

test('create_subtask creates the subtask once and dispatches exactly one task, even when redelivered', async () => {
  const lastMessageId = registerTask('HW-1', { agentId: 'refinement-agent' });

  const envelope = await publishGatewayOp(a2aPayload({
    taskId: 'HW-1',
    contextId: 'HW-1',
    referenceMessageId: lastMessageId,
    state: 'working',
    parts: [
      buildTextPart('Creating subtask: Implement thing'),
      buildDataPart({ operation: 'create_subtask', summary: 'Implement thing', description: 'Do the thing', agentFieldValue: 'backend-agent' }),
    ],
  }));

  // Simulate a redelivery of the SAME logical message (same messageId) —
  // e.g. ScrumMaster crashed after the first attempt succeeded but before ack.
  await client.xAdd(gatewayStream(), '*', { data: JSON.stringify(envelope) });

  await gateway.startGatewaySubscriber();
  try {
    await waitForXLen(agentStream('backend'), 1);
    // Give the second (duplicate) entry a chance to be processed too.
    await new Promise(r => setTimeout(r, 300));
  } finally {
    await gateway.stopGatewaySubscriber();
  }

  assert.equal(fakeJira.issues.size, 2, 'exactly one subtask should have been created');
  const subtaskKeys = [...fakeJira.issues.keys()].filter(k => k !== 'HW-1');
  assert.equal(subtaskKeys.length, 1);

  assert.equal(await client.xLen(agentStream('backend')), 1, 'exactly one task dispatch, not two');
  const dispatched = await client.xRange(agentStream('backend'), '-', '+');
  const dispatchedEnvelope = fromStreamFields(dispatched[0].message);
  assert.equal(dispatchedEnvelope.taskId, subtaskKeys[0]);
  assert.equal(dispatchedEnvelope.contextId, 'HW-1');
  assert.equal(dispatchedEnvelope.payload.role, 'client');

  assert.equal(fakeJira.transitions.filter(t => t.key === subtaskKeys[0] && t.status === 'In Progress').length, 1);
});

test('a gateway envelope claiming the wrong project is dead-lettered without any Jira effect', async () => {
  registerTask('HW-1');
  const foreignEnvelope = buildEnvelope({
    kind: KIND.JIRA_OPERATION,
    project: 'hello-desktop', // claims a DIFFERENT project than the stream it's on
    taskId: 'HW-1',
    payload: a2aPayload({
      taskId: 'HW-1', contextId: 'HW-1', state: 'working',
      parts: [buildTextPart('hi'), buildDataPart({ operation: 'comment' })],
    }),
  });
  await client.xAdd(gatewayStream(), '*', { data: JSON.stringify(foreignEnvelope) });

  await gateway.startGatewaySubscriber();
  try {
    await waitForXLen(streams.deadLetterStreamName(gatewayStream()), 1);
  } finally {
    await gateway.stopGatewaySubscriber();
  }

  assert.equal(fakeJira.comments.length, 0, 'no Jira comment should have been posted');
});

test('comment operation posts to Jira exactly once even if delivered twice with the same messageId', async () => {
  const lastMessageId = registerTask('HW-1');

  const envelope = await publishGatewayOp(a2aPayload({
    taskId: 'HW-1',
    contextId: 'HW-1',
    referenceMessageId: lastMessageId,
    state: 'working',
    parts: [
      buildTextPart('Finished the thing'),
      buildDataPart({ operation: 'comment', reference: { file: 'src/x.js', function: 'doThing' } }),
    ],
  }));
  await client.xAdd(gatewayStream(), '*', { data: JSON.stringify(envelope) }); // redelivery

  await gateway.startGatewaySubscriber();
  try {
    await new Promise(r => setTimeout(r, 500));
  } finally {
    await gateway.stopGatewaySubscriber();
  }

  assert.equal(fakeJira.comments.length, 1);
  assert.match(fakeJira.comments[0].body, /Finished the thing/);
});

test('durably accepted out-of-order subtask chain defers completion until both predecessors succeed', async () => {
  const originalGetMode = canonicalWorkItems.getMode;
  canonicalWorkItems.getMode = async () => ({ mode: 'jira' });
  const seedMessageId = registerTask('HW-1', { agentId: 'refinement-agent' });

  const firstPayload = a2aPayload({
    taskId: 'HW-1', contextId: 'HW-1', referenceMessageId: seedMessageId, state: 'working',
    parts: [
      buildTextPart('Creating first subtask'),
      buildDataPart({ operation: 'create_subtask', summary: 'First', description: 'First task', agentFieldValue: 'backend-agent' }),
    ],
  });
  const secondPayload = a2aPayload({
    taskId: 'HW-1', contextId: 'HW-1', referenceMessageId: firstPayload.message.messageId, state: 'working',
    parts: [
      buildTextPart('Creating second subtask'),
      buildDataPart({ operation: 'create_subtask', summary: 'Second', description: 'Second task', agentFieldValue: 'frontend-agent' }),
    ],
  });
  const completionPayload = a2aPayload({
    taskId: 'HW-1', contextId: 'HW-1', referenceMessageId: secondPayload.message.messageId, state: 'completed',
    parts: [buildTextPart('Created both subtasks')],
  });

  const first = await publishGatewayOp(firstPayload);
  const second = await publishGatewayOp(secondPayload);
  const completion = await publishGatewayOp(completionPayload);

  try {
    await assert.rejects(
      gateway._handleA2ASubmission(completion, 'hello-world'),
      taskStore.A2ACausalDependencyPendingError
    );
    await assert.rejects(
      gateway._handleA2ASubmission(second, 'hello-world'),
      taskStore.A2ACausalDependencyPendingError
    );
    assert.equal(taskStore.getTaskById('HW-1').state, 'submitted');

    await gateway._handleA2ASubmission(first, 'hello-world');
    assert.equal(taskStore.getTaskById('HW-1').state, 'working');
    await gateway._handleA2ASubmission(second, 'hello-world');
    assert.equal(taskStore.getTaskById('HW-1').state, 'working');
    await gateway._handleA2ASubmission(completion, 'hello-world');

    const record = taskStore.getTaskById('HW-1');
    assert.equal(record.state, 'completed');
    assert.deepEqual(
      record.messages.slice(-3).map(message => message.messageId),
      [firstPayload.message.messageId, secondPayload.message.messageId, completionPayload.message.messageId]
    );
    assert.equal([...fakeJira.issues.keys()].filter(key => key !== 'HW-1').length, 2);
  } finally {
    canonicalWorkItems.getMode = originalGetMode;
  }
});

test('a materializeDecomposition operation routes to dependencies.js and is acked on success', async () => {
  const originalFn = dependencies.routeMaterialization;
  let calledWith = null;
  dependencies.routeMaterialization = async (message, projectName) => {
    calledWith = { message, projectName };
    return { idToKey: new Map([['proposal-1', 'HW-101']]) };
  };

  try {
    await publishGatewayOp({
      operation: 'materializeDecomposition',
      parentJiraIssueKey: 'HW-1',
      subtasks: [{ id: 'proposal-1', displayName: 'x', description: 'y', agent: 'backend-agent', 'Blocked By': [] }],
    });

    await gateway.startGatewaySubscriber();
    try {
      await new Promise(r => setTimeout(r, 400));
    } finally {
      await gateway.stopGatewaySubscriber();
    }

    assert.ok(calledWith, 'dependencies.routeMaterialization should have been called');
    assert.equal(calledWith.message.parentId, 'HW-1');
    assert.equal(calledWith.projectName, 'hello-world');
    assert.equal((await client.xPending(gatewayStream(), registry.GATEWAY_GROUP)).pending, 0, 'entry should be acked');
  } finally {
    dependencies.routeMaterialization = originalFn;
  }
});

test('a MaterializationValidationError from dependencies.js is dead-lettered, not retried', async () => {
  const originalFn = dependencies.routeMaterialization;
  let attempts = 0;
  dependencies.routeMaterialization = async () => {
    attempts += 1;
    throw new dependencies.MaterializationValidationError(
      [{ subtaskId: 'proposal-1', displayName: 'x', requestedAgent: 'nope-agent' }],
      ['backend-agent']
    );
  };

  try {
    await publishGatewayOp({
      operation: 'materializeDecomposition',
      parentJiraIssueKey: 'HW-1',
      subtasks: [{ id: 'proposal-1', displayName: 'x', description: 'y', agent: 'nope-agent', 'Blocked By': [] }],
    });

    await gateway.startGatewaySubscriber();
    try {
      await waitForXLen(streams.deadLetterStreamName(gatewayStream()), 1);
    } finally {
      await gateway.stopGatewaySubscriber();
    }

    assert.equal(attempts, 1, 'a validation failure must not be retried');
  } finally {
    dependencies.routeMaterialization = originalFn;
  }
});

// Regression test for the gap a 2026-09-07 doc-vs-code audit found: REQ-12's
// mode-aware routing (dependencies.js's routeMaterialization) was built and
// unit-tested in isolation, but gateway.js's dispatch path called
// dependencies.materializeDecomposition directly, bypassing it entirely — so
// a local-mode project's decomposition silently kept going straight to Jira
// in production despite the Implementation Status checklist reading as done.
// This test exercises gateway.js's real, unmocked entry point end to end and
// would have failed against that bug.
test('a materializeDecomposition operation for a local-mode project publishes to the Internal Work-Item Service, not Jira', async () => {
  const originalGetMode = canonicalWorkItems.getMode;
  const originalPublishCommand = canonicalWorkItems.publishCommand;
  const originalCreateSubtask = jira.createSubtaskForProposal;
  const publishedCommands = [];
  let jiraSubtaskCalls = 0;

  canonicalWorkItems.getMode = async () => ({ mode: 'local' });
  canonicalWorkItems.publishCommand = async (project, payload) => {
    publishedCommands.push({ project, payload });
    return { deduped: false };
  };
  jira.createSubtaskForProposal = async (...args) => {
    jiraSubtaskCalls += 1;
    return originalCreateSubtask(...args);
  };

  try {
    await publishGatewayOp({
      operation: 'materializeDecomposition',
      parentJiraIssueKey: 'local-parent-work-item-id',
      subtasks: [{ id: 'proposal-1', displayName: 'x', description: 'y', agent: 'backend-agent', 'Blocked By': [] }],
    });

    await gateway.startGatewaySubscriber();
    try {
      await new Promise(r => setTimeout(r, 400));
    } finally {
      await gateway.stopGatewaySubscriber();
    }

    assert.equal(publishedCommands.length, 1, 'local mode must publish a canonical materializeDecomposition command');
    assert.equal(publishedCommands[0].payload.command, 'materializeDecomposition');
    assert.equal(publishedCommands[0].payload.message.parentWorkItemId, 'local-parent-work-item-id');
    assert.equal(jiraSubtaskCalls, 0, 'local mode must never touch Jira directly');
    assert.equal((await client.xPending(gatewayStream(), registry.GATEWAY_GROUP)).pending, 0, 'entry should be acked');
  } finally {
    canonicalWorkItems.getMode = originalGetMode;
    canonicalWorkItems.publishCommand = originalPublishCommand;
    jira.createSubtaskForProposal = originalCreateSubtask;
  }
});
