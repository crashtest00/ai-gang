"""
Mirrors services/work-item-service/test/commandConsumer.test.js. Uses a real Redis
Streams consumer group (workitems.streams.Consumer, running on a
background thread against the real test Redis container) and a real
Postgres-backed store — no mocks for the pieces that matter, same as the
Node reference implementation's approach.
"""

from __future__ import annotations

import time
import uuid

from workitems import project_config, store
from workitems.command_consumer import create_command_consumer
from workitems.envelope import Kind, build_envelope
from workitems.stream_topology import command_stream_name
from workitems.streams import dead_letter_stream_name, publish

PROJECT = 'test-project'


def publish_command(redis_client, payload):
    envelope = build_envelope(Kind.WORK_ITEM_COMMAND, PROJECT, payload=payload)
    return publish(redis_client, command_stream_name(PROJECT), envelope)


def wait_for(predicate, timeout_s=5.0, interval_s=0.03):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise TimeoutError('wait_for timed out')


def test_req03_create_command_over_streams_is_durably_applied(clean_db, redis_client, redis_factory):
    item_id = uuid.uuid4()
    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='test-1')
    consumer.start()
    try:
        publish_command(redis_client, {'command': 'create', 'actor': 'tester',
                                        'input': {'id': str(item_id), 'project': PROJECT, 'type': 'task', 'displayName': 'Via Streams'}})
        wait_for(lambda: store.get_work_item(item_id) is not None)
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
        wait_for(lambda: store.get_work_item(item_id) is not None)

        publish_command(redis_client, {'command': 'assign', 'actor': 'tester', 'workItemId': str(item_id), 'agentId': 'backend-agent'})
        wait_for(lambda: store.get_work_item(item_id).assignee_agent_id == 'backend-agent')

        publish_command(redis_client, {'command': 'transitionStatus', 'actor': 'tester', 'workItemId': str(item_id), 'status': 'in-progress'})
        wait_for(lambda: store.get_work_item(item_id).status == 'in-progress')

        publish_command(redis_client, {'command': 'attachArtifact', 'actor': 'tester', 'workItemId': str(item_id),
                                        'artifactType': 'commit', 'reference': 'sha1'})
        publish_command(redis_client, {'command': 'appendComment', 'actor': 'tester', 'workItemId': str(item_id),
                                        'author': 'backend-agent', 'body': 'done'})

        from workitems.models import WorkItemComment
        wait_for(lambda: WorkItemComment.objects.filter(work_item_id=item_id).count() == 1)
    finally:
        consumer.stop()


def test_req03_unknown_command_is_dead_lettered(clean_db, redis_client, redis_factory):
    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='test-3')
    consumer.start()
    try:
        publish_command(redis_client, {'command': 'not-a-real-command'})
        wait_for(lambda: redis_client.xlen(dead_letter_stream_name(command_stream_name(PROJECT))) == 1)
    finally:
        consumer.stop()


def test_req15_record_external_key_command_over_streams_is_idempotent(clean_db, redis_client, redis_factory):
    """canonical-work-model.md REQ-15: jiraCatchupConsumer.js reports a
    newly-created Jira issue's key back via this command. Not REQ-10-gated
    (external_key isn't a status/assignment/dependency field), and applying
    it twice (a redelivered command) must not error or overwrite a
    different key — record_external_key's own idempotency contract."""
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Catch-up target'})

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='test-5')
    consumer.start()
    try:
        publish_command(redis_client, {'command': 'recordExternalKey', 'actor': 'jira-catchup',
                                        'workItemId': str(item_id), 'externalKey': 'GANG-1'})
        wait_for(lambda: store.get_work_item(item_id).external_key == 'GANG-1')

        # Redelivery / retry with the same key: must remain a no-op, not an error.
        publish_command(redis_client, {'command': 'recordExternalKey', 'actor': 'jira-catchup',
                                        'workItemId': str(item_id), 'externalKey': 'GANG-1'})
        time.sleep(0.2)
    finally:
        consumer.stop()

    assert store.get_work_item(item_id).external_key == 'GANG-1'


def test_req10_status_change_against_jira_mode_project_is_dead_lettered(clean_db, redis_client, redis_factory):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Gated'})
    project_config.set_mode(PROJECT, 'jira')

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='test-4')
    consumer.start()
    try:
        publish_command(redis_client, {'command': 'transitionStatus', 'actor': 'tester', 'workItemId': str(item_id), 'status': 'in-progress'})
        wait_for(lambda: redis_client.xlen(dead_letter_stream_name(command_stream_name(PROJECT))) == 1)
    finally:
        consumer.stop()
        project_config.revert_to_local(PROJECT)

    item = store.get_work_item(item_id)
    assert item.status == 'proposed', 'the gated write must never have been applied'


def test_release_candidate_cut_against_dirty_queue_is_dead_lettered(clean_db, redis_client, redis_factory):
    """canonical-release-workflow.md REQ-03 — a store.ReleaseGateError is a
    well-formed rejection (a dirty beta queue, not a transient failure), so
    a candidate-cut command arriving over Streams must be dead-lettered
    immediately rather than retried, same as REQ-10's write-gate rejection
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
                                        'workItemId': str(release_id), 'status': 'in-review'})
        wait_for(lambda: redis_client.xlen(dead_letter_stream_name(command_stream_name(PROJECT))) == 1)
    finally:
        consumer.stop()

    item = store.get_work_item(release_id)
    assert item.status == 'proposed', 'the rejected candidate cut must never have been applied'
