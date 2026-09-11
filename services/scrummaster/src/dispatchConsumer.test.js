'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

process.env.AGENTS_CATALOG_PATH = path.join(__dirname, '../config/agents.json');
process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, '../config/projects.json');

const jira = require('./jira');
const canonicalWorkItems = require('./canonicalWorkItems');
const handlers = require('./handlers');
const taskStore = require('./a2a/taskStore');
const { buildTask } = require('./a2a/parts');
const {
  handleWorkItemEventEnvelope,
  maybeDispatch,
  maybeRedispatchForRework,
  handleStoryIntake,
  handleBlockedClearedSideEffect,
} = require('./dispatchConsumer');

const PROJECT = 'hello-world';

test.beforeEach(() => taskStore._reset());

function createdEnvelope(workItemId) {
  return {
    messageId: 'wh-1',
    payload: { eventType: 'work_item.created', workItemId, data: { id: workItemId, status: 'ready' } },
  };
}

function statusChangedEnvelope(workItemId, extra = {}) {
  return {
    messageId: 'wh-2',
    payload: { eventType: 'work_item.status_changed', workItemId, data: { id: workItemId, ...extra } },
  };
}

// ---------------------------------------------------------------------------
// REQ-21's own acceptance test: a local-mode work item (no Jira issue at
// all) reaches dispatch through the SAME mechanism a Jira-originated one
// does — no local-mode-specific branch in maybeDispatch.
// ---------------------------------------------------------------------------

test('REQ-21: a local-mode Story reaching ready dispatches to refinement-agent with no Jira involvement', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-local-1', project: PROJECT, type: 'story', status: 'ready',
    assignee_agent_id: 'refinement-agent', external_key: null, parent_id: null,
    display_name: 'Local story', description: 'desc',
    storyDetail: { behavior: 'b', acceptance_criteria: 'ac', constraints: 'c', edge_cases: 'e', out_of_scope: 'oos' },
    comments: [],
  }));
  let jiraCalled = false;
  t.mock.method(jira, 'getIssue', async () => { jiraCalled = true; });
  const dispatched = [];
  t.mock.method(handlers, 'dispatchTask', async (issue, agent, opts) => {
    dispatched.push({ issue, agent, prompt: opts.promptFactory({ id: issue.key, contextId: issue.key }, { messageId: 'm-1' }) });
  });

  await maybeDispatch('wi-local-1', { messageId: 'env-1' });

  assert.equal(jiraCalled, false, 'a local-mode item must never call the Jira gateway');
  assert.equal(dispatched.length, 1);
  assert.equal(dispatched[0].issue.key, 'wi-local-1');
  assert.equal(dispatched[0].agent.id, 'refinement-agent');
  assert.match(dispatched[0].prompt, /### Behavior/);
  assert.match(dispatched[0].prompt, /## ALLOWED AGENTS/);
});

test('REQ-21: a Jira-mode item reaching ready dispatches through the identical maybeDispatch code path', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-jira-1', project: PROJECT, type: 'task', status: 'ready',
    assignee_agent_id: 'backend-agent', external_key: 'GANG-42', parent_id: null,
  }));
  t.mock.method(jira, 'getIssue', async () => ({
    key: 'GANG-42', project: 'GANG', projectName: PROJECT, parent: null, summary: 'Do the thing', comments: [],
  }));
  let transitioned = null;
  t.mock.method(jira, 'transitionIssue', async (key, status) => { transitioned = { key, status }; });
  const dispatched = [];
  t.mock.method(handlers, 'dispatchTask', async (issue, agent) => { dispatched.push({ issue, agent }); });

  await maybeDispatch('wi-jira-1', { messageId: 'env-2' });

  assert.equal(dispatched.length, 1);
  assert.equal(dispatched[0].issue.key, 'GANG-42');
  assert.equal(dispatched[0].agent.id, 'backend-agent');
  assert.deepEqual(transitioned, { key: 'GANG-42', status: 'In Progress' },
    'handleShovelReady\'s post-dispatch Jira status mirror must be preserved');
});

test('maybeDispatch does nothing when the item is not yet dispatch-eligible', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-3', project: PROJECT, type: 'task', status: 'proposed', assignee_agent_id: 'backend-agent', external_key: null,
  }));
  let called = false;
  t.mock.method(handlers, 'dispatchTask', async () => { called = true; });

  await maybeDispatch('wi-3', { messageId: 'env-3' });
  assert.equal(called, false);
});

test('maybeDispatch does nothing when no agent is assigned yet', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-4', project: PROJECT, type: 'task', status: 'ready', assignee_agent_id: null, external_key: null,
  }));
  let called = false;
  t.mock.method(handlers, 'dispatchTask', async () => { called = true; });

  await maybeDispatch('wi-4', { messageId: 'env-4' });
  assert.equal(called, false);
});

