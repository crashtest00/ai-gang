"""
durable-a2a-message-record.md REQ-01, REQ-02, REQ-03 and REQ-05 — the Task
record, its messages in order, their artifacts, and idempotency on the A2A
`messageId`.

Through the production path end to end: ScrumMaster's own `dispatchTask` and
gateway consumer wiring (tests/a2a_scrummaster_driver.js) publish the
dispatches and the verdicts, the real submit tool and `gateway-publish.js` the
submissions, and core's real `core-a2a-archive` and command consumers write the
record. An input both agent tools refuse is `XADD`ed, as each Acceptance says.
"""

from __future__ import annotations

import json
import threading
import time
import uuid

import pytest
from django.db import DatabaseError, connection, transaction
from django.test import Client

from workitems import store

from tests.a2a_support import (  # noqa: F401 - a2a_db and core are pytest fixtures, used by name
    BACKEND_STREAM, GATEWAY_STREAM, PROJECT, A2AArtifact, A2AConflict, A2AMessage, A2ATask, A2AVerdict, a2a_db,
    agent_message, core, gateway_envelope, gateway_publish, last_entry, message_view, new_work_item,
    outbox_rows, pending_in_archive_group, publish_verdict, record, scrummaster, stored, submit_tool, submitted,
    wait_archive_drained, wait_for, wait_reading, wait_stored, xadd,
)


def state_history(task_id):
    return [(h['state'], h['messageId'], h['verdict']) for h in record(task_id)['stateHistory']]


def message_ids_in_order(task_id):
    return [m['messageId'] for m in record(task_id)['messages']]


def fail_at(monkeypatch, message_id: str, *, times: int = 1, point: str = 'message_inserted'):
    """Make core's archive write of one message fail `times` times, after its
    insert and before its commit."""
    state = {'left': times}
    real = store._a2a_write_checkpoint

    def checkpoint(where, **detail):
        if where == point and detail.get('message_id') == message_id and state['left'] > 0:
            state['left'] -= 1
            raise RuntimeError(f'injected failure writing {message_id}')
        real(where, **detail)

    monkeypatch.setattr(store, '_a2a_write_checkpoint', checkpoint)
    return state


# ---------------------------------------------------------------------------
# REQ-01 — the durable Task record and its state history
# ---------------------------------------------------------------------------

def test_req01_task_record_from_the_dispatch_then_states_from_applied_submissions(a2a_db, redis_client, core):
    work_item_id = new_work_item()
    core.start_archive().start_command()
    with scrummaster() as sm:
        sm.start_gateway()
        dispatched = sm.dispatch(work_item_id)
        wait_stored(dispatched['messageId'])
        _entry, dispatch_envelope = last_entry(redis_client, BACKEND_STREAM)

        rec = record(work_item_id)
        assert rec['id'] == dispatch_envelope['taskId'] == work_item_id
        assert rec['contextId'] == dispatch_envelope['contextId']
        assert rec['project'] == dispatch_envelope['project']
        assert rec['workItemId'] == work_item_id and rec['associationError'] is None
        assert rec['state'] is None and rec['stateHistory'] == [], 'a dispatch records no state'

        _e, working = submitted(redis_client, submit_tool(dispatched, ['progress', '--text', 'started']))
        _e, waiting = submitted(redis_client, submit_tool(dispatched, ['input-required', '--text', 'which column?']))
        working_id = working['payload']['message']['messageId']
        waiting_id = waiting['payload']['message']['messageId']
        wait_reading(work_item_id, working_id, verdict='applied')
        wait_reading(work_item_id, waiting_id, verdict='applied')

    assert record(work_item_id)['state'] == 'input-required'
    assert state_history(work_item_id) == [('working', working_id, 'applied'),
                                           ('input-required', waiting_id, 'applied')]


def test_req01_a_submission_for_a_task_core_holds_nothing_for_creates_the_task_record(a2a_db, redis_client, core):
    task_id = new_work_item()
    message = agent_message(task_id, task_id)
    core.start_archive([GATEWAY_STREAM])
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': message},
                                                        context_id='ctx-from-envelope'))
    wait_stored(message['messageId'])
    task = A2ATask.objects.get(id=task_id)
    assert task.created_from_message_id == message['messageId']
    assert task.context_id == 'ctx-from-envelope' and task.project == PROJECT


