"""
canonical-delivery-state.md REQ-09, "An assignment -> `set_agent_field`" —
the inbound half: `webhook_consumer._handle_changelog_item` MUST treat an
Agent-field changelog item as a validated assignment, origin
JIRA_WEBHOOK, read with `jira_interpret.parse_agent_field` and validated
against the agent catalog. Audit row 11 (V5.2_DOC_VS_CODE_AUDIT.md).

Every test drives `webhook_consumer.handle_webhook_envelope`, the real
consumer entry the webhook stream calls, with a `jira:issue_updated`
envelope carrying an Agent-field changelog item — never
`_handle_agent_field_change` directly, matching every other handler test
in this suite (see test_webhook_interpretation.py's and
test_subtask_mirror.py's module comments).
"""

from __future__ import annotations

import uuid

import pytest

from workitems import project_config, store, write_gate
from workitems.envelope import Kind, build_envelope
from workitems.models import OutboxEvent, WebhookFailure, WorkItem, WorkItemHistory
from workitems.webhook_consumer import handle_webhook_envelope

from tests.jira_fixture import permissive_jira  # noqa: F401 - a pytest fixture, used by name

PROJECT = 'test-project'
AGENT_FIELD_ID = 'customfield_10050'


def _set_agent_field_env(monkeypatch):
    monkeypatch.setenv('JIRA_AGENT_FIELD_ID', AGENT_FIELD_ID)


def _make_item(*, external_key, assignee='backend-agent', status='in-progress'):
    item_id = uuid.uuid4()
    store.create_work_item({
        'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Implement thing',
        'status': status, 'assigneeAgentId': assignee, 'externalKey': external_key,
    })
    return item_id


def _agent_change(from_value, to_value):
    return {'field': 'Agent', 'fieldId': AGENT_FIELD_ID, 'fromString': from_value, 'toString': to_value}


def _agent_field_value(value):
    return {'value': value} if value else None


def _envelope(issue_key, *, to_value, fields_extra=None):
    """A `jira:issue_updated` webhook carrying one Agent-field changelog
    item, with the issue's current field snapshot — `_handle_agent_field_change`
    reads the committed value from the snapshot, not from the changelog
    item's `to`/`toString` (same rule `_handle_blocked_flag_cleared_status`
    and `_sync_story_detail` already follow)."""
    fields = {
        'summary': 'Implement thing', 'issuetype': {'name': 'Task'},
        'project': {'name': PROJECT, 'key': 'TP'},
        AGENT_FIELD_ID: _agent_field_value(to_value),
    }
    if fields_extra:
        fields.update(fields_extra)
    body = {
        'webhookEvent': 'jira:issue_updated', 'issue': {'key': issue_key, 'fields': fields},
        'changelog': {'items': [_agent_change(None, to_value)]},
    }
    return build_envelope(Kind.WEBHOOK_EVENT, PROJECT, payload={
        'event': 'jira:issue_updated', 'issue': body['issue'], 'body': body,
    })


def test_a_catalog_agent_is_applied_as_a_validated_assignment_with_webhook_origin(
        clean_db, monkeypatch, permissive_jira):
    _set_agent_field_env(monkeypatch)
    item_id = _make_item(external_key='TP-100', assignee='backend-agent')
    project_config.set_mode(PROJECT, 'jira')

    handle_webhook_envelope(_envelope('TP-100', to_value='refinement-agent'))

    item = store.get_work_item(item_id)
    assert item.assignee_agent_id == 'refinement-agent'
    history = WorkItemHistory.objects.filter(work_item_id=item_id, field='assignee_agent_id').latest('occurred_at')
    assert history.old_value == 'backend-agent'
    assert history.new_value == 'refinement-agent'
    assert history.actor == 'jira-webhook:TP-100'
    event = OutboxEvent.objects.filter(work_item_id=item_id, event_type='work_item.assigned').latest('created_at')
    assert event.payload['assigneeAgentId'] == 'refinement-agent'
    assert WebhookFailure.objects.filter(work_item_id=item_id).count() == 0


