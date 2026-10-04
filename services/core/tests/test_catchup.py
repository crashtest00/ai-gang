"""Covers what remains in catchup.py after v5.1 retired the catch-up push
(REQ-06) and v5.2 took the mode flip out of it
(canonical-delivery-state.md REQ-10): recording an external key.

The mode flip either way is now the two management commands', and is
covered where they are — tests/test_connect_jira_command.py and
tests/test_disconnect_jira_command.py. catchup.connect_jira switched a
project with no push at all, which is the thing REQ-10 exists to stop."""

from __future__ import annotations

import uuid

from workitems import catchup, store

PROJECT = 'test-project'


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
