'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  materializeDecomposition,
  handleSubtaskDone,
  routeMaterialization,
  MaterializationValidationError,
  MaterializationNoProgressError,
} = require('./dependencies');
const realJira = require('./jira');

// In-memory fake of the jira.js functions dependencies.js calls, so these
// tests exercise the materialization/Done-Handler algorithm without a live
// Jira instance. Reuses the real proposalLabel/proposalIdFromLabel helpers
// since those are pure and part of the persisted-label contract under test.
function makeFakeJira(parentKey, projectKey) {
  const parent = { key: parentKey, status: 'In Progress', project: projectKey };
  const subtasks = new Map(); // key -> { key, parent, summary, status, labels, agent }
  const links = []; // { blockerKey, dependentKey }
  const comments = [];
  const calls = { createSubtaskForProposal: 0, createIssueLink: 0, transitionIssue: [] };
  let counter = 0;

  function nextKey() {
    counter += 1;
    return `${projectKey}-${100 + counter}`;
  }

  return {
    calls,
    comments,
    proposalLabel: realJira.proposalLabel,
    proposalIdFromLabel: realJira.proposalIdFromLabel,

    async getIssue(key) {
      if (key === parentKey) return { ...parent };
      const s = subtasks.get(key);
      if (!s) throw new Error(`fakeJira.getIssue: unknown issue ${key}`);
      return { key: s.key, status: s.status, project: projectKey };
    },

    async getSubtasksByParent(pKey) {
      return Array.from(subtasks.values())
        .filter(s => s.parent === pKey)
        .map(s => ({ key: s.key, summary: s.summary, status: s.status, labels: s.labels.slice() }));
    },

    async getIssueLinks(key) {
      return {
        blocks: links.filter(l => l.blockerKey === key).map(l => l.dependentKey),
        isBlockedBy: links.filter(l => l.dependentKey === key).map(l => l.blockerKey),
      };
    },

    async createSubtaskForProposal(pKey, pProjectKey, { summary, description, agentFieldValue, proposalId }) {
      calls.createSubtaskForProposal += 1;
      const key = nextKey();
      subtasks.set(key, {
        key,
        parent: pKey,
        summary,
        status: 'Backlog',
        labels: [realJira.proposalLabel(proposalId)],
        agent: agentFieldValue,
      });
      return key;
    },

    async createIssueLink(blockerKey, dependentKey) {
      calls.createIssueLink += 1;
      links.push({ blockerKey, dependentKey });
    },

    async transitionIssue(key, status) {
      calls.transitionIssue.push({ key, status });
      const s = subtasks.get(key);
      if (s) s.status = status;
    },

    async postComment(key, text) {
      comments.push({ key, text });
    },

    // Test-only helpers for setting up Done-Handler scenarios directly.
    _addSubtask(key, { status = 'Backlog' } = {}) {
      subtasks.set(key, { key, parent: parentKey, summary: key, status, labels: [] });
    },
    _addLink(blockerKey, dependentKey) {
      links.push({ blockerKey, dependentKey });
    },
    _status(key) {
      return subtasks.get(key)?.status;
    },
    _subtaskCount() {
      return subtasks.size;
    },
  };
}

const allowAllAssignment = { validateDecomposition: () => ({ ok: true }) };

function proposal(id, displayName, blockedBy = [], agent = 'backend-agent') {
  return { id, displayName, description: `desc for ${displayName}`, agent, 'Blocked By': blockedBy };
}

test('materializes an independent and a dependent subtask, only the root reaches Shovel Ready', async () => {
  const jira = makeFakeJira('PROJ-1', 'PROJ');
  const backend = proposal('b1', 'Backend API');
  const frontend = proposal('f1', 'Frontend screen', ['b1']);

  const { idToKey } = await materializeDecomposition(
    { parentJiraIssueKey: 'PROJ-1', subtasks: [frontend, backend] }, // dependent listed first
    'proj',
    { jira, assignment: allowAllAssignment }
  );

  assert.equal(jira.calls.createSubtaskForProposal, 2);
  assert.equal(jira.calls.createIssueLink, 1);
  assert.equal(jira._status(idToKey.get('b1')), 'Shovel Ready');
  assert.equal(jira._status(idToKey.get('f1')), 'Backlog');
});

