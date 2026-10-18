'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');

const { routeMaterialization } = require('./dependencies');

// From v5.1 dependencies.js forwards a decomposition to `core` and nothing
// else: there is no Jira-mode branch and no local materializer, so the whole
// of its behaviour is the one canonical command it publishes. The
// order-independent, atomic-rejection algorithm that used to live here lives
// in core's materialize.py, covered by services/core/tests/test_materialize.py.

function makeFakeCanonicalWorkItems(mode) {
  const publishedCommands = [];
  let modeReads = 0;
  return {
    publishedCommands,
    get modeReads() { return modeReads; },
    async getMode() { modeReads += 1; return { mode }; },
    async publishCommand(project, payload) {
      publishedCommands.push({ project, payload });
      return { deduped: false };
    },
  };
}

test('routeMaterialization publishes one canonical materializeDecomposition command', async () => {
  const canonicalWorkItems = makeFakeCanonicalWorkItems('local');

  const message = {
    parentId: 'a-canonical-parent-id',
    subtasks: [
      { id: 'p1', displayName: 'Root', description: 'd', agent: 'backend-agent', 'Blocked By': [] },
      { id: 'p2', displayName: 'Dependent', description: 'd', agent: 'frontend-agent', 'Blocked By': ['p1'] },
    ],
  };
  await routeMaterialization(message, 'test-project', { canonicalWorkItems });

  assert.equal(canonicalWorkItems.publishedCommands.length, 1);
  const published = canonicalWorkItems.publishedCommands[0];
  assert.equal(published.project, 'test-project');
  assert.equal(published.payload.command, 'materializeDecomposition');
  assert.equal(published.payload.actor, 'refinement-agent');
  assert.equal(published.payload.message.parentWorkItemId, 'a-canonical-parent-id');
  assert.deepEqual(published.payload.message.subtasks, message.subtasks);
});

// REQ-06 — routeMaterialization contains no mode branch. A project in Jira
// mode gets the same single canonical command, carrying the parent's canonical
// id: core's write gate refuses it and dead-letters it as WRITE_GATE_REJECTED,
// and executing it against Jira is v5.2's. The project's mode is not even read.
test('routeMaterialization publishes the same command for a project in Jira mode, and reads no mode', async () => {
  const canonicalWorkItems = makeFakeCanonicalWorkItems('jira');

  const message = {
    parentId: 'a-canonical-parent-id',
    subtasks: [{ id: 'p1', displayName: 'Root', description: 'd', agent: 'backend-agent', 'Blocked By': [] }],
  };
  await routeMaterialization(message, 'test-project', { canonicalWorkItems });

  assert.equal(canonicalWorkItems.modeReads, 0, 'no mode branch means no mode read');
  assert.equal(canonicalWorkItems.publishedCommands.length, 1);
  const published = canonicalWorkItems.publishedCommands[0];
  assert.equal(published.payload.command, 'materializeDecomposition');
  assert.equal(published.payload.message.parentWorkItemId, 'a-canonical-parent-id');
});
