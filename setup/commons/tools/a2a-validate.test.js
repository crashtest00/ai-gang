'use strict';

/**
 * Construction-time validation on the raw file/stdin entry point — the one
 * Jenkins uses and any producer that already holds a message
 * (V5.0 Deterministic Gateway Message Tooling REQ-01, REQ-02, REQ-03, REQ-07).
 *
 * Every case runs `gateway-publish.js` as a real child process against the
 * real test Redis (redis://localhost:16399,
 * services/core/docker-compose.test.yml — start it first if it is not up;
 * never `down` it), so what is under test is the production path a producer
 * invokes, not the validator function on its own. A rejection is proved twice
 * over: the process exits non-zero with the field named on stderr, and the
 * gateway stream is no longer than it was before the call.
 *
 * Run through setup/commons/tools/test.sh, which holds
 * /tmp/v4-wis-suite.lock and sets NODE_PATH — the shared test Redis is also
 * ScrumMaster's and core's (V4 audit Pass 2 row 35).
 */

const { test, before, beforeEach, after } = require('node:test');
const assert = require('node:assert/strict');
const { execFile } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createClient } = require('redis');
const { fromStreamFields } = require('./envelope');

const REDIS_URL = process.env.REDIS_TEST_URL || 'redis://localhost:16399';
const TOOL = path.join(__dirname, 'gateway-publish.js');
const PROJECT = 'v5-validator-raw-test';
const STREAM = `aigang:gateway:${PROJECT}`;
const TASK_ID = 'HW-1';

let client;
let workDir;

before(async () => {
  client = createClient({ url: REDIS_URL });
  await client.connect();
  workDir = fs.mkdtempSync(path.join(os.tmpdir(), 'a2a-validate-test-'));
});

// A stream name of this track's own, deleted rather than flushed: the test
// Redis is shared with suites that have live consumers on their own keys.
beforeEach(async () => {
  await client.del(STREAM);
});

after(async () => {
  await client.del(STREAM);
  await client.quit();
  fs.rmSync(workDir, { recursive: true, force: true });
});

// Run the raw entry point on `payload`, either from a file or on stdin.
// `redisHost`/`redisPort` default to the shared test Redis; one case points them
// at a closed port instead.
function publish(payload, { onStdin = false, redisHost = 'localhost', redisPort = '16399' } = {}) {
  const json = JSON.stringify(payload);
  const args = [TOOL, PROJECT, onStdin ? '-' : path.join(workDir, `payload-${process.hrtime.bigint()}.json`)];
  if (!onStdin) fs.writeFileSync(args[2], json);

  return new Promise(resolve => {
    const child = execFile(
      process.execPath,
      args,
      { env: { ...process.env, REDIS_HOST: redisHost, REDIS_PORT: redisPort } },
      (err, stdout, stderr) => resolve({ code: err ? (err.code ?? 1) : 0, stdout, stderr })
    );
    if (onStdin) {
      child.stdin.write(json);
      child.stdin.end();
    }
  });
}

async function entries() {
  return (await client.xRange(STREAM, '-', '+')).map(e => fromStreamFields(e.message));
}

// ---------------------------------------------------------------------------
// One well-formed payload per operation type the gateway routes.
// ---------------------------------------------------------------------------

function message(parts) {
  return {
    kind: 'message',
    messageId: `msg-${process.hrtime.bigint()}`,
    taskId: TASK_ID,
    contextId: TASK_ID,
    role: 'agent',
    referenceMessageId: 'msg-dispatch-1',
    parts,
  };
}

