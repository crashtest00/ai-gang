'use strict';

// End-to-end exercise of the dispatch stream consumer against a real Redis,
// driven through dispatchConsumer.js's own public entry point
// (startDispatchConsumers/stopDispatchConsumers) — the real consumer group,
// the real handler, the real envelopes, the real publishes.
//
// The canonical work-item service does not run in this process, so it is
// stood in for at its two real boundaries and nowhere else:
//
//  - its HTTP reads (canonicalWorkItems.getWorkItem/getMode) are answered
//    from an in-memory record, the same way this suite's other integration
//    test answers Jira's;
//  - its Redis command stream is consumed by a small applier that applies a
//    transitionStatus command to that record and then republishes the
//    status-changed event the service's own outbox relay would publish —
//    same stream, same envelope shape.
//
// Nothing stubs the code under test: the command ScrumMaster publishes is
// built and published by the production path, and the status this asserts on
// is the one the applier actually persisted from that command, not a call
// that was merely recorded.

const path = require('node:path');
const { test, before, after, beforeEach } = require('node:test');
const assert = require('node:assert/strict');

process.env.AGENTS_CATALOG_PATH = path.join(__dirname, '..', 'config', 'agents.json');
process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, '..', 'config', 'projects.json');
process.env.REDIS_HOST = process.env.REDIS_TEST_HOST || 'localhost';
process.env.REDIS_PORT = process.env.REDIS_TEST_PORT || '16399';

const registry = require('../src/registry');
const streams = require('../src/streams');
const canonicalWorkItems = require('../src/canonicalWorkItems');
const taskStore = require('../src/a2a/taskStore');
const { KIND, fromStreamFields } = require('../src/envelope');
const dispatchConsumer = require('../src/dispatchConsumer');
const redisModule = require('../src/redis');

const PROJECT = 'hello-world';

let client;
let outboxSeq = 0;

// The stand-in canonical store: work item id -> record, in the full-record
// shape the service's HTTP API returns.
const workItems = new Map();
const appendedComments = [];

function seedWorkItem(item) {
  workItems.set(item.id, item);
  return item;
}

// Publish an outbound canonical event exactly as the service's outbox relay
// does — same stream, same envelope fields, same payload shape.
async function publishWorkItemEvent(eventType, workItemId, data) {
  const outboxId = `outbox-${++outboxSeq}`;
  const envelope = {
    schemaVersion: '1',
    messageId: outboxId,
    kind: KIND.WORK_ITEM_EVENT,
    project: PROJECT,
    taskId: null,
    contextId: null,
    correlationId: null,
    createdAt: new Date().toISOString(),
    payload: { outboxId, eventType, workItemId, data },
  };
  await streams.publish(client, registry.workItemEventStreamName(PROJECT), envelope);
  return envelope;
}

// Consume the canonical command stream the way the service's own command
// consumer does, apply what these tests need to the in-memory record, and
// emit the resulting outbound event. Its own consumer group, so it never
// competes with the dispatch consumer for anything.
function createCommandApplier() {
  return streams.createConsumer(client, {
    stream: canonicalWorkItems.commandStreamName(PROJECT),
    group: 'canonical-store-stand-in',
    consumerName: 'applier',
    blockMs: 50,
    handler: async envelope => {
      const payload = envelope.payload || {};
      const item = workItems.get(payload.workItemId);
      if (!item) return;
      if (payload.command === 'transitionStatus') {
        const previous = item.status;
        item.status = payload.status;
        item.history = [
          ...(item.history || []),
          { field: 'status', old_value: previous, new_value: payload.status },
        ];
        await publishWorkItemEvent('work_item.status_changed', item.id, {
          id: item.id, status: item.status, previous,
        });
      } else if (payload.command === 'appendComment') {
        appendedComments.push(payload);
      }
    },
  });
}

async function waitFor(predicate, description, timeoutMs = 5000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await predicate()) return;
    await new Promise(r => setTimeout(r, 25));
  }
  throw new Error(`timed out waiting for ${description}`);
}

before(async () => {
  await redisModule.connect();
  client = redisModule.getClient();

  canonicalWorkItems.getWorkItem = async id => {
    const item = workItems.get(id);
    return item ? JSON.parse(JSON.stringify(item)) : null;
  };
  canonicalWorkItems.getMode = async () => ({ project: PROJECT, mode: 'local', jiraProjectKey: null });
});

after(async () => {
  await client.quit();
});

