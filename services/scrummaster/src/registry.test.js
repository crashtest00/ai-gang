'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

process.env.AGENTS_CATALOG_PATH = path.join(__dirname, '../config/agents.json');
process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, '../config/projects.json');
const registry = require('./registry');
const schema = require('./a2a/schema');

// REQ-08 — every registered agent produces a valid derived AgentCard, and
// advertised skills are distinguishable from internal operating methods.

test('every registered agent produces a valid derived AgentCard', () => {
  const cards = registry.listAgentCards();
  const fieldValues = ['refinement-agent', 'backend-agent', 'frontend-agent', 'devops-agent'];

  for (const fieldValue of fieldValues) {
    const card = cards[fieldValue];
    assert.ok(card, `expected an AgentCard for ${fieldValue}`);
    schema.validateAgentCard(card);
    assert.ok(card.skills.length >= 1, `${fieldValue} must advertise at least one skill`);
  }
});

test('getAgentCard returns null for an unregistered agent field value', () => {
  assert.equal(registry.getAgentCard('nonexistent-agent'), null);
});

test('an AgentCard skill is distinct from routing metadata', () => {
  const card = registry.getAgentCard('backend-agent');
  const skillKeys = Object.keys(card.skills[0]);
  assert.deepEqual(skillKeys.sort(), ['description', 'id', 'name']);
  assert.ok(!('definition_path' in card), 'AgentCard must not carry routing metadata');
});
