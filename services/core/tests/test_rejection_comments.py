"""
canonical-delivery-state.md REQ-09, "Rejections, in every mode".

A command `core` rejects for a validation is dead-lettered as permanent and
leaves exactly ONE comment on its work item — for a decomposition, on the
parent — in every mode, through the one comment path, keyed
`<messageId>:rejection` so a redelivery adds none. The comment is
`[system] <command> rejected: <the error's message>` followed by the detail
the error carries.

Driven through `command_consumer._handler`, the function the Streams consumer
calls, so what is under test is the handler's behaviour after a permanent
rejection rather than a helper computing a string. The release gate keeps
today's comment, appended inside `transition_status` itself, and the handler
adds no second one for it — tested here too, because the two rules only make
sense together.
"""

from __future__ import annotations

import uuid

import pytest

from workitems import command_consumer, project_config, store, write_gate
from workitems.envelope import Kind, build_envelope
from workitems.models import WorkItemComment

from tests.jira_fixture import permissive_jira  # noqa: F401 - a pytest fixture, used by name
from tests.jira_fixture import posted_comment_texts

PROJECT = 'test-project'
STORY_DETAIL = {'behavior': 'b', 'acceptanceCriteria': 'ac', 'constraints': 'c',
                 'edgeCases': 'e', 'outOfScope': 'oos'}


def command(payload, project=PROJECT, message_id=None):
    envelope = build_envelope(Kind.WORK_ITEM_COMMAND, project, payload=payload)
    if message_id:
        envelope['messageId'] = message_id
    return envelope


def bodies_for(work_item_id):
    return list(WorkItemComment.objects.filter(work_item_id=work_item_id)
                 .order_by('created_at').values_list('body', flat=True))


def test_a_rejected_assignment_leaves_one_comment_naming_the_permitted_agents(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})

    envelope = command({'command': 'assign', 'actor': 'scrummaster',
                         'workItemId': str(item_id), 'agentId': 'not-an-agent'})
    with pytest.raises(store.AssignmentRejectedError):
        command_consumer._handler(envelope)

    bodies = bodies_for(item_id)
    assert len(bodies) == 1
    assert bodies[0].startswith('[system] assign rejected: ')
    assert 'not-an-agent' in bodies[0]
    assert 'Permitted agents for this project: refinement-agent, backend-agent' in bodies[0]

    comment = WorkItemComment.objects.get(work_item_id=item_id)
    assert comment.source_message_id == f'{envelope["messageId"]}:rejection'


def test_a_redelivered_rejection_adds_no_second_comment(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})
    envelope = command({'command': 'assign', 'actor': 'scrummaster',
                         'workItemId': str(item_id), 'agentId': 'not-an-agent'})

    for _ in range(2):
        with pytest.raises(store.AssignmentRejectedError):
            command_consumer._handler(envelope)

    assert len(bodies_for(item_id)) == 1


