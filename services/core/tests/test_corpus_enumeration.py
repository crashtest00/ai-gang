"""
corpus-indexing.md REQ-11 (Core's paged, changed-since enumeration of work
items, work-item comments and delivery artifacts, with their links),
REQ-07's Core half (the outbox row an admin delete writes) and
artifact-text-extraction.md REQ-04's Core half (the artifact listing).

The enumerations are read through the real HTTP routes (Django's test Client
against the real URL routing, views, store and Postgres), the records are
written through the real store, and the deletes are made through the real
admin views (`GatedAdminMixin.delete_model` and `delete_queryset`).
"""

from __future__ import annotations

import uuid

import pytest
from django.db import connection
from django.test import Client

from artifacts.models import Artifact, ArtifactAccessLog
from workitems import store
from workitems.models import AccessLog, OutboxEvent, WorkItem, WorkItemArtifact, WorkItemComment

from tests.artifacts_support import admin_client
from tests.test_work_item_references_store import make_artifact, make_work_item

PROJECT = 'enum-project'

WORK_ITEMS = '/work-items/changes'
COMMENTS = '/work-item-comments/changes'
DELIVERY_ARTIFACTS = '/work-item-artifacts/changes'


def read(path, **params):
    response = Client().get(path, params)
    assert response.status_code == 200, response.content
    return response.json()


def enumerate_all(path, key, limit=2):
    """Every record of a class from an empty cursor, page by page, and the
    cursor the round ends on."""
    records, cursor = [], None
    while True:
        body = read(path, limit=limit, **({'cursor': cursor} if cursor else {}))
        records.extend(body[key])
        cursor = body['nextCursor']
        if not body['hasMore']:
            return records, cursor


# ---------------------------------------------------------------------------
# REQ-11 — each class: every record across pages, then only what changed
# ---------------------------------------------------------------------------

def test_req11_work_items_enumerate_across_pages_then_only_what_changed(clean_db):
    items = [make_work_item(project=PROJECT) for _ in range(5)]

    records, cursor = enumerate_all(WORK_ITEMS, 'workItems')
    assert sorted(r['id'] for r in records) == sorted(str(i.id) for i in items)
    assert len({r['id'] for r in records}) == 5, 'each record once within a round'

    assert read(WORK_ITEMS, cursor=cursor)['workItems'] == [], 'nothing changed since the last cursor'

    fresh = make_work_item(project=PROJECT)
    store.transition_status(items[0].id, 'in-progress')
    body = read(WORK_ITEMS, cursor=cursor)
    assert sorted(r['id'] for r in body['workItems']) == sorted([str(fresh.id), str(items[0].id)])
    assert body['hasMore'] is False


def test_req11_comments_enumerate_across_pages_then_only_new_ones(clean_db):
    item = make_work_item(project=PROJECT)
    for n in range(5):
        store.append_comment(item.id, 'tester', f'comment {n}')

    records, cursor = enumerate_all(COMMENTS, 'comments')
    assert sorted(r['body'] for r in records) == [f'comment {n}' for n in range(5)]
    assert {r['work_item_id'] for r in records} == {str(item.id)}
    assert {r['project'] for r in records} == {PROJECT}
    assert read(COMMENTS, cursor=cursor)['comments'] == []

    store.append_comment(item.id, 'tester', 'a new comment')
    body = read(COMMENTS, cursor=cursor)
    assert [c['body'] for c in body['comments']] == ['a new comment']


def test_req11_delivery_artifacts_enumerate_across_pages_then_only_new_ones(clean_db):
    item = make_work_item(project=PROJECT)
    spec = make_artifact()
    store.record_specification_link(item.id, spec.id, 'REQ-7')
    for n in range(5):
        store.attach_artifact(item.id, 'commit', f'sha-{n}')

    records, cursor = enumerate_all(DELIVERY_ARTIFACTS, 'deliveryArtifacts')
    assert sorted(r['reference'] for r in records) == [f'sha-{n}' for n in range(5)]
    assert {r['work_item_id'] for r in records} == {str(item.id)}
    assert {r['project'] for r in records} == {PROJECT}
    assert {(r['specification_link']['artifact_id'], r['specification_link']['requirement_id']) for r in records} == {
        (str(spec.id), 'REQ-7')}
    assert read(DELIVERY_ARTIFACTS, cursor=cursor)['deliveryArtifacts'] == []

    store.attach_artifact(item.id, 'pull_request', 'pr-9')
    body = read(DELIVERY_ARTIFACTS, cursor=cursor)
    assert [a['reference'] for a in body['deliveryArtifacts']] == ['pr-9']


