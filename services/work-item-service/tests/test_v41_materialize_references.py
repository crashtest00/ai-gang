"""
strategy/v4.1/features/agent-artifact-automation.md REQ-01 — a
`materializeDecomposition` command's subtask entries carry the same
`specificationLink`/`artifactLinks` shape `create` already accepts
(work-items.md REQ-01, REQ-02), and an artifact id that does not resolve
rejects the whole command atomically, reported on the parent work item as
a system comment naming both the subtask and the artifact id, with the
parent transitioned to `needs-clarification`.

Driven through the real command consumer against the real test Redis
container and a real Postgres-backed store, and read back through the real
HTTP view — the "gate 1" enforcement-point path this track's build brief
names explicitly ("Django tests publish a real materializeDecomposition
command with references onto the real test stream and read the created
subtask by canonical id through the real HTTP view"), mirroring
tests/test_work_item_references_command_consumer.py's own style.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from django.test import Client

from workitems import store
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


def test_req01_materialize_decomposition_over_streams_records_references_on_the_subtask(clean_db, redis_client, redis_factory):
    """REQ-01's acceptance: two create_subtask-shaped subtask entries under
    one story, one carrying both references and one carrying none, produce
    two work items whose records show exactly those references, readable
    by canonical id — through the real materializeDecomposition command
    and the real HTTP read view."""
    spec_artifact = make_artifact()
    link_artifact = make_artifact()
    parent_id = uuid.uuid4()
    store.create_work_item({'id': parent_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Parent story'})

    with_refs = uuid.uuid4()
    without_refs = uuid.uuid4()

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='v41-test-1')
    consumer.start()
    try:
        publish_command(redis_client, {
            'command': 'materializeDecomposition', 'actor': 'refinement-agent',
            'message': {
                'parentWorkItemId': str(parent_id),
                'subtasks': [
                    {
                        'id': str(with_refs), 'displayName': 'Backend: implement endpoint', 'description': 'do it',
                        'agent': 'backend-agent', 'Blocked By': [],
                        'specificationLink': {'artifactId': str(spec_artifact.id), 'requirementId': 'REQ-9'},
                        'artifactLinks': [str(link_artifact.id)],
                    },
                    {
                        # test-project's fixture catalog only permits
                        # refinement-agent/backend-agent
                        # (services/scrummaster/test/fixtures/projects.json)
                        # — a second backend-agent subtask, not a second
                        # role, is what keeps this test's batch valid.
                        'id': str(without_refs), 'displayName': 'Backend: implement other endpoint', 'description': 'do it too',
                        'agent': 'backend-agent', 'Blocked By': [],
                    },
                ],
            },
        })
        wait_for(lambda: store.get_work_item(with_refs) is not None and store.get_work_item(without_refs) is not None)
    finally:
        consumer.stop()

    client = Client()
    with_refs_body = client.get(f'/work-items/{with_refs}', {'full': 'true'}).json()
    without_refs_body = client.get(f'/work-items/{without_refs}', {'full': 'true'}).json()

    assert with_refs_body['specification_link']['artifact_id'] == str(spec_artifact.id)
    assert with_refs_body['specification_link']['requirement_id'] == 'REQ-9'
    assert [l['artifact_id'] for l in with_refs_body['artifact_links']] == [str(link_artifact.id)]

    assert without_refs_body['specification_link'] is None
    assert without_refs_body['artifact_links'] == []


def test_req01_materialize_decomposition_with_unresolved_artifact_rolls_back_and_reports_on_parent(clean_db, redis_client, redis_factory):
    """REQ-01's second acceptance clause: a submission naming an
    unregistered artifact id creates no work item and no link, the
    dead-lettered command's error names the offending subtask and the
    artifact id, and the parent story carries a system comment naming both
    and sits in needs-clarification at the moment of the rejection.

    The parent is created as type 'task' rather than 'story' so this test
    exercises only REQ-01's own mechanism (the comment-then-transition
    written after the rollback) without also invoking
    `_assert_story_fields_present`'s unrelated story-detail gate on the
    'proposed' -> 'needs-clarification' transition."""
    parent_id = uuid.uuid4()
    store.create_work_item({'id': parent_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Parent story'})

    subtask_id = uuid.uuid4()
    bogus_artifact_id = uuid.uuid4()
    stream = command_stream_name(PROJECT)

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='v41-test-2')
    consumer.start()
    try:
        publish_command(redis_client, {
            'command': 'materializeDecomposition', 'actor': 'refinement-agent',
            'message': {
                'parentWorkItemId': str(parent_id),
                'subtasks': [{
                    'id': str(subtask_id), 'displayName': 'Backend: implement endpoint', 'description': 'do it',
                    'agent': 'backend-agent', 'Blocked By': [],
                    'artifactLinks': [str(bogus_artifact_id)],
                }],
            },
        })
        wait_for(lambda: redis_client.xlen(dead_letter_stream_name(stream)) == 1)
    finally:
        consumer.stop()

    # No subtask was created — the atomic block rolled back.
    assert store.get_work_item(subtask_id) is None

    # The dead-lettered command's error names both ids.
    entries = redis_client.xrange(dead_letter_stream_name(stream))
    assert len(entries) == 1
    _entry_id, fields = entries[0]
    dead_letter = json.loads(fields['data'])
    assert str(subtask_id) in dead_letter['reason']
    assert str(bogus_artifact_id) in dead_letter['reason']

    # Reported on the parent, at the moment of the rejection — nothing
    # else touches this story in this test, so the status observed here is
    # exactly what the rejection itself set (REQ-20's rollup is a later,
    # separate concern this feature does not fight).
    parent = store.get_work_item(parent_id)
    assert parent.status == 'needs-clarification'

    client = Client()
    parent_body = client.get(f'/work-items/{parent_id}', {'full': 'true'}).json()
    comment_bodies = [c['body'] for c in parent_body['comments']]
    assert any(
        str(subtask_id) in body and str(bogus_artifact_id) in body for body in comment_bodies
    ), f'expected a comment naming both the subtask id and the artifact id, got: {comment_bodies}'


def test_req01_materialize_decomposition_with_unresolved_specification_link_also_rolls_back(clean_db, redis_client, redis_factory):
    """The same rejection path, triggered by a malformed specificationLink
    reference instead of an artifactLinks entry — store.create_work_item
    checks the specification link before the artifact links loop, and
    either must roll back the whole command the same way."""
    parent_id = uuid.uuid4()
    store.create_work_item({'id': parent_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Parent story'})

    subtask_id = uuid.uuid4()
    bogus_artifact_id = uuid.uuid4()
    stream = command_stream_name(PROJECT)

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='v41-test-3')
    consumer.start()
    try:
        publish_command(redis_client, {
            'command': 'materializeDecomposition', 'actor': 'refinement-agent',
            'message': {
                'parentWorkItemId': str(parent_id),
                'subtasks': [{
                    'id': str(subtask_id), 'displayName': 'Backend: implement endpoint', 'description': 'do it',
                    'agent': 'backend-agent', 'Blocked By': [],
                    'specificationLink': {'artifactId': str(bogus_artifact_id), 'requirementId': 'REQ-1'},
                }],
            },
        })
        wait_for(lambda: redis_client.xlen(dead_letter_stream_name(stream)) == 1)
    finally:
        consumer.stop()

    assert store.get_work_item(subtask_id) is None
    parent = store.get_work_item(parent_id)
    assert parent.status == 'needs-clarification'


def test_req01_materialize_decomposition_unresolved_artifact_report_reaches_a_story_parent_with_complete_fields(clean_db, redis_client, redis_factory):
    """The reachable case named by the V4.1 doc-vs-code audit's row on
    materialize.py's rejection report: dispatch only ever decomposes a
    parent that already sits at 'ready', and reaching 'ready' already ran
    `_transition_status_core`'s story-detail gate (store.py:443-451,
    `_assert_story_fields_present`) — so a `story` parent with complete
    story fields takes the rejection report's 'proposed' ->
    'needs-clarification' transition exactly the way a `task` parent
    does."""
    parent_id = uuid.uuid4()
    store.create_work_item({
        'id': parent_id, 'project': PROJECT, 'type': 'story', 'displayName': 'Parent story',
        'storyDetail': {
            'behavior': 'b', 'acceptanceCriteria': 'ac', 'constraints': 'c',
            'edgeCases': 'e', 'outOfScope': 'oos',
        },
    })

    subtask_id = uuid.uuid4()
    bogus_artifact_id = uuid.uuid4()
    stream = command_stream_name(PROJECT)

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='v41-test-4')
    consumer.start()
    try:
        publish_command(redis_client, {
            'command': 'materializeDecomposition', 'actor': 'refinement-agent',
            'message': {
                'parentWorkItemId': str(parent_id),
                'subtasks': [{
                    'id': str(subtask_id), 'displayName': 'Backend: implement endpoint', 'description': 'do it',
                    'agent': 'backend-agent', 'Blocked By': [],
                    'artifactLinks': [str(bogus_artifact_id)],
                }],
            },
        })
        wait_for(lambda: redis_client.xlen(dead_letter_stream_name(stream)) == 1)
    finally:
        consumer.stop()

    assert store.get_work_item(subtask_id) is None

    # The dead-lettered command's error still names both ids.
    entries = redis_client.xrange(dead_letter_stream_name(stream))
    assert len(entries) == 1
    _entry_id, fields = entries[0]
    dead_letter = json.loads(fields['data'])
    assert str(subtask_id) in dead_letter['reason']
    assert str(bogus_artifact_id) in dead_letter['reason']

    parent = store.get_work_item(parent_id)
    assert parent.status == 'needs-clarification'

    client = Client()
    parent_body = client.get(f'/work-items/{parent_id}', {'full': 'true'}).json()
    comment_bodies = [c['body'] for c in parent_body['comments']]
    assert any(
        str(subtask_id) in body and str(bogus_artifact_id) in body for body in comment_bodies
    ), f'expected a comment naming both the subtask id and the artifact id, got: {comment_bodies}'


def test_req01_materialize_decomposition_unresolved_artifact_report_survives_a_refused_transition(clean_db, redis_client, redis_factory, caplog):
    """The row's central worry: if a parent's `needs-clarification`
    transition is ever refused (here, a `story` parent missing every
    required story-detail field, which `_assert_story_fields_present`
    refuses at store.py:443-451 the same way it would refuse any other
    incomplete story leaving 'proposed'), that refusal must not replace the
    `MaterializationUnresolvedArtifactError` the dead letter and
    `is_permanent_rejection` need. The comment write is independent of the
    transition and still lands; the refused transition is logged once and
    swallowed, not raised."""
    parent_id = uuid.uuid4()
    store.create_work_item({
        'id': parent_id, 'project': PROJECT, 'type': 'story', 'displayName': 'Incomplete parent story',
        # No storyDetail at all — every one of the five required fields is
        # missing, which is what _assert_story_fields_present refuses.
    })

    subtask_id = uuid.uuid4()
    bogus_artifact_id = uuid.uuid4()
    stream = command_stream_name(PROJECT)

    consumer = create_command_consumer(redis_factory, PROJECT, consumer_name='v41-test-5')
    consumer.start()
    try:
        with caplog.at_level(logging.WARNING, logger='workitems.materialize'):
            publish_command(redis_client, {
                'command': 'materializeDecomposition', 'actor': 'refinement-agent',
                'message': {
                    'parentWorkItemId': str(parent_id),
                    'subtasks': [{
                        'id': str(subtask_id), 'displayName': 'Backend: implement endpoint', 'description': 'do it',
                        'agent': 'backend-agent', 'Blocked By': [],
                        'artifactLinks': [str(bogus_artifact_id)],
                    }],
                },
            })
            wait_for(lambda: redis_client.xlen(dead_letter_stream_name(stream)) == 1)
    finally:
        consumer.stop()

    assert store.get_work_item(subtask_id) is None

    # The dead-lettered command's error is still the original
    # MaterializationUnresolvedArtifactError, naming both ids — never
    # replaced by the transition's own ValidationError.
    entries = redis_client.xrange(dead_letter_stream_name(stream))
    assert len(entries) == 1
    _entry_id, fields = entries[0]
    dead_letter = json.loads(fields['data'])
    assert str(subtask_id) in dead_letter['reason']
    assert str(bogus_artifact_id) in dead_letter['reason']

    # The transition was refused — status is unchanged.
    parent = store.get_work_item(parent_id)
    assert parent.status == 'proposed'

    # The comment write does not depend on the transition and still lands.
    client = Client()
    parent_body = client.get(f'/work-items/{parent_id}', {'full': 'true'}).json()
    comment_bodies = [c['body'] for c in parent_body['comments']]
    assert any(
        str(subtask_id) in body and str(bogus_artifact_id) in body for body in comment_bodies
    ), f'expected a comment naming both the subtask id and the artifact id, got: {comment_bodies}'

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == 'workitems.materialize']
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert str(parent_id) in warnings[0].getMessage()
    assert str(subtask_id) in warnings[0].getMessage()
