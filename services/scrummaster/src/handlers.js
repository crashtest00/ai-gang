'use strict';

const { execFile } = require('child_process');
const redis = require('./redis');
const registry = require('./registry');
const streams = require('./streams');
const jenkins = require('./jenkins');
const canonicalWorkItems = require('./canonicalWorkItems');
const taskStore = require('./a2a/taskStore');
const { newMessageId } = require('./a2a/ids');
const { buildTextPart, buildMessage, buildTask } = require('./a2a/parts');
const { buildEnvelope, KIND } = require('./envelope');

// Dispatch or continue the one A2A Task for a work item: one Task per work
// item for its whole lifecycle — assignment, unblock and redispatch are all
// continuations of the same Task, never a new assignment. `promptFactory(task,
// message)` receives the not-yet-
// published task id/contextId/messageId so prompt text can embed them for
// the agent to reference in its replies.
//
// `dispatchId` should be stable across retries of the *caller's* work (e.g.
// the triggering canonical event's own messageId) so that a partial-failure
// retry of the calling handler does not launch a second concurrent copy of
// the same task when the handler re-runs from scratch. Known limitation: such
// a retry does re-run this function and appends a second, never-actually-
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
      metadata: {
        // Display-only residue (RELEASE.md §5): the project's tracker key as
        // the canonical project configuration records it. No module resolves
        // anything by it.
        jiraProjectKey: issue.jiraProjectKey || null,
        projectName: issue.projectName,
        agentId: agent.id,
      },
      status: { state, timestamp: new Date().toISOString(), message },
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

// A release work item reached `done`, the single production-approval gate:
// promote the exact SHA that was previewed. `ref` is `{ workItemId, project }`
// — dispatchConsumer.js resolves every release event to a canonical work item
// before calling in, and logs as unresolved any it cannot.
//
// core's store.transition_status already runs the local-mode-native
// dependency unblock (_unblock_dependents) for every work item that reaches a
// terminal status, release or otherwise, so there is nothing to re-derive
// here.
async function handleDone(ref) {
  const { workItemId, project } = ref;

  console.log(`[handler] Done: release ${workItemId}`);
  const full = await canonicalWorkItems.getWorkItem(workItemId, { full: true });
  const candidateSha = full && full.releaseDetail && full.releaseDetail.candidate_sha;
  if (!candidateSha) {
    console.error(`[handler] Release ${workItemId} has no Candidate SHA recorded — cannot promote`);
    return;
  }

  await jenkins.triggerProductionPromote({ workItemId }, project, candidateSha);
}

// A release work item's request (a local Release's `proposed` -> `in-progress`
// move; a Jira Release's creation) was validated. The
// beta-queue-clean check runs in Django BEFORE this event is ever published
// (store.py's transition_status), so there is nothing left to re-check here —
// an event reaching this handler at all already means the queue was clean.
async function handleReleaseRequested(ref) {
  const { workItemId, project } = ref;

  console.log(`[handler] Release requested: ${workItemId}`);
  await jenkins.triggerReleaseCandidate({ workItemId }, project);
  console.log(`[handler] Release ${workItemId} — queue already validated by Django, triggered release-candidate for ${project}`);
}

// A release work item reached `cancelled`. Tears down its preview container
// so it doesn't outlive the release.
async function handleReleaseAbandoned(ref) {
  const { workItemId, project } = ref;

  console.log(`[handler] Release abandoned: ${workItemId}`);
  await jenkins.triggerPreviewTeardown({ workItemId }, project);
}

// Search project workspaces for a BLOCKED {workItemId} marker.
// Returns { file, line, text } or null. Keyed on the canonical work item id:
// the marker convention the agent role documents give is
// `BLOCKED <work item id>`, in each file's own comment syntax.
function findBlockedMarker(workItemId) {
  return new Promise(resolve => {
    const basePath = process.env.PROJECTS_BASE_PATH || '/projects';
    const pattern = `BLOCKED ${workItemId}`;

    execFile('grep', ['-rn', '--include=*', pattern, basePath], (_err, stdout) => {
      if (!stdout || !stdout.trim()) {
        resolve(null);
        return;
      }
      // grep output: /path/to/file:42:  // BLOCKED <work item id> waiting for auth endpoint
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
  handleDone,
  handleReleaseRequested,
  handleReleaseAbandoned,
  // Exported for reuse by dispatchConsumer.js's dispatch paths, which must go
  // through the same dispatch and A2A Task registration.
  dispatchTask,
  findBlockedMarker,
};
