'use strict';

const crypto = require('crypto');
const redis = require('./redis');
const registry = require('./registry');
const streams = require('./streams');
const idempotency = require('./idempotency');
const dependencies = require('./dependencies');
const canonicalWorkItems = require('./canonicalWorkItems');
const assignment = require('./assignment');
const taskStore = require('./a2a/taskStore');
const schema = require('./a2a/schema');
const { buildEnvelope, KIND, fromStreamFields } = require('./envelope');

// One durable consumer per project's gateway stream (aigang:gateway:{project},
// group "scrummaster"). Project identity comes from which stream a consumer is
// bound to, never from message content.
const consumers = [];

// A container's subscriber can only report whether the agent process exited
// cleanly — it has no way to know that the gateway side effect one of its
// submissions asked for was itself rejected or never landed. handleTaskStatus
// covers the case where that rejection happened synchronously, within the
// same delivery, by marking the message failed before returning (see its own
// comment). It has no way to cover a submission whose gateway-side handling
// merely errored transiently and was later dead-lettered after exhausting
// retries on a wholly separate delivery — nothing calls markMessageFailed for
// that path otherwise, so the Task keeps reading as if the submission were
// still pending, its successor stays deferred forever (taskStore's
// requireSuccessfulReference/retryWithoutAttempt), and a container that
// happens to exit cleanly anyway is reported completed. This is the other
// half of that guarantee: whatever the gateway stream ever dead-letters is
// also reflected onto the Task it belonged to.
//
// A submission can dead-letter before it was ever recorded on the Task at
// all, though: a Redis lookup findAcceptedPredecessor depends on erroring,
// or the submission's own referenceMessageId turning out unresolvable, both
// throw ahead of applyTransition ever running. recordDeadLetteredMessageFailure
// writes the failure regardless of whether an entry already existed, so a
// successor that names this messageId as its own referenceMessageId is
// still rejected outright by taskStore's lineage check instead of reading it
// as merely unresolved (accepted-but-not-yet-applied) and deferring forever.
function markDeadLetteredSubmissionFailed(envelope) {
  const taskId = envelope && envelope.taskId;
  const messageId = envelope && envelope.payload && envelope.payload.message && envelope.payload.message.messageId;
  if (!taskId || !messageId) return;
  const result = taskStore.recordDeadLetteredMessageFailure(taskId, messageId);
  if (result === 'unknown-task') {
    console.warn(`[gateway] Dead-lettered message ${messageId} names unknown task ${taskId} — nothing to record`);
  } else if (result === 'recorded') {
    console.error(
      `[gateway] Dead-lettered message ${messageId} for task ${taskId} was never recorded on the Task — ` +
      `recording its failure directly so a successor referencing it as its own predecessor is not deferred forever`
    );
  }
}

// The exact handler/onDeadLetter wiring one project's gateway stream
// consumer uses — factored out so a test can drive it through
// streams.createConsumer with its own (fast) retry timing instead of
// production's, while still exercising the real wiring rather than a
// reimplementation of it.
function gatewayConsumerOptions(projectName) {
  return {
    handler: envelope => handleGatewayEnvelope(envelope, projectName),
    onDeadLetter: markDeadLetteredSubmissionFailed,
  };
}

async function startGatewaySubscriber() {
  const client = redis.getClient();
  const consumerName = process.env.SCRUMMASTER_CONSUMER_ID || require('os').hostname();

  for (const projectName of registry.getProjectNames()) {
    const stream = registry.gatewayStreamName(projectName);
    const consumer = streams.createConsumer(client, {
      stream,
      group: registry.GATEWAY_GROUP,
      consumerName,
      ...gatewayConsumerOptions(projectName),
    });
    await consumer.start();
    consumers.push(consumer);
    console.log(`[gateway] Consuming ${stream} as ${registry.GATEWAY_GROUP}/${consumerName}`);
  }
}

async function stopGatewaySubscriber() {
  await Promise.all(consumers.splice(0).map(c => c.stop()));
}

// Top-level entry point for every gateway stream entry. Rejects a payload
// whose declared project doesn't match the stream it arrived on (dead-letters
// without ever reaching a handler), then runs the operation exactly
// once per messageId so a redelivered entry that already completed
// doesn't repeat its side effects.
async function handleGatewayEnvelope(envelope, projectName) {
  const expectedProject = registry.normalizeProjectName(projectName);
  if (envelope.project !== expectedProject) {
    const err = new Error(
      `gateway envelope declares project "${envelope.project}", does not match destination stream's project "${expectedProject}"`
    );
    err.permanent = true;
    throw err;
  }

  const client = redis.getClient();
  const { duplicate, outcome } = await idempotency.once(client, 'gateway', envelope.messageId, () =>
    dispatchGatewayOperation(envelope, projectName)
  );

  if (duplicate) {
    console.log(`[gateway] Duplicate delivery of ${envelope.messageId} on ${projectName} — already applied, outcome:`, outcome);
  }
  return outcome;
}