test('a dev-agent item already having a Task uses the unblock prompt on redispatch, not a fresh one', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-5', project: PROJECT, type: 'task', status: 'ready', assignee_agent_id: 'backend-agent', external_key: null,
    parent_id: null, display_name: 'X', description: '', comments: [],
  }));
  taskStore.register(buildTask({
    id: 'wi-5', contextId: 'wi-5', status: { state: 'input-required', timestamp: new Date().toISOString() },
  }));
  const dispatched = [];
  t.mock.method(handlers, 'dispatchTask', async (issue, agent, opts) => {
    dispatched.push(opts.promptFactory({ id: issue.key, contextId: issue.key }, { messageId: 'm-2' }));
  });

  await maybeDispatch('wi-5', { messageId: 'env-5' });

  assert.equal(dispatched.length, 1);
  assert.match(dispatched[0], /## RESUME POINT/);
});

// ---------------------------------------------------------------------------
// Handler 7 — rework requested (handlers.js redispatchImplementationOwner
// via handleReworkRequested), now triggered by the canonical status history
// rather than a raw Jira changelog match.
// ---------------------------------------------------------------------------

test('REQ-22: In Review -> In Progress redispatches with the retry prompt (rework)', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-6', project: PROJECT, type: 'task', status: 'in-progress', assignee_agent_id: 'backend-agent',
    external_key: 'GANG-7', parent_id: null,
    history: [
      { field: 'status', old_value: 'ready', new_value: 'in-progress' },
      { field: 'status', old_value: 'in-progress', new_value: 'in-review' },
      { field: 'status', old_value: 'in-review', new_value: 'in-progress' },
    ],
  }));
  t.mock.method(jira, 'getIssue', async () => ({
    key: 'GANG-7', project: 'GANG', projectName: PROJECT, parent: null, summary: 'X', comments: [],
  }));
  const dispatched = [];
  t.mock.method(handlers, 'dispatchTask', async (issue, agent, opts) => {
    dispatched.push(opts.promptFactory({ id: issue.key, contextId: issue.key }, { messageId: 'm-3' }));
  });

  await maybeRedispatchForRework('wi-6', { messageId: 'env-6' });

  assert.equal(dispatched.length, 1);
  assert.match(dispatched[0], /human reviewer requested rework/);
});

test('a plain ready -> in-progress transition (post-dispatch echo) does not trigger a rework redispatch', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-7', project: PROJECT, type: 'task', status: 'in-progress', assignee_agent_id: 'backend-agent',
    external_key: 'GANG-8',
    history: [{ field: 'status', old_value: 'ready', new_value: 'in-progress' }],
  }));
  let called = false;
  t.mock.method(handlers, 'dispatchTask', async () => { called = true; });

  await maybeRedispatchForRework('wi-7', { messageId: 'env-7' });
  assert.equal(called, false, 'must not double-dispatch when ScrumMaster\'s own post-dispatch transition echoes back');
});

// ---------------------------------------------------------------------------
// Handlers 1/3 — Story intake side effects (Django-decided, executed here)
// ---------------------------------------------------------------------------

test('handleStoryIntake: fields complete -> sets Agent field and posts the ack comment only', async (t) => {
  const calls = [];
  t.mock.method(jira, 'setAgentField', async (k, v) => calls.push(['setAgentField', k, v]));
  t.mock.method(jira, 'postComment', async (k, b) => calls.push(['postComment', k, b]));
  t.mock.method(jira, 'setBlockedField', async (k, v) => calls.push(['setBlockedField', k, v]));

  await handleStoryIntake({ ok: true, missing: [] }, 'GANG-1');

  assert.deepEqual(calls, [
    ['setAgentField', 'GANG-1', 'refinement-agent'],
    ['postComment', 'GANG-1', 'Ticket received. Assigned to Refinement Agent for decomposition.'],
  ]);
});

test('handleStoryIntake: fields missing -> also posts the missing-fields comment and blocks', async (t) => {
  const calls = [];
  t.mock.method(jira, 'setAgentField', async (k, v) => calls.push(['setAgentField', k, v]));
  t.mock.method(jira, 'postComment', async (k, b) => calls.push(['postComment', k, b]));
  t.mock.method(jira, 'setBlockedField', async (k, v) => calls.push(['setBlockedField', k, v]));

  await handleStoryIntake({ ok: false, missing: ['Acceptance Criteria', 'Edge Cases'] }, 'GANG-2');

  assert.equal(calls[0][0], 'setAgentField');
  assert.equal(calls[1][0], 'postComment');
  assert.match(calls[2][1], /GANG-2/);
  assert.match(calls[2][2], /Story is missing required fields/);
  assert.match(calls[2][2], /Acceptance Criteria/);
  assert.deepEqual(calls[3], ['setBlockedField', 'GANG-2', true]);
});

