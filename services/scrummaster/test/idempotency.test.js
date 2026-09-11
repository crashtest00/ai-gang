'use strict';

const { test, before, after, beforeEach } = require('node:test');
const assert = require('node:assert/strict');
const { createClient } = require('redis');
const idempotency = require('../src/idempotency');

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

test('claim returns true once then false for the same key', async () => {
  assert.equal(await idempotency.claim(client, 'ns', 'id-1', 60), true);
  assert.equal(await idempotency.claim(client, 'ns', 'id-1', 60), false);
});

test('recordOutcome/getOutcome round-trips JSON values, including null', async () => {
  assert.equal(await idempotency.getOutcome(client, 'ns', 'missing'), undefined);
  await idempotency.recordOutcome(client, 'ns', 'a', { subtaskKey: 'GANG-42' });
  assert.deepEqual(await idempotency.getOutcome(client, 'ns', 'a'), { subtaskKey: 'GANG-42' });
  await idempotency.recordOutcome(client, 'ns', 'b', null);
  assert.equal(await idempotency.getOutcome(client, 'ns', 'b'), null); // recorded-but-null, distinct from undefined
});

test('once() runs fn exactly once per (namespace, id)', async () => {
  let calls = 0;
  const fn = async () => { calls += 1; return 'result'; };

  const first = await idempotency.once(client, 'ns', 'op-1', fn);
  assert.deepEqual(first, { duplicate: false, outcome: 'result' });

  const second = await idempotency.once(client, 'ns', 'op-1', fn);
  assert.deepEqual(second, { duplicate: true, outcome: 'result' });

  assert.equal(calls, 1);
});

test('once() does not record an outcome when fn throws, so a retry re-runs it', async () => {
  let calls = 0;
  const flaky = async () => {
    calls += 1;
    if (calls === 1) throw new Error('boom');
    return 'succeeded-on-retry';
  };

  await assert.rejects(() => idempotency.once(client, 'ns', 'op-2', flaky), /boom/);
  assert.equal(calls, 1);

  const retried = await idempotency.once(client, 'ns', 'op-2', flaky);
  assert.deepEqual(retried, { duplicate: false, outcome: 'succeeded-on-retry' });
  assert.equal(calls, 2);
});
