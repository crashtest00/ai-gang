'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { execFile } = require('node:child_process');
const { createClient: createRedisClient } = require('redis');

process.env.AGENTS_CATALOG_PATH = path.join(__dirname, '../config/agents.json');
process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, '../config/projects.json');

const redis = require('./redis');
const registry = require('./registry');
const streams = require('./streams');
const idempotency = require('./idempotency');
const canonicalWorkItems = require('./canonicalWorkItems');
const handlers = require('./handlers');
const taskStore = require('./a2a/taskStore');
const { newMessageId, newArtifactId } = require('./a2a/ids');
const { buildTextPart, buildDataPart, buildMessage, buildTask, buildArtifact } = require('./a2a/parts');
const { buildTaskPrompt } = require('./prompt');
const { fromStreamFields } = require('./envelope');
const { _handleA2ASubmission: handleA2ASubmission, _handlePipelineRetry: handlePipelineRetry, _handleTaskStatus: handleTaskStatus } = require('./gateway');

// The A2A Task id is the work item's canonical id, in every mode (REQ-06).
const WORK_ITEM_ID = 'wi-gateway-42';
const PROJECT_NAME = 'hello-world';

function registerTask(overrides = {}) {
  const contextId = 'ctx-gateway-test';
  const messageId = newMessageId();
  const message = buildMessage({
    messageId, taskId: WORK_ITEM_ID, contextId, role: 'client', parts: [buildTextPart('dispatch prompt')],
  });
  const task = buildTask({
    id: WORK_ITEM_ID,
    contextId,
    status: { state: 'submitted', timestamp: new Date().toISOString(), message },
    metadata: {
      // Display-only residue (RELEASE.md §5). No module resolves anything by it.
      jiraProjectKey: 'GANG',
      projectName: PROJECT_NAME,
      agentId: 'backend-agent',
      ...overrides,
    },
  });
  taskStore.register(task);
  return { contextId, messageId };
}

function envelope({ contextId, referenceMessageId, state, parts, artifacts }) {
  return {
    schemaVersion: '1',
    messageId: newMessageId(),
    kind: 'gateway_operation',
    project: PROJECT_NAME,
    taskId: WORK_ITEM_ID,
    contextId,
    createdAt: new Date().toISOString(),
    payload: {
      state,
      message: buildMessage({
        messageId: newMessageId(),
        taskId: WORK_ITEM_ID,
        contextId,
        role: 'agent',
        parts,
        referenceMessageId,
      }),
      ...(artifacts ? { artifacts } : {}),
    },
  };
}

test.beforeEach(() => taskStore._reset());

// From v5.1 every gateway side effect is a canonical command on core's
// Streams command channel — there is no second destination and no mode branch
// (REQ-04, REQ-05), so this one harness serves every test below. It collects
// the commands published and stubs the one HTTP read the gateway still makes
// (a work item's own persisted status, for reporting only).
function mockCanonicalWrites(t) {
  const calls = [];
  t.mock.method(canonicalWorkItems, 'publishCommand', async (project, payload) => {
    calls.push({ project, payload });
    return { deduped: false };
  });
  t.mock.method(canonicalWorkItems, 'getWorkItem', async (id) => ({ id, status: 'ready' }));
  return calls;
}

test('a working message with no data part is still appended as a plain comment', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'working', parts: [buildTextPart('just a note')] }),
    PROJECT_NAME
  );

  const comment = calls.find(c => c.payload.command === 'appendComment');
  assert.ok(comment);
  assert.match(comment.payload.body, /just a note/);
});

test('auth-required is projected distinctly from input-required', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'auth-required', parts: [buildTextPart('need prod credentials')] }),
    PROJECT_NAME
  );

  const comment = calls.find(c => c.payload.command === 'appendComment');
  assert.match(comment.payload.body, /AUTHORIZATION REQUIRED/);
});

test('an interrupted task can be resumed and later completed without changing its identity', async (t) => {
  mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'input-required', parts: [buildTextPart('need info')] }),
    PROJECT_NAME
  );
  assert.equal(taskStore.getTaskById(WORK_ITEM_ID).state, 'input-required');

  const replyId = taskStore.lastMessage(WORK_ITEM_ID).messageId;
  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: replyId, state: 'completed', parts: [buildTextPart('done after clarification')] }),
    PROJECT_NAME
  );

  const record = taskStore.getTaskById(WORK_ITEM_ID);
  assert.equal(record.id, WORK_ITEM_ID);
  assert.equal(record.contextId, contextId);
  assert.equal(record.state, 'completed');
});

test('completed with no artifact and a summary appends only the closing comment', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'completed', parts: [buildTextPart('Decomposed into 2 subtasks')] }),
    PROJECT_NAME
  );

  assert.deepEqual(calls.map(c => c.payload.command), ['appendComment'],
    'completion appends a comment and moves nothing');
});

test('reassign with no agentFieldValue reports the rejection on the work item instead of dropping silently', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  const outcome = await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [buildTextPart('handing off'), buildDataPart({ operation: 'reassign' })],
    }),
    PROJECT_NAME
  );

  assert.equal(outcome, null);
  assert.equal(calls.some(c => c.payload.command === 'assign'), false);
  const transition = calls.find(c => c.payload.command === 'transitionStatus');
  assert.equal(transition.payload.status, 'needs-clarification',
    'a dropped request must not leave the work item reading as ready to work on');
  const comments = calls.filter(c => c.payload.command === 'appendComment');
  assert.equal(comments.length, 1, 'the work item must carry a visible record of the rejection');
  assert.equal(comments[0].payload.workItemId, WORK_ITEM_ID);
  assert.match(comments[0].payload.body, /agentFieldValue/);
  assert.match(comments[0].payload.body, /backend-agent/, 'the permitted agent ids are named for recovery');
  assert.equal(taskStore.failedMessageIds(WORK_ITEM_ID).length, 1, 'the Task must carry the failure so it cannot log completed');
});

