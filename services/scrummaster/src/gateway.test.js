'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

process.env.AGENTS_CATALOG_PATH = path.join(__dirname, '../config/agents.json');
process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, '../config/projects.json');

const jira = require('./jira');
const redis = require('./redis');
const streams = require('./streams');
const idempotency = require('./idempotency');
const canonicalWorkItems = require('./canonicalWorkItems');
const handlers = require('./handlers');
const taskStore = require('./a2a/taskStore');
const { newMessageId, newArtifactId } = require('./a2a/ids');
const { buildTextPart, buildDataPart, buildMessage, buildTask, buildArtifact } = require('./a2a/parts');
const { _handleA2ASubmission: handleA2ASubmission, _handlePipelineRetry: handlePipelineRetry, _handleTaskStatus: handleTaskStatus } = require('./gateway');

const ISSUE_KEY = 'GANG-42';
const PROJECT_NAME = 'hello-world';

function registerTask(overrides = {}) {
  const contextId = 'ctx-gateway-test';
  const messageId = newMessageId();
  const message = buildMessage({
    messageId, taskId: ISSUE_KEY, contextId, role: 'client', parts: [buildTextPart('dispatch prompt')],
  });
  const task = buildTask({
    id: ISSUE_KEY,
    contextId,
    status: { state: 'submitted', timestamp: new Date().toISOString(), message },
    metadata: {
      jiraIssueKey: ISSUE_KEY,
      jiraProjectKey: 'GANG',
      jiraProjectName: PROJECT_NAME,
      agentId: 'backend-agent',
      ...overrides,
    },
  });
  taskStore.register(task);
  return { contextId, messageId };
}

function envelope({ contextId, referenceMessageId, state, parts, artifacts }) {
  return {
    schemaVersion: '1',
    messageId: newMessageId(),
    kind: 'jira_operation',
    project: PROJECT_NAME,
    taskId: ISSUE_KEY,
    contextId,
    createdAt: new Date().toISOString(),
    payload: {
      state,
      message: buildMessage({
        messageId: newMessageId(),
        taskId: ISSUE_KEY,
        contextId,
        role: 'agent',
        parts,
        referenceMessageId,
      }),
      ...(artifacts ? { artifacts } : {}),
    },
  };
}

test.beforeEach(() => taskStore._reset());

// Every existing test in this file exercises Jira-mode behavior (the
// existing, unmodified jira.* side effects) — no required behavior change
// for a Jira-mode project. gateway.js
// now reads the project's mode via canonicalWorkItems.getMode() before
// deciding which side effect to perform, so that real HTTP call needs
// stubbing out here the same way jira.js's calls already are.
function mockJiraMode(t) {
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ mode: 'jira' }));
}

test('comment operation posts a formatted Jira comment', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask();
  let posted = null;
  t.mock.method(jira, 'postComment', async (key, body) => { posted = { key, body }; });

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'working', parts: [buildTextPart('progress note'), buildDataPart({ operation: 'comment' })] }),
    PROJECT_NAME
  );

  assert.ok(posted, 'postComment must be called');
  assert.equal(posted.key, ISSUE_KEY);
  assert.match(posted.body, /progress note/);
  assert.match(posted.body, new RegExp(ISSUE_KEY));
});

test('a working message with no data part is still posted as a plain comment', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask();
  let posted = null;
  t.mock.method(jira, 'postComment', async (key, body) => { posted = { key, body }; });

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'working', parts: [buildTextPart('just a note')] }),
    PROJECT_NAME
  );

  assert.ok(posted);
  assert.match(posted.body, /just a note/);
});

test('input-required sets the Blocked field and posts a comment with the reference', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask();
  let blocked = null;
  let posted = null;
  t.mock.method(jira, 'setBlockedField', async (key, value) => { blocked = { key, value }; });
  t.mock.method(jira, 'postComment', async (key, body) => { posted = { key, body }; });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'input-required',
      parts: [
        buildTextPart('Which endpoint should doAuth() call?'),
        buildDataPart({ reference: { file: 'src/auth.py', function: 'doAuth()' } }),
      ],
    }),
    PROJECT_NAME
  );

  assert.deepEqual(blocked, { key: ISSUE_KEY, value: true });
  assert.match(posted.body, /BLOCKED/);
  assert.match(posted.body, /doAuth\(\)/);
  assert.match(posted.body, /src\/auth\.py/);
});

test('auth-required is projected distinctly from input-required', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask();
  let posted = null;
  t.mock.method(jira, 'setBlockedField', async () => {});
  t.mock.method(jira, 'postComment', async (key, body) => { posted = { key, body }; });

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'auth-required', parts: [buildTextPart('need prod credentials')] }),
    PROJECT_NAME
  );

  assert.match(posted.body, /AUTHORIZATION REQUIRED/);
});