const OPERATIONS = {
  comment: () => ({
    state: 'working',
    message: message([{ kind: 'text', text: 'progress' }, { kind: 'data', data: { operation: 'comment' } }]),
  }),
  progress: () => ({
    state: 'working',
    message: message([{ kind: 'text', text: 'still going' }]),
  }),
  'progress with a reference': () => ({
    state: 'working',
    message: message([
      { kind: 'text', text: 'still going' },
      { kind: 'data', data: { reference: { file: 'db/migrate.sql', function: 'up' } } },
    ]),
  }),
  blocked: () => ({
    state: 'input-required',
    message: message([{ kind: 'text', text: 'which column is canonical?' }]),
  }),
  reassign: () => ({
    state: 'working',
    message: message([
      { kind: 'text', text: 'handing over' },
      { kind: 'data', data: { operation: 'reassign', agentFieldValue: 'frontend-agent' } },
    ]),
  }),
  create_subtask: () => ({
    state: 'working',
    message: message([
      { kind: 'text', text: 'splitting this out' },
      {
        kind: 'data',
        data: {
          operation: 'create_subtask',
          summary: 'Frontend Agent: wire the form',
          description: 'Bind the new endpoint to the signup form.',
          agentFieldValue: 'frontend-agent',
        },
      },
    ]),
  }),
  completed: () => ({
    state: 'completed',
    message: message([{ kind: 'text', text: 'done' }]),
    artifacts: [{
      kind: 'artifact',
      artifactId: 'artifact-1',
      taskId: TASK_ID,
      name: 'pull-request',
      parts: [
        { kind: 'file', file: { name: 'pull-request', mimeType: 'text/uri-list', uri: 'https://example.test/pull/1' } },
        { kind: 'text', text: 'summary' },
      ],
    }],
  }),
  terminal: () => ({
    state: 'failed',
    message: message([{ kind: 'text', text: 'the toolchain is missing' }]),
  }),
};

test('every operation type publishes when it is well formed', async () => {
  for (const [name, build] of Object.entries(OPERATIONS)) {
    await client.del(STREAM);
    const result = await publish(build());
    assert.equal(result.code, 0, `${name} should publish: ${result.stderr}`);
    const published = await entries();
    assert.equal(published.length, 1, `${name} should have written exactly one entry`);
    assert.equal(published[0].payload.message.taskId, TASK_ID);
  }
});

test('a well-formed payload publishes identically from stdin and from a file', async () => {
  await publish(OPERATIONS.comment(), { onStdin: false });
  await publish(OPERATIONS.comment(), { onStdin: true });
  const published = await entries();
  assert.equal(published.length, 2);
  assert.deepEqual(published[0].payload.state, published[1].payload.state);
  assert.deepEqual(published[0].payload.message.parts, published[1].payload.message.parts);
});

// ---------------------------------------------------------------------------
// REQ-01 — message-shape validation before publish, for every operation type
// ---------------------------------------------------------------------------

const SHAPE_FAULTS = [
  {
    id: 'a missing messageId',
    apply: p => { delete p.message.messageId; },
    expect: /message\.messageId must be a non-empty string/,
  },
  {
    id: 'an invalid role',
    apply: p => { p.message.role = 'operator'; },
    expect: /message\.role must be one of client\/agent/,
  },
  {
    // Row 37: `schema.MESSAGE_ROLES` allows both A2A roles, so this used to
    // publish — and `handleA2ASubmission` then dropped it with a console.warn,
    // after the durable write, which no producer reads.
    id: 'the client role, which the gateway drops after the durable write (row 37)',
    apply: p => { p.message.role = 'client'; },
    expect: /message\.role must be "agent"/,
  },
  {
    id: 'an invalid state',
    apply: p => { p.state = 'in-progress'; },
    expect: /state must be one of submitted\/working/,
  },
  {
    id: 'a parts entry with an unrecognized kind',
    apply: p => { p.message.parts.push({ kind: 'diagram', text: 'x' }); },
    expect: /message\.parts\[\d+\]\.kind must be one of text\/data\/file/,
  },
  {
    id: 'an artifact missing a required field',
    apply: p => {
      p.artifacts = [{
        kind: 'artifact', artifactId: 'artifact-1', taskId: TASK_ID,
        parts: [{ kind: 'text', text: 'x' }],
      }];
    },
    expect: /artifacts\[0\]\.name must be a non-empty string/,
  },
];

