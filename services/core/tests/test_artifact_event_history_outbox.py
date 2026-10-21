"""
Durable Artifact Event History — the outbox row, the relay, the upload
path and the history (durable-artifact-event-history.md REQ-01, REQ-02,
REQ-05, REQ-06, REQ-07, and §4 "Deployment"'s no-back-fill rule).

Everything runs against the real test Postgres and Redis. Uploads go
through the real admin views; rows are relayed by the real
``workitems.relay.relay_once``. The librarian's half (REQ-04) is in
``test_artifact_event_history_librarian.py``, the wire comparison (REQ-03)
in ``test_artifact_event_history_wire.py`` and the relay-restart scenario
(REQ-05's first Acceptance) in ``test_artifact_event_history_relay_restart.py``.
"""

from __future__ import annotations

import inspect
import json
import uuid

import pytest
import redis
from django.contrib import admin as django_admin
from django.db import IntegrityError, connection, transaction
from django.test import RequestFactory

from artifacts import admin as artifacts_admin
from artifacts.events import ACTION_CREATED, ACTION_REPLACED, ARTIFACT_EVENT_STREAM, ARTIFACT_UPLOADED
from artifacts.models import Artifact
from librarian.stream_topology import RESPONSE_STREAM
from workitems import store
from workitems.admin import OutboxEventAdmin
from workitems.envelope import EnvelopeError, Kind, build_envelope
from workitems.models import OutboxEvent
from workitems.relay import relay_once
from workitems.stream_topology import event_stream_name

from tests import artifact_event_history_pre_change as pre_change
from tests.artifacts_support import (  # noqa: F401 - committed_artifact_env is a fixture
    MARKDOWN_BYTES, admin_client, change_url, committed_artifact_env, drain_outbox, post_upload, read_events, upload,
)
from tests.test_artifacts_events import _redis_that_is_down  # noqa: F401 - helper, not a test
from tests.librarian_support import (  # noqa: F401 - librarian_env is a fixture
    librarian_env, make_repo, request_delivery, seed_artifact,
)

PROJECT = 'test-project'
MOCKUP = b'\x89PNG\r\n\x1a\n-pretend-this-is-a-mockup-'


def _announcement_rows(stream=ARTIFACT_EVENT_STREAM):
    return list(OutboxEvent.objects.filter(stream=stream).order_by('created_at'))


def _entries(client, stream):
    return [(entry_id, fields['data']) for entry_id, fields in client.xrange(stream)]


# --- REQ-01: one row per upload, in the save's transaction ----------------

def test_req01_a_replacement_adds_one_row_and_one_replaced_entry(committed_artifact_env):
    client = admin_client()
    artifact = upload(client, content=MARKDOWN_BYTES, filename='prd.md')
    assert len(_announcement_rows()) == 1
    assert len(read_events(committed_artifact_env)) == 1

    assert post_upload(client, content=b'# v2\n', filename='prd.md', url=change_url(artifact.id)).status_code == 302

    rows = _announcement_rows()
    assert len(rows) == 2
    assert rows[1].published_at is None
    assert rows[1].envelope['payload']['action'] == ACTION_REPLACED
    events = read_events(committed_artifact_env)
    assert [e['payload']['action'] for e in events] == [ACTION_CREATED, ACTION_REPLACED]
    assert OutboxEvent.objects.get(id=rows[1].id).published_at is not None


def test_req01_an_edit_of_only_the_original_filename_writes_no_row(committed_artifact_env):
    client = admin_client()
    artifact = upload(client, content=MARKDOWN_BYTES, filename='prd.md')

    assert client.post(change_url(artifact.id), data={'original_filename': 'renamed.md'}).status_code == 302

    assert Artifact.objects.get(id=artifact.id).original_filename == 'renamed.md'
    assert len(_announcement_rows()) == 1


def _redis_stopped(monkeypatch):
    from workitems import redis_client as redis_client_module
    monkeypatch.setattr(redis_client_module, 'get_client', _redis_that_is_down)
    monkeypatch.setattr(redis_client_module, 'new_client', _redis_that_is_down)


