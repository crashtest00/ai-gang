"""
Django-owned interpretation of the full
Jira webhook payload (not just `changelog` entries with `field ==
'status'`). Each test here proves ONE of `services/scrummaster/src/server.js`'s
former `routeWebhookEvent` scenarios now produces the same resulting
canonical behavior via `workitems/webhook_consumer.py` +
`workitems/jira_interpret.py`.
"""

from __future__ import annotations

import uuid

import pytest

from workitems import jira_client, jira_writer, project_config, registry, store, write_gate
from workitems.envelope import Kind, build_envelope
from workitems.models import OutboxEvent, WebhookFailure, WorkItem, WorkItemComment, WorkItemReleaseDetail
from workitems import webhook_consumer
from workitems.webhook_consumer import handle_webhook_envelope

from tests.jira_fixture import permissive_jira  # noqa: F401 - a pytest fixture, used by name
from tests.jira_fixture import posted_comment_texts

PROJECT = 'test-project'

# v5.2: a Jira-mode project's story intake now POSTS its acknowledgement and
# missing-fields comments to Jira through the outbound writer, instead of
# leaving them to a consumer that did not exist
# (canonical-delivery-state.md REQ-09). Every test below whose subject is the
# INBOUND interpretation but which now makes that outbound call takes
# `permissive_jira`, the fixture Jira that accepts and records anything — the
# posted text itself is asserted in test_jira_writer.py and in the two tests
# at the end of this file.

STORY_FIELD_IDS = {
    'JIRA_BEHAVIOR_FIELD_ID': 'customfield_behavior',
    'JIRA_AC_FIELD_ID': 'customfield_ac',
    'JIRA_CONSTRAINTS_FIELD_ID': 'customfield_constraints',
    'JIRA_EDGE_CASES_FIELD_ID': 'customfield_edge',
    'JIRA_OUT_OF_SCOPE_FIELD_ID': 'customfield_oos',
}

RELEASE_FIELD_IDS = {
    'JIRA_TARGET_PROJECT_FIELD_ID': 'customfield_target_project',
    'JIRA_RELEASE_NOTES_FIELD_ID': 'customfield_release_notes',
    'JIRA_CANDIDATE_SHA_FIELD_ID': 'customfield_candidate_sha',
    'JIRA_BUILD_IDENTIFIER_FIELD_ID': 'customfield_build_id',
    'JIRA_PREVIEW_URL_FIELD_ID': 'customfield_preview_url',
}


def _set_story_field_env(monkeypatch):
    for env_name, field_id in STORY_FIELD_IDS.items():
        monkeypatch.setenv(env_name, field_id)


def _set_release_field_env(monkeypatch):
    for env_name, field_id in RELEASE_FIELD_IDS.items():
        monkeypatch.setenv(env_name, field_id)


def _adf(text):
    return {'type': 'doc', 'version': 1, 'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': text}]}]}


def _story_fields(summary='A new story', complete=True, project=PROJECT):
    fields = {
        'summary': summary,
        'issuetype': {'name': 'Story'},
        'project': {'name': project, 'key': 'TP'},
    }
    if complete:
        fields.update({
            'customfield_behavior': _adf('Users can log in.'),
            'customfield_ac': _adf('Given valid creds, a session is created.'),
            'customfield_constraints': _adf('Must use OAuth.'),
            'customfield_edge': _adf('Invalid creds are rejected.'),
            'customfield_oos': _adf('SSO is out of scope.'),
        })
    return fields


def envelope_for(issue_key, event, issue_fields, *, body_extra=None, project=PROJECT):
    body = {'webhookEvent': event, 'issue': {'key': issue_key, 'fields': issue_fields}}
    if body_extra:
        body.update(body_extra)
    return build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(project), payload={
        'event': event, 'issue': body['issue'], 'body': body,
    })


# ---------------------------------------------------------------------------
# Handler 1 — Story created (handlers.js `handleStoryCreated`)
# ---------------------------------------------------------------------------

def test_story_created_with_complete_fields_is_dispatch_eligible_immediately(clean_db, monkeypatch, permissive_jira):
    _set_story_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    handle_webhook_envelope(envelope_for('TP-1', 'jira:issue_created', _story_fields(complete=True)))

    item = WorkItem.objects.get(external_key='TP-1')
    assert item.type == 'story'
    assert item.status == 'ready', "fields complete -> immediately dispatch-eligible"
    assert item.assignee_agent_id == 'refinement-agent'
    assert item.story_detail.behavior == 'Users can log in.'

    side_effect = OutboxEvent.objects.get(event_type='work_item.jira_side_effect', work_item_id=item.id)
    assert side_effect.payload == {'kind': 'story_intake', 'externalKey': 'TP-1', 'detail': {'ok': True, 'missing': []}}
    assert 'jiraIssueKey' not in side_effect.payload

    created_event = OutboxEvent.objects.get(event_type='work_item.created', work_item_id=item.id)
    assert created_event.payload['status'] == 'ready'


def test_story_created_missing_required_fields_stays_proposed_and_blocked(clean_db, monkeypatch, permissive_jira):
    _set_story_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    handle_webhook_envelope(envelope_for('TP-2', 'jira:issue_created', _story_fields(complete=False)))

    item = WorkItem.objects.get(external_key='TP-2')
    assert item.status == 'proposed', "must not leave 'proposed' with required fields missing"
    assert item.assignee_agent_id == 'refinement-agent', 'handlers.js sets the Agent field regardless of validation outcome'

    side_effect = OutboxEvent.objects.get(event_type='work_item.jira_side_effect', work_item_id=item.id)
    detail = side_effect.payload['detail']
    assert detail['ok'] is False
    assert detail['missing'] == ['Behavior', 'Acceptance Criteria', 'Constraints', 'Edge Cases', 'Out of Scope']


def test_story_created_is_idempotent_against_webhook_redelivery(clean_db, monkeypatch, permissive_jira):
    _set_story_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    env = envelope_for('TP-3', 'jira:issue_created', _story_fields(complete=True))
    handle_webhook_envelope(env)
    handle_webhook_envelope(env)  # redelivery — must not create a second work item.

    assert WorkItem.objects.filter(external_key='TP-3').count() == 1