test('an interrupted task can be resumed and later completed without changing its identity', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask();
  t.mock.method(jira, 'setBlockedField', async () => {});
  t.mock.method(jira, 'postComment', async () => {});

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'input-required', parts: [buildTextPart('need info')] }),
    PROJECT_NAME
  );
  assert.equal(taskStore.getTaskById(ISSUE_KEY).state, 'input-required');

  const replyId = taskStore.lastMessage(ISSUE_KEY).messageId;
  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: replyId, state: 'completed', parts: [buildTextPart('done after clarification')] }),
    PROJECT_NAME
  );

  const record = taskStore.getTaskById(ISSUE_KEY);
  assert.equal(record.id, ISSUE_KEY);
  assert.equal(record.contextId, contextId);
  assert.equal(record.state, 'completed');
});

test('completed with a pull-request artifact posts only a comment — no transition or reassignment', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask();
  const calls = [];
  t.mock.method(jira, 'postComment', async (k, b) => calls.push(['postComment', k, b]));
  t.mock.method(jira, 'transitionIssue', async (k, s) => calls.push(['transitionIssue', k, s]));
  t.mock.method(jira, 'setAgentField', async (k, v) => calls.push(['setAgentField', k, v]));

  const artifact = buildArtifact({
    artifactId: newArtifactId(),
    taskId: ISSUE_KEY,
    name: 'pull-request',
    parts: [
      { kind: 'file', file: { name: 'pull-request', mimeType: 'text/uri-list', uri: 'https://github.com/org/repo/pull/7' } },
      buildTextPart('Implements the endpoint'),
    ],
  });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'completed',
      parts: [buildTextPart('Verified and opened PR')],
      artifacts: [artifact],
    }),
    PROJECT_NAME
  );

  assert.equal(calls.length, 1, 'only postComment — Jenkins owns the In Review transition');
  assert.equal(calls[0][0], 'postComment');
  assert.match(calls[0][2], /github\.com\/org\/repo\/pull\/7/);
});

test('completed with no artifact and a summary posts only the closing comment', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask();
  const calls = [];
  t.mock.method(jira, 'postComment', async (k, b) => calls.push(['postComment', k, b]));
  t.mock.method(jira, 'transitionIssue', async (k, s) => calls.push(['transitionIssue', k, s]));

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'completed', parts: [buildTextPart('Decomposed into 2 subtasks')] }),
    PROJECT_NAME
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], 'postComment');
});

test('failed/canceled/rejected leave a visible, attributable blocked state', async (t) => {
  mockJiraMode(t);
  for (const state of ['failed', 'canceled', 'rejected']) {
    taskStore._reset();
    const { contextId, messageId } = registerTask();
    let blocked = null;
    let posted = null;
    t.mock.method(jira, 'setBlockedField', async (k, v) => { blocked = { k, v }; });
    t.mock.method(jira, 'postComment', async (k, b) => { posted = b; });

    await handleA2ASubmission(
      envelope({ contextId, referenceMessageId: messageId, state, parts: [buildTextPart('agent process crashed')] }),
      PROJECT_NAME
    );

    assert.deepEqual(blocked, { k: ISSUE_KEY, v: true }, `${state} must set Blocked`);
    assert.match(posted, new RegExp(state.toUpperCase()));
  }
});

test('reassign sets the Agent field to a catalog-valid, project-enabled agent', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask();
  let set = null;
  t.mock.method(jira, 'setAgentField', async (k, v) => { set = { k, v }; });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [buildTextPart('handing off'), buildDataPart({ operation: 'reassign', agentFieldValue: 'frontend-agent' })],
    }),
    PROJECT_NAME
  );

  assert.deepEqual(set, { k: ISSUE_KEY, v: 'frontend-agent' });
});

test('reassign to an unknown agent reports a visible assignment failure instead of touching the Agent field', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask();
  let setAgentFieldCalled = false;
  let blocked = null;
  let posted = null;
  t.mock.method(jira, 'setAgentField', async () => { setAgentFieldCalled = true; });
  t.mock.method(jira, 'setBlockedField', async (k, v) => { blocked = { k, v }; });
  t.mock.method(jira, 'postComment', async (k, b) => { posted = b; });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [buildTextPart('handing off'), buildDataPart({ operation: 'reassign', agentFieldValue: 'nonexistent-agent' })],
    }),
    PROJECT_NAME
  );

  assert.equal(setAgentFieldCalled, false);
  assert.deepEqual(blocked, { k: ISSUE_KEY, v: true });
  assert.match(posted, /catalog validation/);
});

test('reassign with no agentFieldValue comments the rejection on the ticket instead of dropping silently', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask();
  let setAgentFieldCalled = false;
  const posted = [];
  t.mock.method(jira, 'setAgentField', async () => { setAgentFieldCalled = true; });
  t.mock.method(jira, 'postComment', async (key, body) => { posted.push({ key, body }); });

  const outcome = await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [buildTextPart('handing off'), buildDataPart({ operation: 'reassign' })],
    }),
    PROJECT_NAME
  );

  assert.equal(outcome, null);
  assert.equal(setAgentFieldCalled, false);
  assert.equal(posted.length, 1, 'the ticket must carry a visible record of the rejection');
  assert.equal(posted[0].key, ISSUE_KEY);
  assert.match(posted[0].body, /agentFieldValue/);
  assert.match(posted[0].body, /backend-agent/, 'the permitted agent ids are named for recovery');
  assert.doesNotMatch(posted[0].body, /resend the create_subtask operation/);
  assert.equal(taskStore.failedMessageIds(ISSUE_KEY).length, 1, 'the Task must carry the failure so it cannot log completed');
});

