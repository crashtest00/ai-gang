'use strict';

// BF-08 — two operational findings a dead-export sweep surfaced
// (../../../../aigang-mgmt/strategy/v5.0/v5.1/BUGFIXES.md, "BF-08"):
//
//   1. services/scrummaster has no SIGTERM/SIGINT handler anywhere, so
//      gateway.js's stopGatewaySubscriber and dispatchConsumer.js's
//      stopDispatchConsumers are unreachable in production and Redis
//      consumer groups are never released on shutdown.
//   2. core's command stream (aigang:workitems:{project}) is never
//      trimmed — core's trim_acknowledged/trim_dead_letters have no
//      caller, and ScrumMaster's own retention list didn't know about
//      core's stream.
//
// Both findings are fixed in src/index.js alone (SIGTERM/SIGINT wired to
// the existing stop functions; everyStreamGroup() gains core's command
// stream, group "core", with the existing defaults). Per the build's gate
// 1 ("every REQ whose acceptance is 'X happens in the real dispatch or
// production path' is tested through that path"), both tests below spawn
// the real entry point (src/index.js) as its own process — the real
// signal path and the real startup-time retention run — rather than
// calling scheduleRetention()/the shutdown logic as test-only helpers.
// Neither is exported from index.js (it has no module.exports at all), so
// this is also the only way to exercise them.

const { test, before, after, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const net = require('node:net');
const { spawn } = require('node:child_process');
const { createClient } = require('redis');

const REDIS_HOST = process.env.REDIS_TEST_HOST || 'localhost';
const REDIS_PORT = process.env.REDIS_TEST_PORT || '16399';
const REDIS_URL = `redis://${REDIS_HOST}:${REDIS_PORT}`;

const INDEX_PATH = path.join(__dirname, '..', 'src', 'index.js');
const CONFIG_DIR = path.join(__dirname, '..', 'config');

// Two of config/projects.json's three projects, used for independent
// aspects of the retention check so neither test's setup masks the other's
// (a pending entry protected under group "core" widens the retained
// window past the plain cutoff for that whole stream, so the plain
// 7-day-cutoff behaviour is checked on a different project's stream).
const PROJECT_A = 'hello-world';
const PROJECT_B = 'hello-desktop';
const COMMAND_STREAM_A = `aigang:workitems:${PROJECT_A}`;
const COMMAND_STREAM_B = `aigang:workitems:${PROJECT_B}`;
const DEAD_LETTER_STREAM_B = `${COMMAND_STREAM_B}:dead`;
const CORE_GROUP = 'core';

const DAY_MS = 24 * 60 * 60 * 1000;

function idFromAge(ageMs) {
  return `${Date.now() - ageMs}-0`;
}

function getFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on('error', reject);
    srv.listen(0, () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function waitForOutput(child, pattern, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    let out = '';
    const onData = chunk => {
      out += chunk;
      if (pattern.test(out)) {
        clearTimeout(timer);
        child.stdout.removeListener('data', onData);
        resolve(out);
      }
    };
    const timer = setTimeout(() => {
      child.stdout.removeListener('data', onData);
      reject(new Error(`timed out waiting for ${pattern}; stdout so far:\n${out}`));
    }, timeoutMs);
    child.stdout.on('data', onData);
  });
}

function waitForExit(child, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('timed out waiting for process exit')), timeoutMs);
    child.once('exit', (code, signal) => {
      clearTimeout(timer);
      resolve({ code, signal });
    });
  });
}

// Spawns the real production entry point against the real test Redis,
// using the shipped config (the same AGENTS_CATALOG_PATH/PROJECTS_CONFIG_PATH
// every other integration test in this directory points at). Returns once
// main() has reached app.listen(), i.e. registry.load(), connect(),
// bootstrapStreams(), startGatewaySubscriber(), startDispatchConsumers()
// and the first scheduleRetention() pass have all been reached.
async function spawnScrummaster(extraEnv = {}) {
  const port = await getFreePort();
  const child = spawn(process.execPath, [INDEX_PATH], {
    env: {
      ...process.env,
      REDIS_HOST,
      REDIS_PORT,
      AGENTS_CATALOG_PATH: path.join(CONFIG_DIR, 'agents.json'),
      PROJECTS_CONFIG_PATH: path.join(CONFIG_DIR, 'projects.json'),
      PORT: String(port),
      ...extraEnv,
    },
  });
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', c => { stdout += c; });
  child.stderr.on('data', c => { stderr += c; });
  await waitForOutput(child, /Listening on port/);
  return { child, getStdout: () => stdout, getStderr: () => stderr };
}

// Always kill the child, even if an assertion above throws, so a failing
// test doesn't leave a scrummaster process (and its Redis connections)
// running past the test run.
async function withScrummaster(extraEnv, fn) {
  const session = await spawnScrummaster(extraEnv);
  try {
    return await fn(session);
  } finally {
    if (session.child.exitCode === null && session.child.signalCode === null) {
      session.child.kill('SIGKILL');
    }
  }
}

async function waitFor(predicate, { timeoutMs = 5000, intervalMs = 50 } = {}) {
  const start = Date.now();
  for (;;) {
    if (await predicate()) return;
    if (Date.now() - start > timeoutMs) throw new Error('waitFor timed out');
    await new Promise(r => setTimeout(r, intervalMs));
  }
}

let client;

