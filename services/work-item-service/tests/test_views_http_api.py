"""
Mirrors services/work-item-service/test/httpApi.test.js. Uses Django's test Client
— an in-process request through the real URL routing / view / store /
readstore stack and the real test Postgres database (no mocks), the
Django-idiomatic equivalent of the Node test's `fetch()` against a live
`http.Server`. Every assertion (status code, JSON shape, access_log/
outbox_event side effects) is preserved from the Node test file so the
HTTP contract services/scrummaster/src/canonicalWorkItems.js depends on is verified
identically.
"""

from __future__ import annotations

import json
import uuid

from django.test import Client

from workitems import project_config, store, views as views_module, write_gate
from workitems.models import AccessLog, OutboxEvent

PROJECT = 'test-project'


def test_get_work_item_404_then_200_with_full_record(clean_db):
    client = Client()
    missing = client.get(f'/work-items/{uuid.uuid4()}', {'full': 'true'})
    assert missing.status_code == 404

    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})
    found = client.get(f'/work-items/{item_id}', {'full': 'true'})
    assert found.status_code == 200
    body = found.json()
    assert body['id'] == str(item_id)
    assert isinstance(body['history'], list)

    rows = AccessLog.objects.filter(work_item_id=item_id)
    assert rows.count() >= 1, 'the HTTP read must be recorded in access_log'


def test_get_project_mode_reports_local_by_default(clean_db):
    client = Client()
    res = client.get(f'/projects/{PROJECT}/mode')
    body = res.json()
    assert body['mode'] == 'local'
    # No ProjectConfig row exists yet for this project — distinct from a row
    # explicitly set to local (see the next test), so a caller that needs to
    # tell the two apart (dispatchConsumer.js's issueLikeFor) can do so from
    # this one endpoint response.
    assert body['configured'] is False


def test_get_project_mode_reports_configured_true_for_an_explicit_row(clean_db):
    client = Client()
    project_config.set_mode(PROJECT, 'local')
    res = client.get(f'/projects/{PROJECT}/mode')
    body = res.json()
    assert body['mode'] == 'local'
    assert body['configured'] is True


def test_admin_transition_rejected_for_jira_mode_project(clean_db):
    client = Client()
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})
    project_config.set_mode(PROJECT, 'jira')

    res = client.post(f'/admin/work-items/{item_id}/transition', data=json.dumps({'status': 'in-progress'}),
                       content_type='application/json')
    assert res.status_code == 409
    body = res.json()
    assert body['error'] == 'WRITE_GATE_REJECTED'


def test_admin_create_endpoint_uses_external_api_origin(clean_db, monkeypatch):
    """Origin split: views.py's unauthenticated HTTP handler must
    reach store.py as Origins.EXTERNAL_API, never Origins.ADMIN_UI (reserved
    for admin.py's session-authenticated writes) — the write-gate/audit
    trail must not mistake an anonymous HTTP POST for an authenticated
    admin action."""
    captured = {}
    real_create = store.create_work_item

    def spy(payload, *, actor, origin):
        captured['origin'] = origin
        return real_create(payload, actor=actor, origin=origin)

    monkeypatch.setattr(views_module.store, 'create_work_item', spy)

    client = Client()
    item_id = uuid.uuid4()
    res = client.post('/admin/work-items', data=json.dumps({
        'id': str(item_id), 'project': PROJECT, 'type': 'task', 'displayName': 'X',
    }), content_type='application/json')
    assert res.status_code == 201
    assert captured.get('origin') == write_gate.Origins.EXTERNAL_API


def test_admin_created_item_visible_via_read_interface_and_produces_outbound_event(clean_db):
    client = Client()
    item_id = uuid.uuid4()
    res = client.post('/admin/work-items', data=json.dumps({
        'id': str(item_id), 'project': PROJECT, 'type': 'task', 'displayName': 'Admin created',
    }), content_type='application/json')
    assert res.status_code == 201

    read_back = client.get(f'/work-items/{item_id}')
    body = read_back.json()
    assert body['display_name'] == 'Admin created'

    rows = OutboxEvent.objects.filter(work_item_id=item_id)
    assert rows.count() == 1, 'an admin-UI write still produces an outbound event'


def _create_release(client, project=PROJECT):
    item_id = uuid.uuid4()
    res = client.post('/admin/work-items', data=json.dumps({
        'id': str(item_id), 'project': project, 'type': 'release', 'displayName': 'A release',
    }), content_type='application/json')
    assert res.status_code == 201
    return item_id


def test_admin_record_release_candidate_success(clean_db):
    client = Client()
    item_id = _create_release(client)
    client.post(f'/admin/work-items/{item_id}/transition', data=json.dumps({'status': 'in-review'}),
                content_type='application/json')

    res = client.post(f'/admin/work-items/{item_id}/release-candidate', data=json.dumps({
        'candidateSha': 'abc123', 'buildIdentifier': 'build-9', 'previewUrl': 'https://preview.example/abc123',
    }), content_type='application/json')
    assert res.status_code == 200

    full = client.get(f'/work-items/{item_id}', {'full': 'true'}).json()
    assert full['releaseDetail']['candidate_sha'] == 'abc123'
    assert full['releaseDetail']['build_identifier'] == 'build-9'
    assert full['releaseDetail']['preview_url'] == 'https://preview.example/abc123'
    assert any('abc123' in c['body'] for c in full['comments'])


def test_admin_record_release_candidate_missing_sha_rejected(clean_db):
    client = Client()
    item_id = _create_release(client)

    res = client.post(f'/admin/work-items/{item_id}/release-candidate', data=json.dumps({}),
                       content_type='application/json')
    assert res.status_code == 400
    assert res.json()['error'] == 'VALIDATION_ERROR'


def test_admin_record_release_candidate_unknown_item_rejected(clean_db):
    client = Client()
    res = client.post(f'/admin/work-items/{uuid.uuid4()}/release-candidate', data=json.dumps({
        'candidateSha': 'abc123',
    }), content_type='application/json')
    assert res.status_code == 400
    assert res.json()['error'] == 'VALIDATION_ERROR'


def test_get_work_item_full_includes_release_detail(clean_db):
    """releaseDetail must round-trip through the same full-record
    read path canonicalWorkItems.js's getWorkItem(..., {full: true}) uses,
    the same way storyDetail already does."""
    client = Client()
    item_id = _create_release(client)

    # No row at all until something is actually written to it — same as
    # storyDetail: store.create_work_item only creates the child row when
    # explicit detail data is passed in, and the internal-API create
    # path (admin_create_work_item) has no way to do that for a release
    # today (only release_notes could conceivably be set at creation, and
    # nothing currently plumbs it through) — candidate cut is the first
    # thing that ever writes here.
    full_before = client.get(f'/work-items/{item_id}', {'full': 'true'}).json()
    assert full_before['releaseDetail'] is None

    client.post(f'/admin/work-items/{item_id}/transition', data=json.dumps({'status': 'in-review'}),
                content_type='application/json')
    client.post(f'/admin/work-items/{item_id}/release-candidate', data=json.dumps({'candidateSha': 'def456'}),
                content_type='application/json')

    full_after = client.get(f'/work-items/{item_id}', {'full': 'true'}).json()
    assert full_after['releaseDetail']['candidate_sha'] == 'def456'
