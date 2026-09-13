'use strict';

// Catalog-backed agent-assignment validation — the sole acceptance boundary
// for agent responsibility. Every path that creates or changes agent
// responsibility (initial refinement/decomposition, Shovel Ready dispatch,
// blocked-clear redispatch, reassignment) MUST call this module rather than
// writing a literal agent id to Jira or dispatching an agent directly.
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

// Derive the agent a `create_subtask` request is for when the request carries
// no explicit agent id. Subtask summaries follow a `<Role>: <what to do>`
// convention, so that prefix is the one role signal the payload already
// carries. Derivation succeeds only when the prefix names exactly one agent
// the project actually has — an unrecognized, ambiguous, or absent prefix
// returns null, leaving the caller to report the request rather than guess.
// This never falls back to a default agent: it only recovers an id the
// request itself already implies.
// Returns the catalog entry, or null.
function deriveAgentFromSummary(projectName, summary) {
  const role = summaryRolePrefix(summary);
  if (!role) return null;

  const matches = registry
    .getEffectiveAgents(projectName)
    .filter(agent => roleAliases(agent).includes(role));

  return matches.length === 1 ? matches[0] : null;
}

// Compare role names on their letters and digits alone, so "Backend",
// "backend-agent" and "Backend Agent" are the same role.
function normalizeRole(text) {
  return String(text || '').toLowerCase().replace(/[^a-z0-9]/g, '');
}

function withoutAgentSuffix(token) {
  return token.replace(/agent$/, '') || token;
}

function summaryRolePrefix(summary) {
  const text = String(summary || '');
  const colon = text.indexOf(':');
  if (colon <= 0) return null;
  return withoutAgentSuffix(normalizeRole(text.slice(0, colon))) || null;
}

// Every spelling of one agent's role that a summary prefix may legitimately
// use: its catalog id, its stream routing suffix, and its display name, each
// with and without a trailing "agent".
function roleAliases(agent) {
  const tokens = [agent.id, agent.routing && agent.routing.channelSuffix, agent.displayName]
    .filter(Boolean)
    .map(normalizeRole)
    .filter(Boolean);
  return Array.from(new Set(tokens.concat(tokens.map(withoutAgentSuffix))));
}

// Validate an entire proposed decomposition atomically: every subtask's
// `agent` must be valid, or the whole batch is rejected together.
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

module.exports = { validateAssignment, validateDecomposition, deriveAgentFromSummary, ERROR_CODES };
