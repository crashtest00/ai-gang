"""
Redis Streams transport — a Python port of services/scrummaster/src/streams.js,
reimplemented against redis-py rather than required cross-language —
Redis Streams is a wire protocol, not a language-specific library. Same
stream/consumer-group
topology, same ack/retry/dead-letter semantics, same idempotency
(dedupe-on-publish) mechanism as the Node original — see each function's
docstring for the line-by-line correspondence.

Architectural note (final report): this is a deliberate REIMPLEMENTATION,
not a call into ScrumMaster's HTTP surface. Streams delivery has no
natural HTTP shape (XADD/XREADGROUP/XAUTOCLAIM are wire-protocol Redis
commands), so there is no "call scrummaster over HTTP instead" option here
in the way there is for catalog-backed assignment validation
(see registry.py/assignment.py's own module comments) — the only
implementation choices were "reimplement in Python" or "require() from
Node," and the latter is impossible across languages.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .envelope import from_stream_fields, to_stream_fields, validate_envelope

DEFAULT_LEASE_MS = 35 * 60 * 1000
DEFAULT_RETRY_DELAY_MS = 10 * 60 * 1000
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BLOCK_MS = 5000
DEFAULT_RECLAIM_INTERVAL_MS = 60 * 1000
DEFAULT_BATCH_SIZE = 10


class PermanentError(Exception):
    """Raised by a handler to send an entry straight to the dead-letter
    stream without consuming a retry attempt — mirrors streams.js's
    `err.permanent = true` convention."""


def dead_letter_stream_name(stream: str) -> str:
    return f'{stream}:dead'


def attempts_key(stream: str, group: str) -> str:
    return f'aigang:attempts:{stream}:{group}'


def health_key(stream: str, group: str) -> str:
    return f'aigang:health:{stream}:{group}'


def ensure_group(client, stream: str, group: str) -> None:
    """Idempotently create a stream + consumer group, starting at '0' (not
    '$') so a freshly created group sees the stream's full retained
    history — same rationale as streams.js's ensureGroup."""
    try:
        client.xgroup_create(stream, group, id='0', mkstream=True)
    except Exception as err:  # noqa: BLE001 - mirrors the Node BUSYGROUP catch
        if 'BUSYGROUP' not in str(err):
            raise


def publish(client, stream: str, envelope: dict[str, Any], *, dedupe_key: Optional[str] = None,
            dedupe_ttl_seconds: int = 7 * 24 * 60 * 60) -> dict[str, Any]:
    """Durably enqueue an envelope. If dedupe_key is given, the add is
    idempotent via SET NX EX — a caller that already durably enqueued this
    logical message gets {'deduped': True} back instead of a second entry.
    """
    validate_envelope(envelope)

    if dedupe_key:
        idem_key = f'aigang:idem:publish:{dedupe_key}'
        claimed = client.set(idem_key, envelope['messageId'], nx=True, ex=dedupe_ttl_seconds)
        if not claimed:
            return {'deduped': True, 'messageId': envelope['messageId'], 'entryId': None}

    entry_id = client.xadd(stream, to_stream_fields(envelope))
    if isinstance(entry_id, bytes):
        entry_id = entry_id.decode()
    return {'deduped': False, 'entryId': entry_id, 'messageId': envelope['messageId']}


def dead_letter(client, stream: str, group: str, entry_id: str, envelope: Any, reason: str, attempts: int) -> None:
    dead = dead_letter_stream_name(stream)
    client.xadd(dead, {'data': json.dumps({
        'originalStream': stream,
        'originalEntryId': entry_id,
        'envelope': envelope,
        'reason': reason,
        'attempts': attempts,
        'deadLetteredAt': datetime.now(timezone.utc).isoformat(),
    })})
    client.xack(stream, group, entry_id)
    client.hdel(attempts_key(stream, group), entry_id)


def _compare_stream_ids(a: str, b: str) -> int:
    a_ms, _, a_seq = a.partition('-')
    b_ms, _, b_seq = b.partition('-')
    a_ms, b_ms = int(a_ms), int(b_ms)
    if a_ms != b_ms:
        return a_ms - b_ms
    return int(a_seq or 0) - int(b_seq or 0)


def trim_acknowledged(client, stream: str, group: str, retention_ms: int = 7 * 24 * 60 * 60 * 1000) -> None:
    cutoff_id = f'{int(time.time() * 1000) - retention_ms}-0'
    min_id = cutoff_id
    try:
        range_ = client.xpending_range(stream, group, min='-', max='+', count=1)
        if range_:
            first_id = range_[0]['message_id']
            if isinstance(first_id, bytes):
                first_id = first_id.decode()
            if _compare_stream_ids(first_id, cutoff_id) < 0:
                min_id = first_id
    except Exception:
        pass
    client.xtrim(stream, minid=min_id)