for (const fault of SHAPE_FAULTS) {
  test(`REQ-01: ${fault.id} is rejected before any Streams write, for every operation type`, async () => {
    for (const [name, build] of Object.entries(OPERATIONS)) {
      const payload = build();
      fault.apply(payload);
      const before = await client.xLen(STREAM);
      const result = await publish(payload);
      assert.notEqual(result.code, 0, `${name} with ${fault.id} should have been rejected`);
      assert.match(result.stderr, fault.expect, `${name} with ${fault.id} should name the field`);
      assert.match(result.stderr, /nothing was published/i);
      assert.equal(await client.xLen(STREAM), before, `${name} with ${fault.id} wrote to the stream`);
    }
  });
}

test('REQ-01: a payload that is not a JSON object is rejected', async () => {
  const result = await publish(['not', 'an', 'object']);
  assert.notEqual(result.code, 0);
  assert.match(result.stderr, /must be a JSON object/);
  assert.equal(await client.xLen(STREAM), 0);
});

// ---------------------------------------------------------------------------
// REQ-02 — operation-specific fields, and the two approved tightenings
// ---------------------------------------------------------------------------

function withSubtaskData(changes) {
  const payload = OPERATIONS.create_subtask();
  const data = payload.message.parts[1].data;
  for (const [key, value] of Object.entries(changes)) {
    if (value === undefined) delete data[key];
    else data[key] = value;
  }
  return payload;
}

const FIELD_CASES = [
  {
    id: 'create_subtask with no summary',
    payload: () => withSubtaskData({ summary: undefined }),
    expect: /data\.summary must be a non-empty string for the "create_subtask" operation/,
  },
  {
    id: 'create_subtask with no agentFieldValue (approved tightening 2)',
    payload: () => withSubtaskData({ agentFieldValue: undefined }),
    expect: /data\.agentFieldValue must be a non-empty string for the "create_subtask" operation/,
  },
  {
    id: 'create_subtask with no description (approved tightening 1)',
    payload: () => withSubtaskData({ description: undefined }),
    expect: /data\.description must be a non-empty string for the "create_subtask" operation/,
  },
  {
    id: 'create_subtask with a specificationLink that is not an object',
    payload: () => withSubtaskData({ specificationLink: 'ART-1' }),
    expect: /data\.specificationLink is present but is not an object with non-empty "artifactId" and "requirementId" strings/,
  },
  {
    id: 'create_subtask with a specificationLink missing requirementId',
    payload: () => withSubtaskData({ specificationLink: { artifactId: 'ART-1' } }),
    expect: /data\.specificationLink is present but is not an object/,
  },
  {
    id: 'create_subtask with artifactLinks that are not an array',
    payload: () => withSubtaskData({ artifactLinks: 'ART-2' }),
    expect: /data\.artifactLinks is present but is not an array of non-empty artifact id strings/,
  },
  {
    id: 'create_subtask with an empty string among artifactLinks',
    payload: () => withSubtaskData({ artifactLinks: ['ART-2', ''] }),
    expect: /data\.artifactLinks is present but is not an array/,
  },
  {
    id: 'reassign with no agentFieldValue',
    payload: () => {
      const payload = OPERATIONS.reassign();
      delete payload.message.parts[1].data.agentFieldValue;
      return payload;
    },
    expect: /data\.agentFieldValue must be a non-empty string for the "reassign" operation/,
  },
  {
    id: 'a completed pull-request artifact whose file part has no uri',
    payload: () => {
      const payload = OPERATIONS.completed();
      delete payload.artifacts[0].parts[0].file.uri;
      return payload;
    },
    // The Part schema catches a file with neither uri nor bytes first; both
    // messages name the missing reference, which is what REQ-03 requires.
    expect: /file must have a "uri" reference or inline "bytes"/,
  },
  {
    id: 'a completed pull-request artifact with no file part at all',
    payload: () => {
      const payload = OPERATIONS.completed();
      payload.artifacts[0].parts = [{ kind: 'text', text: 'summary' }];
      return payload;
    },
    expect: /artifacts\[0\] is named "pull-request" and must carry a file Part whose file\.uri is a non-empty string/,
  },
  {
    // Row 37: any unrecognised operation used to publish, and `gateway.js`'s
    // switch then fell to reportUnsupportedOperation — after the entry was
    // already on the stream.
    id: 'an operation the gateway does not route (row 37)',
    payload: () => {
      const payload = OPERATIONS.comment();
      payload.message.parts[1].data.operation = 'set_blocked';
      return payload;
    },
    expect: /data\.operation must be one of comment\/reassign\/create_subtask, or absent for a plain progress note/,
  },
  {
    id: 'a reference with no file',
    payload: () => {
      const payload = OPERATIONS.comment();
      payload.message.parts[1].data.reference = { function: 'up' };
      return payload;
    },
    expect: /data\.reference\.file must be a non-empty string/,
  },
  {
    id: 'a reference that is neither an object nor a string',
    payload: () => {
      const payload = OPERATIONS.comment();
      payload.message.parts[1].data.reference = 42;
      return payload;
    },
    expect: /data\.reference must be an object with a non-empty "file" string/,
  },
];

