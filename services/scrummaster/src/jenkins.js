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

// `ref` is always `{ workItemId }` — the canonical work-item id, the only
// work-item identifier these three jobs take, in either mode (V5.2
// Canonical Delivery State REQ-08; the bare-tracker-key shape a Jira-mode
// release used to carry here is retired along with that key). Sent as the
// payload's only work-item field, so jenkins.yaml's genericTrigger binds
// one variable, `WORK_ITEM_ID`.
function _refPayload(ref) {
  return { workItemId: ref.workItemId };
}

function _refLabel(ref) {
  return ref.workItemId;
}

// Fired once `core` has confirmed beta's queue is clean, for a release in
// either mode (REQ-08 ends the v5.1 exception under which a Jira-mode
// release event triggered nothing). Jenkins pins the candidate SHA, cuts
// `release/<sha>`, opens the `release/<sha> → prod` PR, and stands up the
// preview container.
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

// Fired when `core` reports a release reached a terminal state without
// shipping (its `abandoned` release event), so its preview container
// doesn't outlive it.
async function triggerPreviewTeardown(ref, projectName) {
  await invoke('release-preview-teardown', { ..._refPayload(ref), projectName });
  console.log(`[jenkins] Triggered release-preview-teardown for ${_refLabel(ref)} (${projectName})`);
}

module.exports = { triggerReleaseCandidate, triggerProductionPromote, triggerPreviewTeardown };
