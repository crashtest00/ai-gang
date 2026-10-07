#!/usr/bin/env node
'use strict';

/**
 * Container Redis Streams Consumer
 *
 * Runs inside a project container. Consumes durable task entries from this
 * container's agent-dispatch stream(s) and invokes Claude Code for each one.
 * Replaces the Pub/Sub subscriber's in-memory queue with Redis Streams
 * consumer-group semantics: a task survives a container restart, an
 * unacknowledged task is reclaimed and retried, and a terminal outcome is
 * always durably reported back on the gateway stream.
 *
 * Required env vars:
 *   PROJECT_NAME          — matches the canonical project name recorded on the
 *                           work items in `core` (e.g. "hello-world"), which is
 *                           what a task envelope's `project` carries and what
 *                           makeTaskHandler below compares it against. Not a
 *                           Jira project name: no part of a dispatch reads one
 *   REDIS_HOST            — hostname of the Redis container
 *   REDIS_PORT            — Redis port (default 6379)
 *   ANTHROPIC_API_KEY     — required by Claude Code when the platform's
 *                           authMethod is "api-key"
 *   CLAUDE_CODE_OAUTH_TOKEN — required instead when it is "oauth-token"
 *                           (a token from `claude setup-token`)
 *
 * Optional env vars:
 *   AGENT_CHANNEL_SUFFIX   — scope this container to one agent role (e.g.
 *                            "backend"). Consumes exactly
 *                            aigang:agent:{project}:{suffix}.
 *   AGENT_CHANNEL_SUFFIXES — comma-separated list of roles for a single
 *                            container handling multiple roles (e.g.
 *                            "refinement,backend,frontend,devops"). Each
 *                            listed stream is consumed explicitly — there is
 *                            no wildcard/pattern subscription.
 *                            Defaults to "refinement,backend,frontend,devops"
 *                            if neither this nor AGENT_CHANNEL_SUFFIX is set;
 *                            keep this in sync with services/scrummaster/config/agents.json.
 *   AGENT_DISPLAY_NAME     — label used in terminal task-status reports
 *                            (defaults to PROJECT_NAME).
 *   AGENT_MAX_ATTEMPTS     — must match ScrumMaster's per-stream maxAttempts
 *                            default (default 3).
 *
 * Tasks are processed serially across all consumed streams — if a message
 * arrives while Claude is running, it waits until the current session
 * completes before being picked up (same behavior as the previous in-memory
 * queue, now backed by pending Streams entries instead of process memory).
 *
 * Each dispatch runs from a per-dispatch snapshot of everything an agent reads
 * during its session — the commons, the per-role definitions and the two role
 * handbooks (see setup/dispatch-snapshot.js) — with every mount path the
 * session is given for one of them rewritten onto that snapshot, so no file an
 * agent reads can change under it. The whole dispatch context is in the
 * session's environment rather than only in its prompt — seven variables:
 * PROJECT_NAME, REDIS_HOST, A2A_TASK_ID, A2A_CONTEXT_ID, A2A_LAST_MESSAGE_ID,
 * AIGANG_COMMONS_DIR and AIGANG_COMMONS_VERSION — with the snapshot's `tools/`
 * first on PATH, so a tool is invoked by name.
 *
 * A kind=TASK envelope's `payload` is a canonical A2A Message ({ kind:
 * "message", messageId, taskId, contextId, role, parts, ... }). This
 * subscriber only needs the
 * text Part(s) as the Claude Code prompt; it does not otherwise parse or
 * validate the message.
 */

const { createClient } = require('redis');
const { execFile } = require('child_process');
const { buildEnvelope, KIND } = require('./commons/tools/envelope');
const { ensureGroup, createConsumer, publish: streamsPublish } = require('./commons/tools/streams');
const {
  createSnapshot,
  installSkills,
  sessionEnv,
  rewriteAgentPaths,
  removeSnapshot,
  pruneSnapshotRoot,
} = require('./dispatch-snapshot');

const PROJECT_NAME = process.env.PROJECT_NAME;
const REDIS_HOST = process.env.REDIS_HOST || 'localhost';
const REDIS_PORT = process.env.REDIS_PORT || 6379;
const AGENT_DISPLAY_NAME = process.env.AGENT_DISPLAY_NAME || PROJECT_NAME;
const MAX_ATTEMPTS = parseInt(process.env.AGENT_MAX_ATTEMPTS || '3', 10);
const DEFAULT_SUFFIXES = ['refinement', 'backend', 'frontend', 'devops'];

if (!PROJECT_NAME) {
  console.error('[subscriber] PROJECT_NAME env var is required');
  process.exit(1);
}

const NORMALIZED_PROJECT = PROJECT_NAME.toLowerCase();
const REDIS_URL = `redis://${REDIS_HOST}:${REDIS_PORT}`;
const GATEWAY_STREAM = `aigang:gateway:${NORMALIZED_PROJECT}`;

