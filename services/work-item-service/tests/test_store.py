"""
Mirrors services/work-item-service/test/store.test.js (Node reference
implementation) one-for-one: same scenarios, same assertions, ported onto
workitems/store.py + pytest.
"""

from __future__ import annotations

import uuid

import pytest

from workitems import project_config, store, write_gate
from workitems.models import OutboxEvent, WorkItemHistory

PROJECT = 'test-project'


def outbox_events_for(work_item_id):
    return list(OutboxEvent.objects.filter(work_item_id=work_item_id).order_by('created_at'))


def history_for(work_item_id):
    return list(WorkItemHistory.objects.filter(work_item_id=work_item_id).order_by('occurred_at'))


def test_create_work_item_req01_req05_req06(clean_db):
    item_id = uuid.uuid4()
    item = store.create_work_item(
        {'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'Do the thing', 'description': 'desc'},
        actor='tester',
    )
    assert item.id == item_id
    assert item.status == 'proposed'

    history = history_for(item_id)
    assert len(history) == 1
    assert history[0].field == 'status'
    assert history[0].new_value == 'proposed'

    events = outbox_events_for(item_id)
    assert len(events) == 1
    assert events[0].event_type == 'work_item.created'
    assert events[0].published_at is None


def test_create_work_item_req17_story_with_all_fields(clean_db):
    item_id = uuid.uuid4()
    item = store.create_work_item({
        'id': item_id, 'project': PROJECT, 'type': 'story', 'displayName': 'A story', 'status': 'ready',
        'storyDetail': {'behavior': 'b', 'acceptanceCriteria': 'ac', 'constraints': 'c', 'edgeCases': 'e', 'outOfScope': 'oos'},
    }, actor='tester')
    assert item.status == 'ready'


def test_create_work_item_req17_story_missing_field_rejected(clean_db):
    item_id = uuid.uuid4()
    with pytest.raises(store.ValidationError, match='(?i)Acceptance'):
        store.create_work_item({
            'id': item_id, 'project': PROJECT, 'type': 'story', 'displayName': 'Incomplete story', 'status': 'ready',
            'storyDetail': {'behavior': 'b', 'acceptanceCriteria': '', 'constraints': 'c', 'edgeCases': 'e', 'outOfScope': 'oos'},
        }, actor='tester')


def test_create_work_item_rejects_unknown_status(clean_db):
    with pytest.raises(store.ValidationError):
        store.create_work_item({'id': uuid.uuid4(), 'project': PROJECT, 'type': 'task', 'displayName': 'X', 'status': 'bogus'})


def test_create_work_item_accepts_declared_custom_status(clean_db):
    project_config.declare_custom_status(PROJECT, 'in-qa', 'in-review', 'QA Review')
    item_id = uuid.uuid4()
    item = store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X', 'status': 'in-qa'})
    assert item.status == 'in-qa'


def test_assign_work_item_req03_accepts_valid_agent(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})
    item = store.assign_work_item(item_id, 'backend-agent', actor='tester')
    assert item.assignee_agent_id == 'backend-agent'

    history = history_for(item_id)
    assign_entry = next(h for h in history if h.field == 'assignee_agent_id')
    assert assign_entry.new_value == 'backend-agent'


def test_assign_work_item_rejects_agent_not_permitted_for_project(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})
    with pytest.raises(store.AssignmentRejectedError):
        store.assign_work_item(item_id, 'frontend-agent', actor='tester')  # not enabled for test-project


def test_assign_work_item_rejects_unknown_catalog_agent(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})
    with pytest.raises(store.AssignmentRejectedError):
        store.assign_work_item(item_id, 'ghost-agent', actor='tester')


def test_transition_status_req04_dependency_gate(clean_db):
    blocker = uuid.uuid4()
    dependent = uuid.uuid4()
    store.create_work_item({'id': blocker, 'project': PROJECT, 'type': 'task', 'displayName': 'Blocker'})
    store.create_work_item({'id': dependent, 'project': PROJECT, 'type': 'task', 'displayName': 'Dependent'})
    store.create_link(blocker, dependent, 'blocks', actor='tester')

    with pytest.raises(store.DependencyGateError):
        store.transition_status(dependent, 'ready', actor='tester')

    store.transition_status(blocker, 'in-progress', actor='tester')
    store.transition_status(blocker, 'done', actor='tester')

    item = store.transition_status(dependent, 'ready', actor='tester')
    assert item.status == 'ready'


