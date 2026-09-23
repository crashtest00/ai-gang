'use strict';

// v4.1 agent-artifact-automation.md REQ-04 — the shared A2A instructions
// builder names a work item's specification link and artifact ids by
// canonical id, and the create_subtask operation row documents the two
// optional reference fields beside the three required ones. Driven
// against buildTaskPrompt/buildUnblockPrompt/buildRetryPrompt directly
// (the real prompt-building entry points every dispatch/continuation/retry
// uses), not a reimplementation of buildA2AInstructions.

const test = require('node:test');
const assert = require('node:assert/strict');

const { buildTaskPrompt, buildUnblockPrompt, buildRetryPrompt } = require('./prompt');

const AGENT = { displayName: 'Backend Agent', definitionPath: '/agent-docs/backend-agent.md' };
const TASK = { id: 'task-1', contextId: 'ctx-1' };
const MESSAGE = { messageId: 'msg-1' };

function baseIssue(overrides = {}) {
  return {
    key: 'WI-1', summary: 'Do the thing', projectName: 'hello-world', parent: null,
    comments: [], ...overrides,
  };
}

test('buildTaskPrompt names the specification link and artifact ids by canonical id when present', () => {
  const issue = baseIssue({
    specificationLink: { artifactId: 'art-spec-1', requirementId: 'REQ-7' },
    artifactLinks: ['art-1', 'art-2'],
  });

  const prompt = buildTaskPrompt(issue, AGENT, { task: TASK, message: MESSAGE });

  assert.match(prompt, /Specification link: art-spec-1 \(REQ-7\)/);
  assert.match(prompt, /Artifact links: art-1, art-2/);
});

test('buildTaskPrompt says so, rather than omitting the lines, when there are no references', () => {
  const issue = baseIssue();

  const prompt = buildTaskPrompt(issue, AGENT, { task: TASK, message: MESSAGE });

  assert.match(prompt, /Specification link: none/);
  assert.match(prompt, /Artifact links: none/);
});

test('an empty artifactLinks array reads the same as absent', () => {
  const issue = baseIssue({ specificationLink: null, artifactLinks: [] });

  const prompt = buildTaskPrompt(issue, AGENT, { task: TASK, message: MESSAGE });

  assert.match(prompt, /Artifact links: none/);
});

test('every reference line names a canonical id only — no delivered path, no tracker key', () => {
  const issue = baseIssue({
    specificationLink: { artifactId: 'art-spec-1', requirementId: 'REQ-7' },
    artifactLinks: ['art-1'],
  });

  const prompt = buildTaskPrompt(issue, AGENT, { task: TASK, message: MESSAGE });
  const specLine = prompt.split('\n').find(line => line.startsWith('Specification link:'));
  const artifactLine = prompt.split('\n').find(line => line.startsWith('Artifact links:'));

  assert.doesNotMatch(specLine, /\/workspace|\.\//, 'must not name a delivered path');
  assert.doesNotMatch(artifactLine, /\/workspace|\.\//, 'must not name a delivered path');
  // The pre-existing tracker-labelled key line is untouched and unrelated
  // to these two new lines (spec REQ-04's acceptance).
  assert.match(prompt, /Jira issue key: WI-1/);
});

test('buildUnblockPrompt and buildRetryPrompt render the same reference lines — one shared builder', () => {
  const issue = baseIssue({
    specificationLink: { artifactId: 'art-spec-2', requirementId: 'REQ-3' },
    artifactLinks: ['art-9'],
  });

  const unblock = buildUnblockPrompt(issue, AGENT, TASK, MESSAGE, null);
  const retry = buildRetryPrompt(issue, AGENT, { kind: 'human_rework' }, TASK, MESSAGE);

  for (const prompt of [unblock, retry]) {
    assert.match(prompt, /Specification link: art-spec-2 \(REQ-3\)/);
    assert.match(prompt, /Artifact links: art-9/);
  }
});

test('the create_subtask operation row documents the two optional reference fields beside the three required ones', () => {
  const issue = baseIssue();
  const prompt = buildTaskPrompt(issue, AGENT, { task: TASK, message: MESSAGE });

  const row = prompt.split('\n').find(line => line.includes('| create_subtask '));
  assert.ok(row, 'the operations table must have a create_subtask row');
  assert.match(row, /specificationLink/);
  assert.match(row, /artifactLinks/);
  assert.match(row, /optional/);
  assert.doesNotMatch(row, /All three fields are required/, 'the row must no longer claim only three fields exist');
  assert.match(row, /summary, description and agentFieldValue are required/);
});
