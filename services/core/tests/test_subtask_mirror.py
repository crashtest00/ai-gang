"""
canonical-delivery-state.md REQ-11, second half — "Jira Sub-tasks are
mirrored into `core`", against a fixture Jira API.

Every test drives `webhook_consumer.handle_webhook_envelope`, the real
consumer entry the webhook stream calls, with the body
`tests/jira_fixture.py`'s `FakeJiraInstance` would have sent for that issue
— never the mirror's own functions. REQ-11's acceptance is about what the
fixture's webhooks yield, and the dispatch (which branch of
`_handle_issue_created` / `_handle_changelog_item` / the `issuelink_created`
branch ahead of the `issue_key` guard) is half of what is under test.
"""

from __future__ import annotations

import uuid

import pytest

from workitems import command_consumer, jira_writer, project_config, store, write_gate
from workitems.models import (
    JiraDecompositionProposal, OutboxEvent, WebhookFailure, WorkItem, WorkItemHistory, WorkItemLink,
)
from workitems.webhook_consumer import handle_webhook_envelope

from tests.jira_fixture import jira_instance, permissive_jira  # noqa: F401 - pytest fixtures, used by name

PROJECT = 'test-project'
STORY_DETAIL = {'behavior': 'b', 'acceptanceCriteria': 'a', 'constraints': 'c',
                 'edgeCases': 'e', 'outOfScope': 'o'}

_deliveries = {'n': 0}


def make_parent(instance, *, external_key='TP-1', status='ready', jira_status='Shovel Ready'):
    parent_id = uuid.uuid4()
    store.create_work_item({
        'id': parent_id, 'project': PROJECT, 'type': 'story', 'displayName': 'A story',
        'status': status, 'externalKey': external_key, 'storyDetail': STORY_DETAIL,
        'assigneeAgentId': 'refinement-agent',
    })
    instance.add_issue(external_key, issuetype='Story', status=jira_status)
    return parent_id


def jira_mode(project=PROJECT, key='TP'):
    project_config.set_mode(project, 'jira', jira_project_key=key)


def deliver(instance, key, *, event='jira:issue_created', changelog=None, message_id=None):
    """One webhook envelope for `key`, in the shape `views.jira_webhook`
    enqueues."""
    _deliveries['n'] += 1
    body = instance.issue_webhook(key, event, changelog=changelog)
    handle_webhook_envelope({
        'messageId': message_id or f'webhook-{_deliveries["n"]}', 'project': PROJECT,
        'payload': {'event': event, 'issue': body['issue'], 'body': body},
    })


def deliver_link_event(instance, blocker_key, dependent_key, *, link_id='9001', message_id=None,
                        project=PROJECT, link_type_id=None):
    """Jira's own `issuelink_created`, whose body "carries `issueLink` and
    no `issue`" — the shape that reaches the consumer ahead of its
    `issue_key` guard."""
    from tests.jira_fixture import BLOCKS_LINK_TYPE_ID

    _deliveries['n'] += 1
    handle_webhook_envelope({
        'messageId': message_id or f'link-{_deliveries["n"]}', 'project': project,
        'payload': {
            'event': 'issuelink_created', 'issue': None,
            'body': {'webhookEvent': 'issuelink_created', 'issueLink': {
                'id': link_id,
                'sourceIssueId': instance.id_of(blocker_key),
                'destinationIssueId': instance.id_of(dependent_key),
                'issueLinkType': {'id': link_type_id or BLOCKS_LINK_TYPE_ID, 'name': 'Blocks'},
            }},
        },
    })


def decompose(instance, parent_id, subtasks, *, message_id='command-1'):
    return command_consumer.handle_command({
        'messageId': message_id, 'project': PROJECT,
        'payload': {'command': 'materializeDecomposition', 'actor': 'refinement-agent',
                     'message': {'parentWorkItemId': str(parent_id), 'subtasks': subtasks}},
    })


def proposal(display_name, *, agent='backend-agent', blocked_by=None, **extra):
    entry = {'id': str(uuid.uuid4()), 'displayName': display_name,
             'description': f'{display_name} desc', 'agent': agent}
    if blocked_by:
        entry['Blocked By'] = blocked_by
    entry.update(extra)
    return entry


