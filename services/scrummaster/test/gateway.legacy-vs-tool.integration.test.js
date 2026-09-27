'use strict';

/**
 * REQ-05 — valid input is a strict improvement, not a behavior change
 * (V5.0 Deterministic Gateway Message Tooling).
 *
 * For each operation type, the same well-formed submission is made twice
 * against the same fixture and the same real gateway consumer: once the legacy
 * way, a hand-authored `{ state, message, artifacts? }` payload published
 * straight onto the gateway stream, and once through the new constructor,
 * `setup/commons/tools/a2a-submit.js`, run as a real child process with the
 * dispatch environment the subscriber exports. Both rounds are then compared on
 * two things: the Streams entry that reached the gateway, and the gateway-side
 * effect it produced — comments, the Blocked flag, the Agent field,
 * transitions, subtasks created and tasks dispatched.
 *
 * Only identifiers and timestamps are normalised away, because the constructor
 * mints its own and the whole point of it is that no id crosses the agent.
 * Nothing in gateway.js changes for this test: it drives the real
 * startGatewaySubscriber over the same jira.js in-memory fake
 * gateway.integration.test.js installs.
 *
 * The two approved divergences (a `create_subtask` with no `description` and
 * one with no `agentFieldValue`, both accepted by the gateway and both refused
 * at construction — REQ-02's tightenings, READINESS_DECISIONS.md item 4) are
 * excluded from this comparison by construction: the constructor cannot express
 * either, and setup/commons/tools/a2a-submit.test.js asserts each is refused.
 */

const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { execFile } = require('node:child_process');
const { test, before, after, beforeEach } = require('node:test');
const assert = require('node:assert/strict');

process.env.AGENTS_CATALOG_PATH = path.join(__dirname, '..', 'config', 'agents.json');
process.env.PROJECTS_CONFIG_PATH = path.join(__dirname, '..', 'config', 'projects.json');
process.env.REDIS_HOST = process.env.REDIS_TEST_HOST || 'localhost';
process.env.REDIS_PORT = process.env.REDIS_TEST_PORT || '16399';

const jira = require('../src/jira');
const registry = require('../src/registry');
const streams = require('../src/streams');
const canonicalWorkItems = require('../src/canonicalWorkItems');
const taskStore = require('../src/a2a/taskStore');
const { buildMessage, buildTask } = require('../src/a2a/parts');
const { buildEnvelope, KIND, fromStreamFields } = require('../src/envelope');
const gateway = require('../src/gateway');
const redisModule = require('../src/redis');

const PROJECT = 'hello-world';
const TASK_ID = 'HW-1';
const SEED_MESSAGE_ID = 'msg-seed-HW-1';
const AGENT_SUFFIXES = ['backend', 'frontend', 'refinement', 'devops'];

// The constructor, at its commons path, and the node_modules that resolve its
// `redis` require — the commons is mounted into a container that installs it
// globally, and here ScrumMaster's own copy stands in for that.
const CONSTRUCTOR = path.join(__dirname, '..', '..', '..', 'setup', 'commons', 'tools', 'a2a-submit.js');
const NODE_PATH = process.env.NODE_PATH || path.join(__dirname, '..', 'node_modules');

let client;
let dispatchDir;

const fakeJira = {
  issues: new Map(),
  comments: [],
  blocked: new Map(),
  agentField: new Map(),
  transitions: [],
  nextSubtaskSeq: 1,
};

function resetFakeJira() {
  fakeJira.issues.clear();
  fakeJira.comments = [];
  fakeJira.blocked.clear();
  fakeJira.agentField.clear();
  fakeJira.transitions = [];
  fakeJira.nextSubtaskSeq = 1;
  fakeJira.issues.set(TASK_ID, {
    key: TASK_ID, summary: 'Parent story', project: 'HW', projectName: PROJECT,
    comments: [], parent: null,
  });
}

function registerTask() {
  const message = buildMessage({
    messageId: SEED_MESSAGE_ID, taskId: TASK_ID, contextId: TASK_ID, role: 'client',
    parts: [{ kind: 'text', text: 'seed dispatch' }],
  });
  taskStore.register(buildTask({
    id: TASK_ID,
    contextId: TASK_ID,
    status: { state: 'submitted', timestamp: new Date().toISOString(), message },
    metadata: {
      jiraIssueKey: TASK_ID, jiraProjectKey: 'HW', jiraProjectName: PROJECT,
      agentId: 'refinement-agent',
    },
  }));
}

