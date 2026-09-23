"""Mirrors services/work-item-service/test/materialize.test.js."""

from __future__ import annotations

import uuid

import pytest

from workitems import readstore, store
from workitems.materialize import (
    MaterializationNoProgressError,
    MaterializationUnresolvedArtifactError,
    MaterializationValidationError,
    materialize_decomposition,
)
from workitems.models import WorkItemLink

from tests.test_work_item_references_store import make_artifact

PROJECT = 'test-project'


def test_order_independent_forward_reference_resolves_in_one_pass(clean_db):
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # c is declared BEFORE b in the array, but c is blocked by b — the
    # algorithm must not assume declaration order matches dependency order.
    subtasks = [
        {'id': a, 'displayName': 'A (root)', 'description': 'a', 'agent': 'backend-agent', 'Blocked By': []},
        {'id': c, 'displayName': 'C (blocked by B)', 'description': 'c', 'agent': 'backend-agent', 'Blocked By': [b]},
        {'id': b, 'displayName': 'B (blocked by A)', 'description': 'b', 'agent': 'backend-agent', 'Blocked By': [a]},
    ]

    materialize_decomposition({'subtasks': subtasks}, PROJECT, actor='refinement-agent')

    item_a = store.get_work_item(a)
    item_b = store.get_work_item(b)
    item_c = store.get_work_item(c)
    assert item_a.status == 'ready'
    assert item_b.status == 'waiting-on-dependency'
    assert item_c.status == 'waiting-on-dependency'

    assert WorkItemLink.objects.count() == 2


def test_req04_completing_blocker_unblocks_dependent(clean_db):
    a, b = uuid.uuid4(), uuid.uuid4()
    subtasks = [
        {'id': a, 'displayName': 'A', 'description': 'a', 'agent': 'backend-agent', 'Blocked By': []},
        {'id': b, 'displayName': 'B', 'description': 'b', 'agent': 'backend-agent', 'Blocked By': [a]},
    ]
    materialize_decomposition({'subtasks': subtasks}, PROJECT, actor='refinement-agent')

    assert store.get_work_item(b).status == 'waiting-on-dependency'

    store.transition_status(a, 'in-progress', actor='tester')
    store.transition_status(a, 'done', actor='tester')

    assert store.get_work_item(b).status == 'ready'


def test_atomic_rejection_invalid_agent_rejects_whole_batch(clean_db):
    a, b = uuid.uuid4(), uuid.uuid4()
    subtasks = [
        {'id': a, 'displayName': 'Valid', 'description': 'a', 'agent': 'backend-agent', 'Blocked By': []},
        {'id': b, 'displayName': 'Invalid', 'description': 'b', 'agent': 'ghost-agent', 'Blocked By': []},
    ]

    with pytest.raises(MaterializationValidationError):
        materialize_decomposition({'subtasks': subtasks}, PROJECT, actor='refinement-agent')

    assert store.get_work_item(a) is None
    assert store.get_work_item(b) is None


def test_no_progress_missing_blocker_reference_reported(clean_db):
    a = uuid.uuid4()
    ghost_blocker_id = uuid.uuid4()  # never declared in this batch
    subtasks = [
        {'id': a, 'displayName': 'Stuck', 'description': 'a', 'agent': 'backend-agent', 'Blocked By': [ghost_blocker_id]},
    ]

    with pytest.raises(MaterializationNoProgressError) as exc_info:
        materialize_decomposition({'subtasks': subtasks}, PROJECT, actor='refinement-agent')

    assert len(exc_info.value.unresolved) == 1
    assert exc_info.value.unresolved[0]['id'] == a


def test_redelivery_of_already_materialized_batch_is_idempotent(clean_db):
    a, b = uuid.uuid4(), uuid.uuid4()
    subtasks = [
        {'id': a, 'displayName': 'A', 'description': 'a', 'agent': 'backend-agent', 'Blocked By': []},
        {'id': b, 'displayName': 'B', 'description': 'b', 'agent': 'backend-agent', 'Blocked By': [a]},
    ]
    materialize_decomposition({'subtasks': subtasks}, PROJECT, actor='refinement-agent')
    materialize_decomposition({'subtasks': subtasks}, PROJECT, actor='refinement-agent')  # redelivery

    from workitems.models import WorkItem
    assert WorkItem.objects.filter(id__in=[a, b]).count() == 2
    assert WorkItemLink.objects.count() == 1


# ---------------------------------------------------------------------------
# v4.1 agent-artifact-automation.md REQ-01 — a subtask proposal's optional
# specificationLink/artifactLinks forward unchanged onto create_work_item,
# and an unresolved artifact id rolls back the whole call and names both
# the subtask and the artifact id. tests/test_v41_materialize_references.py
# covers the same requirement through the real Streams command path (this
# track's gate-1 enforcement point); these exercise materialize.py's own
# per-proposal wrapping directly.
# ---------------------------------------------------------------------------