def test_create_link_idempotent(clean_db):
    a = uuid.uuid4()
    b = uuid.uuid4()
    store.create_work_item({'id': a, 'project': PROJECT, 'type': 'task', 'displayName': 'A'})
    store.create_work_item({'id': b, 'project': PROJECT, 'type': 'task', 'displayName': 'B'})

    first = store.create_link(a, b, 'blocks', actor='tester')
    second = store.create_link(a, b, 'blocks', actor='tester')
    assert first['deduped'] is False
    assert second['deduped'] is True
    assert first['id'] == second['id']

    from workitems.models import WorkItemLink
    assert WorkItemLink.objects.filter(from_work_item_id=a).count() == 1


def test_req20_parent_rolls_up_to_done_only_when_all_children_done(clean_db):
    parent = uuid.uuid4()
    c1, c2, c3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    story_detail = {'behavior': 'b', 'acceptanceCriteria': 'ac', 'constraints': 'c', 'edgeCases': 'e', 'outOfScope': 'oos'}
    store.create_work_item({'id': parent, 'project': PROJECT, 'type': 'story', 'displayName': 'Parent',
                             'status': 'in-progress', 'storyDetail': story_detail})
    for c in (c1, c2, c3):
        store.create_work_item({'id': c, 'project': PROJECT, 'type': 'task', 'displayName': str(c),
                                 'status': 'in-progress', 'parentId': parent})

    from workitems.models import WorkItem

    store.transition_status(c1, 'done', actor='tester')
    assert WorkItem.objects.get(id=parent).status == 'in-progress'

    store.transition_status(c2, 'done', actor='tester')
    store.transition_status(c3, 'done', actor='tester')

    assert WorkItem.objects.get(id=parent).status == 'done'

    events = outbox_events_for(parent)
    assert any(e.event_type == 'work_item.status_changed' for e in events)


def test_req20_terminal_non_done_child_holds_parent(clean_db):
    parent = uuid.uuid4()
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    story_detail = {'behavior': 'b', 'acceptanceCriteria': 'ac', 'constraints': 'c', 'edgeCases': 'e', 'outOfScope': 'oos'}
    store.create_work_item({'id': parent, 'project': PROJECT, 'type': 'story', 'displayName': 'Parent',
                             'status': 'in-progress', 'storyDetail': story_detail})
    store.create_work_item({'id': c1, 'project': PROJECT, 'type': 'task', 'displayName': str(c1),
                             'status': 'in-progress', 'parentId': parent})
    store.create_work_item({'id': c2, 'project': PROJECT, 'type': 'task', 'displayName': str(c2),
                             'status': 'in-progress', 'parentId': parent})

    store.transition_status(c1, 'cancelled', actor='tester')

    from workitems.models import WorkItem
    assert WorkItem.objects.get(id=parent).status == 'in-progress'


def test_attach_artifact_req19_completion_marker_idempotent(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})

    first = store.record_completion_marker(item_id, 'evidence:beta-promoted', 'build-42', actor='jenkins')
    assert first['alreadyMarked'] is False

    second = store.record_completion_marker(item_id, 'evidence:beta-promoted', 'build-42', actor='jenkins')
    assert second['alreadyMarked'] is True

    from workitems.models import WorkItemArtifact
    assert WorkItemArtifact.objects.filter(work_item_id=item_id, artifact_type='evidence:beta-promoted').count() == 1


def test_append_comment_req18_idempotent_redelivery(clean_db):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})

    first = store.append_comment(item_id, 'backend-agent', 'PR opened', source_message_id='msg-1')
    second = store.append_comment(item_id, 'backend-agent', 'PR opened', source_message_id='msg-1')
    assert first['id'] == second['id']

    from workitems.models import WorkItemComment
    assert WorkItemComment.objects.filter(work_item_id=item_id).count() == 1


def test_req10_write_gate_rejects_direct_write_in_jira_mode(clean_db):
    jira_project = 'jira-project'
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': jira_project, 'type': 'task', 'displayName': 'X'})
    project_config.set_mode(jira_project, 'jira')

    with pytest.raises(write_gate.WriteGateRejectedError):
        store.transition_status(item_id, 'in-progress', actor='tester', origin=write_gate.Origins.DIRECT)

    # The same change, arriving as a validated Jira-originated event, succeeds.
    item = store.transition_status(item_id, 'in-progress', actor='jira-webhook', origin=write_gate.Origins.JIRA_WEBHOOK)
    assert item.status == 'in-progress'


def _make_release(clean_db, project=PROJECT):
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': project, 'type': 'release', 'displayName': 'Release'})
    return item_id


