'use strict';

// In-process Task/context store: identity, lifecycle enforcement, and
// lineage validation for A2A Tasks, Messages, and Artifacts.
//
// This store is intentionally in-memory only and does not survive a
// ScrumMaster restart. Durable, restart-safe Task state is a separate
// concern handled by Redis Streams; this module defines the object model
// and lifecycle rules, not their persistence. A restart while Tasks are in
// flight loses in-memory Task identity — the guarantee that a continuation
// must not create a new assignment therefore only holds within one
// ScrumMaster process lifetime.

const schema = require('./schema');

class A2ATaskNotFoundError extends Error {
  constructor(message) {
    super(message);
    this.name = 'A2ATaskNotFoundError';
  }
}

class A2ATerminalTaskError extends Error {
  constructor(message) {
    super(message);
    this.name = 'A2ATerminalTaskError';
  }
}

class A2AReferenceError extends Error {
  constructor(message) {
    super(message);
    this.name = 'A2AReferenceError';
  }
}

class A2ACausalDependencyPendingError extends Error {
  constructor(message) {
    super(message);
    this.name = 'A2ACausalDependencyPendingError';
    this.retryWithoutAttempt = true;
  }
}

class A2ACausalDependencyFailedError extends Error {
  constructor(message) {
    super(message);
    this.name = 'A2ACausalDependencyFailedError';
    this.permanent = true;
  }
}

const tasksById = new Map(); // taskId -> TaskRecord
const messageOutcomesByTask = new Map(); // taskId -> Map(messageId -> pending/succeeded/failed)

// A Task's own id is the Jira issue key itself (one Task per ticket for its
// whole lifecycle), and its context groups it with the rest of the
// coordinated body of work: a subtask shares its parent story's context, and
// a top-level story starts its own — both deterministic from the issue, not
// randomly generated, so identity survives a ScrumMaster restart even though
// in-memory message history does not (see the module comment above).
function contextFor(issue) {
  return issue.parent || issue.key;
}

function checkMessageLineage(record, message, {
  acceptedMessageIds = new Set(),
  requireSuccessfulReference = false,
} = {}) {
  if (message.taskId !== record.id) {
    throw new A2AReferenceError(
      `Message ${message.messageId} references taskId ${message.taskId}, but is being applied to task ${record.id}`
    );
  }
  if (message.referenceMessageId) {
    const found = record.messages.some(m => m.messageId === message.referenceMessageId);
    const outcome = requireSuccessfulReference
      ? messageOutcomesByTask.get(record.id)?.get(message.referenceMessageId)
      : undefined;

    // A predecessor already known dead must reject its successor outright,
    // checked ahead of the found/accepted branch below so this covers both
    // ways a predecessor can be known-failed: applied via applyTransition
    // and later failed (found === true), or dead-lettered before
    // applyTransition ever recorded it, with gateway.js's onDeadLetter hook
    // recording the failure directly instead (found === false — see
    // recordDeadLetteredMessageFailure below; without this branch that case
    // fell through to "merely accepted" and deferred forever). 'superseded'
    // is the same rejection for a predecessor a controlled reopen (below)
    // neutralized rather than deleted, so a stale successor from the
    // abandoned attempt can't pass lineage just because the entry is gone.
    if (outcome === 'failed' || outcome === 'superseded') {
      throw new A2ACausalDependencyFailedError(
        `Message ${message.messageId} cannot proceed because predecessor ${message.referenceMessageId} failed`
      );
    }

    if (!found) {
      if (acceptedMessageIds.has(message.referenceMessageId)) {
        throw new A2ACausalDependencyPendingError(
          `Message ${message.messageId} is waiting for accepted predecessor ${message.referenceMessageId}`
        );
      }
      throw new A2AReferenceError(
        `Message ${message.messageId} has an unresolvable referenceMessageId ${message.referenceMessageId}`
      );
    }

    if (outcome === 'pending') {
      throw new A2ACausalDependencyPendingError(
        `Message ${message.messageId} is waiting for predecessor ${message.referenceMessageId} to finish`
      );
    }
  }
}

function checkArtifactLineage(record, artifact) {
  if (artifact.taskId !== record.id) {
    throw new A2AReferenceError(
      `Artifact ${artifact.artifactId} references taskId ${artifact.taskId}, but is being applied to task ${record.id}`
    );
  }
  if (artifact.referenceArtifactId) {
    const found = record.artifacts.some(a => a.artifactId === artifact.referenceArtifactId);
    if (!found) {
      throw new A2AReferenceError(
        `Artifact ${artifact.artifactId} has an unresolvable referenceArtifactId ${artifact.referenceArtifactId}`
      );
    }
  }
}