test('a reverse-ordered three-link chain materializes and only the root enters Shovel Ready', async () => {
  const jira = makeFakeJira('PROJ-2', 'PROJ');
  const c = proposal('c', 'C', ['b']);
  const b = proposal('b', 'B', ['a']);
  const a = proposal('a', 'A', []);

  const { idToKey } = await materializeDecomposition(
    { parentJiraIssueKey: 'PROJ-2', subtasks: [c, b, a] },
    'proj',
    { jira, assignment: allowAllAssignment }
  );

  assert.equal(jira.calls.createSubtaskForProposal, 3);
  assert.equal(jira.calls.createIssueLink, 2);
  assert.equal(jira._status(idToKey.get('a')), 'Shovel Ready');
  assert.equal(jira._status(idToKey.get('b')), 'Backlog');
  assert.equal(jira._status(idToKey.get('c')), 'Backlog');
});

test('a dependent with two blockers is not created until both blockers resolve', async () => {
  const jira = makeFakeJira('PROJ-3', 'PROJ');
  const dependent = proposal('d', 'D', ['a', 'b']);
  const a = proposal('a', 'A', []);
  const b = proposal('b', 'B', []);

  const { idToKey } = await materializeDecomposition(
    { parentJiraIssueKey: 'PROJ-3', subtasks: [dependent, a, b] },
    'proj',
    { jira, assignment: allowAllAssignment }
  );

  assert.equal(jira.calls.createSubtaskForProposal, 3);
  assert.equal(jira.calls.createIssueLink, 2);
  assert.equal(jira._status(idToKey.get('d')), 'Backlog');
});

test('a self-dependency stalls materialization with no-progress and creates nothing', async () => {
  const jira = makeFakeJira('PROJ-4', 'PROJ');
  const selfBlocked = proposal('x', 'X', ['x']);

  await assert.rejects(
    () => materializeDecomposition(
      { parentJiraIssueKey: 'PROJ-4', subtasks: [selfBlocked] },
      'proj',
      { jira, assignment: allowAllAssignment }
    ),
    MaterializationNoProgressError
  );

  assert.equal(jira.calls.createSubtaskForProposal, 0);
  assert.equal(jira._subtaskCount(), 0);
  assert.equal(jira.comments.length, 1);
});

test('a two-subtask cycle stalls materialization with no-progress and creates nothing', async () => {
  const jira = makeFakeJira('PROJ-5', 'PROJ');
  const a = proposal('a', 'A', ['b']);
  const b = proposal('b', 'B', ['a']);

  await assert.rejects(
    () => materializeDecomposition(
      { parentJiraIssueKey: 'PROJ-5', subtasks: [a, b] },
      'proj',
      { jira, assignment: allowAllAssignment }
    ),
    MaterializationNoProgressError
  );

  assert.equal(jira.calls.createSubtaskForProposal, 0);
});

test('a missing blocker UUID stalls only the subtask that references it', async () => {
  const jira = makeFakeJira('PROJ-6', 'PROJ');
  const orphan = proposal('o', 'Orphan', ['does-not-exist']);
  const fine = proposal('f', 'Fine', []);

  await assert.rejects(
    () => materializeDecomposition(
      { parentJiraIssueKey: 'PROJ-6', subtasks: [orphan, fine] },
      'proj',
      { jira, assignment: allowAllAssignment }
    ),
    err => {
      assert.ok(err instanceof MaterializationNoProgressError);
      assert.equal(err.unresolved.length, 1);
      assert.equal(err.unresolved[0].id, 'o');
      return true;
    }
  );

  // The independent subtask still has no-progress semantics applied to the
  // whole batch — spec requires a mechanical, un-interpreted report, and
  // the fine subtask *does* get created since it makes progress every pass;
  // only "o" is left unresolved. Confirm "fine" was materialized.
  assert.equal(jira.calls.createSubtaskForProposal, 1);
});

test('rejects the whole decomposition and creates nothing when assignment validation fails', async () => {
  const jira = makeFakeJira('PROJ-7', 'PROJ');
  const rejecting = {
    validateDecomposition: () => ({
      ok: false,
      rejected: [{ subtaskId: 'x', displayName: 'X', requestedAgent: 'nonexistent-agent' }],
      permittedAgents: ['backend-agent'],
    }),
  };

  await assert.rejects(
    () => materializeDecomposition(
      { parentJiraIssueKey: 'PROJ-7', subtasks: [proposal('x', 'X', [], 'nonexistent-agent')] },
      'proj',
      { jira, assignment: rejecting }
    ),
    MaterializationValidationError
  );

  assert.equal(jira.calls.createSubtaskForProposal, 0);
  assert.equal(jira.comments.length, 1);
});

