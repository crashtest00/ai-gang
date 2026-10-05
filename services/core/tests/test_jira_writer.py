"""
canonical-delivery-state.md REQ-09 — `core`'s outbound Jira writer, against
a fixture Jira API.

REQ-09's acceptance is explicitly "against a fixture Jira API, for a
Jira-mode project: each gated command produces the Jira state `jira.js`'s
equivalent call produced, changes no canonical row until the fixture's
webhook for that change is delivered". So every test here drives the real
enforcement point for the behaviour it claims — `store`'s routing handlers,
`command_consumer.handle_command`, or `jira_writer.handle_event_envelope` —
over a real HTTP round trip to the fixture Jira in `tests/jira_fixture.py`,
and reads the canonical side back out of the database.

The status map, the completion records and the missing-transition outcome
are the writer's own decisions rather than a pass-through, and are tested
at `jira_writer`'s own functions: there is no caller that can make a
status-map tie or a 30-day retention boundary happen on demand.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from workitems import jira_client, jira_writer, project_config, store, write_gate
from workitems.models import (
    JiraWriteCompletion, ProjectStatusConfig, WebhookFailure, WorkItem, WorkItemComment, WorkItemLink,
)
from workitems.webhook_consumer import DEFAULT_JIRA_STATUS_MAP

from tests.jira_fixture import permissive_jira  # noqa: F401 - a pytest fixture, used by name
from tests.jira_fixture import posted_comment_texts, requests_to

# The catalog fixture's one project (services/scrummaster/test/fixtures/
# projects.json), so catalog-backed assignment validation has something real
# to validate against — the same reason conftest.py points
# AGENTS_CATALOG_PATH/PROJECTS_CONFIG_PATH at those fixtures.
PROJECT = 'test-project'


def make_item(*, status='proposed', item_type='task', external_key='WT-1', project=PROJECT,
               display_name='A task', parent_id=None, assignee=None):
    item_id = uuid.uuid4()
    store.create_work_item({
        'id': item_id, 'project': project, 'type': item_type, 'displayName': display_name,
        'status': status, 'externalKey': external_key, 'parentId': parent_id,
        'assigneeAgentId': assignee,
    })
    return item_id


def jira_mode(project=PROJECT):
    project_config.set_mode(project, 'jira')


def offer_transition(permissive_jira, key, *status_names):
    permissive_jira.routes[('GET', f'/rest/api/3/issue/{key}/transitions')] = lambda q, b: (
        200, {'transitions': [{'id': str(10 + i), 'to': {'name': name}} for i, name in enumerate(status_names)]}
    )
    posted = []
    permissive_jira.routes[('POST', f'/rest/api/3/issue/{key}/transitions')] = \
        lambda q, b: (posted.append(b), (200, None))[1]
    return posted


# ---------------------------------------------------------------------------
# The status map, per project (Shovel Ready Pass 8, answer 1.1)
# ---------------------------------------------------------------------------

def test_a_project_with_no_status_rows_is_mapped_by_the_default_map(clean_db):
    assert jira_writer.canonical_to_jira_status(PROJECT, 'in-review') == 'In Review'
    assert jira_writer.canonical_to_jira_status(PROJECT, 'done') == 'Done'
    assert jira_writer.canonical_to_jira_status(PROJECT, 'ready') == 'Shovel Ready'
    # `assigned` and `waiting-on-dependency` are in no default entry, so a
    # direct write to either is rejected in Jira mode (§4, a recorded
    # limitation, not a gap).
    assert jira_writer.canonical_to_jira_status(PROJECT, 'assigned') is None
    assert jira_writer.canonical_to_jira_status(PROJECT, 'waiting-on-dependency') is None


def test_a_project_with_status_rows_is_mapped_by_its_rows_alone(clean_db):
    """REQ-09 — "a project with `ProjectStatusConfig` rows is mapped by its
    rows alone", so a canonical status only `DEFAULT_JIRA_STATUS_MAP` maps
    is UNMAPPED for it."""
    project_config.declare_custom_status(PROJECT, 'in-review', 'in-review', 'Tester Acceptance')
    assert jira_writer.canonical_to_jira_status(PROJECT, 'in-review') == 'Tester Acceptance'
    assert jira_writer.canonical_to_jira_status(PROJECT, 'done') is None, \
        'the default map does not apply to a project that has rows'


def test_a_tie_among_a_projects_rows_resolves_to_the_lowest_id_row(clean_db):
    """REQ-09's tie rule, in the direction it is reachable. The unique
    constraint on (project, status) means no two of a project's rows can
    declare the same CANONICAL status, so a tie can only arise where two
    rows name the same JIRA status — and the rule is the same either way:
    the lowest-id row wins, which is what `order_by('id')` and the first
    match give."""
    first = ProjectStatusConfig.objects.create(project=PROJECT, status='in-review',
                                                baseline_status='in-review', jira_status_name='In Review')
    ProjectStatusConfig.objects.create(project=PROJECT, status='code-review',
                                        baseline_status='in-review', jira_status_name='In Review')

    assert first.id == min(ProjectStatusConfig.objects.values_list('id', flat=True))
    assert jira_writer.jira_status_to_canonical(PROJECT, 'In Review') == 'in-review'


def test_a_canonical_status_several_default_entries_map_to_is_written_as_the_first_such_entry(
        clean_db, monkeypatch):
    """The other half of the tie rule, for a project with NO rows. The
    built-in default map has no canonical status two of its entries share,
    so this uses a fixture default map — exactly what REQ-09's acceptance
    allows ("with a fixture default map if the built one has no such
    status")."""
    from workitems import webhook_consumer

    monkeypatch.setattr(webhook_consumer, 'DEFAULT_JIRA_STATUS_MAP', {
        'Tester Acceptance': 'in-review',
        'In Review': 'in-review',
        'Done': 'done',
    })
    assert jira_writer.canonical_to_jira_status(PROJECT, 'in-review') == 'Tester Acceptance'


def test_the_forward_map_names_no_canonical_status_for_a_jira_status_the_project_does_not_declare(clean_db):
    """The forward direction, which the writer and the webhook consumer
    share — one function, in `webhook_consumer`, which this module aliases
    (REQ-09: "The writer's status map is the inverse of
    `webhook_consumer.py`'s inbound map"). A Jira status the project's map
    does not name maps to NO canonical status, in both directions and for
    both callers."""
    from workitems import webhook_consumer

    assert jira_writer.jira_status_to_canonical is not webhook_consumer.jira_status_to_canonical
    assert jira_writer.jira_status_to_canonical(PROJECT, 'In Review') == 'in-review'
    assert jira_writer.jira_status_to_canonical(PROJECT, 'Awaiting Legal') is None, \
        'no passthrough of the raw Jira status name'

    project_config.declare_custom_status(PROJECT, 'in-review', 'in-review', 'Tester Acceptance')
    assert jira_writer.jira_status_to_canonical(PROJECT, 'Tester Acceptance') == 'in-review'
    for name, mapper in (('writer', jira_writer.jira_status_to_canonical),
                         ('webhook consumer', webhook_consumer.jira_status_to_canonical)):
        assert mapper(PROJECT, 'In Review') is None, \
            f'a project with rows is mapped by its rows alone, in the {name}\'s reading too'


# ---------------------------------------------------------------------------
# Each routed write produces the Jira state jira.js's equivalent produced
# ---------------------------------------------------------------------------

def test_a_routed_status_write_transitions_the_issue_and_records_nothing(clean_db, permissive_jira):
    item_id = make_item()
    jira_mode()
    posted = offer_transition(permissive_jira, 'WT-1', 'In Review')

    result = store.transition_status(item_id, 'in-review', actor='jenkins',
                                      origin=write_gate.Origins.DIRECT)

    assert posted == [{'transition': {'id': '10'}}]
    assert result['posted'] is True
    assert result['workItemId'] == str(item_id)
    assert store.get_work_item(item_id).status == 'proposed'


def test_a_status_write_to_a_blocked_flag_status_sets_the_flag_instead(clean_db, permissive_jira):
    """REQ-09 — a status write to `needs-clarification`, `failed` or
    `cancelled` is `set_blocked_field(true)`: Jira shows all three as one
    flag (§4), which is why all three are read back as
    `needs-clarification`."""
    statuses = ('needs-clarification', 'failed', 'cancelled')
    item_ids = {status: make_item(external_key=f'WT-flag-{status}') for status in statuses}
    jira_mode()
    for status in statuses:
        store.transition_status(item_ids[status], status, actor='backend-agent',
                                 origin=write_gate.Origins.DIRECT)

        puts = requests_to('PUT', f'/issue/WT-flag-{status}')
        assert puts, f'{status} must set the Blocked field'
        assert puts[-1]['body']['fields']['customfield_10051'] == {'value': 'Yes'}
        assert requests_to('POST', f'/issue/WT-flag-{status}/transitions') == [], \
            'no transition is attempted for a flag status'


def test_a_status_write_to_an_unmapped_status_is_rejected_as_a_validation(clean_db, permissive_jira):
    item_id = make_item()
    jira_mode()

    with pytest.raises(jira_writer.JiraWriteRejectedError) as excinfo:
        store.transition_status(item_id, 'assigned', actor='backend-agent', origin=write_gate.Origins.DIRECT)

    assert excinfo.value.code == 'VALIDATION_ERROR', \
        'so the command path dead-letters it once and leaves one rejection comment'
    assert requests_to('POST', '/issue/WT-1/transitions') == [], 'no Jira call is made'


def test_a_routed_assignment_sets_the_agent_field(clean_db, permissive_jira):
    item_id = make_item()
    jira_mode()

    store.assign_work_item(item_id, 'backend-agent', actor='scrummaster',
                            origin=write_gate.Origins.DIRECT)

    puts = requests_to('PUT', '/issue/WT-1')
    assert puts[-1]['body']['fields']['customfield_10050'] == {'value': 'backend-agent'}
    assert store.get_work_item(item_id).assignee_agent_id is None, 'nothing recorded'


def test_a_routed_assignment_outside_the_catalog_is_rejected_before_any_jira_call(clean_db, permissive_jira):
    """REQ-09 step 3 — the push "runs every validation it runs in local
    mode apart from the gate", catalog assignment included."""
    item_id = make_item()
    jira_mode()

    with pytest.raises(store.AssignmentRejectedError):
        store.assign_work_item(item_id, 'not-an-agent', actor='scrummaster',
                                origin=write_gate.Origins.DIRECT)

    assert requests_to('PUT', '/issue/WT-1') == []


def test_a_routed_link_creates_the_blocks_link_unless_jira_already_shows_it(clean_db, permissive_jira):
    blocker_id = make_item(external_key='WT-B')
    dependent_id = make_item(external_key='WT-D')
    jira_mode()

    permissive_jira.routes[('GET', '/rest/api/3/issueLinkType')] = lambda q, b: (
        200, {'issueLinkTypes': [{'id': '10000', 'name': 'Blocks'}]})
    permissive_jira.routes[('GET', '/rest/api/3/issue/WT-D')] = lambda q, b: (200, {'fields': {'issuelinks': []}})

    result = store.create_link(blocker_id, dependent_id, 'blocks', actor='refinement-agent',
                                origin=write_gate.Origins.DIRECT)

    assert result['workItemId'] == str(dependent_id), "the dependent, create_link's to_work_item_id"
    created = requests_to('POST', '/issueLink')
    assert len(created) == 1
    assert created[0]['body']['outwardIssue'] == {'key': 'WT-B'}
    assert created[0]['body']['inwardIssue'] == {'key': 'WT-D'}
    assert WorkItemLink.objects.count() == 0, 'nothing recorded'

    # Jira already shows it: no second create.
    permissive_jira.routes[('GET', '/rest/api/3/issue/WT-D')] = lambda q, b: (200, {'fields': {'issuelinks': [
        {'type': {'id': '10000'}, 'inwardIssue': {'key': 'WT-B'}},
    ]}})
    store.create_link(blocker_id, dependent_id, 'blocks', actor='refinement-agent',
                       origin=write_gate.Origins.DIRECT)
    assert len(requests_to('POST', '/issueLink')) == 1


def test_a_jira_mode_create_is_refused_for_every_machine_and_person_origin(clean_db, permissive_jira):
    """REQ-09 — `create_work_item` is not routed: a Jira-mode work item is
    created IN Jira, so a create is refused on `push` as well as on
    `refuse`, leaving JIRA_WEBHOOK as the one origin that creates one."""
    jira_mode()
    for origin in (write_gate.Origins.DIRECT, write_gate.Origins.ADMIN_UI,
                   write_gate.Origins.EXTERNAL_API, write_gate.Origins.ROLLUP):
        with pytest.raises(write_gate.WriteGateRejectedError):
            store.create_work_item({'id': uuid.uuid4(), 'project': PROJECT, 'type': 'task',
                                     'displayName': 'nope'}, origin=origin)

    item = store.create_work_item({'id': uuid.uuid4(), 'project': PROJECT, 'type': 'task',
                                    'displayName': 'from Jira', 'externalKey': 'WT-NEW'},
                                   origin=write_gate.Origins.JIRA_WEBHOOK)
    assert item.external_key == 'WT-NEW'


def test_a_comment_is_posted_with_its_author_prefix_and_not_recorded(clean_db, permissive_jira):
    item_id = make_item()
    jira_mode()

    result = store.append_comment(item_id, 'backend-agent', 'Pushed the fix.')

    assert posted_comment_texts() == ['[backend-agent] Pushed the fix.']
    assert result == {'posted': True, 'workItemId': str(item_id), 'id': None}
    assert WorkItemComment.objects.filter(work_item_id=item_id).count() == 0


def test_the_comment_path_refuses_the_admin_in_jira_mode(clean_db, permissive_jira):
    item_id = make_item()
    jira_mode()

    with pytest.raises(write_gate.WriteGateRejectedError):
        store.append_comment(item_id, 'an-operator', 'by hand', origin=write_gate.Origins.ADMIN_UI)

    assert posted_comment_texts() == [], 'a person comments in Jira'


def test_a_jira_mode_work_item_with_no_external_key_records_one_webhook_failure(clean_db, permissive_jira):
    """REQ-09 "Redelivery" — "A Jira-mode work item with no `external_key`
    has no issue to write: the writer records one webhook failure naming it,
    writes nothing, and acknowledges"."""
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Keyless'})
    jira_mode()

    store.append_comment(item_id, 'backend-agent', 'anything')

    failure = WebhookFailure.objects.get(work_item_id=item_id)
    assert 'external_key' in failure.reason
    assert posted_comment_texts() == []


# ---------------------------------------------------------------------------
# A missing transition is an outcome, not a success
# ---------------------------------------------------------------------------

def test_a_missing_transition_records_one_webhook_failure_and_is_not_retried(clean_db, permissive_jira):
    item_id = make_item()
    jira_mode()
    # The issue offers no In Review transition and is in a Jira status that
    # maps to something else.
    offer_transition(permissive_jira, 'WT-1', 'Done')
    permissive_jira.routes[('GET', '/rest/api/3/issue/WT-1')] = lambda q, b: (
        200, {'key': 'WT-1', 'fields': {'status': {'name': 'In Progress'}, 'customfield_10051': None}})

    store.transition_status(item_id, 'in-review', actor='jenkins', origin=write_gate.Origins.DIRECT,
                             completion_key='msg-1')

    failures = WebhookFailure.objects.filter(work_item_id=item_id)
    assert failures.count() == 1
    assert 'In Review' in failures.first().reason
    assert jira_writer.is_step_complete('msg-1', item_id, jira_writer.STEP_STATUS), \
        'recorded complete, so it is never retried'


def test_an_issue_already_in_a_status_mapping_to_the_target_is_a_success_while_its_flag_is_clear(
        clean_db, permissive_jira):
    item_id = make_item()
    jira_mode()
    offer_transition(permissive_jira, 'WT-1', 'Done')  # no In Review on offer
    permissive_jira.routes[('GET', '/rest/api/3/issue/WT-1')] = lambda q, b: (
        200, {'key': 'WT-1', 'fields': {'status': {'name': 'In Review'}, 'customfield_10051': None}})

    store.transition_status(item_id, 'in-review', actor='jenkins', origin=write_gate.Origins.DIRECT,
                             completion_key='msg-2')

    assert WebhookFailure.objects.filter(work_item_id=item_id).count() == 0, \
        'the write it asked for has taken effect'
    assert jira_writer.is_step_complete('msg-2', item_id, jira_writer.STEP_STATUS)


def test_an_issue_already_in_the_target_status_but_flagged_records_one_webhook_failure(clean_db, permissive_jira):
    item_id = make_item()
    jira_mode()
    offer_transition(permissive_jira, 'WT-1', 'Done')
    permissive_jira.routes[('GET', '/rest/api/3/issue/WT-1')] = lambda q, b: (
        200, {'key': 'WT-1', 'fields': {'status': {'name': 'In Review'},
                                         'customfield_10051': {'value': 'Yes'}}})

    store.transition_status(item_id, 'in-review', actor='jenkins', origin=write_gate.Origins.DIRECT)

    assert WebhookFailure.objects.filter(work_item_id=item_id).count() == 1


# ---------------------------------------------------------------------------
# Derived writes go through the router too
# ---------------------------------------------------------------------------

def test_a_parents_rollup_is_pushed_after_the_webhooks_transaction_commits(clean_db, permissive_jira):
    """REQ-09 — in Jira mode a Story whose last subtask is recorded `done`
    is TRANSITIONED in Jira through `transaction.on_commit`, and recorded
    `done` only from the Story's own webhook."""
    parent_id = make_item(external_key='WT-P', display_name='Parent')
    child_id = make_item(external_key='WT-C', display_name='Child', parent_id=parent_id)
    jira_mode()
    posted = offer_transition(permissive_jira, 'WT-P', 'Done')

    # The child's own `done` arrives as a validated Jira webhook, so it is
    # RECORDED; the rollup it triggers is PUSHED.
    store.transition_status(child_id, 'done', actor='jira-webhook:WT-C',
                             origin=write_gate.Origins.JIRA_WEBHOOK)

    assert store.get_work_item(child_id).status == 'done'
    assert store.get_work_item(parent_id).status == 'proposed', \
        "the parent's done comes back on its own webhook"
    assert posted == [{'transition': {'id': '10'}}]


def test_a_dependents_unblock_is_pushed_and_two_pushes_from_one_webhook_leave_no_failure(
        clean_db, permissive_jira):
    """REQ-09's acceptance: "two pushes of the same status for one unflagged
    dependent from one webhook leave one Shovel Ready and no webhook
    failure"."""
    blocker_id = make_item(external_key='WT-BL', display_name='Blocker')
    dependent_id = make_item(external_key='WT-DE', display_name='Dependent',
                              status='waiting-on-dependency')
    store.create_link(blocker_id, dependent_id, 'blocks', actor='refinement-agent')
    jira_mode()

    # Jira offers the transition the first time and, as a workflow with no
    # self-transition would, not the second; the issue is then already in
    # Shovel Ready with its flag clear, which the writer counts as done.
    calls = {'n': 0}

    def transitions(query, body):
        calls['n'] += 1
        if calls['n'] == 1:
            return 200, {'transitions': [{'id': '10', 'to': {'name': 'Shovel Ready'}}]}
        return 200, {'transitions': []}

    permissive_jira.routes[('GET', '/rest/api/3/issue/WT-DE/transitions')] = transitions
    permissive_jira.routes[('POST', '/rest/api/3/issue/WT-DE/transitions')] = lambda q, b: (200, None)
    permissive_jira.routes[('GET', '/rest/api/3/issue/WT-DE')] = lambda q, b: (
        200, {'key': 'WT-DE', 'fields': {'status': {'name': 'Shovel Ready'}, 'customfield_10051': None}})

    store.transition_status(blocker_id, 'done', actor='jira-webhook:WT-BL',
                            origin=write_gate.Origins.JIRA_WEBHOOK)
    jira_writer.register_derived_status_push(store.get_work_item(dependent_id), 'ready')

    assert store.get_work_item(dependent_id).status == 'waiting-on-dependency', \
        "recorded only from the dependent's own webhook"
    assert WebhookFailure.objects.filter(work_item_id=dependent_id).count() == 0


def test_a_derived_push_that_fails_records_one_webhook_failure_and_leaves_the_triggering_status(
        clean_db, permissive_jira, monkeypatch):
    """REQ-09 — a push registered on commit cannot fail its caller's
    message, so its failure is recorded as one webhook failure naming the
    item and the pushed status, it raises nothing, and the triggering status
    stays recorded."""
    parent_id = make_item(external_key='WT-P2', display_name='Parent')
    child_id = make_item(external_key='WT-C2', display_name='Child', parent_id=parent_id)
    jira_mode()

    def explode(key, status_name):
        raise RuntimeError('Jira is down')

    monkeypatch.setattr(jira_client, 'transition_issue', explode)

    store.transition_status(child_id, 'done', actor='jira-webhook:WT-C2',
                             origin=write_gate.Origins.JIRA_WEBHOOK)

    assert store.get_work_item(child_id).status == 'done', 'the triggering status stays recorded'
    failure = WebhookFailure.objects.get(work_item_id=parent_id)
    assert 'derived push' in failure.reason
    assert 'done' in failure.reason


def test_a_derived_write_in_local_mode_carries_rollup_origin_and_a_system_actor(clean_db):
    """The local-mode half: REQ-09 requires every
    `work_item.status_changed` to carry its write's origin, and a derived
    write recorded in local mode to carry ROLLUP and a `system:` actor.
    Before v5.2 the event carried no origin and inherited the triggering
    actor."""
    from workitems.models import OutboxEvent, WorkItemHistory

    parent_id = make_item(external_key=None, display_name='Parent')
    child_id = make_item(external_key=None, display_name='Child', parent_id=parent_id)

    store.transition_status(child_id, 'done', actor='a-person')

    rollup = OutboxEvent.objects.get(event_type='work_item.status_changed', work_item_id=parent_id)
    assert rollup.payload['origin'] == write_gate.Origins.ROLLUP
    assert rollup.payload['status'] == 'done'
    history = WorkItemHistory.objects.filter(work_item_id=parent_id, field='status').order_by('occurred_at').last()
    assert history.actor == 'system:rollup'

    triggering = OutboxEvent.objects.filter(
        event_type='work_item.status_changed', work_item_id=child_id).first()
    assert triggering.payload['origin'] == write_gate.Origins.DIRECT


# ---------------------------------------------------------------------------
# Canonical events with a Jira side effect
# ---------------------------------------------------------------------------

def _event_envelope(event_type, project, work_item_id, data, message_id='msg-event-1'):
    return {
        'schemaVersion': '1', 'messageId': message_id, 'kind': 'work_item_event',
        'project': project, 'createdAt': timezone.now().isoformat(),
        'payload': {'eventType': event_type, 'workItemId': str(work_item_id) if work_item_id else None,
                     'data': data},
    }


def test_a_release_candidate_recorded_event_sets_the_three_fields_and_transitions(clean_db, permissive_jira, monkeypatch):
    for name, field_id in (('JIRA_CANDIDATE_SHA_FIELD_ID', 'cf_sha'),
                            ('JIRA_BUILD_IDENTIFIER_FIELD_ID', 'cf_build'),
                            ('JIRA_PREVIEW_URL_FIELD_ID', 'cf_preview')):
        monkeypatch.setenv(name, field_id)

    item_id = make_item(item_type='release', external_key='WT-R', display_name='Release')
    jira_mode()
    posted = offer_transition(permissive_jira, 'WT-R', 'In Review')

    jira_writer.handle_event_envelope(_event_envelope(
        'work_item.release_candidate_recorded', PROJECT, item_id,
        {'id': str(item_id), 'candidateSha': 'abc1234', 'buildIdentifier': 'b-7',
         'previewUrl': 'https://preview.example.com'},
    ))

    fields = {}
    for request in requests_to('PUT', '/issue/WT-R'):
        fields.update(request['body']['fields'])
    assert fields == {'cf_sha': 'abc1234', 'cf_build': 'b-7', 'cf_preview': 'https://preview.example.com'}
    assert posted == [{'transition': {'id': '10'}}]


def test_a_status_changed_event_makes_no_jira_write(clean_db, permissive_jira):
    item_id = make_item()
    jira_mode()

    jira_writer.handle_event_envelope(_event_envelope(
        'work_item.status_changed', PROJECT, item_id,
        {'id': str(item_id), 'status': 'in-review', 'previous': 'in-progress',
         'origin': write_gate.Origins.JIRA_WEBHOOK},
    ))

    assert permissive_jira.received == [], 'in Jira mode a status is recorded only from Jira'


def test_a_story_intake_side_effect_sets_the_agent_field_and_the_blocked_flag(clean_db, permissive_jira, redis_client):
    """The envelope is the real producer's, end to end: the webhook consumer
    writes the `story_intake` OutboxEvent for a Story created in Jira, the
    relay publishes it, and the writer is fed the stream entry. A hand-built
    envelope here once carried `externalKey` while the producer still emitted
    `jiraIssueKey` (REQ-09, v5.2 audit row 21)."""
    import json

    from workitems import registry
    from workitems.envelope import Kind, build_envelope
    from workitems.relay import relay_once
    from workitems.stream_topology import event_stream_name
    from workitems.webhook_consumer import handle_webhook_envelope

    jira_mode()
    body = {'webhookEvent': 'jira:issue_created', 'issue': {'key': 'WT-S', 'fields': {
        'summary': 'Story', 'issuetype': {'name': 'Story'}, 'project': {'name': PROJECT, 'key': 'WT'},
    }}}
    handle_webhook_envelope(build_envelope(
        Kind.WEBHOOK_EVENT, registry.normalize_project_name(PROJECT),
        payload={'event': 'jira:issue_created', 'issue': body['issue'], 'body': body},
    ))
    item = WorkItem.objects.get(external_key='WT-S')

    relay_once(redis_client)
    entries = [json.loads(fields['data']) for _, fields in redis_client.xrange(event_stream_name(PROJECT), '-', '+')]
    side_effects = [e for e in entries if e['payload']['eventType'] == 'work_item.jira_side_effect'
                    and e['payload']['workItemId'] == str(item.id)]
    assert len(side_effects) == 1
    assert side_effects[0]['payload']['data']['externalKey'] == 'WT-S'
    assert 'jiraIssueKey' not in side_effects[0]['payload']['data']
    assert side_effects[0]['payload']['data']['detail']['ok'] is False

    permissive_jira.received.clear()  # the webhook's own comment posts are not this writer's.
    jira_writer.handle_event_envelope(side_effects[0])

    fields = {}
    for request in requests_to('PUT', '/issue/WT-S'):
        fields.update(request['body']['fields'])
    assert fields['customfield_10050'] == {'value': 'refinement-agent'}
    assert fields['customfield_10051'] == {'value': 'Yes'}


def test_an_accepted_story_intake_side_effect_sets_no_blocked_flag(clean_db, permissive_jira):
    item_id = make_item(external_key='WT-S2', item_type='story', display_name='Story')
    jira_mode()

    jira_writer.handle_event_envelope(_event_envelope(
        'work_item.jira_side_effect', PROJECT, item_id,
        {'kind': 'story_intake', 'externalKey': 'WT-S2', 'detail': {'ok': True, 'missing': []}},
    ))

    fields = {}
    for request in requests_to('PUT', '/issue/WT-S2'):
        fields.update(request['body']['fields'])
    assert fields == {'customfield_10050': {'value': 'refinement-agent'}}


def test_a_local_mode_projects_events_make_no_jira_call(clean_db, permissive_jira):
    item_id = make_item(external_key='WT-L')

    jira_writer.handle_event_envelope(_event_envelope(
        'work_item.jira_side_effect', PROJECT, item_id,
        {'kind': 'story_intake', 'externalKey': 'WT-L', 'detail': {'ok': True, 'missing': []}},
    ))

    assert permissive_jira.received == []


def test_the_writers_consumer_group_is_created_at_the_streams_end(clean_db, redis_client, redis_factory):
    """REQ-09 — the group is created at the stream's END, unlike
    `streams.ensure_group`'s default of its start, so nothing published
    before the writer existed is written (v5.1 audit SR-5-02)."""
    from workitems.stream_topology import event_stream_name

    stream = event_stream_name(PROJECT)
    redis_client.xadd(stream, {'data': 'published before the writer existed'})

    consumer = jira_writer.create_writer_consumer(redis_factory, PROJECT, consumer_name='writer-test')
    try:
        groups = {g['name']: g for g in redis_client.xinfo_groups(stream)}
        assert jira_writer.WRITER_GROUP in groups
        assert groups[jira_writer.WRITER_GROUP]['entries-read'] in (1, None) or \
            groups[jira_writer.WRITER_GROUP]['lag'] == 0, groups[jira_writer.WRITER_GROUP]
    finally:
        consumer.stop()


# ---------------------------------------------------------------------------
# Completion records
# ---------------------------------------------------------------------------

def test_a_registered_step_is_not_complete_and_is_not_skipped(clean_db):
    item_id = make_item(external_key=None)
    key = 'msg-reg:acknowledgement'
    jira_writer.register_step(key, item_id, jira_writer.STEP_COMMENT)

    assert jira_writer.is_step_complete(key, item_id, jira_writer.STEP_COMMENT) is False
    assert jira_writer.pending_comment_steps('msg-reg', item_id) == ['acknowledgement']

    jira_writer.mark_step_complete(key, item_id, jira_writer.STEP_COMMENT)
    assert jira_writer.is_step_complete(key, item_id, jira_writer.STEP_COMMENT) is True
    assert jira_writer.pending_comment_steps('msg-reg', item_id) == []


def test_a_step_with_no_key_records_no_completion_and_is_never_skipped(clean_db):
    item_id = make_item(external_key=None)
    jira_writer.mark_step_complete(None, item_id, jira_writer.STEP_STATUS)
    assert JiraWriteCompletion.objects.count() == 0
    assert jira_writer.is_step_complete(None, item_id, jira_writer.STEP_STATUS) is False


def test_completion_records_younger_than_thirty_days_are_kept(clean_db):
    item_id = make_item(external_key=None)
    jira_writer.mark_step_complete('recent', item_id, jira_writer.STEP_STATUS)
    jira_writer.mark_step_complete('old', item_id, jira_writer.STEP_COMMENT)
    JiraWriteCompletion.objects.filter(completion_key='old').update(
        registered_at=timezone.now() - timedelta(days=JiraWriteCompletion.RETENTION_DAYS + 1))

    removed = jira_writer.prune_completion_records()

    assert removed == 1
    assert list(JiraWriteCompletion.objects.values_list('completion_key', flat=True)) == ['recent']


def test_the_default_map_is_the_one_the_inbound_consumer_uses(clean_db):
    """Gate 4's shape, applied to a map rather than a doc: the writer's map
    is stated as the INVERSE of `webhook_consumer.py`'s, so it has to be
    computed from that one and not a second copy of it."""
    for jira_status, canonical in DEFAULT_JIRA_STATUS_MAP.items():
        assert jira_writer.jira_status_to_canonical(PROJECT, jira_status) == canonical
        assert jira_writer.canonical_to_jira_status(PROJECT, canonical) is not None


# ---------------------------------------------------------------------------
# No Jira call inside an open transaction
# ---------------------------------------------------------------------------

def test_a_push_inside_an_open_transaction_runs_only_after_it_commits(clean_db, permissive_jira):
    """REQ-09 step 3 — "Called inside a caller's transaction
    (`connection.in_atomic_block`), the handler registers that call with
    `transaction.on_commit` and returns at once." So a Jira call is never
    made inside an open transaction: the caller keeps its own ordering, and
    a rollback drops the call entirely."""
    from django.db import transaction

    item_id = make_item(external_key='WT-TX')
    jira_mode()

    with transaction.atomic():
        result = store.append_comment(item_id, 'backend-agent', 'inside a transaction')
        assert result == {'posted': True, 'workItemId': str(item_id), 'id': None}
        assert posted_comment_texts() == [], 'no Jira call while the transaction is open'

    assert posted_comment_texts() == ['[backend-agent] inside a transaction']


def test_a_push_registered_inside_a_transaction_that_rolls_back_is_never_made(clean_db, permissive_jira):
    from django.db import transaction

    item_id = make_item(external_key='WT-RB')
    jira_mode()

    class Rollback(Exception):
        pass

    with pytest.raises(Rollback):
        with transaction.atomic():
            store.append_comment(item_id, 'backend-agent', 'should never reach Jira')
            raise Rollback()

    assert posted_comment_texts() == []


def test_an_append_comment_republished_with_the_same_source_message_id_posts_once(clean_db, permissive_jira):
    """REQ-09's acceptance: "an `appendComment` republished with the same
    `sourceMessageId` under a new `messageId` leaves one Jira comment in
    Jira mode and one row in local mode." ScrumMaster's gateway sets
    `sourceMessageId` to its own entry's id, so a redelivered gateway entry
    published as a NEW command still writes one comment. In Jira mode
    nothing is recorded, so that id is the writer's completion key
    instead of the row's."""
    item_id = make_item(external_key='WT-DEDUPE')
    jira_mode()

    store.append_comment(item_id, 'backend-agent', 'Progress.', source_message_id='gw-entry-1')
    store.append_comment(item_id, 'backend-agent', 'Progress.', source_message_id='gw-entry-1')

    assert posted_comment_texts() == ['[backend-agent] Progress.']
    assert JiraWriteCompletion.objects.filter(
        completion_key='gw-entry-1', work_item_id=item_id, step=jira_writer.STEP_COMMENT,
        completed_at__isnull=False,
    ).count() == 1


def test_the_same_republished_comment_leaves_one_row_in_local_mode(clean_db):
    item_id = make_item(external_key=None)

    first = store.append_comment(item_id, 'backend-agent', 'Progress.', source_message_id='gw-entry-2')
    second = store.append_comment(item_id, 'backend-agent', 'Progress.', source_message_id='gw-entry-2')

    assert first['id'] == second['id']
    assert WorkItemComment.objects.filter(work_item_id=item_id).count() == 1