// Register a brand-new Task. `task` must already be a fully-formed, schema-
// valid Task object (see a2a/parts.js builders) including its first status
// message, if any.
//
// Idempotent per task.id: a caller's handler can legitimately be retried in
// full (a partial-failure retry of the surrounding webhook/gateway handler —
// see dispatch.js and the Streams dedupeKey pattern) and rebuild the
// same taskId from scratch. The *first* registration's initial message wins;
// a retried registration attempt is a no-op that returns the existing record
// unchanged, since the actual dispatch it would have produced is separately
// deduped before it ever reaches an agent (streams.publish's dedupeKey).
function register(task) {
  schema.validateTask(task);
  const existing = tasksById.get(task.id);
  if (existing) return existing;

  const jiraIssueKey = task.metadata && task.metadata.jiraIssueKey;
  const initialMessage = task.status.message || null;

  const record = {
    id: task.id,
    contextId: task.contextId,
    jiraIssueKey: jiraIssueKey || null,
    metadata: task.metadata || {},
    state: task.status.state,
    messages: [],
    artifacts: [],
  };

  if (initialMessage) {
    checkMessageLineage(record, initialMessage);
    record.messages.push(initialMessage);
  }

  tasksById.set(record.id, record);
  const outcomes = new Map();
  if (initialMessage) outcomes.set(initialMessage.messageId, 'succeeded');
  messageOutcomesByTask.set(record.id, outcomes);
  return record;
}

function getTaskById(taskId) {
  return tasksById.get(taskId) || null;
}

// Apply an incoming/outgoing status transition to a stored Task: append the
// message and any artifacts (append-only — nothing is ever overwritten), then
// move the Task to `state`. A caller that has independently confirmed a
// canonical redispatch may set `reopen`; ordinary agent messages cannot
// reopen terminal Tasks. Gateway callers may also require a referenced
// agent message's operation to have succeeded, rather than merely having
// reached message history.
function applyTransition(taskId, {
  state,
  message,
  artifacts,
  reopen = false,
  acceptedMessageIds,
  requireSuccessfulReference = false,
} = {}) {
  const record = tasksById.get(taskId);
  if (!record) throw new A2ATaskNotFoundError(`Unknown task ${taskId}`);
  if (!schema.TASK_STATES.includes(state)) {
    throw new Error(`Unknown task state "${state}"`);
  }

  const existingMessage = message
    ? record.messages.find(candidate => candidate.messageId === message.messageId)
    : null;
  if (existingMessage && JSON.stringify(existingMessage) !== JSON.stringify(message)) {
    throw new A2AReferenceError(`Message id ${message.messageId} was already used with different content`);
  }

  const existingOutcome = existingMessage
    ? messageOutcomesByTask.get(taskId)?.get(message.messageId)
    : undefined;
  if (existingMessage && existingOutcome === 'succeeded') {
    return record;
  }

  const controlledReopen = reopen && state === 'working' && message?.role === 'client';

  // A controlled reopen is a fresh continuation: a human or canonical event
  // (pipeline retry, human rework, unblock) deliberately redispatching this
  // Task past whatever happened on its earlier attempt. A message already
  // recorded failed (markMessageFailed) belongs to that earlier attempt —
  // the agent that sent it cannot see the rejection comment it produced, and
  // a literal resend referencing it would itself be refused by the lineage
  // check below, so the only real recovery *is* a reopen. Carrying the old
  // failure forward past that point would leave the Task reading failed
  // (gateway.js's handleTaskStatus) forever, even once the redispatched
  // attempt genuinely succeeds — so a genuine reopen must stop it counting
  // against that read. It must not simply delete the entry, though: a
  // successor from the abandoned attempt can still be sitting on the
  // gateway stream deferring on this same referenceMessageId
  // (A2ACausalDependencyPendingError/retryWithoutAttempt), and deleting the
  // entry makes its next lineage check see `undefined` — indistinguishable
  // from a predecessor that simply never existed here — and pass, running
  // that stale successor's side effect on top of the new dispatch. Recording
  // 'superseded' instead keeps checkMessageLineage rejecting it (see its
  // `outcome === 'failed' || outcome === 'superseded'` branch above) without
  // it counting as this Task's own current failure. Guarded on
  // `!existingMessage` so a redelivery of the same reopen message (already
  // applied) doesn't re-run this against bookkeeping a later, unrelated
  // failure may since have added.
  if (controlledReopen && !existingMessage) {
    const outcomes = messageOutcomesByTask.get(taskId);
    if (outcomes) {
      for (const [msgId, outcome] of outcomes) {
        if (outcome === 'failed') outcomes.set(msgId, 'superseded');
      }
    }
  }

  const pendingRetry = existingMessage && existingOutcome === 'pending' && record.state === state;
  if (schema.TERMINAL_STATES.includes(record.state) && !controlledReopen && !pendingRetry) {
    throw new A2ATerminalTaskError(
      `Task ${taskId} is already terminal (${record.state}) and cannot accept further messages`
    );
  }

  if (message) {
    schema.validateMessage(message);
    checkMessageLineage(record, message, { acceptedMessageIds, requireSuccessfulReference });
    if (!existingMessage) {
      record.messages.push(message);
      messageOutcomesByTask.get(taskId).set(
        message.messageId,
        message.role === 'agent' ? 'pending' : 'succeeded'
      );
    }
  }

  for (const artifact of artifacts || []) {
    schema.validateArtifact(artifact);
    checkArtifactLineage(record, artifact);
    if (!record.artifacts.some(existing => existing.artifactId === artifact.artifactId)) {
      record.artifacts.push(artifact);
    }
  }

  record.state = state;
  return record;
}

