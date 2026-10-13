"""
durable-a2a-message-record.md REQ-04 — message verdict and outcome are
durable.

Every verdict here is published by ScrumMaster itself — `handleA2ASubmission`,
its dead-letter hook, and `dispatchTask` for `superseded` — run from its own
source by tests/a2a_scrummaster_driver.js, onto the real command stream, and
applied by core's real command consumer (`command_consumer._handler`). The
cases that test core's handling of a command shape ScrumMaster never publishes
(a redelivery, a malformed command, a late refusal) publish that command
directly, in the envelope `canonicalWorkItems.publishCommand` builds.

The topology is two configured projects, so that a submission can arrive on
another project's gateway stream (`wrong_project`).
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.db import DatabaseError, connection, transaction

from workitems import registry
from workitems.stream_topology import command_stream_name
from workitems.streams import dead_letter_stream_name

from tests.a2a_support import (  # noqa: F401 - a2a_db and core are pytest fixtures, used by name
    BACKEND_STREAM, GATEWAY_STREAM, PROJECT, A2AVerdict, CoreConsumers, a2a_db, agent_message, core,
    gateway_envelope, gateway_publish, last_entry, message_view, new_work_item, outbox_rows, publish_verdict,
    record, scrummaster, stored, submit_tool, submitted, verdict_rows, wait_archive_drained, wait_for,
    wait_reading, wait_stored, wait_verdict_rows, xadd,
)

OTHER_PROJECT = 'other-project'
OTHER_GATEWAY = registry.gateway_stream_name(OTHER_PROJECT)


@pytest.fixture(autouse=True)
def _reload_registry_after():
    yield
    registry.load(force=True)


@pytest.fixture(autouse=True)
def two_projects(settings, tmp_path):
    """The fixture catalog's agents, and a second project beside
    `test-project`. Core's registry and the ScrumMaster driver read the same
    two files."""
    with open(settings.AGENTS_CATALOG_PATH) as fh:
        catalog = json.load(fh)
    catalog_path, projects_path = tmp_path / 'agents.json', tmp_path / 'projects.json'
    catalog_path.write_text(json.dumps(catalog))
    projects_path.write_text(json.dumps({'projects': [
        {'name': PROJECT, 'jiraProjectKey': 'TP', 'agents': ['refinement-agent', 'backend-agent']},
        {'name': OTHER_PROJECT, 'jiraProjectKey': 'OP', 'agents': ['backend-agent']},
    ]}))
    settings.AGENTS_CATALOG_PATH, settings.PROJECTS_CONFIG_PATH = str(catalog_path), str(projects_path)
    registry.load(force=True)


def message_id_of(envelope: dict) -> str:
    return envelope['payload']['message']['messageId']


def refuse_reading(view):
    return view['verdict'], view['reason'], view['finishedState'], view['outcome']


# ---------------------------------------------------------------------------
# The verdicts and outcomes ScrumMaster publishes
# ---------------------------------------------------------------------------

def test_req04_a_late_submission_is_refused_task_finished_and_the_state_stays_completed(a2a_db, redis_client, core):
    work_item_id = new_work_item()
    core.start_archive().start_command()
    with scrummaster() as sm:
        sm.start_gateway()
        dispatched = sm.dispatch(work_item_id)
        completed = message_id_of(submitted(redis_client, submit_tool(dispatched, ['completed', '--text', 'done']))[1])
        wait_reading(work_item_id, completed, verdict='applied', outcome='succeeded')
        late = message_id_of(submitted(redis_client, submit_tool(dispatched, ['progress', '--text', 'late']))[1])
        view = wait_reading(work_item_id, late, verdict='refused')

    assert refuse_reading(view) == ('refused', 'task_finished', 'completed', None)
    assert record(work_item_id)['state'] == 'completed'
    [row] = verdict_rows(late)
    assert row.command['sourceMessageId'] == row.source_message_id and row.actor == 'scrummaster'


def test_req04_applied_then_succeeded_failed_superseded_and_a_dead_lettered_refusal(a2a_db, redis_client, core):
    work_item_id = new_work_item()
    core.start_archive().start_command()
    with scrummaster() as sm:
        sm.start_gateway()
        dispatched = sm.dispatch(work_item_id)
        comment = message_id_of(submitted(redis_client, submit_tool(dispatched, ['comment', '--text', 'hello']))[1])
        wait_reading(work_item_id, comment, verdict='applied', outcome='succeeded')

        # `reassign` with no agentFieldValue: both agent tools refuse it, so it
        # is XADDed; the gateway reports it and returns no outcome.
        reassign = agent_message(work_item_id, dispatched['contextId'], text='hand it over',
                                 data={'operation': 'reassign'}, reference_message_id=comment)
        xadd(redis_client, GATEWAY_STREAM, gateway_envelope(work_item_id, {'state': 'working', 'message': reassign},
                                                            context_id=dispatched['contextId']))
        wait_reading(work_item_id, reassign['messageId'], verdict='applied', outcome='failed')

        sm.dispatch(work_item_id)  # a reopen: dispatchTask turns the failed outcome superseded
        view = wait_reading(work_item_id, reassign['messageId'], outcome='superseded')
        assert [(r['verdict'], r['outcome']) for r in view['verdictRows']] == [
            ('applied', None), (None, 'failed'), (None, 'superseded')]

        # A successor naming the superseded message: checkMessageLineage
        # rejects it as permanent, before it is recorded on the Task.
        stale = {'kind': 'message', 'messageId': str(uuid.uuid4()), 'taskId': work_item_id,
                 'contextId': dispatched['contextId'], 'role': 'agent', 'referenceMessageId': reassign['messageId'],
                 'parts': [{'kind': 'text', 'text': 'stale successor'}]}
        gateway_publish({'state': 'working', 'message': stale})
        view = wait_reading(work_item_id, stale['messageId'], verdict='refused')
        assert refuse_reading(view) == ('refused', 'dead_lettered', None, None)

        sm.dispatch(work_item_id)  # a second reopen supersedes the dead-lettered message too
        wait_reading(work_item_id, stale['messageId'], verdict='refused', outcome='superseded')
        before_restart = record(work_item_id)

    with scrummaster():
        pass  # restarted: taskStore is empty, core's record is not
    after_restart = record(work_item_id)
    for message_id in (comment, reassign['messageId'], stale['messageId']):
        before = next(m for m in before_restart['messages'] if m['messageId'] == message_id)
        after = next(m for m in after_restart['messages'] if m['messageId'] == message_id)
        assert refuse_reading(after) == refuse_reading(before)
    assert refuse_reading(message_view(work_item_id, comment)) == ('applied', None, None, 'succeeded')


def test_req04_each_refusal_reason_and_no_refused_message_sets_the_state(a2a_db, redis_client, core, redis_factory):
    work_item_id = new_work_item()
    other_core = CoreConsumers(redis_factory, project=OTHER_PROJECT)
    core.start_archive().start_command()
    other_core.start_command()
    try:
        with scrummaster() as sm:
            sm.start_gateway()
            sm.start_gateway(project=OTHER_PROJECT)
            dispatched = sm.dispatch(work_item_id)
            working = message_id_of(submitted(redis_client, submit_tool(dispatched, ['progress', '--text', 'go']))[1])
            wait_reading(work_item_id, working, verdict='applied')

            def chained(**extra):
                return agent_message(work_item_id, dispatched['contextId'], reference_message_id=working, **extra)

            bad_state = chained()
            xadd(redis_client, GATEWAY_STREAM, gateway_envelope(
                work_item_id, {'state': 'not-a-task-state', 'message': bad_state}, context_id=dispatched['contextId']))
            client_role = chained(role='client')
            xadd(redis_client, GATEWAY_STREAM, gateway_envelope(
                work_item_id, {'state': 'working', 'message': client_role}, context_id=dispatched['contextId']))

            unknown_task = str(uuid.uuid4())
            not_held = {'kind': 'message', 'messageId': str(uuid.uuid4()), 'taskId': unknown_task,
                        'contextId': unknown_task, 'role': 'agent', 'parts': [{'kind': 'text', 'text': 'who?'}]}
            gateway_publish({'state': 'working', 'message': not_held})

            elsewhere = {'kind': 'message', 'messageId': str(uuid.uuid4()), 'taskId': work_item_id,
                         'contextId': dispatched['contextId'], 'role': 'agent', 'referenceMessageId': working,
                         'parts': [{'kind': 'text', 'text': 'wrong stream'}]}
            gateway_publish({'state': 'completed', 'message': elsewhere}, project=OTHER_PROJECT)

            expected = {
                bad_state['messageId']: (work_item_id, 'invalid'),
                client_role['messageId']: (work_item_id, 'wrong_role'),
                not_held['messageId']: (unknown_task, 'unknown_task'),
                elsewhere['messageId']: (work_item_id, 'wrong_project'),
            }
            for message_id, (task_id, reason) in expected.items():
                view = wait_reading(task_id, message_id, verdict='refused')
                assert view['reason'] == reason, message_id
    finally:
        other_core.stop()

    # The wrong_project refusal went to the command stream of the project
    # whose gateway stream carried it.
    [wrong_project_row] = verdict_rows(elsewhere['messageId'])
    assert wrong_project_row.project == OTHER_PROJECT

    assert record(work_item_id)['state'] == 'working', 'no refused message sets the current state'
    assert message_view(unknown_task, not_held['messageId'])['outcome'] is None, 'unknown_task has no outcome'
    assert stored(not_held['messageId'])


def test_req04_an_invalid_submission_with_no_message_id_gets_no_verdict(a2a_db, redis_client, core):
    work_item_id = new_work_item()
    core.start_command()
    with scrummaster() as sm:
        sm.dispatch(work_item_id)
        sm.start_gateway()
        before = redis_client.xlen(command_stream_name(PROJECT))
        no_id = agent_message(work_item_id, work_item_id)
        del no_id['messageId']
        xadd(redis_client, GATEWAY_STREAM, gateway_envelope(work_item_id, {'state': 'working', 'message': no_id},
                                                            context_id=work_item_id))

        def scrummaster_acknowledged():
            group = {g['name']: g for g in redis_client.xinfo_groups(GATEWAY_STREAM)}['scrummaster']
            return group['pending'] == 0 and (group.get('lag') or 0) == 0
        wait_for(scrummaster_acknowledged, expected='the gateway to drop the invalid submission')

    commands = [json.loads(f['data'])['payload'] for _id, f in redis_client.xrange(command_stream_name(PROJECT))]
    assert redis_client.xlen(command_stream_name(PROJECT)) == before
    assert not [c for c in commands if c.get('command') == 'recordA2AVerdict' and c.get('taskId') == work_item_id
                and c.get('verdict') == 'refused']


def test_req04_a_verdict_recorded_before_its_message_is_listed_then_joined(a2a_db, redis_client, core):
    work_item_id = new_work_item()
    core.start_archive().start_command()
    with scrummaster() as sm:
        sm.start_gateway()
        dispatched = sm.dispatch(work_item_id)
        wait_stored(dispatched['messageId'])
        core.stop_archive()
        working = message_id_of(submitted(redis_client, submit_tool(dispatched, ['progress', '--text', 'early']))[1])
        wait_verdict_rows(working, 1)

        rec = record(work_item_id)
        assert [v['messageId'] for v in rec['unarchivedVerdicts']] == [working]
        assert rec['unarchivedVerdicts'][0]['verdict'] == 'applied'
        assert working not in [m['messageId'] for m in rec['messages']]
        assert rec['state'] is None

        core.start_archive()
        wait_stored(working)
    rec = record(work_item_id)
    assert rec['unarchivedVerdicts'] == []
    assert message_view(work_item_id, working)['verdict'] == 'applied'
    assert rec['state'] == 'working'


def test_req04_a_redelivered_command_is_stored_once_with_one_outbox_row_in_its_transaction(a2a_db, redis_client, core):
    task_id = new_work_item()
    message_id = str(uuid.uuid4())
    core.start_command()
    envelope = publish_verdict(redis_client, {'taskId': task_id, 'messageId': message_id, 'verdict': 'applied'})
    # The same command envelope, XADDed again: one envelope messageId.
    redis_client.xadd(command_stream_name(PROJECT), {'data': json.dumps(envelope)})
    wait_for(lambda: redis_client.xinfo_groups(command_stream_name(PROJECT))[0]['pending'] == 0
             and (redis_client.xinfo_groups(command_stream_name(PROJECT))[0].get('lag') or 0) == 0,
             expected='both deliveries acknowledged')

    [row] = verdict_rows(message_id)
    assert row.command_message_id == envelope['messageId']
    [outbox] = outbox_rows('a2a.verdict_recorded', messageId=message_id)
    assert outbox.payload == {'taskId': task_id, 'messageId': message_id, 'verdict': 'applied'}
    with connection.cursor() as cursor:
        cursor.execute('SELECT xmin::text::bigint FROM outbox_event WHERE id = %s', [outbox.id])
        outbox_tx = cursor.fetchone()[0]
    assert outbox_tx == row.change_xid % 4294967296, 'the verdict row and its outbox row: one transaction'


def test_req04_a_command_whose_acknowledgement_failed_is_stored_once(a2a_db, redis_client, core):
    task_id = new_work_item()
    message_id = str(uuid.uuid4())
    core.start_command()
    real = core.command._client.xack
    failed = {'left': 1}

    def xack(*args, **kwargs):
        if failed['left']:
            failed['left'] -= 1
            raise ConnectionError('injected acknowledgement failure')
        return real(*args, **kwargs)

    core.command._client.xack = xack
    publish_verdict(redis_client, {'taskId': task_id, 'messageId': message_id, 'outcome': 'succeeded'})
    wait_for(lambda: failed['left'] == 0, expected='the acknowledgement to fail once')
    wait_for(lambda: redis_client.xpending(command_stream_name(PROJECT), 'core')['pending'] == 0, timeout_s=10,
             expected='the command reclaimed and acknowledged')
    assert len(verdict_rows(message_id)) == 1
    assert len(outbox_rows('a2a.verdict_recorded', messageId=message_id)) == 1


def test_req04_a_refusal_published_after_applied_does_not_replace_it(a2a_db, redis_client, core):
    task_id = new_work_item()
    message_id = str(uuid.uuid4())
    core.start_command()
    publish_verdict(redis_client, {'taskId': task_id, 'messageId': message_id, 'verdict': 'applied'},
                    created_at='2026-10-12T10:00:00+00:00')
    publish_verdict(redis_client, {'taskId': task_id, 'messageId': message_id, 'verdict': 'refused',
                                   'reason': 'unknown_task'}, created_at='2026-10-12T10:00:05+00:00')
    wait_verdict_rows(message_id, 2)
    [view] = record(task_id)['unarchivedVerdicts']
    assert view['verdict'] == 'applied' and view['outcome'] == 'pending'
    assert [(r['verdict'], r['reason']) for r in view['verdictRows']] == [('applied', None), ('refused', 'unknown_task')]


@pytest.mark.parametrize('fields', [
    {'verdict': 'accepted'},
    {},
    {'verdict': 'applied', 'outcome': 'succeeded'},
    {'verdict': 'refused'},
    {'verdict': 'refused', 'reason': 'because'},
    {'outcome': 'pending'},
], ids=['unknown verdict', 'neither', 'both', 'refused without a reason', 'unknown reason', 'unknown outcome'])
def test_req04_a_malformed_command_is_a_validation_error_dead_lettered_storing_nothing(
        a2a_db, redis_client, core, fields):
    from workitems import store
    task_id = new_work_item()
    message_id = str(uuid.uuid4())
    payload = {'command': 'recordA2AVerdict', 'taskId': task_id, 'messageId': message_id, **fields}
    with pytest.raises(store.ValidationError) as raised:
        store.record_a2a_verdict({'messageId': 'cmd', 'createdAt': '2026-10-12T00:00:00Z', 'project': PROJECT,
                                  'payload': payload})
    assert raised.value.code == 'VALIDATION_ERROR'

    core.start_command()
    publish_verdict(redis_client, {'taskId': task_id, 'messageId': message_id, **fields})
    wait_for(lambda: redis_client.xlen(dead_letter_stream_name(command_stream_name(PROJECT))) == 1,
             expected='the command dead-lettered at once')
    assert A2AVerdict.objects.count() == 0
    assert outbox_rows('a2a.verdict_recorded') == []
    from workitems.models import WorkItemComment
    assert not WorkItemComment.objects.exists(), 'no rejection comment: the command names no work item'


def test_req04_missing_task_or_message_id_is_a_validation_error(a2a_db):
    from workitems import store
    for payload in ({'messageId': 'm', 'verdict': 'applied'}, {'taskId': 't', 'verdict': 'applied'},
                    {'taskId': '', 'messageId': 'm', 'verdict': 'applied'}):
        with pytest.raises(store.ValidationError):
            store.record_a2a_verdict({'messageId': 'cmd', 'createdAt': '2026-10-12T00:00:00Z', 'project': PROJECT,
                                      'payload': {'command': 'recordA2AVerdict', **payload}})


def test_req04_pending_before_reclaim_failed_after_the_dead_letter_succeeded_after_its_replay(
        a2a_db, redis_client, core):
    work_item_id = new_work_item()
    core.start_archive().start_command()
    with scrummaster() as sm:
        sm.start_gateway(retryDelayMs=60_000)  # hold the gateway's retry off
        dispatched = sm.dispatch(work_item_id)
        sm.fail_publishes(1, which='projection')
        working = message_id_of(submitted(redis_client, submit_tool(
            dispatched, ['comment', '--text', 'third time lucky']))[1])
        view = wait_reading(work_item_id, working, verdict='applied')
        assert view['outcome'] == 'pending', 'applied, its projection not yet done'

        sm.stop_gateway()
        sm.fail_publishes(100, which='projection')
        sm.start_gateway(retryDelayMs=100, maxAttempts=3)
        wait_for(lambda: redis_client.xlen(dead_letter_stream_name(GATEWAY_STREAM)) == 1, timeout_s=10,
                 expected='ScrumMaster dead-letters the submission')
        view = wait_reading(work_item_id, working, verdict='applied', outcome='failed')

        sm.fail_publishes(0)
        dead_entry_id = redis_client.xrange(dead_letter_stream_name(GATEWAY_STREAM))[0][0]
        sm.replay(GATEWAY_STREAM, dead_entry_id)
        view = wait_reading(work_item_id, working, verdict='applied', outcome='succeeded')
    assert [r['outcome'] for r in view['verdictRows'] if r['outcome']] == ['failed', 'succeeded']


def test_req04_a_verdict_before_core_holds_the_task_announces_a_null_work_item(a2a_db, redis_client, core):
    work_item_id = new_work_item()
    core.start_command()  # core-a2a-archive stopped before the dispatch
    with scrummaster() as sm:
        sm.start_gateway()
        dispatched = sm.dispatch(work_item_id)
        working = message_id_of(submitted(redis_client, submit_tool(dispatched, ['progress', '--text', 'x']))[1])
        wait_verdict_rows(working, 2)  # `applied`, then the outcome `succeeded`
    rows = outbox_rows('a2a.verdict_recorded', messageId=working)
    assert rows[0].payload == {'taskId': work_item_id, 'messageId': working, 'verdict': 'applied'}
    assert [r.work_item_id for r in rows] == [None, None], 'no Task record, so no association to announce'


def test_req04_a_stored_verdict_row_cannot_be_updated_or_deleted(a2a_db, redis_client, core):
    message_id = str(uuid.uuid4())
    core.start_command()
    publish_verdict(redis_client, {'taskId': 't', 'messageId': message_id, 'verdict': 'applied'})
    wait_verdict_rows(message_id, 1)
    for sql in ("UPDATE a2a_verdict SET verdict = 'refused', reason = 'invalid' WHERE message_id = %s",
                'DELETE FROM a2a_verdict WHERE message_id = %s'):
        with pytest.raises(DatabaseError, match='a2a_verdict is append-only'):
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(sql, [message_id])
    assert verdict_rows(message_id)[0].verdict == 'applied'
