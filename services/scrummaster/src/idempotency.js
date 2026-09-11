'use strict';

// Idempotency records backed by Redis keys. Used both by producers (has this
// logical message already been durably enqueued?) and consumers (has the
// external side effect for this messageId already been applied?).
//
// A record must survive process restart and outlive the maximum source
// retention/replay window (REQ-05, REQ-10 default 7d acknowledged / 30d
// dead-letter) — the default TTL here is 30 days so a single store covers
// both windows without a second configuration knob.
const DEFAULT_TTL_SECONDS = 30 * 24 * 60 * 60;

function keyFor(namespace, id) {
  return `aigang:idem:${namespace}:${id}`;
}

// Attempt to atomically claim an idempotency key. Returns true the first
// time a given (namespace, id) is claimed, false on every subsequent call
// (i.e. the caller is looking at a duplicate/redelivery and must not repeat
// the external side effect).
async function claim(client, namespace, id, ttlSeconds = DEFAULT_TTL_SECONDS) {
  const key = keyFor(namespace, id);
  const result = await client.set(key, String(Date.now()), { NX: true, EX: ttlSeconds });
  return result === 'OK';
}

// Check whether a key has already been claimed, without claiming it.
async function isClaimed(client, namespace, id) {
  const key = keyFor(namespace, id);
  const value = await client.get(key);
  return value !== null;
}

// Record the durable outcome associated with a previously claimed key, so a
// duplicate caller can retrieve what happened instead of only knowing "it
// already happened". Optional — callers that don't need the result value
// (most gateway operations) can skip this.
async function recordOutcome(client, namespace, id, outcome, ttlSeconds = DEFAULT_TTL_SECONDS) {
  const key = keyFor(namespace, id);
  await client.set(key, JSON.stringify(outcome), { EX: ttlSeconds });
}

async function getOutcome(client, namespace, id) {
  const key = keyFor(namespace, id);
  const value = await client.get(key);
  if (value === null) return undefined;
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

// Run `fn` exactly once per (namespace, id), across process restarts and
// redeliveries. The outcome is recorded only after `fn` resolves — if `fn`
// throws (a partial failure), nothing is recorded and a later retry runs
// `fn` again from scratch. This is the "did the whole operation already
// complete" fast path (REQ-04/REQ-05); callers with side effects that must
// also survive a partial-failure retry (e.g. a dispatch a handler makes
// partway through its own work) still need their own stable dedupeKey at
// that specific call site — see streams.publish's `dedupeKey` option.
async function once(client, namespace, id, fn) {
  const existing = await getOutcome(client, namespace, id);
  if (existing !== undefined) {
    return { duplicate: true, outcome: existing };
  }
  const outcome = await fn();
  await recordOutcome(client, namespace, id, outcome === undefined ? null : outcome);
  return { duplicate: false, outcome };
}

module.exports = { claim, isClaimed, recordOutcome, getOutcome, once, DEFAULT_TTL_SECONDS };
