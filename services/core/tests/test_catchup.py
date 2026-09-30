"""Covers what remains in catchup.py after v5.1 retired the catch-up push
(REQ-06): recording an external key, and the mode flip either way."""

from __future__ import annotations

import uuid

from workitems import catchup, project_config, store

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


def test_req14_connect_and_disconnect_jira(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})

    mode = project_config.get_mode(PROJECT)
    assert mode['mode'] == 'local'

    catchup.connect_jira(PROJECT, 'TP')
    mode = project_config.get_mode(PROJECT)
    assert mode['mode'] == 'jira'
    assert mode['jiraProjectKey'] == 'TP'

    # REQ-06: disconnect_jira is the operator's switch back to local mode, and
    # it keeps the project's recorded tracker key — display-only residue, not a
    # lookup handle, so there is nothing to reconcile on the way back.
    catchup.disconnect_jira(PROJECT)
    mode = project_config.get_mode(PROJECT)
    assert mode['mode'] == 'local'
    assert mode['jiraProjectKey'] == 'TP'

    item = store.get_work_item(item_id)
    assert item.id == item_id