test('a pending create_subtask side effect blocks dependent completion until its successful retry', async (t) => {
  mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });
  let releaseCreate;
  const createReleased = new Promise(resolve => { releaseCreate = resolve; });
  let startedCreate;
  const createStarted = new Promise(resolve => { startedCreate = resolve; });

  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(streams, 'publish', async () => ({ deduped: false, entryId: '0-1' }));
  t.mock.method(idempotency, 'getOutcome', async () => {
    startedCreate();
    await createReleased;
    return undefined;
  });
  t.mock.method(idempotency, 'recordOutcome', async () => {});

  const create = envelope({
    contextId, referenceMessageId: messageId, state: 'working',
    parts: [
      buildTextPart('Creating subtask'),
      buildDataPart({ operation: 'create_subtask', summary: 'Backend: implement endpoint', description: 'full desc', agentFieldValue: 'backend-agent' }),
    ],
  });
  const creating = handleA2ASubmission(create, PROJECT_NAME);
  await createStarted;

  const completion = envelope({
    contextId, referenceMessageId: create.payload.message.messageId, state: 'completed',
    parts: [buildTextPart('Subtask creation complete')],
  });
  await assert.rejects(
    handleA2ASubmission(completion, PROJECT_NAME),
    taskStore.A2ACausalDependencyPendingError
  );
  assert.equal(taskStore.getTaskById(WORK_ITEM_ID).state, 'working');

  releaseCreate();
  await creating;
  await handleA2ASubmission(completion, PROJECT_NAME);

  const record = taskStore.getTaskById(WORK_ITEM_ID);
  assert.equal(record.state, 'completed');
  assert.deepEqual(record.messages.map(message => message.messageId), [
    messageId,
    create.payload.message.messageId,
    completion.payload.message.messageId,
  ]);
});

test('a completed message retries its pending side effect without a terminal guard or duplicate history', async (t) => {
  const { contextId, messageId } = registerTask();
  t.mock.method(canonicalWorkItems, 'getWorkItem', async (id) => ({ id, status: 'ready' }));
  let postAttempts = 0;
  t.mock.method(canonicalWorkItems, 'publishCommand', async () => {
    postAttempts += 1;
    if (postAttempts === 1) throw new Error('temporary core outage');
    return { deduped: false };
  });

  const completion = envelope({
    contextId, referenceMessageId: messageId, state: 'completed',
    parts: [buildTextPart('Finished')],
  });
  await assert.rejects(handleA2ASubmission(completion, PROJECT_NAME), /temporary core outage/);
  assert.equal(taskStore.getTaskById(WORK_ITEM_ID).state, 'completed');

  await handleA2ASubmission(completion, PROJECT_NAME);

  const record = taskStore.getTaskById(WORK_ITEM_ID);
  assert.equal(postAttempts, 2);
  assert.equal(record.state, 'completed');
  assert.equal(
    record.messages.filter(message => message.messageId === completion.payload.message.messageId).length,
    1,
    'the retry reuses the pending message rather than appending it again'
  );
});

test('a stream predecessor for a different task remains an invalid reference', async (t) => {
  const { contextId } = registerTask();
  const foreignPredecessor = envelope({
    contextId: 'ctx-other-task', referenceMessageId: null, state: 'working',
    parts: [buildTextPart('Other task progress')],
  });
  foreignPredecessor.taskId = 'GANG-99';
  foreignPredecessor.payload.message.taskId = 'GANG-99';
  t.mock.method(redis, 'getClient', () => ({
    xRange: async () => [{ message: { data: JSON.stringify(foreignPredecessor) } }],
  }));

  const dependent = envelope({
    contextId,
    referenceMessageId: foreignPredecessor.payload.message.messageId,
    state: 'working',
    parts: [buildTextPart('Must not accept a foreign predecessor')],
  });
  await assert.rejects(
    handleA2ASubmission(dependent, PROJECT_NAME),
    taskStore.A2AReferenceError
  );
  assert.equal(taskStore.getTaskById(WORK_ITEM_ID).messages.length, 1);
});

// Derivation is a recovery, not a router: it may only recover an id the
// request itself already implies. Two agents in the same project answering
// to the same role prefix mean the request implies neither of them, and the
// requesting agent is never a candidate for its own request.

// What an agent is taught about naming a subtask's owner, end to end.
//
// Before V5.0 the dispatch prompt was where an agent learned what to call
// that field, and this test took the name out of the rendered prompt. The
// prompt no longer names any submission field: the a2a-submit skill is the
// one agent-facing description of submitting, and the constructor is what
// turns the argument it teaches into the field the gateway reads
// (deterministic-gateway-message-tooling.md REQ-04). The property is
// unchanged — the name an agent is taught must be the name the gateway reads
// (gateway.js's `agentFieldValue` at :896 and :920) — so the chain under test
// is the whole one: the skill's own text supplies the argument, the real
// constructor runs as a child process and publishes to the real test Redis,
// and the entry that lands on the gateway stream is what this file's own
// gateway entry point then consumes. Nothing is retyped in between, which is
// what makes a rename anywhere in that chain fail here.

const SKILL_PATH = path.join(__dirname, '../../../setup/commons/skills/a2a-submit/SKILL.md');
const CONSTRUCTOR_PATH = path.join(__dirname, '../../../setup/commons/tools/a2a-submit.js');
const TEST_REDIS = {
  host: process.env.REDIS_TEST_HOST || 'localhost',
  port: process.env.REDIS_TEST_PORT || '16399',
};

// The skill's create-subtask arguments, read out of the skill rather than
// restated here: one list entry per argument, each marked required or
// optional and saying what it sets.
function skillCreateSubtaskArguments() {
  const skill = fs.readFileSync(SKILL_PATH, 'utf8');
  const heading = skill.indexOf('## create-subtask');
  assert.ok(heading !== -1, 'the skill must document the create-subtask operation');
  const next = skill.indexOf('\n## ', heading + 1);
  const section = skill.slice(heading, next === -1 ? undefined : next);

  // One entry per list item, continuation lines folded in, so the skill stays
  // free to wrap its prose wherever it reads best.
  const entries = [];
  for (const line of section.split('\n')) {
    const match = line.match(/^- `(--[a-z-]+)`(.*)$/);
    if (match) entries.push({ flag: match[1], text: match[2] });
    else if (entries.length && /^\s+\S/.test(line)) entries[entries.length - 1].text += ` ${line.trim()}`;
  }
  assert.ok(entries.length > 0, 'the skill must list create-subtask\'s arguments');
  return entries;
}

