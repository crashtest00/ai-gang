"""canonical-work-item-schema.md REQ-05 — DB-level append-only enforcement
(migration 0002_work_item_history_append_only): a raw UPDATE or DELETE
against work_item_history must be rejected by the database itself, not
merely avoided by application code going through store.py."""

from __future__ import annotations

import uuid

import pytest
from django.db import connection
from django.db.utils import Error as DjangoDbError

from workitems.models import WorkItem, WorkItemHistory


def _make_history_row():
    item = WorkItem.objects.create(
        id=uuid.uuid4(), project='test-project', type='task', display_name='x', status='proposed',
    )
    return WorkItemHistory.objects.create(
        work_item=item, field='status', old_value=None, new_value='proposed', actor='tester',
    )


def test_update_work_item_history_is_rejected_at_db_level(clean_db):
    row = _make_history_row()

    with pytest.raises(DjangoDbError, match='append-only'):
        with connection.cursor() as cursor:
            cursor.execute('UPDATE work_item_history SET new_value = %s WHERE id = %s', ['tampered', row.id])


def test_delete_work_item_history_is_rejected_at_db_level(clean_db):
    row = _make_history_row()

    with pytest.raises(DjangoDbError, match='append-only'):
        with connection.cursor() as cursor:
            cursor.execute('DELETE FROM work_item_history WHERE id = %s', [row.id])

    assert WorkItemHistory.objects.filter(id=row.id).exists()