def key_of(proposal_entry):
    return jira_writer.proposal_record(proposal_entry['id']).jira_key


def row_for(key):
    return WorkItem.objects.filter(external_key=key).first()


def status_events(work_item_id):
    return [event.payload for event in OutboxEvent.objects.filter(
        work_item_id=work_item_id, event_type='work_item.status_changed').order_by('created_at')]


# ---------------------------------------------------------------------------
# The decomposition's own subtasks, mirrored back
# ---------------------------------------------------------------------------

def test_the_fixtures_webhooks_yield_one_subtask_and_one_link_per_jira_one(clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "the fixture's webhooks then yield one canonical
    subtask per Jira subtask and one canonical `blocks` link per Jira
    link"; "each mirrored subtask and each reconciled link records origin
    `JIRA_WEBHOOK`"; and a mirrored subtask gets "the proposal id the
    record holds for its key as its canonical id, as local mode does"."""
    parent = make_parent(jira_instance)
    jira_mode()
    root = proposal('Root')
    dependent = proposal('Dependent', blocked_by=[root['id']])
    decompose(jira_instance, parent, [root, dependent])

    deliver(jira_instance, key_of(root))
    deliver(jira_instance, key_of(dependent))

    root_row = row_for(key_of(root))
    dependent_row = row_for(key_of(dependent))
    assert str(root_row.id) == root['id'], 'the proposal id, as local mode gives it'
    assert str(dependent_row.id) == dependent['id']
    assert root_row.type == 'task' and root_row.parent_id == parent
    assert root_row.assignee_agent_id == 'backend-agent'
    assert WorkItem.objects.filter(project=PROJECT).count() == 3
    links = [(str(link.from_work_item_id), str(link.to_work_item_id))
             for link in WorkItemLink.objects.all()]
    assert links == [(root['id'], dependent['id'])]
    # Origin JIRA_WEBHOOK, visible as the actor on every row it wrote.
    assert all(row.actor.startswith('jira-webhook:')
               for row in WorkItemHistory.objects.filter(work_item_id=dependent_row.id))


def test_a_root_subtask_at_shovel_ready_is_mirrored_ready(clean_db, jira_instance):  # noqa: F811
    """"Its status maps from its Jira status" — the writer moved the root to
    Shovel Ready, so the mirror reads `ready` back off it and the item is
    dispatchable, exactly as in local mode."""
    parent = make_parent(jira_instance)
    jira_mode()
    root = proposal('Root')
    decompose(jira_instance, parent, [root])

    deliver(jira_instance, key_of(root))

    assert row_for(key_of(root)).status == 'ready'


def test_a_dependent_reaches_waiting_on_dependency_whichever_webhook_is_first(
        clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "a dependent reaches `waiting-on-dependency`
    whether its create or its link webhook (the fixture's
    `issuelink_created`, whose body carries no `issue`) is processed
    first"."""
    parent = make_parent(jira_instance)
    jira_mode()
    root = proposal('Root')
    dependent = proposal('Dependent', blocked_by=[root['id']])
    decompose(jira_instance, parent, [root, dependent])

    # Link event FIRST, before either subtask has a canonical row.
    deliver_link_event(jira_instance, key_of(root), key_of(dependent))
    assert WorkItem.objects.filter(project=PROJECT).count() == 1, 'nothing to link yet'
    assert WebhookFailure.objects.count() == 0, \
        'a link the record holds is skipped with no failure (Pass 7, SR-7-11)'

    deliver(jira_instance, key_of(dependent))
    deliver(jira_instance, key_of(root))

    dependent_row = row_for(key_of(dependent))
    assert dependent_row.status == 'waiting-on-dependency'
    assert WorkItemLink.objects.count() == 1, 'added when the second end was mirrored'


def test_a_dependent_mirrored_after_its_blocker_is_still_waiting_on_dependency(
        clean_db, jira_instance):  # noqa: F811
    """The other order: the blocker's row exists first, so the mirror reads
    the record's pair and derives `waiting-on-dependency` straight away."""
    parent = make_parent(jira_instance)
    jira_mode()
    root = proposal('Root')
    dependent = proposal('Dependent', blocked_by=[root['id']])
    decompose(jira_instance, parent, [root, dependent])

    deliver(jira_instance, key_of(root))
    deliver(jira_instance, key_of(dependent))

    assert row_for(key_of(dependent)).status == 'waiting-on-dependency'
    assert WorkItemLink.objects.count() == 1


def test_a_proposals_references_land_on_the_canonical_subtask(clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "a proposal's specification and artifact links
    are on its canonical subtask whichever lands first". Here the record
    lands first and the mirror attaches them; both attaches are
    idempotent."""
    from workitems import readstore

    from tests.test_work_item_references_store import make_artifact

    spec = make_artifact()
    extra = make_artifact()
    parent = make_parent(jira_instance)
    jira_mode()
    root = proposal('Root', specificationLink={'artifactId': str(spec.id), 'requirementId': 'REQ-11'},
                     artifactLinks=[str(extra.id)])
    decompose(jira_instance, parent, [root])

    deliver(jira_instance, key_of(root))
    deliver(jira_instance, key_of(root), event='jira:issue_updated',
            changelog=[{'field': 'summary', 'toString': 'again'}])

    link = readstore.get_specification_link(root['id'])
    assert link.artifact_id == spec.id and link.requirement_id == 'REQ-11'
    assert [l.artifact_id for l in readstore.list_artifact_links(root['id'])] == [extra.id]


# ---------------------------------------------------------------------------
# Skipping an issue whose key already has a row
# ---------------------------------------------------------------------------

def test_a_redelivered_issue_created_for_a_subtask_adds_no_second_row(clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "a redelivered `jira:issue_created` for a
    Sub-task ... adds no second canonical row"."""
    parent = make_parent(jira_instance)
    jira_mode()
    root = proposal('Root')
    decompose(jira_instance, parent, [root])

    deliver(jira_instance, key_of(root), message_id='webhook-same')
    deliver(jira_instance, key_of(root), message_id='webhook-same')

    assert WorkItem.objects.filter(external_key=key_of(root)).count() == 1


def test_a_subtask_connect_jira_already_keyed_adds_no_second_row(clean_db, jira_instance):  # noqa: F811
    """REQ-11: "skip it when one does (a redelivered create, or a Sub-task
    `connect_jira` pushed and keyed)" — the row `connect_jira` keyed is the
    row, and its own canonical id stands."""
    parent = make_parent(jira_instance)
    subtask_id = uuid.uuid4()
    store.create_work_item({
        'id': subtask_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Pushed',
        'status': 'in-progress', 'parentId': parent, 'externalKey': 'TP-50',
        'assigneeAgentId': 'backend-agent',
    })
    jira_instance.add_issue('TP-50', issuetype='Sub-task', parent='TP-1', status='In Progress')
    jira_mode()

    deliver(jira_instance, 'TP-50')

    assert WorkItem.objects.filter(external_key='TP-50').count() == 1
    assert row_for('TP-50').id == subtask_id
    assert row_for('TP-50').status == 'in-progress', 'and nothing about it is rewritten'


def test_a_subtask_first_seen_on_an_update_at_done_is_mirrored_done_and_rolls_its_parent_up(
        clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "a Sub-task first seen on a
    `jira:issue_updated` at Done is mirrored as `done` in one transaction
    and its parent's rollup runs" — the create and the transition are one
    transaction precisely so `_recompute_parent_rollup` and
    `_unblock_dependents` run for it."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    jira_instance.add_issue('TP-60', issuetype='Sub-task', parent='TP-1', status='Done',
                             fields={jira_instance.agent_field: {'value': 'backend-agent'}})

    deliver(jira_instance, 'TP-60', event='jira:issue_updated',
            changelog=[{'field': 'status', 'fromString': 'In Progress', 'toString': 'Done'}])

    subtask = row_for('TP-60')
    assert subtask is not None and subtask.status == 'done'
    # Every child done, so the parent's rollup pushed Done to Jira (Jira
    # mode records the parent's own `done` only from its webhook).
    assert jira_instance.status_of('TP-1') == 'Done'
    assert store.get_work_item(parent).status == 'in-progress', 'not recorded until its own webhook'


def test_a_subtask_with_no_parent_row_is_mirrored_parentless_with_a_failure(
        clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "a subtask whose parent has no row is mirrored
    parentless with a webhook failure"."""
    jira_mode()
    jira_instance.add_issue('TP-1', issuetype='Story', status='Shovel Ready')
    jira_instance.add_issue('TP-61', issuetype='Sub-task', parent='TP-1',
                             fields={jira_instance.agent_field: {'value': 'backend-agent'}})

    deliver(jira_instance, 'TP-61')

    subtask = row_for('TP-61')
    assert subtask is not None and subtask.parent_id is None
    failure = WebhookFailure.objects.get()
    assert 'parent issue TP-1' in failure.reason


def test_a_subtask_with_an_unknown_agent_is_mirrored_unassigned_with_a_failure(
        clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "one with an unknown Agent is mirrored
    unassigned with a webhook failure". Validated against the agent
    catalog, not against what Jira's field offers."""
    make_parent(jira_instance)
    jira_mode()
    jira_instance.add_issue('TP-62', issuetype='Sub-task', parent='TP-1',
                             fields={jira_instance.agent_field: {'value': 'no-such-agent'}})

    deliver(jira_instance, 'TP-62')

    subtask = row_for('TP-62')
    assert subtask is not None and subtask.assignee_agent_id is None
    assert 'no-such-agent' in WebhookFailure.objects.get().reason


def test_a_subtask_with_no_agent_field_is_mirrored_unassigned_with_a_failure(
        clean_db, jira_instance):  # noqa: F811
    make_parent(jira_instance)
    jira_mode()
    jira_instance.add_issue('TP-63', issuetype='Sub-task', parent='TP-1')

    deliver(jira_instance, 'TP-63')

    assert row_for('TP-63').assignee_agent_id is None
    assert 'Agent field is not set' in WebhookFailure.objects.get().reason


# ---------------------------------------------------------------------------
# Dependency progression: Jira moves first
# ---------------------------------------------------------------------------

def test_moving_a_blocker_to_done_moves_its_dependent_to_shovel_ready_then_records_ready(
        clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "moving a blocker to Done moves its dependent to
    Shovel Ready in Jira and then, from that webhook, to `ready` in `core`,
    and dispatches it". `_unblock_dependents` pushes through `route`, origin
    ROLLUP, and nothing is recorded until the dependent's own webhook
    returns."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    root = proposal('Root')
    dependent = proposal('Dependent', blocked_by=[root['id']])
    decompose(jira_instance, parent, [root, dependent])
    deliver(jira_instance, key_of(root))
    deliver(jira_instance, key_of(dependent))
    assert row_for(key_of(dependent)).status == 'waiting-on-dependency'

    # A person (or an agent's own delivery) moves the blocker to Done in Jira.
    jira_instance.issues[key_of(root)]['status'] = 'Done'
    deliver(jira_instance, key_of(root), event='jira:issue_updated',
            changelog=[{'field': 'status', 'fromString': 'Shovel Ready', 'toString': 'Done'}])

    assert jira_instance.status_of(key_of(dependent)) == 'Shovel Ready', 'pushed to Jira first'
    assert row_for(key_of(dependent)).status == 'waiting-on-dependency', \
        'and nothing recorded until its own webhook returns'

    deliver(jira_instance, key_of(dependent), event='jira:issue_updated',
            changelog=[{'field': 'status', 'fromString': 'Backlog', 'toString': 'Shovel Ready'}])

    dependent_row = row_for(key_of(dependent))
    assert dependent_row.status == 'ready'
    # The `status_changed` dispatchConsumer.js dispatches on.
    assert status_events(dependent_row.id)[-1]['status'] == 'ready'


def test_unblock_counts_the_records_pairs_with_no_canonical_link(clean_db, jira_instance):  # noqa: F811
    """REQ-11: "`_unblock_dependents` applies the same rule in every mode
    and reads no mode" — it counts "every Blocks pair the record holds",
    which is what stops a dependent being stranded when its Jira link event
    has not been processed yet (§4's "Jira-mode unblocking from the
    decomposition record" row).

    Here both subtasks have rows but the canonical `blocks` link does not
    exist — the state §4 describes, where the Jira link has not reached
    `core` yet (an unprocessed link event, or a blocker row that
    `connect_jira` keyed rather than the mirror created, so no
    record-pair link was added with it). The record is then the only thing
    that knows the dependent is waiting."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    root = proposal('Root')
    dependent = proposal('Dependent', blocked_by=[root['id']])
    decompose(jira_instance, parent, [root, dependent])
    deliver(jira_instance, key_of(root))
    deliver(jira_instance, key_of(dependent))
    WorkItemLink.objects.all().delete()
    jira_instance.links.clear()
    assert row_for(key_of(dependent)).status == 'waiting-on-dependency'

    jira_instance.issues[key_of(root)]['status'] = 'Done'
    deliver(jira_instance, key_of(root), event='jira:issue_updated',
            changelog=[{'field': 'status', 'fromString': 'Shovel Ready', 'toString': 'Done'}])

    assert jira_instance.status_of(key_of(dependent)) == 'Shovel Ready', \
        'the unblock found the dependent through the record'


def test_a_waiting_subtask_whose_unblock_push_failed_is_pushed_on_the_next_update(
        clean_db, jira_instance, monkeypatch):  # noqa: F811
    """REQ-11's acceptance: "a `waiting-on-dependency` Sub-task whose
    unblock push failed is pushed to Shovel Ready on the next
    `jira:issue_updated` for it, and recorded `ready` only from that push's
    webhook" — the one case §4 says does not stall for ever."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    root = proposal('Root')
    dependent = proposal('Dependent', blocked_by=[root['id']])
    decompose(jira_instance, parent, [root, dependent])
    deliver(jira_instance, key_of(root))
    deliver(jira_instance, key_of(dependent))

    # The unblock's push fails: recorded as one webhook failure, not retried.
    real_transition = jira_instance._transition
    jira_instance.handler.routes[('POST', f'/rest/api/3/issue/{key_of(dependent)}/transitions')] = \
        lambda q, b: (500, {'errorMessages': ['Jira is down']})
    jira_instance.issues[key_of(root)]['status'] = 'Done'
    deliver(jira_instance, key_of(root), event='jira:issue_updated',
            changelog=[{'field': 'status', 'fromString': 'Shovel Ready', 'toString': 'Done'}])

    assert jira_instance.status_of(key_of(dependent)) == 'Backlog', 'the push failed'
    assert row_for(key_of(dependent)).status == 'waiting-on-dependency'
    assert WebhookFailure.objects.filter(work_item_id=row_for(key_of(dependent)).id).exists()

    # Jira recovers, and the dependent's next update pushes it again.
    jira_instance.handler.routes[('POST', f'/rest/api/3/issue/{key_of(dependent)}/transitions')] = \
        lambda q, b: real_transition(key_of(dependent), b)
    deliver(jira_instance, key_of(dependent), event='jira:issue_updated',
            changelog=[{'field': 'summary', 'toString': 'Dependent, renamed'}])

    assert jira_instance.status_of(key_of(dependent)) == 'Shovel Ready'
    assert row_for(key_of(dependent)).status == 'waiting-on-dependency', \
        'recorded `ready` only from that push\'s own webhook'


def test_a_backlog_subtask_a_person_moved_back_keeps_the_mapped_status(clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "a subtask whose blockers are all `done`, moved
    by a person from Shovel Ready back to Backlog, keeps the status Backlog
    maps to and receives no Shovel Ready push" (Shovel Ready Pass 6,
    decision 5.1). The re-derivation acts only on a subtask that is waiting
    or newly linked, so a deliberate move back is not undone."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    root = proposal('Root')
    dependent = proposal('Dependent', blocked_by=[root['id']])
    decompose(jira_instance, parent, [root, dependent])
    deliver(jira_instance, key_of(root))
    deliver(jira_instance, key_of(dependent))
    jira_instance.issues[key_of(root)]['status'] = 'Done'
    deliver(jira_instance, key_of(root), event='jira:issue_updated',
            changelog=[{'field': 'status', 'fromString': 'Shovel Ready', 'toString': 'Done'}])
    deliver(jira_instance, key_of(dependent), event='jira:issue_updated',
            changelog=[{'field': 'status', 'fromString': 'Backlog', 'toString': 'Shovel Ready'}])
    assert row_for(key_of(dependent)).status == 'ready'

    # A person moves it back to Backlog.
    jira_instance.issues[key_of(dependent)]['status'] = 'Backlog'
    pushes_before = len([request for request in jira_instance.handler.received
                          if request['method'] == 'POST' and request['path'].endswith('/transitions')])
    deliver(jira_instance, key_of(dependent), event='jira:issue_updated',
            changelog=[{'field': 'status', 'fromString': 'Shovel Ready', 'toString': 'Backlog'}])

    assert row_for(key_of(dependent)).status == 'proposed', 'the status Backlog maps to'
    assert len([request for request in jira_instance.handler.received
                if request['method'] == 'POST' and request['path'].endswith('/transitions')]) == \
        pushes_before, 'and no Shovel Ready push'


# ---------------------------------------------------------------------------
# A link a person draws in Jira
# ---------------------------------------------------------------------------

def mirror_two_backlog_subtasks(instance, parent, *, blocker_status='Backlog'):
    """Two Sub-tasks the decomposition record does not name, as a person
    would have created them in Jira, both mirrored."""
    instance.add_issue('TP-80', issuetype='Sub-task', parent='TP-1', status=blocker_status,
                        fields={instance.agent_field: {'value': 'backend-agent'}})
    instance.add_issue('TP-81', issuetype='Sub-task', parent='TP-1', status='Backlog',
                        fields={instance.agent_field: {'value': 'backend-agent'}})
    deliver(instance, 'TP-80')
    deliver(instance, 'TP-81')
    return row_for('TP-80'), row_for('TP-81')


def test_a_person_drawn_link_whose_blocker_is_not_done_records_waiting_on_dependency(
        clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "a Blocks link a person creates in Jira between
    two mirrored Backlog subtasks the record does not name, whose blocker
    is not `done`, adds one canonical `blocks` link and records the
    dependent `waiting-on-dependency` with origin `JIRA_WEBHOOK` and no
    Jira write"."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    blocker, dependent = mirror_two_backlog_subtasks(jira_instance, parent)
    assert dependent.status == 'proposed', 'Backlog, with no blocker'
    writes_before = len([request for request in jira_instance.handler.received
                          if request['method'] in ('POST', 'PUT')])

    jira_instance.links.append(('TP-80', 'TP-81'))
    deliver_link_event(jira_instance, 'TP-80', 'TP-81')

    links = [(link.from_work_item_id, link.to_work_item_id) for link in WorkItemLink.objects.all()]
    assert links == [(blocker.id, dependent.id)]
    assert row_for('TP-81').status == 'waiting-on-dependency'
    assert status_events(dependent.id)[-1]['origin'] == write_gate.Origins.JIRA_WEBHOOK
    assert len([request for request in jira_instance.handler.received
                if request['method'] in ('POST', 'PUT')]) == writes_before, 'and no Jira write'
    assert WebhookFailure.objects.count() == 0


def test_a_person_drawn_link_whose_blocker_is_done_pushes_shovel_ready(clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "and one whose blocker is `done` pushes Shovel
    Ready through `route`, origin `ROLLUP`, after the webhook's transaction
    commits, and records `ready` only from that issue's own webhook"."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    blocker, dependent = mirror_two_backlog_subtasks(jira_instance, parent, blocker_status='Done')
    assert blocker.status == 'done'

    jira_instance.links.append(('TP-80', 'TP-81'))
    deliver_link_event(jira_instance, 'TP-80', 'TP-81')

    assert WorkItemLink.objects.count() == 1
    assert jira_instance.status_of('TP-81') == 'Shovel Ready', 'pushed'
    assert row_for('TP-81').status == 'proposed', 'and not recorded until its own webhook'

    deliver(jira_instance, 'TP-81', event='jira:issue_updated',
            changelog=[{'field': 'status', 'fromString': 'Backlog', 'toString': 'Shovel Ready'}])

    assert row_for('TP-81').status == 'ready'


def test_a_link_event_is_idempotent_with_the_issue_updated_that_follows_it(clean_db, jira_instance):  # noqa: F811
    """REQ-11: "Reconciliation is idempotent per pair, so whichever of the
    link event and an `issue_updated` arrives first does the work"."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    mirror_two_backlog_subtasks(jira_instance, parent)

    jira_instance.links.append(('TP-80', 'TP-81'))
    deliver_link_event(jira_instance, 'TP-80', 'TP-81')
    deliver(jira_instance, 'TP-81', event='jira:issue_updated',
            changelog=[{'field': 'summary', 'toString': 'renamed'}])

    assert WorkItemLink.objects.count() == 1


def test_a_reconciled_link_the_record_does_not_hold_whose_end_has_no_row_is_a_failure(
        clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "a reconciled link the record does not hold,
    whose other end has no row, records a webhook failure and adds no
    link"."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    jira_instance.add_issue('TP-80', issuetype='Sub-task', parent='TP-1',
                             fields={jira_instance.agent_field: {'value': 'backend-agent'}})
    jira_instance.add_issue('TP-90', issuetype='Sub-task', parent='TP-1',
                             fields={jira_instance.agent_field: {'value': 'backend-agent'}})
    deliver(jira_instance, 'TP-80')  # TP-90 is never mirrored.

    jira_instance.links.append(('TP-80', 'TP-90'))
    deliver_link_event(jira_instance, 'TP-80', 'TP-90')

    assert WorkItemLink.objects.count() == 0
    failure = WebhookFailure.objects.get()
    assert 'TP-90' in failure.reason
    assert failure.payload['event'] == 'blocks_link_reconciliation'


def test_a_reconciled_link_the_record_holds_whose_end_has_no_row_records_no_failure(
        clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "one the record holds, whose other end has no
    row, records no webhook failure, and its canonical link is added when
    that end is mirrored" (Pass 7, SR-7-11; the product owner's ruling,
    point 1 — a decomposition's links record no failure in local mode
    either)."""
    parent = make_parent(jira_instance)
    jira_mode()
    root = proposal('Root')
    dependent = proposal('Dependent', blocked_by=[root['id']])
    decompose(jira_instance, parent, [root, dependent])

    deliver(jira_instance, key_of(root))  # the dependent has no row yet

    assert WebhookFailure.objects.count() == 0, 'the record holds the pair'
    assert WorkItemLink.objects.count() == 0

    deliver(jira_instance, key_of(dependent))

    assert WorkItemLink.objects.count() == 1, 'added when that end was mirrored'


def test_an_issuelink_created_for_a_local_mode_project_is_ignored(clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "an `issuelink_created` for a local-mode project
    is ignored" — the same local-mode ignore every other Jira webhook gets,
    applied before the `issue_key` guard the body cannot satisfy."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    mirror_two_backlog_subtasks(jira_instance, parent)
    project_config.revert_to_local(PROJECT)
    jira_instance.links.append(('TP-80', 'TP-81'))

    deliver_link_event(jira_instance, 'TP-80', 'TP-81')

    assert WorkItemLink.objects.count() == 0
    assert WebhookFailure.objects.count() == 0
    assert OutboxEvent.objects.filter(event_type='work_item.jira_event_received').exists(), \
        'recorded, not applied, and not a failure'


def test_a_link_event_of_another_type_reconciles_nothing(clean_db, jira_instance):  # noqa: F811
    """"If `issueLink.issueLinkType.id` is the Blocks type
    (`get_blocks_link_type_id`), `core` reconciles that one pair" — a
    Relates or Duplicates link is recorded and nothing else."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    mirror_two_backlog_subtasks(jira_instance, parent)

    deliver_link_event(jira_instance, 'TP-80', 'TP-81', link_type_id='99999')

    assert WorkItemLink.objects.count() == 0
    assert WebhookFailure.objects.count() == 0


def test_a_storys_last_subtask_reaching_done_pushes_the_story_to_done(clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "moving a Story's last subtask to Done moves the
    Story to Done in Jira and then, from that webhook, to `done` in
    `core`"."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    root = proposal('Root')
    decompose(jira_instance, parent, [root])
    deliver(jira_instance, key_of(root))

    jira_instance.issues[key_of(root)]['status'] = 'Done'
    deliver(jira_instance, key_of(root), event='jira:issue_updated',
            changelog=[{'field': 'status', 'fromString': 'Shovel Ready', 'toString': 'Done'}])

    assert jira_instance.status_of('TP-1') == 'Done', 'the rollup pushed it'
    assert store.get_work_item(parent).status == 'in-progress'

    deliver(jira_instance, 'TP-1', event='jira:issue_updated',
            changelog=[{'field': 'status', 'fromString': 'In Progress', 'toString': 'Done'}])

    assert store.get_work_item(parent).status == 'done'


def test_a_project_returned_to_local_mode_still_counts_the_records_pairs(clean_db, jira_instance):  # noqa: F811
    """REQ-11: "`_unblock_dependents` applies the same rule in EVERY mode
    and reads no mode: only a Jira-mode decomposition writes the record, so
    a project that was never in Jira mode has no pair; a project returned
    to local mode by `disconnect_jira` keeps its pairs, and they still
    count" (§4).

    So the dependent of a record pair moves on its blocker's `done` after a
    disconnect too — recorded directly, because local mode records."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    root = proposal('Root')
    dependent = proposal('Dependent', blocked_by=[root['id']])
    decompose(jira_instance, parent, [root, dependent])
    deliver(jira_instance, key_of(root))
    deliver(jira_instance, key_of(dependent))
    WorkItemLink.objects.all().delete()
    dependent_id = row_for(key_of(dependent)).id

    project_config.revert_to_local(PROJECT)
    store.transition_status(key_of(root) and row_for(key_of(root)).id, 'done', actor='operator')

    assert store.get_work_item(dependent_id).status == 'ready', \
        'recorded, not pushed — local mode is the write authority again'
    assert status_events(dependent_id)[-1]['origin'] == write_gate.Origins.ROLLUP


def test_a_project_that_was_never_in_jira_mode_has_no_pair_to_count(clean_db):
    """The other half: "only a Jira-mode decomposition writes the record",
    so a local-mode project's unblock is exactly what it was before v5.2 —
    its canonical links and nothing else."""
    from workitems import materialize

    parent_id = uuid.uuid4()
    store.create_work_item({'id': parent_id, 'project': PROJECT, 'type': 'story',
                             'displayName': 'A story', 'status': 'ready',
                             'storyDetail': STORY_DETAIL, 'assigneeAgentId': 'refinement-agent'})
    root = proposal('Root')
    dependent = proposal('Dependent', blocked_by=[root['id']])
    materialize.materialize_decomposition(
        {'parentWorkItemId': str(parent_id), 'subtasks': [root, dependent]}, PROJECT,
    )
    assert JiraDecompositionProposal.objects.count() == 0

    store.transition_status(root['id'], 'done', actor='operator')

    assert store.get_work_item(dependent['id']).status == 'ready'


def test_a_subtask_jira_has_past_a_blocker_is_still_mirrored_with_a_failure(
        clean_db, jira_instance):  # noqa: F811
    """A case REQ-11 leaves open: a Sub-task whose Jira status maps to a
    canonical status the dependency gate refuses — a person moved it to
    Shovel Ready while a blocker of it is not `done`.

    The mirror goes through the inbound layer's own
    validated-write-or-record-a-failure path, so the subtask is mirrored
    (at the status it could reach) and the refusal is recorded as one
    webhook failure, rather than raising and failing the webhook message
    for ever. That is what this consumer already does for every other
    Jira-originated status it cannot apply."""
    parent = make_parent(jira_instance, status='in-progress', jira_status='In Progress')
    jira_mode()
    root = proposal('Root')
    dependent = proposal('Dependent', blocked_by=[root['id']])
    decompose(jira_instance, parent, [root, dependent])
    # The blocker is mirrored and is NOT done; a person moves the dependent
    # to Shovel Ready in Jira before the mirror has ever seen it.
    deliver(jira_instance, key_of(root))
    jira_instance.issues[key_of(dependent)]['status'] = 'Shovel Ready'

    deliver(jira_instance, key_of(dependent))

    dependent_row = row_for(key_of(dependent))
    assert dependent_row is not None, 'mirrored, not lost'
    assert dependent_row.status == 'proposed'
    failure = WebhookFailure.objects.filter(work_item_id=dependent_row.id).get()
    assert 'blocker' in failure.reason
