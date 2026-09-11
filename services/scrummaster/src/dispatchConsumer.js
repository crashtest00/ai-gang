'use strict';

// Dispatch is triggered by canonical domain events, not by ingestion path.
// A work item's transition into a dispatch-eligible state MUST trigger
// agent dispatch via the SAME mechanism regardless of which mode or
// ingress produced that transition — a Jira-originated event validated by
// work-item-service, a Django Admin Panel write (local mode), or any
// future ingress. This is that single mechanism: one durable Streams
// consumer on work-item-service's outbound canonical-event stream
// (aigang:workitems:{project}:events — the same stream
// jiraCatchupConsumer.js already consumes, for a different purpose; this
// is a second, independent consumer group, Streams' normal fan-out).
//
// This is ALSO where ScrumMaster's now-deleted `routeWebhookEvent`
// Jira-mode side effects (posting a comment, setting the Agent/Blocked
// custom fields, mirroring a dispatch to Jira's "In Progress" status,
// triggering the Release Jenkins jobs) are re-homed, driven by the small,
// already-Django-decided `work_item.jira_side_effect`/
// `work_item.jira_release_event` canonical events
// workitems/webhook_consumer.py now publishes — this module never
// interprets a raw Jira field name, status string, or changelog shape
// itself; it only executes what Django already decided.
//
// Known simplification (flagged in the final report): dispatch-eligibility
// below checks `status === 'ready'` literally rather than resolving a
// project's custom status configuration to its baseline — ScrumMaster has
// no local copy of ProjectStatusConfig and no HTTP endpoint currently
// exposes it. A project
// using ONLY the ten fixed minimum-vocabulary statuses (the common case,
// and the only case any test in this repo exercises) is unaffected; a
// project that renames 'ready' via a custom status would not dispatch
// through this literal check.

const redis = require('./redis');
const registry = require('./registry');
const streams = require('./streams');
const jira = require('./jira');
const canonicalWorkItems = require('./canonicalWorkItems');
const taskStore = require('./a2a/taskStore');
const handlers = require('./handlers');
const { buildTaskPrompt, buildUnblockPrompt, buildRetryPrompt } = require('./prompt');

const consumers = [];

async function startDispatchConsumers() {
  const client = redis.getClient();
  const consumerName = process.env.SCRUMMASTER_CONSUMER_ID || require('os').hostname();

  for (const projectName of registry.getProjectNames()) {
    const stream = registry.workItemEventStreamName(projectName);
    const consumer = streams.createConsumer(client, {
      stream,
      group: registry.DISPATCH_GROUP,
      consumerName,
      handler: envelope => handleWorkItemEventEnvelope(envelope, projectName),
    });
    await consumer.start();
    consumers.push(consumer);
    console.log(`[dispatch] Consuming ${stream} as ${registry.DISPATCH_GROUP}/${consumerName}`);
  }
}

async function stopDispatchConsumers() {
  await Promise.all(consumers.splice(0).map(c => c.stop()));
}

// Map a canonical work item (work-item-service's full-record HTTP shape,
// serializers.serialize_work_item_full) into the same "issue"-shaped
// object jira.getIssue() returns, so the existing dispatchTask/
// buildTaskPrompt/buildUnblockPrompt/buildRetryPrompt pipeline needs no
// local-mode-specific branch of its own — dispatch deliberately has no
// local-mode-specific code path.
function issueLikeFromCanonical(full) {
  const detail = full.storyDetail || {};
  return {
    key: full.id,
    project: full.project,
    projectName: full.project,
    parent: full.parent_id || null,
    summary: full.display_name,
    description: full.description || '',
    status: full.status,
    issuetype: full.type === 'story' ? 'Story' : 'Task',
    agent: full.assignee_agent_id,
    behavior: detail.behavior || null,
    acceptanceCriteria: detail.acceptance_criteria || null,
    constraints: detail.constraints || null,
    edgeCases: detail.edge_cases || null,
    outOfScope: detail.out_of_scope || null,
    comments: (full.comments || []).map(c => ({ author: c.author, body: c.body, timestamp: c.created_at })),
  };
}