test('create_subtask creates a Jira subtask and dispatches a new Task to it', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  t.mock.method(jira, 'getIssue', async (key) => ({
    key,
    project: 'GANG',
    projectName: 'hello-world',
    parent: key === 'GANG-43' ? 'GANG-42' : null,
    summary: 'Backend: implement endpoint',
    behavior: null,
    acceptanceCriteria: null,
    constraints: null,
    edgeCases: null,
    outOfScope: null,
    comments: [],
  }));
  let createdSubtask = null;
  t.mock.method(jira, 'createSubtask', async (parentKey, projectKey, summary, description, agentFieldValue) => {
    createdSubtask = { parentKey, projectKey, summary, description, agentFieldValue };
    return 'GANG-43';
  });
  t.mock.method(jira, 'transitionIssue', async () => {});
  t.mock.method(redis, 'getClient', () => ({}));
  let published = null;
  t.mock.method(streams, 'publish', async (_client, stream, streamEnvelope) => {
    published = { stream, envelope: streamEnvelope };
    return { deduped: false, entryId: '0-1', messageId: streamEnvelope.messageId };
  });
  t.mock.method(idempotency, 'getOutcome', async () => undefined);
  t.mock.method(idempotency, 'recordOutcome', async () => {});

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask: Backend: implement endpoint'),
        buildDataPart({ operation: 'create_subtask', summary: 'Backend: implement endpoint', description: 'full desc', agentFieldValue: 'backend-agent' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.deepEqual(createdSubtask, {
    parentKey: ISSUE_KEY, projectKey: 'GANG', summary: 'Backend: implement endpoint', description: 'full desc', agentFieldValue: 'backend-agent',
  });
  assert.ok(published, 'the new subtask Task must be dispatched');
  assert.equal(published.envelope.taskId, 'GANG-43');
  assert.notEqual(published.envelope.taskId, ISSUE_KEY, 'the subtask gets its own Task identity');
});

test('a pending create_subtask side effect blocks dependent completion until its successful retry', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });
  let releaseCreate;
  const createReleased = new Promise(resolve => { releaseCreate = resolve; });
  let startedCreate;
  const createStarted = new Promise(resolve => { startedCreate = resolve; });

  t.mock.method(jira, 'getIssue', async key => ({
    key,
    project: 'GANG',
    projectName: PROJECT_NAME,
    parent: key === 'GANG-43' ? ISSUE_KEY : null,
    summary: 'Backend: implement endpoint',
    comments: [],
  }));
  t.mock.method(jira, 'createSubtask', async () => {
    startedCreate();
    await createReleased;
    return 'GANG-43';
  });
  t.mock.method(jira, 'transitionIssue', async () => {});
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(streams, 'publish', async () => ({ deduped: false, entryId: '0-1' }));
  t.mock.method(idempotency, 'getOutcome', async () => undefined);
  t.mock.method(idempotency, 'recordOutcome', async () => {});
  t.mock.method(jira, 'postComment', async () => {});

  const create = envelope({
    contextId, referenceMessageId: messageId, state: 'working',
    parts: [
      buildTextPart('Creating subtask'),
      buildDataPart({ operation: 'create_subtask', summary: 'Backend: implement endpoint', description: 'full desc', agentFieldValue: 'backend-agent' }),
    ],
  });
  const creating = handleA2ASubmission(create, PROJECT_NAME);
  await createStarted;

  const completion = envelope({
    contextId, referenceMessageId: create.payload.message.messageId, state: 'completed',
    parts: [buildTextPart('Subtask creation complete')],
  });
  await assert.rejects(
    handleA2ASubmission(completion, PROJECT_NAME),
    taskStore.A2ACausalDependencyPendingError
  );
  assert.equal(taskStore.getTaskById(ISSUE_KEY).state, 'working');

  releaseCreate();
  await creating;
  await handleA2ASubmission(completion, PROJECT_NAME);

  const record = taskStore.getTaskById(ISSUE_KEY);
  assert.equal(record.state, 'completed');
  assert.deepEqual(record.messages.map(message => message.messageId), [
    messageId,
    create.payload.message.messageId,
    completion.payload.message.messageId,
  ]);
});

test('a completed message retries its pending side effect without a terminal guard or duplicate history', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask();
  let postAttempts = 0;
  t.mock.method(jira, 'postComment', async () => {
    postAttempts += 1;
    if (postAttempts === 1) throw new Error('temporary Jira outage');
  });

  const completion = envelope({
    contextId, referenceMessageId: messageId, state: 'completed',
    parts: [buildTextPart('Finished')],
  });
  await assert.rejects(handleA2ASubmission(completion, PROJECT_NAME), /temporary Jira outage/);
  assert.equal(taskStore.getTaskById(ISSUE_KEY).state, 'completed');

  await handleA2ASubmission(completion, PROJECT_NAME);

  const record = taskStore.getTaskById(ISSUE_KEY);
  assert.equal(postAttempts, 2);
  assert.equal(record.state, 'completed');
  assert.equal(
    record.messages.filter(message => message.messageId === completion.payload.message.messageId).length,
    1,
    'the retry reuses the pending message rather than appending it again'
  );
});

