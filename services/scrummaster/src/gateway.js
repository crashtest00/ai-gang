'use strict';

const crypto = require('crypto');
const jira = require('./jira');
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
const { buildTaskPrompt } = require('./prompt');
const { redispatchImplementationOwner, dispatchTask, reportAssignmentFailure: reportJiraAssignmentFailure } = require('./handlers');

// One durable consumer per project's gateway stream (aigang:gateway:{project},
// group "scrummaster") — replaces the single `PSUBSCRIBE jira-gateway:*`
// subscriber. Project identity comes from which stream a consumer is bound
// to, never from message content.
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

// Materialize a Refinement Agent decomposition, mode-aware:
// dependencies.js's routeMaterialization sends a Jira-mode project through
// the exact existing Jira-subtask-and-dependency-link path unchanged, and a
// local-mode project to the Internal Work-Item Service instead. Validation and
// no-progress failures already post an explanatory Jira comment inside
// dependencies.js's Jira-mode path — retrying an unmodified invalid/stalled
// decomposition can't succeed, so those are re-thrown as permanent to
// dead-letter immediately rather than burning retry attempts. Any other
// failure (e.g. a transient Jira API error) is left to the normal
// retry/dead-letter path.
async function handleMaterializeDecomposition(msg, projectName) {
  try {
    const result = await dependencies.routeMaterialization(
      { parentId: msg.parentJiraIssueKey, subtasks: msg.subtasks },
      projectName
    );
    console.log(`[gateway] Materialized decomposition under ${msg.parentJiraIssueKey}`);
    return result;
  } catch (err) {
    if (
      err instanceof dependencies.MaterializationValidationError ||
      err instanceof dependencies.MaterializationNoProgressError
    ) {
      const permanent = new Error(err.message);
      permanent.permanent = true;
      throw permanent;
    }
    throw err;
  }
}

// Handle a terminal Task outcome reported by a project container's
// subscriber. A 'completed' status is informational
// — the agent's own gateway submission already carries the human-readable
// summary. A 'failed' status (retry exhaustion, timeout, or an invalid
// message) has no such comment, so ScrumMaster must post one itself and
// surface the ticket as needing attention rather than leaving it silently
// stuck "In Progress". Also mirrors the outcome into the A2A Task record
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

    const mode = await canonicalWorkItems.getMode(projectName);
    if (mode.mode === 'jira') {
      await jira.postComment(key, comment);
      await jira.setBlockedField(key, true);
    } else {
      await canonicalWorkItems.publishCommand(projectName, {
        command: 'transitionStatus', actor: label, workItemId: key, status: 'failed',
      });
      await postComment(key, { projectName, mode, messageId: envelope.messageId }, label, comment, null);
    }
    console.error(`[gateway] Task ${taskId} failed — ${key} blocked, reason: ${reason}`);
    return { taskId, status };
  }

  console.warn(`[gateway] Unknown task_status "${status}" for task ${taskId}`);
  return null;
}

// Handle a pipeline-failure retry request from Jenkins. The message
// identifies the ticket and the failed build but never
// asserts an agent owner — ScrumMaster looks up the ticket's own preserved
// Agent field and redispatches that agent. Deduplicated per (ticket, build) —
// a domain-level dedupe independent of this message's own messageId, since
// Jenkins could in principle fire two distinct messages for the same failure.
async function handlePipelineRetry(msg, _projectName) {
  const { ticket_key, build_url, build_number } = msg;

  if (!ticket_key) {
    console.warn('[gateway] pipeline_retry message missing ticket_key — dropping');
    return null;
  }

  const dedupeKey = `retry-dispatch:${ticket_key}:${build_url || build_number || 'unknown'}`;
  const acquired = await redis.acquireOnce(dedupeKey, 3600);
  if (!acquired) {
    console.log(`[gateway] Duplicate pipeline_retry for ${ticket_key} (build ${build_number || build_url}) — skipping`);
    return { ticket_key, skipped: true };
  }

  await redispatchImplementationOwner(ticket_key, {
    kind: 'pipeline_failure',
    build_url: build_url || null,
    build_number: build_number || null,
  });
  return { ticket_key, skipped: false };
}