before(async () => {
  client = createClient({ url: REDIS_URL });
  await client.connect();
});

after(async () => {
  await client.quit();
});

beforeEach(async () => {
  await client.flushDb();
});

// --- Finding 1: SIGTERM/SIGINT release the gateway and dispatch consumers ---

for (const signal of ['SIGTERM', 'SIGINT']) {
  test(`${signal} stops the gateway subscriber and dispatch consumers through the real handler`, async () => {
    await withScrummaster({}, async ({ child, getStdout }) => {
      child.kill(signal);
      const { code, signal: killedBy } = await waitForExit(child);

      // Before this fix, neither SIGTERM nor SIGINT had a listener, so
      // Node's default disposition kills the process immediately: the
      // 'exit' event reports code === null and signal === the sent signal,
      // and stopGatewaySubscriber/stopDispatchConsumers never run. A
      // process that instead exits via its own process.exit(0), after
      // those two calls resolve, reports code === 0 and signal === null.
      assert.equal(killedBy, null, `process was killed by ${killedBy} instead of exiting through the handler`);
      assert.equal(code, 0);

      const stdout = getStdout();
      assert.match(stdout, new RegExp(`${signal} received, stopping gateway and dispatch consumers`));
      assert.match(stdout, /Shutdown complete/);
    });
  });
}

// --- Finding 2: core's command stream is trimmed with ScrumMaster's existing defaults ---

test('the startup retention run reaches core\'s command stream through group "core" specifically', async () => {
  // A single old entry (10 days), read but not acked by a "core" consumer —
  // this is what BF-08 describes core's own command_consumer.py doing in
  // production (stream_topology.py's COMMAND_GROUP = 'core'). trimAcknowledged
  // protects the oldest *pending* entry of the group it is told to check,
  // regardless of age, so this entry surviving the default 7-day cutoff is
  // proof the retention call reaching this stream named the group "core" —
  // the wrong group name (or none) would find no pending entry, fall back
  // to the plain cutoff, and remove it, since 10 days exceeds the 7-day
  // default.
  const pendingId = idFromAge(10 * DAY_MS);
  await client.xAdd(COMMAND_STREAM_A, pendingId, { data: JSON.stringify({ fake: 'old-pending-command' }) });
  await client.xGroupCreate(COMMAND_STREAM_A, CORE_GROUP, '0');
  await client.xReadGroup(CORE_GROUP, 'core-test-consumer', [{ key: COMMAND_STREAM_A, id: '>' }], { COUNT: 1 });

  // No STREAM_RETENTION_DAYS override — the stated default (7 days), not a
  // value this test chose.
  await withScrummaster({}, async () => {
    // Give the startup retention run (triggered unconditionally by every
    // scrummaster start, including the shutdown tests above) time to reach
    // every project's streams before asserting on this one.
    await new Promise(r => setTimeout(r, 2000));

    const commandIds = (await client.xRange(COMMAND_STREAM_A, '-', '+')).map(e => e.id);
    assert.ok(
      commandIds.includes(pendingId),
      'the oldest pending entry for group "core" on the command stream must be protected, not trimmed by age alone'
    );
  });
});

test('the startup retention run trims core\'s command stream (and its dead letters) under the existing 7/30-day defaults', async () => {
  // No group/pending entanglement here — this is the plain cutoff case,
  // isolated on a different project's command stream so the pending
  // protection above doesn't widen this one's retained window.
  const oldId = idFromAge(10 * DAY_MS);
  await client.xAdd(COMMAND_STREAM_B, oldId, { data: JSON.stringify({ fake: 'old-command' }) });
  const recentId = idFromAge(1 * DAY_MS);
  await client.xAdd(COMMAND_STREAM_B, recentId, { data: JSON.stringify({ fake: 'recent-command' }) });

  // Dead-letter side: past and within the default 30-day
  // DEAD_LETTER_RETENTION_DAYS cutoff.
  const oldDeadId = idFromAge(40 * DAY_MS);
  await client.xAdd(DEAD_LETTER_STREAM_B, oldDeadId, { data: JSON.stringify({ fake: 'old-dead-command' }) });
  const recentDeadId = idFromAge(1 * DAY_MS);
  await client.xAdd(DEAD_LETTER_STREAM_B, recentDeadId, { data: JSON.stringify({ fake: 'recent-dead-command' }) });

  // No STREAM_RETENTION_DAYS/DEAD_LETTER_RETENTION_DAYS override — the
  // stated defaults (7 days, 30 for dead letters), not values this test
  // chose.
  await withScrummaster({}, async () => {
    await waitFor(async () => {
      const ids = (await client.xRange(COMMAND_STREAM_B, '-', '+')).map(e => e.id);
      return !ids.includes(oldId);
    });

    const commandIds = (await client.xRange(COMMAND_STREAM_B, '-', '+')).map(e => e.id);
    assert.ok(!commandIds.includes(oldId), 'an old command-stream entry must be trimmed under the 7-day default');
    assert.ok(commandIds.includes(recentId), 'a recent command-stream entry must survive');

    const deadIds = (await client.xRange(DEAD_LETTER_STREAM_B, '-', '+')).map(e => e.id);
    assert.ok(!deadIds.includes(oldDeadId), 'a dead-letter entry past the 30-day default must be trimmed');
    assert.ok(deadIds.includes(recentDeadId), 'a recent dead-letter entry must survive');
  });
});
