'use strict';

// Build the Claude Code prompt for a task_assigned-equivalent A2A dispatch.
// issue: result of jira.getIssue()
// agent: result of registry.getAgent()
// context.allowedAgents (optional): catalog entries for the effective
// allowed-agent set of the target project, included only for dispatches
// (e.g. to the Refinement Agent) that need to choose among agent ids.
// Rendered dynamically from the catalog rather than hardcoded in any prompt text.
// context.task, context.message: the Task and the dispatch message this
// prompt belongs to. Both stay in the promptFactory contract every dispatch
// path calls this through, and neither is rendered: a session gets its
// dispatch context from the environment the subscriber exports, so no prompt
// carries an id for an agent to copy into anything (agent-commons.md REQ-03,
// deterministic-gateway-message-tooling.md REQ-04).
function buildTaskPrompt(issue, agent, context = {}) {
  const lines = [];
  lines.push(`## ROLE`);
  lines.push(`You are the ${agent.displayName}. Read your full agent definition before taking any action:`);
  lines.push(agent.definitionPath);
  lines.push('');

  lines.push(`## TICKET CONTEXT`);
  lines.push(`Ticket: ${issue.key} — ${issue.summary}`);
  if (issue.parent) lines.push(`Parent ticket: ${issue.parent}`);
  lines.push('');

  if (issue.behavior) {
    lines.push(`### Behavior`);
    lines.push(issue.behavior);
    lines.push('');
  }

  if (issue.acceptanceCriteria) {
    lines.push(`### Acceptance Criteria`);
    lines.push(issue.acceptanceCriteria);
    lines.push('');
  }

  if (issue.constraints) {
    lines.push(`### Constraints`);
    lines.push(issue.constraints);
    lines.push('');
  }

  if (issue.edgeCases) {
    lines.push(`### Edge Cases`);
    lines.push(issue.edgeCases);
    lines.push('');
  }

  if (issue.outOfScope) {
    lines.push(`### Out of Scope`);
    lines.push(issue.outOfScope);
    lines.push('');
  }

  if (issue.comments.length > 0) {
    lines.push(`## COMMENT THREAD`);
    lines.push('The following clarifications have been provided:');
    for (const c of issue.comments) {
      lines.push(`[${c.timestamp}] ${c.author}: ${c.body}`);
    }
    lines.push('');
  }

  if (context.allowedAgents && context.allowedAgents.length > 0) {
    lines.push(`## ALLOWED AGENTS`);
    lines.push(`This project permits assigning subtasks only to the agent ids below. A request naming`);
    lines.push(`any other value is refused and creates nothing — name exactly one of these ids, copied`);
    lines.push(`exactly, as the agent each subtask you request is for:`);
    for (const a of context.allowedAgents) {
      lines.push(`- ${a.id}: ${a.agentCard.description}`);
    }
    lines.push('');
  }

  lines.push(...buildWorkItemReferences(issue));

  return lines.join('\n');
}

// Build the Claude Code prompt for an unblocked (continuation) A2A dispatch.
// blockedMarker: { file, line, text } or null
function buildUnblockPrompt(issue, agent, task, message, blockedMarker) {
  const lines = [];
  lines.push(`## ROLE`);
  lines.push(`You are the ${agent.displayName}. Read your full agent definition before taking any action:`);
  lines.push(agent.definitionPath);
  lines.push('');

  lines.push(`## TICKET CONTEXT`);
  lines.push(`Ticket: ${issue.key} — ${issue.summary}`);
  if (issue.parent) lines.push(`Parent ticket: ${issue.parent}`);
  lines.push('');
  lines.push(`Description:`);
  lines.push(issue.description || '(no description provided)');
  lines.push('');

  if (issue.comments.length > 0) {
    lines.push(`## COMMENT THREAD`);
    lines.push('The following clarifications have been provided (most recent last):');
    for (const c of issue.comments) {
      lines.push(`[${c.timestamp}] ${c.author}: ${c.body}`);
    }
    lines.push('');
  }

  lines.push(`## RESUME POINT`);
  lines.push(`You previously stopped work on this ticket and left a BLOCKED marker.`);
  if (blockedMarker) {
    lines.push(`File: ${blockedMarker.file}`);
    if (blockedMarker.line) lines.push(`Line: ${blockedMarker.line}`);
    if (blockedMarker.text) lines.push(`Your note: ${blockedMarker.text}`);
  } else {
    lines.push(`(No BLOCKED marker found in codebase — review the comment thread for context.)`);
  }
  lines.push('Continue from this point using the clarification provided above.');
  lines.push('');

  lines.push(...buildWorkItemReferences(issue));

  return lines.join('\n');
}

