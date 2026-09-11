'use strict';

const { execFile } = require('child_process');
const jira = require('./jira');
const redis = require('./redis');
const registry = require('./registry');
const streams = require('./streams');
const assignment = require('./assignment');
const dependencies = require('./dependencies');
const jenkins = require('./jenkins');
const canonicalWorkItems = require('./canonicalWorkItems');
const taskStore = require('./a2a/taskStore');
const { newMessageId } = require('./a2a/ids');
const { buildTextPart, buildMessage, buildTask } = require('./a2a/parts');
const { buildEnvelope, KIND } = require('./envelope');
const { buildTaskPrompt, buildUnblockPrompt, buildRetryPrompt } = require('./prompt');

// Dispatch or continue the one A2A Task for a Jira issue: one Task per
// ticket for its whole lifecycle — assignment, unblock, and
// pipeline-retry/rework redispatch are all continuations of the same
// Task, never a new assignment. `promptFactory(task, message)` receives
// the not-yet-
// published task id/contextId/messageId so prompt text can embed them for
// the agent to reference in its replies.
//
// `dispatchId` should be stable across retries of the *caller's* work (e.g.
// the triggering webhook envelope's own messageId) so that a partial-failure
// retry of the calling handler (dispatch succeeds, a later Jira call throws)
// does not launch a second concurrent copy of the same task when the handler
// re-runs from scratch. Known limitation: such a
// retry does re-run this function and appends a second, never-actually-
// transmitted continuation message to the in-memory Task record before
// streams.publish's own dedupeKey discards the duplicate XADD — harmless
// (the agent never sees it, and lineage validation only requires the
// referenced id to appear somewhere in history, not to be the latest), but
// worth knowing about if the in-memory history ever looks one message longer
// than what an agent actually received.
async function dispatchTask(issue, agent, { dispatchId, promptFactory }) {
  const contextId = taskStore.contextFor(issue);
  const existing = taskStore.getTaskById(issue.key);
  const messageId = newMessageId();
  const state = existing ? 'working' : 'submitted';
  const referenceMessageId = existing ? (taskStore.lastMessage(existing.id)?.messageId || null) : null;

  const taskRef = { id: issue.key, contextId: existing ? existing.contextId : contextId };
  const prompt = promptFactory(taskRef, { messageId });

  const message = buildMessage({
    messageId,
    taskId: taskRef.id,
    contextId: taskRef.contextId,
    role: 'client',
    parts: [buildTextPart(prompt)],
    referenceMessageId,
  });

  if (existing) {
    // Reaching this path means a fresh canonical dispatch event selected an
    // already-known Task. That is the one controlled reason a terminal Task
    // may reopen; agent-authored gateway messages do not receive this flag.
    taskStore.applyTransition(taskRef.id, { state, message, reopen: true });
  } else {
    taskStore.register(buildTask({
      id: taskRef.id,
      contextId: taskRef.contextId,
      status: { state, timestamp: new Date().toISOString(), message },
      metadata: {
        jiraIssueKey: issue.key,
        jiraProjectKey: issue.project,
        jiraProjectName: issue.projectName,
        agentId: agent.id,
      },
    }));
  }

  const client = redis.getClient();
  const stream = registry.agentStreamName(issue.projectName, agent.routing.channelSuffix);
  const envelope = buildEnvelope({
    kind: KIND.TASK,
    project: registry.normalizeProjectName(issue.projectName),
    taskId: taskRef.id,
    contextId: taskRef.contextId,
    payload: message,
  });
  await streams.publish(client, stream, envelope, { dedupeKey: `dispatch:${dispatchId || newMessageId()}` });
  console.log(`[handler] Dispatched (${state}) for ${issue.key} to ${agent.id} on ${stream}`);
}

