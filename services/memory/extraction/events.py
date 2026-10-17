"""The ``artifact.uploaded`` consumer (artifact-text-extraction.md REQ-03, REQ-04).

Core's relay publishes one entry per upload and re-upload to
``aigang:artifacts:events`` (``services/core/artifacts/events.py``): an
envelope in the entry's single ``data`` field, whose payload carries
``eventType`` ``artifact.uploaded``, ``artifactId`` and ``action``
(``created`` or ``replaced``). Each one becomes an extraction job; a
``replaced`` one is how a re-upload causes a new extraction (REQ-03).

Memory reads the stream through a consumer group of its own,
``memory-extraction``, created at the stream's beginning, so a first start
also queues every upload the stream still holds. An entry is acknowledged
only after its job is committed, so a worker that stops between the two
reads the entry again from its pending list on restart. An entry that is
not an upload event is acknowledged and skipped: it names no file.
"""

from __future__ import annotations

import json
import logging
import uuid

import redis

from . import jobs

log = logging.getLogger('memory.extraction')

STREAM = 'aigang:artifacts:events'
GROUP = 'memory-extraction'
UPLOADED = 'artifact.uploaded'


class EventConsumer:
    def __init__(self, url: str, consumer: str, *, stream: str = STREAM, group: str = GROUP):
        self.client = redis.Redis.from_url(url, decode_responses=True)
        self.stream, self.group, self.consumer = stream, group, consumer
        self._backlog = True  # first drain this consumer's own unacknowledged entries
        self._group_ready = False

    def _ensure_group(self) -> None:
        # Here rather than at construction, so a worker started while Redis
        # is down still runs jobs and the sweep, and joins once Redis is up.
        if self._group_ready:
            return
        try:
            self.client.xgroup_create(self.stream, self.group, id='0', mkstream=True)
        except redis.ResponseError as err:
            if 'BUSYGROUP' not in str(err):
                raise
        self._group_ready = True

    def _artifact_id(self, fields: dict):
        try:
            envelope = json.loads(fields.get('data', ''))
            payload = envelope.get('payload') or {}
            if payload.get('eventType') != UPLOADED:
                return None, None
            return str(uuid.UUID(str(payload.get('artifactId')))), str(payload.get('action') or 'uploaded')
        except (ValueError, TypeError, AttributeError):
            return None, None

    def poll(self, block_ms: int = 1000) -> int:
        """Read once (blocking up to ``block_ms``), queue a job per upload
        event, and acknowledge. Returns how many entries were handled."""
        self._ensure_group()
        start = '0' if self._backlog else '>'
        response = self.client.xreadgroup(self.group, self.consumer, {self.stream: start}, count=100,
                                          block=None if self._backlog else block_ms)
        entries = response[0][1] if response else []
        if self._backlog and not entries:
            self._backlog = False
        for entry_id, fields in entries:
            artifact_id, action = self._artifact_id(fields or {})
            if artifact_id is None:
                log.warning('skipping %s on %s: not an artifact.uploaded event', entry_id, self.stream)
            else:
                jobs.enqueue(artifact_id, action)
            self.client.xack(self.stream, self.group, entry_id)
        return len(entries)
