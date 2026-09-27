#!/usr/bin/env node
'use strict';

/**
 * Gateway publish helper — replaces `redis-cli publish jira-gateway:$PROJECT`
 * in agent prompts with a supported Streams gateway command/helper.
 *
 * Usage:
 *   node /agent-docs/commons/tools/gateway-publish.js <project-name> <path-to-json-file>
 *   node /agent-docs/commons/tools/gateway-publish.js <project-name> -   (read JSON from stdin)
 *
 * This is the raw entry point: it takes a message the producer already holds,
 * which is Jenkins' case (setup/Jenkinsfile.template's pipeline-failure
 * handler). An agent names an operation and its fields instead, through
 * a2a-submit.js, and authors no message.
 *
 * Wraps the raw payload in the versioned envelope and durably XADDs
 * it to aigang:gateway:{project}. Exits non-zero (and prints to stderr) on
 * any failure — the caller should treat a non-zero exit as "the gateway
 * operation was NOT durably accepted" and retry.
 *
 * a2a-validate.js runs on the payload before the connection is opened, so a
 * message the gateway would refuse only after it was durably written is
 * refused here instead, naming the failing field(s) on stderr in the same call
 * the producer made (V5.0 Deterministic Gateway Message Tooling REQ-01,
 * REQ-02, REQ-03).
 *
 * The normal V1 payload shape is the canonical A2A submission documented in
 * SCRUMMASTER_SPEC_v1.md:
 *   { state, message: { taskId, contextId, ... }, artifacts?: [...] }
 * taskId/contextId for the transport envelope come from that message —
 * never from agent-supplied top-level fields, so an agent cannot misroute a
 * submission by writing a different id at the top level. The legacy
 * ticket_key/parent_ticket_key/parentJiraIssueKey fallbacks below exist only
 * for the materializeDecomposition operation, which
 * defines its own structured-data contract outside A2A Message shape.
 */

const fs = require('fs');
const { createClient } = require('redis');
const { buildEnvelope, KIND, toStreamFields } = require('./envelope');
const { validateGatewayPayload, GatewayValidationError } = require('./a2a-validate');

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

  // Before the envelope, before the connection, before any XADD.
  try {
    validateGatewayPayload(payload);
  } catch (err) {
    if (!(err instanceof GatewayValidationError)) throw err;
    console.error(`[gateway-publish] Refused: this ${err.kind} payload is not valid, so nothing was published:`);
    for (const problem of err.errors) console.error(`  - ${problem}`);
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