test('handleStoryIntake: reblock -> only the re-block comment and Blocked field, no ack/Agent-field write', async (t) => {
  const calls = [];
  t.mock.method(jira, 'setAgentField', async () => calls.push('setAgentField'));
  t.mock.method(jira, 'postComment', async (k, b) => calls.push(['postComment', b]));
  t.mock.method(jira, 'setBlockedField', async (k, v) => calls.push(['setBlockedField', v]));

  await handleStoryIntake({ ok: false, missing: ['Behavior'], reblock: true }, 'GANG-3');

  assert.equal(calls.length, 2);
  assert.match(calls[0][1], /still missing required fields/);
  assert.deepEqual(calls[1], ['setBlockedField', true]);
});

// ---------------------------------------------------------------------------
// Handler 3 (non-refinement branch) — Blocked cleared on a dev-agent ticket
// ---------------------------------------------------------------------------

test('handleBlockedClearedSideEffect redispatches a dev-agent ticket with the unblock prompt', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-8', project: PROJECT, type: 'task', status: 'in-progress', assignee_agent_id: 'backend-agent',
    external_key: null, parent_id: null, display_name: 'X', description: 'desc', comments: [
      { author: 'Jane', body: 'try again', created_at: '2026-01-01T00:00:00Z' },
    ],
  }));
  const dispatched = [];
  t.mock.method(handlers, 'dispatchTask', async (issue, agent, opts) => {
    dispatched.push(opts.promptFactory({ id: issue.key, contextId: issue.key }, { messageId: 'm-4' }));
  });

  await handleBlockedClearedSideEffect('wi-8', { messageId: 'env-8' });

  assert.equal(dispatched.length, 1);
  assert.match(dispatched[0], /## RESUME POINT/);
  assert.match(dispatched[0], /try again/);
});

// ---------------------------------------------------------------------------
// Handlers 5/6 — Release requested/abandoned/done routing
// ---------------------------------------------------------------------------

test('a work_item.jira_release_event of kind "requested" invokes handleReleaseRequested', async (t) => {
  let called = null;
  t.mock.method(handlers, 'handleReleaseRequested', async (ref) => { called = ref; });

  await handleWorkItemEventEnvelope({
    payload: { eventType: 'work_item.jira_release_event', workItemId: null, data: { kind: 'requested', jiraIssueKey: 'GANG-50' } },
  }, PROJECT);

  assert.deepEqual(called, { jiraIssueKey: 'GANG-50', workItemId: undefined, project: undefined });
});

test('a work_item.jira_release_event of kind "abandoned" invokes handleReleaseAbandoned', async (t) => {
  let called = null;
  t.mock.method(handlers, 'handleReleaseAbandoned', async (ref) => { called = ref; });

  await handleWorkItemEventEnvelope({
    payload: { eventType: 'work_item.jira_release_event', workItemId: null, data: { kind: 'abandoned', jiraIssueKey: 'GANG-51' } },
  }, PROJECT);

  assert.deepEqual(called, { jiraIssueKey: 'GANG-51', workItemId: undefined, project: undefined });
});

test('a work_item.jira_release_event of kind "done" invokes handleDone', async (t) => {
  let called = null;
  t.mock.method(handlers, 'handleDone', async (ref) => { called = ref; });

  await handleWorkItemEventEnvelope({
    payload: { eventType: 'work_item.jira_release_event', workItemId: null, data: { kind: 'done', jiraIssueKey: 'GANG-52' } },
  }, PROJECT);

  assert.deepEqual(called, { jiraIssueKey: 'GANG-52', workItemId: undefined, project: undefined });
});

test('a local-mode work_item.jira_release_event (no jiraIssueKey) invokes handleReleaseRequested with workItemId/project', async (t) => {
  let called = null;
  t.mock.method(handlers, 'handleReleaseRequested', async (ref) => { called = ref; });

  await handleWorkItemEventEnvelope({
    payload: {
      eventType: 'work_item.jira_release_event', workItemId: 'wi-release-1',
      data: { kind: 'requested', workItemId: 'wi-release-1', project: PROJECT },
    },
  }, PROJECT);

  assert.deepEqual(called, { jiraIssueKey: undefined, workItemId: 'wi-release-1', project: PROJECT });
});

test('an event type this consumer has no use for is silently ignored', async (t) => {
  let dispatchCalled = false;
  t.mock.method(handlers, 'dispatchTask', async () => { dispatchCalled = true; });

  await handleWorkItemEventEnvelope({
    payload: { eventType: 'work_item.comment_added', workItemId: 'wi-9', data: {} },
  }, PROJECT);

  assert.equal(dispatchCalled, false);
});