// Post a durable, visible record of a rejected agent assignment.
// Identifies the
// attempted responsibility, requested agent, failure reason, and recovery
// action, and never reports the responsibility as assigned, in progress, or
// complete — callers must return without dispatching or transitioning.
async function reportAssignmentFailure(issueKey, requestedAgent, result) {
  const reason = result.code === assignment.ERROR_CODES.UNKNOWN_AGENT
    ? `"${requestedAgent}" is not a registered agent id in the catalog.`
    : result.code === assignment.ERROR_CODES.UNKNOWN_PROJECT
      ? `This project has no available-agent configuration.`
      : `"${requestedAgent}" is a registered agent but is not enabled for this project.`;

  const permitted = result.permittedAgents.length > 0
    ? result.permittedAgents.join(', ')
    : '(none configured for this project)';

  await jira.postComment(
    issueKey,
    `Cannot dispatch this ticket — its Agent field value ("${requestedAgent}") failed catalog validation.\n\n` +
    `Reason: ${reason}\n` +
    `Permitted agents for this project: ${permitted}\n\n` +
    `Recovery: set the Agent field to one of the permitted values above and re-trigger dispatch ` +
    `(move back to Backlog and forward to Shovel Ready again, or clear/re-set Blocked).`
  );
  await jira.setBlockedField(issueKey, true);
  console.error(`[handler] ${issueKey} assignment rejected — requested "${requestedAgent}" (${result.code})`);
}

// Required story schema fields. ScrumMaster owns this check — the Refinement Agent
// only ever receives a story when all required fields are non-empty.
const REQUIRED_STORY_FIELDS = [
  { key: 'behavior',           label: 'Behavior' },
  { key: 'acceptanceCriteria', label: 'Acceptance Criteria' },
  { key: 'constraints',        label: 'Constraints' },
  { key: 'edgeCases',          label: 'Edge Cases' },
  { key: 'outOfScope',         label: 'Out of Scope' },
];

// Handler 1: A new Story was created in Jira.
// Validates required schema fields, then assigns to refinement-agent and dispatches.
// If any required field is missing, blocks the ticket and comments with what's missing.
// `dispatchId` (optional): stable id for this logical trigger — see dispatchTask.
async function handleStoryCreated(issueKey, { dispatchId } = {}) {
  console.log(`[handler] Story created: ${issueKey}`);

  const agent = registry.getAgent('refinement-agent');
  if (!agent) {
    console.error('[handler] refinement-agent not found in registry');
    return;
  }

  // Set Agent field and post acknowledgement comment
  await jira.setAgentField(issueKey, 'refinement-agent');
  await jira.postComment(issueKey, 'Ticket received. Assigned to Refinement Agent for decomposition.');

  const issue = await jira.getIssue(issueKey);

  // Validate required schema fields before dispatching to the Refinement Agent
  const missing = REQUIRED_STORY_FIELDS
    .filter(f => !issue[f.key] || !issue[f.key].trim())
    .map(f => f.label);

  if (missing.length > 0) {
    const list = missing.map(l => `  - ${l}`).join('\n');
    await jira.postComment(
      issueKey,
      `Story is missing required fields and cannot be refined until they are filled in:\n\n${list}\n\nPlease complete these fields and move the ticket back to Backlog to retry.`
    );
    await jira.setBlockedField(issueKey, true);
    console.log(`[handler] Story ${issueKey} blocked — missing fields: ${missing.join(', ')}`);
    return;
  }

  // The Refinement Agent's assignment decisions become authoritative only
  // through the catalog-backed decomposition tool, but it still needs the
  // project's effective allowed-agent set up front to choose sensibly —
  // derived from the catalog, never hardcoded.
  const allowedAgents = registry.getEffectiveAgents(issue.projectName);

  await dispatchTask(issue, agent, {
    dispatchId,
    promptFactory: (task, message) => buildTaskPrompt(issue, agent, { allowedAgents, task, message }),
  });

  console.log(`[handler] Story ${issueKey} dispatched to refinement-agent`);
}

// Handler 2: A ticket's status changed to "Shovel Ready".
// Dispatches the assigned dev agent and transitions ticket to "In Progress".
async function handleShovelReady(issueKey, { dispatchId } = {}) {
  console.log(`[handler] Shovel Ready: ${issueKey}`);

  const issue = await jira.getIssue(issueKey);
  const agentValue = issue.agent;

  if (!agentValue) {
    console.warn(`[handler] ${issueKey} has no Agent field set — skipping dispatch`);
    return;
  }

  const result = assignment.validateAssignment(issue.projectName, agentValue);
  if (!result.ok) {
    await reportAssignmentFailure(issueKey, agentValue, result);
    return;
  }
  const agent = result.agent;

  await dispatchTask(issue, agent, {
    dispatchId,
    promptFactory: (task, message) => buildTaskPrompt(issue, agent, { task, message }),
  });

  await jira.transitionIssue(issueKey, 'In Progress');

  console.log(`[handler] ${issueKey} dispatched to ${agentValue}`);
}

