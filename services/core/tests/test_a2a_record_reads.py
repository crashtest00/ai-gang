"""
durable-a2a-message-record.md REQ-06 (the direct read), REQ-07 (permanence
past ScrumMaster's trim), REQ-09 (association by canonical id), REQ-11 (the
changed-since enumeration), REQ-12 (the session id, as core stores it) and §4
"Volume" (the health view).

The reads go through the HTTP routes, `GET /a2a/tasks/<id>` and
`GET /a2a/tasks`, and the health view, `GET /health`. The record they read is
written as in production: ScrumMaster's own dispatch and gateway
(tests/a2a_scrummaster_driver.js), the real submit tool, and core's
`core-a2a-archive` and command consumers.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid

from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from workitems import project_config
from workitems.models import AccessLog, WorkItem

from tests.a2a_support import (  # noqa: F401 - a2a_db and core are pytest fixtures, used by name
    BACKEND_STREAM, GATEWAY_STREAM, PROJECT, SCRUMMASTER_DIR, A2ATask, a2a_db, agent_message, core,
    gateway_envelope, gateway_publish, last_entry, message_view, new_work_item, publish_verdict, record,
    scrummaster, stored, submit_tool, submitted, wait_archive_drained, wait_for, wait_reading, wait_stored, xadd,
)


def message_id_of(envelope: dict) -> str:
    return envelope['payload']['message']['messageId']


# ---------------------------------------------------------------------------
# REQ-06 — a direct, access-logged read
# ---------------------------------------------------------------------------

def test_req06_a_tasks_full_record_is_a_synchronous_read_logged_in_access_log(a2a_db, redis_client, core):
    work_item_id = new_work_item()
    core.start_archive().start_command()
    with scrummaster() as sm:
        sm.start_gateway()
        dispatched = sm.dispatch(work_item_id)
        comment = message_id_of(submitted(redis_client, submit_tool(dispatched, ['comment', '--text', 'hi']))[1])
        wait_reading(work_item_id, comment, verdict='applied', outcome='succeeded')
        sm.stop_gateway()  # the next submission gets no verdict
        quiet = message_id_of(submitted(redis_client, submit_tool(dispatched, ['progress', '--text', 'quiet']))[1])
        wait_stored(quiet)

    before = AccessLog.objects.count()
    response = Client().get(f'/a2a/tasks/{work_item_id}', HTTP_X_ACTOR='reader-under-test')
    assert response.status_code == 200
    body = response.json()

    by_id = {m['messageId']: m for m in body['messages']}
    assert (by_id[comment]['verdict'], by_id[comment]['reason'], by_id[comment]['outcome']) == (
        'applied', None, 'succeeded')
    assert (by_id[quiet]['verdict'], by_id[quiet]['outcome']) == (None, None), 'no verdict yet'
    assert by_id[dispatched['messageId']]['verdict'] is None, 'a dispatch has no verdict'
    assert body['state'] == 'working'

    logged = AccessLog.objects.order_by('-id').first()
    assert AccessLog.objects.count() == before + 1
    assert (logged.operation, logged.actor, str(logged.work_item_id), logged.project) == (
        'getA2ATask', 'reader-under-test', work_item_id, PROJECT)

    assert Client().get(f'/a2a/tasks/{uuid.uuid4()}').status_code == 404


# ---------------------------------------------------------------------------
# REQ-07 — the record outlives ScrumMaster's trim (PRD AC-02)
# ---------------------------------------------------------------------------

def test_req07_the_record_reads_back_whole_after_scrummasters_trim_removes_its_entries(a2a_db, redis_client, core):
    work_item_id = new_work_item()
    core.start_archive().start_command()
    with scrummaster() as sm:
        sm.start_gateway()  # acknowledges every gateway entry in `scrummaster`
        dispatched = sm.dispatch(work_item_id)
        first = message_id_of(submitted(redis_client, submit_tool(dispatched, ['comment', '--text', 'one']))[1])
        last = message_id_of(submitted(redis_client, submit_tool(
            dispatched, ['completed', '--text', 'done', '--pull-request', 'https://git.example/pr/1']))[1])
        wait_reading(work_item_id, last, verdict='applied', outcome='succeeded')
        wait_reading(work_item_id, first, verdict='applied', outcome='succeeded')
    wait_archive_drained(redis_client, GATEWAY_STREAM)
    wait_archive_drained(redis_client, BACKEND_STREAM)

    # The project container's subscriber acknowledges the dispatch in its own
    # group, `agent-backend` (setup/subscriber.js).
    redis_client.xgroup_create(BACKEND_STREAM, 'agent-backend', id='0', mkstream=True)
    for _stream, entries in redis_client.xreadgroup('agent-backend', 'subscriber', {BACKEND_STREAM: '>'}):
        for entry_id, _fields in entries:
            redis_client.xack(BACKEND_STREAM, 'agent-backend', entry_id)
    before = record(work_item_id)

    # ScrumMaster's own trimAcknowledged, eight days later, on the pairs
    # everyStreamGroup gives it.
    script = r"""
      const streams = require('./src/streams');
      const { createClient } = require('redis');
      const realNow = Date.now;
      Date.now = () => realNow() + 8 * 24 * 60 * 60 * 1000;
      (async () => {
        const client = createClient({ url: process.argv[1] });
        await client.connect();
        await streams.trimAcknowledged(client, process.argv[2], 'scrummaster');
        await streams.trimAcknowledged(client, process.argv[3], 'agent-backend');
        await client.quit();
      })().catch(e => { console.error(e); process.exit(1); });
    """
    trimmed = subprocess.run(['node', '-e', script, os.environ['REDIS_TEST_URL'], GATEWAY_STREAM, BACKEND_STREAM],
                             cwd=str(SCRUMMASTER_DIR), capture_output=True, text=True, timeout=30)
    assert trimmed.returncode == 0, trimmed.stderr
    assert redis_client.xlen(GATEWAY_STREAM) == 0 and redis_client.xlen(BACKEND_STREAM) == 0, 'the entries are gone'

    after = record(work_item_id)
    assert after == before
    assert [m['messageId'] for m in after['messages']] == [dispatched['messageId'], first, last]
    assert after['artifacts'] and after['state'] == 'completed'


# ---------------------------------------------------------------------------
# REQ-09 — the association resolves by canonical id, in both modes
# ---------------------------------------------------------------------------

def test_req09_the_association_is_the_work_item_whose_id_is_the_task_id_in_both_modes(
        a2a_db, redis_client, core, monkeypatch):
    from workitems import store

    local_item = new_work_item()
    jira_item = new_work_item(display_name='Jira-mode item')
    # A Jira-mode item carries an external key; the association must not use it.
    WorkItem.objects.filter(id=jira_item).update(external_key='TP-9')
    queries = []
    real = store.record_a2a_message

    def recording(**kwargs):
        with CaptureQueriesContext(connection) as captured:
            result = real(**kwargs)
        queries.extend(q['sql'] for q in captured.captured_queries)
        return result

    monkeypatch.setattr(store, 'record_a2a_message', recording)
    core.start_archive([GATEWAY_STREAM])
    local_message = agent_message(local_item, local_item)
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(local_item, {'state': 'working', 'message': local_message},
                                                        context_id=local_item))
    wait_stored(local_message['messageId'])

    project_config.set_mode(PROJECT, project_config.JIRA, jira_project_key='TP')
    jira_message = agent_message(jira_item, jira_item)
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(jira_item, {'state': 'working', 'message': jira_message},
                                                        context_id=jira_item))
    wait_stored(jira_message['messageId'])

    assert str(A2ATask.objects.get(id=local_item).work_item_id) == local_item
    assert str(A2ATask.objects.get(id=jira_item).work_item_id) == jira_item
    assert queries and not [q for q in queries if 'external_key' in q], 'no read of external_key on this path'


# ---------------------------------------------------------------------------
# REQ-11 — the paged, changed-since enumeration
# ---------------------------------------------------------------------------

def enumerate_tasks(cursor: str | None, limit: int = 2) -> tuple[list[dict], str]:
    """Every page of one round, through the HTTP route; returns the Tasks and
    the cursor for the next round."""
    tasks, client = [], Client()
    while True:
        query = {'limit': limit}
        if cursor:
            query['cursor'] = cursor
        page = client.get('/a2a/tasks', query).json()
        tasks.extend(page['tasks'])
        cursor = page['nextCursor']
        if not page['hasMore']:
            return tasks, cursor


def test_req11_a_paged_changed_since_enumeration_returns_each_changed_task_alone_with_its_record(
        a2a_db, redis_client, core):
    items = [new_work_item(display_name=f'Task {i}') for i in range(3)]
    core.start_archive().start_command()
    with scrummaster() as sm:
        sm.start_gateway()
        dispatched = [sm.dispatch(item) for item in items]
        progress = [message_id_of(submitted(redis_client, submit_tool(d, ['progress', '--text', 'begun']))[1])
                    for d in dispatched]
        for item, message_id in zip(items, progress):
            wait_reading(item, message_id, verdict='applied', outcome='succeeded')

        access_rows = AccessLog.objects.count()
        tasks, cursor = enumerate_tasks(None, limit=2)
        assert sorted(t['id'] for t in tasks) == sorted(items), 'all three, across pages'
        for task in tasks:
            assert task == record(task['id']), 'each with its full record, as REQ-06 returns it'
            assert len(task['messages']) == 2 and task['stateHistory'] and task['state'] == 'working'

        def changed_alone(expected_item, before_cursor):
            changed, next_cursor = enumerate_tasks(before_cursor)
            assert [t['id'] for t in changed] == [expected_item]
            return changed[0], next_cursor

        # A message on the first.
        note = message_id_of(submitted(redis_client, submit_tool(dispatched[0], ['comment', '--text', 'more']))[1])
        wait_reading(items[0], note, verdict='applied', outcome='succeeded')
        task, cursor = changed_alone(items[0], cursor)
        assert note in [m['messageId'] for m in task['messages']]

        # A continuation dispatched to the second.
        continuation = sm.dispatch(items[1])
        wait_stored(continuation['messageId'])
        _task, cursor = changed_alone(items[1], cursor)

        # An artifact on the third.
        done = message_id_of(submitted(redis_client, submit_tool(
            dispatched[2], ['completed', '--text', 'done', '--pull-request', 'https://git.example/pr/3']))[1])
        wait_reading(items[2], done, verdict='applied', outcome='succeeded')
        task, cursor = changed_alone(items[2], cursor)
        assert task['artifacts']

        # A content conflict on the first, with ScrumMaster's gateway consumer
        # stopped, as in REQ-05's Acceptance.
        sm.stop_gateway()
        stored_note = message_view(items[0], note)
        altered = {**stored_note['message'], 'parts': [{'kind': 'text', 'text': 'altered'}]}
        conflict_entry = xadd(redis_client, GATEWAY_STREAM, gateway_envelope(
            items[0], {'state': 'working', 'message': altered}, context_id=dispatched[0]['contextId']))
        wait_for(lambda: record(items[0])['conflicts'], expected='the conflict recorded')
        task, cursor = changed_alone(items[0], cursor)
        assert [c['entryId'] for c in task['conflicts']] == [conflict_entry]

        # A verdict row recording an outcome for a message of the second.
        publish_verdict(redis_client, {'taskId': items[1], 'messageId': progress[1], 'outcome': 'failed'})
        wait_reading(items[1], progress[1], outcome='failed')
        task, cursor = changed_alone(items[1], cursor)
        assert message_view(items[1], progress[1])['outcome'] == 'failed'
        assert next(m for m in task['messages'] if m['messageId'] == progress[1])['outcome'] == 'failed'

    assert AccessLog.objects.count() == access_rows, 'the enumeration writes no access-log row'


def test_req11_a_verdict_recorded_before_its_task_changes_the_task_once_its_record_exists(a2a_db, redis_client, core):
    task_id = new_work_item()
    message = agent_message(task_id, task_id)
    core.start_command()
    _tasks, cursor = enumerate_tasks(None)
    publish_verdict(redis_client, {'taskId': task_id, 'messageId': message['messageId'], 'verdict': 'applied'})
    wait_reading(task_id, message['messageId'], verdict='applied')
    changed, cursor_after_verdict = enumerate_tasks(cursor)
    assert changed == [], 'no Task record yet, so nothing to enumerate'

    core.start_archive([GATEWAY_STREAM])
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': message},
                                                        context_id=task_id))
    wait_stored(message['messageId'])
    changed, _ = enumerate_tasks(cursor_after_verdict)
    assert [t['id'] for t in changed] == [task_id] and changed[0]['state'] == 'working'


def test_req11_a_malformed_cursor_or_limit_is_a_client_error(a2a_db):
    client = Client()
    assert client.get('/a2a/tasks', {'cursor': 'not-a-cursor'}).status_code == 400
    assert client.get('/a2a/tasks', {'limit': '0'}).status_code == 400
    assert client.get('/a2a/tasks', {'limit': 'many'}).status_code == 400


# ---------------------------------------------------------------------------
# REQ-12 — the session id, as core stores it
# ---------------------------------------------------------------------------

def test_req12_agent_messages_carry_their_session_id_and_the_dispatch_and_raw_publishes_none(
        a2a_db, redis_client, core):
    work_item_id = new_work_item()
    session_id = str(uuid.uuid4())
    core.start_archive()
    with scrummaster() as sm:
        dispatched = sm.dispatch(work_item_id)
        first = message_id_of(submitted(redis_client, submit_tool(
            dispatched, ['progress', '--text', 'a'], session_id=session_id))[1])
        second = message_id_of(submitted(redis_client, submit_tool(
            dispatched, ['comment', '--text', 'b'], session_id=session_id))[1])
        refused = submit_tool(dispatched, ['comment', '--text', 'c'], unset=('CLAUDE_CODE_SESSION_ID',))
    raw = {'kind': 'message', 'messageId': str(uuid.uuid4()), 'taskId': work_item_id,
           'contextId': dispatched['contextId'], 'role': 'agent', 'parts': [{'kind': 'text', 'text': 'raw'}]}
    gateway_publish({'state': 'working', 'message': raw})
    for message_id in (dispatched['messageId'], first, second, raw['messageId']):
        wait_stored(message_id)
    wait_archive_drained(redis_client, GATEWAY_STREAM)

    assert message_view(work_item_id, first)['metadata'] == {'sessionId': session_id}
    assert message_view(work_item_id, second)['metadata'] == {'sessionId': session_id}
    assert message_view(work_item_id, dispatched['messageId'])['metadata'] is None, 'ScrumMaster carries none'
    assert message_view(work_item_id, raw['messageId'])['metadata'] is None, 'stored without one'
    assert refused.returncode != 0 and 'CLAUDE_CODE_SESSION_ID is not set' in refused.stderr
    assert len(record(work_item_id)['messages']) == 4, 'the refused submission stored nothing'


# ---------------------------------------------------------------------------
# §4 "Volume" — the health view reports the A2A tables
# ---------------------------------------------------------------------------

def test_volume_the_health_view_reports_each_a2a_tables_row_count_and_size(a2a_db, redis_client, core):
    task_id = new_work_item()
    core.start_archive([GATEWAY_STREAM])
    for _ in range(2):
        message = agent_message(task_id, task_id)
        xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': message},
                                                            context_id=task_id))
        wait_stored(message['messageId'])

    body = Client().get('/health').json()
    volume = {row['table']: row for row in body['a2aRecord']}
    assert set(volume) == {'a2a_task', 'a2a_message', 'a2a_artifact', 'a2a_conflict', 'a2a_verdict'}
    assert volume['a2a_task']['rowCount'] == 1 and volume['a2a_message']['rowCount'] == 2
    assert volume['a2a_verdict']['rowCount'] == 0
    assert all(isinstance(row['sizeBytes'], int) and row['sizeBytes'] > 0 for row in volume.values())
    assert 'streams' in body and body['status'] in ('ok', 'degraded', 'unhealthy')