function requiredFlags(entries) {
  return entries.filter(e => e.text.includes('(required)')).map(e => e.flag);
}

// The one optional argument the skill says sets a given field of the subtask.
function optionalFlagsFor(entries, field) {
  const entry = entries.find(e => e.text.includes('(optional') && e.text.includes(field));
  assert.ok(entry, `the skill must document an optional argument that sets ${field}`);
  const flags = [entry.flag, ...[...entry.text.matchAll(/`(--[a-z-]+)`/g)].map(m => m[1])];
  return [...new Set(flags)];
}

// One dispatch's worth of session state: the constructor reads its context
// from the environment the subscriber exports and keeps its chain in a
// `state/` directory beside the commons snapshot.
function dispatchEnv({ contextId, messageId }) {
  const dispatchDir = fs.mkdtempSync(path.join(os.tmpdir(), 'v5-skill-chain-'));
  fs.mkdirSync(path.join(dispatchDir, 'state'));
  return {
    dispatchDir,
    env: {
      ...process.env,
      PROJECT_NAME: PROJECT_NAME,
      REDIS_HOST: TEST_REDIS.host,
      REDIS_PORT: TEST_REDIS.port,
      A2A_TASK_ID: WORK_ITEM_ID,
      A2A_CONTEXT_ID: contextId,
      A2A_LAST_MESSAGE_ID: messageId,
      AIGANG_COMMONS_DIR: path.join(dispatchDir, 'commons'),
      // The constructor is mounted read-only beside `services/`, outside this
      // package, so its own `redis` comes from here.
      NODE_PATH: path.join(__dirname, '../node_modules'),
    },
  };
}

// Runs the real constructor and returns the envelope it published, read back
// off the gateway stream exactly as ScrumMaster's own consumer reads it.
async function submitThroughConstructor(args, { contextId, messageId }) {
  const stream = `aigang:gateway:${PROJECT_NAME}`;
  const { dispatchDir, env } = dispatchEnv({ contextId, messageId });
  const client = createRedisClient({ url: `redis://${TEST_REDIS.host}:${TEST_REDIS.port}` });
  await client.connect();
  try {
    await client.del(stream);
    const run = await new Promise(resolve => {
      execFile(process.execPath, [CONSTRUCTOR_PATH, ...args], { env }, (error, stdout, stderr) =>
        resolve({ code: error ? error.code : 0, stdout, stderr }));
    });
    assert.equal(run.code, 0, `the constructor must publish: ${run.stderr}`);
    const entries = await client.xRange(stream, '-', '+');
    assert.equal(entries.length, 1, 'exactly one message must reach the gateway stream');
    await client.del(stream);
    return fromStreamFields(entries[0].message);
  } finally {
    await client.quit().catch(() => {});
    fs.rmSync(dispatchDir, { recursive: true, force: true });
  }
}

test('the argument the skill teaches for a subtask\'s owner is the field the gateway reads', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  const entries = skillCreateSubtaskArguments();
  const required = requiredFlags(entries);
  assert.deepEqual([...required].sort(), ['--agent', '--description', '--summary'],
    'the skill must document exactly three required arguments for a subtask request');
  // Derived, not named: the required argument that is neither the summary nor
  // the description is the one that carries the owner.
  const ownerFlag = required.find(flag => flag !== '--summary' && flag !== '--description');

  // The prompt must not teach a second name for it. The allowed-agent list is
  // the only place a prompt still speaks about choosing an agent, and after
  // REQ-04 it names ids, never a field of a submission.
  const prompt = buildTaskPrompt(
    { key: WORK_ITEM_ID, summary: 'Parent story', projectName: PROJECT_NAME, parent: null, comments: [] },
    registry.getAgent('refinement-agent'),
    { allowedAgents: registry.getEffectiveAgents(PROJECT_NAME), task: { id: WORK_ITEM_ID, contextId }, message: { messageId } }
  );
  const allowedSection = prompt.slice(prompt.indexOf('## ALLOWED AGENTS'), prompt.indexOf('## WORK ITEM REFERENCES'));
  assert.match(allowedSection, /backend-agent/, 'the allowed-agent list must name the project\'s agent ids');
  assert.doesNotMatch(allowedSection, /agentFieldValue/,
    'the prompt must not name a submission field an agent never writes');

  const envelope = await submitThroughConstructor([
    'create-subtask',
    ownerFlag, 'backend-agent',
    '--summary', 'Backend: implement endpoint',
    '--description', 'full desc',
  ], { contextId, messageId });

  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(idempotency, 'getOutcome', async () => undefined);
  t.mock.method(idempotency, 'recordOutcome', async () => {});

  await handleA2ASubmission(envelope, PROJECT_NAME);

  const materialize = calls.find(c => c.payload.command === 'materializeDecomposition');
  assert.ok(materialize, 'a request built by the constructor must not be refused');
  assert.equal(materialize.payload.message.subtasks[0].agent, 'backend-agent',
    `the gateway must read the agent id the skill's ${ownerFlag} carried`);
  assert.equal(materialize.payload.message.parentWorkItemId, WORK_ITEM_ID);
});

// v4.1 agent-artifact-automation.md REQ-01, a Locked contract: the
// create_subtask request shape an agent is shown documents specificationLink
// and artifactLinks as optional beside the three required fields. That shape
// used to be an operations table in the dispatch prompt
// (prompt.test.js asserted it there); it is the skill's now, so the clause is
// asserted against the skill — and against the canonical command the gateway
// forwards, so "documented as optional" means the two really do travel and
// really are optional, rather than merely being written down.