test('a stream predecessor for a different task remains an invalid reference', async (t) => {
  const { contextId } = registerTask();
  const foreignPredecessor = envelope({
    contextId: 'ctx-other-task', referenceMessageId: null, state: 'working',
    parts: [buildTextPart('Other task progress')],
  });
  foreignPredecessor.taskId = 'GANG-99';
  foreignPredecessor.payload.message.taskId = 'GANG-99';
  t.mock.method(redis, 'getClient', () => ({
    xRange: async () => [{ message: { data: JSON.stringify(foreignPredecessor) } }],
  }));

  const dependent = envelope({
    contextId,
    referenceMessageId: foreignPredecessor.payload.message.messageId,
    state: 'working',
    parts: [buildTextPart('Must not accept a foreign predecessor')],
  });
  await assert.rejects(
    handleA2ASubmission(dependent, PROJECT_NAME),
    taskStore.A2AReferenceError
  );
  assert.equal(taskStore.getTaskById(ISSUE_KEY).messages.length, 1);
});

test('create_subtask with an invalid agent reports a visible assignment failure and creates nothing', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });
  let createSubtaskCalled = false;
  let blocked = null;
  t.mock.method(jira, 'createSubtask', async () => { createSubtaskCalled = true; });
  t.mock.method(jira, 'setBlockedField', async (k, v) => { blocked = { k, v }; });
  t.mock.method(jira, 'postComment', async () => {});

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({ operation: 'create_subtask', summary: 'x', description: 'y', agentFieldValue: 'nonexistent-agent' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.equal(createSubtaskCalled, false);
  assert.deepEqual(blocked, { k: ISSUE_KEY, v: true });
});

test('create_subtask without agentFieldValue derives the agent from the summary role prefix', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  t.mock.method(jira, 'getIssue', async (key) => ({
    key,
    project: 'GANG',
    projectName: PROJECT_NAME,
    parent: key === 'GANG-43' ? ISSUE_KEY : null,
    summary: 'Backend: /health endpoint',
    comments: [],
  }));
  let createdSubtask = null;
  t.mock.method(jira, 'createSubtask', async (parentKey, projectKey, summary, description, agentFieldValue) => {
    createdSubtask = { parentKey, projectKey, summary, description, agentFieldValue };
    return 'GANG-43';
  });
  t.mock.method(jira, 'transitionIssue', async () => {});
  t.mock.method(jira, 'postComment', async () => { throw new Error('a derivable request must not be reported as rejected'); });
  t.mock.method(redis, 'getClient', () => ({}));
  let published = null;
  t.mock.method(streams, 'publish', async (_client, stream, streamEnvelope) => {
    published = { stream, envelope: streamEnvelope };
    return { deduped: false, entryId: '0-1', messageId: streamEnvelope.messageId };
  });
  t.mock.method(idempotency, 'getOutcome', async () => undefined);
  t.mock.method(idempotency, 'recordOutcome', async () => {});

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask: Backend: /health endpoint'),
        buildDataPart({ operation: 'create_subtask', summary: 'Backend: /health endpoint', description: 'full desc' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.equal(createdSubtask.agentFieldValue, 'backend-agent', 'the omitted agent id is derived from the "Backend:" prefix');
  assert.equal(createdSubtask.summary, 'Backend: /health endpoint');
  assert.ok(published, 'the derived subtask is dispatched like any other');
  assert.equal(published.envelope.taskId, 'GANG-43');
});

test('create_subtask with no derivable agent comments the rejection on the parent and creates nothing', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });
  let createSubtaskCalled = false;
  t.mock.method(jira, 'createSubtask', async () => { createSubtaskCalled = true; });
  const posted = [];
  t.mock.method(jira, 'postComment', async (key, body) => { posted.push({ key, body }); });

  const outcome = await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({ operation: 'create_subtask', summary: 'Add a /health endpoint', description: 'full desc' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.equal(outcome, null);
  assert.equal(createSubtaskCalled, false);
  assert.equal(posted.length, 1, 'the parent ticket must carry a visible record of the rejection');
  assert.equal(posted[0].key, ISSUE_KEY);
  assert.match(posted[0].body, /agentFieldValue/);
  assert.match(posted[0].body, /Add a \/health endpoint/);
  assert.match(posted[0].body, /backend-agent/, 'the permitted agent ids are named for recovery');
});

