'use strict';

const { test, before, after, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const { createClient } = require('redis');
const streams = require('../src/streams');
const { buildEnvelope, KIND } = require('../src/envelope');

const REDIS_URL = process.env.REDIS_TEST_URL || 'redis://localhost:16399';
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

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

async function waitFor(predicate, { timeoutMs = 3000, intervalMs = 25 } = {}) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await predicate()) return;
    await sleep(intervalMs);
  }
  throw new Error('waitFor timed out');
}

function envelope(overrides = {}) {
  return buildEnvelope({
    kind: KIND.TASK,
    project: 'hello-world',
    taskId: 'HW-1',
    contextId: 'HW-1',
    payload: { hello: 'world' },
    ...overrides,
  });
}

test('publish is idempotent when a dedupeKey is reused', async () => {
  const stream = 'test:pub';
  const e1 = envelope();
  const r1 = await streams.publish(client, stream, e1, { dedupeKey: 'dk-1' });
  assert.equal(r1.deduped, false);

  const e2 = envelope(); // different messageId, same logical dispatch
  const r2 = await streams.publish(client, stream, e2, { dedupeKey: 'dk-1' });
  assert.equal(r2.deduped, true);

  assert.equal(await client.xLen(stream), 1);
});

test('ensureGroup is safe to call twice (BUSYGROUP swallowed)', async () => {
  const stream = 'test:group';
  await streams.ensureGroup(client, stream, 'g');
  await streams.ensureGroup(client, stream, 'g'); // must not throw
  const groups = await client.xInfoGroups(stream);
  assert.equal(groups.length, 1);
});

test('ensureGroup starting at 0 sees entries added before the group existed', async () => {
  const stream = 'test:pre-existing';
  await streams.publish(client, stream, envelope()); // no group yet
  await streams.ensureGroup(client, stream, 'g');
  const pending = await client.xReadGroup('g', 'c1', [{ key: stream, id: '>' }], { COUNT: 10, BLOCK: 200 });
  assert.equal(pending.length, 1);
  assert.equal(pending[0].messages.length, 1);
});

test('a poison (invalid) message is dead-lettered without ever reaching the handler', async () => {
  const stream = 'test:poison';
  await client.xAdd(stream, '*', { data: 'not valid json at all' });

  let handlerCalls = 0;
  const consumer = streams.createConsumer(client, {
    stream,
    group: 'g',
    consumerName: 'c1',
    handler: async () => { handlerCalls += 1; },
    blockMs: 100,
  });
  await consumer.start();
  try {
    await waitFor(async () => (await client.xLen(streams.deadLetterStreamName(stream))) === 1);
  } finally {
    await consumer.stop();
  }

  assert.equal(handlerCalls, 0);
  const dead = await client.xRange(streams.deadLetterStreamName(stream), '-', '+');
  const record = JSON.parse(dead[0].message.data);
  assert.equal(record.reason, 'invalid_envelope');
});

test('createConsumer processes an entry and acks it on success', async () => {
  const stream = 'test:consume-ok';
  await streams.publish(client, stream, envelope());

  const seen = [];
  const consumer = streams.createConsumer(client, {
    stream,
    group: 'g',
    consumerName: 'c1',
    handler: async e => { seen.push(e.taskId); },
    blockMs: 100,
  });
  await consumer.start();

  try {
    await waitFor(() => seen.length === 1);
    await waitFor(async () => (await client.xPending(stream, 'g')).pending === 0);
  } finally {
    await consumer.stop();
  }
  assert.deepEqual(seen, ['HW-1']);
});

test('a permanent handler error dead-letters immediately without burning retries', async () => {
  const stream = 'test:permanent';
  await streams.publish(client, stream, envelope());

  let attempts = 0;
  const consumer = streams.createConsumer(client, {
    stream,
    group: 'g',
    consumerName: 'c1',
    handler: async () => {
      attempts += 1;
      const err = new Error('unauthorized destination');
      err.permanent = true;
      throw err;
    },
    blockMs: 100,
    retryDelayMs: 50,
    maxAttempts: 5,
  });
  await consumer.start();

  try {
    await waitFor(async () => (await client.xLen(streams.deadLetterStreamName(stream))) === 1);
  } finally {
    await consumer.stop();
  }

  assert.equal(attempts, 1);
  const dead = await client.xRange(streams.deadLetterStreamName(stream), '-', '+');
  const record = JSON.parse(dead[0].message.data);
  assert.equal(record.reason, 'unauthorized destination');
  assert.equal(record.attempts, 1);
});

test('retry exhaustion dead-letters after maxAttempts transient failures', async () => {
  const stream = 'test:retry-exhaust';
  await streams.publish(client, stream, envelope());

  let attempts = 0;
  const consumer = streams.createConsumer(client, {
    stream,
    group: 'g',
    consumerName: 'c1',
    handler: async () => { attempts += 1; throw new Error('transient'); },
    blockMs: 100,
    retryDelayMs: 50,
    reclaimIntervalMs: 60,
    maxAttempts: 3,
  });
  await consumer.start();

  try {
    await waitFor(async () => (await client.xLen(streams.deadLetterStreamName(stream))) === 1, { timeoutMs: 5000 });
  } finally {
    await consumer.stop();
  }

  assert.equal(attempts, 3);
});

