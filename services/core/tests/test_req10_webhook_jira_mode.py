"""
jira-integration-relocation.md REQ-10 — `core` applies a Jira webhook only to
a project in Jira mode. Three scenarios, each paired with its positive
control (the same fixture, switched to Jira mode, applied as today):

  - the mode read at `handle_webhook_envelope`'s entry, keyed on the
    envelope's own project (the ticket's own Jira project, or a Release's
    Target Project once materialized);
  - the mode read in the Release mirror (`_materialize_release`), which
    refuses to file a Jira Release under a Target Project not in Jira mode;
  - the mode read on the work item a webhook resolves to, at both its
    enforcement points — `_handle_changelog_item` and `_handle_comment_event`
    — which catches a Release filed under a local-mode Target Project before
    this check existed (or switched to local since), and a comment on any
    such item.
"""

from __future__ import annotations

import uuid

from workitems import project_config, registry, store
from workitems.envelope import Kind, build_envelope
from workitems.models import OutboxEvent, WebhookFailure, WorkItem, WorkItemComment, WorkItemReleaseDetail
from workitems.webhook_consumer import handle_webhook_envelope

from tests.jira_fixture import permissive_jira  # noqa: F401 - a pytest fixture, used by name

PROJECT = 'test-project'
TARGET_PROJECT = 'engineering-app'

RELEASE_FIELD_IDS = {
    'JIRA_TARGET_PROJECT_FIELD_ID': 'customfield_target_project',
    'JIRA_RELEASE_NOTES_FIELD_ID': 'customfield_release_notes',
    'JIRA_CANDIDATE_SHA_FIELD_ID': 'customfield_candidate_sha',
    'JIRA_BUILD_IDENTIFIER_FIELD_ID': 'customfield_build_id',
    'JIRA_PREVIEW_URL_FIELD_ID': 'customfield_preview_url',
}


def _set_release_field_env(monkeypatch):
    for env_name, field_id in RELEASE_FIELD_IDS.items():
        monkeypatch.setenv(env_name, field_id)


def _envelope(project, event, issue_key, fields, *, changelog=None, comment=None):
    body = {'webhookEvent': event, 'issue': {'key': issue_key, 'fields': fields}}
    if changelog is not None:
        body['changelog'] = {'items': changelog}
    if comment is not None:
        body['comment'] = comment
    return build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(project), payload={
        'event': event, 'issue': {'key': issue_key, 'fields': fields}, 'body': body,
    })


def _story_fields(project):
    return {'summary': 'A story', 'issuetype': {'name': 'Story'}, 'project': {'name': project, 'key': 'TP'}}


def _release_fields(project, target_project=None):
    fields = {'summary': 'Release it', 'issuetype': {'name': 'Release'}, 'project': {'name': project, 'key': 'TP'}}
    if target_project is not None:
        fields['customfield_target_project'] = {'key': 'ENG', 'name': target_project}
    return fields


# ---------------------------------------------------------------------------
# Read 1 — the mode read at handle_webhook_envelope's entry.
# ---------------------------------------------------------------------------

def test_a_local_mode_project_ignores_a_jira_issue_created_webhook(clean_db):
    env = _envelope(PROJECT, 'jira:issue_created', 'TP-100', _story_fields(PROJECT))
    handle_webhook_envelope(env)  # must not raise — the entry is acknowledged.

    assert WorkItem.objects.filter(external_key='TP-100').count() == 0
    assert not OutboxEvent.objects.filter(event_type='work_item.jira_side_effect').exists()
    assert WebhookFailure.objects.count() == 0
    generic = OutboxEvent.objects.get(event_type='work_item.jira_event_received')
    assert generic.payload['jiraIssueKey'] == 'TP-100'
    assert generic.payload['detail']['ignored']


def test_a_local_mode_project_ignores_a_jira_issue_updated_webhook(clean_db):
    fields = _story_fields(PROJECT)
    env = _envelope(PROJECT, 'jira:issue_updated', 'TP-101', fields,
                    changelog=[{'field': 'status', 'toString': 'In Progress'}])
    handle_webhook_envelope(env)

    assert WorkItem.objects.filter(external_key='TP-101').count() == 0
    assert WebhookFailure.objects.count() == 0
    generic = OutboxEvent.objects.get(event_type='work_item.jira_event_received')
    assert generic.payload['jiraIssueKey'] == 'TP-101'
    assert generic.payload['detail']['ignored']


