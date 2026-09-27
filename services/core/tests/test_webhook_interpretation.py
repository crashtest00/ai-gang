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

from workitems import project_config, registry, store, write_gate
from workitems.envelope import Kind, build_envelope
from workitems.models import OutboxEvent, WebhookFailure, WorkItem, WorkItemComment, WorkItemReleaseDetail
from workitems.webhook_consumer import handle_webhook_envelope

PROJECT = 'test-project'

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

def test_story_created_with_complete_fields_is_dispatch_eligible_immediately(clean_db, monkeypatch):
    _set_story_field_env(monkeypatch)
    handle_webhook_envelope(envelope_for('TP-1', 'jira:issue_created', _story_fields(complete=True)))

    item = WorkItem.objects.get(external_key='TP-1')
    assert item.type == 'story'
    assert item.status == 'ready', "fields complete -> immediately dispatch-eligible"
    assert item.assignee_agent_id == 'refinement-agent'
    assert item.story_detail.behavior == 'Users can log in.'

    side_effect = OutboxEvent.objects.get(event_type='work_item.jira_side_effect', work_item_id=item.id)
    assert side_effect.payload == {'kind': 'story_intake', 'jiraIssueKey': 'TP-1', 'detail': {'ok': True, 'missing': []}}

    created_event = OutboxEvent.objects.get(event_type='work_item.created', work_item_id=item.id)
    assert created_event.payload['status'] == 'ready'


def test_story_created_missing_required_fields_stays_proposed_and_blocked(clean_db, monkeypatch):
    _set_story_field_env(monkeypatch)
    handle_webhook_envelope(envelope_for('TP-2', 'jira:issue_created', _story_fields(complete=False)))

    item = WorkItem.objects.get(external_key='TP-2')
    assert item.status == 'proposed', "must not leave 'proposed' with required fields missing"
    assert item.assignee_agent_id == 'refinement-agent', 'handlers.js sets the Agent field regardless of validation outcome'

    side_effect = OutboxEvent.objects.get(event_type='work_item.jira_side_effect', work_item_id=item.id)
    detail = side_effect.payload['detail']
    assert detail['ok'] is False
    assert detail['missing'] == ['Behavior', 'Acceptance Criteria', 'Constraints', 'Edge Cases', 'Out of Scope']


def test_story_created_is_idempotent_against_webhook_redelivery(clean_db, monkeypatch):
    _set_story_field_env(monkeypatch)
    env = envelope_for('TP-3', 'jira:issue_created', _story_fields(complete=True))
    handle_webhook_envelope(env)
    handle_webhook_envelope(env)  # redelivery — must not create a second work item.

    assert WorkItem.objects.filter(external_key='TP-3').count() == 1


def test_issue_created_of_an_unhandled_issuetype_is_recorded_not_dropped(clean_db):
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


def test_blocked_cleared_on_a_story_with_fields_now_complete_dispatches_with_full_context(clean_db, monkeypatch):
    _set_story_field_env(monkeypatch)
    monkeypatch.setenv('JIRA_BLOCKED_FIELD_ID', 'customfield_blocked')

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


def test_blocked_cleared_on_a_story_still_missing_fields_re_blocks(clean_db, monkeypatch):
    _set_story_field_env(monkeypatch)
    monkeypatch.setenv('JIRA_BLOCKED_FIELD_ID', 'customfield_blocked')

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
    assert event.payload['jiraIssueKey'] == 'TP-7'


# ---------------------------------------------------------------------------
# Comment webhooks (previously silently discarded)
# ---------------------------------------------------------------------------