test('a causally deferred entry does not consume its retry budget', async () => {
  const stream = 'test:causal-defer';
  await streams.publish(client, stream, envelope());

  let calls = 0;
  const consumer = streams.createConsumer(client, {
    stream,
    group: 'g',
    consumerName: 'c1',
    handler: async () => {
      calls += 1;
      if (calls < 4) {
        const err = new Error('predecessor still processing');
        err.retryWithoutAttempt = true;
        throw err;
      }
    },
    blockMs: 100,
    retryDelayMs: 20,
    reclaimIntervalMs: 30,
    maxAttempts: 1,
  });
  await consumer.start();

  try {
    await waitFor(async () => calls === 4 && (await client.xPending(stream, 'g')).pending === 0, { timeoutMs: 5000 });
  } finally {
    await consumer.stop();
  }

  assert.equal(calls, 4, 'the entry must survive more deferrals than maxAttempts');
  assert.equal(await client.xLen(streams.deadLetterStreamName(stream)), 0);
  const health = await streams.health(client, stream, 'g');
  assert.equal(health.retryCount, 0);
});

test('replay resubmits a dead-lettered entry onto its original stream', async () => {
  const stream = 'test:replay';
  const original = envelope();
  await streams.publish(client, stream, original);

  const consumer = streams.createConsumer(client, {
    stream,
    group: 'g',
    consumerName: 'c1',
    handler: async () => { throw new Error('bad'); },
    blockMs: 100,
    retryDelayMs: 20,
    reclaimIntervalMs: 30,
    maxAttempts: 1,
  });
  await consumer.start();
  try {
    await waitFor(async () => (await client.xLen(streams.deadLetterStreamName(stream))) === 1);
  } finally {
    await consumer.stop();
  }

  const deadEntries = await client.xRange(streams.deadLetterStreamName(stream), '-', '+');
  const replayed = await streams.replay(client, streams.deadLetterStreamName(stream), deadEntries[0].id);

  assert.equal(replayed.stream, stream);
  assert.equal(replayed.envelope.messageId, original.messageId);
  assert.equal(replayed.envelope.correlationId, original.messageId);
  assert.equal(await client.xLen(stream), 2); // original (now dead-lettered+acked) + replay
});

test('trimAcknowledged removes old entries but never one still pending', async () => {
  const stream = 'test:trim';
  await streams.ensureGroup(client, stream, 'g');

  // An "old" acknowledged entry (id timestamped far in the past) and a
  // recent one still pending in the group. xAck is a no-op on an entry
  // that was never delivered, so '1-1' must be read via the group first.
  await client.xAdd(stream, '1-1', { data: 'old-acked' });
  await client.xReadGroup('g', 'c1', [{ key: stream, id: '>' }], { COUNT: 10 });
  await client.xAck(stream, 'g', '1-1');
  const recentId = `${Date.now()}-0`;
  await client.xAdd(stream, recentId, { data: 'recent-pending' });
  await client.xReadGroup('g', 'c1', [{ key: stream, id: '>' }], { COUNT: 10 }); // deliver, don't ack

  await streams.trimAcknowledged(client, stream, 'g', 1000); // 1s retention — "old" entry is ancient
  const remaining = await client.xRange(stream, '-', '+');
  assert.deepEqual(remaining.map(e => e.id), [recentId], 'pending entry must survive trimming regardless of age');
});

test('trimDeadLetters removes dead-letter entries older than the retention window', async () => {
  const stream = 'test:trim-dead';
  const dead = streams.deadLetterStreamName(stream);
  await client.xAdd(dead, '1-1', { data: 'ancient' });
  const recentId = `${Date.now()}-0`;
  await client.xAdd(dead, recentId, { data: 'recent' });

  await streams.trimDeadLetters(client, stream, 1000);
  const remaining = await client.xRange(dead, '-', '+');
  assert.deepEqual(remaining.map(e => e.id), [recentId]);
});

test('health/classifyHealth report pending and dead-letter counts', async () => {
  const stream = 'test:health';
  await streams.ensureGroup(client, stream, 'g');
  await streams.publish(client, stream, envelope());

  const before1 = await streams.health(client, stream, 'g');
  assert.equal(before1.length, 1);
  assert.equal(before1.undeliveredCount, 1); // published, never read by this group yet
  assert.equal(before1.retryCount, 0);
  assert.equal(streams.classifyHealth(before1), 'healthy');

  // Manually push a dead-letter entry to exercise the degraded threshold.
  await client.xAdd(streams.deadLetterStreamName(stream), '*', { data: JSON.stringify({ reason: 'x' }) });
  const after1 = await streams.health(client, stream, 'g');
  assert.equal(after1.deadLetterCount, 1);
  assert.equal(streams.classifyHealth(after1), 'degraded');
});
