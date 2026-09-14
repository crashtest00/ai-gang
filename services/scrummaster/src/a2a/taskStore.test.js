'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const taskStore = require('./taskStore');
const { newTaskId, newMessageId, newArtifactId } = require('./ids');
const { buildTextPart, buildMessage, buildTask, buildArtifact } = require('./parts');

function freshTask(overrides = {}) {
  const taskId = newTaskId();
  const contextId = 'ctx-test';
  const messageId = newMessageId();
  const message = buildMessage({
    messageId,
    taskId,
    contextId,
    role: 'client',
    parts: [buildTextPart('dispatch')],
  });
  const task = buildTask({
    id: taskId,
    contextId,
    status: { state: 'submitted', timestamp: new Date().toISOString(), message },
    metadata: { jiraIssueKey: `KEY-${taskId}` },
    ...overrides,
  });
  return { task, message, taskId, contextId, messageId };
}

test.beforeEach(() => taskStore._reset());

// Task identity and lifecycle

test('register + applyTransition exercises every lifecycle state', () => {
  const { task, taskId } = freshTask();
  taskStore.register(task);

  for (const state of ['working', 'input-required', 'working', 'auth-required', 'working']) {
    const record = taskStore.applyTransition(taskId, { state });
    assert.equal(record.state, state);
    assert.equal(record.id, taskId, 'task identity must not change across transitions');
  }
});

test('a terminal task rejects continuation messages', () => {
  const { task, taskId } = freshTask();
  taskStore.register(task);
  taskStore.applyTransition(taskId, { state: 'completed' });

  assert.throws(
    () => taskStore.applyTransition(taskId, { state: 'working' }),
    taskStore.A2ATerminalTaskError
  );
});

test('a controlled client redispatch reopens a terminal Task in place and retains history', () => {
  const { task, taskId, contextId, messageId } = freshTask();
  taskStore.register(task);
  taskStore.applyTransition(taskId, { state: 'completed' });

  const redispatch = buildMessage({
    messageId: newMessageId(),
    taskId,
    contextId,
    role: 'client',
    parts: [buildTextPart('redispatch after canonical ready')],
    referenceMessageId: messageId,
  });
  const record = taskStore.applyTransition(taskId, {
    state: 'working',
    message: redispatch,
    reopen: true,
  });

  assert.equal(record.id, taskId);
  assert.equal(record.contextId, contextId);
  assert.equal(record.state, 'working');
  assert.deepEqual(record.messages.map(message => message.messageId), [messageId, redispatch.messageId]);
});

test('an agent-authored message cannot use the reopen path on a terminal Task', () => {
  const { task, taskId, contextId, messageId } = freshTask();
  taskStore.register(task);
  taskStore.applyTransition(taskId, { state: 'completed' });

  const unsolicited = buildMessage({
    messageId: newMessageId(), taskId, contextId, role: 'agent',
    parts: [buildTextPart('work again')], referenceMessageId: messageId,
  });
  assert.throws(
    () => taskStore.applyTransition(taskId, { state: 'working', message: unsolicited, reopen: true }),
    taskStore.A2ATerminalTaskError
  );
});

test('a successful old message replay cannot regress a later terminal state', () => {
  const { task, taskId, contextId, messageId } = freshTask();
  taskStore.register(task);
  const working = buildMessage({
    messageId: newMessageId(), taskId, contextId, role: 'agent',
    parts: [buildTextPart('working')], referenceMessageId: messageId,
  });
  taskStore.applyTransition(taskId, { state: 'working', message: working });
  taskStore.markMessageSucceeded(taskId, working.messageId);
  taskStore.applyTransition(taskId, { state: 'completed' });

  const record = taskStore.applyTransition(taskId, { state: 'working', message: working });
  assert.equal(record.state, 'completed');
  assert.equal(record.messages.filter(message => message.messageId === working.messageId).length, 1);
});

test('an interrupted task is resumable without changing task identity', () => {
  const { task, taskId, contextId, messageId } = freshTask();
  taskStore.register(task);
  taskStore.applyTransition(taskId, { state: 'input-required' });

  const replyId = newMessageId();
  const reply = buildMessage({
    messageId: replyId,
    taskId,
    contextId,
    role: 'client',
    parts: [buildTextPart('here is the answer')],
    referenceMessageId: messageId,
  });

  const record = taskStore.applyTransition(taskId, { state: 'working', message: reply });
  assert.equal(record.id, taskId);
  assert.equal(record.contextId, contextId);
  assert.equal(record.state, 'working');
  assert.equal(taskStore.lastMessage(taskId).messageId, replyId, 'a new Message identity is created for the continuation');
});

// Lineage and immutability

test('lineage can be walked backward through referenceMessageId', () => {
  const { task, taskId, contextId, messageId } = freshTask();
  taskStore.register(task);

  const secondId = newMessageId();
  taskStore.applyTransition(taskId, {
    state: 'working',
    message: buildMessage({ messageId: secondId, taskId, contextId, role: 'agent', parts: [buildTextPart('ack')], referenceMessageId: messageId }),
  });

  const thirdId = newMessageId();
  taskStore.applyTransition(taskId, {
    state: 'working',
    message: buildMessage({ messageId: thirdId, taskId, contextId, role: 'client', parts: [buildTextPart('thanks')], referenceMessageId: secondId }),
  });

  // Walk backward from the last message to the first.
  const record = taskStore.getTaskById(taskId);
  const byId = new Map(record.messages.map(m => [m.messageId, m]));
  let cursor = byId.get(thirdId);
  const chain = [cursor.messageId];
  while (cursor.referenceMessageId) {
    cursor = byId.get(cursor.referenceMessageId);
    chain.push(cursor.messageId);
  }
  assert.deepEqual(chain, [thirdId, secondId, messageId]);
});