def test_comment_created_webhook_is_projected_into_the_canonical_comment_thread(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X', 'externalKey': 'TP-8'})

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
    body = {'webhookEvent': 'comment_created', 'issue': {'key': 'TP-99', 'fields': {}},
            'comment': {'id': '1', 'author': {'displayName': 'X'}, 'body': _adf('hi')}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'comment_created', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)
    assert OutboxEvent.objects.filter(event_type='work_item.jira_event_received').exists()


# ---------------------------------------------------------------------------
# Handlers 5/6 — Release requested / abandoned. BF-01 (v2.1/BUGFIXES.md)
# narrowed the scope carve-out below: Django now ALSO materializes a
# canonical `release` work item and its `work_item_release_detail` row from
# the ticket's five fields (REQ-01), in addition to recording/republishing
# `work_item.jira_release_event` unchanged. The beta-queue-clean check and
# the Jenkins triggers remain ScrumMaster's — handleReleaseRequested/
# handleReleaseAbandoned are still the executors — see webhook_consumer.py's
# module docstring for why.
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


def test_release_ticket_created_materializes_a_canonical_release_work_item(clean_db, monkeypatch):
    _set_release_field_env(monkeypatch)
    fields = _release_fields()
    handle_webhook_envelope(envelope_for('TP-10', 'jira:issue_created', fields))

    event = OutboxEvent.objects.get(event_type='work_item.jira_release_event')
    assert event.payload == {'kind': 'requested', 'jiraIssueKey': 'TP-10'}, \
        'unchanged from before BF-01 — handlers.js reads Target Project/Candidate SHA off a fresh jira.getIssue() call, not this payload'

    item = WorkItem.objects.get(external_key='TP-10')
    assert item.type == 'release'
    assert item.status == 'proposed'
    assert item.project == 'engineering-app', 'Target Project custom field maps onto the work item\'s own project (REQ-01), not the ticket\'s own containing Jira project'

    detail = WorkItemReleaseDetail.objects.get(work_item_id=item.id)
    assert detail.release_notes == 'Fixes login bug.'
    assert detail.candidate_sha is None, 'automation-populated field — empty until candidate cut (REQ-01)'
    assert detail.build_identifier is None
    assert detail.preview_url is None


def test_release_ticket_created_without_target_project_field_falls_back_to_the_containing_jira_project(clean_db, monkeypatch):
    _set_release_field_env(monkeypatch)
    fields = _release_fields(target_project=None)
    handle_webhook_envelope(envelope_for('TP-14', 'jira:issue_created', fields))

    item = WorkItem.objects.get(external_key='TP-14')
    assert item.project == registry.normalize_project_name(PROJECT)


def test_release_ticket_created_is_idempotent_against_webhook_redelivery(clean_db, monkeypatch):
    _set_release_field_env(monkeypatch)
    env = envelope_for('TP-15', 'jira:issue_created', _release_fields())
    handle_webhook_envelope(env)
    handle_webhook_envelope(env)  # redelivery — must not create a second work item.

    assert WorkItem.objects.filter(external_key='TP-15').count() == 1
    assert WorkItemReleaseDetail.objects.filter(work_item__external_key='TP-15').count() == 1


def test_release_ticket_update_resyncs_candidate_fields_written_back_by_jenkins(clean_db, monkeypatch):
    _set_release_field_env(monkeypatch)
    handle_webhook_envelope(envelope_for('TP-16', 'jira:issue_created', _release_fields()))
    item = WorkItem.objects.get(external_key='TP-16')

    # The release-candidate Jenkins job writes Candidate SHA, Build
    # Identifier, and Preview URL directly onto the Jira ticket
    # (scripts/create-release-fields.sh) — an ordinary issue_updated
    # webhook the same as any other Jira field edit.
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


def test_release_ticket_update_with_no_prior_create_is_idempotent_against_a_second_update(clean_db, monkeypatch):
    _set_release_field_env(monkeypatch)

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
    fields = {'summary': 'Release it', 'issuetype': {'name': 'Release'}, 'project': {'name': PROJECT, 'key': 'TP'}}
    body = {'webhookEvent': 'jira:issue_updated', 'issue': {'key': 'TP-11', 'fields': fields},
            'changelog': {'items': [{'field': 'resolution', 'toString': 'Abandoned'}]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    event = OutboxEvent.objects.get(event_type='work_item.jira_release_event')
    assert event.payload == {'kind': 'abandoned', 'jiraIssueKey': 'TP-11'}


def test_release_done_is_recorded_and_republished(clean_db):
    fields = {'summary': 'Release it', 'issuetype': {'name': 'Release'}, 'project': {'name': PROJECT, 'key': 'TP'}}
    body = {'webhookEvent': 'jira:issue_updated', 'issue': {'key': 'TP-12', 'fields': fields},
            'changelog': {'items': [{'field': 'status', 'toString': 'Done'}]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    event = OutboxEvent.objects.get(event_type='work_item.jira_release_event')
    assert event.payload == {'kind': 'done', 'jiraIssueKey': 'TP-12'}


# ---------------------------------------------------------------------------
# Catch-all — an issue-link changelog entry, previously silently
# discarded, must be durably recorded and republished.
# ---------------------------------------------------------------------------

def test_issue_link_changelog_entry_is_recorded_not_dropped(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X', 'externalKey': 'TP-13'})

    fields = {'issuetype': {'name': 'Task'}, 'project': {'name': PROJECT, 'key': 'TP'}}
    body = {'webhookEvent': 'jira:issue_updated', 'issue': {'key': 'TP-13', 'fields': fields},
            'changelog': {'items': [{'field': 'Link', 'toString': 'This issue blocks TP-14'}]}}
    env = build_envelope(Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT), payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })
    handle_webhook_envelope(env)

    event = OutboxEvent.objects.get(event_type='work_item.jira_event_received', work_item_id=item_id)
    assert event.payload['detail']['field'] == 'Link'