def test_req01_materialize_decomposition_forwards_references_to_create_work_item(clean_db):
    spec_artifact = make_artifact()
    link_artifact = make_artifact()
    subtask_id = uuid.uuid4()
    subtasks = [{
        'id': subtask_id, 'displayName': 'Backend: implement endpoint', 'description': 'do it',
        'agent': 'backend-agent', 'Blocked By': [],
        'specificationLink': {'artifactId': str(spec_artifact.id), 'requirementId': 'REQ-9'},
        'artifactLinks': [str(link_artifact.id)],
    }]

    materialize_decomposition({'subtasks': subtasks}, PROJECT, actor='refinement-agent')

    link = readstore.get_specification_link(subtask_id)
    assert link.artifact_id == spec_artifact.id
    assert link.requirement_id == 'REQ-9'
    assert [l.artifact_id for l in readstore.list_artifact_links(subtask_id)] == [link_artifact.id]


def test_req01_unresolved_artifact_link_rolls_back_and_names_subtask_and_artifact(clean_db):
    parent_id = uuid.uuid4()
    store.create_work_item({'id': parent_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Parent'})
    subtask_id = uuid.uuid4()
    bogus_artifact_id = uuid.uuid4()
    subtasks = [{
        'id': subtask_id, 'displayName': 'Backend: implement endpoint', 'description': 'do it',
        'agent': 'backend-agent', 'Blocked By': [],
        'artifactLinks': [str(bogus_artifact_id)],
    }]

    with pytest.raises(MaterializationUnresolvedArtifactError) as exc_info:
        materialize_decomposition(
            {'parentWorkItemId': str(parent_id), 'subtasks': subtasks}, PROJECT, actor='refinement-agent',
        )

    assert exc_info.value.subtask_id == subtask_id
    assert str(bogus_artifact_id) in str(exc_info.value)
    assert store.get_work_item(subtask_id) is None

    parent = store.get_work_item(parent_id)
    assert parent.status == 'needs-clarification'


def test_req01_unresolved_artifact_without_a_parent_still_raises_without_crashing(clean_db):
    """materialize_decomposition's own unit-level entry point (unlike
    handleCreateSubtask/gateway.js, which always supplies a
    parentWorkItemId) may be called with none, as every other test in this
    file already does — the report-on-parent step must not itself error
    when there is no parent to report on."""
    subtask_id = uuid.uuid4()
    bogus_artifact_id = uuid.uuid4()
    subtasks = [{
        'id': subtask_id, 'displayName': 'Backend: implement endpoint', 'description': 'do it',
        'agent': 'backend-agent', 'Blocked By': [],
        'artifactLinks': [str(bogus_artifact_id)],
    }]

    with pytest.raises(MaterializationUnresolvedArtifactError):
        materialize_decomposition({'subtasks': subtasks}, PROJECT, actor='refinement-agent')

    assert store.get_work_item(subtask_id) is None


def test_req01_subtask_with_neither_reference_is_unaffected(clean_db):
    """A decomposition that uses neither field behaves exactly as before —
    no specification link, no artifact links, no new failure mode."""
    a = uuid.uuid4()
    subtasks = [{'id': a, 'displayName': 'A', 'description': 'a', 'agent': 'backend-agent', 'Blocked By': []}]

    materialize_decomposition({'subtasks': subtasks}, PROJECT, actor='refinement-agent')

    assert readstore.get_specification_link(a) is None
    assert readstore.list_artifact_links(a) == []


def test_req05_forward_query_enumerates_a_decomposition_created_subtask(clean_db):
    """REQ-05: "the forward query (work-items.md REQ-06) ... MUST enumerate
    the story and every subtask created from it" — this feature adds the
    subtask rows that query traverses; work-items.md REQ-06's own query
    mechanism is unchanged and already covered
    (test_work_item_references_store.py's
    test_forward_query_req06_enumerates_every_work_item_recording_the_link).
    What's new here is that a decomposition-created subtask's
    specification link is recorded in a shape that query actually finds."""
    artifact = make_artifact()
    subtask_id = uuid.uuid4()
    subtasks = [{
        'id': subtask_id, 'displayName': 'Backend: implement endpoint', 'description': 'do it',
        'agent': 'backend-agent', 'Blocked By': [],
        'specificationLink': {'artifactId': str(artifact.id), 'requirementId': 'REQ-9'},
    }]

    materialize_decomposition({'subtasks': subtasks}, PROJECT, actor='refinement-agent')

    rows = readstore.list_work_items(project=PROJECT, spec_artifact_id=str(artifact.id), requirement_id='REQ-9')
    assert {r.id for r in rows} == {subtask_id}