test('local mode: create_subtask with no derivable agent appends the rejection comment and materializes nothing', async (t) => {
  const calls = mockLocalMode(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({ operation: 'create_subtask', summary: 'Add a /health endpoint', description: 'full desc' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.ok(!calls.some(c => c.payload.command === 'materializeDecomposition'));
  const comment = calls.find(c => c.payload.command === 'appendComment');
  assert.ok(comment, 'the parent work item must carry a visible record of the rejection');
  assert.match(comment.payload.body, /agentFieldValue/);
  assert.match(comment.payload.body, /Add a \/health endpoint/);
});

test('a task whose submission was rejected is reported failed, not completed', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });
  t.mock.method(jira, 'createSubtask', async () => { throw new Error('must not create anything'); });
  t.mock.method(jira, 'postComment', async () => {});

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({ operation: 'create_subtask', summary: 'Add a /health endpoint', description: 'full desc' }),
      ],
    }),
    PROJECT_NAME
  );

  const logs = [];
  t.mock.method(console, 'log', (...args) => logs.push(args.join(' ')));

  // The agent's own container exits cleanly and reports success — it cannot
  // see that the gateway rejected what it sent.
  const result = await handleTaskStatus({
    kind: 'task_status',
    messageId: newMessageId(),
    taskId: ISSUE_KEY,
    contextId,
    payload: { status: 'completed', ticket_key: ISSUE_KEY, agent_name: 'refinement-agent' },
  }, PROJECT_NAME);

  assert.equal(result.status, 'failed');
  assert.equal(taskStore.getTaskById(ISSUE_KEY).state, 'failed');
  assert.ok(!logs.some(line => /completed/.test(line)), 'the task must never be logged as completed');
});

test('a task whose chain permanently failed is reported failed, not completed', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });
  t.mock.method(jira, 'postComment', async () => {});

  // The rejected create_subtask poisons the rest of the chain: the agent's
  // own completion message references it and is permanently failed.
  const rejected = envelope({
    contextId, referenceMessageId: messageId, state: 'working',
    parts: [
      buildTextPart('Creating subtask'),
      buildDataPart({ operation: 'create_subtask', summary: 'Add a /health endpoint', description: 'full desc' }),
    ],
  });
  await handleA2ASubmission(rejected, PROJECT_NAME);

  await assert.rejects(
    handleA2ASubmission(
      envelope({
        contextId, referenceMessageId: rejected.payload.message.messageId, state: 'completed',
        parts: [buildTextPart('Decomposed into 1 subtask')],
      }),
      PROJECT_NAME
    ),
    taskStore.A2ACausalDependencyFailedError
  );

  const result = await handleTaskStatus({
    kind: 'task_status',
    messageId: newMessageId(),
    taskId: ISSUE_KEY,
    contextId,
    payload: { status: 'completed', ticket_key: ISSUE_KEY, agent_name: 'refinement-agent' },
  }, PROJECT_NAME);

  assert.equal(result.status, 'failed');
  assert.deepEqual(result.failedMessageIds, [rejected.payload.message.messageId]);
});

test('a task with no failed submission is still reported completed', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask();
  t.mock.method(jira, 'postComment', async () => {});

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'working', parts: [buildTextPart('progress note'), buildDataPart({ operation: 'comment' })] }),
    PROJECT_NAME
  );

  const result = await handleTaskStatus({
    kind: 'task_status',
    messageId: newMessageId(),
    taskId: ISSUE_KEY,
    contextId,
    payload: { status: 'completed', ticket_key: ISSUE_KEY, agent_name: 'backend-agent' },
  }, PROJECT_NAME);

  assert.deepEqual(result, { taskId: ISSUE_KEY, status: 'completed' });
  assert.equal(taskStore.getTaskById(ISSUE_KEY).state, 'completed');
});

// A rejection the requesting agent cannot see or usefully resend leaves the
// ticket recoverable only through a fresh dispatch (handlers.dispatchTask's
// controlled-reopen path) — that redispatch must supersede the stale
// failure, not leave the Task reading failed forever once it genuinely
// succeeds.
test('a redispatch supersedes an earlier rejection so the task can complete cleanly afterward', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });
  t.mock.method(jira, 'postComment', async () => {});
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(streams, 'publish', async () => ({ deduped: false, entryId: '0-1' }));

  const rejected = envelope({
    contextId, referenceMessageId: messageId, state: 'working',
    parts: [
      buildTextPart('Creating subtask'),
      buildDataPart({ operation: 'create_subtask', summary: 'Add a /health endpoint', description: 'full desc' }),
    ],
  });
  await handleA2ASubmission(rejected, PROJECT_NAME);
  assert.deepEqual(taskStore.failedMessageIds(ISSUE_KEY), [rejected.payload.message.messageId]);

  // The same reopen mechanism a pipeline-retry/rework/unblock redispatch
  // uses (handlers.dispatchTask), driven through its real entry point.
  await handlers.dispatchTask(
    { key: ISSUE_KEY, project: 'GANG', projectName: PROJECT_NAME },
    { id: 'refinement-agent', routing: { channelSuffix: 'refinement' } },
    { dispatchId: 'retry-1', promptFactory: () => 'retry prompt' }
  );
  assert.deepEqual(taskStore.failedMessageIds(ISSUE_KEY), [], 'the redispatch must supersede the earlier rejection');

  const newSeed = taskStore.lastMessage(ISSUE_KEY).messageId;
  const completion = envelope({
    contextId, referenceMessageId: newSeed, state: 'completed',
    parts: [buildTextPart('Created the subtask this time')],
  });
  await handleA2ASubmission(completion, PROJECT_NAME);

  const result = await handleTaskStatus({
    kind: 'task_status',
    messageId: newMessageId(),
    taskId: ISSUE_KEY,
    contextId,
    payload: { status: 'completed', ticket_key: ISSUE_KEY, agent_name: 'refinement-agent' },
  }, PROJECT_NAME);

  assert.deepEqual(result, { taskId: ISSUE_KEY, status: 'completed' }, 'a superseding success must not still read failed');
});

