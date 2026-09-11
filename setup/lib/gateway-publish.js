#!/usr/bin/env node
'use strict';

/**
 * Gateway publish helper — replaces `redis-cli publish jira-gateway:$PROJECT`
 * in agent prompts (redis-streams.md: "Replace Pub/Sub commands in generated
 * prompts and agent instructions with a supported Streams gateway
 * command/helper").
 *
 * Usage:
 *   node /agent-docs/lib/gateway-publish.js <project-name> <path-to-json-file>
 *   node /agent-docs/lib/gateway-publish.js <project-name> -   (read JSON from stdin)
 *
 * Wraps the agent's raw payload in the versioned envelope and durably XADDs
 * it to aigang:gateway:{project}. Exits non-zero (and prints to stderr) on
 * any failure — the agent should treat a non-zero exit as "the gateway
 * operation was NOT durably accepted" and retry.
 *
 * The normal V1 payload shape is the canonical A2A submission documented in
 * SCRUMMASTER_SPEC_v1.md and the a2a-messaging design:
 *   { state, message: { taskId, contextId, ... }, artifacts?: [...] }
 * taskId/contextId for the transport envelope come from that message —
 * never from agent-supplied top-level fields, so an agent cannot misroute a
 * submission by writing a different id at the top level. The legacy
 * ticket_key/parent_ticket_key/parentJiraIssueKey fallbacks below exist only
 * for the dependency-handling.md materializeDecomposition operation, which
 * defines its own structured-data contract outside A2A Message shape.
 */

const fs = require('fs');
const { createClient } = require('redis');
const { buildEnvelope, KIND, toStreamFields } = require('./envelope');

async function main() {
  const [, , projectArg, pathArg] = process.argv;

  if (!projectArg || !pathArg) {
    console.error('Usage: gateway-publish.js <project-name> <path-to-json-file|->');
    process.exit(1);
  }

  const raw = pathArg === '-'
    ? fs.readFileSync(0, 'utf8')
    : fs.readFileSync(pathArg, 'utf8');

  let payload;
  try {
    payload = JSON.parse(raw);
  } catch (err) {
    console.error('Invalid JSON payload:', err.message);
    process.exit(1);
  }

  const project = projectArg.toLowerCase();
  const stream = `aigang:gateway:${project}`;

  const envelope = buildEnvelope({
    kind: KIND.JIRA_OPERATION,
    project,
    taskId: payload.message?.taskId || payload.ticket_key || payload.parent_ticket_key || payload.parentJiraIssueKey || null,
    contextId: payload.message?.contextId || null,
    payload,
  });

  const redisUrl = `redis://${process.env.REDIS_HOST || 'localhost'}:${process.env.REDIS_PORT || 6379}`;
  const client = createClient({ url: redisUrl });
  client.on('error', err => console.error('[gateway-publish] Redis error:', err.message));

  try {
    await client.connect();
    const entryId = await client.xAdd(stream, '*', toStreamFields(envelope));
    console.log(`Accepted: ${stream} entry ${entryId} (messageId=${envelope.messageId})`);
  } catch (err) {
    console.error('[gateway-publish] Failed to durably enqueue:', err.message);
    process.exitCode = 1;
  } finally {
    await client.quit().catch(() => {});
  }
}

main();
