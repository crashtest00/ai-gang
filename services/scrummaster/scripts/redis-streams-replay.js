#!/usr/bin/env node
'use strict';

/**
 * Operator tool: replay a dead-lettered Redis Stream entry
 * (the redis-streams design REQ-08).
 *
 * Run from the services/scrummaster/ directory (needs its node_modules):
 *   node scripts/redis-streams-replay.js <dead-letter-stream> <entry-id>
 *
 * Example — replay a failed gateway operation for hello-world:
 *   node scripts/redis-streams-replay.js aigang:gateway:hello-world:dead 1719000000000-0
 *
 * List dead-letter entries first with:
 *   docker exec ai-gang-redis redis-cli XRANGE aigang:gateway:hello-world:dead - +
 *
 * The replay is appended as a NEW entry on the original source stream with
 * the same messageId/taskId/contextId and correlationId set to that
 * messageId, so it passes normal validation and idempotency checks exactly
 * like any other delivery — this does not delete or modify the dead-letter
 * record, which remains for audit.
 */

const { createClient } = require('redis');
const streams = require('../src/streams');

async function main() {
  const [, , deadStream, entryId] = process.argv;
  if (!deadStream || !entryId) {
    console.error('Usage: node scripts/redis-streams-replay.js <dead-letter-stream> <entry-id>');
    process.exit(1);
  }

  const redisUrl = `redis://${process.env.REDIS_HOST || 'localhost'}:${process.env.REDIS_PORT || 6379}`;
  const client = createClient({ url: redisUrl });
  client.on('error', err => console.error('[replay] Redis error:', err.message));
  await client.connect();

  try {
    const result = await streams.replay(client, deadStream, entryId);
    console.log(`Replayed ${deadStream}#${entryId} -> ${result.stream}#${result.entryId}`);
    console.log(`messageId=${result.envelope.messageId} correlationId=${result.envelope.correlationId}`);
  } finally {
    await client.quit();
  }
}

main().catch(err => {
  console.error('[replay] Failed:', err.message);
  process.exit(1);
});
