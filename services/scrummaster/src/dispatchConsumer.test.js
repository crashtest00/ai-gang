'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

process.env.AGENTS_CATALOG_PATH = path.join(__dirname, '../config/agents.json');
process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, '../config/projects.json');

const redis = require('./redis');
const streams = require('./streams');
const canonicalWorkItems = require('./canonicalWorkItems');
const handlers = require('./handlers');
const taskStore = require('./a2a/taskStore');
const { buildTask } = require('./a2a/parts');
const {
  handleWorkItemEventEnvelope,
  maybeDispatch,
  maybeRedispatchForRework,
  handleBlockedClearedSideEffect,
  issueLikeFromCanonical,
} = require('./dispatchConsumer');

const PROJECT = 'hello-world';

test.beforeEach(() => taskStore._reset());

function createdEnvelope(workItemId) {
  return {
    messageId: 'wh-1',
    payload: { eventType: 'work_item.created', workItemId, data: { id: workItemId, status: 'ready' } },
  };
}

// ---------------------------------------------------------------------------
// Every work item reaches dispatch through one mechanism, from its canonical
// row, in every mode: after REQ-04/REQ-05 maybeDispatch has no tracker branch
// left to take.
// ---------------------------------------------------------------------------

test('a Story reaching ready dispatches to refinement-agent from its canonical row', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-local-1', project: PROJECT, type: 'story', status: 'ready',
    assignee_agent_id: 'refinement-agent', external_key: null, parent_id: null,
    display_name: 'Local story', description: 'desc',
    storyDetail: { behavior: 'b', acceptance_criteria: 'ac', constraints: 'c', edge_cases: 'e', out_of_scope: 'oos' },
    comments: [],
  }));
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ mode: 'local', jiraProjectKey: null }));
  const published = [];
  t.mock.method(canonicalWorkItems, 'publishCommand', async (project, payload) => { published.push({ project, payload }); });
  const dispatched = [];
  t.mock.method(handlers, 'dispatchTask', async (issue, agent, opts) => {
    dispatched.push({ issue, agent, prompt: opts.promptFactory({ id: issue.key, contextId: issue.key }, { messageId: 'm-1' }) });
  });

  await maybeDispatch('wi-local-1', { messageId: 'env-1' });

  assert.equal(dispatched.length, 1);
  assert.equal(dispatched[0].issue.key, 'wi-local-1');
  assert.equal(dispatched[0].agent.id, 'refinement-agent');
  assert.match(dispatched[0].prompt, /### Behavior/);
  assert.match(dispatched[0].prompt, /## ALLOWED AGENTS/);
  assert.deepEqual(published.map(c => [c.payload.command, c.payload.status]), [['transitionStatus', 'in-progress']],
    'the item must stop reading as waiting to be picked up once an agent has it');
  assert.equal(published[0].payload.workItemId, 'wi-local-1');
});

// v4.1 agent-artifact-automation.md REQ-04 (build brief §1b carry-forward
// 1) — issueLikeFromCanonical is the one place the `?full=true` read's
// specification_link/artifact_links were being dropped before reaching the
// prompt builder.

test('issueLikeFromCanonical carries the specification link and artifact links from the canonical record', () => {
  const full = {
    id: 'wi-refs-unit', project: PROJECT, display_name: 'X', status: 'ready',
    specification_link: { work_item_id: 'wi-refs-unit', artifact_id: 'art-1', requirement_id: 'REQ-9' },
    artifact_links: [
      { id: 'l1', work_item_id: 'wi-refs-unit', artifact_id: 'art-2', position: 0 },
      { id: 'l2', work_item_id: 'wi-refs-unit', artifact_id: 'art-3', position: 1 },
    ],
    comments: [],
  };

  const issueLike = issueLikeFromCanonical(full);

  assert.deepEqual(issueLike.specificationLink, { artifactId: 'art-1', requirementId: 'REQ-9' });
  assert.deepEqual(issueLike.artifactLinks, ['art-2', 'art-3']);
});

