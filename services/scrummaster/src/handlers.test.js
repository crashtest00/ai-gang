'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

process.env.AGENTS_CATALOG_PATH = path.join(__dirname, '../config/agents.json');
process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, '../config/projects.json');

const redis = require('./redis');
const streams = require('./streams');
const registry = require('./registry');
const jira = require('./jira');
const jenkins = require('./jenkins');
const canonicalWorkItems = require('./canonicalWorkItems');
const taskStore = require('./a2a/taskStore');
const schema = require('./a2a/schema');
const { dispatchTask, handleBlockedCleared, handleDone, handleReleaseRequested, handleReleaseAbandoned } = require('./handlers');

const ISSUE = { key: 'GANG-42', project: 'GANG', projectName: 'hello-world', parent: null };

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
// dispatch/continuation envelope (the a2a-messaging design
// REQ-02, REQ-07: one Task per ticket for its whole lifecycle).

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

// REQ-07 — continuation reuses the existing Task and context, with a new Message identity

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

test('dispatchTask does not create a second Task for the same Jira issue', async (t) => {
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
  const subtask = { key: 'GANG-43', project: 'GANG', projectName: 'hello-world', parent: 'GANG-42' };

  await dispatchTask(subtask, agent, { promptFactory: () => 'prompt' });

  assert.equal(taskStore.getTaskById('GANG-43').contextId, 'GANG-42');
});

// handleBlockedCleared — B2: clearing Blocked on a refinement-agent story
// must not bypass the same required-fields gate handleStoryCreated applies,
// and once fields are present it must redispatch with the full task prompt
// (Behavior/Acceptance Criteria/Constraints/Edge Cases/Out of Scope), not
// the bare-description unblock prompt used for dev-agent tickets.

