'use strict';

/**
 * The constructor entry point — an agent's path
 * (V5.0 Deterministic Gateway Message Tooling REQ-01, REQ-02, REQ-03;
 * Agent Commons REQ-03's "builds a correctly addressed and chained submission
 * with no id passed as an argument").
 *
 * Every case runs `a2a-submit.js` as a real child process, from a copy of the
 * commons laid out the way a dispatch lays it out —
 * `<dispatch>/commons/tools/` beside an empty `<dispatch>/state/` — with the
 * seven environment variables the subscriber exports, against the real test
 * Redis (redis://localhost:16399, services/core/docker-compose.test.yml; start
 * it first if it is not up, never `down` it). No internal function is called
 * in the tool's place, and what is asserted is the entry that reached the
 * gateway stream.
 *
 * Run through setup/commons/tools/test.sh, which holds
 * /tmp/v4-wis-suite.lock and sets NODE_PATH.
 */

const { test, before, beforeEach, after } = require('node:test');
const assert = require('node:assert/strict');
const { execFile } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createClient } = require('redis');
const { fromStreamFields } = require('./envelope');

const REDIS_URL = process.env.REDIS_TEST_URL || 'redis://localhost:16399';
const PROJECT = 'v5-constructor-test';
const STREAM = `aigang:gateway:${PROJECT}`;
const TASK_ID = 'HW-1';
const CONTEXT_ID = 'HW-CTX-1';
const DISPATCH_MESSAGE_ID = 'msg-dispatch-abc';

let client;
let root;

before(async () => {
  client = createClient({ url: REDIS_URL });
  await client.connect();
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'a2a-submit-test-'));
});

beforeEach(async () => {
  await client.del(STREAM);
});

after(async () => {
  await client.del(STREAM);
  await client.quit();
  fs.rmSync(root, { recursive: true, force: true });
});

/**
 * One dispatch's layout: the commons snapshot, and the empty `state/`
 * directory the subscriber creates beside it. Each call is a fresh session, so
 * the chain starts empty — which is what the chain tests need.
 */
function newDispatch() {
  const dispatchDir = fs.mkdtempSync(path.join(root, 'dispatch-'));
  const toolsDir = path.join(dispatchDir, 'commons', 'tools');
  fs.cpSync(__dirname, toolsDir, {
    recursive: true,
    filter: src => !src.endsWith('.test.js'),
  });
  fs.mkdirSync(path.join(dispatchDir, 'state'));
  return {
    dispatchDir,
    commonsDir: path.join(dispatchDir, 'commons'),
    tool: path.join(toolsDir, 'a2a-submit.js'),
    stateDir: path.join(dispatchDir, 'state'),
  };
}

function submit(dispatch, args, { env = {}, taskId = TASK_ID } = {}) {
  return new Promise(resolve => {
    execFile(
      process.execPath,
      [dispatch.tool, ...args],
      {
        env: {
          ...process.env,
          PROJECT_NAME: PROJECT,
          REDIS_HOST: 'localhost',
          REDIS_PORT: '16399',
          A2A_TASK_ID: taskId,
          A2A_CONTEXT_ID: CONTEXT_ID,
          A2A_LAST_MESSAGE_ID: DISPATCH_MESSAGE_ID,
          AIGANG_COMMONS_DIR: dispatch.commonsDir,
          ...env,
        },
      },
      (err, stdout, stderr) => resolve({ code: err ? (err.code ?? 1) : 0, stdout, stderr })
    );
  });
}

async function entries() {
  return (await client.xRange(STREAM, '-', '+')).map(e => fromStreamFields(e.message));
}

async function onlyEntry(result) {
  assert.equal(result.code, 0, `expected a successful submission: ${result.stderr}`);
  const published = await entries();
  assert.equal(published.length, 1, 'exactly one entry');
  return published[0];
}

function textPart(message) {
  return message.parts.find(p => p.kind === 'text');
}

function dataPart(message) {
  return message.parts.find(p => p.kind === 'data');
}