for (const testCase of FIELD_CASES) {
  test(`REQ-02: ${testCase.id} is rejected, naming the field, before any Streams write`, async () => {
    const result = await publish(testCase.payload());
    assert.notEqual(result.code, 0);
    assert.match(result.stderr, testCase.expect);
    assert.equal(await client.xLen(STREAM), 0, 'nothing may reach the stream');
  });
}

test('REQ-02/REQ-05: the two optional create_subtask references publish when well formed', async () => {
  const result = await publish(withSubtaskData({
    specificationLink: { artifactId: 'ART-1', requirementId: 'REQ-03' },
    artifactLinks: ['ART-2', 'ART-3'],
  }));
  assert.equal(result.code, 0, result.stderr);
  const [published] = await entries();
  assert.deepEqual(published.payload.message.parts[1].data.specificationLink, { artifactId: 'ART-1', requirementId: 'REQ-03' });
  assert.deepEqual(published.payload.message.parts[1].data.artifactLinks, ['ART-2', 'ART-3']);
});

test('REQ-05: a bare-string reference still publishes — formatReference accepts one, so this tool must not refuse it', async () => {
  const payload = OPERATIONS.comment();
  payload.message.parts[1].data.reference = 'db/migrate.sql';
  const result = await publish(payload);
  assert.equal(result.code, 0, result.stderr);
  assert.equal(await client.xLen(STREAM), 1);
});

test('REQ-05: a reference on an operation that does not consume it is not refused', async () => {
  // `reassign` reaches nothing with a reference, which is why the constructor
  // offers no flag for one. A hand-built message that carries one anyway is
  // still exactly as publishable as it is today.
  const payload = OPERATIONS.reassign();
  payload.message.parts[1].data.reference = { file: 'src/app.js' };
  const result = await publish(payload);
  assert.equal(result.code, 0, result.stderr);
  assert.equal(await client.xLen(STREAM), 1);
});

// Row 84: the per-operation *field* checks were not behind
// `gatewayReadsOperation` although the allow-list beside them was, so a
// terminal or interrupted submission carrying `operation: create_subtask` or
// `reassign` was refused for fields no branch that handles it ever reads. One
// case per state class `handleA2ASubmission` branches into, each carrying a
// bare operation and none of its fields.
const UNREAD_OPERATION_STATES = [
  { id: 'a completed submission (handleCompleted)', state: 'completed', operation: 'create_subtask' },
  { id: 'a failed submission (handleTerminalFailure)', state: 'failed', operation: 'create_subtask' },
  { id: 'a canceled submission (handleTerminalFailure)', state: 'canceled', operation: 'reassign' },
  { id: 'an input-required submission (handleInterrupted)', state: 'input-required', operation: 'create_subtask' },
  { id: 'an auth-required submission (handleInterrupted)', state: 'auth-required', operation: 'reassign' },
];