test('handleBlockedCleared redispatches a refinement-agent story with the full task prompt once required fields are present', async (t) => {
  const published = mockPublish(t);
  t.mock.method(jira, 'getIssue', async () => ({
    key: 'GANG-42',
    project: 'GANG',
    projectName: 'hello-world',
    parent: null,
    agent: 'refinement-agent',
    summary: 'Add password reset flow',
    description: 'bare description text',
    behavior: 'Users can request a reset link.',
    acceptanceCriteria: 'Given an email, a reset link is sent.',
    constraints: 'Must expire in 1 hour.',
    edgeCases: 'Unknown email is silently accepted.',
    outOfScope: 'SMS-based reset.',
    comments: [],
  }));
  let commentPosted = false;
  let blockedSet = false;
  t.mock.method(jira, 'postComment', async () => { commentPosted = true; });
  t.mock.method(jira, 'setBlockedField', async () => { blockedSet = true; });

  await handleBlockedCleared('GANG-42', { dispatchId: 'wh-refine-1' });

  assert.equal(commentPosted, false, 'should not re-block or comment when fields are present');
  assert.equal(blockedSet, false);
  assert.equal(published.length, 1);
  const prompt = published[0].envelope.payload.parts[0].text;
  assert.match(prompt, /### Behavior/);
  assert.match(prompt, /### Acceptance Criteria/);
  assert.match(prompt, /### Constraints/);
  assert.match(prompt, /### Edge Cases/);
  assert.match(prompt, /### Out of Scope/);
  assert.match(prompt, /## ALLOWED AGENTS/);
});

test('handleBlockedCleared re-blocks a refinement-agent story that is still missing required fields', async (t) => {
  const published = mockPublish(t);
  t.mock.method(jira, 'getIssue', async () => ({
    key: 'GANG-42',
    project: 'GANG',
    projectName: 'hello-world',
    parent: null,
    agent: 'refinement-agent',
    summary: 'Add password reset flow',
    description: 'bare description text',
    behavior: 'Users can request a reset link.',
    acceptanceCriteria: '',
    constraints: 'Must expire in 1 hour.',
    edgeCases: '',
    outOfScope: 'SMS-based reset.',
    comments: [],
  }));
  let posted = null;
  let blocked = null;
  t.mock.method(jira, 'postComment', async (key, body) => { posted = { key, body }; });
  t.mock.method(jira, 'setBlockedField', async (key, value) => { blocked = { key, value }; });

  await handleBlockedCleared('GANG-42');

  assert.equal(published.length, 0, 'must not dispatch while required fields are still missing');
  assert.deepEqual(blocked, { key: 'GANG-42', value: true });
  assert.match(posted.body, /Acceptance Criteria/);
  assert.match(posted.body, /Edge Cases/);
});

test('handleBlockedCleared still uses the bare-description unblock prompt for a dev-agent ticket', async (t) => {
  const published = mockPublish(t);
  t.mock.method(jira, 'getIssue', async () => ({
    key: 'GANG-44',
    project: 'GANG',
    projectName: 'hello-world',
    parent: null,
    agent: 'backend-agent',
    summary: 'Implement reset endpoint',
    description: 'Implement the reset endpoint per the story.',
    comments: [],
  }));

  await handleBlockedCleared('GANG-44', { dispatchId: 'wh-dev-1' });

  assert.equal(published.length, 1);
  const prompt = published[0].envelope.payload.parts[0].text;
  assert.match(prompt, /## RESUME POINT/);
  assert.doesNotMatch(prompt, /## ALLOWED AGENTS/);
  assert.doesNotMatch(prompt, /### Behavior/);
});

// Handler 5 — handleReleaseRequested triggers Jenkins only once the beta
// queue is confirmed clean (release-workflow.md).

test('handleReleaseRequested triggers the release-candidate job when the beta queue is clean', async (t) => {
  t.mock.method(jira, 'getIssue', async () => ({
    key: 'GANG-50', targetProject: 'GANG', targetProjectName: 'hello-world',
  }));
  t.mock.method(jira, 'searchIssues', async () => []);
  let triggered = null;
  t.mock.method(jenkins, 'triggerReleaseCandidate', async (issueKey, projectName) => {
    triggered = { issueKey, projectName };
  });

  await handleReleaseRequested({ jiraIssueKey: 'GANG-50' });

  assert.deepEqual(triggered, { issueKey: 'GANG-50', projectName: 'hello-world' });
});

test('handleReleaseRequested (local mode) triggers release-candidate directly — Django already checked the queue', async (t) => {
  let searchCalled = false;
  t.mock.method(jira, 'searchIssues', async () => { searchCalled = true; return []; });
  let triggered = null;
  t.mock.method(jenkins, 'triggerReleaseCandidate', async (ref, projectName) => {
    triggered = { ref, projectName };
  });

  await handleReleaseRequested({ workItemId: 'wi-release-1', project: 'hello-world' });

  assert.equal(searchCalled, false); // no Jira call at all for a local-mode event
  assert.deepEqual(triggered, { ref: { workItemId: 'wi-release-1' }, projectName: 'hello-world' });
});

test('handleReleaseRequested blocks on outstanding in-review tickets instead of triggering Jenkins', async (t) => {
  t.mock.method(jira, 'getIssue', async () => ({
    key: 'GANG-50', targetProject: 'GANG', targetProjectName: 'hello-world',
  }));
  t.mock.method(jira, 'searchIssues', async () => [{ key: 'GANG-10' }, { key: 'GANG-11' }]);
  let called = false;
  t.mock.method(jenkins, 'triggerReleaseCandidate', async () => { called = true; });
  let posted = null;
  t.mock.method(jira, 'postComment', async (key, body) => { posted = { key, body }; });

  await handleReleaseRequested({ jiraIssueKey: 'GANG-50' });

  assert.equal(called, false);
  assert.equal(posted.key, 'GANG-50');
  assert.match(posted.body, /GANG-10/);
  assert.match(posted.body, /GANG-11/);
});

test('handleReleaseRequested with no Target Project reports the error and never calls Jenkins', async (t) => {
  t.mock.method(jira, 'getIssue', async () => ({ key: 'GANG-50', targetProject: null, targetProjectName: null }));
  let searchCalled = false;
  t.mock.method(jira, 'searchIssues', async () => { searchCalled = true; return []; });
  let jenkinsCalled = false;
  t.mock.method(jenkins, 'triggerReleaseCandidate', async () => { jenkinsCalled = true; });
  let posted = null;
  t.mock.method(jira, 'postComment', async (key, body) => { posted = { key, body }; });

  await handleReleaseRequested({ jiraIssueKey: 'GANG-50' });

  assert.equal(searchCalled, false);
  assert.equal(jenkinsCalled, false);
  assert.match(posted.body, /Target Project/);
});

// Handler 4 — handleDone promotes to production only for a Release ticket
// with both a Target Project and a pinned Candidate SHA.

test('handleDone triggers production-promote for a fully-formed Release ticket', async (t) => {
  t.mock.method(jira, 'getIssue', async () => ({
    key: 'GANG-60', issuetype: 'Release', targetProject: 'GANG', targetProjectName: 'hello-world', candidateSha: 'abc1234',
  }));
  let triggered = null;
  t.mock.method(jenkins, 'triggerProductionPromote', async (issueKey, projectName, candidateSha) => {
    triggered = { issueKey, projectName, candidateSha };
  });

  await handleDone({ jiraIssueKey: 'GANG-60' });

  assert.deepEqual(triggered, { issueKey: 'GANG-60', projectName: 'hello-world', candidateSha: 'abc1234' });
});

test('handleDone (local mode) promotes using the release work item\'s recorded Candidate SHA', async (t) => {
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

test('handleDone (local mode) does not promote when no Candidate SHA is recorded', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({ id: 'wi-release-1', releaseDetail: null }));
  let called = false;
  t.mock.method(jenkins, 'triggerProductionPromote', async () => { called = true; });

  await handleDone({ workItemId: 'wi-release-1', project: 'hello-world' });

  assert.equal(called, false);
});

test('handleDone does not promote a Release ticket with no Candidate SHA set', async (t) => {
  t.mock.method(jira, 'getIssue', async () => ({
    key: 'GANG-60', issuetype: 'Release', targetProject: 'GANG', targetProjectName: 'hello-world', candidateSha: null,
  }));
  let called = false;
  t.mock.method(jenkins, 'triggerProductionPromote', async () => { called = true; });

  await handleDone({ jiraIssueKey: 'GANG-60' });

  assert.equal(called, false);
});

test('handleDone does not promote a Release ticket with no Target Project set', async (t) => {
  t.mock.method(jira, 'getIssue', async () => ({
    key: 'GANG-60', issuetype: 'Release', targetProject: null, targetProjectName: null, candidateSha: 'abc1234',
  }));
  let called = false;
  t.mock.method(jenkins, 'triggerProductionPromote', async () => { called = true; });

  await handleDone({ jiraIssueKey: 'GANG-60' });

  assert.equal(called, false);
});

test('handleDone never calls Jenkins for a non-Release ticket', async (t) => {
  t.mock.method(jira, 'getIssue', async () => ({
    key: 'GANG-61', issuetype: 'Story', targetProject: 'GANG', targetProjectName: 'hello-world', candidateSha: 'abc1234',
  }));
  let called = false;
  t.mock.method(jenkins, 'triggerProductionPromote', async () => { called = true; });

  await handleDone({ jiraIssueKey: 'GANG-61' });

  assert.equal(called, false);
});

// Handler 6 — handleReleaseAbandoned tears down the preview container so it
// doesn't outlive an abandoned Release ticket.

test('handleReleaseAbandoned tears down the preview container', async (t) => {
  t.mock.method(jira, 'getIssue', async () => ({
    key: 'GANG-62', targetProject: 'GANG', targetProjectName: 'hello-world',
  }));
  let triggered = null;
  t.mock.method(jenkins, 'triggerPreviewTeardown', async (issueKey, projectName) => {
    triggered = { issueKey, projectName };
  });

  await handleReleaseAbandoned({ jiraIssueKey: 'GANG-62' });

  assert.deepEqual(triggered, { issueKey: 'GANG-62', projectName: 'hello-world' });
});

test('handleReleaseAbandoned (local mode) tears down the preview container without calling Jira', async (t) => {
  let jiraCalled = false;
  t.mock.method(jira, 'getIssue', async () => { jiraCalled = true; return {}; });
  let triggered = null;
  t.mock.method(jenkins, 'triggerPreviewTeardown', async (ref, projectName) => {
    triggered = { ref, projectName };
  });

  await handleReleaseAbandoned({ workItemId: 'wi-release-1', project: 'hello-world' });

  assert.equal(jiraCalled, false);
  assert.deepEqual(triggered, { ref: { workItemId: 'wi-release-1' }, projectName: 'hello-world' });
});

test('handleReleaseAbandoned with no Target Project never calls Jenkins', async (t) => {
  t.mock.method(jira, 'getIssue', async () => ({ key: 'GANG-62', targetProject: null, targetProjectName: null }));
  let called = false;
  t.mock.method(jenkins, 'triggerPreviewTeardown', async () => { called = true; });

  await handleReleaseAbandoned({ jiraIssueKey: 'GANG-62' });

  assert.equal(called, false);
});