test('issueLikeFromCanonical maps an absent specification link to null and absent artifact links to an empty array', () => {
  const full = { id: 'wi-norefs-unit', project: PROJECT, display_name: 'X', status: 'ready', comments: [] };

  const issueLike = issueLikeFromCanonical(full);

  assert.equal(issueLike.specificationLink, null);
  assert.deepEqual(issueLike.artifactLinks, []);
});

// REQ-06 — the issue-like object uses canonical names throughout: `type`
// carries the canonical work-item type rather than a tracker's own issue-type
// name, and no field of it is tracker-shaped. Asserted by the object's full key
// set, so a tracker-named field reappearing fails here.
test('issueLikeFromCanonical carries the canonical type, and every field of it is canonical', () => {
  const issueLike = issueLikeFromCanonical(
    { id: 'wi-t', project: PROJECT, display_name: 'X', status: 'ready', type: 'story', comments: [] }
  );

  assert.equal(issueLike.type, 'story');
  assert.deepEqual(Object.keys(issueLike).sort(), [
    'acceptanceCriteria', 'agent', 'artifactLinks', 'behavior', 'comments',
    'constraints', 'description', 'edgeCases', 'externalKey', 'jiraProjectKey',
    'key', 'outOfScope', 'parent', 'projectName', 'specificationLink', 'status',
    'summary', 'type',
  ]);
});

// REQ-06 — the external key rides along for display exactly when the project
// is in Jira mode, and is null otherwise. Nothing branches on its value.
test('issueLikeFromCanonical carries externalKey only for a project in Jira mode', () => {
  const full = {
    id: 'wi-x', project: PROJECT, display_name: 'X', status: 'ready', type: 'task',
    external_key: 'GANG-42', comments: [],
  };

  const jiraMode = issueLikeFromCanonical(full, { mode: 'jira', jiraProjectKey: 'GANG' });
  assert.equal(jiraMode.externalKey, 'GANG-42');
  assert.equal(jiraMode.jiraProjectKey, 'GANG');

  const localMode = issueLikeFromCanonical(full, { mode: 'local', jiraProjectKey: null });
  assert.equal(localMode.externalKey, null);
  assert.equal(localMode.jiraProjectKey, null);

  assert.equal(issueLikeFromCanonical(full).externalKey, null, 'no mode read at all means no external key');
});

// The enforcement point per the build brief's gate 1: the real dispatch
// path (maybeDispatch -> issueLikeFor -> issueLikeFromCanonical ->
// buildTaskPrompt) produces a prompt naming the references, not a
// reimplementation of the mapping alone.

test('an item with references dispatches with a prompt naming its specification link and artifact ids', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-refs-1', project: PROJECT, type: 'task', status: 'ready',
    assignee_agent_id: 'backend-agent', external_key: null, parent_id: 'wi-parent',
    display_name: 'X', description: 'desc', comments: [],
    specification_link: { work_item_id: 'wi-refs-1', artifact_id: 'art-spec-1', requirement_id: 'REQ-7' },
    artifact_links: [{ id: 'l1', work_item_id: 'wi-refs-1', artifact_id: 'art-link-1', position: 0 }],
  }));
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ mode: 'local', jiraProjectKey: null }));
  t.mock.method(canonicalWorkItems, 'publishCommand', async () => {});
  const dispatched = [];
  t.mock.method(handlers, 'dispatchTask', async (issue, agent, opts) => {
    dispatched.push(opts.promptFactory({ id: issue.key, contextId: issue.key }, { messageId: 'm-refs' }));
  });

  await maybeDispatch('wi-refs-1', { messageId: 'env-refs' });

  assert.equal(dispatched.length, 1);
  assert.match(dispatched[0], /Specification link: art-spec-1 \(REQ-7\)/);
  assert.match(dispatched[0], /Artifact links: art-link-1/);
});

