"""Mirrors services/work-item-service/test/materialize.test.js."""

from __future__ import annotations

import uuid

import pytest

from workitems import store
from workitems.materialize import MaterializationNoProgressError, MaterializationValidationError, materialize_decomposition
from workitems.models import WorkItemLink

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
