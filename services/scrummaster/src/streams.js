'use strict';

// Redis Streams transport: durable producer/consumer primitives shared by
// every ScrumMaster<->agent flow. See the redis-streams design.
//
// Design notes:
// - One shared client is enough. Unlike Pub/Sub, Streams commands (XADD,
//   XREADGROUP, XACK, ...) do not require a dedicated connection.
// - A stream entry carries exactly one field, `data`, holding the JSON
//   envelope (see envelope.js). Redis stream entry IDs are transport
//   offsets, not message identity — messageId/taskId/contextId live inside
//   the envelope and survive redelivery, reclaim, and dead-lettering.
// - Retry/attempt bookkeeping is kept in a side hash so the original entry
//   stays byte-for-byte immutable across retries.

const { fromStreamFields, toStreamFields, validateEnvelope } = require('./envelope');

const DEFAULTS = Object.freeze({
  leaseMs: 35 * 60 * 1000,      // REQ-06 default: 35 minutes
  // XAUTOCLAIM takes a single minIdleTime for the whole scan, so this is the
  // only threshold actually enforced before an entry is reclaimed — it must
  // exceed realistic handler duration, not just genuine-retry latency,
  // because `attempts` is bumped before the handler runs (see processEntry),
  // so a still-running first attempt already reads as attempts>0 by the time
  // reclaimStale looks at it. 10 minutes comfortably covers a Claude Code
  // task; a handler that's still working past that is reclaimed and re-run
  // concurrently (see the mitigated bug in reclaimStale below).
  retryDelayMs: 10 * 60 * 1000,
  maxAttempts: 3,                // REQ-06 default: 3 attempts
  blockMs: 5000,
  reclaimIntervalMs: 60 * 1000,
  batchSize: 10,
});

function deadLetterStreamName(stream) {
  return `${stream}:dead`;
}

function attemptsKey(stream, group) {
  return `aigang:attempts:${stream}:${group}`;
}

function healthKey(stream, group) {
  return `aigang:health:${stream}:${group}`;
}

// Idempotently create a stream + consumer group. Safe to call on every
// startup — creating a *new* group would incorrectly replay retained
// history, so this only creates the group once and otherwise no-ops.
//
// The group starts at '0', not '$': if a message was ever added to this
// stream before the group existed (a producer racing bootstrap, or a
// pre-existing stream from an earlier deployment), '$' would silently make
// that entry permanently unreadable by this group. Starting at '0' costs
// nothing once the group already exists (BUSYGROUP short-circuits) and
// guarantees a freshly created group sees the stream's full retained
// history instead of only entries added after it.
async function ensureGroup(client, stream, group) {
  try {
    await client.xGroupCreate(stream, group, '0', { MKSTREAM: true });
  } catch (err) {
    if (!/BUSYGROUP/.test(err.message)) throw err;
  }
}

// Durably enqueue an envelope. If `dedupeKey` is supplied, the add is
// idempotent: a caller that has already durably enqueued this logical
// message (e.g. the same Jira webhook delivered twice) gets
// `{ deduped: true }` back instead of a second stream entry (REQ-02, REQ-05).
async function publish(client, stream, envelope, { dedupeKey, dedupeTtlSeconds = 7 * 24 * 60 * 60 } = {}) {
  validateEnvelope(envelope);

  if (dedupeKey) {
    const idemKey = `aigang:idem:publish:${dedupeKey}`;
    const claimed = await client.set(idemKey, envelope.messageId, { NX: true, EX: dedupeTtlSeconds });
    if (claimed !== 'OK') {
      return { deduped: true, messageId: envelope.messageId };
    }
  }

  const entryId = await client.xAdd(stream, '*', toStreamFields(envelope));
  return { deduped: false, entryId, messageId: envelope.messageId };
}

// Move an entry to <stream>:dead, acknowledge it in the source group, and
// record why. Preserves the original entry id and envelope for replay.
async function deadLetter(client, stream, group, entryId, envelope, reason, attempts) {
  const dead = deadLetterStreamName(stream);
  await client.xAdd(dead, '*', {
    data: JSON.stringify({
      originalStream: stream,
      originalEntryId: entryId,
      envelope,
      reason,
      attempts,
      deadLetteredAt: new Date().toISOString(),
    }),
  });
  await client.xAck(stream, group, entryId);
  await client.hDel(attemptsKey(stream, group), entryId);
}