// Every regular file under `dir`, path -> sha256, so a snapshot can be shown
// unchanged (Agent Commons REQ-02: nothing writes into it).
function fingerprint(dir, prefix = '') {
  const out = {};
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const rel = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) Object.assign(out, fingerprint(path.join(dir, entry.name), rel));
    else out[rel] = crypto.createHash('sha256').update(fs.readFileSync(path.join(dir, entry.name))).digest('hex');
  }
  return out;
}

// ---------------------------------------------------------------------------
// REQ-01 — every operation, constructed from valid arguments alone
// ---------------------------------------------------------------------------

const CASES = [
  {
    name: 'comment',
    args: ['comment', '--text', 'migration written', '--reference-file', 'db/migrate.sql', '--reference-function', 'up'],
    state: 'working',
    text: 'migration written',
    data: { operation: 'comment', reference: { file: 'db/migrate.sql', function: 'up' } },
  },
  {
    name: 'comment without a reference',
    args: ['comment', '--text', 'plain comment'],
    state: 'working',
    text: 'plain comment',
    data: { operation: 'comment' },
  },
  {
    name: 'progress',
    args: ['progress', '--text', 'halfway'],
    state: 'working',
    text: 'halfway',
    data: null,
  },
  {
    name: 'progress with a reference',
    args: ['progress', '--text', 'halfway', '--reference-file', 'src/app.js'],
    state: 'working',
    text: 'halfway',
    data: { reference: { file: 'src/app.js' } },
  },
  {
    name: 'input-required',
    args: ['input-required', '--text', 'which column is canonical?', '--reference-file', 'models.py'],
    state: 'input-required',
    text: 'which column is canonical?',
    data: { reference: { file: 'models.py' } },
  },
  {
    name: 'auth-required',
    args: ['auth-required', '--text', 'the deploy key is missing'],
    state: 'auth-required',
    text: 'the deploy key is missing',
    data: null,
  },
  {
    name: 'reassign',
    args: ['reassign', '--agent', 'frontend-agent'],
    state: 'working',
    text: null,
    data: { operation: 'reassign', agentFieldValue: 'frontend-agent' },
  },
  {
    name: 'create-subtask',
    args: [
      'create-subtask',
      '--summary', 'Frontend Agent: wire the form',
      '--description', 'Bind the new endpoint to the signup form.',
      '--agent', 'frontend-agent',
    ],
    state: 'working',
    text: null,
    data: {
      operation: 'create_subtask',
      summary: 'Frontend Agent: wire the form',
      description: 'Bind the new endpoint to the signup form.',
      agentFieldValue: 'frontend-agent',
    },
  },
  {
    name: 'create-subtask with both optional references',
    args: [
      'create-subtask',
      '--summary', 'Frontend Agent: wire the form',
      '--description', 'Bind the new endpoint to the signup form.',
      '--agent', 'frontend-agent',
      '--specification-artifact', 'ART-1',
      '--specification-requirement', 'REQ-03',
      '--artifact-link', 'ART-2',
      '--artifact-link', 'ART-3',
    ],
    state: 'working',
    text: null,
    data: {
      operation: 'create_subtask',
      summary: 'Frontend Agent: wire the form',
      description: 'Bind the new endpoint to the signup form.',
      agentFieldValue: 'frontend-agent',
      specificationLink: { artifactId: 'ART-1', requirementId: 'REQ-03' },
      artifactLinks: ['ART-2', 'ART-3'],
    },
  },
  {
    name: 'completed',
    args: ['completed', '--text', 'all green'],
    state: 'completed',
    text: 'all green',
    data: null,
  },
  {
    name: 'failed',
    args: ['failed', '--text', 'the toolchain is missing'],
    state: 'failed',
    text: 'the toolchain is missing',
    data: null,
  },
  {
    name: 'canceled',
    args: ['canceled', '--text', 'superseded by HW-9'],
    state: 'canceled',
    text: 'superseded by HW-9',
    data: null,
  },
  {
    name: 'rejected',
    args: ['rejected', '--text', 'this is frontend work'],
    state: 'rejected',
    text: 'this is frontend work',
    data: null,
  },
];

