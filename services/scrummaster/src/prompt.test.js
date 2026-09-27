'use strict';

// v4.1 agent-artifact-automation.md REQ-04 — the shared references builder
// names a work item's specification link and artifact ids by canonical id,
// under the one heading that survives V5.0's deletion of the instruction and
// task-context blocks (deterministic-gateway-message-tooling.md REQ-04).
// Driven against buildTaskPrompt/buildUnblockPrompt/buildRetryPrompt directly
// (the real prompt-building entry points every dispatch/continuation/retry
// uses), not a reimplementation of the builder they share.
//
// The create_subtask request shape an agent is shown is no longer in any
// prompt, so V4.1 REQ-01's contract for it is asserted where that shape now
// lives — the a2a-submit skill and the constructor — in gateway.test.js.

const test = require('node:test');
const assert = require('node:assert/strict');

const { buildTaskPrompt, buildUnblockPrompt, buildRetryPrompt } = require('./prompt');

const AGENT = { displayName: 'Backend Agent', definitionPath: '/agent-docs/agents/backend-agent.md' };
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

// REQ-04's acceptance, on the three real builders: the block an agent was
// told to author a submission from is gone, the block of ids it was told to
// transcribe is gone, and the work item's three references survive as a block
// of their own.
//
// `buildA2AInstructions` is deliberately not imported: it no longer exists,
// and these assertions are about what the builders every dispatch calls
// actually render.

// The search REQ-04 is accepted by (`REQ-04/message-terms`) reads these terms
// out of prompt.js itself. Asserting the rendered prompt against the same
// terms is the runtime half of that: a term reintroduced through a template
// literal, a helper or a future field fails here as well as in the search.
const MESSAGE_TERMS = [
  '"state"', '"messageId"', 'referenceMessageId', '"parts"', '"operation"',
  '"artifacts"', 'text Part', 'data Part', 'submission shape',
  'submission example', 'Redis Message Contract', 'Gateway Message Reference',
  'A2A TASK CONTEXT', 'Last Message ID', 'Task ID', 'Context ID',
];

const ALLOWED_AGENTS = [
  { id: 'backend-agent', agentCard: { description: 'Backend implementation' } },
];

function everyBuilder(issue) {
  return {
    buildTaskPrompt: buildTaskPrompt(issue, AGENT, { allowedAgents: ALLOWED_AGENTS, task: TASK, message: MESSAGE }),
    buildUnblockPrompt: buildUnblockPrompt(issue, AGENT, TASK, MESSAGE, null),
    buildRetryPrompt: buildRetryPrompt(issue, AGENT, { kind: 'human_rework' }, TASK, MESSAGE),
  };
}

test('no builder renders the deleted instruction or task-context block', () => {
  for (const [name, prompt] of Object.entries(everyBuilder(baseIssue()))) {
    assert.doesNotMatch(prompt, /## INSTRUCTIONS/, `${name} must not render an instruction block`);
    assert.doesNotMatch(prompt, /## A2A TASK CONTEXT/, `${name} must not render a task-context block`);
  }
});

test('no builder renders a term of a submission, or a submission an agent would author by hand', () => {
  const issue = baseIssue({
    specificationLink: { artifactId: 'art-spec-1', requirementId: 'REQ-7' },
    artifactLinks: ['art-1'],
  });

  for (const [name, prompt] of Object.entries(everyBuilder(issue))) {
    for (const term of MESSAGE_TERMS) {
      assert.ok(!prompt.includes(term), `${name} must not render "${term}"`);
    }
    // The patterns of the other search (`REQ-04/hand-authored-file`) are
    // matched by their shape rather than written out: its scope is the whole
    // repository, so a literal here would be a hit of its own.
    assert.doesNotMatch(prompt, /<<\s*'?[A-Z]/, `${name} must not render a heredoc`);
    assert.doesNotMatch(prompt, /\/tmp\//, `${name} must not name a temporary file to publish`);
    assert.doesNotMatch(prompt, /gateway-publish/, `${name} must not name the raw publish entry point`);
  }
});

test('the surviving block is WORK ITEM REFERENCES, and it holds exactly the three reference lines', () => {
  const issue = baseIssue({
    specificationLink: { artifactId: 'art-spec-1', requirementId: 'REQ-7' },
    artifactLinks: ['art-1', 'art-2'],
  });

  for (const [name, prompt] of Object.entries(everyBuilder(issue))) {
    assert.match(prompt, /## WORK ITEM REFERENCES/, `${name} must render the references block`);
    const block = prompt.slice(prompt.indexOf('## WORK ITEM REFERENCES'))
      .split('\n').slice(1).filter(line => line.trim() !== '');
    assert.deepEqual(block, [
      'Jira issue key: WI-1',
      'Specification link: art-spec-1 (REQ-7)',
      'Artifact links: art-1, art-2',
    ], `${name}'s references block must carry those three lines and nothing else`);
  }
});

test('the allowed-agent list survives, on the one builder that is given one', () => {
  const issue = baseIssue();
  const prompts = everyBuilder(issue);

  assert.match(prompts.buildTaskPrompt, /## ALLOWED AGENTS/);
  assert.match(prompts.buildTaskPrompt, /- backend-agent: Backend implementation/);
  for (const name of ['buildUnblockPrompt', 'buildRetryPrompt']) {
    assert.doesNotMatch(prompts[name], /## ALLOWED AGENTS/,
      `${name} is given no allowed-agent set, so it renders no list`);
  }
});

test('a dispatch with no allowed-agent set renders no allowed-agent list', () => {
  const prompt = buildTaskPrompt(baseIssue(), AGENT, { task: TASK, message: MESSAGE });

  assert.doesNotMatch(prompt, /## ALLOWED AGENTS/);
  assert.match(prompt, /## WORK ITEM REFERENCES/);
});