def test_req01_a_retried_earlier_submission_takes_its_place_without_becoming_the_current_state(
        a2a_db, redis_client, core, monkeypatch):
    work_item_id = new_work_item()
    core.start_command()
    with scrummaster() as sm:
        core.start_archive([BACKEND_STREAM])
        dispatched = sm.dispatch(work_item_id)
        wait_stored(dispatched['messageId'])
        sm.start_gateway()

        # The archive write of `working` fails once and is held off; the later
        # `completed` commits first; then `working` is reclaimed.
        core.timing['retry_delay_ms'] = 60_000
        core.start_archive([GATEWAY_STREAM])
        _e, working = submitted(redis_client, submit_tool(dispatched, ['progress', '--text', 'working']))
        working_id = working['payload']['message']['messageId']
        injected = fail_at(monkeypatch, working_id)
        wait_for(lambda: injected['left'] == 0, expected='the working write to fail once')
        _e, completed = submitted(redis_client, submit_tool(dispatched, ['completed', '--text', 'done']))
        completed_id = completed['payload']['message']['messageId']
        wait_stored(completed_id)
        assert not stored(working_id)

        core.stop_archive([GATEWAY_STREAM])
        core.timing['retry_delay_ms'] = 100
        core.start_archive([GATEWAY_STREAM])
        wait_stored(working_id)
        wait_reading(work_item_id, working_id, verdict='applied')
        wait_reading(work_item_id, completed_id, verdict='applied')

    assert state_history(work_item_id) == [('working', working_id, 'applied'), ('completed', completed_id, 'applied')]
    assert record(work_item_id)['state'] == 'completed'


def test_req01_two_consumers_creating_one_task_record_concurrently_find_it(
        a2a_db, redis_client, core, monkeypatch):
    work_item_id = new_work_item()
    with scrummaster() as sm:
        dispatched = sm.dispatch(work_item_id)
        _e, submission = submitted(redis_client, submit_tool(dispatched, ['progress', '--text', 'concurrent']))
    submission_id = submission['payload']['message']['messageId']

    held = {'first': None, 'other_waited': False}
    real = store._a2a_write_checkpoint

    def hold_first_task_insert(where, **detail):
        if where == 'task_inserted' and held['first'] is None:
            held['first'] = detail['message_id']
            # Hold this Task-record insert open until the other consumer's own
            # insert of the same record is waiting on it.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    # PostgreSQL snapshots backend status once per transaction;
                    # this runs inside the open one, so drop the snapshot or
                    # every poll after the first repeats the first answer.
                    cursor.execute('SELECT pg_stat_clear_snapshot()')
                    cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                   "AND query LIKE 'INSERT INTO \"a2a_task\"%%'")
                    if cursor.fetchone()[0] > 0:
                        held['other_waited'] = True
                        break
                time.sleep(0.02)
        real(where, **detail)

    monkeypatch.setattr(store, '_a2a_write_checkpoint', hold_first_task_insert)
    core.start_archive([GATEWAY_STREAM, BACKEND_STREAM])
    # Above the hold's own 10 s deadline, so a slow second consumer cannot
    # make these waits expire before the hold does.
    wait_stored(dispatched['messageId'], timeout_s=30)
    wait_stored(submission_id, timeout_s=30)
    wait_archive_drained(redis_client, GATEWAY_STREAM, timeout_s=30)
    wait_archive_drained(redis_client, BACKEND_STREAM, timeout_s=30)

    assert held['other_waited'], 'the second consumer reached its own Task-record insert while the first was open'
    assert A2ATask.objects.filter(id=work_item_id).count() == 1
    assert pending_in_archive_group(redis_client, GATEWAY_STREAM) == 0
    assert pending_in_archive_group(redis_client, BACKEND_STREAM) == 0
    assert len(outbox_rows('a2a.task_recorded', taskId=work_item_id)) == 1


