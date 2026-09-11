"""Mirrors services/work-item-service/test/catchup.test.js."""

from __future__ import annotations

import uuid

from workitems import catchup, project_config, store
from workitems.models import OutboxEvent

PROJECT = 'test-project'


def outbox_event_types_for(project):
    return list(OutboxEvent.objects.filter(project=project, event_type='work_item.jira_catchup_requested'))


def test_req15_start_catchup_push_queues_items_missing_external_key(clean_db):
    a, b, already_pushed = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    store.create_work_item({'id': a, 'project': PROJECT, 'type': 'task', 'displayName': 'A'})
    store.create_work_item({'id': b, 'project': PROJECT, 'type': 'task', 'displayName': 'B'})
    store.create_work_item({'id': already_pushed, 'project': PROJECT, 'type': 'task', 'displayName': 'Already', 'externalKey': 'TP-99'})

    result = catchup.start_catchup_push(PROJECT)
    assert sorted(result['queued']) == sorted([str(a), str(b)])

    events = outbox_event_types_for(PROJECT)
    assert len(events) == 2, 'the already-pushed item must be skipped, not re-queued'


def test_req15_record_external_key_is_idempotent(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})

    first = catchup.record_external_key(item_id, 'TP-1')
    assert first['alreadyRecorded'] is False
    assert first['externalKey'] == 'TP-1'

    # A retry with a DIFFERENT key must not overwrite the first.
    second = catchup.record_external_key(item_id, 'TP-2')
    assert second['alreadyRecorded'] is True
    assert second['externalKey'] == 'TP-1'

    item = store.get_work_item(item_id)
    assert item.external_key == 'TP-1'


def test_req15_resumed_push_after_partial_failure_only_queues_remaining(clean_db):
    a, b = uuid.uuid4(), uuid.uuid4()
    store.create_work_item({'id': a, 'project': PROJECT, 'type': 'task', 'displayName': 'A'})
    store.create_work_item({'id': b, 'project': PROJECT, 'type': 'task', 'displayName': 'B'})

    catchup.start_catchup_push(PROJECT)
    catchup.record_external_key(a, 'TP-A')

    retry = catchup.start_catchup_push(PROJECT)
    assert retry['queued'] == [str(b)]


def test_req14_connect_and_disconnect_jira(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})

    mode = project_config.get_mode(PROJECT)
    assert mode['mode'] == 'local'

    catchup.connect_jira(PROJECT, 'TP')
    mode = project_config.get_mode(PROJECT)
    assert mode['mode'] == 'jira'
    assert mode['jiraProjectKey'] == 'TP'

    catchup.disconnect_jira(PROJECT)
    mode = project_config.get_mode(PROJECT)
    assert mode['mode'] == 'local'

    item = store.get_work_item(item_id)
    assert item.id == item_id
