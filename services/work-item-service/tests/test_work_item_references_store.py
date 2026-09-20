"""
work-items.md REQ-01 (specification link), REQ-02 (artifact links), REQ-04
(a referenced artifact must resolve), and REQ-06 (traceability both
directions) — store.py/readstore.py unit coverage.

Mirrors test_store.py's own style: real Postgres (`clean_db`), no mocks.
Test artifacts are plain `Artifact.objects.create(...)` rows rather than a
real multipart upload — what REQ-04 asks this track to prove is that the
resolution check works against a real row in `artifacts.Artifact` (a real
foreign key, gate 3's schema-level evidence), not how that row came to
exist. `tests/test_work_item_references_admin.py` additionally exercises
the real upload path for the admin write surface.
"""

from __future__ import annotations

import uuid

import pytest
from django.db import IntegrityError

from artifacts.models import Artifact
from workitems import readstore, store
from workitems.models import OutboxEvent, WorkItemArtifactLink, WorkItemSpecificationLink

PROJECT = 'test-project'


def make_artifact() -> Artifact:
    artifact_id = uuid.uuid4()
    return Artifact.objects.create(
        id=artifact_id, file=f'{str(artifact_id)[:2]}/{artifact_id}', original_filename='spec.md', uploaded_by='tester',
    )


def make_work_item(project=PROJECT, **overrides):
    item_id = overrides.pop('id', uuid.uuid4())
    payload = {'id': item_id, 'project': project, 'type': 'task', 'displayName': 'X'}
    payload.update(overrides)
    return store.create_work_item(payload)


# ---------------------------------------------------------------------------
# REQ-01 — specification link
# ---------------------------------------------------------------------------

def test_record_specification_link_req01(clean_db):
    artifact = make_artifact()
    item = make_work_item()

    result = store.record_specification_link(item.id, artifact.id, 'REQ-18')
    assert result == {'workItemId': str(item.id), 'artifactId': str(artifact.id), 'requirementId': 'REQ-18'}

    link = readstore.get_specification_link(item.id)
    assert link.artifact_id == artifact.id
    assert link.requirement_id == 'REQ-18'


def test_record_specification_link_replaces_existing_req01(clean_db):
    """work-items.md §4 fixes cardinality at one per work item; recording a
    second link for the same work item replaces the first rather than
    erroring or creating a second row."""
    first_artifact = make_artifact()
    second_artifact = make_artifact()
    item = make_work_item()

    store.record_specification_link(item.id, first_artifact.id, 'REQ-1')
    store.record_specification_link(item.id, second_artifact.id, 'REQ-2')

    assert WorkItemSpecificationLink.objects.filter(work_item_id=item.id).count() == 1
    link = readstore.get_specification_link(item.id)
    assert link.artifact_id == second_artifact.id
    assert link.requirement_id == 'REQ-2'


def test_record_specification_link_identical_redelivery_is_a_noop_req03(clean_db):
    """work-items.md REQ-03's docstring contract on `_record_specification_link`:
    a redelivered command carrying the SAME (artifactId, requirementId)
    must be a safe no-op — no bumped `updated_at`, no second outbox event
    — mirroring `_add_artifact_link`'s identical-payload dedupe. A
    different pair (test_record_specification_link_replaces_existing_req01
    above) still replaces and emits."""
    artifact = make_artifact()
    item = make_work_item()

    store.record_specification_link(item.id, artifact.id, 'REQ-18')
    link_before = WorkItemSpecificationLink.objects.get(work_item_id=item.id)
    events_before = OutboxEvent.objects.filter(
        work_item_id=item.id, event_type='work_item.specification_link_recorded',
    ).count()

    result = store.record_specification_link(item.id, artifact.id, 'REQ-18')
    assert result == {'workItemId': str(item.id), 'artifactId': str(artifact.id), 'requirementId': 'REQ-18'}

    link_after = WorkItemSpecificationLink.objects.get(work_item_id=item.id)
    assert link_after.updated_at == link_before.updated_at
    assert WorkItemSpecificationLink.objects.filter(work_item_id=item.id).count() == 1
    events_after = OutboxEvent.objects.filter(
        work_item_id=item.id, event_type='work_item.specification_link_recorded',
    ).count()
    assert events_after == events_before


def test_record_specification_link_req04_rejects_unresolved_artifact(clean_db):
    item = make_work_item()
    bogus_artifact_id = uuid.uuid4()

    with pytest.raises(store.UnresolvedArtifactError):
        store.record_specification_link(item.id, bogus_artifact_id, 'REQ-1')

    assert readstore.get_specification_link(item.id) is None


