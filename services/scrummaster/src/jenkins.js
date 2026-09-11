'use strict';

const axios = require('axios');

// dev → beta promotion is no longer triggered here — it happens automatically,
// per-project, inside each project's own Jenkinsfile pipeline on merge to `dev`.
// ScrumMaster's role in the release
// flow is now limited to the three jobs below, all fired from a Release ticket.

async function invoke(token, payload) {
  const jenkinsUrl = process.env.JENKINS_URL;
  if (!jenkinsUrl) {
    console.warn(`[jenkins] JENKINS_URL not set — skipping ${token} trigger`);
    return;
  }

  const url = `${jenkinsUrl.replace(/\/$/, '')}/generic-webhook-trigger/invoke`;
  await axios.post(url, payload, { params: { token } });
}

// `ref` is `'GANG-42'` (a bare Jira issue key, Jira mode — unchanged) or
// `{ workItemId }` (local mode — no
// Jira ticket). Sent as distinct payload fields (`issueKey` vs.
// `workItemId`), never both, so jenkins.yaml's genericTrigger can tell
// which mode a build is for and route its writeback stage accordingly
// (see that file's `WORK_ITEM_ID` genericVariable).
function _refPayload(ref) {
  return typeof ref === 'string' ? { issueKey: ref } : { workItemId: ref.workItemId };
}

function _refLabel(ref) {
  return typeof ref === 'string' ? ref : ref.workItemId;
}

// Fired once ScrumMaster (Jira mode) or work-item-service (local mode, via
// its own Django-side check) has confirmed beta's queue is clean for a new
// release. Jenkins pins the candidate SHA, cuts `release/<sha>`, opens the
// `release/<sha> → prod` PR, and stands up the preview container.
async function triggerReleaseCandidate(ref, projectName) {
  await invoke('release-candidate', { ..._refPayload(ref), projectName });
  console.log(`[jenkins] Triggered release-candidate for ${_refLabel(ref)} (${projectName})`);
}

// Fired when a release moves to Done — the single production-approval gate.
// Jenkins merges the frozen PR and redeploys the already-built artifact
// pinned at candidateSha, without rebuilding.
async function triggerProductionPromote(ref, projectName, candidateSha) {
  await invoke('production-promote', { ..._refPayload(ref), projectName, candidateSha });
  console.log(`[jenkins] Triggered production-promote for ${_refLabel(ref)} (${projectName} @ ${candidateSha})`);
}

// Fired when a release reaches a terminal state without shipping (Jira
// mode: resolution set to Abandoned; local mode: `cancelled`) so its
// preview container doesn't outlive it.
async function triggerPreviewTeardown(ref, projectName) {
  await invoke('release-preview-teardown', { ..._refPayload(ref), projectName });
  console.log(`[jenkins] Triggered release-preview-teardown for ${_refLabel(ref)} (${projectName})`);
}

module.exports = { triggerReleaseCandidate, triggerProductionPromote, triggerPreviewTeardown };