test('a revision is a new object and does not overwrite the original', () => {
  const { task, taskId } = freshTask();
  taskStore.register(task);

  const first = buildArtifact({ artifactId: newArtifactId(), taskId, name: 'pull-request', parts: [buildTextPart('v1')] });
  taskStore.applyTransition(taskId, { state: 'working', artifacts: [first] });

  const revision = buildArtifact({
    artifactId: newArtifactId(),
    taskId,
    name: 'pull-request',
    parts: [buildTextPart('v2')],
    referenceArtifactId: first.artifactId,
  });
  taskStore.applyTransition(taskId, { state: 'working', artifacts: [revision] });

  const record = taskStore.getTaskById(taskId);
  assert.equal(record.artifacts.length, 2, 'the original artifact record is preserved, not overwritten');
  assert.equal(record.artifacts[0].parts[0].text, 'v1');
  assert.equal(record.artifacts[1].referenceArtifactId, first.artifactId);
});

test('an unresolvable required reference produces an explicit failure', () => {
  const { task, taskId, contextId } = freshTask();
  taskStore.register(task);

  const dangling = buildMessage({
    messageId: newMessageId(),
    taskId,
    contextId,
    role: 'agent',
    parts: [buildTextPart('orphaned reply')],
    referenceMessageId: 'msg-does-not-exist',
  });

  assert.throws(
    () => taskStore.applyTransition(taskId, { state: 'working', message: dangling }),
    taskStore.A2AReferenceError
  );
});

test('a durably accepted same-Task predecessor waits instead of failing lineage', () => {
  const { task, taskId, contextId } = freshTask();
  taskStore.register(task);
  const acceptedMessageId = newMessageId();
  const successor = buildMessage({
    messageId: newMessageId(), taskId, contextId, role: 'agent',
    parts: [buildTextPart('successor')], referenceMessageId: acceptedMessageId,
  });

  assert.throws(
    () => taskStore.applyTransition(taskId, {
      state: 'working',
      message: successor,
      acceptedMessageIds: new Set([acceptedMessageId]),
      requireSuccessfulReference: true,
    }),
    taskStore.A2ACausalDependencyPendingError
  );
  assert.equal(taskStore.getTaskById(taskId).messages.some(message => message.messageId === successor.messageId), false);
});

// Regression test for a gap in the controlled-reopen path: it used to
// delete a superseded predecessor's failed outcome outright, so a stale
// successor still waiting on that same referenceMessageId — one that was
// deferring on A2ACausalDependencyPendingError before the reopen — would
// pass checkMessageLineage's lineage check on its next retry (undefined is
// neither 'pending' nor 'failed') and go on to run its side effect from the
// abandoned attempt on top of the fresh dispatch. Driven through
// applyTransition/checkMessageLineage, the real lineage-check entry point
// every gateway submission goes through — not a direct call to a reopen
// helper.
test('a redispatch does not un-gate a stale successor still waiting on the rejection it superseded', () => {
  const { task, taskId, contextId, messageId: seedMessageId } = freshTask();
  taskStore.register(task);

  const rejected = buildMessage({
    messageId: newMessageId(), taskId, contextId, role: 'agent',
    parts: [buildTextPart('will be rejected')], referenceMessageId: seedMessageId,
  });
  taskStore.applyTransition(taskId, { state: 'working', message: rejected });
  taskStore.markMessageFailed(taskId, rejected.messageId);

  const stillPending = buildMessage({
    messageId: newMessageId(), taskId, contextId, role: 'agent',
    parts: [buildTextPart('successor of the rejected message')], referenceMessageId: rejected.messageId,
  });

  // Before the redispatch: the successor is correctly rejected outright —
  // this is the existing, already-working half of the guarantee.
  assert.throws(
    () => taskStore.applyTransition(taskId, { state: 'working', message: stillPending, requireSuccessfulReference: true }),
    taskStore.A2ACausalDependencyFailedError
  );

  const redispatch = buildMessage({
    messageId: newMessageId(), taskId, contextId, role: 'client',
    parts: [buildTextPart('redispatch after canonical ready')], referenceMessageId: seedMessageId,
  });
  taskStore.applyTransition(taskId, { state: 'working', message: redispatch, reopen: true });

  // After the redispatch: the successor's own reclaim retries the exact
  // same message again. It must still be rejected outright — not silently
  // pass lineage and apply its stale side effect on top of the new
  // dispatch just because the predecessor's failed entry is gone.
  assert.throws(
    () => taskStore.applyTransition(taskId, { state: 'working', message: stillPending, requireSuccessfulReference: true }),
    taskStore.A2ACausalDependencyFailedError
  );
  assert.equal(
    taskStore.getTaskById(taskId).messages.some(m => m.messageId === stillPending.messageId),
    false,
    'the stale successor must never have been applied to the Task'
  );
});

test('applying a transition to an unknown task fails explicitly', () => {
  assert.throws(() => taskStore.applyTransition('task-unknown', { state: 'working' }), taskStore.A2ATaskNotFoundError);
});

// contextId grouping

test('a subtask shares its parent story\'s contextId', () => {
  const story = { key: 'GANG-1', parent: null };
  const subtask = { key: 'GANG-2', parent: 'GANG-1' };
  assert.equal(taskStore.contextFor(subtask), taskStore.contextFor(story));
});

test('two unrelated stories get different contextIds', () => {
  const a = { key: 'GANG-1', parent: null };
  const b = { key: 'GANG-9', parent: null };
  assert.notEqual(taskStore.contextFor(a), taskStore.contextFor(b));
});
