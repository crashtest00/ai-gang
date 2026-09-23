'use strict';

/**
 * Tests for request-artifact.js against the real test Redis
 * (services/work-item-service/docker-compose.test.yml,
 * redis://localhost:16399 — start with `docker compose -f
 * docker-compose.test.yml up -d --wait` from services/work-item-service/
 * if it is not already up; never `down` it, per this track's build brief).
 *
 * The librarian itself is Python and out of this track's scope
 * (librarian/README.md, librarian/consumer.py) — these tests simulate it by
 * writing the reply entry directly onto aigang:librarian:responses, exactly
 * the way librarian/README.md's contract says any requester's answer
 * arrives. request-artifact.js is run as a real child process talking to
 * real Redis throughout; no internal function is called in its place.
 *
 * Run through setup/lib/test.sh (needs the `redis` package resolvable —
 * the project containers get it from a global npm install per
 * Docker Templates/Dockerfile-node.template; locally, install it the same
 * way and point NODE_PATH at it):
 *   sudo npm install -g redis
 *   setup/lib/test.sh
 *
 * setup/lib/test.sh, not a bare `node --test` invocation, because these
 * tests share the real test Redis (REDIS_TEST_URL above) with
 * services/work-item-service's own suite and write to the real
 * aigang:librarian:requests/:responses stream names a live librarian
 * consumer also reads (V4 audit Pass 2 row 35) — the script holds the
 * same /tmp/v4-wis-suite.lock that suite's run_tests.sh does, so the two
 * never run concurrently against the shared container.
 */

const { test, before, beforeEach, afterEach, after } = require('node:test');
const assert = require('node:assert/strict');
const { execFile } = require('node:child_process');
const path = require('node:path');
const crypto = require('node:crypto');
const { createClient } = require('redis');
const { buildEnvelope, KIND, toStreamFields, fromStreamFields } = require('./envelope');

const REDIS_URL = process.env.REDIS_TEST_URL || 'redis://localhost:16399';
const HELPER_PATH = path.join(__dirname, 'request-artifact.js');
const REQUEST_STREAM = 'aigang:librarian:requests';
const RESPONSE_STREAM = 'aigang:librarian:responses';

let client;
let requestStreamStartId;
let responseStreamStartId;

before(async () => {
  client = createClient({ url: REDIS_URL });
  await client.connect();
});

after(async () => {
  await client.quit();
});

// These streams are the real, shared aigang:librarian:requests/:responses
// names a live librarian consumer also reads — `flushDb()` between tests
// would be a bigger hammer than this file owns: it would erase whatever
// unrelated state that consumer, or a concurrent test run of another
// suite against this same test Redis, currently has. Instead, record each
// stream's last-generated-id before the test runs...
beforeEach(async () => {
  requestStreamStartId = await lastId(REQUEST_STREAM);
  responseStreamStartId = await lastId(RESPONSE_STREAM);
});

// ...and afterward, delete every entry that appeared during this test
// (V4 audit Pass 2 row 35), which under the suite lock (`test.sh`) is
// exactly this test's own — `trimStreamSince` below deletes everything
// after the recorded cursor regardless of producer, and the lock is what
// keeps that from touching anyone else's entries. This is what keeps a
// test's request/response envelopes from lingering for a real librarian
// consumer to pick up after the test process exits.
afterEach(async () => {
  await trimStreamSince(REQUEST_STREAM, requestStreamStartId);
  await trimStreamSince(RESPONSE_STREAM, responseStreamStartId);
});

async function trimStreamSince(stream, sinceId) {
  const entries = await client.xRange(stream, `(${sinceId}`, '+');
  if (entries.length > 0) {
    await client.xDel(stream, entries.map(e => e.id));
  }
}

function runHelper(args, extraEnv = {}) {
  return new Promise(resolve => {
    const env = { ...process.env, REDIS_HOST: '127.0.0.1', REDIS_PORT: '16399', PROJECT_NAME: 'test-project' };
    delete env.AGENT_DISPLAY_NAME;
    Object.assign(env, extraEnv);
    execFile('node', [HELPER_PATH, ...args], { env, timeout: 15000 }, (err, stdout, stderr) => {
      resolve({ code: err ? (typeof err.code === 'number' ? err.code : 1) : 0, stdout, stderr });
    });
  });
}

async function lastId(stream) {
  try {
    const info = await client.xInfoStream(stream);
    return info['last-generated-id'] || '0-0';
  } catch {
    return '0-0';
  }
}