def test_req01_a_replay_stored_before_the_original_is_placed_at_the_replays_entry(
        a2a_db, redis_client, core, monkeypatch):
    task_id = new_work_item()
    message = agent_message(task_id, task_id)
    original = gateway_envelope(task_id, {'state': 'working', 'message': message}, context_id=task_id)
    injected = fail_at(monkeypatch, message['messageId'])
    core.timing['retry_delay_ms'] = 60_000
    core.start_archive([GATEWAY_STREAM])
    original_entry = xadd(redis_client, GATEWAY_STREAM, original)
    wait_for(lambda: injected['left'] == 0, expected='the original write to fail once')

    # What `streams.replay` re-adds: the same envelope, with correlationId set
    # to its messageId (services/scrummaster/src/streams.js).
    replay_entry = xadd(redis_client, GATEWAY_STREAM, {**original, 'correlationId': original['messageId']})
    wait_stored(message['messageId'])
    assert A2AMessage.objects.get(message_id=message['messageId']).entry_id == replay_entry

    core.stop_archive()
    core.timing['retry_delay_ms'] = 100
    core.start_archive([GATEWAY_STREAM])
    wait_archive_drained(redis_client, GATEWAY_STREAM)
    row = A2AMessage.objects.get(message_id=message['messageId'])
    assert row.entry_id == replay_entry and row.entry_id != original_entry
    assert A2AMessage.objects.filter(message_id=message['messageId']).count() == 1
    assert A2AConflict.objects.count() == 0, 'reclaiming the original changes nothing'


def test_req01_submissions_the_gateway_drops_are_stored_and_acknowledged(a2a_db, redis_client, core):
    """With ScrumMaster's gateway consumer stopped: a Task ScrumMaster does
    not hold, `role: client`, and `parts` not an array (the last two refused
    by gateway-publish.js)."""
    known = new_work_item()
    unknown = str(uuid.uuid4())
    entries = [
        agent_message(unknown, unknown),
        agent_message(known, known, role='client'),
        {**agent_message(known, known), 'parts': {'kind': 'text', 'text': 'not an array'}},
    ]
    core.start_archive([GATEWAY_STREAM])
    for message in entries:
        tid = message['taskId']
        xadd(redis_client, GATEWAY_STREAM, gateway_envelope(tid, {'state': 'working', 'message': message},
                                                            context_id=tid))
    wait_archive_drained(redis_client, GATEWAY_STREAM)
    for message in entries:
        assert stored(message['messageId'])
    row = A2AMessage.objects.get(message_id=entries[2]['messageId'])
    assert row.parts == {'kind': 'text', 'text': 'not an array'}, 'malformed content is stored as it appeared'
    assert A2AMessage.objects.get(message_id=entries[1]['messageId']).role == 'client'


def test_req01_a_working_refused_after_completed_is_listed_refused_and_the_state_stays_completed(
        a2a_db, redis_client, core):
    work_item_id = new_work_item()
    core.start_archive().start_command()
    with scrummaster() as sm:
        sm.start_gateway()
        dispatched = sm.dispatch(work_item_id)
        _e, completed = submitted(redis_client, submit_tool(dispatched, ['completed', '--text', 'done']))
        completed_id = completed['payload']['message']['messageId']
        wait_reading(work_item_id, completed_id, verdict='applied')
        _e, late = submitted(redis_client, submit_tool(dispatched, ['progress', '--text', 'one more thing']))
        late_id = late['payload']['message']['messageId']
        late_view = wait_reading(work_item_id, late_id, verdict='refused')
        sm_state = sm.task_state(work_item_id)['state']

    assert late_view['reason'] == 'task_finished' and late_view['finishedState'] == 'completed'
    assert stored(completed_id) and stored(late_id)
    assert state_history(work_item_id) == [('completed', completed_id, 'applied'), ('working', late_id, 'refused')]
    assert record(work_item_id)['state'] == 'completed' == sm_state, "as taskStore's is"


def test_req01_context_id_from_the_message_and_no_state_for_an_empty_one(a2a_db, redis_client, core):
    task_id = new_work_item()
    first = agent_message(task_id, 'ctx-from-message')
    second = agent_message(task_id, 'ctx-from-message')
    core.start_archive([GATEWAY_STREAM]).start_command()
    first_envelope = gateway_envelope(task_id, {'state': 'working', 'message': first})
    first_envelope['contextId'] = None
    xadd(redis_client, GATEWAY_STREAM, first_envelope)
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': '', 'message': second}))
    for message in (first, second):
        publish_verdict(redis_client, {'taskId': task_id, 'messageId': message['messageId'], 'verdict': 'applied'})
    wait_reading(task_id, first['messageId'], verdict='applied')
    wait_reading(task_id, second['messageId'], verdict='applied')

    assert A2ATask.objects.get(id=task_id).context_id == 'ctx-from-message'
    assert A2AMessage.objects.get(message_id=second['messageId']).state is None
    assert record(task_id)['state'] == 'working'