test('the skill documents a subtask\'s two references as optional, and they travel onto the subtask', async (t) => {
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  const entries = skillCreateSubtaskArguments();
  const specificationFlags = optionalFlagsFor(entries, 'specificationLink');
  const artifactFlags = optionalFlagsFor(entries, 'artifactLinks');
  assert.equal(specificationFlags.length, 2,
    'a specification reference is two arguments, an artifact id and a requirement id');
  assert.equal(artifactFlags.length, 1);

  const envelope = await submitThroughConstructor([
    'create-subtask',
    '--summary', 'Backend: implement endpoint',
    '--description', 'full desc',
    '--agent', 'backend-agent',
    specificationFlags[0], 'art-spec-1',
    specificationFlags[1], 'REQ-7',
    artifactFlags[0], 'art-1',
    artifactFlags[0], 'art-2',
  ], { contextId, messageId });

  let command = null;
  t.mock.method(canonicalWorkItems, 'publishCommand', async (_project, c) => { command = c; });
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(idempotency, 'getOutcome', async () => undefined);
  t.mock.method(idempotency, 'recordOutcome', async () => {});

  await handleA2ASubmission(envelope, PROJECT_NAME);

  const subtask = command.message.subtasks[0];
  assert.deepEqual(subtask.specificationLink, { artifactId: 'art-spec-1', requirementId: 'REQ-7' });
  assert.deepEqual(subtask.artifactLinks, ['art-1', 'art-2']);
  assert.equal(subtask.agent, 'backend-agent');
});

test('a subtask request that leaves both references out is created all the same — they are optional', async (t) => {
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  const envelope = await submitThroughConstructor([
    'create-subtask',
    '--summary', 'Backend: implement endpoint',
    '--description', 'full desc',
    '--agent', 'backend-agent',
  ], { contextId, messageId });

  let command = null;
  t.mock.method(canonicalWorkItems, 'publishCommand', async (_project, c) => { command = c; });
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(idempotency, 'getOutcome', async () => undefined);
  t.mock.method(idempotency, 'recordOutcome', async () => {});

  await handleA2ASubmission(envelope, PROJECT_NAME);

  const subtask = command.message.subtasks[0];
  assert.equal(subtask.specificationLink, undefined);
  assert.equal(subtask.artifactLinks, undefined);
  assert.equal(subtask.displayName, 'Backend: implement endpoint');
});

test('create_subtask does not derive an agent when two of the project\'s agents answer to the same role prefix', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  const backend = registry.getAgent('backend-agent');
  const twin = {
    ...backend,
    id: 'backend-platform-agent',
    displayName: 'Backend Agent',
    routing: { channelSuffix: 'backend-platform' },
  };
  t.mock.method(registry, 'getEffectiveAgents', () => [backend, twin]);

  const outcome = await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({ operation: 'create_subtask', summary: 'Backend: implement endpoint', description: 'full desc' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.equal(outcome, null, 'an ambiguous prefix must not be resolved by picking one');
  assert.equal(calls.some(c => c.payload.command === 'materializeDecomposition'), false);
  const comments = calls.filter(c => c.payload.command === 'appendComment');
  assert.equal(comments.length, 1);
  assert.match(comments[0].payload.body, /agentFieldValue/);
  assert.match(comments[0].payload.body, /no single one of them/);
});

test('create_subtask does not derive the requesting agent back onto its own subtask', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  const outcome = await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({ operation: 'create_subtask', summary: 'Refinement: break this down further', description: 'full desc' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.equal(outcome, null, 'the requester is not a derivation candidate for its own request');
  assert.equal(calls.some(c => c.payload.command === 'materializeDecomposition'), false);
  const comments = calls.filter(c => c.payload.command === 'appendComment');
  assert.equal(comments.length, 1, 'the parent work item must carry a visible record of the rejection');
  assert.match(comments[0].payload.body, /agentFieldValue/);
});

test('create_subtask with no derivable agent appends the rejection comment and materializes nothing', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({ operation: 'create_subtask', summary: 'Add a /health endpoint', description: 'full desc' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.ok(!calls.some(c => c.payload.command === 'materializeDecomposition'));
  const comment = calls.find(c => c.payload.command === 'appendComment');
  assert.ok(comment, 'the parent work item must carry a visible record of the rejection');
  assert.match(comment.payload.body, /agentFieldValue/);
  assert.match(comment.payload.body, /Add a \/health endpoint/);
  const transition = calls.find(c => c.payload.command === 'transitionStatus');
  assert.ok(transition, 'a dropped subtask chain must move the parent, not only comment on it');
  assert.equal(transition.payload.status, 'needs-clarification',
    'the same state the sibling assignment-failure path leaves it in');
});

test('a task whose submission was rejected is reported failed, not completed', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({ operation: 'create_subtask', summary: 'Add a /health endpoint', description: 'full desc' }),
      ],
    }),
    PROJECT_NAME
  );

  const logs = [];
  t.mock.method(console, 'log', (...args) => logs.push(args.join(' ')));

  // The agent's own container exits cleanly and reports success — it cannot
  // see that the gateway rejected what it sent.
  const result = await handleTaskStatus({
    kind: 'task_status',
    messageId: newMessageId(),
    taskId: WORK_ITEM_ID,
    contextId,
    payload: { status: 'completed', ticket_key: WORK_ITEM_ID, agent_name: 'refinement-agent' },
  }, PROJECT_NAME);

  assert.equal(result.status, 'failed');
  assert.equal(taskStore.getTaskById(WORK_ITEM_ID).state, 'failed');
  assert.ok(!logs.some(line => /completed/.test(line)), 'the task must never be logged as completed');
});