def test_issue_created_of_an_unhandled_issuetype_is_recorded_not_dropped(clean_db):
    project_config.set_mode(PROJECT, 'jira')
    fields = {'summary': 'A bug', 'issuetype': {'name': 'Bug'}, 'project': {'name': PROJECT, 'key': 'TP'}}
    handle_webhook_envelope(envelope_for('TP-4', 'jira:issue_created', fields))

    assert WorkItem.objects.filter(external_key='TP-4').count() == 0, 'no canonical type for Bug — no work item invented'
    event = OutboxEvent.objects.get(event_type='work_item.jira_event_received')
    assert event.payload['jiraIssueKey'] == 'TP-4'
    assert event.payload['detail']['issuetype'] == 'Bug'


# ---------------------------------------------------------------------------
# Handler 3 — Blocked field cleared (handlers.js `handleBlockedCleared`),
# including the refinement-agent Story regression this task calls out
# explicitly.
# ---------------------------------------------------------------------------

def _blocked_change(from_val, to_val):
    return {'field': 'Blocked', 'fieldId': 'customfield_blocked', 'from': from_val, 'to': to_val}


def test_blocked_cleared_on_a_story_with_fields_now_complete_dispatches_with_full_context(clean_db, monkeypatch, permissive_jira):
    _set_story_field_env(monkeypatch)
    monkeypatch.setenv('JIRA_BLOCKED_FIELD_ID', 'customfield_blocked')
    project_config.set_mode(PROJECT, 'jira')

    handle_webhook_envelope(envelope_for('TP-5', 'jira:issue_created', _story_fields(complete=False)))
    item = WorkItem.objects.get(external_key='TP-5')
    assert item.status == 'proposed'

    # The human filled in the missing fields in Jira, then cleared Blocked.
    # The webhook's `issue` snapshot now carries the complete field set.
    complete_fields = _story_fields(complete=True)
    body = {'webhookEvent': 'jira:issue_updated', 'issue': {'key': 'TP-5', 'fields': complete_fields},
            'changelog': {'items': [_blocked_change('10001', None)]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    item.refresh_from_db()
    assert item.status == 'ready', 'story-fields gate now passes -> dispatch-eligible, matching handleBlockedCleared'
    assert item.story_detail.acceptance_criteria == 'Given valid creds, a session is created.'
    assert WebhookFailure.objects.filter(work_item_id=item.id).count() == 0


def test_blocked_cleared_on_a_story_still_missing_fields_re_blocks(clean_db, monkeypatch, permissive_jira):
    _set_story_field_env(monkeypatch)
    monkeypatch.setenv('JIRA_BLOCKED_FIELD_ID', 'customfield_blocked')
    project_config.set_mode(PROJECT, 'jira')

    handle_webhook_envelope(envelope_for('TP-6', 'jira:issue_created', _story_fields(complete=False)))
    item = WorkItem.objects.get(external_key='TP-6')

    still_incomplete = _story_fields(complete=False)
    body = {'webhookEvent': 'jira:issue_updated', 'issue': {'key': 'TP-6', 'fields': still_incomplete},
            'changelog': {'items': [_blocked_change('10001', None)]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    item.refresh_from_db()
    assert item.status == 'proposed', 'must not dispatch while required fields are still missing'
    failure = WebhookFailure.objects.get(work_item_id=item.id)
    assert failure.reason  # a durable, operator-visible failure record.
    side_effects = list(OutboxEvent.objects.filter(event_type='work_item.jira_side_effect', work_item_id=item.id))
    reblock = [e for e in side_effects if e.payload['detail'].get('reblock')]
    assert len(reblock) == 1
    assert reblock[0].payload['detail']['missing'] == ['Behavior', 'Acceptance Criteria', 'Constraints', 'Edge Cases', 'Out of Scope']


def test_blocked_cleared_on_a_dev_agent_ticket_does_not_touch_status_and_signals_redispatch(clean_db, monkeypatch):
    monkeypatch.setenv('JIRA_BLOCKED_FIELD_ID', 'customfield_blocked')
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Implement thing',
                             'status': 'in-progress', 'assigneeAgentId': 'backend-agent', 'externalKey': 'TP-7'})
    # REQ-10: the project connects to Jira (with an item already on it)
    # only after that item exists — connect_jira's own ordering — then the
    # webhook delivers.
    project_config.set_mode(PROJECT, 'jira')

    fields = {'summary': 'Implement thing', 'issuetype': {'name': 'Task'}, 'project': {'name': PROJECT, 'key': 'TP'}}
    body = {'webhookEvent': 'jira:issue_updated', 'issue': {'key': 'TP-7', 'fields': fields},
            'changelog': {'items': [_blocked_change('10001', None)]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    item = store.get_work_item(item_id)
    assert item.status == 'in-progress', "clearing Blocked on a non-story must not itself change canonical status"
    event = OutboxEvent.objects.get(event_type='work_item.jira_side_effect', work_item_id=item_id)
    assert event.payload['kind'] == 'blocked_cleared'
    assert event.payload['externalKey'] == 'TP-7'
    assert 'jiraIssueKey' not in event.payload


# ---------------------------------------------------------------------------
# Comment webhooks (previously silently discarded)
# ---------------------------------------------------------------------------

def test_comment_created_webhook_is_projected_into_the_canonical_comment_thread(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X', 'externalKey': 'TP-8'})
    project_config.set_mode(PROJECT, 'jira')

    body = {
        'webhookEvent': 'comment_created',
        'issue': {'key': 'TP-8', 'fields': {}},
        'comment': {'id': '999', 'author': {'displayName': 'Jane Doe'}, 'body': _adf('Please clarify the auth flow.')},
    }
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'comment_created', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    comment = WorkItemComment.objects.get(work_item_id=item_id)
    assert comment.author == 'Jane Doe'
    assert comment.body == 'Please clarify the auth flow.'
    assert OutboxEvent.objects.filter(event_type='work_item.comment_added', work_item_id=item_id).exists()

    # Redelivery of the same Jira comment must not create a second row.
    handle_webhook_envelope(env)
    assert WorkItemComment.objects.filter(work_item_id=item_id).count() == 1


def test_comment_on_an_untracked_issue_is_recorded_generically_not_dropped(clean_db):
    project_config.set_mode(PROJECT, 'jira')
    body = {'webhookEvent': 'comment_created', 'issue': {'key': 'TP-99', 'fields': {}},
            'comment': {'id': '1', 'author': {'displayName': 'X'}, 'body': _adf('hi')}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'comment_created', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)
    assert OutboxEvent.objects.filter(event_type='work_item.jira_event_received').exists()


# ---------------------------------------------------------------------------
# Handlers 5/6 — Release requested / done / abandoned. BF-01
# (v2.1/BUGFIXES.md) first made Django ALSO materialize a canonical
# `release` work item and its `work_item_release_detail` row from the
# ticket's five fields (REQ-01). From V5.2 Canonical Delivery State REQ-08,
# Django's three release publishers (`_handle_release_requested`/
# `_handle_release_done`/`_handle_release_abandoned`) are the one trigger
# for a Jira-mode candidate cut, production promotion or teardown: each
# carries the materialized Release's canonical `workItemId` and, as
# `project`, its Target Project, and ScrumMaster's `routeReleaseEvent`
# triggers Jenkins for it in either mode (no more Jira-mode early return).
# `_handle_release_requested` also runs the same two release checks
# ScrumMaster's pre-v5.1 Jira-mode branch ran itself (missing Target
# Project, outstanding beta queue) before publishing, since nothing else
# does so for a Jira-mode Release now.
# ---------------------------------------------------------------------------

def _release_fields(summary='Release it', project=PROJECT, target_project=('ENG', 'engineering-app'),
                     release_notes='Fixes login bug.', candidate_sha=None, build_identifier=None, preview_url=None):
    fields = {
        'summary': summary,
        'issuetype': {'name': 'Release'},
        'project': {'name': project, 'key': 'TP'},
    }
    if target_project is not None:
        key, name = target_project
        fields['customfield_target_project'] = {'key': key, 'name': name}
    if release_notes is not None:
        fields['customfield_release_notes'] = release_notes
    if candidate_sha is not None:
        fields['customfield_candidate_sha'] = candidate_sha
    if build_identifier is not None:
        fields['customfield_build_id'] = build_identifier
    if preview_url is not None:
        fields['customfield_preview_url'] = preview_url
    return fields


def test_release_ticket_created_materializes_a_canonical_release_work_item(clean_db, monkeypatch, permissive_jira):
    _set_release_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    project_config.set_mode('engineering-app', 'jira')
    fields = _release_fields()
    handle_webhook_envelope(envelope_for('TP-10', 'jira:issue_created', fields))

    item = WorkItem.objects.get(external_key='TP-10')
    event = OutboxEvent.objects.get(event_type='work_item.jira_release_event')
    assert event.payload == {'kind': 'requested', 'workItemId': str(item.id), 'project': 'engineering-app'}, \
        'carries the materialized Release\'s canonical id and, as project, its Target Project (REQ-08) — no clean beta queue blocks it, so it cuts a candidate'

    assert item.type == 'release'
    assert item.status == 'proposed'
    assert item.project == 'engineering-app', 'Target Project custom field maps onto the work item\'s own project (REQ-01), not the ticket\'s own containing Jira project'

    detail = WorkItemReleaseDetail.objects.get(work_item_id=item.id)
    assert detail.release_notes == 'Fixes login bug.'
    assert detail.candidate_sha is None, 'automation-populated field — empty until candidate cut (REQ-01)'
    assert detail.build_identifier is None
    assert detail.preview_url is None


def test_release_ticket_created_without_target_project_field_falls_back_to_the_containing_jira_project(clean_db, monkeypatch, permissive_jira):
    _set_release_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    fields = _release_fields(target_project=None)
    handle_webhook_envelope(envelope_for('TP-14', 'jira:issue_created', fields))

    item = WorkItem.objects.get(external_key='TP-14')
    assert item.project == registry.normalize_project_name(PROJECT), \
        '_materialize_release still falls back to the ticket\'s own project for the WORK ITEM row'

    # REQ-08: the raw Target Project field (read before that fallback) is
    # what the missing-Target-Project check sees, so this Release is left
    # uncut and gets the one release gate's Target Project refusal
    # (release-mode-parity.md REQ-11) — no beta-queue comment, since that
    # check never runs.
    assert not OutboxEvent.objects.filter(event_type='work_item.jira_release_event').exists()
    texts = posted_comment_texts()
    assert len(texts) == 1
    assert texts[0] == ('[system] [system] release request rejected: Target Project is not set\n\n'
                         'Resolve this and request the release again.'), \
        'the writer posts "[<author>] <body>" over the rejection format\'s own "[system] " marker (REQ-09)'


def test_release_requested_with_outstanding_beta_queue_gets_the_queue_comment_and_cuts_no_candidate(clean_db, monkeypatch, permissive_jira):
    _set_release_field_env(monkeypatch)
    # Seeded while the target project is still local — create_work_item
    # refuses a direct write once a project is in Jira mode (same ordering
    # test_issue_link_changelog_entry_is_recorded_not_dropped uses below).
    outstanding_id = uuid.uuid4()
    store.create_work_item({'id': outstanding_id, 'project': 'engineering-app', 'type': 'task',
                             'displayName': 'Still in review', 'status': 'in-review'})
    project_config.set_mode('engineering-app', 'jira')
    project_config.set_mode(PROJECT, 'jira')

    fields = _release_fields()
    handle_webhook_envelope(envelope_for('TP-19', 'jira:issue_created', fields))

    assert not OutboxEvent.objects.filter(event_type='work_item.jira_release_event').exists(), \
        'a story still in review cuts no candidate'
    item = WorkItem.objects.get(external_key='TP-19')
    assert item.type == 'release', 'the Release still materializes even though it is not cut'
    texts = posted_comment_texts()
    assert len(texts) == 1
    assert texts[0].startswith('[system] [system] release request rejected: 1 work item(s) are still '
                               'awaiting acceptance on beta'), texts
    assert str(outstanding_id) in texts[0], 'lists the outstanding item by its key, or its id when it has none'
    assert texts[0].endswith('Resolve this and request the release again.')


def test_release_requested_release_check_comment_is_posted_once_on_webhook_redelivery(clean_db, monkeypatch, permissive_jira):
    _set_release_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    fields = _release_fields(target_project=None)
    env = envelope_for('TP-20', 'jira:issue_created', fields)
    handle_webhook_envelope(env)
    handle_webhook_envelope(env)  # redelivery — must not post a second comment.

    texts = posted_comment_texts()
    assert len(texts) == 1


def test_release_ticket_created_is_idempotent_against_webhook_redelivery(clean_db, monkeypatch, permissive_jira):
    _set_release_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    project_config.set_mode('engineering-app', 'jira')
    env = envelope_for('TP-15', 'jira:issue_created', _release_fields())
    handle_webhook_envelope(env)
    handle_webhook_envelope(env)  # redelivery — must not create a second work item.

    assert WorkItem.objects.filter(external_key='TP-15').count() == 1
    assert WorkItemReleaseDetail.objects.filter(work_item__external_key='TP-15').count() == 1


def test_release_ticket_update_resyncs_candidate_fields_written_back_by_jenkins(clean_db, monkeypatch, permissive_jira):
    _set_release_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    project_config.set_mode('engineering-app', 'jira')
    handle_webhook_envelope(envelope_for('TP-16', 'jira:issue_created', _release_fields()))
    item = WorkItem.objects.get(external_key='TP-16')

    # The writer pushes Candidate SHA, Build Identifier and Preview URL onto
    # the Jira ticket in one edit (release-mode-parity.md REQ-13) — an
    # ordinary issue_updated webhook the same as any other Jira field edit.
    updated_fields = _release_fields(candidate_sha='abc1234', build_identifier='build-42',
                                      preview_url='https://preview.example.com/abc1234')
    body = {'webhookEvent': 'jira:issue_updated', 'issue': {'key': 'TP-16', 'fields': updated_fields},
            'changelog': {'items': [{'field': 'Candidate SHA', 'fieldId': 'customfield_candidate_sha',
                                      'from': None, 'to': 'abc1234'}]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    detail = WorkItemReleaseDetail.objects.get(work_item_id=item.id)
    assert detail.candidate_sha == 'abc1234'
    assert detail.build_identifier == 'build-42'
    assert detail.preview_url == 'https://preview.example.com/abc1234'
    assert detail.release_notes == 'Fixes login bug.', 'unrelated field carried through from the same full-snapshot resync'


def test_release_ticket_update_with_no_prior_create_materializes_the_canonical_work_item(clean_db, monkeypatch):
    """BUGFIXES.md BF-01 Pass 1 audit row 2: a Release ticket created
    before BF-01 shipped never got a canonical `release` work item from
    the (never-fired) `jira:issue_created` webhook it originally received
    — only its later `jira:issue_updated` webhooks are still arriving.
    REQ-01 says "create OR update webhook" must materialize it."""
    _set_release_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    project_config.set_mode('engineering-app', 'jira')
    fields = _release_fields(release_notes='Fixes the login bug.', candidate_sha='abc1234')
    body = {'webhookEvent': 'jira:issue_updated', 'issue': {'key': 'TP-17', 'fields': fields},
            'changelog': {'items': [{'field': 'Candidate SHA', 'fieldId': 'customfield_candidate_sha',
                                      'from': None, 'to': 'abc1234'}]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    item = WorkItem.objects.get(external_key='TP-17')
    assert item.type == 'release'
    assert item.project == 'engineering-app', 'Target Project custom field still maps onto the work item\'s own project'

    detail = WorkItemReleaseDetail.objects.get(work_item_id=item.id)
    assert detail.release_notes == 'Fixes the login bug.'
    assert detail.candidate_sha == 'abc1234', 'detail columns populated from the payload\'s issue.fields'

    # No jira:issue_created webhook ever fired for this ticket, so the
    # 'requested' side-effect that path publishes must not appear either —
    # only the jira:issue_created handler publishes work_item.jira_release_event.
    assert not OutboxEvent.objects.filter(event_type='work_item.jira_release_event').exists()


def test_release_ticket_update_with_no_prior_create_is_idempotent_against_a_second_update(
        clean_db, monkeypatch, permissive_jira):
    _set_release_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    project_config.set_mode('engineering-app', 'jira')

    def _update_envelope(candidate_sha):
        fields = _release_fields(candidate_sha=candidate_sha)
        body = {'webhookEvent': 'jira:issue_updated', 'issue': {'key': 'TP-18', 'fields': fields},
                'changelog': {'items': [{'field': 'Candidate SHA', 'fieldId': 'customfield_candidate_sha',
                                          'from': None, 'to': candidate_sha}]}}
        return build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
            'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
        })

    handle_webhook_envelope(_update_envelope('abc1234'))
    handle_webhook_envelope(_update_envelope('def5678'))  # a second update — must not create a duplicate.

    assert WorkItem.objects.filter(external_key='TP-18').count() == 1
    assert WorkItemReleaseDetail.objects.filter(work_item__external_key='TP-18').count() == 1
    detail = WorkItemReleaseDetail.objects.get(work_item__external_key='TP-18')
    assert detail.candidate_sha == 'def5678', 'second update still re-syncs the (already-materialized) detail row'


def test_release_abandoned_is_recorded_and_republished(clean_db):
    """release-mode-parity.md REQ-14: a Release moved to the Abandoned
    STATUS (the resolution-based path is gone) is recorded `cancelled` and
    publishes `abandoned`."""
    project_config.set_mode(PROJECT, 'jira')
    fields = {'summary': 'Release it', 'issuetype': {'name': 'Release'}, 'project': {'name': PROJECT, 'key': 'TP'}}
    body = {'webhookEvent': 'jira:issue_updated', 'issue': {'key': 'TP-11', 'fields': fields},
            'changelog': {'items': [{'field': 'status', 'toString': 'Abandoned'}]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    item = WorkItem.objects.get(external_key='TP-11')
    event = OutboxEvent.objects.get(event_type='work_item.jira_release_event')
    assert event.payload == {'kind': 'abandoned', 'workItemId': str(item.id), 'project': item.project}, \
        'carries the materialized Release\'s canonical id and, as project, its Target Project (REQ-08)'
    assert item.status == 'cancelled'


def test_release_done_is_recorded_and_republished_exactly_once_and_the_canonical_status_follows(clean_db):
    """The regression test for REQ-08's echo-suppression: without
    store.transition_status skipping its local-mode-style publish for
    origin=JIRA_WEBHOOK, the validated transition this triggers below would
    publish a SECOND `work_item.jira_release_event`, and
    `OutboxEvent.objects.get(...)` would raise MultipleObjectsReturned."""
    project_config.set_mode(PROJECT, 'jira')
    fields = {'summary': 'Release it', 'issuetype': {'name': 'Release'}, 'project': {'name': PROJECT, 'key': 'TP'}}
    body = {'webhookEvent': 'jira:issue_updated', 'issue': {'key': 'TP-12', 'fields': fields},
            'changelog': {'items': [{'field': 'status', 'toString': 'Done'}]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    item = WorkItem.objects.get(external_key='TP-12')
    event = OutboxEvent.objects.get(event_type='work_item.jira_release_event')
    assert event.payload == {'kind': 'done', 'workItemId': str(item.id), 'project': item.project}, \
        'carries the materialized Release\'s canonical id and, as project, its Target Project (REQ-08)'
    assert item.status == 'done', \
        "_handle_changelog_item's Release/Done branch no longer returns after publishing — it also " \
        'applies the validated transition itself (REQ-08)'
    assert not WebhookFailure.objects.exists()


# REQ-08 acceptance clauses the tests above leave unproven (v5.2 audit row 25).

def _release_changelog_envelope(issue_key, changelog_items, *, fields=None):
    body = {'webhookEvent': 'jira:issue_updated',
            'issue': {'key': issue_key, 'fields': fields if fields is not None else _release_fields()},
            'changelog': {'items': changelog_items}}
    return build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })


def _create_jira_release(monkeypatch, issue_key):
    """A Jira-mode Release created through its own `jira:issue_created`
    webhook, filed under a Target Project (`engineering-app`) that differs
    from the ticket's own project (`test-project`), with no beta queue
    outstanding, so the create publishes its one `requested` event."""
    _set_release_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    project_config.set_mode('engineering-app', 'jira')
    handle_webhook_envelope(envelope_for(issue_key, 'jira:issue_created', _release_fields()))
    return WorkItem.objects.get(external_key=issue_key)


def test_a_releases_in_review_echo_cuts_no_second_candidate(clean_db, monkeypatch, permissive_jira):
    """REQ-08 — "One path cuts a Jira-mode candidate": the creation cuts it,
    and the ticket's later `proposed` to `in-review` Jira status echo is
    recorded with origin JIRA_WEBHOOK, for which `store.transition_status`
    publishes no release event. (Its Done twin is above.) Without that, the
    echo of the status the candidate cut moved the ticket to would cut a
    second one."""
    # The ticket offers In Progress, so the request's In Progress push
    # (release-mode-parity.md REQ-09) succeeds and records no failure.
    permissive_jira.routes[('GET', '/rest/api/3/issue/TP-30/transitions')] = lambda q, b: (
        200, {'transitions': [{'id': '21', 'to': {'name': 'In Progress'}}]})
    item = _create_jira_release(monkeypatch, 'TP-30')
    assert OutboxEvent.objects.filter(event_type='work_item.jira_release_event').count() == 1, \
        'the creation published its one `requested` event'

    handle_webhook_envelope(_release_changelog_envelope(
        'TP-30', [{'field': 'status', 'fromString': 'Backlog', 'toString': 'In Review'}]))

    item.refresh_from_db()
    assert item.status == 'in-review', 'the echo is recorded'
    releases = OutboxEvent.objects.filter(event_type='work_item.jira_release_event')
    assert [r.payload['kind'] for r in releases] == ['requested'], 'and cuts no second candidate'
    echo = OutboxEvent.objects.get(event_type='work_item.status_changed', work_item_id=item.id,
                                    payload__status='in-review')
    assert echo.payload['origin'] == write_gate.Origins.JIRA_WEBHOOK
    assert not WebhookFailure.objects.exists()


def test_an_unset_target_project_posts_only_its_own_comment_even_with_a_story_in_review(
        clean_db, monkeypatch, permissive_jira):
    """REQ-08 — "It runs the Target Project check first and, when that fails,
    runs no queue check, so a webhook posts at most one of the two
    comments." The Release falls back to its own project, where a story is
    awaiting acceptance, so a queue check run first (or at all) would post
    the beta-queue comment: the earlier test with no target project seeds
    nothing outstanding and cannot tell the orders apart."""
    _set_release_field_env(monkeypatch)
    outstanding_id = uuid.uuid4()
    store.create_work_item({'id': outstanding_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'Still in review', 'status': 'in-review'})
    project_config.set_mode(PROJECT, 'jira')
    assert [o.id for o in store._release_beta_queue_outstanding(PROJECT)] == [outstanding_id], \
        'precondition: the project the Release falls back to has an outstanding beta queue'

    handle_webhook_envelope(envelope_for('TP-31', 'jira:issue_created', _release_fields(target_project=None)))

    assert not OutboxEvent.objects.filter(event_type='work_item.jira_release_event').exists()
    texts = posted_comment_texts()
    assert texts == ['[system] [system] release request rejected: Target Project is not set\n\n'
                      'Resolve this and request the release again.'], \
        'exactly one comment, the Target Project one, and no beta-queue comment'


def test_the_release_events_are_published_on_the_tickets_own_projects_stream(clean_db, monkeypatch, permissive_jira):
    """REQ-08 — each publisher carries the Release's Target Project as the
    payload's `project`, while "the outbox row stays on the ticket's own
    project's stream": the `OutboxEvent.project` column, which the relay
    publishes by, is the ticket's own project and not the Target Project."""
    item = _create_jira_release(monkeypatch, 'TP-32')
    assert item.project == 'engineering-app', 'precondition: the Target Project differs from the ticket\'s own'

    handle_webhook_envelope(_release_changelog_envelope(
        'TP-32', [{'field': 'status', 'fromString': 'In Review', 'toString': 'Done'}]))
    handle_webhook_envelope(_release_changelog_envelope(
        'TP-32', [{'field': 'status', 'fromString': 'Done', 'toString': 'Abandoned'}]))

    rows = {r.payload['kind']: r for r in OutboxEvent.objects.filter(event_type='work_item.jira_release_event')}
    assert set(rows) == {'requested', 'done', 'abandoned'}
    for kind, row in rows.items():
        assert row.project == registry.normalize_project_name(PROJECT), \
            f"`{kind}`: the row is on the ticket's own project's stream"
        assert row.payload['project'] == 'engineering-app', f'`{kind}`: the payload names the Target Project'
        assert row.work_item_id == item.id
        assert row.payload['workItemId'] == str(item.id)


def test_a_release_done_is_recorded_with_origin_jira_webhook(clean_db, monkeypatch, permissive_jira):
    """REQ-08 — on `done`, `_handle_changelog_item` "applies
    `_apply_validated_status_change`, so the Release's canonical status moves
    to `done`, origin `JIRA_WEBHOOK`". The earlier Done test asserts only the
    status; this asserts the origin on the status event and the actor on the
    history row the same write recorded."""
    from workitems.models import WorkItemHistory

    item = _create_jira_release(monkeypatch, 'TP-33')

    handle_webhook_envelope(_release_changelog_envelope(
        'TP-33', [{'field': 'status', 'fromString': 'In Review', 'toString': 'Done'}]))

    item.refresh_from_db()
    assert item.status == 'done'
    changed = OutboxEvent.objects.get(event_type='work_item.status_changed', work_item_id=item.id,
                                       payload__status='done')
    assert changed.payload['origin'] == write_gate.Origins.JIRA_WEBHOOK
    history = WorkItemHistory.objects.get(work_item_id=item.id, field='status', new_value='done')
    assert history.actor == 'jira-webhook:TP-33'
    assert OutboxEvent.objects.filter(event_type='work_item.jira_release_event',
                                      payload__kind='done').count() == 1, 'and still one `done` release event'


# ---------------------------------------------------------------------------
# Catch-all — an issue-link changelog entry, previously silently
# discarded, must be durably recorded and republished.
# ---------------------------------------------------------------------------

def test_issue_link_changelog_entry_is_recorded_not_dropped(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X', 'externalKey': 'TP-13'})
    project_config.set_mode(PROJECT, 'jira')

    fields = {'issuetype': {'name': 'Task'}, 'project': {'name': PROJECT, 'key': 'TP'}}
    body = {'webhookEvent': 'jira:issue_updated', 'issue': {'key': 'TP-13', 'fields': fields},
            'changelog': {'items': [{'field': 'Link', 'toString': 'This issue blocks TP-14'}]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    event = OutboxEvent.objects.get(event_type='work_item.jira_event_received', work_item_id=item_id)
    assert event.payload['detail']['field'] == 'Link'


# ---------------------------------------------------------------------------
# v5.2 — story intake's comments, posted through the one comment path
# (canonical-delivery-state.md REQ-09, "Canonical events with a Jira side
# effect"). Driven through `handle_webhook_envelope`, the real webhook
# consumer entry point, against the fixture Jira API — not by calling the
# writer directly.
# ---------------------------------------------------------------------------

def test_story_intake_posts_v1s_acknowledgement_and_missing_fields_comments_to_jira(
        clean_db, monkeypatch, permissive_jira):
    """The accepted-with-missing-fields branch posts the acknowledgement
    THEN the missing-fields comment, both to Jira, both carrying REQ-09's
    `[<author>] ` prefix, and records neither in `core` — a Jira-mode
    comment reaches `core` on its own `comment_created` webhook."""
    _set_story_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')

    handle_webhook_envelope(envelope_for('TP-50', 'jira:issue_created', _story_fields(complete=False)))

    item = WorkItem.objects.get(external_key='TP-50')
    texts = posted_comment_texts()
    assert len(texts) == 2, texts
    assert texts[0] == '[system] Ticket received. Assigned to Refinement Agent for decomposition.'
    assert texts[1].startswith('[system] Story is missing required fields')
    assert '  - Behavior' in texts[1]
    assert 'move the ticket back to Backlog to retry' in texts[1]
    assert WorkItemComment.objects.filter(work_item_id=item.id).count() == 0, \
        'nothing is recorded in Jira mode — the comment returns on its own webhook'


def test_a_story_intake_comment_whose_post_failed_is_posted_on_redelivery_and_the_other_is_not(
        clean_db, monkeypatch, permissive_jira):
    """REQ-09's "Redelivery": story intake's comment steps are registered
    inside the handler's block and recorded complete only after they post,
    so a redelivery of the same `messageId` runs only the step that did not.
    `_handle_story_created`'s early return used to be a bare no-op."""
    _set_story_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    env = envelope_for('TP-51', 'jira:issue_created', _story_fields(complete=False))

    # Fail the SECOND comment post, so the first is recorded complete and
    # the second stays registered.
    calls = {'n': 0}
    real_post = jira_client.post_comment

    def flaky(key, text):
        calls['n'] += 1
        if calls['n'] == 2:
            raise RuntimeError('Jira is briefly unavailable')
        return real_post(key, text)

    monkeypatch.setattr(jira_client, 'post_comment', flaky)
    with pytest.raises(RuntimeError):
        handle_webhook_envelope(env)

    item = WorkItem.objects.get(external_key='TP-51')
    assert jira_writer.is_step_complete(f'{env["messageId"]}:acknowledgement', item.id, jira_writer.STEP_COMMENT)
    assert not jira_writer.is_step_complete(f'{env["messageId"]}:missing-fields', item.id, jira_writer.STEP_COMMENT)

    monkeypatch.setattr(jira_client, 'post_comment', real_post)
    handle_webhook_envelope(env)  # redelivery of the same messageId.

    texts = posted_comment_texts()
    acknowledgements = [t for t in texts if 'Ticket received' in t]
    missing = [t for t in texts if 'missing required fields' in t]
    assert len(acknowledgements) == 1, 'the completed step is skipped, not repeated'
    assert len(missing) == 1, 'the incomplete step runs'


def test_a_story_whose_key_already_has_a_row_posts_no_story_intake_comment(clean_db, monkeypatch, permissive_jira):
    """REQ-09 — "A first delivery for a Story whose key already has a row
    (a Story `connect_jira` pushed, REQ-10) registers none, so it posts no
    story-intake comment." The row exists, this `messageId` registered
    nothing, so there is nothing to resume."""
    _set_story_field_env(monkeypatch)
    project_config.set_mode(PROJECT, 'jira')
    store.create_work_item(
        {'id': uuid.uuid4(), 'project': PROJECT, 'type': 'story', 'displayName': 'Pushed by connect_jira',
         'externalKey': 'TP-52'},
        actor='connect-jira', origin=write_gate.Origins.JIRA_WEBHOOK,
    )

    handle_webhook_envelope(envelope_for('TP-52', 'jira:issue_created', _story_fields(complete=False)))

    assert posted_comment_texts() == []
    assert WorkItem.objects.filter(external_key='TP-52').count() == 1


def test_the_blocked_flag_becoming_set_records_needs_clarification(clean_db, monkeypatch, permissive_jira):
    """REQ-09 — the Blocked field BECOMING SET is a validated transition to
    `needs-clarification`, origin JIRA_WEBHOOK. Up to v5.1 nothing in the
    platform set the flag, so there was no webhook to interpret; from v5.2
    the writer sets it for every machine write of `needs-clarification`,
    `failed` or `cancelled`, which is why all three come back as
    `needs-clarification` (§4)."""
    monkeypatch.setenv('JIRA_BLOCKED_FIELD_ID', 'customfield_blocked')
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'Implement thing', 'externalKey': 'TP-53',
                             'status': 'in-progress', 'assigneeAgentId': 'backend-agent'})
    project_config.set_mode(PROJECT, 'jira')

    body = {'webhookEvent': 'jira:issue_updated',
            'issue': {'key': 'TP-53', 'fields': {'issuetype': {'name': 'Story'}}},
            'changelog': {'items': [_blocked_change(None, '10001')]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    item = store.get_work_item(item_id)
    assert item.status == 'needs-clarification'
    assert WebhookFailure.objects.filter(work_item_id=item_id).count() == 0


def test_the_blocked_flag_set_on_a_proposed_story_keeps_its_status(clean_db, monkeypatch, permissive_jira):
    """The one exception REQ-09 names: a work item in `proposed` — story
    intake's missing-fields block, which flags the ticket precisely so it
    stays `proposed` — keeps its status."""
    monkeypatch.setenv('JIRA_BLOCKED_FIELD_ID', 'customfield_blocked')
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'story',
                             'displayName': 'Incomplete story', 'externalKey': 'TP-54'})
    project_config.set_mode(PROJECT, 'jira')

    body = {'webhookEvent': 'jira:issue_updated',
            'issue': {'key': 'TP-54', 'fields': {'issuetype': {'name': 'Story'}}},
            'changelog': {'items': [_blocked_change(None, '10001')]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    assert store.get_work_item(item_id).status == 'proposed'


def test_a_comment_core_posted_keeps_its_author_on_the_round_trip(clean_db, monkeypatch, permissive_jira):
    """REQ-09, "The author survives the round trip": the writer posts
    `[<author>] <body>`, and this consumer strips that prefix back off and
    records it as the author — but only for a comment Jira says came from
    the configured integration account."""
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'A task', 'externalKey': 'TP-55'})
    project_config.set_mode(PROJECT, 'jira')

    def comment_envelope(author_name, author_email, text):
        body = {'webhookEvent': 'comment_created', 'issue': {'key': 'TP-55'},
                'comment': {'id': f'c-{author_email}', 'author': {'displayName': author_name,
                                                                   'emailAddress': author_email},
                            'body': _adf(text)}}
        return build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
            'event': 'comment_created', 'issue': body['issue'], 'body': body,
        })

    # The integration account's own comment: the prefix is the author.
    handle_webhook_envelope(comment_envelope('AI Gang Bot', 'bot@example.com', '[backend-agent] Done.'))
    recorded = WorkItemComment.objects.get(work_item_id=item_id, body='Done.')
    assert recorded.author == 'backend-agent'

    # A person's comment that happens to start with a bracketed word keeps
    # their own author and their own text.
    handle_webhook_envelope(comment_envelope('Jo Reviewer', 'jo@example.com', '[note] looks fine'))
    person = WorkItemComment.objects.get(work_item_id=item_id, author='Jo Reviewer')
    assert person.body == '[note] looks fine'


# ---------------------------------------------------------------------------
# v5.2 — the inbound status map is per project (canonical-delivery-state.md
# REQ-09, "A status write to any other status"; Shovel Ready Pass 8, answer
# 1.1). A project with `ProjectStatusConfig` rows is mapped by its rows
# ALONE: `DEFAULT_JIRA_STATUS_MAP` does not apply to it, and there is no
# passthrough of the raw Jira status name.
# ---------------------------------------------------------------------------

def _status_change_envelope(issue_key, to_status, *, issuetype='Story'):
    body = {'webhookEvent': 'jira:issue_updated',
            'issue': {'key': issue_key, 'fields': {'issuetype': {'name': issuetype},
                                                    'status': {'name': to_status}}},
            'changelog': {'items': [{'field': 'status', 'fromString': 'In Progress',
                                      'toString': to_status}]}}
    return build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })


def test_a_webhook_to_a_default_map_only_status_changes_no_canonical_status_for_a_project_with_rows(
        clean_db, permissive_jira):
    """REQ-09's Acceptance, the inbound half: "for the same project, a
    Jira-mode webhook moving an issue to a Jira status that only
    `DEFAULT_JIRA_STATUS_MAP` names changes no canonical status, and
    `jira_status_to_canonical` maps that Jira status to no canonical
    status".

    Up to v5.1 this project would have inherited the whole default map
    despite having declared its own rows, so this webhook moved the item to
    `done`.
    """
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'story',
                             'displayName': 'A story', 'externalKey': 'TP-60',
                             'status': 'in-progress', 'storyDetail': {
                                 'behavior': 'b', 'acceptanceCriteria': 'ac', 'constraints': 'c',
                                 'edgeCases': 'e', 'outOfScope': 'oos'}})
    # This project declares its own rows, and 'Done' is not one of them —
    # only DEFAULT_JIRA_STATUS_MAP names it.
    project_config.declare_custom_status(PROJECT, 'in-review', 'in-review', 'Tester Acceptance')
    project_config.set_mode(PROJECT, 'jira')

    assert webhook_consumer.jira_status_to_canonical(PROJECT, 'Done') is None
    assert webhook_consumer.DEFAULT_JIRA_STATUS_MAP['Done'] == 'done', \
        'the default map does name it — it is the project that does not'

    handle_webhook_envelope(_status_change_envelope('TP-60', 'Done'))

    assert store.get_work_item(item_id).status == 'in-progress', 'no canonical status change'
    failures = WebhookFailure.objects.filter(work_item_id=item_id)
    assert failures.count() == 1
    assert 'Done' in failures.first().reason
    assert 'not mapped' in failures.first().reason


def test_a_missing_transition_while_the_issue_sits_in_that_status_records_one_webhook_failure(
        clean_db, permissive_jira):
    """The other half of the same Acceptance clause: because
    `jira_status_to_canonical` maps that Jira status to no canonical
    status, the writer cannot count an issue sitting in it as "already in a
    status that maps to the target", so a missing transition there records
    one webhook failure rather than passing silently."""
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'A task', 'externalKey': 'TP-61'})
    project_config.declare_custom_status(PROJECT, 'in-review', 'in-review', 'Tester Acceptance')
    project_config.set_mode(PROJECT, 'jira')

    # The issue offers no transition to the project's own In Review name,
    # and is sitting in 'Done' — which this project's map does not name.
    permissive_jira.routes[('GET', '/rest/api/3/issue/TP-61/transitions')] = \
        lambda q, b: (200, {'transitions': []})
    permissive_jira.routes[('GET', '/rest/api/3/issue/TP-61')] = lambda q, b: (
        200, {'key': 'TP-61', 'fields': {'status': {'name': 'Done'}, 'customfield_10051': None}})

    store.transition_status(item_id, 'in-review', actor='jenkins',
                             origin=write_gate.Origins.DIRECT, completion_key='msg-map')

    failures = WebhookFailure.objects.filter(work_item_id=item_id)
    assert failures.count() == 1
    assert 'offers no transition' in failures.first().reason
    assert failures.first().payload['currentJiraStatus'] == 'Done'
    assert jira_writer.is_step_complete('msg-map', item_id, jira_writer.STEP_STATUS), \
        'recorded as an outcome, never retried'


def test_a_project_with_no_rows_still_uses_the_default_map(clean_db, permissive_jira):
    """The other side of the per-project rule, unchanged from v5.1: a
    project that has declared nothing is mapped by `DEFAULT_JIRA_STATUS_MAP`
    alone."""
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'A task', 'externalKey': 'TP-62',
                             'status': 'in-progress'})
    project_config.set_mode(PROJECT, 'jira')

    handle_webhook_envelope(_status_change_envelope('TP-62', 'Done', issuetype='Story'))

    assert store.get_work_item(item_id).status == 'done'
    assert WebhookFailure.objects.filter(work_item_id=item_id).count() == 0


