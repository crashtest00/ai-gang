"""
work-items.md REQ-01/REQ-02/REQ-05/REQ-06 — the read side, through the
REAL HTTP interface (Django's test Client against the real URL routing /
view / readstore stack and the real test Postgres database), mirroring
test_views_http_api.py's own approach.
"""

from __future__ import annotations

import uuid

from django.test import Client

from workitems import store
from workitems.models import AccessLog

from tests.test_work_item_references_store import make_artifact, make_work_item

PROJECT = 'test-project'


def test_bare_get_work_item_includes_both_references_req01_req02(clean_db):
    artifact = make_artifact()
    other_artifact = make_artifact()
    item = make_work_item(project=PROJECT)
    store.record_specification_link(item.id, artifact.id, 'REQ-18')
    store.add_artifact_link(item.id, other_artifact.id)

    client = Client()
    body = client.get(f'/work-items/{item.id}').json()

    assert body['specification_link'] == {
        'work_item_id': str(item.id), 'artifact_id': str(artifact.id), 'requirement_id': 'REQ-18',
    }
    assert len(body['artifact_links']) == 1
    assert body['artifact_links'][0]['artifact_id'] == str(other_artifact.id)
    assert body['artifact_links'][0]['position'] == 0


def test_bare_get_work_item_with_no_references_reports_null_and_empty_list(clean_db):
    """Compatibility (PRD §10) — a work item that never recorded either
    reference must not error, and must not fabricate a value."""
    item = make_work_item(project=PROJECT)
    client = Client()
    body = client.get(f'/work-items/{item.id}').json()
    assert body['specification_link'] is None
    assert body['artifact_links'] == []


def test_full_get_work_item_includes_both_references_req01_req02(clean_db):
    artifact = make_artifact()
    item = make_work_item(project=PROJECT)
    store.record_specification_link(item.id, artifact.id, 'REQ-18')

    client = Client()
    body = client.get(f'/work-items/{item.id}', {'full': 'true'}).json()
    assert body['specification_link']['artifact_id'] == str(artifact.id)
    assert isinstance(body['artifact_links'], list)


def test_ac03_specification_link_survives_terminal_status_via_real_http_read(clean_db):
    """AC-03 end to end: record it, drive the work item to a terminal
    status through REAL transitions, read it back through the REAL HTTP
    interface."""
    artifact = make_artifact()
    item = make_work_item(project=PROJECT)
    store.record_specification_link(item.id, artifact.id, 'REQ-18')

    store.transition_status(item.id, 'in-progress')
    store.transition_status(item.id, 'in-review')
    store.transition_status(item.id, 'done')

    client = Client()
    body = client.get(f'/work-items/{item.id}').json()
    assert body['status'] == 'done'
    assert body['specification_link'] == {
        'work_item_id': str(item.id), 'artifact_id': str(artifact.id), 'requirement_id': 'REQ-18',
    }


def test_req05_references_readable_at_dispatch_and_after_redispatch_via_real_http_read(clean_db):
    """REQ-05's suite-provable half: create with both references, dispatch
    it (assign + move to in-progress), read both back by canonical id, then
    redispatch it (send back and reassign) and read again unchanged. (The
    "inside the receiving container's session" leg is the deferred,
    live-infrastructure item this track's build brief records as C-4.)"""
    spec_artifact = make_artifact()
    dep_artifact = make_artifact()
    item = store.create_work_item({
        'id': uuid.uuid4(), 'project': PROJECT, 'type': 'task', 'displayName': 'Dispatched work',
        'specificationLink': {'artifactId': str(spec_artifact.id), 'requirementId': 'REQ-7'},
        'artifactLinks': [str(dep_artifact.id)],
    })

    store.assign_work_item(item.id, 'backend-agent')
    store.transition_status(item.id, 'in-progress')

    client = Client()
    body = client.get(f'/work-items/{item.id}').json()
    assert body['specification_link']['requirement_id'] == 'REQ-7'
    assert [a['artifact_id'] for a in body['artifact_links']] == [str(dep_artifact.id)]

    # Redispatch.
    store.transition_status(item.id, 'needs-clarification')
    store.assign_work_item(item.id, 'backend-agent')
    store.transition_status(item.id, 'in-progress')

    body_after = client.get(f'/work-items/{item.id}').json()
    assert body_after['specification_link']['requirement_id'] == 'REQ-7'
    assert [a['artifact_id'] for a in body_after['artifact_links']] == [str(dep_artifact.id)]


def test_get_work_item_read_is_access_logged(clean_db):
    """The two references ride along with the same access-logged read
    (internal-work-item-service.md REQ-04) — no separate, unlogged path."""
    item = make_work_item(project=PROJECT)
    client = Client()
    client.get(f'/work-items/{item.id}')
    assert AccessLog.objects.filter(work_item_id=item.id, operation='getWorkItem').exists()


# ---------------------------------------------------------------------------
# REQ-06 — forward query (requirement -> work items -> delivery artifacts)
# ---------------------------------------------------------------------------

def test_forward_query_returns_matching_work_items_with_associations_req06(clean_db):
    artifact = make_artifact()
    matching = make_work_item(project=PROJECT)
    non_matching = make_work_item(project=PROJECT)
    store.record_specification_link(matching.id, artifact.id, 'REQ-18')
    store.record_specification_link(non_matching.id, artifact.id, 'REQ-99')
    store.attach_artifact(matching.id, 'commit', 'deadbeef')

    client = Client()
    body = client.get('/work-items', {'specArtifactId': str(artifact.id), 'requirementId': 'REQ-18'}).json()

    assert len(body) == 1
    assert body[0]['id'] == str(matching.id)
    assert body[0]['specification_link']['requirement_id'] == 'REQ-18'
    assert [a['reference'] for a in body[0]['artifacts']] == ['deadbeef']


def test_forward_query_absent_params_is_unaffected_req_compat(clean_db):
    """Compatibility (PRD §10) — the plain list endpoint's response shape
    is unchanged when neither forward-query parameter is supplied."""
    item = make_work_item(project=PROJECT)
    client = Client()
    body = client.get('/work-items', {'project': PROJECT}).json()
    assert body[0]['id'] == str(item.id)
    assert 'artifacts' not in body[0]
    assert 'specification_link' not in body[0]


# ---------------------------------------------------------------------------
# REQ-06 — backward query (delivery artifact -> work item -> requirement)
# ---------------------------------------------------------------------------

def test_backward_query_resolves_specification_link_ac04(clean_db):
    artifact = make_artifact()
    item = make_work_item(project=PROJECT)
    store.record_specification_link(item.id, artifact.id, 'REQ-18')
    delivery = store.attach_artifact(item.id, 'pull_request', 'https://example/pr/1')

    client = Client()
    body = client.get(f"/work-item-artifacts/{delivery['id']}/specification-link").json()

    assert body == {
        'workItemId': str(item.id), 'deliveryArtifactId': delivery['id'],
        'specArtifactId': str(artifact.id), 'requirementId': 'REQ-18',
    }


def test_backward_query_404_for_unknown_delivery_artifact(clean_db):
    client = Client()
    resp = client.get(f'/work-item-artifacts/{uuid.uuid4()}/specification-link')
    assert resp.status_code == 404
