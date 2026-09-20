"""
work-items.md REQ-03 — recording either reference travels ONLY as a
Streams command; there is no synchronous write endpoint. Same shape as
test_command_consumer.py: a real Redis Streams consumer group
(workitems.streams.Consumer) against the real test Redis container, a real
Postgres-backed store — the "gate 1" enforcement-point path this track's
build brief names explicitly for REQ-03/REQ-04.
"""

from __future__ import annotations

import time
import uuid

from django.urls import Resolver404, resolve

from workitems import readstore, store
from workitems.command_consumer import create_command_consumer
from workitems.envelope import Kind, build_envelope
from workitems.stream_topology import command_stream_name
from workitems.streams import dead_letter_stream_name, publish

from tests.test_work_item_references_store import make_artifact

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


def test_req03_record_specification_link_command_over_streams_is_durably_applied(clean_db, redis_client, redis_factory):
    artifact = make_artifact()
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='ref-test-1')
    consumer.start()
    try:
        publish_command(redis_client, {
            'command': 'recordSpecificationLink', 'actor': 'refinement-agent',
            'workItemId': str(item_id), 'artifactId': str(artifact.id), 'requirementId': 'REQ-18',
        })
        wait_for(lambda: readstore.get_specification_link(item_id) is not None)
    finally:
        consumer.stop()

    link = readstore.get_specification_link(item_id)
    assert link.artifact_id == artifact.id
    assert link.requirement_id == 'REQ-18'


def test_req03_add_artifact_link_command_over_streams_preserves_order(clean_db, redis_client, redis_factory):
    artifacts = [make_artifact() for _ in range(3)]
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='ref-test-2')
    consumer.start()
    try:
        for artifact in artifacts:
            publish_command(redis_client, {
                'command': 'addArtifactLink', 'actor': 'refinement-agent',
                'workItemId': str(item_id), 'artifactId': str(artifact.id),
            })
        wait_for(lambda: len(readstore.list_artifact_links(item_id)) == 3)
    finally:
        consumer.stop()

    links = readstore.list_artifact_links(item_id)
    assert [l.artifact_id for l in links] == [a.id for a in artifacts]


def test_req04_record_specification_link_over_streams_with_unresolved_artifact_is_dead_lettered(clean_db, redis_client, redis_factory):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='ref-test-3')
    consumer.start()
    try:
        publish_command(redis_client, {
            'command': 'recordSpecificationLink', 'actor': 'refinement-agent',
            'workItemId': str(item_id), 'artifactId': str(uuid.uuid4()), 'requirementId': 'REQ-18',
        })
        wait_for(lambda: redis_client.xlen(dead_letter_stream_name(command_stream_name(PROJECT))) == 1)
    finally:
        consumer.stop()

    assert readstore.get_specification_link(item_id) is None


def test_req04_add_artifact_link_over_streams_with_unresolved_artifact_is_dead_lettered(clean_db, redis_client, redis_factory):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='ref-test-4')
    consumer.start()
    try:
        publish_command(redis_client, {
            'command': 'addArtifactLink', 'actor': 'refinement-agent',
            'workItemId': str(item_id), 'artifactId': str(uuid.uuid4()),
        })
        wait_for(lambda: redis_client.xlen(dead_letter_stream_name(command_stream_name(PROJECT))) == 1)
    finally:
        consumer.stop()

    assert readstore.list_artifact_links(item_id) == []


def test_req03_create_command_with_inline_references_over_streams(clean_db, redis_client, redis_factory):
    """REQ-05's primary path — recorded at creation, over the real command
    stream, exactly as a Refinement Agent would."""
    artifact = make_artifact()
    other_artifact = make_artifact()
    item_id = uuid.uuid4()

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='ref-test-5')
    consumer.start()
    try:
        publish_command(redis_client, {
            'command': 'create', 'actor': 'refinement-agent',
            'input': {
                'id': str(item_id), 'project': PROJECT, 'type': 'task', 'displayName': 'Built from spec',
                'specificationLink': {'artifactId': str(artifact.id), 'requirementId': 'REQ-18'},
                'artifactLinks': [str(other_artifact.id)],
            },
        })
        wait_for(lambda: store.get_work_item(item_id) is not None)
        wait_for(lambda: readstore.get_specification_link(item_id) is not None)
    finally:
        consumer.stop()

    link = readstore.get_specification_link(item_id)
    assert link.artifact_id == artifact.id
    assert link.requirement_id == 'REQ-18'
    assert [l.artifact_id for l in readstore.list_artifact_links(item_id)] == [other_artifact.id]


def test_req03_no_synchronous_write_route_exists_for_either_reference():
    """REQ-03's negative half: "a direct, non-Streams attempt to record
    either reference finds no such interface to call." Checked against the
    actual URL resolver — the same "does this route exist at all" question
    an HTTP client hitting a guessed path would hit, without depending on
    what status code a catch-all view might return."""
    for guess in (
        '/work-items/00000000-0000-0000-0000-000000000000/specification-link',
        '/admin/work-items/00000000-0000-0000-0000-000000000000/specification-link',
        '/admin/work-items/00000000-0000-0000-0000-000000000000/artifact-links',
        '/work-items/00000000-0000-0000-0000-000000000000/artifact-links',
    ):
        try:
            match = resolve(guess)
        except Resolver404:
            continue
        raise AssertionError(f'expected no route for {guess!r}, resolved to {match}')