test('a task whose chain permanently failed is reported failed, not completed', async (t) => {
  mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  // The rejected create_subtask poisons the rest of the chain: the agent's
  // own completion message references it and is permanently failed.
  const rejected = envelope({
    contextId, referenceMessageId: messageId, state: 'working',
    parts: [
      buildTextPart('Creating subtask'),
      buildDataPart({ operation: 'create_subtask', summary: 'Add a /health endpoint', description: 'full desc' }),
    ],
  });
  await handleA2ASubmission(rejected, PROJECT_NAME);

  await assert.rejects(
    handleA2ASubmission(
      envelope({
        contextId, referenceMessageId: rejected.payload.message.messageId, state: 'completed',
        parts: [buildTextPart('Decomposed into 1 subtask')],
      }),
      PROJECT_NAME
    ),
    taskStore.A2ACausalDependencyFailedError
  );

  const result = await handleTaskStatus({
    kind: 'task_status',
    messageId: newMessageId(),
    taskId: WORK_ITEM_ID,
    contextId,
    payload: { status: 'completed', ticket_key: WORK_ITEM_ID, agent_name: 'refinement-agent' },
  }, PROJECT_NAME);

  assert.equal(result.status, 'failed');
  assert.deepEqual(result.failedMessageIds, [rejected.payload.message.messageId]);
});

test('a task with no failed submission is still reported completed', async (t) => {
  mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'working', parts: [buildTextPart('progress note'), buildDataPart({ operation: 'comment' })] }),
    PROJECT_NAME
  );

  const result = await handleTaskStatus({
    kind: 'task_status',
    messageId: newMessageId(),
    taskId: WORK_ITEM_ID,
    contextId,
    payload: { status: 'completed', ticket_key: WORK_ITEM_ID, agent_name: 'backend-agent' },
  }, PROJECT_NAME);

  assert.deepEqual(result, { taskId: WORK_ITEM_ID, status: 'completed' });
  assert.equal(taskStore.getTaskById(WORK_ITEM_ID).state, 'completed');
});

// A rejection the requesting agent cannot see or usefully resend leaves the
// ticket recoverable only through a fresh dispatch (handlers.dispatchTask's
// controlled-reopen path) — that redispatch must supersede the stale
// failure, not leave the Task reading failed forever once it genuinely
// succeeds.
test('a redispatch supersedes an earlier rejection so the task can complete cleanly afterward', async (t) => {
  mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(streams, 'publish', async () => ({ deduped: false, entryId: '0-1' }));

  const rejected = envelope({
    contextId, referenceMessageId: messageId, state: 'working',
    parts: [
      buildTextPart('Creating subtask'),
      buildDataPart({ operation: 'create_subtask', summary: 'Add a /health endpoint', description: 'full desc' }),
    ],
  });
  await handleA2ASubmission(rejected, PROJECT_NAME);
  assert.deepEqual(taskStore.failedMessageIds(WORK_ITEM_ID), [rejected.payload.message.messageId]);

  // The same reopen mechanism a pipeline-retry/rework/unblock redispatch
  // uses (handlers.dispatchTask), driven through its real entry point.
  await handlers.dispatchTask(
    { key: WORK_ITEM_ID, jiraProjectKey: 'GANG', projectName: PROJECT_NAME },
    { id: 'refinement-agent', routing: { channelSuffix: 'refinement' } },
    { dispatchId: 'retry-1', promptFactory: () => 'retry prompt' }
  );
  assert.deepEqual(taskStore.failedMessageIds(WORK_ITEM_ID), [], 'the redispatch must supersede the earlier rejection');

  const newSeed = taskStore.lastMessage(WORK_ITEM_ID).messageId;
  const completion = envelope({
    contextId, referenceMessageId: newSeed, state: 'completed',
    parts: [buildTextPart('Created the subtask this time')],
  });
  await handleA2ASubmission(completion, PROJECT_NAME);

  const result = await handleTaskStatus({
    kind: 'task_status',
    messageId: newMessageId(),
    taskId: WORK_ITEM_ID,
    contextId,
    payload: { status: 'completed', ticket_key: WORK_ITEM_ID, agent_name: 'refinement-agent' },
  }, PROJECT_NAME);

  assert.deepEqual(result, { taskId: WORK_ITEM_ID, status: 'completed' }, 'a superseding success must not still read failed');
});

test('create_subtask rejection names every missing required field and blocks causal completion', async (t) => {
  mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });
  const warnings = [];
  t.mock.method(console, 'warn', (...args) => warnings.push(args.join(' ')));

  const rejected = envelope({
    contextId,
    referenceMessageId: messageId,
    state: 'working',
    parts: [
      buildTextPart('Creating incomplete subtask'),
      buildDataPart({ operation: 'create_subtask' }),
    ],
  });
  const outcome = await handleA2ASubmission(rejected, PROJECT_NAME);

  assert.equal(outcome, null);
  assert.match(warnings.join('\n'), /summary/);
  assert.match(warnings.join('\n'), /agentFieldValue/);

  const completion = envelope({
    contextId,
    referenceMessageId: rejected.payload.message.messageId,
    state: 'completed',
    parts: [buildTextPart('done despite missing subtask')],
  });
  await assert.rejects(
    handleA2ASubmission(completion, PROJECT_NAME),
    taskStore.A2ACausalDependencyFailedError
  );
  assert.equal(taskStore.getTaskById(WORK_ITEM_ID).state, 'working');
});

// The gateway direction must not accept a reversed (client) role

test('a message with role "client" on the gateway channel is dropped', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  const env = envelope({ contextId, referenceMessageId: messageId, state: 'working', parts: [buildTextPart('spoofed')] });
  env.payload.message.role = 'client';

  await handleA2ASubmission(env, PROJECT_NAME);
  assert.deepEqual(calls, []);
});

// REQ-06 — the cross-check against the Task's own recorded project reads
// `metadata.projectName`, renamed from a tracker-shaped name. It must still
// drop a submission whose Task belongs to another project.
test('a submission whose Task metadata names another project is dropped', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask({ projectName: 'hello-desktop' });

  const outcome = await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [buildTextPart('note'), buildDataPart({ operation: 'comment' })],
    }),
    PROJECT_NAME
  );

  assert.equal(outcome, null);
  assert.deepEqual(calls, [], 'nothing may be written for a Task belonging to another project');
});