// Handler 3: The Blocked field was cleared on a ticket (human provided clarification).
// For a ticket owned by the Refinement Agent, this is a continuation of the
// same schema-validation gate handleStoryCreated enforces — a story most
// often lands here because it was blocked for missing required fields, so
// the redispatch must re-run that check and, once it passes, hand the
// Refinement Agent the same full task prompt (Behavior/Acceptance Criteria/
// Constraints/Edge Cases/Out of Scope) it would have received on first
// dispatch — buildUnblockPrompt's bare description isn't enough for it to
// decompose the story. Every other agent (dev agents mid-implementation)
// keeps the existing BLOCKED-marker resume flow: searches the codebase for
// the marker it left, then re-dispatches with buildUnblockPrompt.
async function handleBlockedCleared(issueKey, { dispatchId } = {}) {
  console.log(`[handler] Blocked cleared: ${issueKey}`);

  const issue = await jira.getIssue(issueKey);
  const agentValue = issue.agent;

  if (!agentValue) {
    console.warn(`[handler] ${issueKey} has no Agent field set — skipping unblock`);
    return;
  }

  const result = assignment.validateAssignment(issue.projectName, agentValue);
  if (!result.ok) {
    await reportAssignmentFailure(issueKey, agentValue, result);
    return;
  }
  const agent = result.agent;

  if (agentValue === 'refinement-agent') {
    // Re-run the same required-schema-fields gate handleStoryCreated applies
    // before ever dispatching to the Refinement Agent — clearing Blocked
    // must not bypass it.
    const missing = REQUIRED_STORY_FIELDS
      .filter(f => !issue[f.key] || !issue[f.key].trim())
      .map(f => f.label);

    if (missing.length > 0) {
      const list = missing.map(l => `  - ${l}`).join('\n');
      await jira.postComment(
        issueKey,
        `Story is still missing required fields and cannot be refined until they are filled in:\n\n${list}\n\nPlease complete these fields and clear the Blocked field again to retry.`
      );
      await jira.setBlockedField(issueKey, true);
      console.log(`[handler] Story ${issueKey} re-blocked — still missing fields: ${missing.join(', ')}`);
      return;
    }

    const allowedAgents = registry.getEffectiveAgents(issue.projectName);

    await dispatchTask(issue, agent, {
      dispatchId,
      promptFactory: (task, message) => buildTaskPrompt(issue, agent, { allowedAgents, task, message }),
    });

    console.log(`[handler] ${issueKey} unblocked, dispatched to refinement-agent with full task prompt`);
    return;
  }

  const blockedMarker = await findBlockedMarker(issueKey);

  await dispatchTask(issue, agent, {
    dispatchId,
    promptFactory: (task, message) => buildUnblockPrompt(issue, agent, task, message, blockedMarker),
  });

  console.log(`[handler] ${issueKey} unblocked, dispatched to ${agentValue}`);
}