def test_req01_a_message_with_no_verdict_sets_no_state(a2a_db, redis_client, core):
    work_item_id = new_work_item()
    core.start_archive().start_command()
    with scrummaster() as sm:
        sm.start_gateway()
        dispatched = sm.dispatch(work_item_id)
        _e, working = submitted(redis_client, submit_tool(dispatched, ['progress', '--text', 'going']))
        wait_reading(work_item_id, working['payload']['message']['messageId'], verdict='applied')
        sm.stop_gateway()  # no verdict is published from here on
        _e, failed = submitted(redis_client, submit_tool(dispatched, ['failed', '--text', 'broke']))
    failed_id = failed['payload']['message']['messageId']
    wait_stored(failed_id)

    assert ('failed', failed_id, None) in state_history(work_item_id)
    assert record(work_item_id)['state'] == 'working'


@pytest.mark.parametrize('first_to_commit', ['archive', 'verdict'])
def test_req01_the_current_state_is_the_same_whichever_transaction_commits_first(
        a2a_db, redis_client, core, monkeypatch, first_to_commit):
    from workitems import command_consumer

    task_id = new_work_item()
    seed = agent_message(task_id, task_id)
    message = agent_message(task_id, task_id)
    core.start_archive([GATEWAY_STREAM]).start_command()
    # A Task with a current state already, so the later one is visibly the
    # submission's.
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': seed},
                                                        context_id=task_id))
    publish_verdict(redis_client, {'taskId': task_id, 'messageId': seed['messageId'], 'verdict': 'applied'})
    wait_reading(task_id, seed['messageId'], verdict='applied')
    core.stop_command()

    verdict_started, release_archive, release_verdict = threading.Event(), threading.Event(), threading.Event()

    def verdict_handler(envelope):
        with transaction.atomic():
            command_consumer._handler(envelope)  # the verdict row, inserted, not yet committed
            verdict_started.set()
            if first_to_commit == 'archive':
                assert release_verdict.wait(10)

    real = store._a2a_write_checkpoint

    def hold_archive(where, **detail):
        if where == 'message_inserted' and detail.get('message_id') == message['messageId']:
            assert verdict_started.wait(10), "the command consumer's transaction never started"
            if first_to_commit == 'verdict':
                assert release_archive.wait(10)
        real(where, **detail)

    monkeypatch.setattr(store, '_a2a_write_checkpoint', hold_archive)
    core.start_command(handler=verdict_handler)
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'input-required', 'message': message},
                                                        context_id=task_id))
    publish_verdict(redis_client, {'taskId': task_id, 'messageId': message['messageId'], 'verdict': 'applied'})
    assert verdict_started.wait(10)

    if first_to_commit == 'archive':
        wait_stored(message['messageId'])
        assert not A2AVerdict.objects.filter(message_id=message['messageId']).exists()
        release_verdict.set()
    else:
        wait_for(lambda: A2AVerdict.objects.filter(message_id=message['messageId']).exists(),
                 expected='the verdict committed first')
        assert not stored(message['messageId'])
        release_archive.set()

    wait_reading(task_id, message['messageId'], verdict='applied')
    client = Client()
    read = client.get(f'/a2a/tasks/{task_id}').json()
    assert read['state'] == 'input-required'


# ---------------------------------------------------------------------------
# REQ-02 — messages are durable, ordered and append-only
# ---------------------------------------------------------------------------

