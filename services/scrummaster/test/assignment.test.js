'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

// Point the singleton registry at the fixture catalog/project config before
// requiring assignment.js (which requires registry.js internally) —
// this is the sole validator every
// assignment-producing path must call.
process.env.AGENTS_CATALOG_PATH = path.join(__dirname, 'fixtures', 'agents.json');
process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, 'fixtures', 'projects.json');
delete require.cache[require.resolve('../src/registry')];
delete require.cache[require.resolve('../src/assignment')];
const assignment = require('../src/assignment');

test('validateAssignment accepts an agent enabled for the project', () => {
  const result = assignment.validateAssignment('test-project', 'backend-agent');
  assert.equal(result.ok, true);
  assert.equal(result.agent.id, 'backend-agent');
});

test('validateAssignment rejects an unknown agent id', () => {
  const result = assignment.validateAssignment('test-project', 'ghost-agent');
  assert.equal(result.ok, false);
  assert.equal(result.code, assignment.ERROR_CODES.UNKNOWN_AGENT);
  assert.deepEqual(result.permittedAgents, ['refinement-agent', 'backend-agent']);
});

test('validateAssignment rejects a registered agent not enabled for the project', () => {
  // frontend-agent exists in the fixture catalog but is not in test-project's allowed set
  const result = assignment.validateAssignment('test-project', 'frontend-agent');
  assert.equal(result.ok, false);
  assert.equal(result.code, assignment.ERROR_CODES.AGENT_NOT_AVAILABLE);
});

test('validateAssignment rejects an unconfigured project', () => {
  const result = assignment.validateAssignment('no-such-project', 'backend-agent');
  assert.equal(result.ok, false);
  assert.equal(result.code, assignment.ERROR_CODES.UNKNOWN_PROJECT);
});

test('validateDecomposition accepts a decomposition where every subtask uses a permitted agent', () => {
  const result = assignment.validateDecomposition('test-project', [
    { id: '1', displayName: 'Backend work', agent: 'backend-agent' },
    { id: '2', displayName: 'Refinement follow-up', agent: 'refinement-agent' },
  ]);
  assert.deepEqual(result, { ok: true });
});

test('validateDecomposition rejects the whole batch atomically when any subtask is invalid', () => {
  const result = assignment.validateDecomposition('test-project', [
    { id: '1', displayName: 'Backend work', agent: 'backend-agent' },
    { id: '2', displayName: 'Frontend work', agent: 'frontend-agent' }, // not available
    { id: '3', displayName: 'Ghost work', agent: 'ghost-agent' },       // unknown
  ]);

  assert.equal(result.ok, false);
  assert.equal(result.errorCode, 'INVALID_AGENT_ASSIGNMENT');
  // Only the invalid entries are reported — the valid one is not silently
  // dropped from the error, but nor is it reported as if accepted.
  assert.deepEqual(result.rejected, [
    { subtaskId: '2', displayName: 'Frontend work', requestedAgent: 'frontend-agent' },
    { subtaskId: '3', displayName: 'Ghost work', requestedAgent: 'ghost-agent' },
  ]);
  assert.deepEqual(result.permittedAgents, ['refinement-agent', 'backend-agent']);
});

// deriveAgentFromSummary — the recovery path for a create_subtask request
// that names its role in the summary but omits the agent id itself.

test('deriveAgentFromSummary derives the agent from a role prefix the project has', () => {
  const agent = assignment.deriveAgentFromSummary('test-project', 'Backend: add the /health endpoint');
  assert.equal(agent.id, 'backend-agent');
});

test('deriveAgentFromSummary accepts the id and display-name spellings of the same role', () => {
  for (const summary of ['backend-agent: do it', 'Backend Agent: do it', 'BACKEND: do it']) {
    assert.equal(assignment.deriveAgentFromSummary('test-project', summary).id, 'backend-agent', summary);
  }
});

test('deriveAgentFromSummary does not derive an agent the project is not permitted', () => {
  // frontend-agent is in the fixture catalog but not in test-project's allowed set
  assert.equal(assignment.deriveAgentFromSummary('test-project', 'Frontend: build the form'), null);
});

test('deriveAgentFromSummary derives nothing from an unrecognized or absent role prefix', () => {
  assert.equal(assignment.deriveAgentFromSummary('test-project', 'Database: add an index'), null);
  assert.equal(assignment.deriveAgentFromSummary('test-project', 'Add the /health endpoint'), null);
  assert.equal(assignment.deriveAgentFromSummary('test-project', ': no role at all'), null);
  assert.equal(assignment.deriveAgentFromSummary('test-project', ''), null);
  assert.equal(assignment.deriveAgentFromSummary('test-project', undefined), null);
});

test('deriveAgentFromSummary derives nothing for an unconfigured project', () => {
  assert.equal(assignment.deriveAgentFromSummary('no-such-project', 'Backend: add the /health endpoint'), null);
});
