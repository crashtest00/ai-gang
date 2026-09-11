'use strict';

// Build the Claude Code prompt for a task_assigned-equivalent A2A dispatch.
// issue: result of jira.getIssue()
// agent: result of registry.getAgent()
// context.allowedAgents (optional): catalog entries for the effective
// allowed-agent set of the target project, included only for dispatches
// (e.g. to the Refinement Agent) that need to choose among agent ids
// (the agent-assignment design REQ-01, REQ-03). Rendered
// dynamically from the catalog rather than hardcoded in any prompt text.
// context.task: { id, contextId } — the Task this dispatch belongs to.
// context.message: { messageId } — the id of this dispatch's own client
//   Message, which the agent must set as `referenceMessageId` on its first
//   reply. See the a2a-messaging design.
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
    lines.push(`This project permits assigning subtasks only to the agent ids below. Any other value`);
    lines.push(`will be rejected atomically by the decomposition tool along with the rest of your submission —`);
    lines.push(`use exactly one of these ids in each subtask's "agent" field:`);
    for (const a of context.allowedAgents) {
      lines.push(`- ${a.id}: ${a.agentCard.description}`);
    }
    lines.push('');
  }

  lines.push(...buildA2AInstructions(issue, context.task, context.message));

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

  lines.push(...buildA2AInstructions(issue, task, message));

  return lines.join('\n');
}

// Build the Claude Code prompt for redispatching the recorded implementation
// owner after a pipeline failure or human-requested rework (release-workflow.md
// REQ-11). evidence: { kind: 'pipeline_failure', build_url, build_number } or
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

  lines.push(...buildA2AInstructions(issue, task, message));

  return lines.join('\n');
}

// Shared A2A task-context + gateway-protocol instructions for every dispatch/
// continuation/retry prompt. See the a2a-messaging design.
//
// Agents submit the *payload* shown below — gateway-publish.js wraps it in
// the transport envelope (schemaVersion/messageId/kind/taskId/contextId; see
// setup/lib/gateway-publish.js and the redis-streams design) —
// so the agent only ever needs to think in A2A terms, never Streams terms.
function buildA2AInstructions(issue, task, message) {
  const lines = [];

  lines.push(`## A2A TASK CONTEXT`);
  lines.push(`Task ID: ${task.id}`);
  lines.push(`Context ID: ${task.contextId}`);
  lines.push(`Last Message ID: ${message.messageId}`);
  lines.push(`Jira issue key: ${issue.key}`);
  lines.push('');

  lines.push(`## INSTRUCTIONS`);
  lines.push(`- Read your agent definition fully before taking any action`);
  lines.push(`- All Jira interactions go through the ScrumMaster gateway stream as canonical A2A content — never write a bare {"type": ...} payload`);
  lines.push(`- Generate a new messageId for every submission you make (e.g. \`cat /proc/sys/kernel/random/uuid\`) and remember it — your NEXT submission's referenceMessageId must point back to it. Your first submission's referenceMessageId is the Last Message ID above.`);
  lines.push(`- Every submission MUST have this exact shape:`);
  lines.push('  {');
  lines.push('    "state": "working | input-required | auth-required | completed",');
  lines.push('    "message": {');
  lines.push('      "kind": "message",');
  lines.push('      "messageId": "<uuid you generated for this submission>",');
  lines.push(`      "taskId": "${task.id}",`);
  lines.push(`      "contextId": "${task.contextId}",`);
  lines.push('      "role": "agent",');
  lines.push('      "referenceMessageId": "<the messageId you are replying to>",');
  lines.push('      "parts": [');
  lines.push('        { "kind": "text", "text": "<human-readable note>" },');
  lines.push('        { "kind": "data", "data": { "operation": "comment" } }');
  lines.push('      ]');
  lines.push('    }');
  lines.push('  }');
  lines.push(`- Use this exact pattern to submit it (temp file avoids shell escaping issues):`);
  lines.push(`  cat > /tmp/msg.json << 'ENDJSON'`);
  lines.push(`  { ...submission... }`);
  lines.push(`  ENDJSON`);
  lines.push(`  node /agent-docs/lib/gateway-publish.js ${issue.projectName} /tmp/msg.json`);
  lines.push(`  The project name is: ${issue.projectName} — use this exact string, do not substitute anything else`);
  lines.push(`  A non-zero exit means the operation was NOT durably accepted — check the printed error and retry`);
  lines.push('- Supported operations, set inside the message\'s "data" part (all also accept an optional "reference": {"file": "...", "function": "..."}):');
  lines.push('  | operation        | state                        | when to use |');
  lines.push('  |------------------|------------------------------|-------------|');
  lines.push('  | comment          | working                      | progress update, no PR yet |');
  lines.push('  | reassign         | working                      | hand the ticket\'s Agent field to another registered agent — data: {"operation":"reassign","agentFieldValue":"<agent>"} |');
  lines.push('  | create_subtask   | working                      | (Refinement Agent only) request a new subtask under THIS ticket — data: {"operation":"create_subtask","summary":"...","description":"...","agentFieldValue":"<agent>"} |');
  lines.push('  | (blocked)        | input-required / auth-required | you need human clarification (input-required) or missing credentials/authorization (auth-required) — omit "operation", put the precise question in the text part; do not block without a precise, located question |');
  lines.push('  | (complete)       | completed                    | your work is fully done — omit "operation", include a summary text part |');
  lines.push('- To open a pull request: set "state" to "completed" and add this sibling "artifacts" array to your submission:');
  lines.push('  "artifacts": [ { "kind": "artifact", "artifactId": "<uuid>", "taskId": "' + task.id + '", "name": "pull-request", "parts": [ { "kind": "file", "file": { "name": "pull-request", "mimeType": "text/uri-list", "uri": "<PR URL>" } }, { "kind": "text", "text": "<summary>" } ] } ]');
  lines.push('  Opening a PR does not transition the ticket or reassign it — that is Jenkins\' job once the pipeline passes (release-workflow.md). Just post the PR and stop.');
  lines.push(`- Do not block without a precise, located question`);

  return lines;
}

module.exports = { buildTaskPrompt, buildUnblockPrompt, buildRetryPrompt };
