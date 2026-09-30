'use strict';

// End-to-end exercise of the gateway stream consumer against a real Redis
// instance, using gateway.js's actual public entry point
// (startGatewaySubscriber/stopGatewaySubscriber), with canonicalWorkItems.js's
// network calls monkey-patched to an in-memory fake `core`. Node caches
// CommonJS modules by reference, so mutating the already-required module's
// exports here also affects gateway.js, which requires the same cached object
// — no production code changes needed to make this testable.
//
// From v5.1 there is one destination for every gateway side effect: a
// canonical command on core's Streams command channel (REQ-04, REQ-05). The
// fake below is that channel, and a Task's id is the work item's canonical id.
//
// Gateway submissions use the canonical A2A payload shape ({ state, message,
// artifacts? }), not the legacy
// `{ type, ... }` contract that shape replaced. A real submission
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

// The in-memory stand-in for core's command channel: every command the
// gateway publishes, in order.
const fakeCore = {
  commands: [],
};

function resetFakeCore() {
  fakeCore.commands = [];
}

function commandsOf(command) {
  return fakeCore.commands.filter(c => c.command === command);
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
    metadata: { jiraProjectKey: 'HW', projectName: 'hello-world', agentId },
  }));
  return messageId;
}

before(async () => {
  await redisModule.connect();
  client = redisModule.getClient();

  canonicalWorkItems.publishCommand = async (project, payload) => {
    fakeCore.commands.push({ project, ...payload });
    return { deduped: false };
  };
  canonicalWorkItems.getWorkItem = async (id) => ({ id, status: 'ready' });
});

after(async () => {
  await client.quit();
});

beforeEach(async () => {
  await client.flushDb();
  resetFakeCore();
  taskStore._reset();
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
    kind: KIND.GATEWAY_OPERATION,
    project: 'hello-world',
    taskId: payload.message?.taskId || payload.parentWorkItemId || null,
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

async function waitFor(predicate, description, timeoutMs = 3000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await predicate()) return;
    await new Promise(r => setTimeout(r, 25));
  }
  throw new Error(`timed out waiting for ${description}`);
}

async function waitForFailedMessage(taskId, timeoutMs = 3000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (taskStore.failedMessageIds(taskId).length > 0) return;
    await new Promise(r => setTimeout(r, 25));
  }
  throw new Error(`timed out waiting for a failed message on task ${taskId}`);
}

test('create_subtask forwards exactly one materialization command, even when redelivered', async () => {
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
    await waitFor(() => commandsOf('materializeDecomposition').length === 1, 'the subtask to be forwarded to core');
    // Give the second (duplicate) entry a chance to be processed too.
    await new Promise(r => setTimeout(r, 300));
  } finally {
    await gateway.stopGatewaySubscriber();
  }

  const materializations = commandsOf('materializeDecomposition');
  assert.equal(materializations.length, 1, 'exactly one materialization, not two');
  assert.equal(materializations[0].message.parentWorkItemId, 'HW-1');
  assert.equal(materializations[0].message.subtasks.length, 1);
  assert.equal(materializations[0].message.subtasks[0].agent, 'backend-agent');

  // The gateway must NOT dispatch the subtask itself: core materializes it,
  // and dispatchConsumer.js picks its own `ready` event up.
  assert.equal(await client.xLen(agentStream('backend')), 0, 'the gateway dispatches nothing directly');
});