// Build the Claude Code prompt for redispatching the recorded implementation
// owner after a pipeline failure or human-requested rework.
// evidence: { kind: 'pipeline_failure', build_url, build_number } or
// { kind: 'human_rework' }.
function buildRetryPrompt(issue, agent, evidence, task, message) {
  const lines = [];
  lines.push(`## ROLE`);
  lines.push(`You are the ${agent.displayName}. Read your full agent definition before taking any action:`);
  lines.push(agent.definitionPath);
  lines.push('');

  lines.push(`## TICKET CONTEXT`);
  lines.push(`Ticket: ${issue.key} — ${issue.summary}`);
  if (issue.parent) lines.push(`Parent ticket: ${issue.parent}`);
  lines.push('');

  lines.push(`## RESUME POINT`);
  if (evidence?.kind === 'pipeline_failure') {
    lines.push(`The project pipeline failed on your pull request for this ticket.`);
    if (evidence.build_url) lines.push(`Build log: ${evidence.build_url}`);
    if (evidence.build_number) lines.push(`Build number: ${evidence.build_number}`);
    lines.push(`Diagnose the failure, fix it, and push the fix to the same branch/PR.`);
  } else {
    lines.push(`A human reviewer requested rework after reviewing this ticket on beta.`);
    lines.push(`Read the comment thread below for the specific rework requested, address it, and push the fix to the same branch/PR.`);
  }
  lines.push('');

  if (issue.comments.length > 0) {
    lines.push(`## COMMENT THREAD`);
    lines.push('The following clarifications have been provided (most recent last):');
    for (const c of issue.comments) {
      lines.push(`[${c.timestamp}] ${c.author}: ${c.body}`);
    }
    lines.push('');
  }

  lines.push(...buildWorkItemReferences(issue));

  return lines.join('\n');
}

// The work item's references, in every dispatch, continuation and retry
// prompt. What an agent needs in order to submit is not here and is not in
// any prompt: the task, the context and the message a submission continues
// reach the session as environment variables the subscriber exports, and the
// constructor in the agent commons reads them itself — an agent names an
// operation and its fields and authors nothing
// (agent-commons.md REQ-02/REQ-03, deterministic-gateway-message-tooling.md
// REQ-04). These three lines are the work item's own references, which an
// agent reads: the tracker key, and the canonical ids of its specification
// link and artifact links.
function buildWorkItemReferences(issue) {
  const lines = [];

  lines.push(`## WORK ITEM REFERENCES`);
  lines.push(`Jira issue key: ${issue.key}`);
  // v4.1 agent-artifact-automation.md REQ-04 — named here, not in a
  // conditionally-omitted block: a work item with neither reference must
  // still produce a prompt that SAYS so ("none"), not one that is merely
  // silent about them (REQ-04's acceptance). `issue.specificationLink`/
  // `issue.artifactLinks` come from dispatchConsumer.js's
  // issueLikeFromCanonical for a local-mode dispatch; a Jira-mode issue
  // (jira.getIssue()) carries neither, so those dispatches read "none" —
  // harmless, since this builder is shared across both modes
  // (build brief §1b carry-forward 2) and REQ-04 scopes the requirement to
  // the canonical path only. Every value here is an AI Gang canonical id —
  // never a delivered path, which the building agent obtains by asking the
  // librarian itself.
  lines.push(`Specification link: ${issue.specificationLink ? `${issue.specificationLink.artifactId} (${issue.specificationLink.requirementId})` : 'none'}`);
  lines.push(`Artifact links: ${(issue.artifactLinks && issue.artifactLinks.length > 0) ? issue.artifactLinks.join(', ') : 'none'}`);

  return lines;
}

module.exports = { buildTaskPrompt, buildUnblockPrompt, buildRetryPrompt };