test('create_subtask rejection names every missing required field and blocks causal completion', async (t) => {
  mockJiraMode(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });
  const warnings = [];
  t.mock.method(console, 'warn', (...args) => warnings.push(args.join(' ')));
  t.mock.method(jira, 'postComment', async () => {});

  const rejected = envelope({
    contextId,
    referenceMessageId: messageId,
    state: 'working',
    parts: [
      buildTextPart('Creating incomplete subtask'),
      buildDataPart({ operation: 'create_subtask' }),
    ],
  });
  const outcome = await handleA2ASubmission(rejected, PROJECT_NAME);

  assert.equal(outcome, null);
  assert.match(warnings.join('\n'), /summary/);
  assert.match(warnings.join('\n'), /agentFieldValue/);

  const completion = envelope({
    contextId,
    referenceMessageId: rejected.payload.message.messageId,
    state: 'completed',
    parts: [buildTextPart('done despite missing subtask')],
  });
  await assert.rejects(
    handleA2ASubmission(completion, PROJECT_NAME),
    taskStore.A2ACausalDependencyFailedError
  );
  assert.equal(taskStore.getTaskById(ISSUE_KEY).state, 'working');
});

// The gateway direction must not accept a reversed (client) role

test('a message with role "client" on the gateway channel is dropped', async (t) => {
  const { contextId, messageId } = registerTask();
  let called = false;
  t.mock.method(jira, 'postComment', async () => { called = true; });

  const env = envelope({ contextId, referenceMessageId: messageId, state: 'working', parts: [buildTextPart('spoofed')] });
  env.payload.message.role = 'client';

  await handleA2ASubmission(env, PROJECT_NAME);
  assert.equal(called, false);
});

// Routing: project identity comes from the channel, never from content

test('an envelope for a different project than the channel is dropped', async (t) => {
  const { contextId, messageId } = registerTask();
  let called = false;
  t.mock.method(jira, 'postComment', async () => { called = true; });

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'working', parts: [buildTextPart('note'), buildDataPart({ operation: 'comment' })] }),
    'some-other-project'
  );

  assert.equal(called, false);
});

test('an envelope for an unknown task is dropped', async (t) => {
  let called = false;
  t.mock.method(jira, 'postComment', async () => { called = true; });

  const env = envelope({ contextId: 'ctx-x', referenceMessageId: null, state: 'working', parts: [buildTextPart('note'), buildDataPart({ operation: 'comment' })] });
  env.taskId = 'GANG-does-not-exist';
  env.payload.message.taskId = 'GANG-does-not-exist';

  await handleA2ASubmission(env, PROJECT_NAME);
  assert.equal(called, false);
});

test('an invalid submission (bad state) is dropped without touching Jira', async (t) => {
  const { contextId, messageId } = registerTask();
  let called = false;
  t.mock.method(jira, 'postComment', async () => { called = true; });

  const env = envelope({ contextId, referenceMessageId: messageId, state: 'in-progress', parts: [buildTextPart('note')] });
  await handleA2ASubmission(env, PROJECT_NAME);
  assert.equal(called, false);
});

// handlePipelineRetry — Jenkins reports a failed
// build back over the gateway channel; ScrumMaster redispatches the ticket's
// recorded implementation owner. redispatchImplementationOwner/dispatchTask
// are destructured into gateway.js at load time, so they run for real here —
// only their lower-level dependencies (jira, redis, streams) are mocked, the
// same technique the create_subtask test above uses.

test('a pipeline_retry message redispatches the ticket\'s recorded agent', async (t) => {
  t.mock.method(redis, 'acquireOnce', async () => true);
  t.mock.method(jira, 'getIssue', async (key) => ({
    key, project: 'GANG', projectName: 'hello-world', parent: null, agent: 'backend-agent', comments: [],
  }));
  t.mock.method(redis, 'getClient', () => ({}));
  let published = null;
  t.mock.method(streams, 'publish', async (_client, stream, streamEnvelope) => {
    published = { stream, envelope: streamEnvelope };
    return { deduped: false, entryId: '0-1', messageId: streamEnvelope.messageId };
  });

  const result = await handlePipelineRetry(
    { ticket_key: 'GANG-70', build_url: 'https://jenkins.example.com/job/hello-world/17/', build_number: 17 },
    PROJECT_NAME
  );

  assert.deepEqual(result, { ticket_key: 'GANG-70', skipped: false });
  assert.ok(published, 'the redispatch Task must be published');
  assert.equal(published.envelope.taskId, 'GANG-70');
});

test('a pipeline_retry message with no ticket_key is dropped', async (t) => {
  t.mock.method(redis, 'acquireOnce', async () => { throw new Error('must not be called'); });
  t.mock.method(jira, 'getIssue', async () => { throw new Error('must not be called'); });

  const result = await handlePipelineRetry({ build_url: 'https://jenkins.example.com/job/hello-world/17/' }, PROJECT_NAME);

  assert.equal(result, null);
});