def test_an_agent_outside_the_catalog_is_one_webhook_failure_and_the_assignee_is_unchanged(
        clean_db, monkeypatch, permissive_jira):
    _set_agent_field_env(monkeypatch)
    item_id = _make_item(external_key='TP-101', assignee='backend-agent')
    project_config.set_mode(PROJECT, 'jira')
    history_before = WorkItemHistory.objects.filter(work_item_id=item_id, field='assignee_agent_id').count()

    handle_webhook_envelope(_envelope('TP-101', to_value='no-such-agent'))

    item = store.get_work_item(item_id)
    assert item.assignee_agent_id == 'backend-agent', 'a rejected assignment must not be applied'
    failures = WebhookFailure.objects.filter(work_item_id=item_id)
    assert failures.count() == 1
    assert 'no-such-agent' in failures.first().reason
    assert WorkItemHistory.objects.filter(work_item_id=item_id, field='assignee_agent_id').count() == history_before
    assert OutboxEvent.objects.filter(work_item_id=item_id, event_type='work_item.assigned').count() == 0


def test_an_agent_not_permitted_for_this_project_is_one_webhook_failure(
        clean_db, monkeypatch, permissive_jira):
    """`frontend-agent` is a real catalog agent (scrummaster/test/fixtures/agents.json)
    but not one `test-project`'s own fixture permits — AGENT_NOT_AVAILABLE,
    not UNKNOWN_AGENT, and the same single-failure, not-applied outcome."""
    _set_agent_field_env(monkeypatch)
    item_id = _make_item(external_key='TP-102', assignee='backend-agent')
    project_config.set_mode(PROJECT, 'jira')

    handle_webhook_envelope(_envelope('TP-102', to_value='frontend-agent'))

    item = store.get_work_item(item_id)
    assert item.assignee_agent_id == 'backend-agent'
    failures = WebhookFailure.objects.filter(work_item_id=item_id)
    assert failures.count() == 1
    assert 'frontend-agent' in failures.first().reason


def test_a_cleared_agent_field_is_one_webhook_failure_and_the_assignee_is_unchanged(
        clean_db, monkeypatch, permissive_jira):
    _set_agent_field_env(monkeypatch)
    item_id = _make_item(external_key='TP-103', assignee='backend-agent')
    project_config.set_mode(PROJECT, 'jira')
    history_before = WorkItemHistory.objects.filter(work_item_id=item_id, field='assignee_agent_id').count()

    handle_webhook_envelope(_envelope('TP-103', to_value=None))

    item = store.get_work_item(item_id)
    assert item.assignee_agent_id == 'backend-agent'
    failures = WebhookFailure.objects.filter(work_item_id=item_id)
    assert failures.count() == 1
    assert 'cleared' in failures.first().reason
    assert WorkItemHistory.objects.filter(work_item_id=item_id, field='assignee_agent_id').count() == history_before


def test_an_agent_field_echo_of_the_current_assignee_records_and_fails_nothing(
        clean_db, monkeypatch, permissive_jira):
    """The webhook that follows `push_assignment`'s own write — or a
    person re-selecting the agent the ticket already has — must not
    double-record: no new history row, no new event, no failure."""
    _set_agent_field_env(monkeypatch)
    item_id = _make_item(external_key='TP-104', assignee='backend-agent')
    project_config.set_mode(PROJECT, 'jira')
    history_before = WorkItemHistory.objects.filter(work_item_id=item_id).count()
    events_before = OutboxEvent.objects.filter(work_item_id=item_id, event_type='work_item.assigned').count()

    handle_webhook_envelope(_envelope('TP-104', to_value='backend-agent'))

    item = store.get_work_item(item_id)
    assert item.assignee_agent_id == 'backend-agent'
    assert WorkItemHistory.objects.filter(work_item_id=item_id).count() == history_before
    assert OutboxEvent.objects.filter(work_item_id=item_id, event_type='work_item.assigned').count() == events_before
    assert WebhookFailure.objects.filter(work_item_id=item_id).count() == 0


def test_a_local_mode_projects_agent_field_webhook_is_ignored(clean_db, monkeypatch):
    """REQ-09's entry-level local-mode ignore still applies — the handler
    reads no mode of its own (`REQ-09/mode-readers`); this is
    `handle_webhook_envelope`'s existing ignore, exercised end to end."""
    _set_agent_field_env(monkeypatch)
    # No project_config.set_mode call: the project defaults to local mode.
    item_id = _make_item(external_key='TP-105', assignee='backend-agent')

    handle_webhook_envelope(_envelope('TP-105', to_value='refinement-agent'))

    item = store.get_work_item(item_id)
    assert item.assignee_agent_id == 'backend-agent', 'a local-mode project\'s webhooks are ignored, not applied'
    assert WebhookFailure.objects.filter(work_item_id=item_id).count() == 0
