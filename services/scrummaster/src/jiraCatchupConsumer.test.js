'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

process.env.AGENTS_CATALOG_PATH = path.join(__dirname, '../config/agents.json');
process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, '../config/projects.json');

const jira = require('./jira');
const canonicalWorkItems = require('./canonicalWorkItems');
const { handleWorkItemEventEnvelope, pushWorkItemToJira, issueTypeFor } = require('./jiraCatchupConsumer');

const PROJECT = 'hello-world';

function catchupEnvelope(workItemId) {
  return {
    schemaVersion: '1',
    messageId: 'msg-1',
    kind: 'work_item_event',
    project: PROJECT,
    payload: {
      outboxId: 'outbox-1',
      eventType: 'work_item.jira_catchup_requested',
      workItemId,
      data: { workItemId, type: 'task', displayName: 'X' },
    },
  };
}

test('issueTypeFor maps known canonical types and falls back for unknown ones', () => {
  assert.equal(issueTypeFor('story'), 'Story');
  assert.equal(issueTypeFor('task'), 'Task');
  assert.equal(issueTypeFor('epic'), 'Epic');
  assert.equal(issueTypeFor(undefined), 'Task');
});

test('a non-catchup event on the fan-out stream is silently ignored', async (t) => {
  let called = false;
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => { called = true; });

  await handleWorkItemEventEnvelope({
    payload: { eventType: 'work_item.status_changed', workItemId: 'wi-1', data: {} },
  }, PROJECT);

  assert.equal(called, false);
});

test('an item that already has an external_key is skipped — REQ-15 idempotency', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({ external_key: 'GANG-1' }));
  let createIssueCalled = false;
  t.mock.method(jira, 'createIssue', async () => { createIssueCalled = true; });

  await pushWorkItemToJira('wi-1', PROJECT);

  assert.equal(createIssueCalled, false);
});

test('a missing jiraProjectKey is a retryable failure, not a dropped event', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({ external_key: null, type: 'task', display_name: 'X' }));
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ project: PROJECT, mode: 'jira', jiraProjectKey: null }));
  let createIssueCalled = false;
  t.mock.method(jira, 'createIssue', async () => { createIssueCalled = true; });

  await assert.rejects(() => pushWorkItemToJira('wi-1', PROJECT));
  assert.equal(createIssueCalled, false);
});

test('a task/story creates a top-level issue and records the returned key', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => (
    { external_key: null, type: 'story', display_name: 'Ship it', description: 'desc', parent_id: null }
  ));
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ project: PROJECT, mode: 'jira', jiraProjectKey: 'GANG' }));

  const createIssueCalls = [];
  t.mock.method(jira, 'createIssue', async (projectKey, issueType, summary, description) => {
    createIssueCalls.push({ projectKey, issueType, summary, description });
    return 'GANG-99';
  });

  const publishCalls = [];
  t.mock.method(canonicalWorkItems, 'publishCommand', async (project, payload) => {
    publishCalls.push({ project, payload });
  });

  await pushWorkItemToJira('wi-1', PROJECT);

  assert.equal(createIssueCalls.length, 1);
  assert.deepEqual(createIssueCalls[0], { projectKey: 'GANG', issueType: 'Story', summary: 'Ship it', description: 'desc' });

  assert.equal(publishCalls.length, 1);
  assert.equal(publishCalls[0].project, PROJECT);
  assert.deepEqual(publishCalls[0].payload, {
    command: 'recordExternalKey', actor: 'jira-catchup', workItemId: 'wi-1', externalKey: 'GANG-99',
  });
});

test('a subtask whose parent has no Jira key yet is retried, not created as an orphan', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async (id) => {
    if (id === 'wi-child') return { external_key: null, type: 'subtask', display_name: 'Sub', parent_id: 'wi-parent' };
    if (id === 'wi-parent') return { external_key: null };
    return null;
  });
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ project: PROJECT, mode: 'jira', jiraProjectKey: 'GANG' }));
  let createSubtaskCalled = false;
  t.mock.method(jira, 'createSubtask', async () => { createSubtaskCalled = true; });

  await assert.rejects(() => pushWorkItemToJira('wi-child', PROJECT));
  assert.equal(createSubtaskCalled, false);
});

test('a subtask whose parent already has a Jira key is created under it', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async (id) => {
    if (id === 'wi-child') return { external_key: null, type: 'subtask', display_name: 'Sub', description: 'sub desc', parent_id: 'wi-parent' };
    if (id === 'wi-parent') return { external_key: 'GANG-1' };
    return null;
  });
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ project: PROJECT, mode: 'jira', jiraProjectKey: 'GANG' }));

  const createSubtaskCalls = [];
  t.mock.method(jira, 'createSubtask', async (parentKey, projectKey, summary, description) => {
    createSubtaskCalls.push({ parentKey, projectKey, summary, description });
    return 'GANG-2';
  });
  const publishCalls = [];
  t.mock.method(canonicalWorkItems, 'publishCommand', async (project, payload) => { publishCalls.push(payload); });

  await pushWorkItemToJira('wi-child', PROJECT);

  assert.equal(createSubtaskCalls.length, 1);
  assert.deepEqual(createSubtaskCalls[0], { parentKey: 'GANG-1', projectKey: 'GANG', summary: 'Sub', description: 'sub desc' });
  assert.deepEqual(publishCalls[0], { command: 'recordExternalKey', actor: 'jira-catchup', workItemId: 'wi-child', externalKey: 'GANG-2' });
});

test('a deleted/unknown work item by the time the event is processed is a silent no-op', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => null);
  let createIssueCalled = false;
  t.mock.method(jira, 'createIssue', async () => { createIssueCalled = true; });

  await pushWorkItemToJira('wi-gone', PROJECT);

  assert.equal(createIssueCalled, false);
});

test('handleWorkItemEventEnvelope routes a catch-up event through pushWorkItemToJira', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({ external_key: 'GANG-1' }));
  let called = false;
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => { called = true; return { external_key: 'GANG-1' }; });

  await handleWorkItemEventEnvelope(catchupEnvelope('wi-1'), PROJECT);

  assert.equal(called, true);
});