// Routes a gateway envelope to the right handler family:
//  - kind=TASK_STATUS: the project container's own subscriber reporting an
//    execution outcome — infrastructure-level, not agent-authored A2A
//    content.
//  - operation=materializeDecomposition: dependency handling's own
//    structured-data contract.
//  - type=pipeline_retry: Jenkins-originated, not agent-authored A2A content.
//  - everything else: agent-authored A2A content — comment, reassign,
//    create_subtask, blocked, and completed (with an optional pull-request
//    Artifact) all arrive here as one canonical
//    `{ state, message, artifacts? }` submission.
async function dispatchGatewayOperation(envelope, projectName) {
  if (envelope.kind === KIND.TASK_STATUS) {
    return handleTaskStatus(envelope, projectName);
  }

  const msg = envelope.payload || {};

  if (msg.operation === 'materializeDecomposition') {
    return handleMaterializeDecomposition(msg, projectName);
  }

  if (msg.type === 'pipeline_retry') {
    return handlePipelineRetry(msg, projectName);
  }

  return handleA2ASubmission(envelope, projectName);
}

// Forward a Refinement Agent decomposition to core, in every mode:
// dependencies.js's routeMaterialization publishes one canonical
// materializeDecomposition command, and core's own materializer validates the
// proposed owners and applies the plan. A rejected or stalled decomposition is
// reported by core on the parent work item and dead-lettered there — this
// gateway sees no such failure, because it makes no decision about the plan.
// A failure raised here is a failure to publish, which the normal
// retry/dead-letter path handles, except one already flagged permanent, which
// dead-letters immediately rather than burning retry attempts.
async function handleMaterializeDecomposition(msg, projectName) {
  const result = await dependencies.routeMaterialization(
    { parentId: msg.parentWorkItemId, subtasks: msg.subtasks },
    projectName
  );
  console.log(`[gateway] Forwarded decomposition under ${msg.parentWorkItemId}`);
  return result;
}

// Handle a terminal Task outcome reported by a project container's
// subscriber. A 'completed' status is informational
// — the agent's own gateway submission already carries the human-readable
// summary. A 'failed' status (retry exhaustion, timeout, or an invalid
// message) has no such comment, so ScrumMaster must post one itself and
// surface the ticket as needing attention rather than leaving it silently
// stuck `in-progress`. Also mirrors the outcome into the A2A Task record
// where one exists — best-effort: a process restart or a race with the
// agent's own completion report can mean there's nothing (or an
// already-terminal record) to update, which is not itself an error.
async function handleTaskStatus(envelope, projectName) {
  const { status, ticket_key, agent_name, reason, diagnostic } = envelope.payload || {};
  const taskId = envelope.taskId;

  if (!taskId) {
    console.warn('[gateway] task_status envelope missing taskId — dropping');
    return null;
  }

  // A container's subscriber reports only whether the agent process exited
  // cleanly. It cannot see that one of the submissions that process sent was
  // rejected here, or that a later one was dead-lettered for depending on a
  // rejected predecessor. Believing such a report is what let a story whose
  // subtask was never created still log as completed, so a Task carrying a
  // failed submission is recorded and logged as failed regardless of what
  // the container reports.
  const failedMessages = status === 'completed' ? taskStore.failedMessageIds(taskId) : [];
  const effectiveStatus = failedMessages.length > 0 ? 'failed' : status;

  if (effectiveStatus === 'completed' || effectiveStatus === 'failed') {
    try {
      taskStore.applyTransition(taskId, { state: effectiveStatus });
    } catch (err) {
      // Unknown task (restart) or already terminal (race with the agent's
      // own report) — the canonical-projection behavior below still applies.
    }
  }

  if (status === 'completed') {
    if (failedMessages.length > 0) {
      console.error(
        `[gateway] Task ${taskId} reported completed by its container (agent=${agent_name || 'unknown'}) ` +
        `but ${failedMessages.length} of its gateway submission(s) failed (${failedMessages.join(', ')}) — recording it as failed`
      );
      return { taskId, status: 'failed', failedMessageIds: failedMessages };
    }
    console.log(`[gateway] Task ${taskId} completed (agent=${agent_name || 'unknown'})`);
    return { taskId, status };
  }

  if (status === 'failed') {
    const key = ticket_key || taskId;
    const label = agent_name || 'Agent';
    let comment = `[${label}] Execution failed for this task and retries were exhausted.\n\nReason: ${reason || '(no reason provided)'}`;
    if (diagnostic) comment += `\n\nDiagnostic: ${diagnostic}`;
    comment += `\nTicket: ${key}`;

    await canonicalWorkItems.publishCommand(projectName, {
      command: 'transitionStatus', actor: label, workItemId: key, status: 'failed',
    });
    await postComment(key, { projectName, messageId: envelope.messageId }, label, comment, null);
    console.error(`[gateway] Task ${taskId} failed — ${key} blocked, reason: ${reason}`);
    return { taskId, status };
  }

  console.warn(`[gateway] Unknown task_status "${status}" for task ${taskId}`);
  return null;
}

