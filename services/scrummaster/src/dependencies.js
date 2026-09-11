'use strict';

// Dependency-aware subtask handling.
//
// ScrumMaster is not an AI agent: this module performs only the deterministic
// Jira writes and reads the spec describes. It never infers a dependency that
// the Refinement Agent did not declare, and it keeps no local dependency
// graph or cache — every decision is derived from Jira at call time.

const jiraDefault = require('./jira');
const canonicalWorkItemsDefault = require('./canonicalWorkItems');

class MaterializationValidationError extends Error {
  constructor(rejected, permittedAgents) {
    super('Decomposition rejected — one or more subtasks reference an agent not permitted for this project');
    this.name = 'MaterializationValidationError';
    this.rejected = rejected;
    this.permittedAgents = permittedAgents;
  }
}

class MaterializationNoProgressError extends Error {
  constructor(unresolved) {
    super('Decomposition materialization made no progress — unresolved blocker reference, self-dependency, or a cycle');
    this.name = 'MaterializationNoProgressError';
    this.unresolved = unresolved;
  }
}

// services/scrummaster/src/assignment.js owns catalog-backed
// validation of proposed agent owners. It is required, not optional: no
// path may create or change agent responsibility without that validation,
// so if the module cannot be loaded this function fails closed (refuses to
// materialize anything) rather than silently skipping the check.
function loadAssignmentModule() {
  try {
    // eslint-disable-next-line global-require
    return require('./assignment');
  } catch (err) {
    return null;
  }
}

// Process one complete Refinement Agent decomposition message and
// materialize it into Jira as subtasks and dependency links,
// order-independently and idempotently.
//
// message: { parentJiraIssueKey, subtasks: [{ id, displayName, description,
//   agent, "Blocked By": [] }, ...] } — the structured-data content of the
//   canonical materializeDecomposition operation.
// projectName: normalized Jira project name, used for catalog validation.
// deps: { jira, assignment } — both injectable for unit testing; default to
//   the real jira.js and a lazily-required ./assignment.
async function materializeDecomposition(message, projectName, deps = {}) {
  const jira = deps.jira || jiraDefault;
  const assignment = deps.assignment !== undefined ? deps.assignment : loadAssignmentModule();

  const parentJiraIssueKey = message && message.parentJiraIssueKey;
  const subtasks = message && message.subtasks;

  if (!parentJiraIssueKey || !Array.isArray(subtasks)) {
    throw new Error('materializeDecomposition: message is missing parentJiraIssueKey or a subtasks array');
  }

  if (!assignment) {
    throw new Error(
      'materializeDecomposition: services/scrummaster/src/assignment.js is not available — refusing to materialize a decomposition without catalog-backed assignment validation'
    );
  }

  // Assignment integrity: reject the whole decomposition atomically on any
  // invalid owner. Nothing is written to Jira from a rejected decomposition.
  const validation = assignment.validateDecomposition(projectName, subtasks);
  if (!validation.ok) {
    const list = validation.rejected
      .map(r => `  - ${r.displayName || r.subtaskId} → requested agent "${r.requestedAgent}"`)
      .join('\n');
    await jira.postComment(
      parentJiraIssueKey,
      `Decomposition rejected — the following subtasks request an agent not permitted for this project:\n\n${list}\n\n` +
      `Permitted agents: ${validation.permittedAgents.join(', ') || '(none configured for this project)'}\n\n` +
      `No Jira subtasks were created. Fix the decomposition and resend it.`
    );
    throw new MaterializationValidationError(validation.rejected, validation.permittedAgents);
  }

  const parent = await jira.getIssue(parentJiraIssueKey);

  // Recover proposal UUID -> Jira key from subtasks already materialized
  // under this parent, so redelivery after a partial run (crash, retry)
  // reuses existing subtasks and links instead of duplicating them.
  // This map is scratch state for this call only — it is
  // never persisted or reused across invocations.
  const existingSubtasks = await jira.getSubtasksByParent(parentJiraIssueKey);
  const idToKey = new Map();
  for (const sub of existingSubtasks) {
    for (const label of sub.labels) {
      const proposalId = jira.proposalIdFromLabel(label);
      if (proposalId) idToKey.set(proposalId, sub.key);
    }
  }

  // Pre-populate already-created links so a retry doesn't attempt to
  // duplicate one Jira does not itself deduplicate.
  const linkedPairs = new Set();
  for (const sub of existingSubtasks) {
    const links = await jira.getIssueLinks(sub.key);
    for (const blockerKey of links.isBlockedBy) {
      linkedPairs.add(`${blockerKey}->${sub.key}`);
    }
  }

  const rootProposalIds = new Set(
    subtasks.filter(s => !s['Blocked By'] || s['Blocked By'].length === 0).map(s => s.id)
  );

  const pending = subtasks.slice();
  let progressed = true;

  // Order-independent multi-pass materialization. A proposal resolved
  // earlier in this same invocation counts as existing for a later proposal
  // in the same pass — a subtask created earlier in this invocation
  // deliberately qualifies as already existing.
  while (pending.length > 0 && progressed) {
    progressed = false;

    for (let i = pending.length - 1; i >= 0; i--) {
      const proposal = pending[i];
      const blockedBy = proposal['Blocked By'] || [];

      const blockerKeys = [];
      let allResolved = true;
      for (const blockerId of blockedBy) {
        const blockerKey = idToKey.get(blockerId);
        if (!blockerKey) { allResolved = false; break; }
        blockerKeys.push(blockerKey);
      }
      if (!allResolved) continue;

      let subtaskKey = idToKey.get(proposal.id);
      if (!subtaskKey) {
        subtaskKey = await jira.createSubtaskForProposal(parentJiraIssueKey, parent.project, {
          summary: proposal.displayName,
          description: proposal.description,
          agentFieldValue: proposal.agent,
          proposalId: proposal.id,
        });
        idToKey.set(proposal.id, subtaskKey);
      }

      for (const blockerKey of blockerKeys) {
        const pairKey = `${blockerKey}->${subtaskKey}`;
        if (!linkedPairs.has(pairKey)) {
          await jira.createIssueLink(blockerKey, subtaskKey);
          linkedPairs.add(pairKey);
        }
      }

      pending.splice(i, 1);
      progressed = true;
    }
  }

  if (pending.length > 0) {
    // No-progress rule: missing blocker references, self-dependencies, and
    // cycles all present the same way — mechanically report them rather than
    // interpreting or repairing the plan.
    const unresolved = pending.map(p => ({
      id: p.id,
      displayName: p.displayName,
      blockedBy: p['Blocked By'] || [],
    }));
    const list = unresolved
      .map(u => `  - ${u.displayName} (${u.id}) — unresolved blockers: ${u.blockedBy.join(', ') || '(none)'}`)
      .join('\n');
    await jira.postComment(
      parentJiraIssueKey,
      `Decomposition materialization stalled — the following subtasks could not be resolved ` +
      `(a missing blocker reference, a self-dependency, or a cycle):\n\n${list}\n\n` +
      `Any subtasks already materialized remain outside Shovel Ready. Fix the decomposition and resend it.`
    );
    throw new MaterializationNoProgressError(unresolved);
  }

  // Only root (independent) subtasks enter Shovel Ready; the existing Shovel
  // Ready webhook handler performs initial dispatch — this function never
  // dispatches an agent directly.
  for (const proposalId of rootProposalIds) {
    const key = idToKey.get(proposalId);
    if (key) await jira.transitionIssue(key, 'Shovel Ready');
  }

  return { idToKey };
}