test('an item with no references dispatches with a prompt saying so, not omitting the lines', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-norefs-1', project: PROJECT, type: 'task', status: 'ready',
    assignee_agent_id: 'backend-agent', external_key: null, parent_id: null,
    display_name: 'X', description: 'desc', comments: [],
  }));
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ mode: 'local', jiraProjectKey: null }));
  t.mock.method(canonicalWorkItems, 'publishCommand', async () => {});
  const dispatched = [];
  t.mock.method(handlers, 'dispatchTask', async (issue, agent, opts) => {
    dispatched.push(opts.promptFactory({ id: issue.key, contextId: issue.key }, { messageId: 'm-norefs' }));
  });

  await maybeDispatch('wi-norefs-1', { messageId: 'env-norefs' });

  assert.equal(dispatched.length, 1);
  assert.match(dispatched[0], /Specification link: none/);
  assert.match(dispatched[0], /Artifact links: none/);
});

// ---------------------------------------------------------------------------
// REQ-05 / REQ-06 acceptance, at the enforcement point: a work item of a
// project in Jira mode is dispatched from its canonical row, its A2A Task is
// registered under the canonical id, and its prompt carries that id with the
// tracker key only on the External key display line. Driven through
// handleWorkItemEventEnvelope — the literal handler startDispatchConsumers
// hands streams.createConsumer — with the real handlers.dispatchTask, so the
// Task record and the published envelope are the production ones.
// ---------------------------------------------------------------------------

test('a Jira-mode work item dispatches from its canonical row, under its canonical id, with an External key line', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-jira-1', project: PROJECT, type: 'task', status: 'ready',
    assignee_agent_id: 'backend-agent', external_key: 'GANG-42', parent_id: null,
    display_name: 'Do the thing', description: 'desc', comments: [],
  }));
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ mode: 'jira', jiraProjectKey: 'GANG' }));
  const published = [];
  t.mock.method(canonicalWorkItems, 'publishCommand', async (project, payload) => { published.push({ project, payload }); });
  const dispatchEnvelopes = [];
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(streams, 'publish', async (_client, stream, envelope) => {
    dispatchEnvelopes.push({ stream, envelope });
    return { deduped: false, entryId: '0-1', messageId: envelope.messageId };
  });

  await handleWorkItemEventEnvelope(createdEnvelope('wi-jira-1'), PROJECT);

  assert.equal(dispatchEnvelopes.length, 1);
  const { envelope } = dispatchEnvelopes[0];
  assert.equal(envelope.taskId, 'wi-jira-1', 'the A2A Task id is the canonical work item id, never the tracker key');

  const record = taskStore.getTaskById('wi-jira-1');
  assert.ok(record, 'the Task is registered under the canonical id');
  assert.equal(record.metadata.jiraProjectKey, 'GANG');
  assert.equal(record.metadata.projectName, PROJECT);
  assert.equal(taskStore.getTaskById('GANG-42'), null, 'no Task is registered under the tracker key');

  const prompt = envelope.payload.parts[0].text;
  assert.match(prompt, /^Work item id: wi-jira-1$/m);
  assert.match(prompt, /^External key: GANG-42$/m);

  // REQ-04: the same canonical command is published in both modes. core's
  // write gate is what refuses it for a project in Jira mode — ScrumMaster
  // makes no such decision itself.
  assert.deepEqual(published.map(c => [c.payload.command, c.payload.status]), [['transitionStatus', 'in-progress']]);
});

test('a local-mode dispatch prompt carries the canonical id and no External key line', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-local-2', project: PROJECT, type: 'task', status: 'ready',
    assignee_agent_id: 'backend-agent', external_key: null, parent_id: null,
    display_name: 'X', description: 'desc', comments: [],
  }));
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ mode: 'local', jiraProjectKey: null }));
  t.mock.method(canonicalWorkItems, 'publishCommand', async () => {});
  const dispatched = [];
  t.mock.method(handlers, 'dispatchTask', async (issue, agent, opts) => {
    dispatched.push(opts.promptFactory({ id: issue.key, contextId: issue.key }, { messageId: 'm-loc' }));
  });

  await maybeDispatch('wi-local-2', { messageId: 'env-loc' });

  assert.match(dispatched[0], /^Work item id: wi-local-2$/m);
  assert.doesNotMatch(dispatched[0], /External key:/);
});