// Handle a pipeline-failure retry request from Jenkins. Jenkins names the
// affected work item only by the tracker key it finds in the failed build's
// branch name, and from v5.1 no ScrumMaster module may use such a key to
// find anything (REQ-05, REQ-07). So this handler dispatches no one, derives
// no Redis key from `ticket_key`, and records the message where an operator
// will see it: at error level, naming the key and the build.
//
// The fix is already planned and not rebuilt here: v5.2's Canonical Delivery
// State REQ-01 replaces `ticket_key` with the failed build's pull requests,
// routes the message through core, and has core publish the retry for
// ScrumMaster with the canonical workItemId. Its routing above and
// a2a-validate.js's `pipeline_retry` rule are unchanged, so the message is
// still validated, accepted and acknowledged rather than dead-lettered.
async function handlePipelineRetry(msg, _projectName) {
  const { ticket_key, build_url, build_number } = msg;

  if (!ticket_key) {
    console.warn('[gateway] pipeline_retry message missing ticket_key — dropping');
    return null;
  }

  console.error(
    `[gateway] Unresolved pipeline_retry naming "${ticket_key}" (build ${build_number || build_url || 'unknown'}) — ` +
    `ScrumMaster cannot use an external tracker key to find a work item, so no agent was redispatched; ` +
    `carrying the canonical work item id here is v5.2's`
  );
  return { ticket_key, unresolved: true };
}

// Apply an incoming agent submission to the stored A2A Task, then translate
// the resulting state/message/artifacts into canonical-state side effects
// (comment, needs-clarification, assign, create_subtask, open_pr), routed
// through core's Streams command channel. There is no mode branch from v5.1:
// ScrumMaster publishes the same canonical commands in every mode, and for a
// project in Jira mode core's write gate refuses each gated one and
// dead-letters it as WRITE_GATE_REJECTED.
//
// The Task's own id is the work item's canonical id, in every mode
// (handlers.js's dispatchTask registers it as `issue.key`, which
// dispatchConsumer.js's issueLikeFromCanonical sets from the canonical
// record), so `record.id` below is the work item every side effect applies
// to.
async function handleA2ASubmission(envelope, projectName) {
  const msg = envelope.payload || {};
  const errors = [];
  if (!schema.TASK_STATES.includes(msg.state)) errors.push(`payload.state must be one of ${schema.TASK_STATES.join('/')}`);
  try {
    schema.validateMessage(msg.message);
  } catch (err) {
    errors.push(err.message);
  }
  for (const artifact of msg.artifacts || []) {
    try {
      schema.validateArtifact(artifact);
    } catch (err) {
      errors.push(err.message);
    }
  }
  if (errors.length > 0) {
    console.warn(`[gateway] Rejected invalid A2A submission for task ${envelope.taskId}:`, errors.join('; '));
    return null;
  }

  const taskId = envelope.taskId;
  const record = taskStore.getTaskById(taskId);
  if (!record) {
    console.warn(`[gateway] Unknown task ${taskId} on ${registry.gatewayStreamName(projectName)} — dropping`);
    return null;
  }

  const taskProjectName = record.metadata.projectName?.toLowerCase();
  if (taskProjectName && taskProjectName !== registry.normalizeProjectName(projectName)) {
    console.warn(`[gateway] Task ${taskId} belongs to project "${taskProjectName}", not "${projectName}" — dropping`);
    return null;
  }

  const message = msg.message;
  if (message.role !== 'agent') {
    console.warn(`[gateway] Task ${taskId} submission has role "${message.role}", expected "agent" — dropping`);
    return null;
  }

  const acceptedMessageIds = await findAcceptedPredecessor(record, message, projectName);
  const stateBeforeApplying = record.state;
  try {
    taskStore.applyTransition(taskId, {
      state: msg.state,
      message,
      artifacts: msg.artifacts,
      acceptedMessageIds,
      requireSuccessfulReference: true,
    });
  } catch (err) {
    if (err instanceof taskStore.A2ATerminalTaskError) {
      await reportTaskAlreadyFinished(record, stateBeforeApplying, projectName, envelope);
      return { ticket_key: record.id, alreadyFinished: stateBeforeApplying };
    }
    throw err;
  }

  const workItemId = record.id;

  let outcome;
  try {
    const ctx = { projectName, messageId: envelope.messageId };

    const agentName = deriveAgentDisplayName(record);
    const textPart = message.parts.find(p => p.kind === 'text');
    const dataPart = message.parts.find(p => p.kind === 'data');
    const body = textPart?.text;
    const operation = dataPart?.data?.operation;
    const reference = dataPart?.data?.reference;

    if (schema.INTERRUPTED_STATES.includes(msg.state)) {
      await handleInterrupted(workItemId, agentName, msg.state, body, reference, ctx);
      outcome = { ticket_key: workItemId };
    } else if (msg.state === 'completed') {
      await handleCompleted(workItemId, agentName, body, msg.artifacts, ctx);
      outcome = { ticket_key: workItemId };
    } else if (msg.state === 'failed' || msg.state === 'canceled' || msg.state === 'rejected') {
      await handleTerminalFailure(workItemId, agentName, msg.state, body, ctx);
      outcome = { ticket_key: workItemId };
    } else {
      // Non-terminal, non-interrupted (submitted/working): branch on the
      // requested operation, if any.
      switch (operation) {
        case 'comment':
          await postFormattedComment(workItemId, agentName, body, reference, ctx);
          outcome = { ticket_key: workItemId };
          break;
        case 'reassign':
          outcome = await handleReassign(record, dataPart.data.agentFieldValue, agentName, ctx)
            ? { ticket_key: workItemId }
            : null;
          break;
        case 'create_subtask':
          outcome = await handleCreateSubtask(record, dataPart.data, ctx);
          break;
        case undefined:
          // A working-state message with no data part is a plain progress note.
          if (body) await postFormattedComment(workItemId, agentName, body, reference, ctx);
          outcome = { ticket_key: workItemId };
          break;
        default:
          await reportUnsupportedOperation(workItemId, operation, ctx);
          outcome = null;
      }
    }
  } catch (err) {
    if (err?.permanent) taskStore.markMessageFailed(taskId, message.messageId);
    throw err;
  }

  if (outcome === null) taskStore.markMessageFailed(taskId, message.messageId);
  else taskStore.markMessageSucceeded(taskId, message.messageId);
  return outcome;
}