def test_req11_a_work_items_record_carries_all_its_links(clean_db):
    parent = make_work_item(project=PROJECT, type='story', status='in-progress', storyDetail={
        'behavior': 'b', 'acceptanceCriteria': 'ac', 'constraints': 'c', 'edgeCases': 'e', 'outOfScope': 'oos'})
    other = make_work_item(project=PROJECT)
    item = make_work_item(project=PROJECT, parentId=parent.id, status='in-progress')
    spec, library = make_artifact(), make_artifact()
    link = store.create_link(item.id, other.id, 'blocks')
    store.record_specification_link(item.id, spec.id, 'REQ-18')
    store.add_artifact_link(item.id, library.id)
    delivery = store.attach_artifact(item.id, 'commit', 'abc123')
    comment = store.append_comment(item.id, 'tester', 'looks right')

    records, _ = enumerate_all(WORK_ITEMS, 'workItems', limit=10)
    record = next(r for r in records if r['id'] == str(item.id))

    assert record['parent_id'] == str(parent.id)
    assert [(l['id'], l['from_work_item_id'], l['to_work_item_id'], l['link_type']) for l in record['links']] == [
        (link['id'], str(item.id), str(other.id), 'blocks')]
    assert record['specification_link'] == {
        'work_item_id': str(item.id), 'artifact_id': str(spec.id), 'requirement_id': 'REQ-18'}
    assert [a['artifact_id'] for a in record['artifact_links']] == [str(library.id)]
    assert [a['id'] for a in record['artifacts']] == [delivery['id']]
    assert [c['id'] for c in record['comments']] == [comment['id']]
    assert any(h['field'] == 'status' for h in record['history']), 'history is carried'

    # The other end of the link carries it too: a consumer rebuilds the
    # relation from either record.
    other_record = next(r for r in records if r['id'] == str(other.id))
    assert [l['id'] for l in other_record['links']] == [link['id']]
    # Each side-record of the item moves its work item's record (the item's
    # comments and delivery artifacts are part of what it enumerates).
    parent_record = next(r for r in records if r['id'] == str(parent.id))
    assert parent_record['storyDetail']['behavior'] == 'b'


def test_req11_a_child_row_written_later_moves_its_work_items_record(clean_db):
    item = make_work_item(project=PROJECT)
    spec, library = make_artifact(), make_artifact()
    _, cursor = enumerate_all(WORK_ITEMS, 'workItems')

    for write in (
        lambda: store.append_comment(item.id, 'tester', 'hi'),
        lambda: store.attach_artifact(item.id, 'commit', 'abc'),
        lambda: store.record_specification_link(item.id, spec.id, 'REQ-1'),
        lambda: store.add_artifact_link(item.id, library.id),
        lambda: store.create_link(item.id, make_work_item(project=PROJECT).id, 'blocks'),
    ):
        write()
        records, cursor = enumerate_all(WORK_ITEMS, 'workItems')
        assert str(item.id) in {r['id'] for r in records}


def test_req11_a_row_that_commits_after_a_later_one_was_read_is_not_skipped(clean_db):
    """The cursor is the oldest transaction still running when a round
    started, so a row whose transaction was open during that round is
    returned by the next one, not lost."""
    item = make_work_item(project=PROJECT)
    late_id = uuid.uuid4()
    late = connection.copy()
    late.set_autocommit(False)
    try:
        with late.cursor() as db:
            db.execute(
                "INSERT INTO work_item_comment (id, work_item_id, author, body, created_at) "
                "VALUES (%s, %s, 'late', 'written first, committed last', now())", [str(late_id), str(item.id)])
        store.append_comment(item.id, 'tester', 'written second, committed first')

        records, cursor = enumerate_all(COMMENTS, 'comments')
        assert [r['body'] for r in records] == ['written second, committed first']
    finally:
        late.commit()
        late.close()

    after_commit = read(COMMENTS, cursor=cursor)['comments']
    assert str(late_id) in {c['id'] for c in after_commit}, 'the late commit is returned by the next round'