def test_req02_a_multi_message_task_reads_back_whole_and_in_order_after_scrummaster_restarts(
        a2a_db, redis_client, core):
    """PRD AC-01."""
    work_item_id = new_work_item()
    core.start_archive().start_command()
    published = []  # (entry id, envelope) as XRANGE returned them, in publish order
    with scrummaster() as sm:
        sm.start_gateway()
        dispatched = sm.dispatch(work_item_id)
        published.append(last_entry(redis_client, BACKEND_STREAM))
        published.append(submitted(redis_client, submit_tool(
            dispatched, ['comment', '--text', 'migration written', '--reference-file', 'db/up.sql'])))
        first_tool_id = published[-1][1]['payload']['message']['messageId']
        stamp = '2026-10-12T10:00:00.000Z'
        for text in ('first of a pair', 'second of a pair'):
            gateway_publish({'state': 'working', 'message': {
                'kind': 'message', 'messageId': str(uuid.uuid4()), 'taskId': work_item_id,
                'contextId': dispatched['contextId'], 'role': 'agent', 'timestamp': stamp,
                'referenceMessageId': first_tool_id,
                'parts': [{'kind': 'text', 'text': text}, {'kind': 'data', 'data': {'operation': 'comment',
                                                                                       'nested': {'n': [1, 2]}}}],
            }})
            published.append(last_entry(redis_client, GATEWAY_STREAM))
        published.append(submitted(redis_client, submit_tool(
            dispatched, ['completed', '--text', 'done', '--pull-request', 'https://git.example/pr/9'])))
        last_id = published[-1][1]['payload']['message']['messageId']
        wait_reading(work_item_id, last_id, verdict='applied', outcome='succeeded')
    with scrummaster():
        pass  # a restarted ScrumMaster, its taskStore empty: the record does not depend on it

    rec = record(work_item_id)
    assert [m['messageId'] for m in rec['messages']] == [
        (env['payload']['message'] if 'message' in env['payload'] else env['payload'])['messageId']
        for _entry, env in published
    ]
    for view, (entry_id, envelope) in zip(rec['messages'], published):
        is_dispatch = envelope['kind'] == 'task'
        message = envelope['payload'] if is_dispatch else envelope['payload']['message']
        assert view['entryId'] == entry_id
        assert view['stream'] == (BACKEND_STREAM if is_dispatch else GATEWAY_STREAM)
        assert view['envelope'] == envelope, 'the stored envelope equals the entry data XRANGE returned'
        assert json.dumps(view['parts'], sort_keys=True) == json.dumps(message['parts'], sort_keys=True)
        assert view['referenceMessageId'] == message.get('referenceMessageId')
        assert view['state'] == (None if is_dispatch else envelope['payload']['state'])
        assert view['recordedAt']
    assert rec['messages'][2]['timestamp'] == rec['messages'][3]['timestamp'] == stamp
    assert rec['artifacts'] and rec['artifacts'][0]['messageId'] == last_id
    assert [h['state'] for h in rec['stateHistory']] == ['working', 'working', 'working', 'completed']
    assert rec['messages'][1]['referenceMessageId'] == dispatched['messageId'], 'the lineage back to the dispatch'


def test_req02_a_backlog_committed_out_of_order_still_reads_in_stream_entry_id_order(a2a_db, redis_client, core):
    work_item_id = new_work_item()
    with scrummaster() as sm:
        dispatched = sm.dispatch(work_item_id)
        _e, first = submitted(redis_client, submit_tool(dispatched, ['progress', '--text', 'one']))
        _e, second = submitted(redis_client, submit_tool(dispatched, ['progress', '--text', 'two']))
    submission_ids = [first['payload']['message']['messageId'], second['payload']['message']['messageId']]

    core.start_archive([GATEWAY_STREAM])
    for message_id in submission_ids:
        wait_stored(message_id)
    core.start_archive([BACKEND_STREAM])  # the dispatch commits last
    wait_stored(dispatched['messageId'])

    dispatch_row = A2AMessage.objects.get(message_id=dispatched['messageId'])
    assert dispatch_row.recorded_at > A2AMessage.objects.get(message_id=submission_ids[1]).recorded_at
    assert message_ids_in_order(work_item_id) == [dispatched['messageId'], *submission_ids]


def test_req02_a_same_millisecond_tie_between_streams_reads_by_stream_name(a2a_db, redis_client, core):
    task_id = new_work_item()
    ms = int(time.time() * 1000) + 60_000
    dispatch = agent_message(task_id, task_id, role='client')
    submission = agent_message(task_id, task_id)
    # BACKEND_STREAM ('aigang:agent:…') sorts before GATEWAY_STREAM
    # ('aigang:gateway:…'); the gateway entry gets the lower sequence number.
    assert BACKEND_STREAM < GATEWAY_STREAM
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': submission},
                                                        context_id=task_id), entry_id=f'{ms}-1')
    xadd(redis_client, BACKEND_STREAM, gateway_envelope(task_id, dispatch, kind='task', context_id=task_id),
         entry_id=f'{ms}-5')
    core.start_archive([GATEWAY_STREAM, BACKEND_STREAM])
    wait_stored(dispatch['messageId'])
    wait_stored(submission['messageId'])
    assert message_ids_in_order(task_id) == [dispatch['messageId'], submission['messageId']]