// Observe the one request request-artifact.js publishes, the way a real
// librarian's consumer would see it arrive. Matched by artifactId (each
// test mints its own crypto.randomUUID()), not by "whatever comes next on
// the stream" — the request/response streams are the real, shared test
// Redis, so a stray entry left by another process is a real possibility
// this should not be fooled by, the same way the helper itself is never
// fooled by an unrelated response (correlationId, not arrival order).
async function awaitRequestEnvelope(cursor, artifactId, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const result = await client.xRead({ key: REQUEST_STREAM, id: cursor }, { COUNT: 20, BLOCK: 500 });
    if (result) {
      for (const { messages } of result) {
        for (const { id, message } of messages) {
          cursor = id;
          const envelope = fromStreamFields(message);
          if (envelope && envelope.payload && envelope.payload.artifactId === artifactId) return envelope;
        }
      }
    }
  }
  throw new Error(`no request for artifactId=${artifactId} observed on ${REQUEST_STREAM} within ${timeoutMs}ms`);
}

// Simulates the librarian's one answer (librarian/README.md's response
// contract, librarian/responses.py's field shape).
async function publishResponse(fields) {
  const { correlationId, taskId = null, ...rest } = fields;
  const envelope = buildEnvelope({
    kind: KIND.ARTIFACT_DELIVERY_RESPONSE,
    project: '_instance',
    correlationId,
    taskId,
    payload: { taskId, ...rest },
  });
  await client.xAdd(RESPONSE_STREAM, '*', toStreamFields(envelope));
}

test('publishes a request with kind=artifact_delivery_request and the librarian field contract', async () => {
  const cursor = await lastId(REQUEST_STREAM);
  const artifactId = crypto.randomUUID();

  const helperPromise = runHelper(['proj-alpha', artifactId, 'designs/mockup.png', '--task-id', 'GANG-1', '--timeout-ms', '4000']);
  const request = await awaitRequestEnvelope(cursor, artifactId);

  assert.equal(request.schemaVersion, '1');
  assert.equal(request.kind, 'artifact_delivery_request');
  assert.equal(request.project, '_instance');
  assert.match(request.messageId, /^msg-/);
  assert.equal(request.payload.requestedBy, 'test-project');
  assert.equal(request.payload.artifactId, artifactId);
  assert.equal(request.payload.destinationRepo, 'proj-alpha');
  assert.equal(request.payload.requestedPath, 'designs/mockup.png');
  assert.equal(request.payload.taskId, 'GANG-1');

  // Answer it so the child exits instead of running to its own timeout.
  await publishResponse({
    correlationId: request.messageId, status: 'delivered', path: 'designs/mockup.png',
    action: 'copied', deliveredAt: new Date().toISOString(),
    artifactId, destinationRepo: 'proj-alpha', requestedBy: 'test-project', taskId: 'GANG-1',
  });
  const result = await helperPromise;
  assert.equal(result.code, 0);
});

test('destinationRepo is the project name unmodified — never lowercased', async () => {
  const cursor = await lastId(REQUEST_STREAM);
  const artifactId = crypto.randomUUID();
  const helperPromise = runHelper(['Mixed-Case-Project', artifactId, 'a.txt']);
  const request = await awaitRequestEnvelope(cursor, artifactId);
  assert.equal(request.payload.destinationRepo, 'Mixed-Case-Project');
  await publishResponse({
    correlationId: request.messageId, status: 'delivered', path: 'a.txt',
    action: 'copied', deliveredAt: new Date().toISOString(),
    artifactId, destinationRepo: 'Mixed-Case-Project', requestedBy: 'test-project',
  });
  const result = await helperPromise;
  assert.equal(result.code, 0);
});

test('AGENT_DISPLAY_NAME wins over PROJECT_NAME for requestedBy when both are set', async () => {
  const cursor = await lastId(REQUEST_STREAM);
  const artifactId = crypto.randomUUID();
  const helperPromise = runHelper(['proj-alpha', artifactId, 'a.txt'], { AGENT_DISPLAY_NAME: 'frontend-agent' });
  const request = await awaitRequestEnvelope(cursor, artifactId);
  assert.equal(request.payload.requestedBy, 'frontend-agent');
  await publishResponse({
    correlationId: request.messageId, status: 'delivered', path: 'a.txt',
    action: 'copied', deliveredAt: new Date().toISOString(),
    artifactId, destinationRepo: 'proj-alpha', requestedBy: 'frontend-agent',
  });
  const result = await helperPromise;
  assert.equal(result.code, 0);
});

test('a delivered reply with a matching correlationId is picked up and its path is printed', async () => {
  const cursor = await lastId(REQUEST_STREAM);
  const artifactId = crypto.randomUUID();
  const helperPromise = runHelper(['proj-alpha', artifactId, 'designs/mockup.png', '--timeout-ms', '5000']);
  const request = await awaitRequestEnvelope(cursor, artifactId);

  // The collision rule (REQ-04) can answer with a path different from the
  // one requested — proving the helper prints what the librarian said, not
  // an echo of its own argument.
  await publishResponse({
    correlationId: request.messageId, status: 'delivered', path: 'designs/mockup-1.png',
    action: 'copied', deliveredAt: new Date().toISOString(),
    artifactId, destinationRepo: 'proj-alpha', requestedBy: 'test-project',
  });

  const result = await helperPromise;
  assert.equal(result.code, 0);
  assert.equal(result.stdout.trim(), 'designs/mockup-1.png');
  assert.equal(result.stderr, '');
});