def test_req11_a_bad_cursor_or_limit_is_a_client_error(clean_db):
    client = Client()
    for path in (WORK_ITEMS, COMMENTS, DELIVERY_ARTIFACTS):
        assert client.get(path, {'cursor': 'not-a-cursor'}).status_code == 400
        assert client.get(path, {'limit': '0'}).status_code == 400
        assert client.get(path, {'limit': 'many'}).status_code == 400


def test_req11_enumerations_write_no_access_log_row(clean_db):
    item = make_work_item(project=PROJECT)
    store.append_comment(item.id, 'tester', 'hi')
    store.attach_artifact(item.id, 'commit', 'abc')
    before = AccessLog.objects.count()

    for path, key in ((WORK_ITEMS, 'workItems'), (COMMENTS, 'comments'), (DELIVERY_ARTIFACTS, 'deliveryArtifacts')):
        records, cursor = enumerate_all(path, key)
        assert records
        read(path, cursor=cursor)

    assert AccessLog.objects.count() == before


# ---------------------------------------------------------------------------
# artifact-text-extraction.md REQ-04 — the artifact listing
# ---------------------------------------------------------------------------

def test_req04_the_artifact_listing_returns_id_and_updated_at_across_pages(clean_db):
    Artifact.objects.all().delete()
    made = [make_artifact() for _ in range(5)]
    before = ArtifactAccessLog.objects.count()

    listed, cursor = [], None
    while True:
        body = read('/artifacts', limit=2, **({'cursor': cursor} if cursor else {}))
        assert all(set(a) == {'id', 'updatedAt'} for a in body['artifacts'])
        listed.extend(body['artifacts'])
        cursor = body['nextCursor']
        if not body['hasMore']:
            break

    assert [a['id'] for a in listed] == sorted(str(a.id) for a in made)
    assert {a['id']: a['updatedAt'] for a in listed} == {str(a.id): a.updated_at.isoformat() for a in made}
    assert ArtifactAccessLog.objects.count() == before, 'a sweep writes no access-log row per file'

    assert Client().get('/artifacts', {'cursor': 'nope'}).status_code == 400
    assert Client().get('/artifacts', {'limit': '0'}).status_code == 400
    assert Client().get(f'/artifacts/{made[0].id}').status_code in (200, 404), 'the by-id read is unaffected'


# ---------------------------------------------------------------------------
# REQ-07 (Core's half) — an admin delete writes an outbox row naming the record
# ---------------------------------------------------------------------------

def deletion_rows():
    return list(OutboxEvent.objects.filter(event_type=store.RECORD_DELETED_EVENT_TYPE).order_by('created_at'))


def test_req07_admin_delete_of_a_comment_writes_an_outbox_row_in_the_same_transaction(clean_db):
    client = admin_client('delete-comment-admin')
    item = make_work_item(project=PROJECT)
    comment = store.append_comment(item.id, 'tester', 'to be deleted')

    resp = client.post(f"/django-admin/workitems/workitemcomment/{comment['id']}/delete/", {'post': 'yes'})
    assert resp.status_code == 302
    assert not WorkItemComment.objects.filter(id=comment['id']).exists()

    (row,) = deletion_rows()
    assert (row.project, str(row.work_item_id)) == (PROJECT, str(item.id))
    assert row.payload == {
        'recordClass': 'comment', 'id': comment['id'], 'workItemId': str(item.id), 'deletedBy': 'delete-comment-admin'}
    assert row.published_at is None, 'it rides the project stream through the relay like every other row'