// A genuinely new submission that arrives after its Task has already
// finished. This is not a redelivery of something already applied — that is
// recognised a layer up, by messageId, and costs nothing. This is a later
// message with its own identity, and there is no longer anything to apply it
// to.
//
// Retrying it cannot help: the Task's state will not become non-terminal on
// its own, so every attempt fails identically until the entry is
// dead-lettered — and dead-lettering it records that message as failed
// against the Task (markDeadLetteredSubmissionFailed above), which then
// turns the container's later, truthful "completed" report into "failed"
// (handleTaskStatus consults failedMessageIds). One stray late message would
// cost a Task that genuinely finished its own outcome.
//
// So the entry is acknowledged on its first delivery instead: no retries, no
// dead letter, nothing recorded against the Task. What an operator needs —
// that a further update arrived too late to be applied, and what the work had
// already finished as — is written where they will see it, on the work item
// itself. Nothing else about the item changes: its recorded outcome is
// exactly what this is protecting.
async function reportTaskAlreadyFinished(record, finishedState, projectName, envelope) {
  const ticketKey = record.id;
  const ctx = { projectName, messageId: envelope.messageId };
  const comment =
    `[system] A further update arrived for this work item after its assigned work had already finished ` +
    `(${finishedState}), so it was not applied and the recorded outcome stands.\n\n` +
    `Restarting work on this item is a fresh dispatch of it, not a resend.\n` +
    `Ticket: ${ticketKey}`;

  await postComment(ticketKey, ctx, 'system', comment, null);
  console.warn(
    `[gateway] Task ${record.id} is already ${finishedState} — a later message was acknowledged without being ` +
    `applied, and reported on ${ticketKey}`
  );
}

// A reference absent from in-memory history may still be a valid predecessor
// already durably accepted onto this Task's gateway stream. The producer's
// existing reference chain is the causal contract; Redis fetch batches do
// not create an additional submission/batch protocol.
async function findAcceptedPredecessor(record, message, projectName) {
  const referenceMessageId = message.referenceMessageId;
  if (!referenceMessageId || record.messages.some(candidate => candidate.messageId === referenceMessageId)) {
    return undefined;
  }

  const client = redis.getClient();
  const stream = registry.gatewayStreamName(projectName);
  const entries = await client.xRange(stream, '-', '+');
  for (const entry of entries) {
    const accepted = fromStreamFields(entry.message);
    if (
      accepted?.taskId === record.id &&
      accepted.payload?.message?.taskId === record.id &&
      accepted.payload.message.messageId === referenceMessageId
    ) {
      return new Set([referenceMessageId]);
    }
  }
  return undefined;
}

function deriveAgentDisplayName(record) {
  const agent = registry.getAgent(record.metadata.agentId);
  return agent?.displayName || record.metadata.agentId || 'Agent';
}