for (const testCase of CASES) {
  test(`REQ-01: the constructor builds and publishes a valid "${testCase.name}" submission from its fields alone`, async () => {
    const dispatch = newDispatch();
    const published = await onlyEntry(await submit(dispatch, testCase.args));
    const message = published.payload.message;

    assert.equal(published.payload.state, testCase.state);
    assert.equal(published.kind, 'gateway_operation');
    assert.equal(published.project, PROJECT);
    assert.equal(message.kind, 'message');
    assert.equal(message.role, 'agent');
    assert.equal(message.taskId, TASK_ID, 'addressed from A2A_TASK_ID, not from an argument');
    assert.equal(message.contextId, CONTEXT_ID, 'addressed from A2A_CONTEXT_ID, not from an argument');
    assert.match(message.messageId, /^[0-9a-f-]{36}$/, 'the tool mints the message id');
    assert.notEqual(message.messageId, published.messageId, 'the message id is never the envelope id');

    if (testCase.text === null) assert.equal(textPart(message), undefined);
    else assert.equal(textPart(message).text, testCase.text);

    if (testCase.data === null) assert.equal(dataPart(message), undefined);
    else assert.deepEqual(dataPart(message).data, testCase.data);

    // Nothing an agent typed is an id, and no argument in this case was one.
    assert.equal(testCase.args.some(a => a === TASK_ID || a === CONTEXT_ID || a === DISPATCH_MESSAGE_ID), false);
  });
}

test('REQ-01: a completed submission with a pull request carries the artifact handleCompleted reads', async () => {
  const dispatch = newDispatch();
  const published = await onlyEntry(await submit(dispatch, [
    'completed', '--text', 'PR open', '--pull-request', 'https://example.test/pull/4',
  ]));

  assert.equal(published.payload.artifacts.length, 1);
  const artifact = published.payload.artifacts[0];
  assert.equal(artifact.kind, 'artifact');
  assert.equal(artifact.name, 'pull-request');
  assert.equal(artifact.taskId, TASK_ID);
  assert.match(artifact.artifactId, /^[0-9a-f-]{36}$/);
  assert.equal(artifact.parts.find(p => p.kind === 'file').file.uri, 'https://example.test/pull/4');
  assert.equal(artifact.parts.find(p => p.kind === 'text').text, 'PR open');
});

test('REQ-01: --pull-request-summary sets the artifact summary independently of the message text', async () => {
  const dispatch = newDispatch();
  const published = await onlyEntry(await submit(dispatch, [
    'completed', '--text', 'closing note', '--pull-request', 'https://example.test/pull/5',
    '--pull-request-summary', 'Adds the endpoint and its tests',
  ]));
  const artifact = published.payload.artifacts[0];
  assert.equal(artifact.parts.find(p => p.kind === 'text').text, 'Adds the endpoint and its tests');
  assert.equal(textPart(published.payload.message).text, 'closing note');
});

// ---------------------------------------------------------------------------
// The chain, tracked by the tool (REQ-01, Agent Commons REQ-03)
// ---------------------------------------------------------------------------

test('REQ-01: the first submission references the dispatch message and the second references the first, with no id passed as an argument', async () => {
  const dispatch = newDispatch();

  await submit(dispatch, ['comment', '--text', 'first']);
  await submit(dispatch, ['comment', '--text', 'second']);

  const published = await entries();
  assert.equal(published.length, 2);

  assert.equal(published[0].payload.message.referenceMessageId, DISPATCH_MESSAGE_ID,
    "the first submission chains to A2A_LAST_MESSAGE_ID, the dispatch message's own A2A id");
  assert.equal(published[1].payload.message.referenceMessageId, published[0].payload.message.messageId,
    "the second chains to the first's message.messageId");
  assert.notEqual(published[1].payload.message.referenceMessageId, published[0].messageId,
    'never the envelope id — a submission chained to that one is refused by taskStore');
});

