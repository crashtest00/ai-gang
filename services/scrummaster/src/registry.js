'use strict';

const fs = require('fs');
const { deriveAgentCard } = require('./a2a/agentCard');

// Canonical agent catalog (services/scrummaster/config/agents.json) and per-project
// available-agent configuration (services/scrummaster/config/projects.json).
// See the agent-assignment design REQ-01 and REQ-02.
//
// Both are loaded and validated once, at first use (effectively at process
// startup — see index.js, which calls load() eagerly so a malformed catalog
// or project config fails the process before it accepts any traffic).

let catalog = null;   // { byId: Map<string, agent>, ids: string[] }
let projects = null;  // Map<string, { name, jiraProjectKey, agents: string[] }>
let agentCardsById = null; // Map<string, AgentCard> — derived from catalog.byId

function catalogPath() {
  return process.env.AGENTS_CATALOG_PATH || '/app/config/agents.json';
}

function projectsPath() {
  return process.env.PROJECTS_CONFIG_PATH || '/app/config/projects.json';
}

function readJson(path) {
  return JSON.parse(fs.readFileSync(path, 'utf8'));
}

// Validate and index the raw agents.json contents. Throws with a message
// that names every problem found (not just the first) so a maintainer can
// fix a broken catalog in one pass.
function parseCatalog(raw, path) {
  const errors = [];
  const agents = Array.isArray(raw.agents) ? raw.agents : null;
  if (!agents) errors.push(`"agents" must be a non-empty array`);

  const seenIds = new Set();
  const byId = new Map();

  (agents || []).forEach((entry, i) => {
    const where = `agents[${i}]`;
    if (!entry || typeof entry !== 'object') {
      errors.push(`${where} must be an object`);
      return;
    }
    const { id, displayName, definitionPath, routing, agentCard } = entry;

    if (!id || typeof id !== 'string') {
      errors.push(`${where}.id is required and must be a non-empty string`);
    } else if (seenIds.has(id)) {
      errors.push(`duplicate agent id "${id}" (${where})`);
    } else {
      seenIds.add(id);
    }

    if (!displayName || typeof displayName !== 'string') {
      errors.push(`${where}.displayName is required (id: ${id || '?'})`);
    }
    if (!definitionPath || typeof definitionPath !== 'string') {
      errors.push(`${where}.definitionPath is required (id: ${id || '?'})`);
    }
    if (!routing || typeof routing !== 'object' || !routing.channelSuffix || typeof routing.channelSuffix !== 'string') {
      errors.push(`${where}.routing.channelSuffix is required (id: ${id || '?'})`);
    }
    if (!agentCard || typeof agentCard !== 'object' || !agentCard.name || !agentCard.description) {
      errors.push(`${where}.agentCard.{name,description} are required (id: ${id || '?'})`);
    }

    if (id && typeof id === 'string' && displayName && definitionPath && routing?.channelSuffix) {
      byId.set(id, entry);
    }
  });

  const retiredAgents = raw.retiredAgents === undefined ? [] : raw.retiredAgents;
  if (!Array.isArray(retiredAgents)) {
    errors.push(`"retiredAgents" must be an array if present`);
  } else {
    retiredAgents.forEach((entry, i) => {
      const where = `retiredAgents[${i}]`;
      if (!entry || typeof entry.id !== 'string' || !entry.id) {
        errors.push(`${where}.id is required`);
        return;
      }
      if (seenIds.has(entry.id)) {
        errors.push(`"${entry.id}" appears in both agents and retiredAgents (${where})`);
      }
    });
  }

  if (errors.length > 0) {
    throw new Error(`Invalid agent catalog at ${path}:\n  - ${errors.join('\n  - ')}`);
  }

  return {
    byId,
    ids: Array.from(byId.keys()),
    retiredIds: retiredAgents.map(r => r.id),
  };
}

// Validate the raw projects.json contents against the loaded catalog.
// Throws, naming the invalid project and agent id, if a project references
// an agent id that isn't a valid catalog entry.
function parseProjects(raw, path, cat) {
  const errors = [];
  const list = Array.isArray(raw.projects) ? raw.projects : null;
  if (!list) errors.push(`"projects" must be a non-empty array`);

  const byName = new Map();

  (list || []).forEach((entry, i) => {
    const where = `projects[${i}]`;
    if (!entry || typeof entry.name !== 'string' || !entry.name) {
      errors.push(`${where}.name is required`);
      return;
    }
    if (byName.has(entry.name)) {
      errors.push(`duplicate project name "${entry.name}" (${where})`);
    }
    const agentIds = Array.isArray(entry.agents) ? entry.agents : null;
    if (!agentIds) {
      errors.push(`${where}.agents must be an array (project: ${entry.name})`);
    } else {
      for (const id of agentIds) {
        if (!cat.byId.has(id)) {
          errors.push(`project "${entry.name}" references unknown agent id "${id}" — not present in agents.json`);
        }
      }
    }
    byName.set(entry.name, {
      name: entry.name,
      jiraProjectKey: entry.jiraProjectKey || null,
      agents: agentIds || [],
    });
  });

  if (errors.length > 0) {
    throw new Error(`Invalid project configuration at ${path}:\n  - ${errors.join('\n  - ')}`);
  }

  return byName;
}

