'use strict';

// Dispatch is triggered by canonical domain events, not by ingestion path.
// A work item's transition into a dispatch-eligible state MUST trigger
// agent dispatch via the SAME mechanism regardless of which mode or
// ingress produced that transition — an externally-originated event
// validated by core, a Django Admin Panel write, or any
// future ingress. This is that single mechanism: one durable Streams
// consumer on core's outbound canonical-event stream
// (aigang:workitems:{project}:events), as one consumer group among the
// groups any interested subscriber creates there.
//
// Every side effect this module performs is a canonical one, decided in
// Django and executed here against the canonical store or Jenkins. From v5.1
// no ScrumMaster module calls an external tracker at all (REQ-04, REQ-05), so
// the side-effect events this consumer acts on are the canonical ones alone:
// a cleared-block redispatch, and a release event it can resolve to a
// canonical work item.
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

// Map a canonical work item (core's full-record HTTP shape,
// serializers.serialize_work_item_full) into the "issue"-shaped object the
// dispatchTask/buildTaskPrompt/buildUnblockPrompt/buildRetryPrompt pipeline
// takes. From v5.1 this is the only such object ScrumMaster ever builds, in
// every mode, and every name on it is a canonical one.
//
// `mode` is the project's own configuration (canonicalWorkItems.getMode's
// result) or null. It decides two display-only fields and nothing else: the
// work item's `externalKey`, carried for the prompt's `External key:` line
// and set only for a project in Jira mode, and the project's own tracker key.
// Neither is read to resolve anything, here or anywhere else in ScrumMaster
// (REQ-07).
function issueLikeFromCanonical(full, mode = null) {
  const detail = full.storyDetail || {};
  const jiraMode = !!mode && mode.mode === 'jira';
  return {
    key: full.id,
    externalKey: jiraMode ? (full.external_key || null) : null,
    jiraProjectKey: (mode && mode.jiraProjectKey) || null,
    projectName: full.project,
    parent: full.parent_id || null,
    summary: full.display_name,
    description: full.description || '',
    status: full.status,
    type: full.type,
    agent: full.assignee_agent_id,
    behavior: detail.behavior || null,
    acceptanceCriteria: detail.acceptance_criteria || null,
    constraints: detail.constraints || null,
    edgeCases: detail.edge_cases || null,
    outOfScope: detail.out_of_scope || null,
    comments: (full.comments || []).map(c => ({ author: c.author, body: c.body, timestamp: c.created_at })),
    // v4.1 agent-artifact-automation.md REQ-04 (build brief §1b
    // carry-forward 1) — the `?full=true` read already returns both
    // (serializers.serialize_work_item_full), so no read-API change was
    // needed; this mapping was the only place dropping them.
    specificationLink: full.specification_link
      ? { artifactId: full.specification_link.artifact_id, requirementId: full.specification_link.requirement_id }
      : null,
    artifactLinks: (full.artifact_links || []).map(l => l.artifact_id),
  };
}

// The canonical record IS the full record, in every mode: from v5.1 no
// ScrumMaster module reads a tracker (REQ-05), so there is no live issue to
// fetch and no branch on a work item's external key. The project's mode is
// read for one reason only — whether the issue-like object carries the work
// item's external key and the project's tracker key for display
// (issueLikeFromCanonical above).
async function issueLikeFor(full) {
  const mode = await canonicalWorkItems.getMode(full.project);
  return issueLikeFromCanonical(full, mode);
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
    // The Refinement Agent always gets the full task prompt (with the
    // project's allowed-agent set), never the bare unblock prompt, whether
    // this is the story's first dispatch or a redispatch after its required
    // fields were filled in.
    const allowedAgents = registry.getEffectiveAgents(full.project);
    promptFactory = (task, message) => buildTaskPrompt(issueLike, agent, { allowedAgents, task, message });
  } else if (!existingTask) {
    // A fresh dispatch: the first time this item has ever been assigned a
    // Task.
    promptFactory = (task, message) => buildTaskPrompt(issueLike, agent, { task, message });
  } else {
    // A dev-agent item reaching 'ready' again after already having a Task
    // (e.g. a dependency-blocked subtask unblocked, then re-readied) —
    // resume via the BLOCKED-marker search.
    const blockedMarker = await handlers.findBlockedMarker(issueLike.key);
    promptFactory = (task, message) => buildUnblockPrompt(issueLike, agent, task, message, blockedMarker);
  }

  await handlers.dispatchTask(issueLike, agent, { dispatchId: envelope.messageId, promptFactory });

  // Post-dispatch, the work item must stop reading as merely waiting to be
  // picked up: an agent now has it, and the only place an operator can see
  // that is the item's own status. So the item itself is moved to
  // 'in-progress' — for every dispatch made here, in every mode, refinement
  // included: nothing else ever moves it off 'ready', and an item being
  // decomposed is being worked just as much as one being implemented. Until
  // this, an item could be dispatched, worked, and have a pull request
  // opened on it while still displaying as ready to pick up.
  //
  // One command, no mode branch (REQ-04): for a project in Jira mode core's
  // own write gate refuses this command and dead-letters it as
  // WRITE_GATE_REJECTED, which is what "Jira mode is off" means in v5.1 —
  // ScrumMaster publishes the same canonical command either way and decides
  // nothing about the tracker.
  //
  // That transition echoes back as a status-changed event this same
  // consumer reads. It costs nothing and repeats nothing: maybeDispatch's
  // own `status !== 'ready'` guard above rejects the echo, and
  // maybeRedispatchForRework acts only on an 'in-review' -> 'in-progress'
  // history entry, which this is not.
  await canonicalWorkItems.publishCommand(full.project, {
    command: 'transitionStatus', actor: full.assignee_agent_id, workItemId: full.id, status: 'in-progress',
  });
}

