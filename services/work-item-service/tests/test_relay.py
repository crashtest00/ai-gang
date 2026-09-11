"""Mirrors services/work-item-service/test/relay.test.js."""

from __future__ import annotations

import json
import uuid

from workitems.models import OutboxEvent
from workitems.relay import relay_once
from workitems.stream_topology import event_stream_name
from workitems.store import write_outbox_event
from django.db import transaction


def test_relay_once_publishes_and_marks_published(clean_db, redis_client):
    work_item_id = uuid.uuid4()
    with transaction.atomic():
        outbox_id = write_outbox_event(
            project='relay-project', event_type='work_item.created', work_item_id=work_item_id, payload={'hello': 'world'},
        )

    processed = relay_once(redis_client, batch_size=10)
    assert processed == 1

    row = OutboxEvent.objects.get(id=outbox_id)
    assert row.published_at is not None
    assert row.stream_entry_id

    stream = event_stream_name('relay-project')
    entries = redis_client.xrange(stream, '-', '+')
    assert len(entries) == 1
    _, fields = entries[0]
    envelope = json.loads(fields['data'])
    assert envelope['messageId'] == outbox_id
    assert envelope['payload']['eventType'] == 'work_item.created'


def test_relay_once_is_a_no_op_once_everything_published(clean_db, redis_client):
    with transaction.atomic():
        write_outbox_event(project='relay-project-2', event_type='x', work_item_id=None, payload={})

    first = relay_once(redis_client)
    assert first == 1
    second = relay_once(redis_client)
    assert second == 0, 'no unpublished rows left'


def test_relay_once_redo_after_simulated_crash_does_not_duplicate(clean_db, redis_client):
    project = 'relay-project-3'
    with transaction.atomic():
        outbox_id = write_outbox_event(project=project, event_type='x', work_item_id=None, payload={})

    # First "attempt": publish, then simulate the crash by resetting
    # published_at back to NULL — reproducing "XADD succeeded, but the
    # process died before the UPDATE."
    relay_once(redis_client)
    OutboxEvent.objects.filter(id=outbox_id).update(published_at=None, stream_entry_id=None)
    relay_once(redis_client)  # redo

    stream = event_stream_name(project)
    entries = redis_client.xrange(stream, '-', '+')
    assert len(entries) == 1, 'the dedupeKey must prevent a second Streams entry for the same outbox row'