def test_a_jira_status_no_map_names_records_a_failure_and_no_status_for_a_project_with_no_rows(
        clean_db, permissive_jira):
    """A project with no rows and a Jira status the default map does not
    name either. Up to v5.1 the raw name was passed through and
    `status_vocabulary.validate_status` rejected it one layer later, which
    also produced one failure row — so this is the same operator-visible
    outcome, with a reason that now says what is actually wrong."""
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'A task', 'externalKey': 'TP-63',
                             'status': 'in-progress'})
    project_config.set_mode(PROJECT, 'jira')

    handle_webhook_envelope(_status_change_envelope('TP-63', 'Awaiting Legal'))

    assert store.get_work_item(item_id).status == 'in-progress'
    failures = WebhookFailure.objects.filter(work_item_id=item_id)
    assert failures.count() == 1
    assert 'Awaiting Legal' in failures.first().reason


def test_clearing_the_blocked_flag_on_an_unmapped_jira_status_records_no_canonical_status(
        clean_db, monkeypatch, permissive_jira):
    """REQ-09's "When the flag is cleared, the work item first returns to
    the status its current Jira status maps to" — for an issue whose
    current Jira status the project's map does not name, there is no status
    to return to, so none is recorded and the existing blocked-cleared
    handling still runs (the side effect below)."""
    monkeypatch.setenv('JIRA_BLOCKED_FIELD_ID', 'customfield_blocked')
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'Implement thing', 'externalKey': 'TP-64',
                             'status': 'needs-clarification', 'assigneeAgentId': 'backend-agent'})
    project_config.declare_custom_status(PROJECT, 'in-review', 'in-review', 'Tester Acceptance')
    project_config.set_mode(PROJECT, 'jira')

    body = {'webhookEvent': 'jira:issue_updated',
            'issue': {'key': 'TP-64', 'fields': {'issuetype': {'name': 'Story'},
                                                  'status': {'name': 'In Progress'}}},
            'changelog': {'items': [_blocked_change('10001', None)]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    assert store.get_work_item(item_id).status == 'needs-clarification', \
        "'In Progress' is not one of this project's declared rows, so there is no status to return to"
    side_effects = OutboxEvent.objects.filter(event_type='work_item.jira_side_effect', work_item_id=item_id)
    assert [e.payload['kind'] for e in side_effects] == ['blocked_cleared'], \
        'the existing blocked-cleared handling still runs'