// Triggered when a Sub-task transitions to Done. Stateless and idempotent:
// derives every decision from live Jira status and links, so a replayed or
// out-of-order Done event cannot dispatch the same dependent subtask twice.
async function handleSubtaskDone(issueKey, deps = {}) {
  const jira = deps.jira || jiraDefault;

  const links = await jira.getIssueLinks(issueKey);

  for (const candidateKey of links.blocks) {
    const candidate = await jira.getIssue(candidateKey);
    // Only a candidate still in the dependency-waiting state (Backlog, with
    // at least one is-blocked-by link) is eligible — this makes a duplicate
    // or out-of-order Done event a no-op once the candidate has moved on.
    if (candidate.status !== 'Backlog') continue;

    const candidateLinks = await jira.getIssueLinks(candidateKey);
    if (candidateLinks.isBlockedBy.length === 0) continue;

    let allBlockersDone = true;
    for (const blockerKey of candidateLinks.isBlockedBy) {
      const blocker = await jira.getIssue(blockerKey);
      if (blocker.status !== 'Done') { allBlockersDone = false; break; }
    }

    if (allBlockersDone) {
      await jira.transitionIssue(candidateKey, 'Shovel Ready');
    }
  }
}

// Extends catalog-backed assignment validation to validate and record
// against canonical work item ids, and covers the internal dependency
// graph — this is the mode-aware entry point that satisfies both without
// touching materializeDecomposition above, which remains the exact,
// unmodified Jira-mode implementation with no regression for a Jira-mode
// project.
//
// message: { parentId, subtasks } — `parentId` is a Jira issue key in Jira
// mode or a canonical work item id in local mode; the caller does not need
// to know which, since that's exactly what this function resolves via
// canonicalWorkItems.getMode(). subtasks keep the existing
// {id, displayName, description, agent, "Blocked By"} shape unchanged in
// both modes.
async function routeMaterialization(message, projectName, deps = {}) {
  const canonicalWorkItems = deps.canonicalWorkItems || canonicalWorkItemsDefault;
  const jira = deps.jira || jiraDefault;
  const assignment = deps.assignment !== undefined ? deps.assignment : loadAssignmentModule();

  const mode = await canonicalWorkItems.getMode(projectName);

  if (mode.mode === 'jira') {
    // Unchanged path — no regression for a Jira-mode project.
    return materializeDecomposition(
      { parentJiraIssueKey: message.parentId, subtasks: message.subtasks },
      projectName,
      { jira, assignment }
    );
  }

  // Local mode: no Jira ticket exists to materialize into. Publish a single
  // Streams command to the Internal Work-Item Service's command channel —
  // the service's own materialize.js applies the identical
  // order-independent, atomic-rejection algorithm against its own store.
  // This function does not itself validate agents or write anything: the
  // write-gating and single-validator rules both require that to happen
  // only inside the internal API's own write path, not duplicated here.
  return canonicalWorkItems.publishCommand(projectName, {
    command: 'materializeDecomposition',
    actor: 'refinement-agent',
    message: { parentWorkItemId: message.parentId, subtasks: message.subtasks },
  });
}

module.exports = {
  materializeDecomposition,
  handleSubtaskDone,
  routeMaterialization,
  MaterializationValidationError,
  MaterializationNoProgressError,
};