function formatReference(reference) {
  if (!reference) return null;
  return typeof reference === 'string'
    ? reference
    : `${reference.file}${reference.function ? ` → ${reference.function}` : ''}`;
}

// `reference` is untyped agent-supplied data (dataPart.data.reference) —
// either a plain string or a { file, function } pair. The internal API's
// appendComment command has dedicated referenceFile/referenceFunction
// fields, so pull them out structurally when available; a bare string
// reference has nothing to split and still
// reaches the reader via the formatted comment body itself.
function referenceFields(reference) {
  if (reference && typeof reference === 'object') {
    return { referenceFile: reference.file || null, referenceFunction: reference.function || null };
  }
  return { referenceFile: null, referenceFunction: null };
}

// Post one comment. `formattedBody` is the exact text, routed through core's
// appendComment Streams command — the only destination in every mode.
// `ctx.messageId` is threaded through as the comment's
// sourceMessageId so a redelivered gateway entry can't double-post it
// (append_comment's own redelivery guard).
async function postComment(ticketKey, ctx, agentName, formattedBody, reference) {
  const { referenceFile, referenceFunction } = referenceFields(reference);
  await canonicalWorkItems.publishCommand(ctx.projectName, {
    command: 'appendComment',
    actor: agentName,
    workItemId: ticketKey,
    author: agentName,
    body: formattedBody,
    referenceFile,
    referenceFunction,
    sourceMessageId: ctx.messageId || null,
  });
}

// Enforce the Agent Comment Standard from the spec
async function postFormattedComment(ticketKey, agentName, body, reference, ctx) {
  let formatted = `[${agentName}] ${body || '(no summary provided)'}`;
  const ref = formatReference(reference);
  if (ref) {
    formatted += `\n\nReference: ${ref}`;
  }
  formatted += `\nTicket: ${ticketKey}`;

  await postComment(ticketKey, ctx, agentName, formatted, reference);
  console.log(`[gateway] Posted comment on ${ticketKey}`);
}

// 'needs-clarification' is the minimum-vocabulary status that means
// "blocked / needs input", so an interrupted submission transitions the work
// item into it.
async function handleInterrupted(ticketKey, agentName, state, body, reference, ctx) {
  const label = state === 'auth-required' ? 'AUTHORIZATION REQUIRED' : 'BLOCKED';
  let comment = `[${agentName}] ${label} — ${body || '(no reason provided)'}`;
  const ref = formatReference(reference);
  if (ref) comment += `\n\nReference: ${ref}`;
  comment += `\nTicket: ${ticketKey}`;

  await canonicalWorkItems.publishCommand(ctx.projectName, {
    command: 'transitionStatus', actor: agentName, workItemId: ticketKey, status: 'needs-clarification',
  });
  await postComment(ticketKey, ctx, agentName, comment, reference);
  console.log(`[gateway] Set ${state} on ${ticketKey}`);
}

// A2A's canceled/rejected states have no dedicated entry in the minimum
// canonical vocabulary (status_vocabulary.py) — both fold into 'cancelled',
// the closest terminal status meaning "will not be retried, not a genuine
// execution failure".
function mapTerminalStateToStatus(state) {
  return state === 'failed' ? 'failed' : 'cancelled';
}

async function handleTerminalFailure(ticketKey, agentName, state, body, ctx) {
  const comment = `[${agentName}] Task ${state.toUpperCase()} — ${body || '(no detail provided)'}\nTicket: ${ticketKey}`;

  await canonicalWorkItems.publishCommand(ctx.projectName, {
    command: 'transitionStatus', actor: agentName, workItemId: ticketKey, status: mapTerminalStateToStatus(state),
  });
  await postComment(ticketKey, ctx, agentName, comment, null);
  console.log(`[gateway] Task ${state} on ${ticketKey}`);
}

// Read the work item's own persisted status. Only for reporting: the
// gateway's in-memory Task state is not the work item's status, and nothing
// in the completion path transitions it, so a log line that names a status
// has to go and look rather than assert one. A failed read must never turn a
// successful projection into a retry, so it degrades to "not known" and says
// so.
async function persistedStatus(ticketKey, ctx) {
  try {
    const item = await canonicalWorkItems.getWorkItem(ticketKey);
    return item && item.status ? item.status : null;
  } catch (err) {
    console.warn(`[gateway] Could not read the persisted status of ${ticketKey}: ${err.message}`);
    return null;
  }
}