function markMessageSucceeded(taskId, messageId) {
  const outcomes = messageOutcomesByTask.get(taskId);
  if (outcomes?.has(messageId)) outcomes.set(messageId, 'succeeded');
}

function markMessageFailed(taskId, messageId) {
  const outcomes = messageOutcomesByTask.get(taskId);
  if (outcomes?.has(messageId)) outcomes.set(messageId, 'failed');
}

// A dead-lettered gateway submission whose own processing threw before ever
// reaching applyTransition (a Redis lookup it depends on erroring, or its
// own referenceMessageId turning out unresolvable) never got an outcomes
// entry in the first place, so markMessageFailed above — guarded on an
// existing entry — silently does nothing for it. Called from gateway.js's
// onDeadLetter hook, this writes the failure regardless, so
// checkMessageLineage's `outcome === 'failed'` branch still rejects a
// successor that names it as a referenceMessageId instead of treating the
// reference as merely unresolved (acceptedMessageIds) and deferring the
// successor forever. The return value tells the caller which case this
// was, so it can log only the one markMessageFailed used to handle
// silently: 'updated' (an entry already existed — the ordinary path, where
// applyTransition ran before the later failure), 'recorded' (none existed —
// the gap this closes), or 'unknown-task' (the Task itself isn't known,
// e.g. after a restart — nothing to record against).
function recordDeadLetteredMessageFailure(taskId, messageId) {
  const outcomes = messageOutcomesByTask.get(taskId);
  if (!outcomes) return 'unknown-task';
  const existed = outcomes.has(messageId);
  outcomes.set(messageId, 'failed');
  return existed ? 'updated' : 'recorded';
}

// Message ids on this Task whose gateway-side effect was rejected or failed.
// A container's own execution outcome cannot see these — it only knows
// whether the agent process exited cleanly — so the gateway consults them
// before believing a "completed" report (see gateway.js's handleTaskStatus).
function failedMessageIds(taskId) {
  const outcomes = messageOutcomesByTask.get(taskId);
  if (!outcomes) return [];
  return Array.from(outcomes.entries())
    .filter(([, outcome]) => outcome === 'failed')
    .map(([messageId]) => messageId);
}

function lastMessage(taskId) {
  const record = tasksById.get(taskId);
  if (!record) throw new A2ATaskNotFoundError(`Unknown task ${taskId}`);
  return record.messages[record.messages.length - 1] || null;
}

// Test-only: drop all in-memory state between test cases.
function _reset() {
  tasksById.clear();
  messageOutcomesByTask.clear();
}

module.exports = {
  A2ATaskNotFoundError,
  A2ATerminalTaskError,
  A2AReferenceError,
  A2ACausalDependencyPendingError,
  A2ACausalDependencyFailedError,
  contextFor,
  register,
  getTaskById,
  applyTransition,
  markMessageSucceeded,
  markMessageFailed,
  recordDeadLetteredMessageFailure,
  failedMessageIds,
  lastMessage,
  _reset,
};
