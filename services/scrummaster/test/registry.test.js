'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const registry = require('../src/registry');

// --- parseCatalog: pure-function contract tests (agent-assignment.md REQ-01) ---

test('parseCatalog accepts a well-formed catalog', () => {
  const result = registry.parseCatalog({
    agents: [
      {
        id: 'backend-agent',
        displayName: 'Backend Agent',
        definitionPath: '/agent-docs/backend-agent.md',
        routing: { channelSuffix: 'backend' },
        agentCard: { name: 'Backend Agent', description: 'Implements APIs.' },
      },
    ],
  }, 'test.json');

  assert.deepEqual(result.ids, ['backend-agent']);
  assert.equal(result.byId.get('backend-agent').displayName, 'Backend Agent');
});

test('parseCatalog rejects duplicate agent ids', () => {
  const raw = {
    agents: [
      { id: 'x', displayName: 'X', definitionPath: '/a.md', routing: { channelSuffix: 'x' }, agentCard: { name: 'X', description: 'd' } },
      { id: 'x', displayName: 'X2', definitionPath: '/b.md', routing: { channelSuffix: 'x2' }, agentCard: { name: 'X2', description: 'd' } },
    ],
  };
  assert.throws(() => registry.parseCatalog(raw, 'test.json'), /duplicate agent id "x"/);
});

test('parseCatalog rejects an entry missing required properties', () => {
  const raw = { agents: [{ id: 'no-fields' }] };
  assert.throws(() => registry.parseCatalog(raw, 'test.json'), /displayName is required/);
});

test('parseCatalog rejects invalid routing metadata', () => {
  const raw = {
    agents: [{
      id: 'x', displayName: 'X', definitionPath: '/a.md',
      routing: {}, // missing channelSuffix
      agentCard: { name: 'X', description: 'd' },
    }],
  };
  assert.throws(() => registry.parseCatalog(raw, 'test.json'), /routing\.channelSuffix is required/);
});

test('parseCatalog rejects an id that is both active and retired', () => {
  const raw = {
    agents: [{ id: 'x', displayName: 'X', definitionPath: '/a.md', routing: { channelSuffix: 'x' }, agentCard: { name: 'X', description: 'd' } }],
    retiredAgents: [{ id: 'x' }],
  };
  assert.throws(() => registry.parseCatalog(raw, 'test.json'), /appears in both agents and retiredAgents/);
});

test('parseCatalog rejects a non-array "agents"', () => {
  assert.throws(() => registry.parseCatalog({ agents: 'nope' }, 'test.json'), /must be a non-empty array/);
});

// --- parseProjects: pure-function contract tests (agent-assignment.md REQ-02) ---

const validCatalog = registry.parseCatalog({
  agents: [
    { id: 'backend-agent', displayName: 'Backend Agent', definitionPath: '/a.md', routing: { channelSuffix: 'backend' }, agentCard: { name: 'Backend Agent', description: 'd' } },
  ],
}, 'test.json');

test('parseProjects accepts a project referencing only known agent ids', () => {
  const result = registry.parseProjects({
    projects: [{ name: 'proj', agents: ['backend-agent'] }],
  }, 'test.json', validCatalog);
  assert.deepEqual(result.get('proj').agents, ['backend-agent']);
});

test('parseProjects rejects a project referencing an unknown agent id', () => {
  assert.throws(
    () => registry.parseProjects({ projects: [{ name: 'proj', agents: ['ghost-agent'] }] }, 'test.json', validCatalog),
    /unknown agent id "ghost-agent"/
  );
});

test('parseProjects rejects a duplicate project name', () => {
  assert.throws(
    () => registry.parseProjects({
      projects: [
        { name: 'proj', agents: ['backend-agent'] },
        { name: 'proj', agents: [] },
      ],
    }, 'test.json', validCatalog),
    /duplicate project name "proj"/
  );
});

// --- Singleton loader, against the fixture files ---

test('load() reads the fixture catalog and project config; getEffectiveAgents/getAgent/agentStreamName behave', async (t) => {
  process.env.AGENTS_CATALOG_PATH = path.join(__dirname, 'fixtures', 'agents.json');
  process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, 'fixtures', 'projects.json');

  // Fresh module instance so this test's env vars are the ones load() sees,
  // independent of any other file's (each node --test file already runs in
  // its own process, but this keeps the test explicit about the dependency).
  delete require.cache[require.resolve('../src/registry')];
  const freshRegistry = require('../src/registry');

  freshRegistry.load();

  assert.deepEqual(freshRegistry.getAllAgentIds(), ['refinement-agent', 'backend-agent', 'frontend-agent']);
  assert.deepEqual(freshRegistry.getRetiredAgentIds(), ['qa-agent']);

  const effective = freshRegistry.getEffectiveAgents('test-project');
  assert.deepEqual(effective.map(a => a.id), ['refinement-agent', 'backend-agent']);

  assert.equal(freshRegistry.getEffectiveAgents('no-such-project').length, 0);

  // projectChannel/gatewayChannel (Pub/Sub channel naming) were removed when
  // ScrumMaster<->agent messaging migrated to Redis Streams — see
  // the redis-streams design. agentStreamName/gatewayStreamName
  // are the replacement, keyed by the same routing.channelSuffix.
  assert.equal(
    freshRegistry.agentStreamName('test-project', 'backend'),
    'aigang:agent:test-project:backend'
  );
  assert.equal(
    freshRegistry.gatewayStreamName('test-project'),
    'aigang:gateway:test-project'
  );
});