def test_req01_a_replacement_saved_while_redis_is_down_is_announced_once_redis_is_back(
        committed_artifact_env, monkeypatch):
    client = admin_client()
    artifact = upload(client, content=MARKDOWN_BYTES, filename='prd.md')
    assert [e['payload']['action'] for e in read_events(committed_artifact_env)] == [ACTION_CREATED]

    with monkeypatch.context() as down:
        _redis_stopped(down)
        response = post_upload(client, content=b'# v2\n', filename='prd.md', url=change_url(artifact.id))
        assert response.status_code == 302
        rows = _announcement_rows()
        assert len(rows) == 2
        assert rows[1].published_at is None
        assert rows[1].envelope['payload']['action'] == ACTION_REPLACED
        with pytest.raises(redis.exceptions.ConnectionError):
            relay_once(_redis_that_is_down())
        assert OutboxEvent.objects.get(id=rows[1].id).published_at is None

    events = read_events(committed_artifact_env)
    assert [e['payload']['action'] for e in events] == [ACTION_CREATED, ACTION_REPLACED]
    assert OutboxEvent.objects.get(id=rows[1].id).published_at is not None


def test_req01_a_filename_only_edit_saved_while_redis_is_down_succeeds_and_writes_no_row(
        committed_artifact_env, monkeypatch):
    client = admin_client()
    artifact = upload(client, content=MARKDOWN_BYTES, filename='prd.md')
    assert len(read_events(committed_artifact_env)) == 1

    with monkeypatch.context() as down:
        _redis_stopped(down)
        response = client.post(change_url(artifact.id), data={'original_filename': 'renamed.md'})
        assert response.status_code == 302
        assert Artifact.objects.get(id=artifact.id).original_filename == 'renamed.md'
        assert len(_announcement_rows()) == 1

    assert len(read_events(committed_artifact_env)) == 1
    assert len(_announcement_rows()) == 1


def test_req01_a_save_rolled_back_after_save_model_leaves_no_row(committed_artifact_env, monkeypatch):
    """The admin's own ``changeform_view`` transaction is forced to roll
    back after ``save_model`` has returned — by failing the step the view
    takes next, ``log_addition`` — so the artifact row and its outbox row
    must disappear together."""
    calls = []
    real_save_model = artifacts_admin.ArtifactAdmin.save_model

    def save_model_then_note(self, request, obj, form, change):
        real_save_model(self, request, obj, form, change)
        calls.append(OutboxEvent.objects.filter(stream=ARTIFACT_EVENT_STREAM).count())

    def fail_after_save(self, request, obj, message):
        raise RuntimeError('forced rollback after save_model returned')

    monkeypatch.setattr(artifacts_admin.ArtifactAdmin, 'save_model', save_model_then_note)
    monkeypatch.setattr(artifacts_admin.ArtifactAdmin, 'log_addition', fail_after_save)

    client = admin_client()
    client.raise_request_exception = False
    response = post_upload(client, content=MARKDOWN_BYTES, filename='prd.md')

    assert response.status_code == 500
    assert calls == [1], 'save_model returned with its outbox row written inside the transaction'
    assert not Artifact.objects.exists()
    assert not OutboxEvent.objects.filter(stream=ARTIFACT_EVENT_STREAM).exists()
    assert read_events(committed_artifact_env) == []


def test_req01_the_post_commit_publish_and_its_failure_handler_are_gone():
    source = inspect.getsource(artifacts_admin)
    assert 'on_commit' not in source
    assert not hasattr(artifacts_admin, '_announce_upload')
    assert '_announce_upload' not in source


# --- REQ-02: a row names its stream; a row naming none is unchanged --------

def _upload_envelope():
    return build_envelope(Kind.ARTIFACT_EVENT, '_instance', payload={
        'eventType': ARTIFACT_UPLOADED, 'artifactId': str(uuid.uuid4()), 'path': 'ab/abcd', 'action': 'created',
        'actor': 'tester', 'uploadedAt': '2026-10-12T00:00:00+00:00',
    })


