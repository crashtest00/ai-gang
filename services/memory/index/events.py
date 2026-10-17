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
"backlog is indexed without loss").
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import redis

from . import follow, reads, store

log = logging.getLogger('memory.index')

GROUP = 'memory-indexer'
WORK_ITEM_STREAMS = 'aigang:workitems:*:events'
ARTIFACT_STREAM = 'aigang:artifacts:events'
RECORD_DELETED = 'work_item.record_deleted'
ARTIFACT_UPLOADED = 'artifact.uploaded'

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


def apply(stream: str, fields: dict) -> None:
    """Act on one stream entry. Raises nothing for an entry that is not an
    event this indexer reads: it is logged and acknowledged."""
    try:
        envelope = json.loads(fields.get('data', ''))
        payload = envelope.get('payload') or {}
    except (ValueError, TypeError, AttributeError):
        log.warning('skipping an entry on %s: not an envelope', stream)
        return
    event_type = payload.get('eventType') or ''
    if stream == ARTIFACT_STREAM:
        if event_type == ARTIFACT_UPLOADED and payload.get('artifactId') and payload.get('uploadedAt'):
            uploaded_at = datetime.fromisoformat(payload['uploadedAt'].replace('Z', '+00:00'))
            follow.note_upload(str(payload['artifactId']), uploaded_at, str(payload.get('action') or ''))
        return
    if event_type == RECORD_DELETED:
        follow.apply_deletion(payload.get('data') or {})
        return
    for name in catch_ups_for(event_type):
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
                apply(stream, entry_fields or {})
                self.client.xack(stream, GROUP, entry_id)
                handled += 1
        return handled
