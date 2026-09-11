'use strict';

// Catalog-backed agent-assignment validation — the sole acceptance boundary
// for agent responsibility (the agent-assignment design
// REQ-03..REQ-05). Every path that creates or changes agent responsibility
// (initial refinement/decomposition, Shovel Ready dispatch, blocked-clear
// redispatch, reassignment) MUST call this module rather than writing a
// literal agent id to Jira or dispatching an agent directly.
//
// Pure and transport-agnostic: no Jira or Redis calls, so callers on any
// transport (pub/sub today, Redis Streams elsewhere) can use it identically.

const registry = require('./registry');

const ERROR_CODES = {
  UNKNOWN_AGENT: 'UNKNOWN_AGENT',           // not a registered catalog id
  AGENT_NOT_AVAILABLE: 'AGENT_NOT_AVAILABLE', // registered, but not enabled for this project
  UNKNOWN_PROJECT: 'UNKNOWN_PROJECT',       // projectName has no project configuration at all
};

// Validate a single proposed assignment.
// Returns { ok: true, agent } or { ok: false, code, requestedAgent, permittedAgents }.
function validateAssignment(projectName, agentId) {
  const project = registry.getProject(projectName);
  if (!project) {
    return {
      ok: false,
      code: ERROR_CODES.UNKNOWN_PROJECT,
      requestedAgent: agentId,
      permittedAgents: [],
    };
  }

  const permittedAgents = project.agents.slice();

  const catalogAgent = registry.getAgent(agentId);
  if (!catalogAgent) {
    return { ok: false, code: ERROR_CODES.UNKNOWN_AGENT, requestedAgent: agentId, permittedAgents };
  }

  if (!permittedAgents.includes(agentId)) {
    return { ok: false, code: ERROR_CODES.AGENT_NOT_AVAILABLE, requestedAgent: agentId, permittedAgents };
  }

  return { ok: true, agent: catalogAgent };
}

// Validate an entire proposed decomposition atomically: every subtask's
// `agent` must be valid, or the whole batch is rejected together (REQ-04).
// subtasks: [{ id, displayName, agent, ... }]
// Returns { ok: true } or:
//   { ok: false, errorCode: 'INVALID_AGENT_ASSIGNMENT',
//     rejected: [{ subtaskId, displayName, requestedAgent }],
//     permittedAgents: [...] }
function validateDecomposition(projectName, subtasks) {
  const project = registry.getProject(projectName);
  const permittedAgents = project ? project.agents.slice() : [];

  const rejected = [];
  for (const subtask of subtasks || []) {
    const result = validateAssignment(projectName, subtask.agent);
    if (!result.ok) {
      rejected.push({
        subtaskId: subtask.id,
        displayName: subtask.displayName,
        requestedAgent: subtask.agent,
      });
    }
  }

  if (rejected.length > 0) {
    return { ok: false, errorCode: 'INVALID_AGENT_ASSIGNMENT', rejected, permittedAgents };
  }
  return { ok: true };
}

module.exports = { validateAssignment, validateDecomposition, ERROR_CODES };