before(async () => {
  await redisModule.connect();
  client = redisModule.getClient();

  jira.postComment = async (key, body) => { fakeJira.comments.push({ key, body }); };
  jira.setBlockedField = async (key, val) => { fakeJira.blocked.set(key, val); };
  jira.setAgentField = async (key, val) => { fakeJira.agentField.set(key, val); };
  jira.transitionIssue = async (key, status) => { fakeJira.transitions.push({ key, status }); };
  jira.getIssue = async (key) => {
    const issue = fakeJira.issues.get(key);
    if (!issue) throw new Error(`fake jira: no such issue ${key}`);
    return { ...issue, comments: issue.comments || [] };
  };
  jira.createSubtask = async (parentKey, projectKey, summary, description, agentFieldValue) => {
    const subtaskKey = `HW-${100 + fakeJira.nextSubtaskSeq++}`;
    fakeJira.issues.set(subtaskKey, {
      key: subtaskKey, summary, description, project: projectKey, projectName: PROJECT,
      parent: parentKey, comments: [], agent: agentFieldValue,
    });
    return subtaskKey;
  };

  // The constructor keeps its chain in the `state/` directory beside the
  // commons snapshot; each round gets a fresh one, so both rounds' submissions
  // are a task's first.
  dispatchDir = fs.mkdtempSync(path.join(os.tmpdir(), 'req05-dispatch-'));
});

after(async () => {
  await client.quit();
  fs.rmSync(dispatchDir, { recursive: true, force: true });
});

beforeEach(async () => {
  await client.flushDb();
  resetFakeJira();
  taskStore._reset();
  canonicalWorkItems.getMode = async () => ({ mode: 'jira' });
});

// --- the two paths ---------------------------------------------------------

async function publishLegacy(payload) {
  const envelope = buildEnvelope({
    kind: KIND.JIRA_OPERATION,
    project: PROJECT,
    taskId: payload.message.taskId,
    contextId: payload.message.contextId,
    payload,
  });
  await streams.publish(client, registry.gatewayStreamName(PROJECT), envelope);
}

function publishThroughConstructor(args) {
  const stateDir = fs.mkdtempSync(path.join(dispatchDir, 'session-'));
  const commonsDir = path.join(stateDir, 'commons');
  fs.mkdirSync(commonsDir);
  fs.mkdirSync(path.join(stateDir, 'state'));

  return new Promise((resolve, reject) => {
    execFile(
      process.execPath,
      [CONSTRUCTOR, ...args],
      {
        env: {
          ...process.env,
          NODE_PATH,
          PROJECT_NAME: PROJECT,
          REDIS_HOST: process.env.REDIS_HOST,
          REDIS_PORT: process.env.REDIS_PORT,
          A2A_TASK_ID: TASK_ID,
          A2A_CONTEXT_ID: TASK_ID,
          A2A_LAST_MESSAGE_ID: SEED_MESSAGE_ID,
          AIGANG_COMMONS_DIR: commonsDir,
        },
      },
      (err, stdout, stderr) => {
        if (err) reject(new Error(`a2a-submit.js failed: ${stderr || stdout}`));
        else resolve();
      }
    );
  });
}

// --- what is compared -----------------------------------------------------

// The gateway stream entry, with everything an id or a clock normalised out.
function normalizeEntry(envelope) {
  const normalizeParts = parts => parts.map(part => (
    part.kind === 'data'
      ? { kind: 'data', data: Object.fromEntries(Object.entries(part.data).sort()) }
      : part
  ));
  return {
    kind: envelope.kind,
    project: envelope.project,
    taskId: envelope.taskId,
    contextId: envelope.contextId,
    payload: {
      state: envelope.payload.state,
      message: {
        topLevelKeys: Object.keys(envelope.payload.message).sort(),
        kind: envelope.payload.message.kind,
        role: envelope.payload.message.role,
        taskId: envelope.payload.message.taskId,
        contextId: envelope.payload.message.contextId,
        chainedToTheDispatch: envelope.payload.message.referenceMessageId === SEED_MESSAGE_ID,
        parts: normalizeParts(envelope.payload.message.parts),
      },
      artifacts: (envelope.payload.artifacts || []).map(artifact => ({
        kind: artifact.kind,
        name: artifact.name,
        taskId: artifact.taskId,
        hasArtifactId: typeof artifact.artifactId === 'string' && artifact.artifactId.length > 0,
        parts: artifact.parts,
      })),
    },
  };
}