// Jira-mode: always fetch the live issue for full fidelity, exactly as
// every dispatch before this change did — the canonical mirror is
// authoritative for status/assignment but does not project every
// Jira-only field (summary/description text formatting, live comment
// authorship) into canonical events. Local mode: no Jira issue exists at
// all, so the canonical record IS the full record.
async function issueLikeFor(full) {
  if (full.external_key) {
    return jira.getIssue(full.external_key);
  }
  return issueLikeFromCanonical(full);
}

async function maybeDispatch(workItemId, envelope) {
  if (!workItemId) return;
  const full = await canonicalWorkItems.getWorkItem(workItemId, { full: true });
  if (!full) return;
  if (!full.assignee_agent_id) return;
  if (full.status !== 'ready') return;

  const agent = registry.getAgent(full.assignee_agent_id);
  if (!agent) {
    console.error(`[dispatch] Unknown agent "${full.assignee_agent_id}" for work item ${workItemId} — skipping dispatch`);
    return;
  }

  const issueLike = await issueLikeFor(full);
  const isRefinement = full.assignee_agent_id === 'refinement-agent';
  const existingTask = taskStore.getTaskById(issueLike.key);

  let promptFactory;
  if (isRefinement) {
    // handleStoryCreated/handleBlockedCleared's refinement-agent branch:
    // always the full task prompt (with the project's allowed-agent set),
    // never the bare unblock prompt, whether this is the Story's first
    // dispatch or a redispatch after its required fields were filled in.
    const allowedAgents = registry.getEffectiveAgents(full.project);
    promptFactory = (task, message) => buildTaskPrompt(issueLike, agent, { allowedAgents, task, message });
  } else if (!existingTask) {
    // handleShovelReady's branch: a fresh dispatch (first time this item
    // has ever been assigned a Task).
    promptFactory = (task, message) => buildTaskPrompt(issueLike, agent, { task, message });
  } else {
    // A dev-agent item reaching 'ready' again after already having a Task
    // (e.g. a dependency-blocked subtask unblocked, then re-readied) —
    // resume via the same BLOCKED-marker search handleBlockedCleared's
    // non-refinement branch used.
    const blockedMarker = await handlers.findBlockedMarker(issueLike.key);
    promptFactory = (task, message) => buildUnblockPrompt(issueLike, agent, task, message, blockedMarker);
  }

  await handlers.dispatchTask(issueLike, agent, { dispatchId: envelope.messageId, promptFactory });

  // handleShovelReady's own post-dispatch side effect, preserved: mirror a
  // dev-agent dispatch to Jira's "In Progress" status. Not applicable to a
  // refinement-agent dispatch (handleStoryCreated never did this) or to a
  // local-mode item (no Jira issue to transition).
  if (!isRefinement && full.external_key) {
    await jira.transitionIssue(full.external_key, 'In Progress');
  }
}

// handleReworkRequested's trigger: a human moved a ticket from "In Review"
// back to "In Progress". Detected from the
// item's own append-only history rather than from any Jira-specific
// string — the canonical status_changed event and the history row behind
// it are all this needs.
async function maybeRedispatchForRework(workItemId, envelope) {
  if (!workItemId) return;
  const full = await canonicalWorkItems.getWorkItem(workItemId, { full: true });
  if (!full || full.status !== 'in-progress' || !full.assignee_agent_id) return;

  const statusHistory = (full.history || []).filter(h => h.field === 'status');
  const last = statusHistory[statusHistory.length - 1];
  if (!last || last.old_value !== 'in-review' || last.new_value !== 'in-progress') return;

  const agent = registry.getAgent(full.assignee_agent_id);
  if (!agent) return;

  const issueLike = await issueLikeFor(full);
  await handlers.dispatchTask(issueLike, agent, {
    dispatchId: envelope.messageId,
    promptFactory: (task, message) => buildRetryPrompt(issueLike, agent, { kind: 'human_rework' }, task, message),
  });
}

