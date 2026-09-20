"""
work-items.md REQ-05's canonical-id lookup (V4 audit Pass 2 row 33): a
dispatched agent's prompt carries the issue key
(`services/scrummaster/src/prompt.js:177-180`), never the work item's
canonical id, and until this filter existed nothing mapped that key back
to the record REQ-05 says the agent can read. Exercised through the REAL
HTTP interface (Django's test Client against the real URL routing / view /
readstore stack and the real test Postgres database), mirroring
test_work_item_references_http.py's own approach.
"""

from __future__ import annotations

from django.test import Client

from workitems import store
from workitems.models import AccessLog

from tests.test_work_item_references_store import make_artifact, make_work_item

PROJECT = 'test-project'
OTHER_PROJECT = 'other-project'


def test_external_key_filter_returns_matching_item_with_references(clean_db):
    artifact = make_artifact()
    dep_artifact = make_artifact()
    item = make_work_item(project=PROJECT, externalKey='PROJ-42')
    store.record_specification_link(item.id, artifact.id, 'REQ-18')
    store.add_artifact_link(item.id, dep_artifact.id)
    # A second, non-matching item must not leak into the result.
    make_work_item(project=PROJECT, externalKey='PROJ-99')

    client = Client()
    body = client.get('/work-items', {'externalKey': 'PROJ-42'}).json()

    assert len(body) == 1
    assert body[0]['id'] == str(item.id)
    assert body[0]['external_key'] == 'PROJ-42'
    assert body[0]['specification_link'] == {
        'work_item_id': str(item.id), 'artifact_id': str(artifact.id), 'requirement_id': 'REQ-18',
    }
    assert [a['artifact_id'] for a in body[0]['artifact_links']] == [str(dep_artifact.id)]


def test_external_key_filter_combinable_with_project(clean_db):
    item = make_work_item(project=PROJECT, externalKey='PROJ-42')
    make_work_item(project=OTHER_PROJECT, externalKey='OTHER-1')

    client = Client()
    body = client.get('/work-items', {'externalKey': 'PROJ-42', 'project': PROJECT}).json()
    assert [row['id'] for row in body] == [str(item.id)]

    # Same key, wrong project: the AND of both filters yields nothing —
    # exact-match uniqueness on external_key alone would already guarantee
    # this, but the combination is exercised explicitly since urls.py
    # documents the two as combinable.
    body_wrong_project = client.get('/work-items', {'externalKey': 'PROJ-42', 'project': OTHER_PROJECT}).json()
    assert body_wrong_project == []


def test_unknown_external_key_returns_empty_list(clean_db):
    make_work_item(project=PROJECT, externalKey='PROJ-1')

    client = Client()
    body = client.get('/work-items', {'externalKey': 'does-not-exist'}).json()
    assert body == []


def test_external_key_filter_read_is_access_logged(clean_db):
    make_work_item(project=PROJECT, externalKey='PROJ-42')

    client = Client()
    client.get('/work-items', {'externalKey': 'PROJ-42'})

    assert AccessLog.objects.filter(operation='listWorkItems').exists()


def test_plain_list_without_external_key_is_unaffected(clean_db):
    """Compatibility: the existing filter-less list behaviour (bare fields,
    no references attached) is unchanged by the externalKey branch."""
    item = make_work_item(project=PROJECT, externalKey='PROJ-42')
    store.record_specification_link(item.id, make_artifact().id, 'REQ-1')

    client = Client()
    body = client.get('/work-items', {'project': PROJECT}).json()

    assert len(body) == 1
    assert body[0]['id'] == str(item.id)
    assert 'specification_link' not in body[0]
    assert 'artifact_links' not in body[0]