test('a reply with a different correlationId is ignored', async () => {
  const cursor = await lastId(REQUEST_STREAM);
  const artifactId = crypto.randomUUID();
  const helperPromise = runHelper(['proj-alpha', artifactId, 'designs/mockup.png', '--timeout-ms', '6000']);
  const request = await awaitRequestEnvelope(cursor, artifactId);

  // A decoy answer to somebody else's request, published first.
  await publishResponse({
    correlationId: `msg-${crypto.randomUUID()}`, status: 'delivered', path: 'WRONG/PATH.png',
    action: 'copied', deliveredAt: new Date().toISOString(),
    artifactId, destinationRepo: 'proj-alpha', requestedBy: 'test-project',
  });
  // The real answer, published second.
  await publishResponse({
    correlationId: request.messageId, status: 'delivered', path: 'designs/mockup.png',
    action: 'copied', deliveredAt: new Date().toISOString(),
    artifactId, destinationRepo: 'proj-alpha', requestedBy: 'test-project',
  });

  const result = await helperPromise;
  assert.equal(result.code, 0);
  assert.equal(result.stdout.trim(), 'designs/mockup.png');
});

test('a failed reply exits non-zero and prints the reason and detail to stderr', async () => {
  const cursor = await lastId(REQUEST_STREAM);
  const artifactId = crypto.randomUUID();
  const helperPromise = runHelper(['proj-alpha', artifactId, 'designs/mockup.png', '--timeout-ms', '5000']);
  const request = await awaitRequestEnvelope(cursor, artifactId);

  await publishResponse({
    correlationId: request.messageId, status: 'failed',
    reason: 'unknown_artifact', detail: `no artifact registered under id "${artifactId}"`,
    artifactId, destinationRepo: 'proj-alpha', requestedBy: 'test-project',
  });

  const result = await helperPromise;
  assert.equal(result.code, 1);
  assert.equal(result.stdout, '');
  assert.match(result.stderr, /unknown_artifact/);
  assert.match(result.stderr, new RegExp(artifactId));
});

test('times out and exits non-zero when no response ever arrives', async () => {
  const artifactId = crypto.randomUUID();
  const result = await runHelper(['proj-alpha', artifactId, 'designs/mockup.png', '--timeout-ms', '400']);
  assert.equal(result.code, 1);
  assert.equal(result.stdout, '');
  assert.match(result.stderr, /Timed out/i);
});

test('reads the request from stdin as JSON when the second argument is "-"', async () => {
  const cursor = await lastId(REQUEST_STREAM);
  const artifactId = crypto.randomUUID();
  const stdinRequest = JSON.stringify({ artifactId, requestedPath: 'from-stdin.txt', taskId: 'GANG-9' });

  const helperPromise = new Promise(resolve => {
    const env = { ...process.env, REDIS_HOST: '127.0.0.1', REDIS_PORT: '16399', PROJECT_NAME: 'test-project' };
    const child = execFile('node', [HELPER_PATH, 'proj-alpha', '-'], { env, timeout: 15000 }, (err, stdout, stderr) => {
      resolve({ code: err ? (typeof err.code === 'number' ? err.code : 1) : 0, stdout, stderr });
    });
    child.stdin.write(stdinRequest);
    child.stdin.end();
  });

  const request = await awaitRequestEnvelope(cursor, artifactId);
  assert.equal(request.payload.artifactId, artifactId);
  assert.equal(request.payload.requestedPath, 'from-stdin.txt');
  assert.equal(request.payload.taskId, 'GANG-9');

  await publishResponse({
    correlationId: request.messageId, status: 'delivered', path: 'from-stdin.txt',
    action: 'already_present', deliveredAt: new Date().toISOString(),
    artifactId, destinationRepo: 'proj-alpha', requestedBy: 'test-project', taskId: 'GANG-9',
  });

  const result = await helperPromise;
  assert.equal(result.code, 0);
  assert.equal(result.stdout.trim(), 'from-stdin.txt');
});

test('exits non-zero with a usage message when required arguments are missing', async () => {
  const result = await runHelper(['proj-alpha']);
  assert.equal(result.code, 1);
  assert.match(result.stderr, /Usage: request-artifact\.js/);
});

test('exits non-zero with a usage message for an unrecognized flag', async () => {
  const result = await runHelper(['proj-alpha', crypto.randomUUID(), 'a.txt', '--bogus-flag']);
  assert.equal(result.code, 1);
  assert.match(result.stderr, /Unrecognized argument/);
});

test('exits non-zero with a usage message for a non-numeric --timeout-ms', async () => {
  const result = await runHelper(['proj-alpha', crypto.randomUUID(), 'a.txt', '--timeout-ms', 'soon']);
  assert.equal(result.code, 1);
  assert.match(result.stderr, /--timeout-ms requires a numeric value/);
});