test('fails closed when the assignment module is unavailable (explicit null override)', async () => {
  const jira = makeFakeJira('PROJ-8', 'PROJ');

  // services/scrummaster/src/assignment.js now exists,
  // so the default deps.assignment resolves to the real module — this
  // exercises the fail-closed branch directly by overriding it to null,
  // simulating the module being absent, rather than skipping catalog
  // validation silently.
  await assert.rejects(
    () => materializeDecomposition(
      { parentJiraIssueKey: 'PROJ-8', subtasks: [proposal('x', 'X')] },
      'proj',
      { jira, assignment: null }
    ),
    /assignment\.js is not available/
  );

  assert.equal(jira.calls.createSubtaskForProposal, 0);
});

test('integrates with the real services/scrummaster/src/assignment.js against the fixture catalog', async () => {
  // No `assignment` override — exercises the real lazy require('./assignment'),
  // against the real registry.js and the fixture agents.json/projects.json
  // shipped in services/scrummaster/config.
  process.env.AGENTS_CATALOG_PATH = require('node:path').join(__dirname, '..', 'config', 'agents.json');
  process.env.PROJECTS_CONFIG_PATH = require('node:path').join(__dirname, '..', 'config', 'projects.json');

  const jira = makeFakeJira('HW-1', 'HW');
  const backend = proposal('b1', 'Backend API', [], 'backend-agent');
  const frontend = proposal('f1', 'Frontend screen', ['b1'], 'frontend-agent');

  const { idToKey } = await materializeDecomposition(
    { parentJiraIssueKey: 'HW-1', subtasks: [backend, frontend] },
    'hello-world', // must match a project name in config/projects.json
    { jira }
  );

  assert.equal(jira._status(idToKey.get('b1')), 'Shovel Ready');
  assert.equal(jira._status(idToKey.get('f1')), 'Backlog');

  // An agent id not permitted for this project must reject the whole batch —
  // the real assignment.js atomic-reject behavior, not the test double's.
  const jira2 = makeFakeJira('HW-2', 'HW');
  await assert.rejects(
    () => materializeDecomposition(
      { parentJiraIssueKey: 'HW-2', subtasks: [proposal('x', 'X', [], 'nonexistent-agent')] },
      'hello-world',
      { jira: jira2 }
    ),
    MaterializationValidationError
  );
  assert.equal(jira2.calls.createSubtaskForProposal, 0);
});

test('redelivering the same decomposition is idempotent — no duplicate subtasks or links', async () => {
  const jira = makeFakeJira('PROJ-9', 'PROJ');
  const message = {
    parentJiraIssueKey: 'PROJ-9',
    subtasks: [proposal('b1', 'Backend'), proposal('f1', 'Frontend', ['b1'])],
  };

  const first = await materializeDecomposition(message, 'proj', { jira, assignment: allowAllAssignment });
  assert.equal(jira.calls.createSubtaskForProposal, 2);
  assert.equal(jira.calls.createIssueLink, 1);

  const second = await materializeDecomposition(message, 'proj', { jira, assignment: allowAllAssignment });
  assert.equal(jira.calls.createSubtaskForProposal, 2, 'no new subtasks created on redelivery');
  assert.equal(jira.calls.createIssueLink, 1, 'no duplicate link created on redelivery');
  assert.equal(second.idToKey.get('b1'), first.idToKey.get('b1'));
  assert.equal(second.idToKey.get('f1'), first.idToKey.get('f1'));
});

test('Done Handler releases a single dependent once its only blocker is Done', async () => {
  const jira = makeFakeJira('PROJ-10', 'PROJ');
  jira._addSubtask('PROJ-101', { status: 'Done' });
  jira._addSubtask('PROJ-102', { status: 'Backlog' });
  jira._addLink('PROJ-101', 'PROJ-102');

  await handleSubtaskDone('PROJ-101', { jira });

  assert.equal(jira._status('PROJ-102'), 'Shovel Ready');
});

test('Done Handler releases multiple dependents from one blocker', async () => {
  const jira = makeFakeJira('PROJ-11', 'PROJ');
  jira._addSubtask('PROJ-111', { status: 'Done' });
  jira._addSubtask('PROJ-112', { status: 'Backlog' });
  jira._addSubtask('PROJ-113', { status: 'Backlog' });
  jira._addLink('PROJ-111', 'PROJ-112');
  jira._addLink('PROJ-111', 'PROJ-113');

  await handleSubtaskDone('PROJ-111', { jira });

  assert.equal(jira._status('PROJ-112'), 'Shovel Ready');
  assert.equal(jira._status('PROJ-113'), 'Shovel Ready');
});

