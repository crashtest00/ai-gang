"""
The outbox relay: reads
OutboxEvent rows written in the same transaction as a datastore write
(store.py), publishes each to this service's outbound event stream, and
marks it published. Never the sole mechanism connecting a write to its
event — the transactional outbox row IS that connection; this process
only has to eventually notice an unpublished row and successfully publish
it, any number of times, without ever leaving a committed write
unpublished or publishing the same logical event twice.

Crash safety is the entire point: this module has no persisted in-process
state of its own. If it dies between XADD succeeding and the UPDATE
marking published_at, the row is still published_at IS NULL on restart, so
it is picked up again — and the redo is itself a no-op at the Streams
layer because `dedupe_key` below reuses the exact same idempotency key on
every attempt for a given outbox row (streams.py's own SET NX pattern,
the Streams layer's existing idempotency mechanism).

Direct port of the Node service's src/relay.js.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from django.db import close_old_connections
from django.utils import timezone

from .envelope import Kind, validate_envelope
from .models import OutboxEvent
from .stream_topology import event_stream_name
from .streams import publish


def relay_once(redis_client, *, batch_size: int = 20, row_delay_ms: int = 0) -> int:
    """Reads up to `batch_size` unpublished rows and publishes each one.
    Returns the number of rows processed (published or
    confirmed-already-published on this pass) so callers/tests can observe
    progress.

    `row_delay_ms` (test-only knob, default 0): an artificial delay
    awaited between rows, so a test can reliably kill this process
    mid-batch instead of racing a batch that completes in a few
    milliseconds.
    """
    rows = list(OutboxEvent.objects.filter(published_at__isnull=True).order_by('created_at')[:batch_size])

    processed = 0
    for row in rows:
        envelope = {
            'schemaVersion': '1',
            'messageId': str(row.id),  # deterministic: stable across retries of the SAME outbox row.
            'kind': Kind.WORK_ITEM_EVENT,
            'project': row.project,
            'taskId': None,
            'contextId': None,
            'correlationId': None,
            'createdAt': row.created_at.isoformat(),
            'payload': {
                'outboxId': str(row.id),
                'eventType': row.event_type,
                'workItemId': str(row.work_item_id) if row.work_item_id else None,
                'data': row.payload,
            },
        }
        validate_envelope(envelope)

        stream = event_stream_name(row.project)
        result = publish(redis_client, stream, envelope, dedupe_key=f'outbox:{row.id}')

        OutboxEvent.objects.filter(id=row.id).update(
            published_at=timezone.now(), stream_entry_id=result.get('entryId'),
        )

        processed += 1
        if row_delay_ms > 0:
            time.sleep(row_delay_ms / 1000)

    return processed


def run_loop(redis_client, *, poll_interval_ms: int = 1000, batch_size: int = 20, row_delay_ms: int = 0,
             should_stop: Callable[[], bool] = lambda: False) -> None:
    """Runs relay_once in a loop until should_stop() returns True (or
    forever, if no stop condition is given — the normal standalone-process
    case, see management/commands/relay.py). Sleeps poll_interval_ms only
    when a pass finds nothing to do, so a backlog drains without waiting
    between rows."""
    while not should_stop():
        # Each pass gets a fresh Django connection health-check
        # (CONN_MAX_AGE/close_old_connections) — matters here because this
        # loop runs indefinitely in a long-lived process, unlike a normal
        # request/response cycle where Django does this for you between
        # requests.
        close_old_connections()
        processed = relay_once(redis_client, batch_size=batch_size, row_delay_ms=row_delay_ms)
        if processed == 0:
            time.sleep(poll_interval_ms / 1000)