// REQ-04 — a refinement dispatch publishes the same transition every other
// dispatch does (audit SR-4-01, decision 2), in either mode.
test('a Jira-mode refinement dispatch publishes transitionStatus to in-progress too', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-jira-refine', project: PROJECT, type: 'story', status: 'ready',
    assignee_agent_id: 'refinement-agent', external_key: 'GANG-77', parent_id: null,
    display_name: 'X', description: 'desc', comments: [],
  }));
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ mode: 'jira', jiraProjectKey: 'GANG' }));
  const published = [];
  t.mock.method(canonicalWorkItems, 'publishCommand', async (project, payload) => { published.push(payload); });
  t.mock.method(handlers, 'dispatchTask', async () => {});

  await maybeDispatch('wi-jira-refine', { messageId: 'env-refine' });

  assert.deepEqual(published.map(p => [p.command, p.status]), [['transitionStatus', 'in-progress']]);
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
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ mode: 'local', jiraProjectKey: null }));
  t.mock.method(canonicalWorkItems, 'publishCommand', async () => {});
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
// Rework, triggered by the canonical status history
// ---------------------------------------------------------------------------

test('in-review -> in-progress redispatches with the retry prompt (rework)', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-6', project: PROJECT, type: 'task', status: 'in-progress', assignee_agent_id: 'backend-agent',
    external_key: 'GANG-7', parent_id: null, display_name: 'X', description: 'desc', comments: [],
    history: [
      { field: 'status', old_value: 'ready', new_value: 'in-progress' },
      { field: 'status', old_value: 'in-progress', new_value: 'in-review' },
      { field: 'status', old_value: 'in-review', new_value: 'in-progress' },
    ],
  }));
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ mode: 'jira', jiraProjectKey: 'GANG' }));
  const dispatched = [];
  t.mock.method(handlers, 'dispatchTask', async (issue, agent, opts) => {
    dispatched.push(opts.promptFactory({ id: issue.key, contextId: issue.key }, { messageId: 'm-3' }));
  });

  await maybeRedispatchForRework('wi-6', { messageId: 'env-6' });

  assert.equal(dispatched.length, 1);
  assert.match(dispatched[0], /human reviewer requested rework/);
  assert.match(dispatched[0], /^Work item id: wi-6$/m);
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
// Side effects: only `blocked_cleared` has a consumer here from v5.1
// ---------------------------------------------------------------------------