async function gatewaySideEffect() {
  const dispatches = [];
  for (const suffix of AGENT_SUFFIXES) {
    const stream = registry.agentStreamName(PROJECT, suffix);
    for (const entry of await client.xRange(stream, '-', '+')) {
      const envelope = fromStreamFields(entry.message);
      dispatches.push({
        suffix, taskId: envelope.taskId, contextId: envelope.contextId, role: envelope.payload.role,
      });
    }
  }
  return {
    comments: fakeJira.comments.map(c => ({ key: c.key, body: c.body })),
    blocked: [...fakeJira.blocked.entries()].sort(),
    agentField: [...fakeJira.agentField.entries()].sort(),
    transitions: fakeJira.transitions,
    issues: [...fakeJira.issues.values()]
      .map(i => ({ key: i.key, summary: i.summary, description: i.description, parent: i.parent, agent: i.agent })),
    dispatches: dispatches.sort((a, b) => `${a.suffix}${a.taskId}`.localeCompare(`${b.suffix}${b.taskId}`)),
    taskState: taskStore.getTaskById(TASK_ID).state,
    failedMessages: taskStore.failedMessageIds(TASK_ID).length,
  };
}

async function waitFor(predicate, description, timeoutMs = 4000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await predicate()) return;
    await new Promise(r => setTimeout(r, 25));
  }
  throw new Error(`timed out waiting for ${description}`);
}

// One round: seed the fixture, publish one submission the given way, let the
// real gateway consumer apply it, and report both the entry and the effect.
async function round(publish, { expectDispatch = false } = {}) {
  await client.flushDb();
  resetFakeJira();
  taskStore._reset();
  registerTask();

  await publish();
  const [entry] = await client.xRange(registry.gatewayStreamName(PROJECT), '-', '+');
  assert.ok(entry, 'the submission should be on the gateway stream');

  await gateway.startGatewaySubscriber();
  try {
    await waitFor(
      async () => (expectDispatch
        ? fakeJira.issues.size > 1
        : fakeJira.comments.length > 0 || fakeJira.agentField.size > 0),
      'the gateway to apply the submission'
    );
    // Let any follow-on step (transition, dispatch) finish too.
    await new Promise(r => setTimeout(r, 250));
  } finally {
    await gateway.stopGatewaySubscriber();
  }

  return { entry: normalizeEntry(fromStreamFields(entry.message)), effect: await gatewaySideEffect() };
}

// --- the comparisons, one per operation type -------------------------------

// The legacy hand-authored message, exactly as the dispatch prompt's template
// taught an agent to write one: a plain object with these six keys and nothing
// else. Not `a2a/parts.js`'s buildMessage, which is ScrumMaster's own
// server-side builder and adds a `timestamp` no agent ever typed.
function message(parts) {
  return {
    kind: 'message',
    messageId: `msg-legacy-${process.hrtime.bigint()}`,
    taskId: TASK_ID,
    contextId: TASK_ID,
    role: 'agent',
    referenceMessageId: SEED_MESSAGE_ID,
    parts,
  };
}

