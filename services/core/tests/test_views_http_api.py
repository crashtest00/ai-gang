"""
Mirrors the Node reference implementation's httpApi.test.js (not carried
into this repository). Uses Django's test Client
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
from workitems.models import AccessLog, OutboxEvent, WorkItemComment

from tests.jira_fixture import permissive_jira  # noqa: F401 - a pytest fixture, used by name
from tests.jira_fixture import posted_comment_texts

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


def test_admin_transition_for_a_jira_mode_project_posts_and_returns_202(clean_db, permissive_jira):
    """canonical-delivery-state.md REQ-09, "The external API is a machine
    caller" — this endpoint used to be refused with WRITE_GATE_REJECTED
    (409). From v5.2 `route` pushes it: the equivalent Jira write is made
    in-process (the view holds no transaction), the response is 202 with
    the posted result, and no canonical row changes until Jira's webhook
    returns."""
    client = Client()
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'X', 'externalKey': 'TP-800'})
    project_config.set_mode(PROJECT, 'jira')

    permissive_jira.routes[('GET', '/rest/api/3/issue/TP-800/transitions')] = \
        lambda query, body: (200, {'transitions': [{'id': '31', 'to': {'name': 'In Progress'}}]})
    permissive_jira.routes[('POST', '/rest/api/3/issue/TP-800/transitions')] = \
        lambda query, body: (200, None)

    res = client.post(f'/admin/work-items/{item_id}/transition', data=json.dumps({'status': 'in-progress'}),
                       content_type='application/json')
    assert res.status_code == 202, res.content
    assert res.json() == {'posted': True, 'workItemId': str(item_id), 'deferred': False}
    assert store.get_work_item(item_id).status == 'proposed'


def test_an_external_api_transition_whose_jira_post_fails_returns_an_error_and_records_nothing(
        clean_db, permissive_jira):
    """REQ-09's acceptance: "in Jira mode an external-API transition or
    comment whose Jira post fails returns an error to its caller and
    records nothing, its view holding no transaction." The fixture answers
    the transition POST with a 500."""
    client = Client()
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'X', 'externalKey': 'TP-801'})
    project_config.set_mode(PROJECT, 'jira')

    permissive_jira.routes[('GET', '/rest/api/3/issue/TP-801/transitions')] = \
        lambda query, body: (200, {'transitions': [{'id': '31', 'to': {'name': 'In Progress'}}]})
    permissive_jira.routes[('POST', '/rest/api/3/issue/TP-801/transitions')] = \
        lambda query, body: (500, {'errorMessages': ['Jira is down']})

    res = client.post(f'/admin/work-items/{item_id}/transition', data=json.dumps({'status': 'in-progress'}),
                       content_type='application/json')
    assert res.status_code >= 400
    assert store.get_work_item(item_id).status == 'proposed'


def test_admin_add_comment_for_a_jira_mode_project_posts_and_returns_202(clean_db, permissive_jira):
    """The one comment path from the external API (REQ-09): the comment is
    posted with its `[<author>] ` prefix, 202 is returned with no row, and
    the comment reaches `core` on its own `comment_created` webhook."""
    client = Client()
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'X', 'externalKey': 'TP-802'})
    project_config.set_mode(PROJECT, 'jira')

    res = client.post(f'/admin/work-items/{item_id}/comments', data=json.dumps({'body': 'Deployed.'}),
                       content_type='application/json', HTTP_X_ACTOR='jenkins')
    assert res.status_code == 202, res.content
    assert res.json()['id'] is None
    assert posted_comment_texts() == ['[jenkins] Deployed.']
    assert WorkItemComment.objects.filter(work_item_id=item_id).count() == 0


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


def test_admin_record_release_candidate_publishes_the_canonical_event_once_in_local_mode(clean_db):
    """REQ-06 acceptance: `work_item.release_candidate_recorded` is
    published in both modes. This is the local-mode half, through the real
    endpoint (the Jira-mode half is in test_jira_writer.py)."""
    client = Client()
    item_id = _create_release(client)

    res = client.post(f'/admin/work-items/{item_id}/release-candidate', data=json.dumps({
        'candidateSha': 'abc123', 'buildIdentifier': 'build-9', 'previewUrl': 'https://preview.example/abc123',
    }), content_type='application/json')
    assert res.status_code == 200

    rows = OutboxEvent.objects.filter(event_type='work_item.release_candidate_recorded', work_item_id=item_id)
    assert rows.count() == 1
    row = rows.get()
    assert row.project == PROJECT
    assert row.payload == {'id': str(item_id), 'candidateSha': 'abc123', 'buildIdentifier': 'build-9',
                           'previewUrl': 'https://preview.example/abc123'}


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
