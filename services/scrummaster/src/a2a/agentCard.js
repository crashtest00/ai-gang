'use strict';

const schema = require('./schema');

// Derive a machine-readable AgentCard from a catalog entry's nested
// `agentCard` block. Routing metadata (id, definitionPath,
// routing.channelSuffix) is a separate concern and is not part of the card.
//
// agent-assignment.md's own catalog contract (REQ-01) requires only
// `agentCard.{name,description}` — a card is still derivable from just that,
// per this feature's REQ-08 ("a machine-readable AgentCard MUST be derivable
// from that definition"). When `skills` is absent, synthesize the one
// implicit skill an agent with a single description obviously has, rather
// than hard-failing catalog load for every entry that predates this field.
function deriveAgentCard(entry) {
  const card = entry.agentCard || {};
  const name = card.name || entry.displayName;

  const skills = card.skills && card.skills.length > 0
    ? card.skills
    : [{ id: entry.id, name, description: card.description }];

  const derived = {
    name,
    description: card.description,
    provider: card.provider || { organization: 'AI Gang' },
    version: card.version || '1.0.0',
    protocolVersion: card.protocolVersion || '0.2',
    capabilities: card.capabilities || { streaming: false, pushNotifications: false },
    skills,
    defaultInputModes: card.defaultInputModes || ['text/plain'],
    defaultOutputModes: card.defaultOutputModes || ['text/plain'],
  };

  schema.validateAgentCard(derived);
  return derived;
}

module.exports = { deriveAgentCard };