def test_req02_a_stored_message_row_cannot_be_updated_or_deleted(a2a_db, redis_client, core):
    task_id = new_work_item()
    message = agent_message(task_id, task_id)
    core.start_archive([GATEWAY_STREAM])
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': message},
                                                        context_id=task_id))
    wait_stored(message['messageId'])
    for sql in ("UPDATE a2a_message SET state = 'completed' WHERE message_id = %s",
                'DELETE FROM a2a_message WHERE message_id = %s'):
        with pytest.raises(DatabaseError, match='a2a_message is append-only'):
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(sql, [message['messageId']])
    assert A2AMessage.objects.get(message_id=message['messageId']).state == 'working'


# ---------------------------------------------------------------------------
# REQ-03 — artifacts are durable with their lineage
# ---------------------------------------------------------------------------

def test_req03_an_artifact_with_a_reference_artifact_id_keeps_its_message_and_its_link(a2a_db, redis_client, core):
    work_item_id = new_work_item()
    core.start_archive()
    with scrummaster() as sm:
        dispatched = sm.dispatch(work_item_id)
    gateway_publish({'state': 'completed', 'message': {
        'kind': 'message', 'messageId': str(uuid.uuid4()), 'taskId': work_item_id,
        'contextId': dispatched['contextId'], 'role': 'agent', 'referenceMessageId': dispatched['messageId'],
        'parts': [{'kind': 'text', 'text': 'v2 of the report'}],
    }, 'artifacts': [{'kind': 'artifact', 'artifactId': 'art-v2', 'taskId': work_item_id, 'name': 'report',
                      'referenceArtifactId': 'art-v1', 'parts': [{'kind': 'text', 'text': 'body'}]}]})
    _entry, envelope = last_entry(redis_client, GATEWAY_STREAM)
    message_id = envelope['payload']['message']['messageId']
    wait_stored(message_id)

    view = message_view(work_item_id, message_id)
    assert [(a['artifactId'], a['referenceArtifactId'], a['messageId']) for a in view['artifacts']] == [
        ('art-v2', 'art-v1', message_id)]
    assert record(work_item_id)['artifacts'][0]['parts'] == [{'kind': 'text', 'text': 'body'}]


def test_req03_a_repeated_artifact_id_is_stored_once_per_carrying_and_an_unidentified_one_stays_in_the_envelope(
        a2a_db, redis_client, core):
    task_id = new_work_item()
    first = agent_message(task_id, task_id)
    second = agent_message(task_id, task_id)
    core.start_archive([GATEWAY_STREAM])
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': first, 'artifacts': [
        {'kind': 'artifact', 'artifactId': 'dup', 'name': 'first name', 'parts': []},
        {'kind': 'artifact', 'artifactId': 'dup', 'name': 'second name', 'parts': []},
        {'kind': 'artifact', 'name': 'no id', 'parts': []},
    ]}, context_id=task_id))
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': second, 'artifacts': [
        {'kind': 'artifact', 'artifactId': 'dup', 'name': 'again', 'parts': []},
    ]}, context_id=task_id))
    wait_archive_drained(redis_client, GATEWAY_STREAM)
    assert stored(first['messageId']) and stored(second['messageId'])
    assert pending_in_archive_group(redis_client, GATEWAY_STREAM) == 0

    first_view = message_view(task_id, first['messageId'])
    assert [(a['artifactId'], a['name']) for a in first_view['artifacts']] == [('dup', 'first name'),
                                                                               ('dup', 'second name')]
    assert [(a['artifactId'], a['name']) for a in message_view(task_id, second['messageId'])['artifacts']] == [
        ('dup', 'again')]
    assert not A2AArtifact.objects.filter(name='no id').exists()
    assert {'kind': 'artifact', 'name': 'no id', 'parts': []} in first_view['envelope']['payload']['artifacts']


# ---------------------------------------------------------------------------
# REQ-05 — idempotent on the A2A messageId; a conflict is recorded
# ---------------------------------------------------------------------------

def _fail_next_ack(consumer, count: int = 1):
    """Core's acknowledgement of the next `count` entries fails after the
    handler's transaction has committed."""
    real = consumer._client.xack
    state = {'left': count}

    def xack(*args, **kwargs):
        if state['left'] > 0:
            state['left'] -= 1
            raise ConnectionError('injected acknowledgement failure')
        return real(*args, **kwargs)

    consumer._client.xack = xack
    return state