function compareStreamIds(a, b) {
  const [aMs, aSeq] = a.split('-').map(Number);
  const [bMs, bSeq] = b.split('-').map(Number);
  if (aMs !== bMs) return aMs - bMs;
  return (aSeq || 0) - (bSeq || 0);
}

// Trim acknowledged entries older than retentionMs (REQ-10, default 7 days).
// Never trims past the oldest entry still pending in `group` — an
// unacknowledged poison message or a slow retry must stay recoverable
// regardless of age.
async function trimAcknowledged(client, stream, group, retentionMs = 7 * 24 * 60 * 60 * 1000) {
  const cutoffId = `${Date.now() - retentionMs}-0`;
  let minId = cutoffId;
  try {
    const range = await client.xPendingRange(stream, group, '-', '+', 1);
    if (range.length > 0 && compareStreamIds(range[0].id, cutoffId) < 0) {
      minId = range[0].id;
    }
  } catch { /* group may not exist yet */ }
  await client.xTrim(stream, 'MINID', minId);
}

// Trim dead-letter entries older than retentionMs (REQ-10, default 30 days).
// Dead-letter entries have no consumer group of their own to protect —
// operators are expected to replay what they need within the window.
async function trimDeadLetters(client, sourceStream, retentionMs = 30 * 24 * 60 * 60 * 1000) {
  const cutoffId = `${Date.now() - retentionMs}-0`;
  await client.xTrim(deadLetterStreamName(sourceStream), 'MINID', cutoffId);
}

// Replay a dead-lettered entry back onto its original stream. The replay
// carries a fresh Redis entry id but keeps the original envelope
// (messageId/taskId/contextId unchanged) so normal idempotency handling
// applies, and sets correlationId to the original messageId so the replay
// is auditably linked to the dead-lettered attempt.
async function replay(client, deadStream, deadEntryId) {
  const entries = await client.xRange(deadStream, deadEntryId, deadEntryId);
  if (!entries.length) throw new Error(`dead-letter entry ${deadEntryId} not found on ${deadStream}`);

  const record = JSON.parse(entries[0].message.data);
  const replayEnvelope = {
    ...record.envelope,
    correlationId: record.envelope.messageId,
  };
  validateEnvelope(replayEnvelope);

  const entryId = await client.xAdd(record.originalStream, '*', toStreamFields(replayEnvelope));
  await client.hSet(deadStream + ':replays', deadEntryId, JSON.stringify({
    replayedAt: new Date().toISOString(),
    newEntryId: entryId,
    newStream: record.originalStream,
  }));
  return { entryId, stream: record.originalStream, envelope: replayEnvelope };
}

async function recordSuccess(client, stream, group) {
  await client.set(healthKey(stream, group), new Date().toISOString());
}

// Aggregate health/diagnostics for one stream+group (REQ-09).
async function health(client, stream, group) {
  const result = {
    stream,
    group,
    connected: client.isReady,
    length: 0,
    pending: 0,
    undeliveredCount: null,
    oldestPendingAgeMs: null,
    retryCount: 0,
    deadLetterCount: 0,
    lastSuccessAt: null,
    consumers: [],
  };

  try {
    result.length = await client.xLen(stream);
  } catch { /* stream may not exist yet */ }

  try {
    const summary = await client.xPending(stream, group);
    result.pending = summary?.pending || 0;
    if (summary?.pending > 0 && summary.consumers) {
      result.consumers = summary.consumers.map(c => ({ name: c.name, pending: Number(c.deliveriesCounter ?? c.pending ?? 0) }));
    }
    const range = await client.xPendingRange(stream, group, '-', '+', 1);
    if (range.length > 0) {
      result.oldestPendingAgeMs = range[0].millisecondsSinceLastDelivery;
    }
  } catch { /* group may not exist yet */ }

  try {
    // node-redis's xInfoGroups() mapping drops the 'lag' field (Redis
    // 7+: entries never yet delivered to this group), so read it via the
    // raw reply instead — this is the REQ-09 "undelivered count".
    const raw = await client.sendCommand(['XINFO', 'GROUPS', stream]);
    const groupInfo = (raw || []).map(flatReplyToObject).find(g => g.name === group);
    if (groupInfo && groupInfo.lag !== undefined && groupInfo.lag !== null) {
      result.undeliveredCount = Number(groupInfo.lag);
    }
  } catch { /* stream/group may not exist yet */ }

  try {
    const attempts = await client.hVals(attemptsKey(stream, group));
    result.retryCount = attempts.reduce((sum, v) => sum + (parseInt(v, 10) || 0), 0);
  } catch { /* no in-flight retries recorded */ }

  try {
    result.deadLetterCount = await client.xLen(deadLetterStreamName(stream));
  } catch { /* no dead-letter stream yet */ }

  result.lastSuccessAt = await client.get(healthKey(stream, group));

  return result;
}