// Routing: project identity comes from the channel, never from content

test('an envelope for a different project than the channel is dropped', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'working', parts: [buildTextPart('note'), buildDataPart({ operation: 'comment' })] }),
    'some-other-project'
  );

  assert.deepEqual(calls, []);
});

test('an envelope for an unknown task is dropped', async (t) => {
  const calls = mockCanonicalWrites(t);

  const env = envelope({ contextId: 'ctx-x', referenceMessageId: null, state: 'working', parts: [buildTextPart('note'), buildDataPart({ operation: 'comment' })] });
  env.taskId = 'wi-does-not-exist';
  env.payload.message.taskId = 'wi-does-not-exist';

  await handleA2ASubmission(env, PROJECT_NAME);
  assert.deepEqual(calls, []);
});

test('an invalid submission (bad state) is dropped without writing anything', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  const env = envelope({ contextId, referenceMessageId: messageId, state: 'in-progress', parts: [buildTextPart('note')] });
  await handleA2ASubmission(env, PROJECT_NAME);
  assert.deepEqual(calls, []);
});

// handlePipelineRetry — Jenkins reports a failed build back over the gateway
// channel, naming the affected work item only by the tracker key it found in
// the failed build's branch name. From v5.1 ScrumMaster may not use such a key
// to find anything (REQ-05, REQ-07), so the message is recorded as unresolved
// and nobody is dispatched. Its routing and a2a-validate.js's `pipeline_retry`
// rule are unchanged, so it is still accepted rather than dead-lettered.

test('a pipeline_retry message is logged as unresolved, dispatches nobody and derives no Redis key', async (t) => {
  assert.equal(redis.acquireOnce, undefined, 'acquireOnce must not exist; no Redis key may be derived from a tracker key');
  t.mock.method(redis, 'getClient', () => ({}));
  let publishCalled = false;
  t.mock.method(streams, 'publish', async () => { publishCalled = true; });
  let publishCommandCalled = false;
  t.mock.method(canonicalWorkItems, 'publishCommand', async () => { publishCommandCalled = true; });
  const errors = [];
  t.mock.method(console, 'error', (...args) => errors.push(args.join(' ')));

  const result = await handlePipelineRetry(
    { ticket_key: 'GANG-70', build_url: 'https://jenkins.example.com/job/hello-world/17/', build_number: 17 },
    PROJECT_NAME
  );

  assert.deepEqual(result, { ticket_key: 'GANG-70', unresolved: true });
  assert.equal(publishCalled, false, 'no agent is dispatched');
  assert.equal(publishCommandCalled, false, 'and nothing is written to the canonical store');
  assert.equal(errors.length, 1);
  assert.match(errors[0], /Unresolved pipeline_retry/);
  assert.match(errors[0], /GANG-70/, 'the key and the build are named so an operator can act on it');
  assert.match(errors[0], /17/);
});

test('a pipeline_retry message with no ticket_key is dropped', async (t) => {
  assert.equal(redis.acquireOnce, undefined, 'acquireOnce must not exist; no Redis key may be derived from a tracker key');

  const result = await handlePipelineRetry({ build_url: 'https://jenkins.example.com/job/hello-world/17/' }, PROJECT_NAME);

  assert.equal(result, null);
});

test('comment operation appends a canonical comment instead of posting to Jira', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({ contextId, referenceMessageId: messageId, state: 'working', parts: [buildTextPart('progress note'), buildDataPart({ operation: 'comment' })] }),
    PROJECT_NAME
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].payload.command, 'appendComment');
  assert.equal(calls[0].payload.workItemId, WORK_ITEM_ID);
  assert.match(calls[0].payload.body, /progress note/);
});

test('input-required transitions to needs-clarification and appends a comment', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'input-required',
      parts: [buildTextPart('Which endpoint should doAuth() call?'), buildDataPart({ reference: { file: 'src/auth.py', function: 'doAuth()' } })],
    }),
    PROJECT_NAME
  );

  assert.equal(calls.length, 2);
  const transition = calls.find(c => c.payload.command === 'transitionStatus');
  const comment = calls.find(c => c.payload.command === 'appendComment');
  assert.equal(transition.payload.status, 'needs-clarification');
  assert.match(comment.payload.body, /BLOCKED/);
  assert.equal(comment.payload.referenceFile, 'src/auth.py');
  assert.equal(comment.payload.referenceFunction, 'doAuth()');
});

test('failed/canceled/rejected transition to the mapped terminal status and append a comment', async (t) => {
  const expected = { failed: 'failed', canceled: 'cancelled', rejected: 'cancelled' };
  // Installed once — see the Jira-mode sibling above for why a second
  // mock of the same method in one test outlives it.
  const calls = mockCanonicalWrites(t);
  for (const state of Object.keys(expected)) {
    taskStore._reset();
    calls.length = 0;
    const { contextId, messageId } = registerTask();

    await handleA2ASubmission(
      envelope({ contextId, referenceMessageId: messageId, state, parts: [buildTextPart('agent process crashed')] }),
      PROJECT_NAME
    );

    const transition = calls.find(c => c.payload.command === 'transitionStatus');
    assert.equal(transition.payload.status, expected[state], `${state} must map to ${expected[state]}`);
  }
});

test('completed with a pull-request artifact only appends a comment, no status transition', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  const artifact = buildArtifact({
    artifactId: newArtifactId(),
    taskId: WORK_ITEM_ID,
    name: 'pull-request',
    parts: [
      { kind: 'file', file: { name: 'pull-request', mimeType: 'text/uri-list', uri: 'https://github.com/org/repo/pull/7' } },
      buildTextPart('Implements the endpoint'),
    ],
  });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'completed',
      parts: [buildTextPart('Verified and opened PR')],
      artifacts: [artifact],
    }),
    PROJECT_NAME
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].payload.command, 'appendComment');
  assert.match(calls[0].payload.body, /github\.com\/org\/repo\/pull\/7/);
});