beforeEach(async () => {
  await client.flushDb();
  workItems.clear();
  appendedComments.length = 0;
  taskStore._reset();
});

// A work item worked by an agent used to keep displaying as ready to pick
// up: only a project with a live Jira integration got a status mirrored onto
// it, so with no such integration an item could be dispatched, worked, and
// have a pull request opened on it while the only view an operator has still
// showed it waiting. This drives the whole path — canonical event in,
// dispatch out, transition command back into the store — and asserts on the
// status the store actually ended up holding.
test('a work item dispatched with no Jira integration ends up reading in-progress, not still ready', async () => {
  seedWorkItem({
    id: 'wi-local-story', project: PROJECT, type: 'story', status: 'ready',
    assignee_agent_id: 'refinement-agent', external_key: null, parent_id: null,
    display_name: 'Local story', description: 'desc',
    storyDetail: {
      behavior: 'b', acceptance_criteria: 'ac', constraints: 'c',
      edge_cases: 'e', out_of_scope: 'oos',
    },
    comments: [],
    history: [{ field: 'status', old_value: 'proposed', new_value: 'ready' }],
  });

  await publishWorkItemEvent('work_item.status_changed', 'wi-local-story', {
    id: 'wi-local-story', status: 'ready', previous: 'proposed',
  });

  const applier = createCommandApplier();
  await applier.start();
  await dispatchConsumer.startDispatchConsumers();
  try {
    await waitFor(
      () => workItems.get('wi-local-story').status === 'in-progress',
      'the work item to be left reading in-progress'
    );
    // Long enough for the echoed status change to have been consumed, and for
    // a second dispatch to have shown up if either dispatch-eligibility guard
    // let the echo through.
    await new Promise(r => setTimeout(r, 400));
  } finally {
    await dispatchConsumer.stopDispatchConsumers();
    await applier.stop();
  }

  assert.equal(workItems.get('wi-local-story').status, 'in-progress',
    'the item an agent is working must not still read as waiting to be picked up');

  const dispatches = await client.xRange(registry.agentStreamName(PROJECT, 'refinement'), '-', '+');
  assert.equal(dispatches.length, 1,
    'exactly one dispatch — the item\'s own transition echoes back as an event and must not redispatch it');
  const dispatched = fromStreamFields(dispatches[0].message);
  assert.equal(dispatched.taskId, 'wi-local-story');

  assert.deepEqual(
    workItems.get('wi-local-story').history.map(h => `${h.old_value}->${h.new_value}`),
    ['proposed->ready', 'ready->in-progress'],
    'one transition, recorded once'
  );

  assert.equal(
    await client.xLen(streams.deadLetterStreamName(registry.workItemEventStreamName(PROJECT))), 0,
    'nothing dead-lettered'
  );
  assert.equal(
    (await client.xPending(registry.workItemEventStreamName(PROJECT), registry.DISPATCH_GROUP)).pending, 0,
    'both the dispatch event and its echo acknowledged'
  );
});

// The same path for a dev-agent item, which is the one that goes on to open
// a pull request. Its echo must also not read as a human asking for rework:
// that is keyed off an in-review -> in-progress history entry specifically,
// and this is a ready -> in-progress one.
test('a dev-agent work item dispatched with no Jira integration reads in-progress and is not redispatched as rework', async () => {
  seedWorkItem({
    id: 'wi-local-task', project: PROJECT, type: 'task', status: 'ready',
    assignee_agent_id: 'backend-agent', external_key: null, parent_id: null,
    display_name: 'Local task', description: 'desc', comments: [],
    history: [{ field: 'status', old_value: 'proposed', new_value: 'ready' }],
  });

  await publishWorkItemEvent('work_item.status_changed', 'wi-local-task', {
    id: 'wi-local-task', status: 'ready', previous: 'proposed',
  });

  const applier = createCommandApplier();
  await applier.start();
  await dispatchConsumer.startDispatchConsumers();
  try {
    await waitFor(
      () => workItems.get('wi-local-task').status === 'in-progress',
      'the work item to be left reading in-progress'
    );
    await new Promise(r => setTimeout(r, 400));
  } finally {
    await dispatchConsumer.stopDispatchConsumers();
    await applier.stop();
  }

  assert.equal(workItems.get('wi-local-task').status, 'in-progress');
  assert.equal(
    (await client.xRange(registry.agentStreamName(PROJECT, 'backend'), '-', '+')).length, 1,
    'exactly one dispatch — the echo is not a rework request'
  );
});