def test_record_specification_link_req04_accepts_registered_artifact_with_arbitrary_requirement_id(clean_db):
    """REQ-04's acceptance, second half: "the same reference naming a
    registered id, with an arbitrary requirement-id string, succeeds" —
    the requirement id is opaque and unvalidated (work-items.md §4)."""
    artifact = make_artifact()
    item = make_work_item()

    store.record_specification_link(item.id, artifact.id, 'not-a-real-requirement-format-at-all')
    assert readstore.get_specification_link(item.id).requirement_id == 'not-a-real-requirement-format-at-all'


def test_create_work_item_with_specification_link_req01_req05(clean_db):
    """REQ-05: "A Refinement Agent records them when it creates the work
    item" — the primary path, not only a follow-up command."""
    artifact = make_artifact()
    item_id = uuid.uuid4()

    item = store.create_work_item({
        'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X',
        'specificationLink': {'artifactId': str(artifact.id), 'requirementId': 'REQ-9'},
    })

    link = readstore.get_specification_link(item.id)
    assert link.artifact_id == artifact.id
    assert link.requirement_id == 'REQ-9'


def test_create_work_item_req04_rolls_back_whole_create_on_unresolved_specification_link(clean_db):
    """The REQ-04 check inside create_work_item runs in the SAME
    transaction as the work item's own creation — an unresolved artifact
    id must fail the entire create, not leave a work item behind with no
    link."""
    item_id = uuid.uuid4()
    with pytest.raises(store.UnresolvedArtifactError):
        store.create_work_item({
            'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X',
            'specificationLink': {'artifactId': str(uuid.uuid4()), 'requirementId': 'REQ-9'},
        })
    assert store.get_work_item(item_id) is None


# ---------------------------------------------------------------------------
# REQ-02 — artifact links (ordered)
# ---------------------------------------------------------------------------

def test_add_artifact_link_req02_preserves_order(clean_db):
    item = make_work_item()
    artifacts = [make_artifact() for _ in range(3)]

    for artifact in artifacts:
        store.add_artifact_link(item.id, artifact.id)

    links = readstore.list_artifact_links(item.id)
    assert [l.artifact_id for l in links] == [a.id for a in artifacts]
    assert [l.position for l in links] == [0, 1, 2]


def test_create_work_item_with_artifact_links_req02_req05(clean_db):
    artifacts = [make_artifact() for _ in range(3)]
    item = store.create_work_item({
        'id': uuid.uuid4(), 'project': PROJECT, 'type': 'task', 'displayName': 'X',
        'artifactLinks': [str(a.id) for a in artifacts],
    })

    links = readstore.list_artifact_links(item.id)
    assert [l.artifact_id for l in links] == [a.id for a in artifacts]


def test_add_artifact_link_req03_idempotent_redelivery(clean_db):
    """A redelivered addArtifactLink command (same work item, same
    artifact) must not create a duplicate list entry or disturb positions
    — mirrors create_link's own idempotent-redelivery contract."""
    item = make_work_item()
    artifact = make_artifact()

    first = store.add_artifact_link(item.id, artifact.id)
    second = store.add_artifact_link(item.id, artifact.id)

    assert first['deduped'] is False
    assert second['deduped'] is True
    assert first['id'] == second['id']
    assert WorkItemArtifactLink.objects.filter(work_item_id=item.id).count() == 1


def test_add_artifact_link_req04_rejects_unresolved_artifact(clean_db):
    item = make_work_item()
    with pytest.raises(store.UnresolvedArtifactError):
        store.add_artifact_link(item.id, uuid.uuid4())
    assert readstore.list_artifact_links(item.id) == []


def test_get_work_item_full_returns_three_artifact_links_regardless_of_delivery_req02(clean_db):
    """REQ-02's acceptance: "a work item with three artifact links returns
    all three by its canonical id, in the order recorded, after delivery of
    one of them and after none of them has been delivered." Delivery
    itself (the librarian) is out of this track's scope — what this proves
    is that the list is unaffected by whether any `work_item_artifact`
    (REQ-06 delivery evidence) rows exist alongside it."""
    item = make_work_item(type='story', status='ready', storyDetail={
        'behavior': 'b', 'acceptanceCriteria': 'ac', 'constraints': 'c', 'edgeCases': 'e', 'outOfScope': 'oos',
    })
    artifacts = [make_artifact() for _ in range(3)]
    for artifact in artifacts:
        store.add_artifact_link(item.id, artifact.id)

    before = readstore.get_work_item_full(item.id)
    assert [l.artifact_id for l in before['artifactLinks']] == [a.id for a in artifacts]

    store.attach_artifact(item.id, 'commit', 'deadbeef')  # "one of them has been delivered"

    after = readstore.get_work_item_full(item.id)
    assert [l.artifact_id for l in after['artifactLinks']] == [a.id for a in artifacts]