// The gateway's Task state is not the work item's status. Nothing in the
// completion path transitions the item, and in the mode the local flow runs
// in nothing earlier did either — so a log line naming a status has to read
// the persisted one. It used to assert "In Progress" while the admin showed
// the item untouched.

test('the PR-opened log reports the work item\'s persisted status, not an assumed one', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();
  const logs = [];
  t.mock.method(console, 'log', (...args) => logs.push(args.join(' ')));

  const artifact = buildArtifact({
    artifactId: newArtifactId(),
    taskId: WORK_ITEM_ID,
    name: 'pull-request',
    parts: [
      { kind: 'file', file: { name: 'pull-request', mimeType: 'text/uri-list', uri: 'https://github.com/org/repo/pull/7' } },
      buildTextPart('Implements the endpoint'),
    ],
  });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'completed',
      parts: [buildTextPart('Verified and opened PR')],
      artifacts: [artifact],
    }),
    PROJECT_NAME
  );

  const line = logs.find(l => l.includes('PR opened for'));
  assert.ok(line, 'the PR-opened line must still be logged');
  assert.match(line, /is "ready"/, 'it must name the status the work item actually holds');
  assert.doesNotMatch(line, /In Progress/, 'and must not name a status nothing persisted');
  assert.ok(!calls.some(c => c.payload.command === 'transitionStatus'), 'reporting the status must not change it');
});

test('reassign publishes an assign command for a catalog-valid agent', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [buildTextPart('handing off'), buildDataPart({ operation: 'reassign', agentFieldValue: 'frontend-agent' })],
    }),
    PROJECT_NAME
  );

  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0].payload, { command: 'assign', actor: 'Backend Agent', workItemId: WORK_ITEM_ID, agentId: 'frontend-agent' });
});

test('reassign to an unknown agent reports a visible failure instead of publishing assign', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [buildTextPart('handing off'), buildDataPart({ operation: 'reassign', agentFieldValue: 'nonexistent-agent' })],
    }),
    PROJECT_NAME
  );

  assert.ok(!calls.some(c => c.payload.command === 'assign'), 'no assign command must be published');
  const transition = calls.find(c => c.payload.command === 'transitionStatus');
  const comment = calls.find(c => c.payload.command === 'appendComment');
  assert.equal(transition.payload.status, 'needs-clarification');
  assert.match(comment.payload.body, /catalog validation/);
});

test('create_subtask publishes a single-subtask materializeDecomposition command and does not dispatch directly', async (t) => {
  const calls = mockCanonicalWrites(t);
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(idempotency, 'getOutcome', async () => undefined);
  let recorded = null;
  t.mock.method(idempotency, 'recordOutcome', async (_client, _ns, _id, outcome) => { recorded = outcome; });
  let publishToAgentStream = false;
  t.mock.method(streams, 'publish', async () => { publishToAgentStream = true; return { deduped: false }; });

  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask: Backend: implement endpoint'),
        buildDataPart({ operation: 'create_subtask', summary: 'Backend: implement endpoint', description: 'full desc', agentFieldValue: 'backend-agent' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.equal(calls.length, 1);
  assert.equal(calls[0].payload.command, 'materializeDecomposition');
  assert.equal(calls[0].payload.message.parentWorkItemId, WORK_ITEM_ID);
  assert.equal(calls[0].payload.message.subtasks.length, 1);
  const subtask = calls[0].payload.message.subtasks[0];
  assert.equal(subtask.displayName, 'Backend: implement endpoint');
  assert.equal(subtask.agent, 'backend-agent');
  assert.ok(subtask.id, 'a canonical subtask id must be minted');
  assert.equal(recorded, subtask.id, 'the minted id is recorded for idempotent retry');
  assert.equal(publishToAgentStream, false, 'gateway.js must not dispatch directly — dispatch must follow the work_item.status_changed event');
});

test('create_subtask reuses the previously-minted id on a from-scratch retry, without re-publishing', async (t) => {
  const calls = mockCanonicalWrites(t);
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(idempotency, 'getOutcome', async () => 'already-minted-id');
  t.mock.method(idempotency, 'recordOutcome', async () => { throw new Error('must not re-record on a reused outcome'); });

  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  const result = await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({ operation: 'create_subtask', summary: 'x', description: 'y', agentFieldValue: 'backend-agent' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.deepEqual(result, { subtaskId: 'already-minted-id' });
  assert.equal(calls.length, 0, 'materialize.py\'s own store-level idempotency already covers redelivery; no need to re-publish');
});

test('create_subtask without agentFieldValue derives the agent from the summary role prefix', async (t) => {
  const calls = mockCanonicalWrites(t);
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(idempotency, 'getOutcome', async () => undefined);
  t.mock.method(idempotency, 'recordOutcome', async () => {});

  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask: Backend: implement endpoint'),
        buildDataPart({ operation: 'create_subtask', summary: 'Backend: implement endpoint', description: 'full desc' }),
      ],
    }),
    PROJECT_NAME
  );

  const materialize = calls.find(c => c.payload.command === 'materializeDecomposition');
  assert.ok(materialize, 'a derivable request must be materialized, not reported as rejected');
  assert.equal(materialize.payload.message.subtasks[0].agent, 'backend-agent',
    'the omitted agent id is derived from the "Backend:" prefix in the mode the local flow runs in');
  assert.ok(!calls.some(c => c.payload.command === 'transitionStatus'), 'nothing was refused, so nothing is flagged');
});

// v4.1 agent-artifact-automation.md REQ-01 — the create_subtask request's
// optional specificationLink/artifactLinks ride, unchanged, inside the
// canonical materializeDecomposition command's subtasks entry.

