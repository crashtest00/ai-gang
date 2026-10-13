"""
durable-a2a-message-record.md §4 — Core's archive group on the raw streams.

The Acceptance of §4 ("Where the write happens" … "Every durable write MUST
write its outbox row in the same transaction"), of "v5.2's gateway relay branch
is not A2A content", and of "V2.2's recorded duplicate dispatch is data, not
corruption". Every case drives the real `core-a2a-archive` consumers
(`a2a_archive.create_archive_consumers`, the function `run_consumers` starts
them with) against the real test Redis and Postgres; dispatches come from
ScrumMaster's own `dispatchTask` (tests/a2a_scrummaster_driver.js) and
submissions from the real submit tool, except where an input those refuse is
`XADD`ed as the Acceptance says.

"With ScrumMaster's gateway consumer stopped" holds throughout this module
unless a case starts it: no ScrumMaster group reads the gateway stream here,
so no other group can dead-letter an entry onto the shared `:dead` stream.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
from django.db import connection

from workitems import a2a_archive, registry, relay, store, streams
from workitems.envelope import Kind
from workitems.models import OutboxEvent
from workitems.stream_topology import command_stream_name, event_stream_name

from tests.a2a_support import (  # noqa: F401 - a2a_db and core are pytest fixtures, used by name
    BACKEND_STREAM, GATEWAY_STREAM, PROJECT, REFINEMENT_STREAM, SCRUMMASTER_DIR, A2AArtifact, A2AMessage,
    A2ATask, a2a_db, agent_message, core, gateway_envelope, last_entry, new_work_item, outbox_rows,
    pending_in_archive_group, scrummaster, stored, submit_tool, submitted, wait_archive_drained, wait_for,
    wait_stored, xadd,
)


def xmin_of(table: str, column: str, value) -> int:
    """The top-level id of the transaction that inserted a row.

    An A2A row carries it as ``change_xid`` (``pg_current_xact_id()``, which
    names the top-level transaction even from inside a savepoint). An outbox
    row is written outside any savepoint, so its ``xmin`` system column is
    that same top-level id, in its 32-bit form; both are reduced to it. Two
    rows with one value were written by one transaction."""
    expression = 'change_xid %% 4294967296' if table.startswith('a2a_') else 'xmin::text::bigint'
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT {expression} FROM {table} WHERE {column} = %s', [value])
        rows = cursor.fetchall()
    assert len(rows) == 1, f'expected one {table} row with {column}={value!r}, found {len(rows)}'
    return int(rows[0][0])


def outbox_xmin(event_type: str, **payload_match) -> int:
    rows = outbox_rows(event_type, **payload_match)
    assert len(rows) == 1, f'expected one {event_type} row matching {payload_match}, found {len(rows)}'
    return xmin_of('outbox_event', 'id', rows[0].id)


def dead_letters(redis_client, stream: str) -> int:
    return redis_client.xlen(streams.dead_letter_stream_name(stream))


# ---------------------------------------------------------------------------
# One archive consumer per distinct stream, on exactly the enumerated streams
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path, *, shared_suffix: bool) -> tuple[str, str]:
    agents = [
        {'id': 'alpha-agent', 'displayName': 'Alpha', 'definitionPath': '/a.md', 'routing': {'channelSuffix': 'alpha'}},
        {'id': 'beta-agent', 'displayName': 'Beta', 'definitionPath': '/b.md', 'routing': {'channelSuffix': 'beta'}},
        {'id': 'gamma-agent', 'displayName': 'Gamma', 'definitionPath': '/c.md', 'routing': {'channelSuffix': 'gamma'}},
    ]
    projects = [
        {'name': 'Proj-One', 'agents': ['alpha-agent', 'beta-agent']},
        {'name': 'proj-two', 'agents': ['gamma-agent']},
    ]
    if shared_suffix:
        agents.append({'id': 'alpha-twin', 'displayName': 'Alpha twin', 'definitionPath': '/d.md',
                       'routing': {'channelSuffix': 'alpha'}})
        projects[0]['agents'].append('alpha-twin')
    catalog, project_config = tmp_path / 'agents.json', tmp_path / 'projects.json'
    catalog.write_text(json.dumps({'agents': agents}))
    project_config.write_text(json.dumps({'projects': projects}))
    return str(catalog), str(project_config)


class _Stop(Exception):
    pass


def _run_consumers_once(monkeypatch) -> list[tuple[str, str]]:
    """Runs the real `run_consumers` command until its first idle sleep, then
    stops it, and returns the (stream, group) of every consumer it started.
    Every consumer shares one consumer name (`run_consumers`), so they are
    counted as they start rather than read back from XINFO CONSUMERS."""
    from django.core.management import call_command
    from workitems.management.commands import run_consumers

    started: list[tuple[str, str]] = []
    real_start = streams.Consumer.start

    def counting_start(self):
        started.append((self.stream, self.group))
        real_start(self)

    def stop_at_first_sleep(_seconds):
        raise _Stop()

    monkeypatch.setattr(streams.Consumer, 'start', counting_start)
    # Only the command's own reference to `time`: the consumers' threads keep
    # the real `time.sleep`.
    monkeypatch.setattr(run_consumers, 'time', type('StopTime', (), {'sleep': staticmethod(stop_at_first_sleep)}))
    monkeypatch.setattr(run_consumers.signal, 'signal', lambda *_args: None)
    with pytest.raises(_Stop):
        call_command('run_consumers', consumer_id='a2a-topology-test')
    return started


@pytest.fixture(autouse=True)
def _reload_registry_after():
    """Set up before, so torn down after, `settings` restores the catalog
    paths a case pointed elsewhere: the registry is reloaded from the
    restored ones."""
    yield
    registry.load(force=True)


@pytest.fixture
def configured(settings, tmp_path):
    def apply(shared_suffix: bool):
        settings.AGENTS_CATALOG_PATH, settings.PROJECTS_CONFIG_PATH = _write_config(tmp_path, shared_suffix=shared_suffix)
        registry.load(force=True)
    return apply


def test_core_a2a_archive_runs_on_each_gateway_and_agent_channel_stream_and_no_other(
        a2a_db, redis_client, configured, monkeypatch):
    configured(shared_suffix=False)
    started = _run_consumers_once(monkeypatch)

    expected = {
        'aigang:gateway:proj-one', 'aigang:gateway:proj-two',
        'aigang:agent:proj-one:alpha', 'aigang:agent:proj-one:beta', 'aigang:agent:proj-two:gamma',
    }
    archive_started = [stream for stream, group in started if group == a2a_archive.A2A_ARCHIVE_GROUP]
    assert sorted(archive_started) == sorted(expected)

    with_group = set()
    for key in redis_client.scan_iter(_type='stream'):
        names = {g['name'] for g in redis_client.xinfo_groups(key)}
        if a2a_archive.A2A_ARCHIVE_GROUP in names:
            with_group.add(key)
    assert with_group == expected, 'core-a2a-archive must exist on exactly the enumerated streams'
    # The other consumers run_consumers starts are unchanged, beside them.
    assert (command_stream_name('Proj-One'), 'core') in started


def test_agents_sharing_a_channel_suffix_get_one_archive_consumer_on_that_stream(
        a2a_db, redis_client, configured, monkeypatch):
    configured(shared_suffix=True)
    started = _run_consumers_once(monkeypatch)
    on_alpha = [s for s, g in started if g == a2a_archive.A2A_ARCHIVE_GROUP and s == 'aigang:agent:proj-one:alpha']
    assert on_alpha == ['aigang:agent:proj-one:alpha'], 'exactly one archive consumer on the shared stream'
    assert len([g for _s, g in started if g == a2a_archive.A2A_ARCHIVE_GROUP]) == 5


def test_core_agent_stream_name_is_scrummasters_verbatim():
    script = ("const r = require('./src/registry');"
              "process.stdout.write(JSON.stringify([r.agentStreamName(' Mixed-Case ', 'backend'),"
              " r.gatewayStreamName(' Mixed-Case ')]))")
    out = subprocess.run(['node', '-e', script], cwd=str(SCRUMMASTER_DIR), capture_output=True, text=True,
                         timeout=30, check=True).stdout
    assert json.loads(out) == [registry.agent_stream_name(' Mixed-Case ', 'backend'),
                               registry.gateway_stream_name(' Mixed-Case ')]


# ---------------------------------------------------------------------------
# What a dispatch and a submission store, in which transaction
# ---------------------------------------------------------------------------

def test_dispatch_and_submission_are_stored_each_in_one_transaction_with_their_outbox_rows(
        a2a_db, redis_client, core):
    work_item_id = new_work_item()
    with scrummaster() as sm:
        core.start_archive([BACKEND_STREAM])  # the dispatch is the Task's first-archived message
        dispatched = sm.dispatch(work_item_id)
        wait_stored(dispatched['messageId'])
        dispatch_entry_id, dispatch_envelope = last_entry(redis_client, BACKEND_STREAM)

        core.start_archive([GATEWAY_STREAM])
        submission_entry_id, submission_envelope = submitted(redis_client, submit_tool(
            dispatched, ['completed', '--text', 'done', '--pull-request', 'https://git.example/pr/7']))
        submission_id = submission_envelope['payload']['message']['messageId']
        wait_stored(submission_id)
    artifact_id = submission_envelope['payload']['artifacts'][0]['artifactId']

    dispatch_row = A2AMessage.objects.get(message_id=dispatched['messageId'])
    submission_row = A2AMessage.objects.get(message_id=submission_id)
    assert dispatch_row.envelope == dispatch_envelope and dispatch_row.entry_id == dispatch_entry_id
    assert submission_row.envelope == submission_envelope and submission_row.entry_id == submission_entry_id
    assert dispatch_row.direction == 'dispatch' and submission_row.direction == 'submission'

    task = A2ATask.objects.get(id=work_item_id)
    assert task.created_from_message_id == dispatched['messageId']

    # The Task record, the first-archived message and both their outbox rows:
    # one transaction.
    task_tx = xmin_of('a2a_task', 'id', work_item_id)
    assert xmin_of('a2a_message', 'message_id', dispatched['messageId']) == task_tx
    assert outbox_xmin('a2a.task_recorded', taskId=work_item_id) == task_tx
    assert outbox_xmin('a2a.message_recorded', messageId=dispatched['messageId']) == task_tx

    # The submission, its artifact and both their outbox rows: one
    # transaction of the submission's own.
    submission_tx = xmin_of('a2a_message', 'message_id', submission_id)
    assert submission_tx != task_tx
    assert xmin_of('a2a_artifact', 'artifact_id', artifact_id) == submission_tx
    assert outbox_xmin('a2a.message_recorded', messageId=submission_id) == submission_tx
    assert outbox_xmin('a2a.artifact_recorded', artifactId=artifact_id) == submission_tx

    # Each outbox row names no destination stream and stores no envelope
    # (the artifact history's REQ-02 columns), so the relay publishes it on
    # the project's own event stream, with the ids §4 lists.
    a2a_rows = OutboxEvent.objects.filter(event_type__startswith='a2a.')
    assert a2a_rows.count() == 4
    assert all(r.stream is None and r.envelope is None for r in a2a_rows)
    while relay.relay_once(redis_client):
        pass
    published = [json.loads(fields['data'])['payload'] for _id, fields in
                 redis_client.xrange(event_stream_name(PROJECT))]
    announced = {(p['eventType'], json.dumps(p['data'], sort_keys=True)) for p in published}
    assert ('a2a.task_recorded', json.dumps({'taskId': work_item_id})) in announced
    assert ('a2a.message_recorded', json.dumps({'messageId': dispatched['messageId'], 'taskId': work_item_id},
                                                sort_keys=True)) in announced
    assert ('a2a.message_recorded', json.dumps({'messageId': submission_id, 'taskId': work_item_id},
                                                sort_keys=True)) in announced
    assert ('a2a.artifact_recorded', json.dumps({'artifactId': artifact_id, 'messageId': submission_id,
                                                 'taskId': work_item_id}, sort_keys=True)) in announced
    assert all(p['workItemId'] == work_item_id for p in published if p['eventType'].startswith('a2a.'))


def test_a_failure_after_the_message_insert_rolls_everything_back_and_the_entry_is_retried(
        a2a_db, redis_client, core, monkeypatch):
    task_id = new_work_item()
    message = agent_message(task_id, task_id)
    failures = {'left': 1}
    real_checkpoint = store._a2a_write_checkpoint

    def fail_once(point, **detail):
        if point == 'message_inserted' and failures['left']:
            failures['left'] -= 1
            raise RuntimeError('injected failure after the message insert')
        real_checkpoint(point, **detail)

    monkeypatch.setattr(store, '_a2a_write_checkpoint', fail_once)
    core.timing['retry_delay_ms'] = 60_000  # hold the reclaim off while the rollback is checked
    core.start_archive([GATEWAY_STREAM])
    entry_id = xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': message},
                                                                   context_id=task_id))
    wait_for(lambda: failures['left'] == 0, expected='the injected failure to fire')
    wait_for(lambda: pending_in_archive_group(redis_client, GATEWAY_STREAM) == 1,
             expected='the failed entry left pending in core-a2a-archive')
    assert not stored(message['messageId'])
    assert not A2ATask.objects.filter(id=task_id).exists()
    assert OutboxEvent.objects.filter(event_type__startswith='a2a.').count() == 0

    core.stop_archive()
    core.timing['retry_delay_ms'] = 100
    core.start_archive([GATEWAY_STREAM])
    wait_stored(message['messageId'])
    wait_archive_drained(redis_client, GATEWAY_STREAM)
    assert A2AMessage.objects.filter(message_id=message['messageId']).count() == 1
    assert A2AMessage.objects.get(message_id=message['messageId']).entry_id == entry_id
    assert len(outbox_rows('a2a.message_recorded', messageId=message['messageId'])) == 1
    assert dead_letters(redis_client, GATEWAY_STREAM) == 0


@pytest.mark.parametrize('failure', ['transient', 'PermanentError', 'permanent attribute'])
def test_the_archive_group_never_dead_letters_a_failing_write(a2a_db, redis_client, core, monkeypatch, failure):
    task_id = new_work_item()
    message = agent_message(task_id, task_id)

    def always_fail(point, **_detail):
        if point != 'message_inserted':
            return
        if failure == 'PermanentError':
            raise streams.PermanentError('injected permanent failure')
        err = RuntimeError('injected failure')
        if failure == 'permanent attribute':
            err.permanent = True
        raise err

    monkeypatch.setattr(store, '_a2a_write_checkpoint', always_fail)
    core.start_archive([GATEWAY_STREAM])
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {'state': 'working', 'message': message},
                                                        context_id=task_id))
    attempts_key = streams.attempts_key(GATEWAY_STREAM, a2a_archive.A2A_ARCHIVE_GROUP)

    def attempts():
        return sum(int(v) for v in redis_client.hvals(attempts_key))

    wait_for(lambda: attempts() > streams.DEFAULT_MAX_ATTEMPTS + 1, timeout_s=10,
             expected=f'more than {streams.DEFAULT_MAX_ATTEMPTS} failed attempts', observed=attempts)
    assert pending_in_archive_group(redis_client, GATEWAY_STREAM) == 1, 'the entry stays pending'
    assert dead_letters(redis_client, GATEWAY_STREAM) == 0, 'nothing is written to the :dead stream'
    assert not stored(message['messageId'])


def test_an_entry_that_is_not_a_valid_envelope_is_acknowledged_stores_nothing_and_is_not_dead_lettered(
        a2a_db, redis_client, core):
    core.start_archive([GATEWAY_STREAM])
    redis_client.xadd(GATEWAY_STREAM, {'data': 'this is not JSON'})
    redis_client.xadd(GATEWAY_STREAM, {'data': json.dumps({'schemaVersion': '1', 'kind': 'gateway_operation'})})
    redis_client.xadd(GATEWAY_STREAM, {'other': 'no data field at all'})
    wait_archive_drained(redis_client, GATEWAY_STREAM)
    assert A2AMessage.objects.count() == 0 and A2ATask.objects.count() == 0
    assert dead_letters(redis_client, GATEWAY_STREAM) == 0


def test_a_submission_whose_work_item_cannot_be_resolved_is_recorded_with_no_association(
        a2a_db, redis_client, core):
    """§4's Acceptance and REQ-09's unresolvable case: the Task record exists,
    with no association and the condition recorded, and its rows publish
    with a null workItemId."""
    task_id = str(uuid.uuid4())  # a canonical-looking id no work item has
    not_a_uuid = 'wi-not-canonical'
    core.start_archive([GATEWAY_STREAM])
    for tid in (task_id, not_a_uuid):
        message = agent_message(tid, tid)
        xadd(redis_client, GATEWAY_STREAM, gateway_envelope(tid, {'state': 'working', 'message': message},
                                                            context_id=tid))
        wait_stored(message['messageId'])
        task = A2ATask.objects.get(id=tid)
        assert task.work_item_id is None
        assert task.association_error and tid in task.association_error

    assert all(r.work_item_id is None for r in OutboxEvent.objects.filter(event_type__startswith='a2a.'))
    while relay.relay_once(redis_client):
        pass
    published = [json.loads(f['data'])['payload'] for _id, f in redis_client.xrange(event_stream_name(PROJECT))]
    assert published and all(p['workItemId'] is None for p in published)


def test_the_jira_writer_and_scrummasters_dispatch_consumer_take_no_action_on_an_a2a_event(
        a2a_db, redis_client, core, monkeypatch):
    from workitems import jira_writer, project_config

    work_item_id = new_work_item()
    project_config.set_mode(PROJECT, project_config.JIRA, jira_project_key='TP')
    core.start_archive([GATEWAY_STREAM])
    message = agent_message(work_item_id, work_item_id)
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(
        work_item_id, {'state': 'completed', 'message': message,
                       'artifacts': [{'kind': 'artifact', 'artifactId': 'art-1', 'name': 'n', 'parts': []}]},
        context_id=work_item_id))
    wait_stored(message['messageId'])
    core.stop_archive()
    store.record_a2a_verdict({'messageId': 'cmd-1', 'createdAt': '2026-10-12T00:00:00+00:00', 'project': PROJECT,
                              'payload': {'command': 'recordA2AVerdict', 'taskId': work_item_id,
                                          'messageId': message['messageId'], 'verdict': 'applied'}})
    while relay.relay_once(redis_client):
        pass
    envelopes = [json.loads(f['data']) for _id, f in redis_client.xrange(event_stream_name(PROJECT))
                 if json.loads(f['data'])['payload']['eventType'].startswith('a2a.')]
    assert {e['payload']['eventType'] for e in envelopes} == {
        'a2a.task_recorded', 'a2a.message_recorded', 'a2a.artifact_recorded', 'a2a.verdict_recorded'}

    def no_jira_call(*_args, **_kwargs):
        raise AssertionError('the Jira writer must make no call for an a2a. event')
    for name in ('push_comment', 'push_transition', 'push_assignment', 'push_link'):
        if hasattr(jira_writer, name):
            monkeypatch.setattr(jira_writer, name, no_jira_call)
    monkeypatch.setattr(jira_writer, 'jira_client', None)
    for envelope in envelopes:
        assert jira_writer.handle_event_envelope(envelope) is None

    script = r"""
      process.env.AGENTS_CATALOG_PATH = process.argv[1];
      process.env.PROJECTS_CONFIG_PATH = process.argv[2];
      const canonical = require('./src/canonicalWorkItems');
      const handlers = require('./src/handlers');
      const touched = [];
      canonical.getWorkItem = async () => { touched.push('getWorkItem'); return null; };
      canonical.getMode = async () => { touched.push('getMode'); return null; };
      canonical.publishCommand = async () => { touched.push('publishCommand'); };
      handlers.dispatchTask = async () => { touched.push('dispatchTask'); };
      const dc = require('./src/dispatchConsumer');
      (async () => {
        for (const envelope of JSON.parse(process.argv[3])) await dc.handleWorkItemEventEnvelope(envelope, 'test-project');
        process.stdout.write(JSON.stringify(touched));
      })().catch(e => { console.error(e); process.exit(1); });
    """
    import os
    out = subprocess.run(['node', '-e', script, os.environ['AGENTS_CATALOG_PATH'], os.environ['PROJECTS_CONFIG_PATH'],
                          json.dumps(envelopes)], cwd=str(SCRUMMASTER_DIR), capture_output=True, text=True,
                         timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [], 'the dispatch consumer must take no action on an a2a. event'


# ---------------------------------------------------------------------------
# v5.2's gateway relay branch is not A2A content
# ---------------------------------------------------------------------------

def test_the_four_non_a2a_gateway_branches_are_acknowledged_and_never_stored(a2a_db, redis_client, core):
    task_id = new_work_item()
    message = lambda: agent_message(task_id, task_id)  # noqa: E731 - a fresh message per entry
    entries = [
        gateway_envelope(task_id, {'type': 'beta_deployed', 'sha': 'abc', 'message': message()}),
        gateway_envelope(task_id, {'type': 'pipeline_retry', 'build_url': 'u', 'message': message()}),
        gateway_envelope(task_id, {'operation': 'materializeDecomposition', 'parentWorkItemId': task_id,
                                   'subtasks': [], 'message': message()}),
        gateway_envelope(task_id, {'status': 'completed', 'message': message()}, kind=Kind.TASK_STATUS,
                         context_id=task_id),
    ]
    core.start_archive([GATEWAY_STREAM])
    for envelope in entries:
        xadd(redis_client, GATEWAY_STREAM, envelope)
    wait_archive_drained(redis_client, GATEWAY_STREAM)
    assert A2AMessage.objects.count() == 0 and A2ATask.objects.count() == 0
    assert dead_letters(redis_client, GATEWAY_STREAM) == 0


# ---------------------------------------------------------------------------
# V2.2's recorded duplicate dispatch is data, not corruption
# ---------------------------------------------------------------------------

def test_a_submitted_then_working_dispatch_are_both_stored_and_a_repeated_dispatch_id_once(
        a2a_db, redis_client, core):
    first_item, second_item = new_work_item(), new_work_item()
    core.start_archive([BACKEND_STREAM, REFINEMENT_STREAM])
    with scrummaster() as sm:
        submitted_dispatch = sm.dispatch(first_item)
        working_dispatch = sm.dispatch(first_item)  # V2.2's recorded `submitted` then `working`
        sm.dispatch(second_item, dispatch_id='same-dispatch')
        sm.dispatch(second_item, dispatch_id='same-dispatch')
        never_transmitted = sm.task_state(second_item)['messageIds'][-1]
    wait_stored(submitted_dispatch['messageId'])
    wait_stored(working_dispatch['messageId'])
    wait_archive_drained(redis_client, BACKEND_STREAM)

    first = A2AMessage.objects.filter(task_id=first_item).order_by('entry_ms', 'entry_seq')
    assert [m.message_id for m in first] == [submitted_dispatch['messageId'], working_dispatch['messageId']]
    assert first[1].reference_message_id == submitted_dispatch['messageId']
    assert all(m.state is None for m in first), 'a dispatch carries no state on the stream'

    second = A2AMessage.objects.filter(task_id=second_item)
    assert second.count() == 1, 'the deduplicated continuation was never on a stream'
    assert not stored(never_transmitted)


def test_artifacts_without_an_id_stay_in_the_envelope_only(a2a_db, redis_client, core):
    task_id = new_work_item()
    message = agent_message(task_id, task_id)
    core.start_archive([GATEWAY_STREAM])
    xadd(redis_client, GATEWAY_STREAM, gateway_envelope(task_id, {
        'state': 'completed', 'message': message,
        'artifacts': [{'kind': 'artifact', 'name': 'no id', 'parts': []}, 'not even an object'],
    }, context_id=task_id))
    wait_stored(message['messageId'])
    assert A2AArtifact.objects.count() == 0
    assert len(A2AMessage.objects.get(message_id=message['messageId']).envelope['payload']['artifacts']) == 2