// Handler 4: A ticket's status changed to "Done".
// For a Sub-task, this also runs the dependency-handling Done Handler, which
// dispatches any dependent subtask whose blockers are now all Done —
// independent of, and in
// addition to, the rest of this function. For a Story/Sub-task otherwise,
// Done means "accepted on beta" — beta already has the code from the
// automatic per-merge deploy, so there's nothing to promote. For a Release
// ticket, Done is the single production-approval gate: promote the exact SHA
// that was previewed.
// `ref` is `{ jiraIssueKey }` (Jira mode — unchanged) or
// `{ workItemId, project }` (local mode — no Jira ticket to fetch).
// dispatchConsumer.js branches the raw
// event payload into one or the other before calling in.
async function handleDone(ref) {
  const { jiraIssueKey, workItemId, project } = ref;

  if (jiraIssueKey) {
    console.log(`[handler] Done: ${jiraIssueKey}`);
    const issue = await jira.getIssue(jiraIssueKey);

    if (issue.issuetype === 'Sub-task') {
      await dependencies.handleSubtaskDone(jiraIssueKey);
    }

    if (issue.issuetype !== 'Release') {
      console.log(`[handler] ${jiraIssueKey} (${issue.issuetype}) accepted on beta — no promotion triggered`);
      return;
    }

    const projectName = issue.targetProjectName || issue.targetProject;
    if (!projectName) {
      console.error(`[handler] Release ${jiraIssueKey} has no Target Project set — cannot promote`);
      return;
    }
    if (!issue.candidateSha) {
      console.error(`[handler] Release ${jiraIssueKey} has no Candidate SHA set — cannot promote`);
      return;
    }

    await jenkins.triggerProductionPromote(jiraIssueKey, projectName, issue.candidateSha);
    return;
  }

  // Local mode: this handler is only ever reached for a `release` work
  // item's own Done transition — the Sub-task dependency-unblock
  // branch above is Jira-mode-only; store.py's transition_status already
  // does the local-mode-native equivalent (_unblock_dependents) for every
  // mode-agnostic work item, release or otherwise, so there is nothing to
  // re-derive here.
  console.log(`[handler] Done: release ${workItemId}`);
  const full = await canonicalWorkItems.getWorkItem(workItemId, { full: true });
  const candidateSha = full && full.releaseDetail && full.releaseDetail.candidate_sha;
  if (!candidateSha) {
    console.error(`[handler] Release ${workItemId} has no Candidate SHA recorded — cannot promote`);
    return;
  }

  await jenkins.triggerProductionPromote({ workItemId }, project, candidateSha);
}

// Handler 5: A new Release ticket was created in Jira, or a local-mode
// `release` work item's own `proposed` -> `in-review` transition was
// validated. Jira mode's
// beta-queue-clean check runs HERE, as it always has; local mode's runs in
// Django BEFORE this event is ever published (store.py's
// transition_status), so there is nothing left to re-check here — a local-
// mode event reaching this handler at all already means the queue was
// clean, since both modes route through this same event/consumer pair.
async function handleReleaseRequested(ref) {
  const { jiraIssueKey, workItemId, project } = ref;

  if (jiraIssueKey) {
    console.log(`[handler] Release requested: ${jiraIssueKey}`);
    const issue = await jira.getIssue(jiraIssueKey);
    const projectKey = issue.targetProject;
    const projectName = issue.targetProjectName || issue.targetProject;

    if (!projectKey) {
      await jira.postComment(jiraIssueKey, 'Release ticket is missing the required Target Project field. Set it and re-create the Release ticket.');
      console.error(`[handler] Release ${jiraIssueKey} has no Target Project set`);
      return;
    }

    const outstanding = await jira.searchIssues(
      `project = "${projectKey}" AND issuetype != Release AND status = "In Review"`
    );

    if (outstanding.length > 0) {
      const list = outstanding.map(i => `  - ${i.key}`).join('\n');
      await jira.postComment(
        jiraIssueKey,
        `Cannot cut a release candidate — the following tickets are still awaiting tester acceptance on beta:\n\n${list}\n\nResolve these (move to Done or otherwise off beta's queue) and re-create the Release ticket.`
      );
      console.log(`[handler] Release ${jiraIssueKey} blocked — outstanding: ${outstanding.map(i => i.key).join(', ')}`);
      return;
    }

    await jenkins.triggerReleaseCandidate(jiraIssueKey, projectName);
    console.log(`[handler] Release ${jiraIssueKey} — queue clean, triggered release-candidate for ${projectName}`);
    return;
  }

  console.log(`[handler] Release requested: ${workItemId}`);
  await jenkins.triggerReleaseCandidate({ workItemId }, project);
  console.log(`[handler] Release ${workItemId} — queue already validated by Django, triggered release-candidate for ${project}`);
}