def test_a_dependency_gate_rejection_names_the_incomplete_blockers(clean_db):
    blocker_id = uuid.uuid4()
    dependent_id = uuid.uuid4()
    store.create_work_item({'id': blocker_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Blocker'})
    store.create_work_item({'id': dependent_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'Dependent', 'status': 'waiting-on-dependency'})
    store.create_link(blocker_id, dependent_id, 'blocks', actor='tester')

    envelope = command({'command': 'transitionStatus', 'actor': 'scrummaster',
                         'workItemId': str(dependent_id), 'status': 'ready'})
    with pytest.raises(store.DependencyGateError):
        command_consumer._handler(envelope)

    bodies = bodies_for(dependent_id)
    assert len(bodies) == 1
    assert bodies[0].startswith('[system] transitionStatus rejected: ')
    assert f'Blockers not yet done: {blocker_id}' in bodies[0]


def test_a_decompositions_rejection_lands_on_the_parent(clean_db):
    parent_id = uuid.uuid4()
    store.create_work_item({'id': parent_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Parent'})

    envelope = command({'command': 'materializeDecomposition', 'actor': 'refinement-agent',
                         'message': {'parentWorkItemId': str(parent_id), 'subtasks': [
                             {'id': str(uuid.uuid4()), 'displayName': 'Nope', 'agent': 'ghost-agent',
                              'Blocked By': []},
                         ]}})
    with pytest.raises(Exception):
        command_consumer._handler(envelope)

    bodies = bodies_for(parent_id)
    assert len(bodies) == 1
    assert bodies[0].startswith('[system] materializeDecomposition rejected: ')
    assert 'ghost-agent' in bodies[0]
    assert 'Permitted agents for this project:' in bodies[0]


def test_a_rejection_whose_work_item_does_not_exist_leaves_no_comment(clean_db):
    envelope = command({'command': 'transitionStatus', 'actor': 'scrummaster',
                         'workItemId': str(uuid.uuid4()), 'status': 'ready'})
    with pytest.raises(store.ValidationError):
        command_consumer._handler(envelope)

    assert WorkItemComment.objects.count() == 0


def test_the_release_gate_keeps_its_own_comment_and_the_handler_adds_no_second(clean_db):
    release_id = uuid.uuid4()
    outstanding_id = uuid.uuid4()
    store.create_work_item({'id': release_id, 'project': PROJECT, 'type': 'release', 'displayName': 'Release'})
    store.create_work_item({'id': outstanding_id, 'project': PROJECT, 'type': 'story',
                             'displayName': 'Outstanding', 'status': 'in-review', 'storyDetail': STORY_DETAIL})

    envelope = command({'command': 'transitionStatus', 'actor': 'scrummaster',
                         'workItemId': str(release_id), 'status': 'in-review'})
    with pytest.raises(store.ReleaseGateError):
        command_consumer._handler(envelope)

    bodies = bodies_for(release_id)
    assert len(bodies) == 1, bodies
    assert bodies[0].startswith('Cannot cut a release candidate'), \
        "today's text, in today's place — the handler adds nothing for it"


def test_a_release_gate_rejection_redelivered_with_the_same_completion_key_leaves_one_comment(clean_db):
    """REQ-09's acceptance: "a `transitionStatus` rejected by the release
    gate and redelivered with the same `completion_key` leaves one
    release-gate comment, keyed `<completion_key>:release-gate`, in either
    mode." The key is the envelope's own `messageId`, which
    `handle_command` threads in as the `completion_key`."""
    release_id = uuid.uuid4()
    outstanding_id = uuid.uuid4()
    store.create_work_item({'id': release_id, 'project': PROJECT, 'type': 'release', 'displayName': 'Release'})
    store.create_work_item({'id': outstanding_id, 'project': PROJECT, 'type': 'story',
                             'displayName': 'Outstanding', 'status': 'in-review', 'storyDetail': STORY_DETAIL})

    envelope = command({'command': 'transitionStatus', 'actor': 'scrummaster',
                         'workItemId': str(release_id), 'status': 'in-review'})
    for _ in range(2):
        with pytest.raises(store.ReleaseGateError):
            command_consumer._handler(envelope)

    assert len(bodies_for(release_id)) == 1
    assert WorkItemComment.objects.get(work_item_id=release_id).source_message_id == \
        f'{envelope["messageId"]}:release-gate'


def test_the_admin_and_the_external_api_get_their_error_and_no_comment(clean_db):
    """REQ-09 — "An admin or external-API rejection returns its error and
    leaves no comment, except the external API's release-gate rejection,
    whose comment `transition_status` appends." Neither reaches the command
    consumer's handler, which is the one place the rejection comment is
    written."""
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})

    with pytest.raises(store.AssignmentRejectedError):
        store.assign_work_item(item_id, 'not-an-agent', actor='an-operator',
                                origin=write_gate.Origins.ADMIN_UI)
    with pytest.raises(store.AssignmentRejectedError):
        store.assign_work_item(item_id, 'not-an-agent', actor='external-api',
                                origin=write_gate.Origins.EXTERNAL_API)

    assert WorkItemComment.objects.count() == 0


def test_in_jira_mode_a_rejection_leaves_the_same_single_comment_in_jira_and_no_row(clean_db, permissive_jira):
    """REQ-09's acceptance: "a gated command failing a validation makes no
    Jira write but its comment … and leaves the same single rejection
    comment it leaves in local mode"."""
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'X', 'externalKey': 'RC-1'})
    project_config.set_mode(PROJECT, 'jira')

    envelope = command({'command': 'assign', 'actor': 'scrummaster',
                         'workItemId': str(item_id), 'agentId': 'not-an-agent'})
    with pytest.raises(store.AssignmentRejectedError):
        command_consumer._handler(envelope)

    texts = posted_comment_texts()
    assert len(texts) == 1
    # The writer's own `[<author>] ` prefix (REQ-09) over the rejection
    # comment's `[system] ` marker: the author is `system`, and the marker is
    # part of the text in both modes.
    assert texts[0] == f'[system] [system] assign rejected: ' \
        f'assignment of "not-an-agent" to {item_id} rejected: UNKNOWN_AGENT' \
        '\n\nPermitted agents for this project: refinement-agent, backend-agent', texts
    assert WorkItemComment.objects.count() == 0, 'nothing recorded — it returns on its own webhook'
    # No OTHER Jira write: the rejected assignment never reached the writer.
    from tests.jira_fixture import requests_to
    assert requests_to('PUT', '/issue/RC-1') == []


def test_a_rejection_comment_whose_post_fails_transiently_is_raised_as_transient(clean_db, permissive_jira,
                                                                                  monkeypatch):
    """REQ-09 — "If its post fails transiently, the handler raises that
    failure as transient." A dead letter written instead would lose the
    comment for good; the redelivered command is rejected again and posts
    only what is not yet recorded complete."""
    from workitems import jira_client

    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'X', 'externalKey': 'RC-2'})
    project_config.set_mode(PROJECT, 'jira')
    monkeypatch.setattr(jira_client, 'post_comment',
                         lambda key, text: (_ for _ in ()).throw(RuntimeError('Jira is briefly unavailable')))

    envelope = command({'command': 'assign', 'actor': 'scrummaster',
                         'workItemId': str(item_id), 'agentId': 'not-an-agent'})
    with pytest.raises(RuntimeError, match='briefly unavailable') as excinfo:
        command_consumer._handler(envelope)

    assert command_consumer.is_permanent_rejection(excinfo.value) is False
    assert getattr(excinfo.value, 'permanent', False) is False, \
        'the command must be redelivered, not dead-lettered with its comment lost'
