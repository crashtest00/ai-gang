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

module.exports = { connect, getClient };