test('REQ-01: the chain is kept per task', async () => {
  const dispatch = newDispatch();

  await submit(dispatch, ['comment', '--text', 'on HW-1'], { taskId: 'HW-1' });
  await submit(dispatch, ['comment', '--text', 'on HW-2'], { taskId: 'HW-2' });
  await submit(dispatch, ['comment', '--text', 'again on HW-1'], { taskId: 'HW-1' });

  const published = await entries();
  assert.equal(published[1].payload.message.referenceMessageId, DISPATCH_MESSAGE_ID,
    "HW-2's first submission chains to its own dispatch, not to HW-1's last message");
  assert.equal(published[2].payload.message.referenceMessageId, published[0].payload.message.messageId,
    "HW-1's second submission chains to HW-1's own last message");
});

test('the chain lives beside the snapshot and never inside it', async () => {
  const dispatch = newDispatch();
  const before = fingerprint(dispatch.commonsDir);

  await submit(dispatch, ['comment', '--text', 'first']);

  assert.deepEqual(fingerprint(dispatch.commonsDir), before,
    'the snapshot is a content hash of what the session ran — nothing may write into it');
  const chainFile = path.join(dispatch.stateDir, 'a2a-chain.json');
  assert.ok(fs.existsSync(chainFile), 'the chain is in the state directory beside the snapshot');
  const chain = JSON.parse(fs.readFileSync(chainFile, 'utf8'));
  assert.deepEqual(Object.keys(chain), [TASK_ID], 'keyed by task id');
  const published = await entries();
  assert.equal(chain[TASK_ID], published[0].payload.message.messageId);
});

test('a rejected submission does not advance the chain', async () => {
  const dispatch = newDispatch();
  await submit(dispatch, ['comment', '--text', 'first']);
  const afterFirst = JSON.parse(fs.readFileSync(path.join(dispatch.stateDir, 'a2a-chain.json'), 'utf8'));

  const rejected = await submit(dispatch, ['create-subtask', '--summary', 'x', '--agent', 'backend-agent']);
  assert.notEqual(rejected.code, 0);

  assert.deepEqual(
    JSON.parse(fs.readFileSync(path.join(dispatch.stateDir, 'a2a-chain.json'), 'utf8')),
    afterFirst
  );
  assert.equal(await client.xLen(STREAM), 1);
});

// ---------------------------------------------------------------------------
// REQ-02/REQ-03 — rejection at construction, naming the field, before any write
// ---------------------------------------------------------------------------

const ARGUMENT_REJECTIONS = [
  {
    id: 'create-subtask with no summary',
    args: ['create-subtask', '--description', 'd', '--agent', 'backend-agent'],
    expect: /--summary is required for "create-subtask" — it becomes data\.summary/,
  },
  {
    id: 'create-subtask with no description (approved tightening 1)',
    args: ['create-subtask', '--summary', 's', '--agent', 'backend-agent'],
    expect: /--description is required for "create-subtask" — it becomes data\.description/,
  },
  {
    id: 'create-subtask with no agentFieldValue (approved tightening 2)',
    args: ['create-subtask', '--summary', 's', '--description', 'd'],
    expect: /--agent is required for "create-subtask" — it becomes data\.agentFieldValue/,
  },
  {
    id: 'create-subtask naming a specification artifact but no requirement',
    args: ['create-subtask', '--summary', 's', '--description', 'd', '--agent', 'backend-agent',
      '--specification-artifact', 'ART-1'],
    expect: /--specification-artifact and --specification-requirement go together/,
  },
  {
    id: 'reassign with no agent',
    args: ['reassign'],
    expect: /--agent is required for "reassign" — it becomes data\.agentFieldValue/,
  },
  {
    id: 'comment with no text',
    args: ['comment'],
    expect: /--text is required for "comment"/,
  },
  {
    id: 'completed with a pull-request summary but no pull request',
    args: ['completed', '--text', 't', '--pull-request-summary', 's'],
    expect: /--pull-request-summary describes the pull request named by --pull-request/,
  },
  {
    id: 'an operation that does not exist',
    args: ['set-blocked', '--text', 't'],
    expect: /there is no "set-blocked" operation/,
  },
  {
    id: 'a reference on reassign, which consumes none',
    args: ['reassign', '--agent', 'backend-agent', '--reference-file', 'src/app.js'],
    expect: /Unknown option '--reference-file'/,
  },
  {
    id: 'a reference on create-subtask, which consumes none',
    args: ['create-subtask', '--summary', 's', '--description', 'd', '--agent', 'backend-agent',
      '--reference-file', 'src/app.js'],
    expect: /Unknown option '--reference-file'/,
  },
  {
    id: 'a reference on completed, which consumes none',
    args: ['completed', '--text', 't', '--reference-file', 'src/app.js'],
    expect: /Unknown option '--reference-file'/,
  },
  {
    id: 'an id offered as an argument',
    args: ['comment', '--text', 't', '--reference-message-id', 'msg-1'],
    expect: /Unknown option '--reference-message-id'/,
  },
  {
    id: 'a bare positional argument',
    args: ['comment', 'a note'],
    expect: /unexpected argument "a note"/,
  },
];