function load() {
  if (catalog && projects) return;
  const cPath = catalogPath();
  const pPath = projectsPath();

  catalog = parseCatalog(readJson(cPath), cPath);
  console.log(`[registry] Loaded ${catalog.ids.length} agent(s) from ${cPath}`);

  projects = parseProjects(readJson(pPath), pPath, catalog);
  console.log(`[registry] Loaded ${projects.size} project(s) from ${pPath}`);

  // Fail fast: every registered agent must produce a valid AgentCard (REQ-08
  // of the a2a-messaging design). A catalog entry that can't
  // derive one is a configuration error, not a runtime-recoverable condition.
  agentCardsById = new Map();
  for (const id of catalog.ids) {
    agentCardsById.set(id, deriveAgentCard(catalog.byId.get(id)));
  }
}

// Look up a catalog entry by id. Returns null if the id is not a registered
// agent — callers must not treat "not found" as a fallback to any default.
function getAgent(id) {
  load();
  return catalog.byId.get(id) || null;
}

// Look up the derived AgentCard for a registered agent, or null if unknown.
function getAgentCard(id) {
  load();
  return agentCardsById.get(id) || null;
}

// All registered AgentCards, keyed by catalog agent id.
function listAgentCards() {
  load();
  return Object.fromEntries(agentCardsById);
}

// All registered agent ids, in catalog order.
function getAllAgentIds() {
  load();
  return catalog.ids.slice();
}

function getRetiredAgentIds() {
  load();
  return catalog.retiredIds.slice();
}

function getProjectNames() {
  load();
  return Array.from(projects.keys());
}

function getProject(name) {
  load();
  return projects.get(name) || null;
}

// The effective allowed-agent set for a project: catalog entries (not just
// ids) for every agent id the project's configuration enables. Returns []
// for an unconfigured project name rather than throwing — callers decide
// whether that's an error for their context.
function getEffectiveAgents(projectName) {
  load();
  const project = projects.get(projectName);
  if (!project) return [];
  return project.agents
    .map(id => catalog.byId.get(id))
    .filter(Boolean);
}

// Stream topology (the redis-streams design §4). Logical
// names are stable configuration, never accepted from an untrusted payload —
// every caller must derive `project` from the configured destination it is
// already bound to (a Jira issue's own project, or the stream a consumer is
// reading), not from message content.
const GATEWAY_GROUP = 'scrummaster';
// canonical-work-model.md REQ-15 / internal-work-item-service.md REQ-05-06:
// this service's own consumer group on the internal work-item service's
// outbound event stream (any interested subscriber creates its own group —
// see services/work-item-service/workitems/stream_topology.py's module comment).
const JIRA_CATCHUP_GROUP = 'jira-catchup';
// canonical-work-model.md REQ-21 — ScrumMaster's dispatch-trigger consumer
// group on that SAME outbound event stream. A second, independent
// subscriber (Streams' normal fan-out — every interested consumer gets its
// own group), not a replacement for JIRA_CATCHUP_GROUP.
const DISPATCH_GROUP = 'dispatch';

function normalizeProjectName(name) {
  return String(name).trim().toLowerCase();
}

// ScrumMaster -> agent: aigang:agent:{project}:{suffix}, group agent-{suffix}
function agentStreamName(projectName, suffix) {
  return `aigang:agent:${normalizeProjectName(projectName)}:${suffix}`;
}

function agentGroupName(suffix) {
  return `agent-${suffix}`;
}

// Agent -> ScrumMaster: aigang:gateway:{project}, group scrummaster
function gatewayStreamName(projectName) {
  return `aigang:gateway:${normalizeProjectName(projectName)}`;
}

// Internal work-item service -> ScrumMaster (outbound events): matches
// services/work-item-service/workitems/stream_topology.py's event_stream_name()
// exactly — the shared naming contract both sides agree on (same reasoning
// as commandStreamName in canonicalWorkItems.js).
function workItemEventStreamName(projectName) {
  return `aigang:workitems:${normalizeProjectName(projectName)}:events`;
}

module.exports = {
  load,
  // Exported for direct contract testing of catalog/project-config
  // validation without needing to write temp files or reset module state.
  parseCatalog,
  parseProjects,
  getAgent,
  getAgentCard,
  listAgentCards,
  getAllAgentIds,
  getRetiredAgentIds,
  getProjectNames,
  getProject,
  getEffectiveAgents,
  normalizeProjectName,
  agentStreamName,
  agentGroupName,
  gatewayStreamName,
  workItemEventStreamName,
  GATEWAY_GROUP,
  JIRA_CATCHUP_GROUP,
  DISPATCH_GROUP,
};