// Handle a completed Task. A "pull-request" Artifact means the agent opened
// a PR — post a comment only. Opening a PR must not move the work item out of
// whatever status it is in, or change its recorded owner: Jenkins is the sole
// owner of the transition into review, firing only after tests pass, merge and
// beta deploy succeed, so there is no status transition to make here.
async function handleCompleted(ticketKey, agentName, body, artifacts, ctx) {
  const prArtifact = (artifacts || []).find(a => a.name === 'pull-request');

  if (prArtifact) {
    const filePart = prArtifact.parts.find(p => p.kind === 'file');
    const summaryPart = prArtifact.parts.find(p => p.kind === 'text');
    const prUrl = filePart?.file?.uri;

    const comment = `[${agentName}] PR opened and ready for review: ${prUrl}${summaryPart ? `\n\n${summaryPart.text}` : ''}\n\nTicket: ${ticketKey}`;
    await postComment(ticketKey, ctx, agentName, comment, null);

    const status = await persistedStatus(ticketKey, ctx);
    console.log(
      `[gateway] PR opened for ${ticketKey} — comment posted, no transition made here; ` +
      (status ? `${ticketKey} is "${status}"` : `${ticketKey}'s status could not be read`)
    );
    return;
  }

  if (body) {
    await postComment(ticketKey, ctx, agentName, `[${agentName}] ${body}\nTicket: ${ticketKey}`, null);
  }
  console.log(`[gateway] Task completed for ${ticketKey}`);
}

// Move a work item into the state that means "a human has to look at this":
// 'needs-clarification'. Every request the gateway refuses to act on ends
// here, so that a refusal is one visible state rather than a different one
// per refusal reason.
async function flagForAttention(ticketKey, actor, ctx) {
  await canonicalWorkItems.publishCommand(ctx.projectName, {
    command: 'transitionStatus', actor, workItemId: ticketKey, status: 'needs-clarification',
  });
}

// Report a rejected agentFieldValue assignment back to the requester — a
// rejected assignment must stay visible on the work item. This is the only
// wording, in every mode: it transitions to 'needs-clarification' like
// handleInterrupted above.
async function reportAssignmentFailure(ticketKey, requestedAgent, result, ctx) {
  const reason = result.code === assignment.ERROR_CODES.UNKNOWN_AGENT
    ? `"${requestedAgent}" is not a registered agent id in the catalog.`
    : result.code === assignment.ERROR_CODES.UNKNOWN_PROJECT
      ? `This project has no available-agent configuration.`
      : `"${requestedAgent}" is a registered agent but is not enabled for this project.`;

  const permitted = result.permittedAgents.length > 0
    ? result.permittedAgents.join(', ')
    : '(none configured for this project)';

  const comment =
    `[system] Cannot assign this work item — its requested agent ("${requestedAgent}") failed catalog validation.\n\n` +
    `Reason: ${reason}\n` +
    `Permitted agents for this project: ${permitted}\n\n` +
    `Recovery: retry with one of the permitted values above.\n` +
    `Ticket: ${ticketKey}`;

  await flagForAttention(ticketKey, 'system', ctx);
  await postComment(ticketKey, ctx, 'system', comment, null);
  console.error(`[gateway] ${ticketKey} assignment rejected — requested "${requestedAgent}" (${result.code})`);
}

// Report an operation request that cannot be acted on because required data
// was missing, mode-aware. A dropped request used to leave the parent work
// item with no subtask/no reassignment, no comment, and no status change —
// nothing a human or the requesting agent could see — so the parent always
// gets a comment naming the missing field(s), with `detailLines` supplying
// whatever operation-specific context helps recovery (e.g. create_subtask's
// requested summary, or the project's permitted agent ids).
//
// This is not recoverable by the requesting agent resending, so the comment
// is written for a human, not the agent: a running agent never reads
// comments, and even one that somehow did could not usefully act on this —
// a resend that references the rejected message is refused by the same
// lineage check that makes markMessageFailed's failure durable (see
// taskStore.js's checkMessageLineage). The one real recovery is a fresh
// dispatch of this ticket, which taskStore.js's controlled-reopen path also
// clears the stale failure for.
//
// The work item is moved to the same needs-a-human state a rejected
// assignment moves it to (flagForAttention above). A dropped request leaves
// the parent with no subtask and no reassignment, and the comment is a row
// in a thread nobody is watching; without the state change the parent still
// reads as ready to work on, which is the one thing it is not. Both
// refusals are the same event — a request the gateway would not act on —
// and they must not leave the work item in two different states.
async function reportMissingFields(ticketKey, operation, missingFields, detailLines, ctx) {
  const comment =
    `[system] Cannot ${operation} — the request is missing ${missingFields.join(' and ')}.\n\n` +
    detailLines.map(line => `${line}\n`).join('') +
    `\nThis cannot be fixed by resending: the agent that made this request cannot see this comment, ` +
    `and a resend referencing the same rejected request would be refused for the same reason. A human ` +
    `must correct the request or the project's configuration and trigger a fresh dispatch of this ticket.\n` +
    `Ticket: ${ticketKey}`;

  await flagForAttention(ticketKey, 'system', ctx);
  await postComment(ticketKey, ctx, 'system', comment, null);
  console.error(`[gateway] ${operation} rejected on ${ticketKey} — missing ${missingFields.join(', ')}`);
}