def trim_dead_letters(client, source_stream: str, retention_ms: int = 30 * 24 * 60 * 60 * 1000) -> None:
    cutoff_id = f'{int(time.time() * 1000) - retention_ms}-0'
    client.xtrim(dead_letter_stream_name(source_stream), minid=cutoff_id)


def replay(client, dead_stream: str, dead_entry_id: str) -> dict[str, Any]:
    entries = client.xrange(dead_stream, min=dead_entry_id, max=dead_entry_id)
    if not entries:
        raise ValueError(f'dead-letter entry {dead_entry_id} not found on {dead_stream}')

    _, fields = entries[0]
    raw = fields.get(b'data') or fields.get('data')
    if isinstance(raw, bytes):
        raw = raw.decode()
    record = json.loads(raw)

    replay_envelope = {**record['envelope'], 'correlationId': record['envelope']['messageId']}
    validate_envelope(replay_envelope)

    entry_id = client.xadd(record['originalStream'], to_stream_fields(replay_envelope))
    if isinstance(entry_id, bytes):
        entry_id = entry_id.decode()
    client.hset(dead_stream + ':replays', dead_entry_id, json.dumps({
        'replayedAt': datetime.now(timezone.utc).isoformat(),
        'newEntryId': entry_id,
        'newStream': record['originalStream'],
    }))
    return {'entryId': entry_id, 'stream': record['originalStream'], 'envelope': replay_envelope}


def record_success(client, stream: str, group: str) -> None:
    client.set(health_key(stream, group), datetime.now(timezone.utc).isoformat())


class Consumer:
    """Durable consumer-group processor for one stream — a Python port of
    streams.js's createConsumer. `handler(envelope)` must return normally
    on success (entry XACKed) or raise to indicate failure; raising
    PermanentError sends the entry straight to dead-letter without
    consuming a retry slot, any other exception is a transient failure left
    pending for reclaim/retry up to max_attempts.

    Runs its blocking XREADGROUP loop on a dedicated connection (mirrors
    streams.js's `readClient = client.duplicate()`) so multiple consumers
    sharing a connection pool don't block each other's blocking reads, and
    so stop() can force-close that connection to interrupt an in-flight
    blocking read promptly.
    """

    def __init__(self, redis_factory: Callable[[], Any], *, stream: str, group: str, consumer_name: str,
                 handler: Callable[[dict], None],
                 lease_ms: int = DEFAULT_LEASE_MS,
                 retry_delay_ms: int = DEFAULT_RETRY_DELAY_MS,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 block_ms: int = DEFAULT_BLOCK_MS,
                 reclaim_interval_ms: int = DEFAULT_RECLAIM_INTERVAL_MS,
                 batch_size: int = DEFAULT_BATCH_SIZE):
        self._redis_factory = redis_factory
        self.stream = stream
        self.group = group
        self.consumer_name = consumer_name
        self.handler = handler
        self.retry_delay_ms = retry_delay_ms
        self.max_attempts = max_attempts
        self.block_ms = block_ms
        self.reclaim_interval_ms = reclaim_interval_ms
        self.batch_size = batch_size

        self._client = redis_factory()
        self._read_client = None
        self._running = False
        self._thread = None
        self._reclaim_thread = None

    def _get_attempts(self, entry_id: str) -> int:
        value = self._client.hget(attempts_key(self.stream, self.group), entry_id)
        return int(value) if value else 0

    def _bump_attempts(self, entry_id: str) -> int:
        return self._client.hincrby(attempts_key(self.stream, self.group), entry_id, 1)

    def _process_entry(self, entry_id: str, fields: dict) -> None:
        envelope = from_stream_fields(fields)
        if envelope is None:
            dead_letter(self._client, self.stream, self.group, entry_id, {'raw': fields}, 'invalid_envelope',
                        self._get_attempts(entry_id))
            return

        attempt_number = self._bump_attempts(entry_id)
        try:
            self.handler(envelope)
            self._client.xack(self.stream, self.group, entry_id)
            self._client.hdel(attempts_key(self.stream, self.group), entry_id)
            record_success(self._client, self.stream, self.group)
        except PermanentError as err:
            dead_letter(self._client, self.stream, self.group, entry_id, envelope, str(err), attempt_number)
        except Exception as err:  # noqa: BLE001
            if getattr(err, 'permanent', False):
                dead_letter(self._client, self.stream, self.group, entry_id, envelope, str(err), attempt_number)
                return
            if attempt_number >= self.max_attempts:
                dead_letter(self._client, self.stream, self.group, entry_id, envelope,
                            f'retry exhausted: {err}', attempt_number)
                return
            # Leave pending — reclaimable once idle past retry_delay_ms.

    def _reclaim_stale(self) -> None:
        try:
            cursor = '0-0'
            while True:
                next_cursor, messages, _deleted = self._client.xautoclaim(
                    self.stream, self.group, self.consumer_name, min_idle_time=self.retry_delay_ms,
                    start_id=cursor, count=self.batch_size,
                )
                cursor = next_cursor
                for entry_id, fields in messages:
                    if not fields:
                        continue
                    self._process_entry(entry_id, fields)
                if cursor in ('0-0', b'0-0') or not messages:
                    break
        except Exception as err:  # noqa: BLE001
            print(f'[streams] reclaim error on {self.stream}/{self.group}: {err}')

    def _loop(self) -> None:
        ensure_group(self._client, self.stream, self.group)
        self._read_client = self._redis_factory()

        while self._running:
            try:
                response = self._read_client.xreadgroup(
                    self.group, self.consumer_name, {self.stream: '>'},
                    count=self.batch_size, block=self.block_ms,
                )
            except Exception as err:  # noqa: BLE001
                if not self._running:
                    break
                print(f'[streams] read error on {self.stream}: {err}')
                time.sleep(1)
                continue

            if not response:
                continue

            for _stream_name, messages in response:
                for entry_id, fields in messages:
                    self._process_entry(entry_id, fields)

        try:
            self._read_client.close()
        except Exception:
            pass

        # This thread may have opened its own Django DB connection (every
        # handler call ultimately runs store.py, which uses the ORM) —
        # Django never closes per-thread connections on its own, so a
        # long-lived consumer thread that stops would otherwise leak one.
        # `connections.close_all()` only closes connections THIS thread
        # created (django.db.utils.ConnectionHandler is thread-local).
        try:
            from django.db import connections
            connections.close_all()
        except Exception:
            pass

    def start(self) -> None:
        import threading
        if self._running:
            return
        self._running = True
        ensure_group(self._client, self.stream, self.group)
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                         name=f'consumer:{self.stream}:{self.group}')
        self._thread.start()

        def reclaim_loop():
            # Polls self._running in small slices rather than sleeping the
            # full reclaim_interval_ms in one call, so stop() can interrupt
            # this thread promptly regardless of how long the configured
            # interval is — mirrors Node's setInterval/clearInterval, where
            # clearInterval cancels immediately rather than waiting out the
            # current interval.
            slice_s = 0.25
            elapsed = 0.0
            while self._running:
                time.sleep(slice_s)
                elapsed += slice_s
                if elapsed >= self.reclaim_interval_ms / 1000:
                    elapsed = 0.0
                    if self._running:
                        self._reclaim_stale()
            try:
                from django.db import connections
                connections.close_all()
            except Exception:
                pass

        self._reclaim_thread = threading.Thread(target=reclaim_loop, daemon=True,
                                                  name=f'reclaim:{self.stream}:{self.group}')
        self._reclaim_thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._running = False
        # Interrupt an in-flight blocking read promptly rather than waiting
        # out block_ms — mirrors streams.js's readClient.disconnect().
        if self._read_client is not None:
            try:
                self._read_client.connection_pool.disconnect()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self._reclaim_thread is not None:
            self._reclaim_thread.join(timeout=timeout)