def test_req02_each_row_goes_to_its_own_stream_and_a_legacy_row_is_byte_identical(clean_db, redis_client):
    envelope = _upload_envelope()
    work_item_id = uuid.uuid4()
    with transaction.atomic():
        named = store.write_outbox_event(stream=ARTIFACT_EVENT_STREAM, envelope=envelope)
    with transaction.atomic():
        legacy = store.write_outbox_event(project=PROJECT, event_type='work_item.created',
                                          work_item_id=work_item_id, payload={'hello': 'world', 'n': 1})

    assert relay_once(redis_client) == 2

    artifact_entries = _entries(redis_client, ARTIFACT_EVENT_STREAM)
    assert len(artifact_entries) == 1
    assert json.loads(artifact_entries[0][1]) == envelope

    work_item_entries = _entries(redis_client, event_stream_name(PROJECT))
    assert len(work_item_entries) == 1
    legacy_row = OutboxEvent.objects.get(id=legacy)
    pre_change_stream, pre_change_envelope = pre_change.relay_envelope(legacy_row)
    assert pre_change_stream == event_stream_name(PROJECT)
    assert work_item_entries[0][1] == json.dumps(pre_change_envelope), 'byte-identical to the pre-change relay'

    # In that order: the named row was written first and published first.
    assert OutboxEvent.objects.get(id=named).published_at <= legacy_row.published_at
    assert OutboxEvent.objects.get(id=named).stream_entry_id == artifact_entries[0][0]


def test_req02_a_malformed_envelope_is_refused_at_write_time(clean_db):
    malformed = {**_upload_envelope(), 'kind': 'not-a-kind'}
    with pytest.raises(EnvelopeError):
        with transaction.atomic():
            store.write_outbox_event(stream=ARTIFACT_EVENT_STREAM, envelope=malformed)
    missing_message_id = {k: v for k, v in _upload_envelope().items() if k != 'messageId'}
    with pytest.raises(EnvelopeError):
        with transaction.atomic():
            store.write_outbox_event(stream=ARTIFACT_EVENT_STREAM, envelope=missing_message_id)
    assert not OutboxEvent.objects.exists()


def test_req02_the_database_refuses_a_stream_without_an_envelope_and_the_reverse(clean_db):
    """``outbox_stream_iff_envelope`` is a database check constraint, so it
    is exercised here with the ORM's own insert, below
    ``write_outbox_event``, as well as through it."""
    with pytest.raises(IntegrityError, match='outbox_stream_iff_envelope'):
        with transaction.atomic():
            OutboxEvent.objects.create(project=PROJECT, event_type='x', payload={}, stream=ARTIFACT_EVENT_STREAM)
    with pytest.raises(IntegrityError, match='outbox_stream_iff_envelope'):
        with transaction.atomic():
            OutboxEvent.objects.create(project='_instance', event_type='x', payload={}, envelope=_upload_envelope())
    with pytest.raises(IntegrityError, match='outbox_stream_iff_envelope'):
        with transaction.atomic():
            store.write_outbox_event(project=PROJECT, event_type='x', payload={}, stream=ARTIFACT_EVENT_STREAM)
    with pytest.raises(IntegrityError, match='outbox_stream_iff_envelope'):
        with transaction.atomic():
            store.write_outbox_event(envelope=_upload_envelope())
    assert not OutboxEvent.objects.exists()


def test_req02_an_announcement_row_takes_project_event_type_and_payload_from_its_envelope(clean_db):
    envelope = _upload_envelope()
    with transaction.atomic():
        row_id = store.write_outbox_event(stream=ARTIFACT_EVENT_STREAM, envelope=envelope)
    with pytest.raises(TypeError):
        store.write_outbox_event(project=PROJECT, stream=ARTIFACT_EVENT_STREAM, envelope=_upload_envelope())

    row = OutboxEvent.objects.get(id=row_id)
    assert (row.project, row.event_type, row.payload, row.work_item_id) == (
        '_instance', ARTIFACT_UPLOADED, envelope['payload'], None,
    )


# --- REQ-05: the per-row dedupe key, and the inherited window --------------

def _artifact_row():
    with transaction.atomic():
        return store.write_outbox_event(stream=ARTIFACT_EVENT_STREAM, envelope=_upload_envelope())


def _work_item_row():
    with transaction.atomic():
        return store.write_outbox_event(project=PROJECT, event_type='work_item.status_changed',
                                        work_item_id=uuid.uuid4(), payload={'to': 'ready'})