def test_release_candidate_cut_req04_publishes_mode_agnostic_release_event(clean_db):
    release_id = _make_release(clean_db)
    item = store.transition_status(release_id, 'in-review', actor='tester')
    assert item.status == 'in-review'

    events = outbox_events_for(release_id)
    release_events = [e for e in events if e.event_type == store.RELEASE_EVENT_TYPE]
    assert len(release_events) == 1
    assert release_events[0].payload == {'kind': 'requested', 'workItemId': str(release_id), 'project': PROJECT}
    assert 'jiraIssueKey' not in release_events[0].payload


def test_release_candidate_cut_req03_rejected_when_beta_queue_dirty(clean_db):
    outstanding = uuid.uuid4()
    story_detail = {'behavior': 'b', 'acceptanceCriteria': 'ac', 'constraints': 'c', 'edgeCases': 'e', 'outOfScope': 'oos'}
    store.create_work_item({'id': outstanding, 'project': PROJECT, 'type': 'story', 'displayName': 'Awaiting review',
                             'status': 'in-review', 'storyDetail': story_detail})
    release_id = _make_release(clean_db)

    with pytest.raises(store.ReleaseGateError) as exc_info:
        store.transition_status(release_id, 'in-review', actor='tester')
    assert exc_info.value.outstanding == [str(outstanding)]

    item = store.get_work_item(release_id)
    assert item.status == 'proposed'  # rejected — never transitioned
    assert not any(e.event_type == store.RELEASE_EVENT_TYPE for e in outbox_events_for(release_id))

    from workitems.models import WorkItemComment
    comment = WorkItemComment.objects.get(work_item_id=release_id)
    assert 'Awaiting review' in comment.body
    assert str(outstanding) in comment.body


def test_release_candidate_cut_ignores_other_releases_in_review(clean_db):
    """A different Release work item sitting in 'in-review' must not itself
    count as outstanding beta-queue work — the Jira-mode-mirrored
    query explicitly excludes `issuetype != Release`."""
    other_release = _make_release(clean_db)
    store.transition_status(other_release, 'in-review', actor='tester')

    release_id = _make_release(clean_db)
    item = store.transition_status(release_id, 'in-review', actor='tester')
    assert item.status == 'in-review'


def test_release_done_req05_publishes_release_event_kind_done(clean_db):
    release_id = _make_release(clean_db)
    store.transition_status(release_id, 'in-review', actor='tester')
    item = store.transition_status(release_id, 'done', actor='tester')
    assert item.status == 'done'

    kinds = [e.payload['kind'] for e in outbox_events_for(release_id) if e.event_type == store.RELEASE_EVENT_TYPE]
    assert kinds == ['requested', 'done']


def test_release_abandoned_req06_publishes_release_event_kind_abandoned(clean_db):
    release_id = _make_release(clean_db)
    store.transition_status(release_id, 'in-review', actor='tester')
    item = store.transition_status(release_id, 'cancelled', actor='tester')
    assert item.status == 'cancelled'

    kinds = [e.payload['kind'] for e in outbox_events_for(release_id) if e.event_type == store.RELEASE_EVENT_TYPE]
    assert kinds == ['requested', 'abandoned']


def test_record_release_candidate_req04_writeback_is_write_once_per_candidate(clean_db):
    release_id = _make_release(clean_db)
    store.transition_status(release_id, 'in-review', actor='tester')

    item = store.record_release_candidate(
        release_id, candidate_sha='abc123', build_identifier='build-1', preview_url='https://preview.example/abc123',
        actor='jenkins',
    )
    detail = item.release_detail
    assert detail.candidate_sha == 'abc123'
    assert detail.build_identifier == 'build-1'
    assert detail.preview_url == 'https://preview.example/abc123'

    from workitems.models import WorkItemComment
    assert WorkItemComment.objects.filter(work_item_id=release_id, body__icontains='abc123').exists()

    # A second candidate cut replaces, not appends.
    store.record_release_candidate(release_id, candidate_sha='def456', actor='jenkins')
    detail.refresh_from_db()
    assert detail.candidate_sha == 'def456'
    assert detail.build_identifier is None


def test_req10_write_gate_does_not_gate_comments_or_artifacts(clean_db):
    jira_project = 'jira-project-2'
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': jira_project, 'type': 'task', 'displayName': 'X'})
    project_config.set_mode(jira_project, 'jira')

    # Neither of these should raise WriteGateRejectedError.
    store.append_comment(item_id, 'human', 'note')
    store.attach_artifact(item_id, 'commit', 'abc123')