for (const testCase of ARGUMENT_REJECTIONS) {
  test(`REQ-02/REQ-03: ${testCase.id} is rejected at construction, before any Streams write`, async () => {
    const dispatch = newDispatch();
    const result = await submit(dispatch, testCase.args);
    assert.notEqual(result.code, 0, 'the exit status must be non-zero');
    assert.match(result.stderr, testCase.expect, 'the failing argument is named on stderr');
    assert.equal(await client.xLen(STREAM), 0, 'nothing may reach the stream');
  });
}

test('row 49: --reference-function with no --reference-file is refused on every operation that takes a reference', async () => {
  // `withReference` drops a function with no file, so the value reached nothing
  // and the agent was never told — while the tool's other two paired-flag cases
  // were both enforced. These four are every operation that takes a reference.
  for (const operation of ['comment', 'progress', 'input-required', 'auth-required']) {
    const dispatch = newDispatch();
    const result = await submit(dispatch, [operation, '--text', 'a note', '--reference-function', 'up']);
    assert.notEqual(result.code, 0, `"${operation}" accepted --reference-function with no --reference-file`);
    assert.match(result.stderr, /--reference-function names the context inside the file --reference-file names, which was not given/,
      `"${operation}" must name the missing flag`);
    assert.equal(await client.xLen(STREAM), 0, `"${operation}" wrote to the stream`);
  }
});

test('REQ-03: a session with no dispatch context is refused, naming the variables', async () => {
  const dispatch = newDispatch();
  const result = await submit(dispatch, ['comment', '--text', 'hello'], {
    env: { A2A_TASK_ID: '', A2A_LAST_MESSAGE_ID: '' },
  });
  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /A2A_TASK_ID/);
  assert.match(result.stderr, /A2A_LAST_MESSAGE_ID/);
  assert.equal(await client.xLen(STREAM), 0);
});

// ---------------------------------------------------------------------------
// REQ-01 — a fault injected into the constructor is caught by the validator
// ---------------------------------------------------------------------------

// The constructor's own copy is patched, so the validator is the only thing
// standing between the fault and the stream. The fields these faults break are
// exactly the ones no argument can set, which is what makes them unreachable
// from this entry point rather than merely unlikely.
function faultyDispatch(find, replace) {
  const dispatch = newDispatch();
  const source = fs.readFileSync(dispatch.tool, 'utf8');
  assert.ok(source.includes(find), `the fault injection site "${find}" must exist in a2a-submit.js`);
  fs.writeFileSync(dispatch.tool, source.replace(find, replace));
  return dispatch;
}