const CASES = [
  {
    name: 'comment, carrying data.reference',
    legacy: {
      state: 'working',
      message: message([
        { kind: 'text', text: 'migration written' },
        { kind: 'data', data: { operation: 'comment', reference: { file: 'db/migrate.sql', function: 'up' } } },
      ]),
    },
    args: ['comment', '--text', 'migration written',
      '--reference-file', 'db/migrate.sql', '--reference-function', 'up'],
  },
  {
    name: 'a plain progress note with no operation, carrying data.reference',
    legacy: {
      state: 'working',
      message: message([
        { kind: 'text', text: 'halfway through the port' },
        { kind: 'data', data: { reference: { file: 'src/app.js' } } },
      ]),
    },
    args: ['progress', '--text', 'halfway through the port', '--reference-file', 'src/app.js'],
  },
  {
    name: 'a blocked submission carrying data.reference',
    legacy: {
      state: 'input-required',
      message: message([
        { kind: 'text', text: 'which column is canonical?' },
        { kind: 'data', data: { reference: { file: 'models.py', function: 'WorkItem' } } },
      ]),
    },
    args: ['input-required', '--text', 'which column is canonical?',
      '--reference-file', 'models.py', '--reference-function', 'WorkItem'],
  },
  {
    name: 'an auth-required submission',
    legacy: {
      state: 'auth-required',
      message: message([{ kind: 'text', text: 'the deploy key is missing' }]),
    },
    args: ['auth-required', '--text', 'the deploy key is missing'],
  },
  {
    name: 'reassign',
    legacy: {
      state: 'working',
      message: message([
        { kind: 'text', text: 'this is frontend work' },
        { kind: 'data', data: { operation: 'reassign', agentFieldValue: 'frontend-agent' } },
      ]),
    },
    args: ['reassign', '--text', 'this is frontend work', '--agent', 'frontend-agent'],
  },
  {
    name: 'create_subtask, with both optional references',
    expectDispatch: true,
    legacy: {
      state: 'working',
      message: message([
        { kind: 'text', text: 'splitting this out' },
        {
          kind: 'data',
          data: {
            operation: 'create_subtask',
            summary: 'Backend Agent: add the endpoint',
            description: 'Expose POST /signup and cover it.',
            agentFieldValue: 'backend-agent',
            specificationLink: { artifactId: 'ART-1', requirementId: 'REQ-03' },
            artifactLinks: ['ART-2', 'ART-3'],
          },
        },
      ]),
    },
    args: ['create-subtask', '--text', 'splitting this out',
      '--summary', 'Backend Agent: add the endpoint',
      '--description', 'Expose POST /signup and cover it.',
      '--agent', 'backend-agent',
      '--specification-artifact', 'ART-1', '--specification-requirement', 'REQ-03',
      '--artifact-link', 'ART-2', '--artifact-link', 'ART-3'],
  },
  {
    name: 'completed with a pull-request artifact',
    legacy: {
      state: 'completed',
      message: message([{ kind: 'text', text: 'PR is open' }]),
      artifacts: [{
        kind: 'artifact',
        artifactId: 'artifact-legacy-1',
        taskId: TASK_ID,
        name: 'pull-request',
        parts: [
          { kind: 'file', file: { name: 'pull-request', mimeType: 'text/uri-list', uri: 'https://example.test/pull/9' } },
          { kind: 'text', text: 'PR is open' },
        ],
      }],
    },
    args: ['completed', '--text', 'PR is open', '--pull-request', 'https://example.test/pull/9'],
  },
  {
    name: 'completed with no artifact',
    legacy: {
      state: 'completed',
      message: message([{ kind: 'text', text: 'all done, nothing to review' }]),
    },
    args: ['completed', '--text', 'all done, nothing to review'],
  },
  {
    name: 'a terminal failure',
    legacy: {
      state: 'failed',
      message: message([{ kind: 'text', text: 'the toolchain is missing' }]),
    },
    args: ['failed', '--text', 'the toolchain is missing'],
  },
];

for (const testCase of CASES) {
  test(`REQ-05: ${testCase.name} — the legacy path and the constructor produce the same entry and the same outcome`, async () => {
    const options = { expectDispatch: testCase.expectDispatch };
    const legacy = await round(() => publishLegacy(testCase.legacy), options);
    const tool = await round(() => publishThroughConstructor(testCase.args), options);

    assert.deepEqual(tool.entry, legacy.entry, 'the Streams entry differs');
    assert.deepEqual(tool.effect, legacy.effect, 'the gateway-side outcome differs');
    assert.equal(tool.effect.failedMessages, 0, 'neither path may record a failed message');
    assert.equal(tool.entry.payload.message.chainedToTheDispatch, true,
      'the constructor chains its first submission to the dispatch message, with no id given to it');
  });
}