def test_switching_the_project_to_jira_mode_applies_the_same_webhook_as_today(clean_db, permissive_jira):
    fields = _story_fields(PROJECT)
    env = _envelope(PROJECT, 'jira:issue_created', 'TP-102', fields)

    handle_webhook_envelope(env)  # local mode — ignored, as above.
    assert WorkItem.objects.filter(external_key='TP-102').count() == 0

    project_config.set_mode(PROJECT, 'jira')
    handle_webhook_envelope(env)  # same envelope, project now in Jira mode.

    item = WorkItem.objects.get(external_key='TP-102')
    assert item.type == 'story'


# ---------------------------------------------------------------------------
# Read 3 (the Release mirror) — a Jira Release is never filed under a
# Target Project not in Jira mode.
# ---------------------------------------------------------------------------

def test_a_release_ticket_is_refused_under_a_local_mode_target_project(clean_db, monkeypatch):
    _set_release_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    # TARGET_PROJECT deliberately left local (no ProjectConfig row).

    env = _envelope(PROJECT, 'jira:issue_created', 'REL-100', _release_fields(PROJECT, TARGET_PROJECT))
    handle_webhook_envelope(env)

    assert WorkItem.objects.filter(external_key='REL-100').count() == 0
    assert not OutboxEvent.objects.filter(event_type='work_item.jira_release_event').exists()
    failure = WebhookFailure.objects.get(external_key='REL-100')
    assert failure.project == TARGET_PROJECT


def test_an_update_for_a_release_ticket_never_materialized_under_a_local_mode_target_is_refused_again(clean_db, monkeypatch):
    _set_release_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')

    fields = _release_fields(PROJECT, TARGET_PROJECT)
    # Two changelog items in one envelope: REQ-10's second bullet requires the
    # failure be recorded "for each such changelog item", and each item
    # re-enters _materialize_release independently, so a single-item changelog
    # cannot tell per-item repetition from per-envelope.
    env = _envelope(PROJECT, 'jira:issue_updated', 'REL-101', fields,
                     changelog=[{'field': 'status', 'toString': 'In Review'},
                                {'field': 'status', 'toString': 'In Progress'}])
    handle_webhook_envelope(env)

    assert WorkItem.objects.filter(external_key='REL-101').count() == 0
    failures = list(WebhookFailure.objects.filter(external_key='REL-101'))
    assert len(failures) == 2, 'one failure per changelog item, not one per envelope'
    assert {f.project for f in failures} == {TARGET_PROJECT}
    # Handled as for any unmaterialized ticket: a status change other than
    # Done is recorded with _record_generic_event, once per item.
    generics = list(OutboxEvent.objects.filter(event_type='work_item.jira_event_received'))
    assert len(generics) == 2
    assert [g.payload['detail']['field'] for g in generics] == ['status', 'status']
    assert {g.payload['detail']['to'] for g in generics} == {'In Review', 'In Progress'}


def test_a_release_ticket_materializes_under_a_jira_mode_target_project_as_today(clean_db, monkeypatch):
    _set_release_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    project_config.set_mode(TARGET_PROJECT, 'jira')

    env = _envelope(PROJECT, 'jira:issue_created', 'REL-102', _release_fields(PROJECT, TARGET_PROJECT))
    handle_webhook_envelope(env)

    item = WorkItem.objects.get(external_key='REL-102')
    assert item.type == 'release'
    assert item.project == TARGET_PROJECT
    assert OutboxEvent.objects.get(event_type='work_item.jira_release_event').payload == {
        'kind': 'requested', 'workItemId': str(item.id), 'project': TARGET_PROJECT,
    }, 'carries the materialized Release\'s canonical id and, as project, its Target Project (V5.2 REQ-08)'
    assert WebhookFailure.objects.count() == 0


# ---------------------------------------------------------------------------
# Read 2 — the mode read on the work item a webhook resolves to: a Release
# filed under a local-mode Target Project before this check existed, or
# whose Target Project was switched to local since.
# ---------------------------------------------------------------------------

