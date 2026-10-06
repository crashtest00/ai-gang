"""
Mirrors the Node reference implementation's commandConsumer.test.js (not
carried into this repository). Uses a real Redis
Streams consumer group (workitems.streams.Consumer, running on a
background thread against the real test Redis container) and a real
Postgres-backed store — no mocks for the pieces that matter, same as the
Node reference implementation's approach.
"""

from __future__ import annotations

import uuid

from workitems import project_config, store
from workitems.command_consumer import create_command_consumer, handle_command
from workitems.envelope import Kind, build_envelope
from workitems.stream_topology import command_stream_name
from workitems.streams import dead_letter_stream_name, publish

from tests.jira_fixture import permissive_jira  # noqa: F401 - a pytest fixture, used by name
from tests.wait_support import wait_for

PROJECT = 'test-project'


def publish_command(redis_client, payload):
    envelope = build_envelope(Kind.WORK_ITEM_COMMAND, PROJECT, payload=payload)
    return publish(redis_client, command_stream_name(PROJECT), envelope)


def test_req03_create_command_over_streams_is_durably_applied(clean_db, redis_client, redis_factory):
    item_id = uuid.uuid4()
    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='test-1')
    consumer.start()
    try:
        publish_command(redis_client, {'command': 'create', 'actor': 'tester',
                                        'input': {'id': str(item_id), 'project': PROJECT, 'type': 'task', 'displayName': 'Via Streams'}})
        wait_for(lambda: store.get_work_item(item_id) is not None,
                 expected=f'the create command to be applied — work item {item_id} readable from the store',
                 observed=lambda: f'get_work_item({item_id}) is {store.get_work_item(item_id)!r}')
    finally:
        consumer.stop()

    item = store.get_work_item(item_id)
    assert item.display_name == 'Via Streams'


def test_req03_full_command_sequence_over_streams(clean_db, redis_client, redis_factory):
    item_id = uuid.uuid4()
    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='test-2')
    consumer.start()
    try:
        publish_command(redis_client, {'command': 'create', 'actor': 'tester',
                                        'input': {'id': str(item_id), 'project': PROJECT, 'type': 'task', 'displayName': 'Seq'}})
        wait_for(lambda: store.get_work_item(item_id) is not None,
                 expected=f'the create command to be applied — work item {item_id} readable from the store',
                 observed=lambda: f'get_work_item({item_id}) is {store.get_work_item(item_id)!r}')

        publish_command(redis_client, {'command': 'assign', 'actor': 'tester', 'workItemId': str(item_id), 'agentId': 'backend-agent'})
        wait_for(lambda: store.get_work_item(item_id).assignee_agent_id == 'backend-agent',
                 expected="the assign command to be applied — assignee_agent_id 'backend-agent'",
                 observed=lambda: f'assignee_agent_id is {store.get_work_item(item_id).assignee_agent_id!r}')

        publish_command(redis_client, {'command': 'transitionStatus', 'actor': 'tester', 'workItemId': str(item_id), 'status': 'in-progress'})
        wait_for(lambda: store.get_work_item(item_id).status == 'in-progress',
                 expected="the transitionStatus command to be applied — status 'in-progress'",
                 observed=lambda: f'status is {store.get_work_item(item_id).status!r}')

        publish_command(redis_client, {'command': 'attachArtifact', 'actor': 'tester', 'workItemId': str(item_id),
                                        'artifactType': 'commit', 'reference': 'sha1'})
        publish_command(redis_client, {'command': 'appendComment', 'actor': 'tester', 'workItemId': str(item_id),
                                        'author': 'backend-agent', 'body': 'done'})

        from workitems.models import WorkItemComment
        wait_for(lambda: WorkItemComment.objects.filter(work_item_id=item_id).count() == 1,
                 expected='the appendComment command to be applied — exactly 1 comment on the work item',
                 observed=lambda: f'{WorkItemComment.objects.filter(work_item_id=item_id).count()} comment(s)')
    finally:
        consumer.stop()


def test_req03_unknown_command_is_dead_lettered(clean_db, redis_client, redis_factory):
    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='test-3')
    consumer.start()
    try:
        publish_command(redis_client, {'command': 'not-a-real-command'})
        wait_for(lambda: redis_client.xlen(dead_letter_stream_name(command_stream_name(PROJECT))) == 1,
                 expected='the unknown command to be dead-lettered — 1 entry on the dead-letter stream',
                 observed=lambda: f'{redis_client.xlen(dead_letter_stream_name(command_stream_name(PROJECT)))} dead-letter entry/entries')
    finally:
        consumer.stop()