// Apply an incoming agent submission to the stored A2A Task, then translate
// the resulting state/message/artifacts into the canonical-state side
// effects the legacy per-type gateway operations used to perform directly
// against Jira (comment, set_blocked, set_agent_field, create_subtask,
// open_pr). Mode-aware: a Jira-mode project
// keeps the exact existing Jira-write behavior; a local-mode project routes
// the same decisions through the Internal Work-Item Service's Streams
// command channel instead — the same split dependencies.js's
// routeMaterialization already established for decomposition. `jiraIssueKey`
// below is the Task's stable external-facing key regardless of mode: in
// Jira mode it's the real Jira issue key, in local mode it's the canonical
// work item id (handlers.js's dispatchTask stores `issue.key` under this
// name in both cases — dispatch deliberately has no local-mode-specific
// code path).
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
    console.warn(`[gateway] Unknown task ${taskId} on jira-gateway:${projectName} — dropping`);
    return null;
  }

  const taskProjectName = record.metadata.jiraProjectName?.toLowerCase();
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
      return { ticket_key: record.jiraIssueKey, alreadyFinished: stateBeforeApplying };
    }
    throw err;
  }

  const jiraIssueKey = record.jiraIssueKey;
  if (!jiraIssueKey) {
    console.warn(`[gateway] Task ${taskId} has no jiraIssueKey in metadata — nothing to project`);
    taskStore.markMessageFailed(taskId, message.messageId);
    return null;
  }

  let outcome;
  try {
    const mode = await canonicalWorkItems.getMode(projectName);
    const ctx = { projectName, mode, messageId: envelope.messageId };

    const agentName = deriveAgentDisplayName(record);
    const textPart = message.parts.find(p => p.kind === 'text');
    const dataPart = message.parts.find(p => p.kind === 'data');
    const body = textPart?.text;
    const operation = dataPart?.data?.operation;
    const reference = dataPart?.data?.reference;

    if (schema.INTERRUPTED_STATES.includes(msg.state)) {
      await handleInterrupted(jiraIssueKey, agentName, msg.state, body, reference, ctx);
      outcome = { ticket_key: jiraIssueKey };
    } else if (msg.state === 'completed') {
      await handleCompleted(jiraIssueKey, agentName, body, msg.artifacts, ctx);
      outcome = { ticket_key: jiraIssueKey };
    } else if (msg.state === 'failed' || msg.state === 'canceled' || msg.state === 'rejected') {
      await handleTerminalFailure(jiraIssueKey, agentName, msg.state, body, ctx);
      outcome = { ticket_key: jiraIssueKey };
    } else {
      // Non-terminal, non-interrupted (submitted/working): branch on the
      // requested operation, if any.
      switch (operation) {
        case 'comment':
          await postFormattedComment(jiraIssueKey, agentName, body, reference, ctx);
          outcome = { ticket_key: jiraIssueKey };
          break;
        case 'reassign':
          outcome = await handleReassign(record, dataPart.data.agentFieldValue, agentName, ctx)
            ? { ticket_key: jiraIssueKey }
            : null;
          break;
        case 'create_subtask':
          outcome = await handleCreateSubtask(record, dataPart.data, ctx);
          break;
        case undefined:
          // A working-state message with no data part is a plain progress note.
          if (body) await postFormattedComment(jiraIssueKey, agentName, body, reference, ctx);
          outcome = { ticket_key: jiraIssueKey };
          break;
        default:
          await reportUnsupportedOperation(jiraIssueKey, operation, ctx);
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
  const ticketKey = record.jiraIssueKey;
  if (!ticketKey) {
    console.warn(
      `[gateway] Task ${record.id} is already ${finishedState} and a later message was acknowledged without ` +
      `being applied, but the Task has no work item to report that on`
    );
    return;
  }

  const mode = await canonicalWorkItems.getMode(projectName);
  const ctx = { projectName, mode, messageId: envelope.messageId };
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

// Post one comment, mode-aware. `formattedBody` is the exact text both
// modes post — both modes must show the same body/reference content, so
// this does not reformat per destination, only redirect it: Jira mode
// keeps the existing jira.postComment call, local mode routes the same
// text through the Internal Work-Item Service's appendComment Streams
// command (no Jira call may be required to succeed). `ctx.messageId` is
// threaded through as the comment's
// sourceMessageId so a redelivered gateway entry can't double-post it
// (append_comment's own redelivery guard).
async function postComment(ticketKey, ctx, agentName, formattedBody, reference) {
  if (ctx.mode.mode === 'jira') {
    await jira.postComment(ticketKey, formattedBody);
    return;
  }
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

// Jira mode represents "blocked/needs input" as a boolean field layered on
// top of whatever status the ticket is already in. The canonical vocabulary
// has no equivalent boolean — 'needs-clarification' is the
// minimum-vocabulary status that means the same thing, so
// local mode transitions into it instead of flipping a flag.
async function handleInterrupted(ticketKey, agentName, state, body, reference, ctx) {
  const label = state === 'auth-required' ? 'AUTHORIZATION REQUIRED' : 'BLOCKED';
  let comment = `[${agentName}] ${label} — ${body || '(no reason provided)'}`;
  const ref = formatReference(reference);
  if (ref) comment += `\n\nReference: ${ref}`;
  comment += `\nTicket: ${ticketKey}`;

  if (ctx.mode.mode === 'jira') {
    await jira.setBlockedField(ticketKey, true);
  } else {
    await canonicalWorkItems.publishCommand(ctx.projectName, {
      command: 'transitionStatus', actor: agentName, workItemId: ticketKey, status: 'needs-clarification',
    });
  }
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

  if (ctx.mode.mode === 'jira') {
    await jira.setBlockedField(ticketKey, true);
  } else {
    await canonicalWorkItems.publishCommand(ctx.projectName, {
      command: 'transitionStatus', actor: agentName, workItemId: ticketKey, status: mapTerminalStateToStatus(state),
    });
  }
  await postComment(ticketKey, ctx, agentName, comment, null);
  console.log(`[gateway] Task ${state} on ${ticketKey}`);
}

// Read the work item's own persisted status, mode-aware. Only for reporting:
// the gateway's in-memory Task state is not the work item's status, and
// nothing in the completion path transitions it, so a log line that names a
// status has to go and look rather than assert one. A failed read must
// never turn a successful projection into a retry, so it degrades to "not
// known" and says so.
async function persistedStatus(ticketKey, ctx) {
  try {
    if (ctx.mode.mode === 'jira') {
      const issue = await jira.getIssue(ticketKey);
      return issue && issue.status ? issue.status : null;
    }
    const item = await canonicalWorkItems.getWorkItem(ticketKey);
    return item && item.status ? item.status : null;
  } catch (err) {
    console.warn(`[gateway] Could not read the persisted status of ${ticketKey}: ${err.message}`);
    return null;
  }
}

// Handle a completed Task. A "pull-request" Artifact means the agent opened
// a PR — post a comment only. Opening a PR must not move the ticket out of
// whatever status it is in, or change its recorded implementation owner:
// Jenkins is the sole owner of the "In Review" transition, firing only after
// tests pass, merge, and beta deploy succeed — a Jira-mode concern only
// (Release work items are carved out of this), so local mode has no status
// transition to make here in either branch.
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

// Move a work item into the state that means "a human has to look at this",
// mode-aware. Jira mode represents it as the Blocked flag layered on top of
// whatever status the ticket holds; the canonical vocabulary has no such
// flag, so 'needs-clarification' carries the same meaning there. Every
// request the gateway refuses to act on ends here, so that a refusal is one
// visible state rather than a different one per refusal reason.
async function flagForAttention(ticketKey, actor, ctx) {
  if (ctx.mode.mode === 'jira') {
    await jira.setBlockedField(ticketKey, true);
    return;
  }
  await canonicalWorkItems.publishCommand(ctx.projectName, {
    command: 'transitionStatus', actor, workItemId: ticketKey, status: 'needs-clarification',
  });
}

// Report a rejected agent-field/agentFieldValue assignment back to the
// requester, mode-aware — a visible assignment failure must stay visible
// for canonical work items too, not just Jira ones. Jira mode keeps the existing
// handlers.js behavior untouched. Local mode has no Blocked field to flip,
// so it transitions to 'needs-clarification' like handleInterrupted above.
async function reportAssignmentFailure(ticketKey, requestedAgent, result, ctx) {
  if (ctx.mode.mode === 'jira') {
    await reportJiraAssignmentFailure(ticketKey, requestedAgent, result);
    return;
  }

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

// Change a ticket's recorded implementation owner. Every path that creates
// or changes agent responsibility must go through the same catalog-backed
// validator.
async function handleReassign(record, agentFieldValue, agentName, ctx) {
  const ticketKey = record.jiraIssueKey;
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

  if (ctx.mode.mode === 'jira') {
    await jira.setAgentField(ticketKey, agentFieldValue);
  } else {
    await canonicalWorkItems.publishCommand(ctx.projectName, {
      command: 'assign', actor: agentName, workItemId: ticketKey, agentId: agentFieldValue,
    });
  }
  console.log(`[gateway] Set Agent field on ${ticketKey} to ${agentFieldValue}`);
  return true;
}

// Create a subtask under the requesting Task's own work item, then dispatch
// a brand-new A2A Task to the assigned agent for it.
//
// Jira mode: unchanged — creates the Jira subtask, dispatches directly, and
// transitions it to "In Progress". The subtask creation carries its own
// idempotency guard keyed off the gateway envelope's messageId, independent
// of the outer once()-wrapped outcome for this whole operation — so if a
// later step (transitionIssue) fails and the operation is retried from
// scratch, the Jira subtask is not duplicated (dispatchTask's own dedupeKey
// separately protects the agent dispatch).
//
// Local mode: this operation has no bespoke local-mode counterpart — it
// reuses the exact same Internal Work-Item Service materializeDecomposition
// command dependencies.js's routeMaterialization already sends for
// Refinement Agent decompositions (a single-subtask, no-dependency
// decomposition is a degenerate case of the same contract; materialize.py
// creates it and immediately transitions it to 'ready'). This is
// deliberate, not a shortcut: every
// dispatch-eligible transition must go through the same
// work-item-service-event -> dispatchConsumer.js path regardless of
// ingress, so this function must NOT call dispatchTask directly for a
// local-mode subtask — dispatchConsumer.js's existing consumer on
// work_item.status_changed picks up the 'ready' transition and dispatches
// it the same way it dispatches every other local-mode work item. The
// subtask id is minted here (a bare UUID — WorkItem.id is a UUIDField,
// an AI-Gang-issued id) and guarded by the same
// getOutcome/recordOutcome idempotency pattern as the Jira-mode subtask key,
// so a from-scratch retry reuses the same id instead of materializing a
// second work item.
async function handleCreateSubtask(record, data, ctx) {
  const { summary, description } = data;
  const parentTicketKey = record.jiraIssueKey;

  if (!parentTicketKey) {
    // Nothing to create the subtask under, and nowhere to report it either.
    console.warn('[gateway] create_subtask missing required fields (parentTicketKey) — dropping. Received:', JSON.stringify(data));
    return null;
  }

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

  if (ctx.mode.mode !== 'jira') {
    let subtaskId = await idempotency.getOutcome(client, 'subtask-create', messageId);
    if (subtaskId === undefined) {
      subtaskId = crypto.randomUUID();
      await canonicalWorkItems.publishCommand(ctx.projectName, {
        command: 'materializeDecomposition',
        actor: agent.id,
        message: {
          parentWorkItemId: parentTicketKey,
          subtasks: [{ id: subtaskId, displayName: summary, description: description || '', agent: agentFieldValue }],
        },
      });
      await idempotency.recordOutcome(client, 'subtask-create', messageId, subtaskId);
      console.log(`[gateway] Materialized subtask ${subtaskId} under ${parentTicketKey} — dispatch follows from its own ready event`);
    } else {
      console.log(`[gateway] Reusing already-materialized subtask ${subtaskId} for messageId ${messageId}`);
    }
    return { subtaskId };
  }

  let subtaskKey = await idempotency.getOutcome(client, 'subtask-create', messageId);
  if (subtaskKey === undefined) {
    const parent = await jira.getIssue(parentTicketKey);
    subtaskKey = await jira.createSubtask(parentTicketKey, parent.project, summary, description || '', agentFieldValue);
    await idempotency.recordOutcome(client, 'subtask-create', messageId, subtaskKey);
    console.log(`[gateway] Created subtask ${subtaskKey} under ${parentTicketKey}`);
  } else {
    console.log(`[gateway] Reusing already-created subtask ${subtaskKey} for messageId ${messageId}`);
  }

  const subtask = await jira.getIssue(subtaskKey);
  await dispatchTask(subtask, agent, {
    dispatchId: messageId,
    promptFactory: (task, message) => buildTaskPrompt(subtask, agent, { task, message }),
  });

  await jira.transitionIssue(subtaskKey, 'In Progress');

  console.log(`[gateway] Subtask ${subtaskKey} dispatched to ${agentFieldValue}`);
  return { subtaskKey, dispatched: true };
}

module.exports = {
  startGatewaySubscriber,
  stopGatewaySubscriber,
  _handleA2ASubmission: handleA2ASubmission,
  _handlePipelineRetry: handlePipelineRetry,
  _handleTaskStatus: handleTaskStatus,
  _gatewayConsumerOptions: gatewayConsumerOptions,
};
