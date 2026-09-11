'use strict';

const { createClient } = require('redis');

const REDIS_URL = `redis://${process.env.REDIS_HOST || 'localhost'}:${process.env.REDIS_PORT || 6379}`;

// A single shared connection. Redis Streams commands (XADD, XREADGROUP,
// XACK, ...) don't need the dedicated subscribe-only connection Pub/Sub
// required — this replaces the old publisher/subscriber pair (no
// PUBLISH/SUBSCRIBE/PSUBSCRIBE in normal operation).
let client;

async function connect() {
  client = createClient({ url: REDIS_URL });
  client.on('error', err => console.error('[redis] Client error:', err));
  await client.connect();
  console.log('[redis] Connected to', REDIS_URL);
}

function getClient() {
  if (!client) throw new Error('[redis] getClient() called before connect()');
  return client;
}

// Claim a one-time dedup key. Returns true the first time a given key is
// seen within ttlSeconds, false on every subsequent call — used for
// domain-level dedup (e.g. "have I already dispatched a retry for this
// (ticket, build) pair") that is independent of any single envelope's
// messageId. See idempotency.js for the envelope-messageId-keyed variant.
async function acquireOnce(key, ttlSeconds) {
  const result = await getClient().set(key, '1', { NX: true, EX: ttlSeconds });
  return result === 'OK';
}

module.exports = { connect, getClient, acquireOnce };