test('a duplicate pipeline_retry for the same ticket and build is skipped', async (t) => {
  t.mock.method(redis, 'acquireOnce', async () => false);
  let getIssueCalled = false;
  t.mock.method(jira, 'getIssue', async () => { getIssueCalled = true; });
  let publishCalled = false;
  t.mock.method(streams, 'publish', async () => { publishCalled = true; });

  const result = await handlePipelineRetry(
    { ticket_key: 'GANG-70', build_url: 'https://jenkins.example.com/job/hello-world/17/', build_number: 17 },
    PROJECT_NAME
  );

  assert.deepEqual(result, { ticket_key: 'GANG-70', skipped: true });
  assert.equal(getIssueCalled, false);
  assert.equal(publishCalled, false);
});

test('the pipeline_retry dedupe key is derived from the ticket and build, falling back from build_url to build_number', async (t) => {
  let dedupeKey = null;
  t.mock.method(redis, 'acquireOnce', async (key) => { dedupeKey = key; return false; });

  await handlePipelineRetry({ ticket_key: 'GANG-70', build_number: 17 }, PROJECT_NAME);

  assert.equal(dedupeKey, 'retry-dispatch:GANG-70:17');
});

// --- Local-mode routing — the same submissions above, but for a project
// in local mode:
// every write must go through canonicalWorkItems.publishCommand's Streams
// command channel instead of jira.*, and no Jira call may occur.

function mockLocalMode(t) {
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ mode: 'local' }));
  const calls = [];
  t.mock.method(canonicalWorkItems, 'publishCommand', async (project, payload) => {
    calls.push({ project, payload });
    return { deduped: false };
  });
  t.mock.method(jira, 'postComment', async () => { throw new Error('must not call jira in local mode'); });
  t.mock.method(jira, 'setBlockedField', async () => { throw new Error('must not call jira in local mode'); });
  t.mock.method(jira, 'setAgentField', async () => { throw new Error('must not call jira in local mode'); });
  t.mock.method(jira, 'getIssue', async () => { throw new Error('must not call jira in local mode'); });
  t.mock.method(jira, 'createSubtask', async () => { throw new Error('must not call jira in local mode'); });
  t.mock.method(jira, 'transitionIssue', async () => { throw new Error('must not call jira in local mode'); });
  return calls;
}

test('local mode: comment operation appends a canonical comment instead of posting to Jira', async (t) => {
  const calls = mockLocalMode(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'working', parts: [buildTextPart('progress note'), buildDataPart({ operation: 'comment' })] }),
    PROJECT_NAME
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].payload.command, 'appendComment');
  assert.equal(calls[0].payload.workItemId, ISSUE_KEY);
  assert.match(calls[0].payload.body, /progress note/);
});

test('local mode: input-required transitions to needs-clarification and appends a comment', async (t) => {
  const calls = mockLocalMode(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'input-required',
      parts: [buildTextPart('Which endpoint should doAuth() call?'), buildDataPart({ reference: { file: 'src/auth.py', function: 'doAuth()' } })],
    }),
    PROJECT_NAME
  );

  assert.equal(calls.length, 2);
  const transition = calls.find(c => c.payload.command === 'transitionStatus');
  const comment = calls.find(c => c.payload.command === 'appendComment');
  assert.equal(transition.payload.status, 'needs-clarification');
  assert.match(comment.payload.body, /BLOCKED/);
  assert.equal(comment.payload.referenceFile, 'src/auth.py');
  assert.equal(comment.payload.referenceFunction, 'doAuth()');
});

test('local mode: failed/canceled/rejected transition to the mapped terminal status and append a comment', async (t) => {
  const expected = { failed: 'failed', canceled: 'cancelled', rejected: 'cancelled' };
  for (const state of Object.keys(expected)) {
    taskStore._reset();
    const calls = mockLocalMode(t);
    const { contextId, messageId } = registerTask();

    await handleA2ASubmission(
      envelope({ contextId, referenceMessageId: messageId, state, parts: [buildTextPart('agent process crashed')] }),
      PROJECT_NAME
    );

    const transition = calls.find(c => c.payload.command === 'transitionStatus');
    assert.equal(transition.payload.status, expected[state], `${state} must map to ${expected[state]}`);
  }
});

test('local mode: completed with a pull-request artifact only appends a comment, no status transition', async (t) => {
  const calls = mockLocalMode(t);
  const { contextId, messageId } = registerTask();

  const artifact = buildArtifact({
    artifactId: newArtifactId(),
    taskId: ISSUE_KEY,
    name: 'pull-request',
    parts: [
      { kind: 'file', file: { name: 'pull-request', mimeType: 'text/uri-list', uri: 'https://github.com/org/repo/pull/7' } },
      buildTextPart('Implements the endpoint'),
    ],
  });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'completed',
      parts: [buildTextPart('Verified and opened PR')],
      artifacts: [artifact],
    }),
    PROJECT_NAME
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].payload.command, 'appendComment');
  assert.match(calls[0].payload.body, /github\.com\/org\/repo\/pull\/7/);
});

test('local mode: reassign publishes an assign command for a catalog-valid agent', async (t) => {
  const calls = mockLocalMode(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [buildTextPart('handing off'), buildDataPart({ operation: 'reassign', agentFieldValue: 'frontend-agent' })],
    }),
    PROJECT_NAME
  );

  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].payload, { command: 'assign', actor: 'Backend Agent', workItemId: ISSUE_KEY, agentId: 'frontend-agent' });
});

