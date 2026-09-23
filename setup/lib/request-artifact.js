#!/usr/bin/env node
'use strict';

/**
 * Artifact request helper — asks the librarian (V4 artifact custody,
 * strategy/v4.0/features/librarian.md, Management repository) for one
 * artifact by canonical id and blocks until it answers.
 *
 * Usage:
 *   node /agent-docs/lib/request-artifact.js <project-name> <artifact-id> <requested-path> [--task-id <id>] [--timeout-ms <ms>]
 *   node /agent-docs/lib/request-artifact.js <project-name> -   (read a JSON request from stdin:
 *     { "artifactId": "...", "requestedPath": "...", "taskId": "..." (optional), "timeoutMs": 30000 (optional) })
 *
 * There is no listing, search or browse: this takes exactly one artifact id
 * the caller was already given (librarian/README.md, "no search, no
 * listing and no browse").
 *
 * Follows librarian/README.md's read sequence exactly:
 *   1. Record aigang:librarian:responses' current last entry id, BEFORE
 *      publishing, so an answer that arrives immediately cannot be missed.
 *   2. XADD the request envelope onto aigang:librarian:requests.
 *   3. XREAD BLOCK on aigang:librarian:responses in a loop, advancing past
 *      every entry read, until one arrives whose correlationId equals the
 *      request's own messageId — a value this script mints itself, so it
 *      knows what to look for before it publishes.
 *
 * `destinationRepo` is <project-name> exactly as given — never lowercased,
 * unlike the gateway stream name gateway-publish.js derives from the same
 * argument. It must match the project's own directory name on disk under
 * the projects root (librarian/README.md, "Repositories").
 *
 * `requestedBy` is always $AGENT_DISPLAY_NAME, falling back to
 * $PROJECT_NAME — the same identity subscriber.js already reports agent
 * task-status updates under (setup/subscriber.js's AGENT_DISPLAY_NAME).
 * There is no flag to override it: the librarian records it as "who is
 * asking", which is the invoking agent, not a value the request should be
 * free to spoof.
 *
 * On `delivered`, prints the path the artifact occupies — relative to
 * /workspace, and possibly not the requested path, per the collision rule
 * (librarian/README.md REQ-04) — and exits 0. On `failed`, prints
 * "<reason>: <detail>" to stderr and exits 1. On timeout, prints a message
 * to stderr and exits 1. A non-zero exit always means no path was printed
 * on stdout — a caller can rely on that pairing without parsing stderr.
 */

const fs = require('fs');
const { createClient } = require('redis');
const { buildEnvelope, KIND, toStreamFields, fromStreamFields } = require('./envelope');

const REQUEST_STREAM = 'aigang:librarian:requests';
const RESPONSE_STREAM = 'aigang:librarian:responses';
const INSTANCE_SCOPE = '_instance'; // librarian/README.md — these streams are instance-wide.

// Sensible default: local delivery normally completes in well under a
// second (a filesystem copy plus one Postgres transaction), so 30s leaves
// generous headroom for load without leaving an agent session blocked
// indefinitely on a librarian that has stopped answering.
const DEFAULT_TIMEOUT_MS = 30000;

// How long each XREAD BLOCK call waits before this script re-checks the
// overall deadline. Short enough that the deadline is honoured promptly;
// long enough not to busy-loop.
const POLL_BLOCK_MS = 2000;

function usageError(message) {
  console.error(message);
  console.error('Usage: request-artifact.js <project-name> <artifact-id> <requested-path> [--task-id <id>] [--timeout-ms <ms>]');
  console.error('   or: request-artifact.js <project-name> -   (read a JSON request from stdin)');
  process.exit(1);
}