// handleStoryCreated / handleBlockedCleared's refinement-agent branch's
// Jira-visible side effects — the DECISION (what happened, what the
// comment should say) was already made in Django
// (workitems/webhook_consumer.py's `_handle_story_created`/
// `_handle_blocked_field_change`); this only executes it.
async function handleStoryIntake(detail, jiraIssueKey) {
  const { ok, missing, reblock } = detail || {};

  if (reblock) {
    const list = (missing || []).map(l => `  - ${l}`).join('\n');
    await jira.postComment(
      jiraIssueKey,
      `Story is still missing required fields and cannot be refined until they are filled in:\n\n${list}\n\nPlease complete these fields and clear the Blocked field again to retry.`
    );
    await jira.setBlockedField(jiraIssueKey, true);
    return;
  }

  await jira.setAgentField(jiraIssueKey, 'refinement-agent');
  await jira.postComment(jiraIssueKey, 'Ticket received. Assigned to Refinement Agent for decomposition.');

  if (!ok) {
    const list = (missing || []).map(l => `  - ${l}`).join('\n');
    await jira.postComment(
      jiraIssueKey,
      `Story is missing required fields and cannot be refined until they are filled in:\n\n${list}\n\nPlease complete these fields and move the ticket back to Backlog to retry.`
    );
    await jira.setBlockedField(jiraIssueKey, true);
  }
}

// handleBlockedCleared's non-refinement branch: Blocked was cleared on a
// dev-agent ticket already mid-implementation — redispatch as a
// continuation using the BLOCKED-marker search, without touching status
// (Django never attempted a status transition for this case — see
// webhook_consumer.py's `_handle_blocked_field_change`).
async function handleBlockedClearedSideEffect(workItemId, envelope) {
  if (!workItemId) return;
  const full = await canonicalWorkItems.getWorkItem(workItemId, { full: true });
  if (!full || !full.assignee_agent_id) return;

  const agent = registry.getAgent(full.assignee_agent_id);
  if (!agent) return;

  const issueLike = await issueLikeFor(full);
  const blockedMarker = await handlers.findBlockedMarker(issueLike.key);
  await handlers.dispatchTask(issueLike, agent, {
    dispatchId: envelope.messageId,
    promptFactory: (task, message) => buildUnblockPrompt(issueLike, agent, task, message, blockedMarker),
  });
}

// The outbound event stream is a fan-out: every event type
// work-item-service emits arrives here, not just the ones this consumer
// acts on, so a non-matching event type is expected and silently ignored.
async function handleWorkItemEventEnvelope(envelope, projectName) {
  const payload = envelope.payload || {};
  const eventType = payload.eventType;
  const data = payload.data || {};

  if (eventType === 'work_item.created' || eventType === 'work_item.status_changed') {
    await maybeDispatch(data.id, envelope);
    if (eventType === 'work_item.status_changed') {
      await maybeRedispatchForRework(data.id, envelope);
    }
    return;
  }

  if (eventType === 'work_item.jira_side_effect') {
    const { kind, jiraIssueKey, detail } = data;
    if (kind === 'story_intake') {
      await handleStoryIntake(detail, jiraIssueKey);
    } else if (kind === 'blocked_cleared') {
      await handleBlockedClearedSideEffect(payload.workItemId, envelope);
    }
    return;
  }

  if (eventType === 'work_item.jira_release_event') {
    // The same event type now also carries a local-mode-originated
    // candidate-cut/abandon/done, keyed by
    // `workItemId`/`project` instead of `jiraIssueKey` (work-item-service's
    // store.py `_publish_release_event`, no jiraIssueKey in the payload).
    // handlers.js branches on which one is present; this routing is
    // otherwise unchanged from Jira mode.
    const { kind, jiraIssueKey, workItemId, project } = data;
    const ref = { jiraIssueKey, workItemId, project };
    if (kind === 'requested') {
      await handlers.handleReleaseRequested(ref);
    } else if (kind === 'abandoned') {
      await handlers.handleReleaseAbandoned(ref);
    } else if (kind === 'done') {
      await handlers.handleDone(ref);
    }
    return;
  }

  // work_item.jira_event_received, work_item.assigned, work_item.comment_added,
  // etc. — recorded by work-item-service already; nothing for this
  // consumer to do.
}

module.exports = {
  startDispatchConsumers,
  stopDispatchConsumers,
  handleWorkItemEventEnvelope,
  maybeDispatch,
  maybeRedispatchForRework,
  handleStoryIntake,
  handleBlockedClearedSideEffect,
  issueLikeFromCanonical,
};