test('local mode: reassign to an unknown agent reports a visible failure instead of publishing assign', async (t) => {
  const calls = mockLocalMode(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [buildTextPart('handing off'), buildDataPart({ operation: 'reassign', agentFieldValue: 'nonexistent-agent' })],
    }),
    PROJECT_NAME
  );

  assert.ok(!calls.some(c => c.payload.command === 'assign'), 'no assign command must be published');
  const transition = calls.find(c => c.payload.command === 'transitionStatus');
  const comment = calls.find(c => c.payload.command === 'appendComment');
  assert.equal(transition.payload.status, 'needs-clarification');
  assert.match(comment.payload.body, /catalog validation/);
});

test('local mode: create_subtask publishes a single-subtask materializeDecomposition command and does not dispatch directly', async (t) => {
  const calls = mockLocalMode(t);
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(idempotency, 'getOutcome', async () => undefined);
  let recorded = null;
  t.mock.method(idempotency, 'recordOutcome', async (_client, _ns, _id, outcome) => { recorded = outcome; });
  let publishToAgentStream = false;
  t.mock.method(streams, 'publish', async () => { publishToAgentStream = true; return { deduped: false }; });

  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask: Backend: implement endpoint'),
        buildDataPart({ operation: 'create_subtask', summary: 'Backend: implement endpoint', description: 'full desc', agentFieldValue: 'backend-agent' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].payload.command, 'materializeDecomposition');
  assert.equal(calls[0].payload.message.parentWorkItemId, ISSUE_KEY);
  assert.equal(calls[0].payload.message.subtasks.length, 1);
  const subtask = calls[0].payload.message.subtasks[0];
  assert.equal(subtask.displayName, 'Backend: implement endpoint');
  assert.equal(subtask.agent, 'backend-agent');
  assert.ok(subtask.id, 'a canonical subtask id must be minted');
  assert.equal(recorded, subtask.id, 'the minted id is recorded for idempotent retry');
  assert.equal(publishToAgentStream, false, 'gateway.js must not dispatch directly — dispatch must follow the work_item.status_changed event');
});

test('local mode: create_subtask reuses the previously-minted id on a from-scratch retry, without re-publishing', async (t) => {
  const calls = mockLocalMode(t);
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(idempotency, 'getOutcome', async () => 'already-minted-id');
  t.mock.method(idempotency, 'recordOutcome', async () => { throw new Error('must not re-record on a reused outcome'); });

  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  const result = await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({ operation: 'create_subtask', summary: 'x', description: 'y', agentFieldValue: 'backend-agent' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.deepEqual(result, { subtaskId: 'already-minted-id' });
  assert.equal(calls.length, 0, 'materialize.py\'s own store-level idempotency already covers redelivery; no need to re-publish');
});

test('local mode: create_subtask with an invalid agent reports a visible failure and publishes nothing', async (t) => {
  const calls = mockLocalMode(t);

  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({ operation: 'create_subtask', summary: 'x', description: 'y', agentFieldValue: 'nonexistent-agent' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.ok(!calls.some(c => c.payload.command === 'materializeDecomposition'));
  const transition = calls.find(c => c.payload.command === 'transitionStatus');
  assert.equal(transition.payload.status, 'needs-clarification');
});

// handleTaskStatus (execution-outcome signal) — a
// container's subscriber wrapper reporting retry exhaustion, exercised
// against both modes since it has its own mode branch independent of
// handleA2ASubmission's.

test('handleTaskStatus (jira mode): failed posts a comment and blocks the ticket', async (t) => {
  mockJiraMode(t);
  const { contextId } = registerTask();
  let blocked = null;
  let posted = null;
  t.mock.method(jira, 'setBlockedField', async (k, v) => { blocked = { k, v }; });
  t.mock.method(jira, 'postComment', async (k, b) => { posted = b; });

  await handleTaskStatus({
    kind: 'task_status',
    messageId: newMessageId(),
    taskId: ISSUE_KEY,
    contextId,
    payload: { status: 'failed', ticket_key: ISSUE_KEY, agent_name: 'backend-agent', reason: 'timeout' },
  }, PROJECT_NAME);

  assert.deepEqual(blocked, { k: ISSUE_KEY, v: true });
  assert.match(posted, /timeout/);
});

test('handleTaskStatus (local mode): failed transitions the work item to failed and appends a comment', async (t) => {
  const calls = mockLocalMode(t);
  const { contextId } = registerTask();

  await handleTaskStatus({
    kind: 'task_status',
    messageId: newMessageId(),
    taskId: ISSUE_KEY,
    contextId,
    payload: { status: 'failed', ticket_key: ISSUE_KEY, agent_name: 'backend-agent', reason: 'timeout' },
  }, PROJECT_NAME);

  assert.equal(calls.length, 2);
  const transition = calls.find(c => c.payload.command === 'transitionStatus');
  const comment = calls.find(c => c.payload.command === 'appendComment');
  assert.equal(transition.payload.status, 'failed');
  assert.match(comment.payload.body, /timeout/);
});