function parseArgs(argv) {
  const projectArg = argv[2];
  const second = argv[3];

  if (!projectArg || !second) {
    usageError('project-name and artifact-id (or -) are required');
  }

  if (second === '-') {
    let payload;
    try {
      payload = JSON.parse(fs.readFileSync(0, 'utf8'));
    } catch (err) {
      usageError(`Invalid JSON request on stdin: ${err.message}`);
    }
    if (!payload.artifactId || !payload.requestedPath) {
      usageError('stdin JSON request must include artifactId and requestedPath');
    }
    return {
      project: projectArg,
      artifactId: String(payload.artifactId),
      requestedPath: String(payload.requestedPath),
      taskId: payload.taskId != null ? String(payload.taskId) : null,
      timeoutMs: payload.timeoutMs != null ? Number(payload.timeoutMs) : DEFAULT_TIMEOUT_MS,
    };
  }

  const requestedPath = argv[4];
  if (!requestedPath) usageError('requested-path is required');

  let taskId = null;
  let timeoutMs = DEFAULT_TIMEOUT_MS;
  for (let i = 5; i < argv.length; i++) {
    if (argv[i] === '--task-id') {
      taskId = argv[++i];
      if (taskId === undefined) usageError('--task-id requires a value');
    } else if (argv[i] === '--timeout-ms') {
      const raw = argv[++i];
      timeoutMs = Number(raw);
      if (!raw || Number.isNaN(timeoutMs)) usageError('--timeout-ms requires a numeric value');
    } else {
      usageError(`Unrecognized argument: ${argv[i]}`);
    }
  }

  return { project: projectArg, artifactId: String(second), requestedPath: String(requestedPath), taskId, timeoutMs };
}

// The response stream's current last entry id, or '0-0' if the stream does
// not exist yet — recorded BEFORE publishing (librarian/README.md step 1).
async function lastResponseId(client) {
  try {
    const info = await client.xInfoStream(RESPONSE_STREAM);
    return info['last-generated-id'] || '0-0';
  } catch {
    return '0-0';
  }
}

// Block on the response stream until an entry whose correlationId matches
// messageId arrives, or the deadline passes. Returns the matching envelope,
// or null on timeout.
async function awaitResponse(client, cursor, messageId, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const blockMs = Math.max(1, Math.min(POLL_BLOCK_MS, deadline - Date.now()));
    const result = await client.xRead({ key: RESPONSE_STREAM, id: cursor }, { COUNT: 20, BLOCK: blockMs });
    if (!result) continue;
    for (const { messages } of result) {
      for (const { id, message } of messages) {
        cursor = id;
        const envelope = fromStreamFields(message);
        if (envelope && envelope.correlationId === messageId) {
          return envelope;
        }
      }
    }
  }
  return null;
}

async function main() {
  const { project, artifactId, requestedPath, taskId, timeoutMs } = parseArgs(process.argv);

  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    usageError('timeout must be a positive number of milliseconds');
  }

  const requestedBy = process.env.AGENT_DISPLAY_NAME || process.env.PROJECT_NAME;
  if (!requestedBy) {
    usageError('requestedBy could not be determined: set $AGENT_DISPLAY_NAME or $PROJECT_NAME');
  }

  const payload = {
    requestedBy,
    artifactId,
    destinationRepo: project,
    requestedPath,
  };
  if (taskId) payload.taskId = taskId;

  const envelope = buildEnvelope({
    kind: KIND.ARTIFACT_DELIVERY_REQUEST,
    project: INSTANCE_SCOPE,
    taskId: taskId || null,
    payload,
  });

  const redisUrl = `redis://${process.env.REDIS_HOST || 'localhost'}:${process.env.REDIS_PORT || 6379}`;
  const client = createClient({ url: redisUrl });
  client.on('error', err => console.error('[request-artifact] Redis error:', err.message));

  try {
    await client.connect();

    const cursor = await lastResponseId(client);
    await client.xAdd(REQUEST_STREAM, '*', toStreamFields(envelope));

    const response = await awaitResponse(client, cursor, envelope.messageId, timeoutMs);

    if (!response) {
      console.error(`[request-artifact] Timed out after ${timeoutMs}ms waiting for the librarian's answer (messageId=${envelope.messageId}). The request may still be pending — retry, or ask again with a longer --timeout-ms.`);
      process.exitCode = 1;
      return;
    }

    const answer = response.payload || {};
    if (answer.status === 'delivered') {
      console.log(answer.path);
      process.exitCode = 0;
    } else if (answer.status === 'failed') {
      console.error(`${answer.reason}: ${answer.detail}`);
      process.exitCode = 1;
    } else {
      console.error(`[request-artifact] Unexpected response status: ${JSON.stringify(answer.status)}`);
      process.exitCode = 1;
    }
  } catch (err) {
    console.error('[request-artifact] Failed:', err.message);
    process.exitCode = 1;
  } finally {
    await client.quit().catch(() => {});
  }
}

main();