test('REQ-01: a constructor that drops the role is stopped by the validator, not published', async () => {
  const dispatch = faultyDispatch("    role: 'agent',\n", '');
  const result = await submit(dispatch, ['comment', '--text', 'hello']);
  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /message\.role must be one of client\/agent/);
  assert.equal(await client.xLen(STREAM), 0);
});

test('row 37: a constructor that submits as the client role is stopped by the validator, not published', async () => {
  // The validator is stricter than the schema here on purpose: both A2A roles
  // are well-formed messages, but only `agent` is a submission, and the gateway
  // drops the other one after the durable write.
  const dispatch = faultyDispatch("    role: 'agent',\n", "    role: 'client',\n");
  const result = await submit(dispatch, ['comment', '--text', 'hello']);
  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /message\.role must be "agent"/);
  assert.equal(await client.xLen(STREAM), 0);
});

test('REQ-01: a constructor that leaves a Part unwrapped is stopped by the validator, not published', async () => {
  const dispatch = faultyDispatch('    parts,\n', '    parts: parts[0],\n');
  const result = await submit(dispatch, ['comment', '--text', 'hello']);
  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /message\.parts must be a non-empty array of Parts/);
  assert.equal(await client.xLen(STREAM), 0);
});

// ---------------------------------------------------------------------------
// Rows 9 and 35 — what the exit status means after the connection is opened
// ---------------------------------------------------------------------------

test('row 9: an unreachable Redis exits non-zero within seconds instead of retrying for ever', async () => {
  // Port 1 is privileged and nothing listens on it: the connection is refused
  // immediately and every retry is refused the same way, which is the shape
  // that used to loop until the subscriber's 30-minute session kill.
  const dispatch = newDispatch();
  const started = Date.now();
  const result = await submit(dispatch, ['comment', '--text', 'nobody is listening'], {
    env: { REDIS_HOST: '127.0.0.1', REDIS_PORT: '1' },
  });
  const elapsed = Date.now() - started;

  assert.notEqual(result.code, 0, 'nothing was published, so the exit status must say so');
  assert.match(result.stderr, /Failed to durably enqueue the "comment" submission/);
  assert.match(result.stderr, /redis:\/\/127\.0\.0\.1:1 is unreachable/,
    'the reason names the unreachable server, on stderr');
  assert.match(result.stderr, /gave up after 3 connection attempts/);
  assert.ok(elapsed < 30000, `the tool must give up in seconds, not hang: took ${elapsed} ms`);
  assert.equal(await client.xLen(STREAM), 0);
});

test('row 35: a chain record that fails after a successful publish warns but still exits 0', async () => {
  const dispatch = newDispatch();
  // The chain file's own path, occupied by a non-empty directory: readChain
  // falls back to {}, the temporary file is written, and the atomic rename onto
  // a directory fails — the one recordChain failure that happens *after* the
  // XADD, which is the case the exit code used to misreport.
  const chainFile = path.join(dispatch.stateDir, 'a2a-chain.json');
  fs.mkdirSync(chainFile);
  fs.writeFileSync(path.join(chainFile, 'occupied'), 'x');

  const result = await submit(dispatch, ['create-subtask',
    '--summary', 'Frontend Agent: wire the form',
    '--description', 'Bind the new endpoint to the signup form.',
    '--agent', 'frontend-agent']);

  // The tool's own contract — its usage text and the a2a-submit skill — is that
  // a non-zero exit means nothing was published and the call should be made
  // again. A re-run of create-subtask materialises a second subtask, since
  // handleCreateSubtask's idempotency key is the envelope id.
  assert.equal(result.code, 0,
    `the submission was published, so the exit status must not say otherwise: ${result.stderr}`);
  assert.match(result.stdout, /^Accepted: create-subtask on task HW-1 /m,
    'the accepted line is still printed');
  assert.match(result.stderr, /Published, but could not record the chain for task HW-1/,
    'the flattened chain is still reported, on stderr');
  const published = await entries();
  assert.equal(published.length, 1,
    'exactly one entry, and nothing telling the agent to create a second subtask');
});