def test_req05_every_row_claims_its_own_outbox_dedupe_key(clean_db, redis_client):
    artifact_row, work_item_row = _artifact_row(), _work_item_row()

    relay_once(redis_client)

    artifact_envelope = OutboxEvent.objects.get(id=artifact_row).envelope
    assert redis_client.get(f'aigang:idem:publish:outbox:{artifact_row}') == artifact_envelope['messageId']
    assert redis_client.get(f'aigang:idem:publish:outbox:{work_item_row}') == work_item_row
    assert 0 < redis_client.ttl(f'aigang:idem:publish:outbox:{artifact_row}') <= 7 * 24 * 60 * 60


@pytest.mark.parametrize('make_row, stream_of', [
    (_artifact_row, lambda row: ARTIFACT_EVENT_STREAM),
    (_work_item_row, lambda row: event_stream_name(PROJECT)),
], ids=['artifact-row', 'work-item-row'])
def test_req05_the_inherited_claim_before_xadd_window_is_unchanged(clean_db, redis_client, make_row, stream_of):
    """A dedupe key claimed with no entry written — what a Redis failure
    between ``SET NX`` and ``XADD`` leaves — marks the row published with
    no entry and ``stream_entry_id`` NULL, for an announcement row exactly
    as for a work-item row: inherited, not widened."""
    row_id = make_row()
    redis_client.set(f'aigang:idem:publish:outbox:{row_id}', 'claimed-before-the-xadd')

    assert relay_once(redis_client) == 1

    row = OutboxEvent.objects.get(id=row_id)
    assert row.published_at is not None
    assert row.stream_entry_id is None
    assert redis_client.xlen(stream_of(row)) == 0


# --- REQ-06: the history outlives the stream, and cannot be deleted --------

def test_req06_both_announcements_are_recoverable_from_postgres_after_a_trim(librarian_env):
    artifact = seed_artifact(MOCKUP)
    make_repo(librarian_env, 'hello-web')
    response = request_delivery(librarian_env, artifact_id=artifact.id, destination_repo='hello-web',
                                requested_path='designs/mockup.png')
    assert response['payload']['status'] == 'delivered'
    drain_outbox(librarian_env.redis)

    redis = librarian_env.redis
    last = max(redis.xinfo_stream(ARTIFACT_EVENT_STREAM)['last-generated-id'],
               redis.xinfo_stream(RESPONSE_STREAM)['last-generated-id'],
               key=lambda i: tuple(int(p) for p in i.split('-')))
    later = f'{int(last.split("-")[0]) + 1}-0'
    redis.xtrim(ARTIFACT_EVENT_STREAM, minid=later)
    redis.xtrim(RESPONSE_STREAM, minid=later)
    assert redis.xlen(ARTIFACT_EVENT_STREAM) == 0 and redis.xlen(RESPONSE_STREAM) == 0

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT stream, project, event_type, envelope->'payload'->>'artifactId', created_at, published_at "
            "FROM outbox_event WHERE stream IN (%s, %s) ORDER BY created_at",
            [ARTIFACT_EVENT_STREAM, RESPONSE_STREAM],
        )
        rows = cursor.fetchall()

    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [
        (ARTIFACT_EVENT_STREAM, '_instance', ARTIFACT_UPLOADED, str(artifact.id)),
        (RESPONSE_STREAM, '_instance', 'artifact_delivery_response', str(artifact.id)),
    ]
    for row in rows:
        assert row[4] is not None and row[5] is not None and row[5] >= row[4]


def test_req06_a_failure_answer_row_has_the_instance_project_and_the_response_kind(librarian_env):
    response = request_delivery(librarian_env, artifact_id=uuid.uuid4(), destination_repo='hello-web',
                                requested_path='a.png')
    assert response['payload']['status'] == 'failed'
    row = OutboxEvent.objects.get(stream=RESPONSE_STREAM)
    assert (row.project, row.event_type) == ('_instance', 'artifact_delivery_response')


