"""
canonical-delivery-state.md REQ-11, first half — "The writer executes a
Jira-mode decomposition", against a fixture Jira API.

Every test drives `materialize.materialize_decomposition`'s own entry, or
`command_consumer.handle_command` where the REQ is about the command path
(the rejection comment, the `completion_key`), never
`jira_writer.execute_decomposition` directly: REQ-11's acceptance is about
what a `materializeDecomposition` command produces, and the routing decision
is half of what is under test.
"""

from __future__ import annotations

import uuid

import pytest

from workitems import command_consumer, jira_writer, materialize, project_config, store
from workitems.models import (
    JiraDecompositionProposal, JiraWriteCompletion, WebhookFailure, WorkItem, WorkItemComment,
    WorkItemLink,
)

from tests.jira_fixture import jira_instance, permissive_jira  # noqa: F401 - pytest fixtures, used by name

PROJECT = 'test-project'

STORY_DETAIL = {
    'behavior': 'b', 'acceptanceCriteria': 'a', 'constraints': 'c',
    'edgeCases': 'e', 'outOfScope': 'o',
}


def make_parent(*, external_key='TP-1', status='ready'):
    parent_id = uuid.uuid4()
    store.create_work_item({
        'id': parent_id, 'project': PROJECT, 'type': 'story', 'displayName': 'A story',
        'status': status, 'externalKey': external_key, 'storyDetail': STORY_DETAIL,
        'assigneeAgentId': 'refinement-agent',
    })
    return parent_id


def proposal(display_name, *, agent='backend-agent', blocked_by=None, **extra):
    entry = {'id': str(uuid.uuid4()), 'displayName': display_name, 'description': f'{display_name} desc',
             'agent': agent}
    if blocked_by:
        entry['Blocked By'] = blocked_by
    entry.update(extra)
    return entry


def jira_mode(instance, project=PROJECT, key='TP'):
    project_config.set_mode(project, 'jira', jira_project_key=key)
    return instance


def run_command(parent_id, subtasks, *, message_id='command-1'):
    return command_consumer.handle_command({
        'messageId': message_id, 'project': PROJECT,
        'payload': {'command': 'materializeDecomposition', 'actor': 'refinement-agent',
                     'message': {'parentWorkItemId': str(parent_id), 'subtasks': subtasks}},
    })


# ---------------------------------------------------------------------------
# What the writer produces
# ---------------------------------------------------------------------------