function flatReplyToObject(pairs) {
  const obj = {};
  for (let i = 0; i < pairs.length; i += 2) obj[pairs[i]] = pairs[i + 1];
  return obj;
}

// health status classification per REQ-09.
function classifyHealth(h, { pendingAgeThresholdMs = 10 * 60 * 1000, deadLetterThreshold = 1 } = {}) {
  if (!h.connected) return 'unhealthy';
  if ((h.oldestPendingAgeMs ?? 0) > pendingAgeThresholdMs) return 'degraded';
  if (h.deadLetterCount >= deadLetterThreshold) return 'degraded';
  return 'healthy';
}

// Create a durable consumer-group processor for one stream.
//
// `handler(envelope, meta)` must return normally on success (the entry is
// then XACKed) or throw to indicate failure. Throwing an Error with
// `.permanent = true` sends the entry straight to the dead-letter stream
// without consuming a retry attempt slot; any other throw is treated as a
// transient failure and left pending for reclaim/retry up to maxAttempts.
function createConsumer(client, {
  stream,
  group,
  consumerName,
  handler,
  leaseMs = DEFAULTS.leaseMs,
  retryDelayMs = DEFAULTS.retryDelayMs,
  maxAttempts = DEFAULTS.maxAttempts,
  blockMs = DEFAULTS.blockMs,
  reclaimIntervalMs = DEFAULTS.reclaimIntervalMs,
  batchSize = DEFAULTS.batchSize,
}) {
  let running = false;
  let loopPromise = null;
  let reclaimTimer = null;
  // A dedicated, duplicated connection for the blocking XREADGROUP call.
  // node-redis serializes all commands over one connection, so multiple
  // concurrent consumers (one per project's gateway/webhook stream, in the
  // same process) sharing the caller's `client` would queue behind each
  // other's blocking reads. Non-blocking commands (XACK, XAUTOCLAIM, ...)
  // stay on the shared `client`, which is fine to interleave.
  let readClient = null;

  async function getAttempts(entryId) {
    const value = await client.hGet(attemptsKey(stream, group), entryId);
    return value ? parseInt(value, 10) : 0;
  }

  async function bumpAttempts(entryId) {
    return client.hIncrBy(attemptsKey(stream, group), entryId, 1);
  }

  async function processEntry(entryId, fields) {
    const envelope = fromStreamFields(fields);

    if (!envelope) {
      await deadLetter(client, stream, group, entryId, { raw: fields }, 'invalid_envelope', await getAttempts(entryId));
      return;
    }

    const attemptNumber = await bumpAttempts(entryId);

    try {
      await handler(envelope, { entryId, attemptNumber });
      await client.xAck(stream, group, entryId);
      await client.hDel(attemptsKey(stream, group), entryId);
      await recordSuccess(client, stream, group);
    } catch (err) {
      if (err && err.retryWithoutAttempt) {
        const attempts = await client.hIncrBy(attemptsKey(stream, group), entryId, -1);
        if (attempts <= 0) await client.hDel(attemptsKey(stream, group), entryId);
        console.log(`[streams] ${stream}/${group} entry ${entryId} deferred without consuming an attempt:`, err.message);
        return;
      }
      if (err && err.permanent) {
        console.error(`[streams] ${stream}/${group} entry ${entryId} permanently failed (attempt ${attemptNumber}):`, err.message);
        await deadLetter(client, stream, group, entryId, envelope, err.message, attemptNumber);
        return;
      }
      if (attemptNumber >= maxAttempts) {
        console.error(`[streams] ${stream}/${group} entry ${entryId} exhausted ${attemptNumber} attempts, dead-lettering:`, err?.message);
        await deadLetter(client, stream, group, entryId, envelope, `retry exhausted: ${err?.message}`, attemptNumber);
        return;
      }
      // Leave pending. It becomes reclaimable once its idle time exceeds
      // retryDelayMs (bounded by leaseMs below for long-running work). Log
      // now, not just on eventual dead-letter — otherwise a stuck entry
      // retrying under maxAttempts is invisible in `docker logs` for its
      // entire retry window (found during V2 local-mode E2E testing: three
      // gateway-stream entries were retrying silently with nothing in the
      // container logs to explain why).
      console.error(`[streams] ${stream}/${group} entry ${entryId} failed (attempt ${attemptNumber}/${maxAttempts}), will retry:`, err?.message);
    }
  }

  async function reclaimStale() {
    try {
      let cursor = '0-0';
      // See the retryDelayMs comment in DEFAULTS: this is the only reclaim
      // threshold actually enforced. A previous version of this code tried
      // to compute a longer, attempts-aware threshold here and pass it to
      // XAUTOCLAIM, but XAUTOCLAIM only accepts one minIdleTime per call, and
      // that computed value was never actually wired in — silently
      // discarded — so every entry was reclaimable after retryDelayMs
      // regardless of whether it was a live first attempt or a genuine
      // retry. Bumping the default (see DEFAULTS) is the mitigation; a full
      // fix would need per-entry idle checks (XPENDING) before XCLAIM
      // instead of blind XAUTOCLAIM.
      for (;;) {
        const result = await client.xAutoClaim(stream, group, consumerName, retryDelayMs, cursor, { COUNT: batchSize });
        cursor = result.nextId;
        for (const { id, message } of result.messages || []) {
          if (!message) continue; // entry was deleted/trimmed between claim and read
          await processEntry(id, message);
        }
        if (cursor === '0-0' || !(result.messages || []).length) break;
      }
    } catch (err) {
      console.error(`[streams] reclaim error on ${stream}/${group}:`, err.message);
    }
  }

  async function loop() {
    await ensureGroup(client, stream, group);
    readClient = client.duplicate();
    readClient.on('error', err => console.error(`[streams] read connection error on ${stream}:`, err.message));
    await readClient.connect();

    while (running) {
      let response;
      try {
        response = await readClient.xReadGroup(group, consumerName, [{ key: stream, id: '>' }], {
          COUNT: batchSize,
          BLOCK: blockMs,
        });
      } catch (err) {
        if (!running) break; // stop() force-disconnected readClient to interrupt the blocking call
        console.error(`[streams] read error on ${stream}:`, err.message);
        await new Promise(r => setTimeout(r, 1000));
        continue;
      }

      if (!response) continue;

      for (const { messages } of response) {
        for (const { id, message } of messages) {
          await processEntry(id, message);
        }
      }
    }

    await readClient.quit().catch(() => {});
  }

  return {
    async start() {
      if (running) return;
      running = true;
      await ensureGroup(client, stream, group);
      loopPromise = loop();
      reclaimTimer = setInterval(() => { reclaimStale(); }, reclaimIntervalMs);
    },
    async stop() {
      running = false;
      if (reclaimTimer) clearInterval(reclaimTimer);
      // Interrupt an in-flight blocking read immediately rather than waiting
      // up to blockMs for it to time out on its own — shutdown must be
      // prompt without abandoning work (the entry stays pending/reclaimable
      // either way, since nothing here acks it).
      if (readClient) await readClient.disconnect().catch(() => {});
      if (loopPromise) await loopPromise;
    },
  };
}

module.exports = {
  DEFAULTS,
  ensureGroup,
  publish,
  deadLetter,
  deadLetterStreamName,
  trimAcknowledged,
  trimDeadLetters,
  replay,
  health,
  classifyHealth,
  createConsumer,
};