function agentStreamName(suffix) {
  return `aigang:agent:${NORMALIZED_PROJECT}:${suffix}`;
}

// Extract the Claude Code prompt text from a kind=TASK envelope's payload —
// a canonical A2A Message (the text Part(s) of `payload.parts`). The A2A
// Message, not a bare
// `.prompt` field, is the wire contract for the payload's content.
function extractPrompt(payload) {
  const parts = payload?.parts;
  if (!Array.isArray(parts)) return null;
  const text = parts.filter(p => p.kind === 'text').map(p => p.text).join('\n\n');
  return text || null;
}

function suffixList() {
  if (process.env.AGENT_CHANNEL_SUFFIX) return [process.env.AGENT_CHANNEL_SUFFIX];
  if (process.env.AGENT_CHANNEL_SUFFIXES) {
    return process.env.AGENT_CHANNEL_SUFFIXES.split(',').map(s => s.trim()).filter(Boolean);
  }
  return DEFAULT_SUFFIXES;
}

// Serialize Claude invocations across every stream this container consumes —
// preserves the original single-container behavior where only one task runs
// at a time, now enforced with an explicit queue instead of process-local
// pub/sub delivery order.
let chain = Promise.resolve();
function runSerially(fn) {
  const result = chain.then(fn, fn);
  chain = result.catch(() => {});
  return result;
}

async function publishTaskStatus(client, envelope, statusPayload) {
  const statusEnvelope = buildEnvelope({
    kind: KIND.TASK_STATUS,
    project: NORMALIZED_PROJECT,
    taskId: envelope.taskId,
    contextId: envelope.contextId,
    correlationId: envelope.messageId,
    payload: {
      // taskId is the work item's own canonical id in both modes (one
      // Task per work item for its whole lifecycle). `ticket_key` is a
      // generic field name that stays; it carries that canonical id, never
      // a Jira issue key (V5.1 REQ-06).
      ticket_key: envelope.taskId,
      agent_name: AGENT_DISPLAY_NAME,
      ...statusPayload,
    },
  });
  await streamsPublish(client, GATEWAY_STREAM, statusEnvelope);
}

// Run Claude Code for one task envelope. Returns { success, reason,
// diagnostic } — never throws, so the caller decides retry/terminal-status
// behavior explicitly rather than via exception flow.
function runClaude(envelope) {
  const prompt = extractPrompt(envelope.payload);
  return new Promise(resolve => {
    if (!prompt) {
      resolve({ success: false, reason: 'invalid_message', diagnostic: 'payload has no text Part' });
      return;
    }

    // Snapshot the mount for this dispatch before spawning the session, so a
    // `git pull` on the host cannot change a tool, a skill, a role definition
    // or a handbook under the running session, and stamp the session with the
    // snapshot's content hash. See setup/dispatch-snapshot.js.
    let snapshot;
    try {
      snapshot = createSnapshot();
      installSkills(snapshot);
    } catch (snapshotErr) {
      if (snapshot) {
        try { removeSnapshot(snapshot); } catch { /* best effort */ }
      }
      console.error(`[subscriber] Commons snapshot failed for ${envelope.taskId}: ${snapshotErr.message}`);
      resolve({ success: false, reason: 'commons_snapshot_failed', diagnostic: snapshotErr.message });
      return;
    }

    // ScrumMaster's prompt opens with the role definition's path, a fixed
    // `/agent-docs/agents/<role>.md` from its own config, and the definition it
    // points at names its handbook the same way. Both are snapshotted, so both
    // paths are rewritten onto this dispatch's snapshot — the definition and
    // the handbook inside `createSnapshot`, the prompt here — and the session
    // is given no path into the live mount for either (Agent Commons REQ-02,
    // Amendment 1).
    const sessionPrompt = rewriteAgentPaths(prompt, snapshot);

    console.log(`[subscriber] Starting Claude for ticket ${envelope.taskId}`);
    console.log(`[subscriber] Commons snapshot ${snapshot.commonsDir} version ${snapshot.version} for ${envelope.taskId}`);
    console.log(
      `[subscriber] Snapshot ${snapshot.dispatchDir} carries ${snapshot.entries.join(', ')}; `
      + `mount paths rewritten in the prompt: ${sessionPrompt === prompt ? 'none' : 'yes'}, `
      + `in ${snapshot.rewritten.length} snapshotted document(s)${snapshot.rewritten.length ? ` (${snapshot.rewritten.join(', ')})` : ''}`
    );

    // Pass the prompt via stdin to avoid shell escaping issues with complex prompts
    const child = execFile(
      'claude',
      ['--print', '--dangerously-skip-permissions', '-'],
      {
        env: {
          ...process.env,
          PROJECT_NAME,
          REDIS_HOST,
          A2A_TASK_ID: envelope.taskId,
          A2A_CONTEXT_ID: envelope.contextId,
          // The A2A message id of the dispatch message this session is
          // replying to. A kind=TASK envelope's `payload` *is* the canonical
          // A2A Message (see the header contract above), so the id is
          // payload.messageId — not envelope.messageId, which is the
          // transport id envelope.js mints for the stream entry.
          A2A_LAST_MESSAGE_ID: envelope.payload?.messageId,
          // PATH (snapshot tools first), AIGANG_COMMONS_DIR and
          // AIGANG_COMMONS_VERSION. Spread last so its PATH wins.
          ...sessionEnv(snapshot),
        },
        timeout: 30 * 60 * 1000, // 30 minute timeout per task
      },
      (err, _stdout, stderr) => {
        // The session has ended: the snapshot and any per-session state a tool
        // kept beside it go with it.
        try {
          removeSnapshot(snapshot);
        } catch (cleanupErr) {
          console.error(`[subscriber] Could not remove snapshot ${snapshot.dispatchDir}: ${cleanupErr.message}`);
        }
        // Bounded, best-effort diagnostic — not a guarantee that no secret
        // ever appears in agent stderr output; this truncates but does not
        // scrub.
        const diagnostic = stderr ? stderr.slice(0, 500) : undefined;
        if (err) {
          const reason = err.killed ? 'timeout' : `exit_code_${err.code ?? 'unknown'}`;
          console.error(`[subscriber] Claude failed for ${envelope.taskId}: ${reason}`);
          resolve({ success: false, reason, diagnostic });
        } else {
          console.log(`[subscriber] Claude completed ticket ${envelope.taskId}`);
          resolve({ success: true });
        }
      }
    );

    // Write the prompt to Claude's stdin, then close stdin to signal end of input
    child.stdin.write(sessionPrompt);
    child.stdin.end();
  });
}