def test_a_jira_mode_decomposition_produces_subtasks_links_and_shovel_ready_roots(
        clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "a decomposition whose blockers are all in the
    same command produces the Jira subtasks, Agent values, Blocks links and
    Shovel Ready roots `dependencies.js`'s Jira branch produced at
    `e39e9ab`, each written by the writer"."""
    parent = make_parent()
    jira_instance.add_issue('TP-1', issuetype='Story', status='Shovel Ready')
    jira_mode(jira_instance)
    root = proposal('Root')
    dependent = proposal('Dependent', agent='refinement-agent', blocked_by=[root['id']])

    result = run_command(parent, [root, dependent])

    assert result['posted'] is True
    assert result['workItemId'] == str(parent)
    root_key = jira_writer.proposal_record(root['id']).jira_key
    dependent_key = jira_writer.proposal_record(dependent['id']).jira_key
    created = {entry['key']: entry['fields'] for entry in jira_instance.created}
    assert created[root_key]['issuetype'] == {'name': 'Sub-task'}
    assert created[root_key]['parent'] == {'key': 'TP-1'}
    assert jira_instance.agent(root_key) == 'backend-agent'
    assert jira_instance.agent(dependent_key) == 'refinement-agent'
    assert jira_instance.links == [(root_key, dependent_key)]
    # Only the root enters Shovel Ready; the dependent is left where Jira
    # created it, and the mirror reads it back as waiting-on-dependency.
    assert jira_instance.status_of(root_key) == 'Shovel Ready'
    assert jira_instance.status_of(dependent_key) == 'Backlog'


def test_nothing_canonical_is_recorded_for_a_jira_mode_decomposition(clean_db, jira_instance):  # noqa: F811
    """The routing layer's rule (REQ-09, step 3): on `push` "It records
    nothing. The change reaches the canonical store only through Jira's
    webhook." The canonical subtasks are REQ-11's mirror's to create."""
    parent = make_parent()
    jira_instance.add_issue('TP-1', issuetype='Story', status='Shovel Ready')
    jira_mode(jira_instance)

    run_command(parent, [proposal('Root')])

    assert WorkItem.objects.filter(project=PROJECT).count() == 1, 'the parent, and nothing else'
    assert WorkItemLink.objects.count() == 0


def test_the_record_holds_each_proposals_key_and_each_blocks_pair(clean_db, jira_instance):  # noqa: F811
    """REQ-11: "`core` records each proposal's resulting Jira key
    immediately after creating it, and each Blocks pair after linking it".
    The record is what gives a mirrored Sub-task its PROPOSAL id and what
    `_unblock_dependents` counts."""
    parent = make_parent()
    jira_instance.add_issue('TP-1', issuetype='Story', status='Shovel Ready')
    jira_mode(jira_instance)
    root = proposal('Root')
    dependent = proposal('Dependent', blocked_by=[root['id']])

    run_command(parent, [root, dependent])

    rows = {str(row.proposal_id): row for row in JiraDecompositionProposal.objects.all()}
    assert set(rows) == {root['id'], dependent['id']}
    assert rows[root['id']].parent_work_item_id == parent
    assert rows[root['id']].project == PROJECT
    assert rows[root['id']].blocks_pairs == [], 'the root is nobody\'s dependent'
    pair = rows[dependent['id']].blocks_pairs[0]
    assert pair['blockerProposalId'] == root['id']
    assert pair['dependentProposalId'] == dependent['id']
    assert pair['blockerKey'] == rows[root['id']].jira_key
    assert pair['dependentKey'] == rows[dependent['id']].jira_key


def test_the_proposals_references_are_carried_on_the_record(clean_db, jira_instance, tmp_path):  # noqa: F811
    """REQ-11: the record holds "the proposal's specification and artifact
    links", because in Jira mode there is no canonical subtask to attach
    them to until the mirror creates one — "whichever of the mirror and the
    writer's key record lands second attaches" them."""
    from tests.test_work_item_references_store import make_artifact

    artifact = make_artifact()
    parent = make_parent()
    jira_instance.add_issue('TP-1', issuetype='Story', status='Shovel Ready')
    jira_mode(jira_instance)
    entry = proposal('Root', specificationLink={'artifactId': str(artifact.id), 'requirementId': 'REQ-11'},
                      artifactLinks=[str(artifact.id)])

    run_command(parent, [entry])

    row = jira_writer.proposal_record(entry['id'])
    assert row.specification_link == {'artifactId': str(artifact.id), 'requirementId': 'REQ-11'}
    assert row.artifact_links == [str(artifact.id)]


def test_a_redelivery_after_every_key_is_recorded_creates_no_second_subtask_or_link(
        clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "redelivering the command after every key is
    recorded creates no second subtask or link"."""
    parent = make_parent()
    jira_instance.add_issue('TP-1', issuetype='Story', status='Shovel Ready')
    jira_mode(jira_instance)
    root = proposal('Root')
    dependent = proposal('Dependent', blocked_by=[root['id']])

    run_command(parent, [root, dependent])
    created_before = list(jira_instance.created)
    links_before = list(jira_instance.links)

    run_command(parent, [root, dependent])

    assert jira_instance.created == created_before
    assert jira_instance.links == links_before
    assert JiraDecompositionProposal.objects.count() == 2


def test_a_redelivery_makes_no_second_root_transition(clean_db, jira_instance):  # noqa: F811
    """REQ-09's step 5 over REQ-11: the command consumer passes the
    envelope's `messageId` as the `completion_key`, so a redelivered
    command "skips any step its completion record shows done"."""
    parent = make_parent()
    jira_instance.add_issue('TP-1', issuetype='Story', status='Shovel Ready')
    jira_mode(jira_instance)
    root = proposal('Root')

    run_command(parent, [root], message_id='command-7')
    transitions_before = len([request for request in jira_instance.handler.received
                               if request['path'].endswith('/transitions') and request['method'] == 'POST'])

    run_command(parent, [root], message_id='command-7')

    assert len([request for request in jira_instance.handler.received
                if request['path'].endswith('/transitions') and request['method'] == 'POST']) == \
        transitions_before
    assert JiraWriteCompletion.objects.filter(
        completion_key='command-7', work_item_id=root['id'], step='status',
        completed_at__isnull=False,
    ).exists()


def test_a_root_whose_workflow_offers_no_shovel_ready_records_one_webhook_failure(
        clean_db, jira_instance, monkeypatch):  # noqa: F811
    """The writer's missing-transition outcome (REQ-09, "A missing
    transition is an outcome, not a success") on the decomposition's own
    root transition: recorded once, naming the item and the step, never
    retried."""
    parent = make_parent()
    jira_instance.add_issue('TP-1', issuetype='Story', status='Shovel Ready')
    jira_mode(jira_instance)

    real_create = jira_instance._create

    def create_with_a_narrow_workflow(query, body):
        status, response = real_create(query, body)
        jira_instance.issues[response['key']]['offered'] = ['Done']
        return status, response

    jira_instance.handler.routes[('POST', '/rest/api/3/issue')] = create_with_a_narrow_workflow
    root = proposal('Root')

    run_command(parent, [root])

    failure = WebhookFailure.objects.get()
    assert failure.work_item_id == parent, 'recorded against the parent, which has a canonical row'
    assert failure.payload['proposalId'] == root['id']
    assert failure.payload['targetJiraStatus'] == 'Shovel Ready'
    assert jira_writer.proposal_record(root['id']).jira_key, 'the subtask itself was still created'


# ---------------------------------------------------------------------------
# Rejections create nothing in Jira
# ---------------------------------------------------------------------------

def test_a_decomposition_rejected_for_an_agent_creates_nothing_in_jira(clean_db, jira_instance):  # noqa: F811
    """REQ-11's acceptance: "a decomposition rejected for an agent or for no
    progress creates nothing in Jira and leaves the same single rejection
    comment as the identical local-mode decomposition"."""
    parent = make_parent()
    jira_instance.add_issue('TP-1', issuetype='Story', status='Shovel Ready')
    jira_mode(jira_instance)

    with pytest.raises(materialize.MaterializationValidationError):
        run_command(parent, [proposal('Root', agent='no-such-agent')])

    assert jira_instance.created == []
    assert JiraDecompositionProposal.objects.count() == 0


def test_a_decomposition_rejected_for_no_progress_creates_nothing_in_jira(clean_db, jira_instance):  # noqa: F811
    """The no-progress rule runs "computed on the proposal graph ... before
    any Jira call", so an unresolvable `Blocked By` costs no Jira write."""
    parent = make_parent()
    jira_instance.add_issue('TP-1', issuetype='Story', status='Shovel Ready')
    jira_mode(jira_instance)

    with pytest.raises(materialize.MaterializationNoProgressError):
        run_command(parent, [proposal('Dependent', blocked_by=['not-in-this-command'])])

    assert jira_instance.created == [], 'nothing created, in a rejection'
    assert JiraDecompositionProposal.objects.count() == 0


def test_an_unresolved_artifact_sets_the_parents_blocked_flag_and_creates_nothing(
        clean_db, jira_instance):  # noqa: F811
    """REQ-09: "In Jira mode a rejection makes no Jira write other than this
    comment, and, for a decomposition rejected for an unresolved artifact,
    the parent's Blocked flag", which stands for local mode's
    `needs-clarification`."""
    parent = make_parent()
    jira_instance.add_issue('TP-1', issuetype='Story', status='Shovel Ready')
    jira_mode(jira_instance)

    with pytest.raises(materialize.MaterializationUnresolvedArtifactError):
        run_command(parent, [proposal('Root', artifactLinks=[str(uuid.uuid4())])])

    assert jira_instance.created == []
    assert jira_instance.blocked('TP-1') == {'value': 'Yes'}
    assert store.get_work_item(parent).status == 'ready', 'and no canonical status is recorded'


# ---------------------------------------------------------------------------
# Local mode is unchanged
# ---------------------------------------------------------------------------

def test_the_identical_decomposition_in_local_mode_produces_the_same_items_and_links(clean_db):
    """REQ-11's acceptance: "the identical decomposition in local mode
    produces the same canonical work items and links as today". The pass
    order now comes from `plan_decomposition`, which this is the check on."""
    parent = make_parent(external_key=None)
    root = proposal('Root')
    middle = proposal('Middle', blocked_by=[root['id']])
    last = proposal('Last', blocked_by=[middle['id'], root['id']])

    result = materialize.materialize_decomposition(
        {'parentWorkItemId': str(parent), 'subtasks': [last, middle, root]}, PROJECT,
    )

    assert set(result['idToWorkItemId']) == {root['id'], middle['id'], last['id']}
    assert store.get_work_item(root['id']).status == 'ready', 'the root alone'
    assert store.get_work_item(middle['id']).status == 'waiting-on-dependency'
    assert store.get_work_item(last['id']).status == 'waiting-on-dependency'
    links = {(str(link.from_work_item_id), str(link.to_work_item_id))
             for link in WorkItemLink.objects.all()}
    assert links == {
        (root['id'], middle['id']), (middle['id'], last['id']), (root['id'], last['id']),
    }
    assert all(item.parent_id == parent for item in WorkItem.objects.exclude(id=parent))
    assert JiraDecompositionProposal.objects.count() == 0, 'the record is a Jira-mode thing only'


def test_a_local_mode_decomposition_is_still_all_or_nothing_on_no_progress(clean_db):
    parent = make_parent(external_key=None)
    root = proposal('Root')

    with pytest.raises(materialize.MaterializationNoProgressError):
        materialize.materialize_decomposition(
            {'parentWorkItemId': str(parent),
             'subtasks': [root, proposal('Orphan', blocked_by=['nope'])]},
            PROJECT,
        )

    assert WorkItem.objects.filter(project=PROJECT).count() == 1, 'the parent alone'


def test_the_admin_origin_is_refused_in_jira_mode(clean_db, jira_instance):  # noqa: F811
    """`materialize_decomposition` is one of REQ-09's five routing
    handlers, so it calls `route` first and raises `WRITE_GATE_REJECTED` on
    `refuse` (REQ-09, step 1) — the same answer the other four give an
    `ADMIN_UI` write to a Jira-mode project."""
    from workitems import write_gate

    parent = make_parent()
    jira_mode(jira_instance)

    with pytest.raises(write_gate.WriteGateRejectedError):
        materialize.materialize_decomposition(
            {'parentWorkItemId': str(parent), 'subtasks': [proposal('Root')]}, PROJECT,
            origin=write_gate.Origins.ADMIN_UI,
        )

    assert jira_instance.created == []
    assert JiraDecompositionProposal.objects.count() == 0


def test_the_writer_attaches_the_references_when_the_mirror_landed_first(clean_db, jira_instance):  # noqa: F811
    """REQ-11: "Whichever of the mirror and the writer's key record lands
    second attaches the proposal's specification and artifact links to the
    canonical subtask; each side commits its own write before it looks for
    the other's, and both attaches are idempotent".

    The mirror-second order is the normal one and is driven end to end in
    `tests/test_subtask_mirror.py`. This is the other order, which only
    arises when a Sub-task's webhook is processed CONCURRENTLY with the
    writer's key record — a race the fixture cannot drive, because every
    call here is sequential. So the writer's side is driven against the
    state it would find: a canonical row already holding the key the writer
    is recording. (§4 records the near case this covers, a Sub-task webhook
    that beats the key record.)"""
    from workitems import readstore

    from tests.test_work_item_references_store import make_artifact

    spec = make_artifact()
    extra = make_artifact()
    parent = make_parent()
    jira_instance.add_issue('TP-1', issuetype='Story', status='Shovel Ready')
    jira_mode(jira_instance)
    entry = proposal('Root', specificationLink={'artifactId': str(spec.id), 'requirementId': 'REQ-11'},
                      artifactLinks=[str(extra.id)])
    run_command(parent, [entry])
    row = jira_writer.proposal_record(entry['id'])

    # The mirror got there first: a canonical subtask already holds the key,
    # with no reference on it.
    project_config.revert_to_local(PROJECT)
    subtask_id = uuid.uuid4()
    store.create_work_item({
        'id': subtask_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Root',
        'status': 'proposed', 'parentId': parent, 'externalKey': row.jira_key,
        'assigneeAgentId': 'backend-agent',
    })
    assert readstore.get_specification_link(subtask_id) is None

    jira_writer.attach_record_references(row)

    link = readstore.get_specification_link(subtask_id)
    assert link.artifact_id == spec.id and link.requirement_id == 'REQ-11'
    assert [l.artifact_id for l in readstore.list_artifact_links(subtask_id)] == [extra.id]

    # Idempotent, as REQ-11 requires of both sides.
    jira_writer.attach_record_references(row)
    assert len(readstore.list_artifact_links(subtask_id)) == 1