// Report a request naming an operation this gateway has no handler for.
// Such a request used to be logged and dropped, leaving the work item with
// no record of it at all: the agent believed it had asked for something, the
// gateway did nothing, and the only trace was a container log line. Reported
// on the work item and flagged the same way every other refused request is.
async function reportUnsupportedOperation(ticketKey, operation, ctx) {
  const comment =
    `[system] Cannot carry out the requested operation "${operation}" — this gateway has no handler for it.\n\n` +
    `Supported operations are comment, reassign and create_subtask.\n\n` +
    `This cannot be fixed by resending: the agent that made this request cannot see this comment, ` +
    `and a resend referencing the same rejected request would be refused for the same reason. A human ` +
    `must correct the request and trigger a fresh dispatch of this ticket.\n` +
    `Ticket: ${ticketKey}`;

  await flagForAttention(ticketKey, 'system', ctx);
  await postComment(ticketKey, ctx, 'system', comment, null);
  console.error(`[gateway] unsupported operation "${operation}" rejected on ${ticketKey}`);
}

// v4.1 agent-artifact-automation.md REQ-01 — a create_subtask request's
// optional specificationLink/artifactLinks, validated for shape only (never
// whether the artifact id actually resolves — that check lives in the
// core service, work-items.md REQ-04, and its rejection is reported
// through the materializeDecomposition dead-letter path, not this one).
// Returns phrases, not bare field names — carry-forward 5 (build brief
// §1b): reportMissingFields' "the request is missing ..." sentence must
// read as a reason a PRESENT field was refused, not as an absent one, so a
// malformed reference is never mistaken for a missing one.
function validateReferenceShape(data) {
  const problems = [];
  const { specificationLink, artifactLinks } = data;

  if (specificationLink !== undefined && specificationLink !== null) {
    const isWellFormed = specificationLink
      && typeof specificationLink === 'object'
      && !Array.isArray(specificationLink)
      && typeof specificationLink.artifactId === 'string' && specificationLink.artifactId.length > 0
      && typeof specificationLink.requirementId === 'string' && specificationLink.requirementId.length > 0;
    if (!isWellFormed) {
      problems.push(
        'a well-formed specificationLink (present, but not an object with non-empty "artifactId" and "requirementId" strings)'
      );
    }
  }

  if (artifactLinks !== undefined && artifactLinks !== null) {
    const isWellFormed = Array.isArray(artifactLinks) && artifactLinks.every(id => typeof id === 'string' && id.length > 0);
    if (!isWellFormed) {
      problems.push(
        'a well-formed artifactLinks list (present, but not an array of non-empty artifact id strings)'
      );
    }
  }

  return problems;
}

// Report a create_subtask request that cannot be acted on — reportMissingFields
// with the create_subtask-specific context (the requested summary, and, when
// agentFieldValue is what's missing, the project's permitted agent ids).
async function reportSubtaskRejection(parentTicketKey, summary, missingFields, ctx) {
  const project = registry.getProject(ctx.projectName);
  const permitted = project && project.agents.length > 0
    ? project.agents.join(', ')
    : '(none configured for this project)';

  const detailLines = [`Requested summary: ${summary ? `"${summary}"` : '(none supplied)'}`];
  if (missingFields.includes('agentFieldValue')) {
    detailLines.push(`Permitted agents for this project: ${permitted}`);
    if (summary) {
      detailLines.push(`The summary's "<Role>: ..." prefix named no single one of them, so no agent could be derived from it.`);
    }
  }

  await reportMissingFields(parentTicketKey, 'create_subtask', missingFields, detailLines, ctx);
}

// Change a work item's recorded implementation owner. Every path that creates
// or changes agent responsibility must go through the same catalog-backed
// validator.
async function handleReassign(record, agentFieldValue, agentName, ctx) {
  const ticketKey = record.id;
  if (!agentFieldValue) {
    const project = registry.getProject(ctx.projectName);
    const permitted = project && project.agents.length > 0
      ? project.agents.join(', ')
      : '(none configured for this project)';
    await reportMissingFields(ticketKey, 'reassign', ['agentFieldValue'], [`Permitted agents for this project: ${permitted}`], ctx);
    return false;
  }

  const result = assignment.validateAssignment(ctx.projectName, agentFieldValue);
  if (!result.ok) {
    await reportAssignmentFailure(ticketKey, agentFieldValue, result, ctx);
    return false;
  }

  await canonicalWorkItems.publishCommand(ctx.projectName, {
    command: 'assign', actor: agentName, workItemId: ticketKey, agentId: agentFieldValue,
  });
  console.log(`[gateway] Assigned ${ticketKey} to ${agentFieldValue}`);
  return true;
}