test('a gateway envelope claiming the wrong project is dead-lettered without any canonical write', async () => {
  registerTask('HW-1');
  const foreignEnvelope = buildEnvelope({
    kind: KIND.GATEWAY_OPERATION,
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

  assert.deepEqual(fakeCore.commands, [], 'nothing should have been written to core');
});

test('comment operation appends exactly once even if delivered twice with the same messageId', async () => {
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

  assert.equal(commandsOf('appendComment').length, 1);
  assert.match(commandsOf('appendComment')[0].body, /Finished the thing/);
});

test('durably accepted out-of-order subtask chain defers completion until both predecessors succeed', async () => {
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
  assert.equal(commandsOf('materializeDecomposition').length, 2);
});

test('a materializeDecomposition operation routes to dependencies.js and is acked on success', async () => {
  const originalFn = dependencies.routeMaterialization;
  let calledWith = null;
  dependencies.routeMaterialization = async (message, projectName) => {
    calledWith = { message, projectName };
    return { deduped: false };
  };

  try {
    await publishGatewayOp({
      operation: 'materializeDecomposition',
      parentWorkItemId: 'HW-1',
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

// A rejected or stalled decomposition is core's to report and dead-letter now
// (materialize.py), so the gateway sees no such failure. What it must still do
// is dead-letter a permanent failure to forward at all, without retrying it.
test('a permanent failure from dependencies.js is dead-lettered, not retried', async () => {
  const originalFn = dependencies.routeMaterialization;
  let attempts = 0;
  dependencies.routeMaterialization = async () => {
    attempts += 1;
    const err = new Error('the decomposition could not be forwarded');
    err.permanent = true;
    throw err;
  };

  try {
    await publishGatewayOp({
      operation: 'materializeDecomposition',
      parentWorkItemId: 'HW-1',
      subtasks: [{ id: 'proposal-1', displayName: 'x', description: 'y', agent: 'nope-agent', 'Blocked By': [] }],
    });

    await gateway.startGatewaySubscriber();
    try {
      await waitForXLen(streams.deadLetterStreamName(gatewayStream()), 1);
    } finally {
      await gateway.stopGatewaySubscriber();
    }

    assert.equal(attempts, 1, 'a permanent failure must not be retried');
  } finally {
    dependencies.routeMaterialization = originalFn;
  }
});

// REQ-06's acceptance, end to end through gateway.js's real, unmocked entry
// point: a decomposition becomes one canonical command carrying the parent's
// canonical id, with no mode read and no second destination. The regression
// this guards is the original one — a passing unit test on
// routeMaterialization while the gateway's dispatch path bypassed it.
test('a materializeDecomposition operation publishes one canonical command carrying the parent work item id', async () => {
  let modeReads = 0;
  const originalGetMode = canonicalWorkItems.getMode;
  canonicalWorkItems.getMode = async (...args) => { modeReads += 1; return originalGetMode(...args); };

  try {
    await publishGatewayOp({
      operation: 'materializeDecomposition',
      parentWorkItemId: 'local-parent-work-item-id',
      subtasks: [{ id: 'proposal-1', displayName: 'x', description: 'y', agent: 'backend-agent', 'Blocked By': [] }],
    });

    await gateway.startGatewaySubscriber();
    try {
      await waitFor(() => commandsOf('materializeDecomposition').length === 1, 'the decomposition to reach core');
    } finally {
      await gateway.stopGatewaySubscriber();
    }

    const materializations = commandsOf('materializeDecomposition');
    assert.equal(materializations.length, 1);
    assert.equal(materializations[0].message.parentWorkItemId, 'local-parent-work-item-id');
    assert.equal(modeReads, 0, 'no mode branch survives on this path');
    assert.equal((await client.xPending(gatewayStream(), registry.GATEWAY_GROUP)).pending, 0, 'entry should be acked');
  } finally {
    canonicalWorkItems.getMode = originalGetMode;
  }
});

// Regression test for a gap between what dead-lettering did and what the
// Task record was told about it: a submission whose gateway-side effect kept
// throwing a transient error was, once streams.js's own retry budget ran
// out, dead-lettered same as any other exhausted entry — but nothing told
// the A2A Task store that message had failed, so the Task kept reading as if
// it were still pending. A container that happened to exit cleanly
// regardless (it cannot see a gateway-side rejection at all) would then have
// its Task read completed, and any successor message waiting on this one as
// its referenceMessageId would defer forever (taskStore.js's
// A2ACausalDependencyPendingError / streams.js's retryWithoutAttempt), never
// itself timing out. This drives the actual gateway stream consumer
// (gateway._gatewayConsumerOptions, the same handler/onDeadLetter wiring
// startGatewaySubscriber uses in production) through real retry exhaustion,
// with only the retry timing sped up.
test('a submission that exhausts its transient retries is recorded failed on its Task, not left reading as pending forever', async () => {
  const lastMessageId = registerTask('HW-1');

  const submission = await publishGatewayOp(a2aPayload({
    taskId: 'HW-1', contextId: 'HW-1', referenceMessageId: lastMessageId, state: 'working',
    parts: [buildTextPart('progress note'), buildDataPart({ operation: 'comment' })],
  }));

  const originalPublishCommand = canonicalWorkItems.publishCommand;
  canonicalWorkItems.publishCommand = async () => { throw new Error('transient core outage'); };

  const consumer = streams.createConsumer(client, {
    stream: gatewayStream(),
    group: 'retry-exhaustion-test',
    consumerName: 'c1',
    ...gateway._gatewayConsumerOptions('hello-world'),
    blockMs: 100,
    retryDelayMs: 50,
    reclaimIntervalMs: 30,
    maxAttempts: 2,
  });
  await consumer.start();
  try {
    await waitForXLen(streams.deadLetterStreamName(gatewayStream()), 1, 5000);
    await waitForFailedMessage('HW-1', 2000);
  } finally {
    await consumer.stop();
    canonicalWorkItems.publishCommand = originalPublishCommand;
  }

  assert.deepEqual(taskStore.failedMessageIds('HW-1'), [submission.payload.message.messageId]);
});

// Regression test for a gap in the fix above: that one only covers a
// submission whose failure happens AFTER applyTransition already recorded
// it on the Task. A submission whose own referenceMessageId is
// unresolvable fails inside applyTransition itself (checkMessageLineage's
// A2AReferenceError — no `.permanent`, so streams.js retries and eventually
// dead-letters it same as any other transient failure), before the message
// is ever pushed onto the Task's history or outcomes map at all. Driven
// through the real gateway stream consumer/onDeadLetter wiring, same as the
// test above, then through gateway._handleA2ASubmission for the successor —
// the real entry point every other test in this file uses it through.
test('a dead-lettered message that never reached applyTransition is still recorded failed, so a successor referencing it is rejected rather than deferred forever', async () => {
  registerTask('HW-1', { agentId: 'refinement-agent' });

  const orphanPayload = a2aPayload({
    taskId: 'HW-1', contextId: 'HW-1', referenceMessageId: 'msg-never-published', state: 'working',
    parts: [buildTextPart('progress note'), buildDataPart({ operation: 'comment' })],
  });
  await publishGatewayOp(orphanPayload);

  const errorLines = [];
  const originalConsoleError = console.error;
  console.error = (...args) => { errorLines.push(args.join(' ')); originalConsoleError(...args); };

  const consumer = streams.createConsumer(client, {
    stream: gatewayStream(),
    group: 'unresolvable-reference-dead-letter-test',
    consumerName: 'c1',
    ...gateway._gatewayConsumerOptions('hello-world'),
    blockMs: 100,
    retryDelayMs: 50,
    reclaimIntervalMs: 30,
    maxAttempts: 2,
  });
  await consumer.start();
  try {
    await waitForXLen(streams.deadLetterStreamName(gatewayStream()), 1, 5000);
    await waitForFailedMessage('HW-1', 2000);
  } finally {
    await consumer.stop();
    console.error = originalConsoleError;
  }

  // The gap this closes: the dead-lettered message is recorded failed even
  // though it was never applied to the Task's own message history.
  assert.deepEqual(taskStore.failedMessageIds('HW-1'), [orphanPayload.message.messageId]);
  assert.ok(taskStore.getTaskById('HW-1').messages.every(m => m.messageId !== orphanPayload.message.messageId),
    'the dead-lettered message must never have been applied to the Task history — otherwise this is not exercising the gap');
  assert.ok(
    errorLines.some(line => line.includes(orphanPayload.message.messageId) && line.includes('never recorded')),
    'the hook must log when it could not find the message to mark, not just record it silently'
  );

  // Without the fix, findAcceptedPredecessor still finds the dead-lettered
  // entry on the stream (deadLetter() only xAcks, it never removes the
  // original entry) and reports it "durably accepted", so this would defer
  // forever (A2ACausalDependencyPendingError) instead of being rejected
  // outright.
  const successor = await publishGatewayOp(a2aPayload({
    taskId: 'HW-1', contextId: 'HW-1', referenceMessageId: orphanPayload.message.messageId, state: 'completed',
    parts: [buildTextPart('done')],
  }));
  await assert.rejects(
    gateway._handleA2ASubmission(successor, 'hello-world'),
    taskStore.A2ACausalDependencyFailedError
  );
});

// A stream entry for a Task that has already reached a terminal state used
// to show up in a live run as a failure ("already terminal"), a retry, and a
// second completion in the logs — the shape the dead-letter work exists to
// remove. A redelivery is the ordinary way that happens: the gateway
// acknowledged an entry it had already applied, and the consumer saw it
// again. It must be acknowledged once, cost nothing, and never re-run or
// re-report the completion it already reported.
test('a late duplicate entry for a terminal task is acknowledged once and does not report completion again', async () => {
  const lastMessageId = registerTask('HW-1');

  const completion = await publishGatewayOp(a2aPayload({
    taskId: 'HW-1', contextId: 'HW-1', referenceMessageId: lastMessageId, state: 'completed',
    parts: [buildTextPart('Verified and done')],
  }));

  const logLines = [];
  const originalConsoleLog = console.log;
  console.log = (...args) => { logLines.push(args.join(' ')); originalConsoleLog(...args); };

  await gateway.startGatewaySubscriber();
  try {
    await waitFor(() => taskStore.getTaskById('HW-1').state === 'completed', 'the Task to reach its terminal state');
    await waitFor(
      async () => (await client.xPending(gatewayStream(), registry.GATEWAY_GROUP)).pending === 0,
      'the first delivery to be acknowledged'
    );

    // The same entry arrives again, after the Task is already terminal.
    await client.xAdd(gatewayStream(), '*', { data: JSON.stringify(completion) });

    await waitFor(
      async () => (await client.xLen(gatewayStream())) === 2
        && (await client.xPending(gatewayStream(), registry.GATEWAY_GROUP)).pending === 0,
      'the duplicate to be acknowledged too'
    );
    // Long enough for a retry of the duplicate to have shown up, had it been
    // left pending instead of acknowledged.
    await new Promise(r => setTimeout(r, 300));
  } finally {
    await gateway.stopGatewaySubscriber();
    console.log = originalConsoleLog;
  }

  assert.equal(
    logLines.filter(line => line.includes('Task completed for HW-1')).length, 1,
    'the completion must be reported once, not once per delivery'
  );
  assert.equal(commandsOf('appendComment').length, 1, 'and its comment appended once');
  assert.equal(await client.xLen(streams.deadLetterStreamName(gatewayStream())), 0,
    'a duplicate is not a failure and must never dead-letter');
  assert.equal((await client.xPending(gatewayStream(), registry.GATEWAY_GROUP)).pending, 0,
    'both entries acknowledged');
  assert.equal(taskStore.failedMessageIds('HW-1').length, 0,
    'and nothing recorded against the Task that genuinely completed');
});

// A submission that arrives after its Task has already finished, carrying an
// identity of its own rather than being a redelivery of one already applied
// (the test above), used to be retried to exhaustion and dead-lettered —
// "already terminal" carried no verdict at all, so streams.js treated it as a
// transient failure. Worse, the dead letter recorded that message as failed
// against the Task, and handleTaskStatus consults exactly that before
// believing a container's report: one stray late message turned the
// container's later, truthful "completed" into "failed", and a task that
// genuinely finished lost its outcome. Driven through the real gateway stream
// consumer wiring (gateway._gatewayConsumerOptions) with only the retry
// timing sped up, so a retry or a dead letter shows up inside the test rather
// than ten minutes later.
test('a later message for a task that already finished is acknowledged and reported, and never costs that task its outcome', async () => {
  const seedMessageId = registerTask('HW-1');
  const group = 'terminal-task-resend-test';

  const completion = await publishGatewayOp(a2aPayload({
    taskId: 'HW-1', contextId: 'HW-1', referenceMessageId: seedMessageId, state: 'completed',
    parts: [buildTextPart('Verified and done')],
  }));

  const consumer = streams.createConsumer(client, {
    stream: gatewayStream(),
    group,
    consumerName: 'c1',
    ...gateway._gatewayConsumerOptions('hello-world'),
    blockMs: 100,
    retryDelayMs: 50,
    reclaimIntervalMs: 30,
    maxAttempts: 2,
  });
  await consumer.start();
  try {
    await waitFor(() => taskStore.getTaskById('HW-1').state === 'completed', 'the Task to reach its terminal state');
    await waitFor(
      async () => (await client.xPending(gatewayStream(), group)).pending === 0,
      'the completion to be acknowledged'
    );

    // A genuinely new message: its own messageId, so nothing upstream
    // recognises it as something already applied.
    const late = await publishGatewayOp(a2aPayload({
      taskId: 'HW-1', contextId: 'HW-1', state: 'working',
      parts: [buildTextPart('one more note'), buildDataPart({ operation: 'comment' })],
    }));
    assert.notEqual(late.payload.message.messageId, completion.payload.message.messageId,
      'this must not be the same message as the completion — otherwise it is the duplicate case, not this one');

    await waitFor(() => commandsOf('appendComment').length === 2, 'the late message to be reported on the work item');
    // Several retry/reclaim cycles at this consumer's timings — long enough
    // for a retry, a dead letter or a second report to have appeared.
    await new Promise(r => setTimeout(r, 400));
  } finally {
    await consumer.stop();
  }

  assert.equal((await client.xPending(gatewayStream(), group)).pending, 0,
    'the later message is acknowledged on its first delivery, not left pending to be retried');
  assert.equal(await client.xLen(streams.deadLetterStreamName(gatewayStream())), 0,
    'and never dead-lettered');
  assert.equal(taskStore.failedMessageIds('HW-1').length, 0,
    'so nothing is recorded failed against a task that genuinely finished');

  const reports = commandsOf('appendComment').filter(c => /already finished/.test(c.body));
  assert.equal(reports.length, 1, 'exactly one comment, saying the work had already finished');
  assert.equal(reports[0].workItemId, 'HW-1');
  assert.match(reports[0].body, /completed/, 'and naming the outcome it finished with');

  // The whole point: the container's own later report for this task still
  // reads as completed.
  const statusReport = buildEnvelope({
    kind: KIND.TASK_STATUS, project: 'hello-world', taskId: 'HW-1', contextId: 'HW-1',
    payload: { status: 'completed', ticket_key: 'HW-1', agent_name: 'backend-agent' },
  });
  assert.deepEqual(
    await gateway._handleTaskStatus(statusReport, 'hello-world'),
    { taskId: 'HW-1', status: 'completed' }
  );
});