def test_a_changelog_update_for_a_release_row_now_in_a_local_mode_project_is_ignored(clean_db, monkeypatch):
    _set_release_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    # The release row already exists (e.g. materialized before v5.1, or its
    # Target Project was switched to local after filing) under a project
    # that is not — and never was, for this test — in Jira mode.
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': TARGET_PROJECT, 'type': 'release',
                             'displayName': 'Release it', 'status': 'proposed', 'externalKey': 'REL-103'})
    WorkItemReleaseDetail.objects.create(work_item_id=item_id, release_notes='Original notes.')

    fields = _release_fields(PROJECT, TARGET_PROJECT)
    env = _envelope(PROJECT, 'jira:issue_updated', 'REL-103', fields,
                    changelog=[{'field': 'status', 'toString': 'In Review'}])
    handle_webhook_envelope(env)

    item = WorkItem.objects.get(id=item_id)
    assert item.status == 'proposed', 'no Jira change reaches a local-mode project\'s work item'
    assert not OutboxEvent.objects.filter(event_type='work_item.jira_release_event').exists()
    assert WebhookFailure.objects.count() == 0, 'ignored, not a failure — the resolved item, not the target project, is unmaterializable'
    generic = OutboxEvent.objects.get(event_type='work_item.jira_event_received', work_item_id=item_id)
    assert generic.payload['detail']['ignored']


def test_a_changelog_update_for_a_release_row_in_a_jira_mode_project_still_syncs_as_today(clean_db, monkeypatch):
    _set_release_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': TARGET_PROJECT, 'type': 'release',
                             'displayName': 'Release it', 'status': 'proposed', 'externalKey': 'REL-104'})
    WorkItemReleaseDetail.objects.create(work_item_id=item_id, release_notes='Original notes.')
    # Switched to Jira mode only after the row exists — proves read 2 lets a
    # resolved Jira-mode item's update through unaffected.
    project_config.set_mode(TARGET_PROJECT, 'jira')

    fields = _release_fields(PROJECT, TARGET_PROJECT)
    fields['customfield_candidate_sha'] = 'abc1234'
    env = _envelope(PROJECT, 'jira:issue_updated', 'REL-104', fields,
                    changelog=[{'field': 'Candidate SHA', 'fieldId': 'customfield_candidate_sha',
                                'from': None, 'to': 'abc1234'}])
    handle_webhook_envelope(env)

    detail = WorkItemReleaseDetail.objects.get(work_item_id=item_id)
    assert detail.candidate_sha == 'abc1234', 'a Jira-mode resolved item still re-syncs from the webhook snapshot'


def _comment_env(issue_key, *, comment_id='9001'):
    return _envelope(PROJECT, 'comment_created', issue_key, _story_fields(PROJECT),
                     comment={'id': comment_id, 'author': {'displayName': 'Jane Jira'},
                              'body': 'A comment from Jira.'})


def _local_mode_story(external_key):
    """A work item in TARGET_PROJECT, which has no ProjectConfig row and is
    therefore local, reached by a webhook whose own project (PROJECT) is in
    Jira mode — the only way read 2 is exercised, since the entry-level check
    would otherwise ignore the envelope first."""
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': TARGET_PROJECT, 'type': 'story',
                             'displayName': 'A story', 'status': 'proposed', 'externalKey': external_key})
    return item_id


def test_a_comment_on_a_work_item_in_a_local_mode_project_is_ignored(clean_db):
    project_config.set_mode(PROJECT, 'jira')
    item_id = _local_mode_story('TP-105')

    handle_webhook_envelope(_comment_env('TP-105'))

    assert WorkItemComment.objects.filter(work_item_id=item_id).count() == 0, \
        'no Jira comment reaches a local-mode project\'s work item'
    assert not OutboxEvent.objects.filter(event_type='work_item.comment_added').exists()
    assert not OutboxEvent.objects.filter(event_type='work_item.jira_side_effect').exists()
    assert WebhookFailure.objects.count() == 0, 'ignored, not a failure'
    generic = OutboxEvent.objects.get(event_type='work_item.jira_event_received', work_item_id=item_id)
    assert generic.payload['jiraIssueKey'] == 'TP-105'
    assert generic.payload['detail']['event'] == 'comment'
    assert generic.payload['detail']['ignored']


def test_a_comment_on_a_work_item_in_a_jira_mode_project_is_appended_as_today(clean_db):
    project_config.set_mode(PROJECT, 'jira')
    item_id = _local_mode_story('TP-106')
    # Switched to Jira mode only after the row exists, so the positive control
    # is the same envelope against the same fixture, mode being the one
    # difference.
    project_config.set_mode(TARGET_PROJECT, 'jira')

    handle_webhook_envelope(_comment_env('TP-106'))

    comment = WorkItemComment.objects.get(work_item_id=item_id)
    assert comment.author == 'Jane Jira'
    assert comment.body == 'A comment from Jira.'
    assert comment.source_message_id == 'jira-comment:TP-106:9001'