// Create a subtask under the requesting Task's own work item.
//
// This operation has no counterpart of its own: in every mode it reuses the
// exact same core materializeDecomposition command dependencies.js's
// routeMaterialization already sends for Refinement Agent decompositions (a
// single-subtask, no-dependency decomposition is a degenerate case of the
// same contract; materialize.py creates it and immediately transitions it to
// 'ready'). This is deliberate, not a shortcut: every dispatch-eligible
// transition must go through the same core-event -> dispatchConsumer.js path
// regardless of ingress, so this function must NOT call dispatchTask
// directly — dispatchConsumer.js's existing consumer on
// work_item.status_changed picks up the 'ready' transition and dispatches it
// the same way it dispatches every other work item. The subtask id is minted
// here (a bare UUID — WorkItem.id is a UUIDField, an AI-Gang-issued id) and
// guarded by the getOutcome/recordOutcome idempotency pattern, so a
// from-scratch retry reuses the same id instead of materializing a second
// work item.
async function handleCreateSubtask(record, data, ctx) {
  const { summary, description, specificationLink, artifactLinks } = data;
  const parentTicketKey = record.id;

  // An omitted agentFieldValue is recoverable when the summary's own
  // `<Role>: ...` prefix names exactly one agent this project has, other
  // than the requester itself — the id the request should have carried is
  // then implied by the request, not guessed, and cannot be the requester's
  // own id, which would route the subtask straight back to the agent that
  // asked for it. Everything else is reported on the parent work item below,
  // never dropped in silence.
  let agentFieldValue = data.agentFieldValue;
  if (!agentFieldValue && summary) {
    const derived = assignment.deriveAgentFromSummary(ctx.projectName, summary, {
      excludeAgentId: record.metadata.agentId,
    });
    if (derived) {
      agentFieldValue = derived.id;
      console.log(`[gateway] create_subtask under ${parentTicketKey} omitted agentFieldValue — derived "${agentFieldValue}" from the summary's role prefix`);
    }
  }

  const missing = [];
  if (!summary) missing.push('summary');
  if (!agentFieldValue) missing.push('agentFieldValue');
  // v4.1 REQ-01 — a malformed reference rejects the whole request the same
  // way a missing required field does: nothing is created, and nothing is
  // ever forwarded half-validated.
  missing.push(...validateReferenceShape(data));
  if (missing.length > 0) {
    console.warn(`[gateway] create_subtask missing required fields (${missing.join(', ')}) — reporting on ${parentTicketKey}. Received:`, JSON.stringify(data));
    await reportSubtaskRejection(parentTicketKey, summary, missing, ctx);
    return null;
  }

  const result = assignment.validateAssignment(ctx.projectName, agentFieldValue);
  if (!result.ok) {
    await reportAssignmentFailure(parentTicketKey, agentFieldValue, result, ctx);
    return null;
  }
  const agent = result.agent;

  const client = redis.getClient();
  const messageId = ctx.messageId;

  let subtaskId = await idempotency.getOutcome(client, 'subtask-create', messageId);
  if (subtaskId === undefined) {
    subtaskId = crypto.randomUUID();
    // v4.1 REQ-01 — the two optional references ride inside this
    // subtask entry untouched, forwarded unchanged to materialize.py,
    // which passes them through create_work_item exactly as `create`
    // does (work-items.md REQ-01, REQ-02). Included only when the
    // request actually carried them, so an unreferenced subtask's
    // canonical command is byte-for-byte what it was before this
    // feature.
    const subtaskEntry = { id: subtaskId, displayName: summary, description: description || '', agent: agentFieldValue };
    if (specificationLink) subtaskEntry.specificationLink = specificationLink;
    if (artifactLinks) subtaskEntry.artifactLinks = artifactLinks;
    await canonicalWorkItems.publishCommand(ctx.projectName, {
      command: 'materializeDecomposition',
      actor: agent.id,
      message: {
        parentWorkItemId: parentTicketKey,
        subtasks: [subtaskEntry],
      },
    });
    await idempotency.recordOutcome(client, 'subtask-create', messageId, subtaskId);
    console.log(`[gateway] Materialized subtask ${subtaskId} under ${parentTicketKey} — dispatch follows from its own ready event`);
  } else {
    console.log(`[gateway] Reusing already-materialized subtask ${subtaskId} for messageId ${messageId}`);
  }
  return { subtaskId };
}

module.exports = {
  startGatewaySubscriber,
  stopGatewaySubscriber,
  _handleA2ASubmission: handleA2ASubmission,
  _handlePipelineRetry: handlePipelineRetry,
  _handleTaskStatus: handleTaskStatus,
  _gatewayConsumerOptions: gatewayConsumerOptions,
};