for (const c of UNREAD_OPERATION_STATES) {
  test(`row 84: ${c.id} carrying a bare "${c.operation}" operation still publishes — that branch reads none of its fields`, async () => {
    const payload = {
      state: c.state,
      message: message([
        { kind: 'text', text: 'nothing routed here' },
        { kind: 'data', data: { operation: c.operation } },
      ]),
    };
    const result = await publish(payload);
    assert.equal(result.code, 0, result.stderr);
    assert.equal(await client.xLen(STREAM), 1);
  });
}

test('row 84: the same fields are still required on a working submission, where gateway.js does route the operation', async () => {
  // The state class the guard must not weaken: `working` reaches the
  // `switch (operation)` branch, so handleCreateSubtask and handleReassign do
  // read these fields and REQ-02 still refuses a submission without them.
  const subtask = await publish({
    state: 'working',
    message: message([
      { kind: 'text', text: 'splitting this out' },
      { kind: 'data', data: { operation: 'create_subtask' } },
    ]),
  });
  assert.notEqual(subtask.code, 0);
  assert.match(subtask.stderr, /data\.summary must be a non-empty string for the "create_subtask" operation/);
  assert.match(subtask.stderr, /data\.description must be a non-empty string for the "create_subtask" operation/);
  assert.match(subtask.stderr, /data\.agentFieldValue must be a non-empty string for the "create_subtask" operation/);
  assert.equal(await client.xLen(STREAM), 0, 'nothing may reach the stream');

  const reassign = await publish({
    state: 'working',
    message: message([
      { kind: 'text', text: 'handing over' },
      { kind: 'data', data: { operation: 'reassign' } },
    ]),
  });
  assert.notEqual(reassign.code, 0);
  assert.match(reassign.stderr, /data\.agentFieldValue must be a non-empty string for the "reassign" operation/);
  assert.equal(await client.xLen(STREAM), 0, 'nothing may reach the stream');
});

test('F-2: a terminal submission carrying an unrouted operation still publishes — handleA2ASubmission never reads operation there', async () => {
  // `set_blocked` is exactly what the row-37 case above refuses on a
  // `working` submission, because gateway.js's non-terminal, non-interrupted
  // branch switches on it. A `failed` submission is handled by
  // handleTerminalFailure instead, which never destructures `operation` at
  // all — so the same value here must not be refused. Refusing it would
  // refuse input the gateway simply ignores, which this file's header
  // promises it does not do.
  const payload = OPERATIONS.terminal();
  payload.message.parts.push({ kind: 'data', data: { operation: 'set_blocked' } });
  const result = await publish(payload);
  assert.equal(result.code, 0, result.stderr);
  assert.equal(await client.xLen(STREAM), 1);
});

// ---------------------------------------------------------------------------
// REQ-07 — a further structured action is a definition, not a second checker
// ---------------------------------------------------------------------------

test('REQ-03/REQ-07: a well-formed pipeline_retry publishes and a malformed one is rejected by name', async () => {
  const ok = await publish({
    type: 'pipeline_retry', ticket_key: 'HW-1', build_url: 'https://ci.test/job/7', build_number: '7',
  });
  assert.equal(ok.code, 0, ok.stderr);
  assert.equal(await client.xLen(STREAM), 1);

  const bad = await publish({ type: 'pipeline_retry', build_url: 'https://ci.test/job/7', build_number: '7' });
  assert.notEqual(bad.code, 0);
  assert.match(bad.stderr, /ticket_key must be a non-empty string for a "pipeline_retry" message/);
  assert.equal(await client.xLen(STREAM), 1, 'the rejected message must not have been written');

  const empty = await publish({ type: 'pipeline_retry', ticket_key: 'HW-1', build_url: '' });
  assert.notEqual(empty.code, 0);
  assert.match(empty.stderr, /build_url must be a non-empty string when present/);
});