def test_req06_the_admin_refuses_to_delete_an_outbox_row(clean_db):
    with transaction.atomic():
        row_id = store.write_outbox_event(stream=ARTIFACT_EVENT_STREAM, envelope=_upload_envelope())
    client = admin_client('outbox-admin')
    from django.contrib.auth import get_user_model
    superuser = get_user_model().objects.get(username='outbox-admin')
    assert superuser.is_superuser

    model_admin = django_admin.site._registry[OutboxEvent]
    request = RequestFactory().get('/')
    request.user = superuser
    assert model_admin.has_delete_permission(request) is False
    assert model_admin.has_delete_permission(request, OutboxEvent.objects.get(id=row_id)) is False

    delete_url = f'/django-admin/workitems/outboxevent/{row_id}/delete/'
    assert client.get(delete_url).status_code == 403
    assert client.post(delete_url, {'post': 'yes'}).status_code == 403
    changelist = '/django-admin/workitems/outboxevent/'
    client.post(changelist, {'action': 'delete_selected', '_selected_action': [row_id], 'post': 'yes'})
    assert OutboxEvent.objects.filter(id=row_id).exists()


def test_req06_the_admin_list_shows_and_filters_by_destination_stream(clean_db):
    with transaction.atomic():
        announcement = store.write_outbox_event(stream=ARTIFACT_EVENT_STREAM, envelope=_upload_envelope())
    with transaction.atomic():
        work_item = store.write_outbox_event(project=PROJECT, event_type='work_item.created',
                                             work_item_id=uuid.uuid4(), payload={})
    assert 'stream' in OutboxEventAdmin.list_display
    assert 'stream' in OutboxEventAdmin.list_filter

    client = admin_client('outbox-viewer')
    changelist = '/django-admin/workitems/outboxevent/'
    shown = client.get(changelist)
    assert shown.status_code == 200
    assert ARTIFACT_EVENT_STREAM in shown.content.decode()

    filtered = client.get(changelist, {'stream': ARTIFACT_EVENT_STREAM})
    assert filtered.status_code == 200
    assert [str(r.id) for r in filtered.context['cl'].result_list] == [announcement]
    unnamed = client.get(changelist, {'stream__isnull': 'True'})
    assert [str(r.id) for r in unnamed.context['cl'].result_list] == [work_item]


# --- REQ-07: the work-item path is unchanged -------------------------------

def test_req07_a_status_transition_is_published_byte_identical_to_the_pre_change_relay(clean_db, redis_client):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Do it'},
                           actor='tester')
    store.transition_status(item_id, 'in-progress', actor='tester')
    # An announcement written between the two work-item transitions rides
    # the same pass and does not disturb them.
    _artifact_row()
    store.transition_status(item_id, 'done', actor='tester')

    drain_outbox(redis_client)

    rows = list(OutboxEvent.objects.filter(project=PROJECT, stream__isnull=True).order_by('created_at'))
    assert rows[0].event_type == 'work_item.created'
    assert sum(r.event_type == 'work_item.status_changed' for r in rows) == 2
    published = [data for _id, data in _entries(redis_client, event_stream_name(PROJECT))]
    expected = [json.dumps(pre_change.relay_envelope(row)[1]) for row in rows]
    assert published == expected, 'same content, same relative order, byte for byte'


# --- §4 "Deployment": no back-fill -----------------------------------------

def test_migrating_a_database_with_pre_feature_artifacts_creates_no_announcement(clean_db, redis_client):
    from django.db.migrations.executor import MigrationExecutor

    before = [('workitems', '0007_v52_jira_decomposition_record')]
    executor = MigrationExecutor(connection)
    # Restore every migration above 0007 (other apps' too), not just 0008.
    leaves = executor.loader.graph.leaf_nodes()
    executor.migrate(before)
    try:
        artifact_id = uuid.uuid4()
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO artifact (id, path, original_filename, uploaded_by, created_at, updated_at) "
                "VALUES (%s, %s, 'old.md', 'pre-feature', now(), now())",
                [artifact_id, f'{str(artifact_id)[:2]}/{artifact_id}'],
            )
            cursor.execute('SELECT count(*) FROM outbox_event')
            assert cursor.fetchone()[0] == 0
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(leaves)

    assert Artifact.objects.filter(id=artifact_id).exists()
    assert not OutboxEvent.objects.exists()
    assert relay_once(redis_client) == 0
    assert redis_client.xlen(ARTIFACT_EVENT_STREAM) == 0