def test_status_change_against_jira_mode_project_is_pushed_to_jira_and_not_recorded(
        clean_db, redis_client, redis_factory, permissive_jira):
    """canonical-delivery-state.md REQ-09 — this command used to be
    dead-lettered as WRITE_GATE_REJECTED, which is what "Jira mode is off"
    meant in v5.1. From v5.2 the router PUSHES a machine write: the
    equivalent Jira write is made, nothing is recorded, the command is
    acknowledged rather than dead-lettered, and the canonical status moves
    only when Jira's webhook returns.

    Driven through the real command consumer against the fixture Jira API —
    the enforcement point, not `store.transition_status` directly."""
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'Gated', 'externalKey': 'TP-900'})
    project_config.set_mode(PROJECT, 'jira')

    transitions = []
    permissive_jira.routes[('GET', '/rest/api/3/issue/TP-900/transitions')] = \
        lambda query, body: (200, {'transitions': [{'id': '31', 'to': {'name': 'In Progress'}}]})

    def post_transition(query, body):
        transitions.append(body)
        return 200, None

    permissive_jira.routes[('POST', '/rest/api/3/issue/TP-900/transitions')] = post_transition

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='test-4')
    consumer.start()
    try:
        publish_command(redis_client, {'command': 'transitionStatus', 'actor': 'tester', 'workItemId': str(item_id), 'status': 'in-progress'})
        wait_for(lambda: len(transitions) == 1,
                 expected='the equivalent Jira transition to have been posted',
                 observed=lambda: f'{len(transitions)} transition(s) posted')
    finally:
        consumer.stop()
        project_config.revert_to_local(PROJECT)

    assert transitions == [{'transition': {'id': '31'}}]
    assert redis_client.xlen(dead_letter_stream_name(command_stream_name(PROJECT))) == 0, \
        'a pushed write is not a rejection'
    item = store.get_work_item(item_id)
    assert item.status == 'proposed', 'nothing is recorded until Jira\'s webhook returns'


def test_a_redelivered_routed_command_makes_no_second_jira_write(
        clean_db, redis_client, redis_factory, permissive_jira):
    """REQ-09 step 5 — `handle_command` passes the envelope's `messageId`
    to every routed handler as its `completion_key`, and the writer records
    each step after the call succeeds, so a command redelivered under the
    same `messageId` skips the step that is already done."""
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task',
                             'displayName': 'Gated', 'externalKey': 'TP-901'})
    project_config.set_mode(PROJECT, 'jira')

    posted = []
    permissive_jira.routes[('GET', '/rest/api/3/issue/TP-901/transitions')] = \
        lambda query, body: (200, {'transitions': [{'id': '31', 'to': {'name': 'In Progress'}}]})
    permissive_jira.routes[('POST', '/rest/api/3/issue/TP-901/transitions')] = \
        lambda query, body: (posted.append(body), (200, None))[1]

    envelope = build_envelope(Kind.WORK_ITEM_COMMAND, PROJECT, payload={
        'command': 'transitionStatus', 'actor': 'tester', 'workItemId': str(item_id), 'status': 'in-progress',
    })
    try:
        handle_command(envelope)
        handle_command(envelope)  # redelivery of the SAME messageId.
    finally:
        project_config.revert_to_local(PROJECT)

    assert len(posted) == 1, 'the completed step is skipped on redelivery'


def test_release_candidate_cut_against_dirty_queue_is_dead_lettered(clean_db, redis_client, redis_factory):
    """A store.ReleaseGateError is a
    well-formed rejection (a dirty beta queue, not a transient failure), so
    a candidate-cut command arriving over Streams must be dead-lettered
    immediately rather than retried, same as the write-gate rejection
    above."""
    release_id = uuid.uuid4()
    outstanding_id = uuid.uuid4()
    story_detail = {'behavior': 'b', 'acceptanceCriteria': 'ac', 'constraints': 'c', 'edgeCases': 'e', 'outOfScope': 'oos'}
    store.create_work_item({'id': release_id, 'project': PROJECT, 'type': 'release', 'displayName': 'Release'})
    store.create_work_item({'id': outstanding_id, 'project': PROJECT, 'type': 'story', 'displayName': 'Outstanding',
                             'status': 'in-review', 'storyDetail': story_detail})

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='test-release-1')
    consumer.start()
    try:
        publish_command(redis_client, {'command': 'transitionStatus', 'actor': 'tester',
                                        'workItemId': str(release_id), 'status': 'in-progress'})
        wait_for(lambda: redis_client.xlen(dead_letter_stream_name(command_stream_name(PROJECT))) == 1,
                 expected='the release-gate rejection to be dead-lettered — 1 entry on the dead-letter stream',
                 observed=lambda: f'{redis_client.xlen(dead_letter_stream_name(command_stream_name(PROJECT)))} dead-letter entry/entries')
    finally:
        consumer.stop()

    item = store.get_work_item(release_id)
    assert item.status == 'proposed', 'the rejected candidate cut must never have been applied'