test('REQ-07: materializeDecomposition — an action outside REQ-01\'s set, validated by its definition alone', async () => {
  const ok = await publish({
    operation: 'materializeDecomposition',
    parentJiraIssueKey: 'HW-1',
    subtasks: [{ id: 'sub-1', displayName: 'Backend Agent: do it', agent: 'backend-agent' }],
  });
  assert.equal(ok.code, 0, ok.stderr);
  assert.equal(await client.xLen(STREAM), 1);

  const noParent = await publish({ operation: 'materializeDecomposition', subtasks: [] });
  assert.notEqual(noParent.code, 0);
  assert.match(noParent.stderr, /parentJiraIssueKey must be a non-empty string for the "materializeDecomposition" operation/);

  const noSubtasks = await publish({ operation: 'materializeDecomposition', parentJiraIssueKey: 'HW-1' });
  assert.notEqual(noSubtasks.code, 0);
  assert.match(noSubtasks.stderr, /subtasks must be an array for the "materializeDecomposition" operation/);

  assert.equal(await client.xLen(STREAM), 1, 'only the well-formed one reached the stream');
});

test('REQ-07: one checker, two entry points — both require the same validator and nothing else checks', () => {
  const files = fs.readdirSync(__dirname).filter(f => f.endsWith('.js') && !f.endsWith('.test.js'));
  const validators = files.filter(f => /validate|schema/.test(f));
  assert.deepEqual(validators.sort(), ['a2a-schema.js', 'a2a-validate.js'],
    'the validator is one module over one copy of the schema');

  for (const entryPoint of ['gateway-publish.js', 'a2a-submit.js']) {
    const source = fs.readFileSync(path.join(__dirname, entryPoint), 'utf8');
    assert.match(source, /require\('\.\/a2a-validate'\)/, `${entryPoint} must validate through a2a-validate.js`);
    assert.doesNotMatch(source, /require\('\.\/a2a-schema'\)/,
      `${entryPoint} must not reach past the validator into the schema copy`);
  }

  const executables = files.filter(f => fs.readFileSync(path.join(__dirname, f), 'utf8').startsWith('#!'));
  assert.deepEqual(executables.sort(), ['a2a-submit.js', 'gateway-publish.js', 'request-artifact.js'],
    'no third gateway entry point exists');
});

// ---------------------------------------------------------------------------
// Row 9 — an unreachable Redis is bounded, not retried for ever
// ---------------------------------------------------------------------------

test('row 9: an unreachable Redis exits non-zero within seconds instead of retrying for ever', async () => {
  // Port 1 is privileged and nothing listens on it, so the connection is
  // refused immediately and every retry is refused the same way — which is
  // exactly the shape that used to loop without end. Jenkins calls this entry
  // point inside a `post { failure { ... } }` shell loop, so an unbounded wait
  // here hangs the build (setup/Jenkinsfile.template's own `timeout` step is
  // the second half of the fix, asserted in jenkins/test/).
  const started = Date.now();
  const result = await publish(OPERATIONS.comment(), { redisHost: '127.0.0.1', redisPort: '1' });
  const elapsed = Date.now() - started;

  assert.notEqual(result.code, 0, 'nothing was published, so the exit status must say so');
  assert.match(result.stderr, /Failed to durably enqueue/);
  assert.match(result.stderr, /redis:\/\/127\.0\.0\.1:1 is unreachable/,
    'the reason names the unreachable server, on stderr');
  assert.match(result.stderr, /gave up after 3 connection attempts/);
  assert.ok(elapsed < 30000, `the tool must give up in seconds, not hang: took ${elapsed} ms`);
  assert.equal(await client.xLen(STREAM), 0);
});