// Handler 6: A Release ticket was abandoned (resolution set to "Abandoned"),
// or a local-mode `release` work item reached `cancelled`. Tears
// down its preview container so it doesn't outlive the release.
async function handleReleaseAbandoned(ref) {
  const { jiraIssueKey, workItemId, project } = ref;

  if (jiraIssueKey) {
    console.log(`[handler] Release abandoned: ${jiraIssueKey}`);
    const issue = await jira.getIssue(jiraIssueKey);
    const projectName = issue.targetProjectName || issue.targetProject;

    if (!projectName) {
      console.error(`[handler] Abandoned Release ${jiraIssueKey} has no Target Project set — cannot tear down preview`);
      return;
    }

    await jenkins.triggerPreviewTeardown(jiraIssueKey, projectName);
    return;
  }

  console.log(`[handler] Release abandoned: ${workItemId}`);
  await jenkins.triggerPreviewTeardown({ workItemId }, project);
}

// Redispatch a ticket's recorded implementation owner with updated Jira
// context. Used both for a Jenkins pipeline-retry
// message and for a human moving a ticket from "In Review" back to
// "In Progress" after requesting rework in a comment. Never invents an
// owner — if the ticket has none recorded, it is surfaced rather than guessed.
async function redispatchImplementationOwner(issueKey, evidence, { dispatchId } = {}) {
  const issue = await jira.getIssue(issueKey);
  const agentValue = issue.agent;

  if (!agentValue) {
    console.error(`[handler] ${issueKey} has no recorded Agent field — cannot redispatch`);
    await jira.postComment(
      issueKey,
      'Cannot redispatch this ticket for rework — no implementation owner is recorded on the Agent field. Set it and retry.'
    );
    await jira.setBlockedField(issueKey, true);
    return;
  }

  const agent = registry.getAgent(agentValue);
  if (!agent) {
    console.error(`[handler] Agent "${agentValue}" not found in registry for ${issueKey}`);
    return;
  }

  await dispatchTask(issue, agent, {
    dispatchId,
    promptFactory: (task, message) => buildRetryPrompt(issue, agent, evidence, task, message),
  });

  console.log(`[handler] ${issueKey} redispatched to ${agentValue} (${evidence?.kind || 'unknown reason'})`);
}

// Handler 7: a human moved a ticket from "In Review" back to "In Progress"
// to request rework after beta review. The
// human is expected to have already left a comment explaining the request;
// redispatchImplementationOwner includes the full comment thread in the
// agent's prompt.
async function handleReworkRequested(issueKey, opts = {}) {
  console.log(`[handler] Rework requested: ${issueKey}`);
  await redispatchImplementationOwner(issueKey, { kind: 'human_rework' }, opts);
}

// Search project workspaces for a BLOCKED {issueKey} marker.
// Returns { file, line, text } or null.
function findBlockedMarker(issueKey) {
  return new Promise(resolve => {
    const basePath = process.env.PROJECTS_BASE_PATH || '/projects';
    const pattern = `BLOCKED ${issueKey}`;

    execFile('grep', ['-rn', '--include=*', pattern, basePath], (_err, stdout) => {
      if (!stdout || !stdout.trim()) {
        resolve(null);
        return;
      }
      // grep output: /path/to/file:42:  // BLOCKED GANG-42 waiting for auth endpoint
      const firstMatch = stdout.trim().split('\n')[0];
      const parts = firstMatch.split(':');
      if (parts.length < 3) { resolve(null); return; }

      const file = parts[0];
      const line = parseInt(parts[1], 10) || null;
      const text = parts.slice(2).join(':').trim();

      resolve({ file, line, text });
    });
  });
}

module.exports = {
  handleStoryCreated,
  handleShovelReady,
  handleBlockedCleared,
  handleDone,
  handleReleaseRequested,
  handleReleaseAbandoned,
  handleReworkRequested,
  redispatchImplementationOwner,
  // Exported for reuse by gateway.js's create_subtask/reassign A2A
  // operations, which must go through the same dispatch and catalog-backed
  // assignment-validation paths as webhook-triggered dispatch.
  dispatchTask,
  reportAssignmentFailure,
  // Exported for reuse by dispatchConsumer.js's continuation
  // dispatch (the same BLOCKED-marker resume flow handleBlockedCleared's
  // non-refinement branch already used).
  findBlockedMarker,
  REQUIRED_STORY_FIELDS,
};