test('create_subtask forwards a well-formed specificationLink and artifactLinks onto the materialized subtask', async (t) => {
  const calls = mockCanonicalWrites(t);
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(idempotency, 'getOutcome', async () => undefined);
  t.mock.method(idempotency, 'recordOutcome', async () => {});

  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask: Backend: implement endpoint'),
        buildDataPart({
          operation: 'create_subtask',
          summary: 'Backend: implement endpoint',
          description: 'full desc',
          agentFieldValue: 'backend-agent',
          specificationLink: { artifactId: 'artifact-1', requirementId: 'REQ-9' },
          artifactLinks: ['artifact-2', 'artifact-3'],
        }),
      ],
    }),
    PROJECT_NAME
  );

  const materialize = calls.find(c => c.payload.command === 'materializeDecomposition');
  assert.ok(materialize, 'a well-formed reference must not block materialization');
  const subtask = materialize.payload.message.subtasks[0];
  assert.deepEqual(subtask.specificationLink, { artifactId: 'artifact-1', requirementId: 'REQ-9' });
  assert.deepEqual(subtask.artifactLinks, ['artifact-2', 'artifact-3']);
});

test('create_subtask with neither reference materializes a subtask entry carrying neither key', async (t) => {
  const calls = mockCanonicalWrites(t);
  t.mock.method(redis, 'getClient', () => ({}));
  t.mock.method(idempotency, 'getOutcome', async () => undefined);
  t.mock.method(idempotency, 'recordOutcome', async () => {});

  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask: Backend: implement endpoint'),
        buildDataPart({
          operation: 'create_subtask', summary: 'Backend: implement endpoint', description: 'full desc', agentFieldValue: 'backend-agent',
        }),
      ],
    }),
    PROJECT_NAME
  );

  const materialize = calls.find(c => c.payload.command === 'materializeDecomposition');
  const subtask = materialize.payload.message.subtasks[0];
  assert.ok(!('specificationLink' in subtask), 'an unreferenced subtask entry must carry no specificationLink key at all');
  assert.ok(!('artifactLinks' in subtask), 'an unreferenced subtask entry must carry no artifactLinks key at all');
});

test('create_subtask with a malformed specificationLink rejects the whole request, naming the field as refused rather than missing', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  const outcome = await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({
          operation: 'create_subtask',
          summary: 'Backend: implement endpoint',
          description: 'full desc',
          agentFieldValue: 'backend-agent',
          specificationLink: { artifactId: 'artifact-1' }, // requirementId missing — malformed, not absent
        }),
      ],
    }),
    PROJECT_NAME
  );

  assert.equal(outcome, null);
  assert.ok(!calls.some(c => c.payload.command === 'materializeDecomposition'), 'a malformed reference must create nothing');
  const comment = calls.find(c => c.payload.command === 'appendComment');
  assert.ok(comment, 'the parent must carry a visible record of the rejection');
  assert.match(comment.payload.body, /specificationLink/);
  assert.match(comment.payload.body, /present, but not an object/, 'the field is present, so the comment must say why it was refused, not that it is missing');
  const transition = calls.find(c => c.payload.command === 'transitionStatus');
  assert.equal(transition.payload.status, 'needs-clarification');
});

test('create_subtask with a malformed artifactLinks rejects the whole request and creates nothing', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  const outcome = await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({
          operation: 'create_subtask',
          summary: 'Backend: implement endpoint',
          description: 'full desc',
          agentFieldValue: 'backend-agent',
          artifactLinks: ['artifact-1', 42], // not all strings — malformed
        }),
      ],
    }),
    PROJECT_NAME
  );

  assert.equal(outcome, null);
  assert.ok(!calls.some(c => c.payload.command === 'materializeDecomposition'));
  const comment = calls.find(c => c.payload.command === 'appendComment');
  assert.match(comment.payload.body, /artifactLinks/);
});

test('create_subtask with an invalid agent reports a visible failure and publishes nothing', async (t) => {
  const calls = mockCanonicalWrites(t);

  const { contextId, messageId } = registerTask({ agentId: 'refinement-agent' });

  await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [
        buildTextPart('Creating subtask'),
        buildDataPart({ operation: 'create_subtask', summary: 'x', description: 'y', agentFieldValue: 'nonexistent-agent' }),
      ],
    }),
    PROJECT_NAME
  );

  assert.ok(!calls.some(c => c.payload.command === 'materializeDecomposition'));
  const transition = calls.find(c => c.payload.command === 'transitionStatus');
  assert.equal(transition.payload.status, 'needs-clarification');
});

// A request naming an operation the gateway has no handler for used to be
// logged and dropped: the agent believed it had asked for something, the
// work item recorded nothing, and the only trace was a container log line.

test('an operation the gateway cannot carry out is appended and flagged on the work item', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId, messageId } = registerTask();

  const outcome = await handleA2ASubmission(
    envelope({
      contextId, referenceMessageId: messageId, state: 'working',
      parts: [buildTextPart('promoting'), buildDataPart({ operation: 'promote_to_release' })],
    }),
    PROJECT_NAME
  );

  assert.equal(outcome, null);
  const comment = calls.find(c => c.payload.command === 'appendComment');
  const transition = calls.find(c => c.payload.command === 'transitionStatus');
  assert.ok(comment, 'the work item must carry a visible record of the refused request');
  assert.match(comment.payload.body, /promote_to_release/);
  assert.equal(transition.payload.status, 'needs-clarification');
});

// handleTaskStatus (execution-outcome signal) — a
// container's subscriber wrapper reporting retry exhaustion, exercised
// against both modes since it has its own mode branch independent of
// handleA2ASubmission's.

test('handleTaskStatus (local mode): failed transitions the work item to failed and appends a comment', async (t) => {
  const calls = mockCanonicalWrites(t);
  const { contextId } = registerTask();

  await handleTaskStatus({
    kind: 'task_status',
    messageId: newMessageId(),
    taskId: WORK_ITEM_ID,
    contextId,
    payload: { status: 'failed', ticket_key: WORK_ITEM_ID, agent_name: 'backend-agent', reason: 'timeout' },
  }, PROJECT_NAME);

  assert.equal(calls.length, 2);
  const transition = calls.find(c => c.payload.command === 'transitionStatus');
  const comment = calls.find(c => c.payload.command === 'appendComment');
  assert.equal(transition.payload.status, 'failed');
  assert.match(comment.payload.body, /timeout/);
});