function makeTaskHandler(client) {
  return async function handleTaskEnvelope(envelope, meta) {
    if (envelope.project !== NORMALIZED_PROJECT) {
      const err = new Error(
        `task envelope declares project "${envelope.project}", does not match this container's project "${NORMALIZED_PROJECT}"`
      );
      err.permanent = true;
      throw err;
    }

    const result = await runSerially(() => runClaude(envelope));

    if (result.success) {
      await publishTaskStatus(client, envelope, { status: 'completed' });
      return;
    }

    // A retryable attempt failure stays delivery metadata; only publish the
    // terminal 'failed' Task outcome once attempts are exhausted.
    if (meta.attemptNumber >= MAX_ATTEMPTS) {
      await publishTaskStatus(client, envelope, {
        status: 'failed',
        reason: result.reason,
        diagnostic: result.diagnostic,
      });
    }

    throw new Error(result.reason);
  };
}

async function main() {
  const client = createClient({ url: REDIS_URL });
  client.on('error', err => console.error('[subscriber] Redis error:', err));
  await client.connect();
  console.log(`[subscriber] Connected to ${REDIS_URL} for project ${PROJECT_NAME}`);

  // Anything left in the snapshot root belongs to a process that no longer
  // exists: a subscriber that was killed rather than shut down never reaches
  // removeSnapshot, and pm2 restarts it. Swept once, here, before the first
  // consumer starts — dispatches are serial, so from the next line on a
  // directory in there may be the running session's (see pruneSnapshotRoot).
  try {
    const pruned = pruneSnapshotRoot();
    if (pruned.length) {
      console.log(`[subscriber] Removed ${pruned.length} commons snapshot(s) left behind by an earlier process`);
    }
  } catch (pruneErr) {
    console.error(`[subscriber] Could not sweep the snapshot root: ${pruneErr.message}`);
  }

  const handler = makeTaskHandler(client);
  const consumerName = process.env.HOSTNAME || `${PROJECT_NAME}-container`;
  const consumers = [];

  for (const suffix of suffixList()) {
    const stream = agentStreamName(suffix);
    const group = `agent-${suffix}`;
    await ensureGroup(client, stream, group);
    const consumer = createConsumer(client, {
      stream,
      group,
      consumerName,
      handler,
      maxAttempts: MAX_ATTEMPTS,
    });
    await consumer.start();
    consumers.push(consumer);
    console.log(`[subscriber] Consuming ${stream} as ${group}/${consumerName}`);
  }

  const shutdown = async () => {
    console.log('[subscriber] Shutting down — stopping consumers cleanly');
    await Promise.all(consumers.map(c => c.stop()));
    await client.quit();
    process.exit(0);
  };
  process.on('SIGTERM', shutdown);
  process.on('SIGINT', shutdown);
}

main().catch(err => {
  console.error('[subscriber] Fatal error:', err);
  process.exit(1);
});