test('Done Handler leaves a dependent waiting until every blocker is Done', async () => {
  const jira = makeFakeJira('PROJ-12', 'PROJ');
  jira._addSubtask('PROJ-121', { status: 'Done' }); // A
  jira._addSubtask('PROJ-122', { status: 'In Progress' }); // B, not done yet
  jira._addSubtask('PROJ-123', { status: 'Backlog' }); // C, blocked by A and B
  jira._addLink('PROJ-121', 'PROJ-123');
  jira._addLink('PROJ-122', 'PROJ-123');

  await handleSubtaskDone('PROJ-121', { jira });
  assert.equal(jira._status('PROJ-123'), 'Backlog', 'still waiting on B');

  jira._addSubtask('PROJ-122', { status: 'Done' }); // B completes
  await handleSubtaskDone('PROJ-122', { jira });
  assert.equal(jira._status('PROJ-123'), 'Shovel Ready');
});

test('Done Handler is idempotent under a replayed or out-of-order event', async () => {
  const jira = makeFakeJira('PROJ-13', 'PROJ');
  jira._addSubtask('PROJ-131', { status: 'Done' });
  jira._addSubtask('PROJ-132', { status: 'Backlog' });
  jira._addLink('PROJ-131', 'PROJ-132');

  await handleSubtaskDone('PROJ-131', { jira });
  assert.equal(jira._status('PROJ-132'), 'Shovel Ready');
  const transitionsAfterFirst = jira.calls.transitionIssue.length;

  // Replay of the same Done webhook — the candidate is no longer in Backlog,
  // so this must be a no-op rather than dispatching or transitioning again.
  await handleSubtaskDone('PROJ-131', { jira });
  assert.equal(jira.calls.transitionIssue.length, transitionsAfterFirst);
});

test('Done Handler ignores unrelated issue links', async () => {
  const jira = makeFakeJira('PROJ-14', 'PROJ');
  jira._addSubtask('PROJ-141', { status: 'Done' });
  jira._addSubtask('PROJ-142', { status: 'Backlog' });
  // No Blocks-type link between them — Done Handler must not touch PROJ-142.
  await handleSubtaskDone('PROJ-141', { jira });
  assert.equal(jira._status('PROJ-142'), 'Backlog');
});

// --- routeMaterialization (mode-aware routing) ---
// A Jira-mode project must go through the EXACT existing
// materializeDecomposition path above, unmodified — these tests assert that
// by reusing the same fakeJira harness the Jira-mode tests above already
// use, not a separate mock.

function makeFakeCanonicalWorkItems(mode) {
  const publishedCommands = [];
  return {
    publishedCommands,
    async getMode() { return { mode }; },
    async publishCommand(project, payload) {
      publishedCommands.push({ project, payload });
      return { deduped: false };
    },
  };
}

test('routeMaterialization: a Jira-mode project materializes into Jira via the existing, unmodified path', async () => {
  const jira = makeFakeJira('PROJ-20', 'PROJ');
  const canonicalWorkItems = makeFakeCanonicalWorkItems('jira');

  const message = {
    parentId: 'PROJ-20',
    subtasks: [
      { id: 'p1', displayName: 'Root', description: 'd', agent: 'backend-agent', 'Blocked By': [] },
    ],
  };
  await routeMaterialization(message, 'test-project', { jira, canonicalWorkItems, assignment: allowAllAssignment });

  assert.equal(jira.calls.createSubtaskForProposal, 1, 'Jira mode must still create the Jira subtask directly');
  assert.equal(canonicalWorkItems.publishedCommands.length, 0, 'no canonical command is published in Jira mode');
});

test('routeMaterialization: a local-mode project publishes a materializeDecomposition command instead of touching Jira', async () => {
  const jira = makeFakeJira('PROJ-21', 'PROJ');
  const canonicalWorkItems = makeFakeCanonicalWorkItems('local');

  const message = {
    parentId: 'a-canonical-parent-id',
    subtasks: [
      { id: 'p1', displayName: 'Root', description: 'd', agent: 'backend-agent', 'Blocked By': [] },
    ],
  };
  await routeMaterialization(message, 'test-project', { jira, canonicalWorkItems });

  assert.equal(jira.calls.createSubtaskForProposal, 0, 'local mode must never touch Jira');
  assert.equal(canonicalWorkItems.publishedCommands.length, 1);
  const published = canonicalWorkItems.publishedCommands[0];
  assert.equal(published.payload.command, 'materializeDecomposition');
  assert.equal(published.payload.message.parentWorkItemId, 'a-canonical-parent-id');
  assert.deepEqual(published.payload.message.subtasks, message.subtasks);
});