# ---------------------------------------------------------------------------
# REQ-01/REQ-05 — persists independent of status, survives real transitions
# ---------------------------------------------------------------------------

def test_specification_link_survives_terminal_status_ac03(clean_db):
    """AC-03: resolvable by canonical id after the work item reaches a
    terminal status, with the artifact id and requirement id it was
    created with — driven through the REAL transition_status path, not a
    direct status column write."""
    artifact = make_artifact()
    item = make_work_item()
    store.record_specification_link(item.id, artifact.id, 'REQ-18')

    store.transition_status(item.id, 'in-progress')
    store.transition_status(item.id, 'in-review')
    store.transition_status(item.id, 'done')

    item = store.get_work_item(item.id)
    assert item.status == 'done'
    link = readstore.get_specification_link(item.id)
    assert link.artifact_id == artifact.id
    assert link.requirement_id == 'REQ-18'


def test_artifact_links_survive_reassignment_and_redispatch_req05(clean_db):
    """REQ-05: read again unchanged after a redispatch (here: reassignment
    to a different agent, then a status transition and back) — real
    store.py mutations, not a direct write to the child table."""
    artifacts = [make_artifact(), make_artifact()]
    item = make_work_item()
    for artifact in artifacts:
        store.add_artifact_link(item.id, artifact.id)

    store.assign_work_item(item.id, 'backend-agent')
    store.transition_status(item.id, 'in-progress')
    # Redispatch: send it back through needs-clarification and reassign it,
    # a real second dispatch rather than a no-op re-save of the same state.
    store.transition_status(item.id, 'needs-clarification')
    store.assign_work_item(item.id, 'backend-agent')
    store.transition_status(item.id, 'in-progress')

    links = readstore.list_artifact_links(item.id)
    assert [l.artifact_id for l in links] == [a.id for a in artifacts]


# ---------------------------------------------------------------------------
# REQ-06 — traceability both directions
# ---------------------------------------------------------------------------

def test_backward_resolution_ac04_is_a_single_lookup(clean_db, django_assert_num_queries):
    """AC-04: "resolving its work item's specification link yields the
    originating artifact id and requirement id in a single lookup" —
    asserted literally: exactly one SELECT (a join across work_item_artifact
    -> work_item -> work_item_specification_link via select_related), plus
    the one access-log INSERT every read in this service performs
    (internal-work-item-service.md REQ-04) — two queries total, neither of
    them a second SELECT."""
    artifact = make_artifact()
    item = make_work_item()
    store.record_specification_link(item.id, artifact.id, 'REQ-18')
    delivery = store.attach_artifact(item.id, 'commit', 'deadbeef')

    with django_assert_num_queries(2):
        result = readstore.get_specification_link_for_delivery_artifact(delivery['id'])

    assert result == {
        'workItemId': str(item.id), 'deliveryArtifactId': delivery['id'],
        'specArtifactId': str(artifact.id), 'requirementId': 'REQ-18',
    }


def test_backward_resolution_missing_association_returns_none(clean_db):
    assert readstore.get_specification_link_for_delivery_artifact(uuid.uuid4()) is None


def test_backward_resolution_association_without_specification_link(clean_db):
    """A delivery association exists, but its work item never recorded a
    specification link — a real, distinct outcome from "no such
    association" (see readstore.get_specification_link_for_delivery_artifact's
    own docstring)."""
    item = make_work_item()
    delivery = store.attach_artifact(item.id, 'commit', 'deadbeef')

    result = readstore.get_specification_link_for_delivery_artifact(delivery['id'])
    assert result['workItemId'] == str(item.id)
    assert result['specArtifactId'] is None
    assert result['requirementId'] is None


def test_forward_query_req06_enumerates_every_work_item_recording_the_link(clean_db):
    artifact = make_artifact()
    other_artifact = make_artifact()

    matching_1 = make_work_item()
    matching_2 = make_work_item()
    non_matching_artifact = make_work_item()
    non_matching_requirement = make_work_item()

    store.record_specification_link(matching_1.id, artifact.id, 'REQ-18')
    store.record_specification_link(matching_2.id, artifact.id, 'REQ-18')
    store.record_specification_link(non_matching_artifact.id, other_artifact.id, 'REQ-18')
    store.record_specification_link(non_matching_requirement.id, artifact.id, 'REQ-99')

    rows = readstore.list_work_items(project=PROJECT, spec_artifact_id=str(artifact.id), requirement_id='REQ-18')
    assert {r.id for r in rows} == {matching_1.id, matching_2.id}