// A human sent a work item back for rework after review. Detected from the
// item's own append-only history — the canonical status_changed event and the
// history row behind it are all this needs.
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

// A cleared block on a dev-agent work item already mid-implementation —
// redispatch as a continuation using the BLOCKED-marker search, without
// touching status (Django never attempted a status transition for this
// case — see webhook_consumer.py's `_handle_blocked_field_change`).
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

// Route one release event to its Jenkins job, or log it as unresolved.
//
// A release event ScrumMaster can act on names a canonical work item
// (`workItemId`). Every release event `core` publishes does, in every mode
// (V5.2 Canonical Delivery State REQ-08): in local mode from
// `store.py`'s `_publish_release_event`, and in Jira mode from
// `webhook_consumer.py`'s three release publishers
// (`_handle_release_requested`/`_handle_release_done`/
// `_handle_release_abandoned`), each carrying the materialized Release's
// canonical id and, as `project`, its Target Project — the former
// Jira-mode early return here (which used to read the project's mode and
// decline every such event, because nothing published one with a
// `workItemId` before this stage) is gone, so this function triggers
// Jenkins the same way for either mode. One kind still cannot be acted on:
//
//  - one carrying no `workItemId`, which `core` would publish straight
//    from a tracker webhook it could not resolve and identify by an
//    external key alone, if one ever reached this consumer.
//
// It is not rebuilt from the canonical replica, because the local-mode
// sibling relies on Django having already run its own gates (REQ-04). It
// is logged at error level, naming the kind, and triggers no Jenkins job.
async function routeReleaseEvent(data, projectName) {
  const { kind, workItemId, project } = data;

  if (!workItemId) {
    console.error(
      `[dispatch] Unresolved release event (kind "${kind}") on ${projectName} — it names no canonical work item, ` +
      `so no Jenkins job was triggered`
    );
    return;
  }

  const ref = { workItemId, project };
  if (kind === 'requested') {
    await handlers.handleReleaseRequested(ref);
  } else if (kind === 'abandoned') {
    await handlers.handleReleaseAbandoned(ref);
  } else if (kind === 'done') {
    await handlers.handleDone(ref);
  }
}

// The outbound event stream is a fan-out: every event type
// core emits arrives here, not just the ones this consumer
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
    // `blocked_cleared` is the one side-effect kind ScrumMaster acts on from
    // v5.1. Every other kind is recorded by core and has no consumer here
    // until v5.2's writer.
    const { kind } = data;
    if (kind === 'blocked_cleared') {
      await handleBlockedClearedSideEffect(payload.workItemId, envelope);
    }
    return;
  }

  if (eventType === 'work_item.jira_release_event') {
    await routeReleaseEvent(data, projectName);
    return;
  }

  // work_item.jira_event_received, work_item.assigned, work_item.comment_added,
  // etc. — recorded by core already; nothing for this
  // consumer to do.
}

module.exports = {
  startDispatchConsumers,
  stopDispatchConsumers,
  handleWorkItemEventEnvelope,
  maybeDispatch,
  maybeRedispatchForRework,
  handleBlockedClearedSideEffect,
  issueLikeFromCanonical,
  // Exported for gateway.js's restored pipeline-retry redispatch (V5.2
  // Canonical Delivery State REQ-01), which "follows dispatchConsumer.js's
  // canonical pattern". Exported rather than reimplemented so the ONE mode
  // read this module keeps (line 112, the dispatch prompt's display line,
  // REQ-09's kept exception) stays the only one: a second caller building
  // its own issue-like object would need its own getMode call, which is
  // exactly the mode leak REQ-09's search forbids.
  issueLikeFor,
};