// ---------------------------------------------------------------------------
// The usage text, which the a2a-submit skill defers argument syntax to
// ---------------------------------------------------------------------------

test('the usage text names every operation and its arguments', async () => {
  const dispatch = newDispatch();
  const result = await submit(dispatch, ['help']);
  assert.equal(result.code, 0, result.stderr);

  for (const operation of [
    'comment', 'progress', 'input-required', 'auth-required', 'reassign',
    'create-subtask', 'completed', 'failed', 'canceled', 'rejected',
  ]) {
    assert.match(result.stdout, new RegExp(`\\n  ${operation}  \\(state: `), `${operation} is missing from the usage text`);
  }

  for (const flag of [
    '--text', '--reference-file', '--reference-function', '--agent', '--summary',
    '--description', '--specification-artifact', '--specification-requirement',
    '--artifact-link', '--pull-request', '--pull-request-summary',
  ]) {
    assert.ok(result.stdout.includes(flag), `${flag} is missing from the usage text`);
  }

  // No id is an argument, and the usage text says where they come from instead.
  assert.doesNotMatch(result.stdout, /--message-id|--task-id|--context-id|--reference-message-id/);
  assert.match(result.stdout, /reads them from the dispatch/);
});

test('help for one operation lists only that operation', async () => {
  const dispatch = newDispatch();
  const result = await submit(dispatch, ['help', 'create-subtask']);
  assert.equal(result.code, 0, result.stderr);
  assert.match(result.stdout, /create-subtask/);
  assert.doesNotMatch(result.stdout, /\n  reassign  \(state: /);
});

// ---------------------------------------------------------------------------
// Agent Commons REQ-02's acceptance clause about this tool: "invoking the
// constructor by name from that session runs it — no node and no path"
// ---------------------------------------------------------------------------

test('the per-dispatch snapshot marks the constructor executable, and it runs by bare name from PATH', async () => {
  const { createSnapshot, removeSnapshot } = require('../../dispatch-snapshot');

  // A stand-in for the mounted /agent-docs, holding the real tools. Every entry
  // the snapshot carries has to be there, definitions and handbooks included:
  // a dispatch that cannot carry one fails rather than leaving it on the mount
  // (Agent Commons REQ-02, Amendment 1).
  const source = fs.mkdtempSync(path.join(root, 'agent-docs-source-'));
  fs.cpSync(__dirname, path.join(source, 'commons', 'tools'), {
    recursive: true,
    filter: src => !src.endsWith('.test.js'),
  });
  fs.mkdirSync(path.join(source, 'commons', 'skills'));
  fs.mkdirSync(path.join(source, 'agents'));
  fs.writeFileSync(path.join(source, 'agents', 'devops-agent.md'), '# DevOps Agent\n');
  fs.writeFileSync(path.join(source, 'DEVOPS_HANDBOOK_v1.md'), '# DevOps handbook\n');
  fs.writeFileSync(path.join(source, 'DESKTOP_HANDBOOK_v1.md'), '# Desktop handbook\n');

  const snapshot = createSnapshot({ source, root: fs.mkdtempSync(path.join(root, 'snapshot-root-')) });
  try {
    assert.ok(snapshot.executables.includes('tools/a2a-submit.js'),
      'a file starting with #! is marked executable by the snapshot');

    const result = await new Promise(resolve => {
      execFile('a2a-submit.js', ['help'], {
        env: {
          ...process.env,
          PATH: `${snapshot.toolsDir}:${process.env.PATH}`,
          AIGANG_COMMONS_DIR: snapshot.commonsDir,
        },
      }, (err, stdout, stderr) => resolve({ code: err ? (err.code ?? 1) : 0, stdout, stderr }));
    });

    assert.equal(result.code, 0, result.stderr);
    assert.match(result.stdout, /Usage: a2a-submit\.js <operation> \[arguments\]/);
  } finally {
    removeSnapshot(snapshot);
  }
});
