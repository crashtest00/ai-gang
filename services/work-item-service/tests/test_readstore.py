"""Mirrors services/work-item-service/test/readStore.test.js."""

from __future__ import annotations

import uuid

from workitems import readstore, store
from workitems.models import AccessLog

PROJECT = 'read-project'


def test_req04_get_work_item_logs_access(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})

    readstore.get_work_item(item_id, actor='test-agent')

    rows = list(AccessLog.objects.filter(work_item_id=item_id))
    assert len(rows) == 1
    assert rows[0].operation == 'getWorkItem'
    assert rows[0].actor == 'test-agent'


def test_list_work_items_orders_by_priority_then_creation(clean_db):
    ids = {'low': uuid.uuid4(), 'urgent': uuid.uuid4(), 'none': uuid.uuid4(), 'high': uuid.uuid4()}
    store.create_work_item({'id': ids['low'], 'project': PROJECT, 'type': 'task', 'displayName': 'low', 'priority': 4})
    store.create_work_item({'id': ids['urgent'], 'project': PROJECT, 'type': 'task', 'displayName': 'urgent', 'priority': 1})
    store.create_work_item({'id': ids['none'], 'project': PROJECT, 'type': 'task', 'displayName': 'none', 'priority': 0})
    store.create_work_item({'id': ids['high'], 'project': PROJECT, 'type': 'task', 'displayName': 'high', 'priority': 2})

    rows = readstore.list_work_items(project=PROJECT)
    assert [r.id for r in rows] == [ids['urgent'], ids['high'], ids['low'], ids['none']]


def test_get_work_item_full_returns_everything_together(clean_db):
    blocker = uuid.uuid4()
    item_id = uuid.uuid4()
    store.create_work_item({'id': blocker, 'project': PROJECT, 'type': 'task', 'displayName': 'blocker'})
    store.create_work_item({
        'id': item_id, 'project': PROJECT, 'type': 'story', 'displayName': 'story',
        'storyDetail': {'behavior': 'b', 'acceptanceCriteria': 'ac', 'constraints': 'c', 'edgeCases': 'e', 'outOfScope': 'oos'},
    })
    store.create_link(blocker, item_id, 'blocks')
    store.attach_artifact(item_id, 'commit', 'sha1')
    store.append_comment(item_id, 'human', 'note')

    full = readstore.get_work_item_full(item_id)
    assert full['storyDetail'].behavior == 'b'
    assert len(full['links']) == 1
    assert len(full['history']) >= 1
    assert len(full['artifacts']) == 1
    assert len(full['comments']) == 1
