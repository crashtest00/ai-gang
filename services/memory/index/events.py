"""The indexer's event consumer (Corpus Indexing REQ-04, REQ-05, REQ-07).

Indexing is driven by the events Core's transactional outbox publishes for
every committed write, never by polling Core's tables:

- every project's work-item stream, ``aigang:workitems:<project>:events``,
  found by name (Redis ``SCAN``, re-run every
  MEMORY_INDEX_STREAM_DISCOVERY_SECONDS, so a new project's stream is
  joined without configuration). A work-item or A2A event asks for a
  catch-up of the class it names. The catch-up then reads Core's
  changed-since enumeration from the indexer's cursor (``follow``), so one
  read covers every record the event's transaction wrote. A
  ``work_item.record_deleted`` event removes the deleted record's units
  directly (REQ-07);
- ``aigang:artifacts:events``. An ``artifact.uploaded`` event marks the file
  "not indexed yet" until its extraction is recorded (REQ-05), and a
  re-upload's marks its earlier units no longer current (REQ-04).

The indexer reads each stream through a consumer group of its own,
``memory-indexer``, created at the stream's beginning. An entry is
acknowledged only after what it caused is committed, so an indexer stopped
in between reads it again from its pending list on restart (REQ-04's
"backlog is indexed without loss"). An entry it cannot act on is recorded
as a failure and acknowledged, so one bad entry never stops the streams
(``handle``, REQ-09).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import redis
from django.db import InterfaceError, OperationalError, transaction

from . import follow, reads, store

log = logging.getLogger('memory.index')

GROUP = 'memory-indexer'
WORK_ITEM_STREAMS = 'aigang:workitems:*:events'
ARTIFACT_STREAM = 'aigang:artifacts:events'
RECORD_DELETED = 'work_item.record_deleted'
ARTIFACT_UPLOADED = 'artifact.uploaded'
EVENT_ENTRY = 'event_entry'  # an IndexFailure's class for a stream entry; it has no units

_ALL_CORE = tuple(reads.CORE_ENUMERATIONS)


def catch_ups_for(event_type: str) -> tuple[str, ...]:
    """Which Core enumerations an event's write can have changed."""
    if event_type.startswith('a2a.'):
        return ('core:a2a_tasks',)
    if event_type == 'work_item.comment_added':
        return ('core:comments', 'core:work_items')
    if event_type == 'work_item.artifact_attached':
        return ('core:delivery_artifacts', 'core:work_items')
    if event_type.startswith('work_item.'):
        return ('core:work_items',)
    return _ALL_CORE  # an event type this indexer does not know: read everything that may have changed


class MalformedEntry(ValueError):
    """A stream entry that is not an event in the shape Core's relay
    publishes."""


def _payload(fields: dict) -> dict:
    try:
        envelope = json.loads(fields.get('data') or '')
    except (ValueError, TypeError) as err:
        raise MalformedEntry(f'the entry\'s data is not a JSON envelope ({err})') from err
    if not isinstance(envelope, dict):
        raise MalformedEntry('the entry\'s envelope is not a JSON object')
    payload = envelope.get('payload')
    if not isinstance(payload, dict):
        raise MalformedEntry('the envelope\'s payload is not a JSON object')
    return payload


def _uploaded_at(value) -> datetime:
    try:
        uploaded_at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError as err:
        raise MalformedEntry(f'uploadedAt {value!r} is not an ISO 8601 time') from err
    if uploaded_at.tzinfo is None:
        raise MalformedEntry(f'uploadedAt {value!r} has no time zone')
    return uploaded_at


def apply(stream: str, fields: dict) -> None:
    """Act on one stream entry. Raises ``MalformedEntry`` for an entry that
    is not an event in the relay's shape; ``handle`` records it."""
    payload = _payload(fields)
    event_type = payload.get('eventType') or ''
    if stream == ARTIFACT_STREAM:
        if event_type == ARTIFACT_UPLOADED and payload.get('artifactId') and payload.get('uploadedAt'):
            follow.note_upload(str(payload['artifactId']), _uploaded_at(payload['uploadedAt']),
                               str(payload.get('action') or ''))
        return
    if event_type == RECORD_DELETED:
        follow.apply_deletion(payload.get('data') or {})
        return
    for name in catch_ups_for(event_type):
        store.request_catch_up(name)


def handle(stream: str, entry_id: str, fields: dict) -> None:
    """Act on one entry under the index's writer lock, so a deletion or an
    upload never interleaves with a catch-up, a retry or a rebuild.

    An entry the indexer cannot act on is never fatal and never skipped
    silently (REQ-09): it is logged and recorded as a failure naming the
    stream, the entry and the reason, with the entry kept for a retry, and
    a work-item stream's entry also asks for a catch-up of every Core read,
    so whatever write it announced is still read. Only a lost connection
    to the index's own database or to Redis stops the consumer, before the
    entry is acknowledged, so it is read again."""
    with store.index_lock():
        try:
            with transaction.atomic():
                apply(stream, fields)
        except (redis.RedisError, OperationalError, InterfaceError):
            raise
        except Exception as err:  # noqa: BLE001 - REQ-09: one entry's failure is recorded, never fatal
            log.exception('cannot act on entry %s of %s', entry_id, stream)
            store.record_failure(f'event:{stream}:{entry_id}', EVENT_ENTRY, f'{type(err).__name__}: {err}',
                                 {'read': follow.EVENTS, 'stream': stream, 'entryId': entry_id,
                                  'fields': dict(fields)})
            if stream != ARTIFACT_STREAM:
                for name in _ALL_CORE:
                    store.request_catch_up(name)


class EventConsumer:
    def __init__(self, url: str, consumer: str):
        self.client = redis.Redis.from_url(url, decode_responses=True)
        self.consumer = consumer
        self.streams: dict[str, bool] = {}  # stream -> still draining its own pending entries

    def discover(self) -> list[str]:
        """Join every work-item event stream and the artifact stream."""
        found = [ARTIFACT_STREAM] + sorted(self.client.scan_iter(match=WORK_ITEM_STREAMS, _type='STREAM'))
        for stream in found:
            if stream in self.streams:
                continue
            try:
                self.client.xgroup_create(stream, GROUP, id='0', mkstream=stream == ARTIFACT_STREAM)
            except redis.ResponseError as err:
                if 'BUSYGROUP' not in str(err):
                    raise
            self.streams[stream] = True
        return found

    def poll(self, block_ms: int = 1000) -> int:
        """Read once from every joined stream, act on each entry, and
        acknowledge it. A stream's own unacknowledged entries are finished
        before its new ones are read. Returns how many entries were handled."""
        handled = 0
        backlog = {s: '0' for s, draining in self.streams.items() if draining}
        if backlog:
            handled += self._read(backlog, None)
        live = {s: '>' for s, draining in self.streams.items() if not draining}
        if live:
            handled += self._read(live, block_ms if block_ms > 0 and not handled else None)
        return handled

    def _read(self, streams: dict, block) -> int:
        response = self.client.xreadgroup(GROUP, self.consumer, streams, count=100, block=block) or []
        got = {stream: entries for stream, entries in response}
        handled = 0
        for stream, position in streams.items():
            entries = got.get(stream) or []
            if position == '0' and not entries:
                self.streams[stream] = False
            for entry_id, entry_fields in entries:
                handle(stream, entry_id, entry_fields or {})
                self.client.xack(stream, GROUP, entry_id)
                handled += 1
        return handled