test('handleBlockedClearedSideEffect redispatches a dev-agent work item with the unblock prompt', async (t) => {
  t.mock.method(canonicalWorkItems, 'getWorkItem', async () => ({
    id: 'wi-8', project: PROJECT, type: 'task', status: 'in-progress', assignee_agent_id: 'backend-agent',
    external_key: null, parent_id: null, display_name: 'X', description: 'desc', comments: [
      { author: 'Jane', body: 'try again', created_at: '2026-01-01T00:00:00Z' },
    ],
  }));
  t.mock.method(canonicalWorkItems, 'getMode', async () => ({ mode: 'local', jiraProjectKey: null }));
  const dispatched = [];
  t.mock.method(handlers, 'dispatchTask', async (issue, agent, opts) => {
    dispatched.push(opts.promptFactory({ id: issue.key, contextId: issue.key }, { messageId: 'm-4' }));
  });

  await handleBlockedClearedSideEffect('wi-8', { messageId: 'env-8' });

  assert.equal(dispatched.length, 1);
  assert.match(dispatched[0], /## RESUME POINT/);
  assert.match(dispatched[0], /try again/);
});

// REQ-04 — `story_intake` has no consumer from v5.1: its Jira-visible outcome
// had no canonical sibling to fall back to, so the branch and its route are
// deleted outright and the event is ignored like any other.
test('a story_intake side effect is ignored — it has no consumer until v5.2', async (t) => {
  let dispatchCalled = false;
  t.mock.method(handlers, 'dispatchTask', async () => { dispatchCalled = true; });
  const published = [];
  t.mock.method(canonicalWorkItems, 'publishCommand', async (project, payload) => { published.push(payload); });

  await handleWorkItemEventEnvelope({
    messageId: 'env-intake',
    payload: {
      eventType: 'work_item.jira_side_effect', workItemId: 'wi-intake',
      data: { kind: 'story_intake', detail: { ok: false, missing: ['Behavior'] } },
    },
  }, PROJECT);

  assert.equal(dispatchCalled, false);
  assert.deepEqual(published, []);
});

// ---------------------------------------------------------------------------
// REQ-04 — release-event routing. A resolvable event triggers its job; one
// that names no work item, and one for a project in Jira mode, are logged as
// unresolved and trigger nothing.
// ---------------------------------------------------------------------------

function releaseEnvelope(kind, data) {
  return { messageId: 'env-rel', payload: { eventType: 'work_item.jira_release_event', workItemId: data.workItemId || null, data: { kind, ...data } } };
}

for (const [kind, handlerName] of [
  ['requested', 'handleReleaseRequested'],
  ['abandoned', 'handleReleaseAbandoned'],
  ['done', 'handleDone'],
]) {
  test(`a release event of kind "${kind}" for a local-mode project invokes ${handlerName} with the canonical id`, async (t) => {
    t.mock.method(canonicalWorkItems, 'getMode', async () => ({ mode: 'local', jiraProjectKey: null }));
    let called = null;
    t.mock.method(handlers, handlerName, async (ref) => { called = ref; });

    await handleWorkItemEventEnvelope(releaseEnvelope(kind, { workItemId: 'wi-release-1', project: PROJECT }), PROJECT);

    assert.deepEqual(called, { workItemId: 'wi-release-1', project: PROJECT });
  });

  test(`a release event of kind "${kind}" with no workItemId is unresolved and triggers no job`, async (t) => {
    const calls = [];
    for (const name of ['handleReleaseRequested', 'handleReleaseAbandoned', 'handleDone']) {
      t.mock.method(handlers, name, async () => { calls.push(name); });
    }
    const errors = [];
    t.mock.method(console, 'error', (...args) => { errors.push(args.join(' ')); });

    await handleWorkItemEventEnvelope(releaseEnvelope(kind, {}), PROJECT);

    assert.deepEqual(calls, [], 'no release handler runs, so no Jenkins job is triggered');
    assert.equal(errors.length, 1);
    assert.match(errors[0], /Unresolved release event/);
    assert.match(errors[0], new RegExp(`kind "${kind}"`));
  });

  test(`a release event of kind "${kind}" for a Jira-mode project is unresolved and triggers no job`, async (t) => {
    t.mock.method(canonicalWorkItems, 'getMode', async () => ({ mode: 'jira', jiraProjectKey: 'GANG' }));
    const calls = [];
    for (const name of ['handleReleaseRequested', 'handleReleaseAbandoned', 'handleDone']) {
      t.mock.method(handlers, name, async () => { calls.push(name); });
    }
    const errors = [];
    t.mock.method(console, 'error', (...args) => { errors.push(args.join(' ')); });

    await handleWorkItemEventEnvelope(releaseEnvelope(kind, { workItemId: 'wi-release-2', project: PROJECT }), PROJECT);

    assert.deepEqual(calls, [], 'a Jira-mode release event is not routed to its local-mode sibling');
    assert.equal(errors.length, 1);
    assert.match(errors[0], /Unresolved release event/);
    assert.match(errors[0], new RegExp(`kind "${kind}"`));
    assert.match(errors[0], /wi-release-2/);
  });
}

test('an event type this consumer has no use for is silently ignored', async (t) => {
  let dispatchCalled = false;
  t.mock.method(handlers, 'dispatchTask', async () => { dispatchCalled = true; });

  await handleWorkItemEventEnvelope({
    payload: { eventType: 'work_item.comment_added', workItemId: 'wi-9', data: {} },
  }, PROJECT);

  assert.equal(dispatchCalled, false);
});