def test_req05_one_message_under_two_envelopes_is_stored_once_and_both_are_acknowledged(
        a2a_db, redis_client, core):
    task_id = new_work_item()
    message = agent_message(task_id, task_id)
    core.start_archive([GATEWAY_STREAM])
    for _ in range(2):
        xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': message},
                                                            context_id=task_id))
    wait_archive_drained(redis_client, GATEWAY_STREAM)
    assert A2AMessage.objects.filter(message_id=message['messageId']).count() == 1
    assert A2AConflict.objects.count() == 0


def test_req05_an_entry_whose_acknowledgement_failed_is_stored_once_when_reclaimed(a2a_db, redis_client, core):
    task_id = new_work_item()
    message = agent_message(task_id, task_id)
    core.start_archive([GATEWAY_STREAM])
    failed_ack = _fail_next_ack(core.archive[0])
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': message},
                                                        context_id=task_id))
    wait_stored(message['messageId'])
    wait_for(lambda: failed_ack['left'] == 0, expected='the acknowledgement to fail once')
    wait_archive_drained(redis_client, GATEWAY_STREAM)
    assert A2AMessage.objects.filter(message_id=message['messageId']).count() == 1
    assert len(outbox_rows('a2a.message_recorded', messageId=message['messageId'])) == 1


def test_req05_altered_content_under_a_stored_message_id_is_rejected_and_recorded_once(a2a_db, redis_client, core):
    """ScrumMaster's gateway consumer is stopped (not running here), so it
    cannot retry the altered entry into a dead letter."""
    task_id = new_work_item()
    message = agent_message(task_id, task_id, text='original')
    core.start_archive([GATEWAY_STREAM])
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': message},
                                                        context_id=task_id))
    wait_stored(message['messageId'])

    failed_ack = _fail_next_ack(core.archive[0])
    altered = {**message, 'parts': [{'kind': 'text', 'text': 'altered'}]}
    conflict_entry = xadd(redis_client, GATEWAY_STREAM, gateway_envelope(
        task_id, {'state': 'working', 'message': altered}, context_id=task_id))
    wait_for(lambda: A2AConflict.objects.exists(), expected='the conflict recorded')
    wait_for(lambda: failed_ack['left'] == 0, expected='the acknowledgement to fail once')
    wait_archive_drained(redis_client, GATEWAY_STREAM)

    assert A2AMessage.objects.get(message_id=message['messageId']).parts == message['parts'], 'unchanged'
    assert A2AConflict.objects.count() == 1, 'recorded once, though the entry was delivered twice'
    conflicts = record(task_id)['conflicts']
    assert [(c['messageId'], c['entryId'], c['stream']) for c in conflicts] == [
        (message['messageId'], conflict_entry, GATEWAY_STREAM)]
    assert conflicts[0]['envelope']['payload']['message']['parts'] == altered['parts']

    rows = outbox_rows('a2a.conflict_recorded', messageId=message['messageId'])
    assert len(rows) == 1 and rows[0].payload == {'taskId': task_id, 'messageId': message['messageId'],
                                                  'stream': GATEWAY_STREAM, 'entryId': conflict_entry}
    conflict_tx = A2AConflict.objects.get().change_xid % 4294967296
    with connection.cursor() as cursor:
        cursor.execute('SELECT xmin::text::bigint FROM outbox_event WHERE id = %s', [rows[0].id])
        assert cursor.fetchone()[0] == conflict_tx, 'the conflict and its outbox row: one transaction'

    # A different state, or different artifacts, is a conflict too.
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'completed', 'message': message},
                                                        context_id=task_id))
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': message, 'artifacts': [
        {'kind': 'artifact', 'artifactId': 'a', 'parts': []}]}, context_id=task_id))
    wait_archive_drained(redis_client, GATEWAY_STREAM)
    assert A2AConflict.objects.count() == 3


def test_req05_the_change_set_adds_no_write_route():
    client = Client()
    for path in ('/a2a/tasks', f'/a2a/tasks/{uuid.uuid4()}'):
        for method in ('post', 'put', 'patch', 'delete'):
            response = getattr(client, method)(path, data='{}', content_type='application/json')
            assert response.status_code == 405, f'{method.upper()} {path} must not be accepted'