def test_req07_admin_delete_of_a_delivery_artifact_writes_an_outbox_row(clean_db):
    client = admin_client('delete-artifact-admin')
    item = make_work_item(project=PROJECT)
    delivery = store.attach_artifact(item.id, 'commit', 'abc123')

    resp = client.post(f"/django-admin/workitems/workitemartifact/{delivery['id']}/delete/", {'post': 'yes'})
    assert resp.status_code == 302
    assert not WorkItemArtifact.objects.filter(id=delivery['id']).exists()

    (row,) = deletion_rows()
    assert row.payload['recordClass'] == 'delivery_artifact'
    assert row.payload['id'] == delivery['id']


def test_req07_admin_delete_of_a_work_item_writes_an_outbox_row_naming_it(clean_db):
    client = admin_client('delete-item-admin')
    item = make_work_item(project=PROJECT)
    OutboxEvent.objects.all().delete()
    from workitems.models import WorkItemHistory
    # create_work_item records history, and history is append-only at the
    # database, so a work item that has any cannot be deleted at all; the
    # delete this row names is of one without.
    with connection.cursor() as db:
        db.execute('ALTER TABLE work_item_history DISABLE TRIGGER work_item_history_append_only')
        try:
            WorkItemHistory.objects.filter(work_item_id=item.id).delete()
        finally:
            db.execute('ALTER TABLE work_item_history ENABLE TRIGGER work_item_history_append_only')

    resp = client.post(f'/django-admin/workitems/workitem/{item.id}/delete/', {'post': 'yes'})
    assert resp.status_code == 302
    assert not WorkItem.objects.filter(id=item.id).exists()

    (row,) = deletion_rows()
    assert row.payload['recordClass'] == 'work_item'
    assert row.payload['id'] == str(item.id)


def test_req07_a_bulk_admin_delete_writes_one_row_per_deleted_record(clean_db):
    client = admin_client('bulk-delete-admin')
    item = make_work_item(project=PROJECT)
    comments = [store.append_comment(item.id, 'tester', f'c{n}') for n in range(3)]
    keep = store.append_comment(item.id, 'tester', 'kept')

    resp = client.post('/django-admin/workitems/workitemcomment/', {
        'action': 'delete_selected', 'post': 'yes', '_selected_action': [c['id'] for c in comments],
    })
    assert resp.status_code == 302
    assert list(WorkItemComment.objects.values_list('id', flat=True)) == [uuid.UUID(keep['id'])]
    assert sorted(r.payload['id'] for r in deletion_rows()) == sorted(c['id'] for c in comments)


def test_req07_a_refused_delete_writes_no_row_and_deletes_nothing(clean_db):
    """A Jira-mode project's rows are refused (canonical-delivery-state.md
    REQ-09); the refusal comes first, so no outbox row names a record that
    still exists."""
    from workitems import project_config
    client = admin_client('refused-delete-admin')
    item = make_work_item(project='jira-enum-project')
    comment = store.append_comment(item.id, 'tester', 'stays')
    project_config.set_mode('jira-enum-project', project_config.JIRA, jira_project_key='JEP')

    client.post(f"/django-admin/workitems/workitemcomment/{comment['id']}/delete/", {'post': 'yes'})

    assert WorkItemComment.objects.filter(id=comment['id']).exists()
    assert deletion_rows() == []


def test_req07_the_relay_publishes_the_row_on_the_project_stream_as_its_own_event_type(clean_db, redis_client):
    """The row rides the project's work-item stream under an event type no
    reader of that stream names (ScrumMaster's `handleWorkItemEventEnvelope`
    and Core's `jira_writer.handle_event_envelope` act only on the types
    they list), so it is acknowledged and ignored there."""
    from workitems import relay
    from workitems.stream_topology import event_stream_name

    client = admin_client('relay-delete-admin')
    item = make_work_item(project=PROJECT)
    comment = store.append_comment(item.id, 'tester', 'bye')
    OutboxEvent.objects.all().delete()
    client.post(f"/django-admin/workitems/workitemcomment/{comment['id']}/delete/", {'post': 'yes'})

    assert relay.relay_once(redis_client) == 1
    (_, fields), = redis_client.xrange(event_stream_name(PROJECT))
    import json
    envelope = json.loads(fields['data'])
    payload = envelope['payload']
    assert payload['eventType'] == store.RECORD_DELETED_EVENT_TYPE
    assert payload['data']['id'] == comment['id']