def create_consumer(redis_factory: Callable[[], Any], **kwargs) -> Consumer:
    return Consumer(redis_factory, **kwargs)


def health(client, stream: str, group: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        'stream': stream, 'group': group, 'connected': True, 'length': 0, 'pending': 0,
        'undeliveredCount': None, 'oldestPendingAgeMs': None, 'retryCount': 0,
        'deadLetterCount': 0, 'lastSuccessAt': None, 'consumers': [],
    }
    try:
        result['length'] = client.xlen(stream)
    except Exception:
        pass
    try:
        summary = client.xpending(stream, group)
        result['pending'] = summary.get('pending', 0) if summary else 0
        range_ = client.xpending_range(stream, group, min='-', max='+', count=1)
        if range_:
            result['oldestPendingAgeMs'] = range_[0].get('time_since_delivered')
    except Exception:
        pass
    try:
        groups = client.xinfo_groups(stream)
        for g in groups:
            name = g.get('name')
            if isinstance(name, bytes):
                name = name.decode()
            if name == group and g.get('lag') is not None:
                result['undeliveredCount'] = g.get('lag')
    except Exception:
        pass
    try:
        attempts = client.hvals(attempts_key(stream, group))
        result['retryCount'] = sum(int(v) for v in attempts)
    except Exception:
        pass
    try:
        result['deadLetterCount'] = client.xlen(dead_letter_stream_name(stream))
    except Exception:
        pass
    result['lastSuccessAt'] = client.get(health_key(stream, group))
    return result


def classify_health(h: dict[str, Any], *, pending_age_threshold_ms: int = 10 * 60 * 1000,
                     dead_letter_threshold: int = 1) -> str:
    if not h.get('connected'):
        return 'unhealthy'
    if (h.get('oldestPendingAgeMs') or 0) > pending_age_threshold_ms:
        return 'degraded'
    if h.get('deadLetterCount', 0) >= dead_letter_threshold:
        return 'degraded'
    return 'healthy'
