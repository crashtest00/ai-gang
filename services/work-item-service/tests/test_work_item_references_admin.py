"""
work-items.md REQ-01/REQ-02 via the Django admin — the human write path
the product owner asked for ("a human [should] be able to link artifacts
to a story when writing it in the Django admin"). Mirrors test_admin.py's
own approach: real admin views through a logged-in Django test Client, no
mocks.

REQ-04's resolution check is exercised here through Django's own ModelForm
FK validation (the same schema-level foreign key models.py documents) as
well as store.py's own pre-check — see workitems/admin.py's module
docstring for the REQ-03/REQ-08 tension this write path raises.
"""

from __future__ import annotations

import uuid

from workitems import store
from workitems.models import WorkItemArtifactLink, WorkItemSpecificationLink

from tests.artifacts_support import admin_client
from tests.test_work_item_references_store import make_artifact, make_work_item

PROJECT = 'test-project'


def test_admin_can_record_a_specification_link_for_a_story(clean_db):
    client = admin_client('spec-link-admin')
    artifact = make_artifact()
    item = make_work_item(project=PROJECT, type='story', status='ready', storyDetail={
        'behavior': 'b', 'acceptanceCriteria': 'ac', 'constraints': 'c', 'edgeCases': 'e', 'outOfScope': 'oos',
    })

    resp = client.post('/django-admin/workitems/workitemspecificationlink/add/', data={
        'work_item': str(item.id), 'artifact': str(artifact.id), 'requirement_id': 'REQ-18',
    })
    assert resp.status_code == 302, getattr(resp, 'context', None) and resp.context['adminform'].form.errors.as_text()

    link = WorkItemSpecificationLink.objects.get(work_item_id=item.id)
    assert link.artifact_id == artifact.id
    assert link.requirement_id == 'REQ-18'


def test_admin_specification_link_rejects_unresolved_artifact_req04(clean_db):
    """The bogus id never reaches store.py at all here — Django's own
    ModelForm validation on the real `artifact` foreign key rejects it
    before save_model runs, redisplaying the form rather than a 500."""
    client = admin_client('spec-link-admin-2')
    item = make_work_item(project=PROJECT)
    bogus_artifact_id = uuid.uuid4()

    resp = client.post('/django-admin/workitems/workitemspecificationlink/add/', data={
        'work_item': str(item.id), 'artifact': str(bogus_artifact_id), 'requirement_id': 'REQ-18',
    })
    assert resp.status_code == 200, 'an unresolved artifact must redisplay the form, not redirect'
    assert not WorkItemSpecificationLink.objects.filter(work_item_id=item.id).exists()


def test_admin_can_add_an_artifact_link(clean_db):
    client = admin_client('artifact-link-admin')
    artifact = make_artifact()
    item = make_work_item(project=PROJECT)

    resp = client.post('/django-admin/workitems/workitemartifactlink/add/', data={
        'work_item': str(item.id), 'artifact': str(artifact.id),
    })
    assert resp.status_code == 302, getattr(resp, 'context', None) and resp.context['adminform'].form.errors.as_text()

    link = WorkItemArtifactLink.objects.get(work_item_id=item.id)
    assert link.artifact_id == artifact.id
    assert link.position == 0


def test_admin_artifact_link_rejects_unresolved_artifact_req04(clean_db):
    client = admin_client('artifact-link-admin-2')
    item = make_work_item(project=PROJECT)

    resp = client.post('/django-admin/workitems/workitemartifactlink/add/', data={
        'work_item': str(item.id), 'artifact': str(uuid.uuid4()),
    })
    assert resp.status_code == 200
    assert not WorkItemArtifactLink.objects.filter(work_item_id=item.id).exists()


def test_admin_duplicate_artifact_link_is_rejected_at_form_validation(clean_db):
    """Submitting the same (work_item, artifact) pair twice through the
    admin never reaches store.add_artifact_link's own idempotent-dedupe
    branch: WorkItemArtifactLink's (work_item, artifact) UniqueConstraint
    (models.py) makes Django's ModelForm reject the second submission
    during form validation — the same "genuine duplicate redisplays the
    form with a field error" shape
    test_admin_add_work_item_with_duplicate_external_key_is_rejected
    already proves for external_key's own unique constraint."""
    client = admin_client('artifact-link-admin-3')
    artifact = make_artifact()
    item = make_work_item(project=PROJECT)
    store.add_artifact_link(item.id, artifact.id)

    resp = client.post('/django-admin/workitems/workitemartifactlink/add/', data={
        'work_item': str(item.id), 'artifact': str(artifact.id),
    })
    assert resp.status_code == 200, 'a genuine duplicate must redisplay the form with a field error, not redirect'
    assert WorkItemArtifactLink.objects.filter(work_item_id=item.id, artifact_id=artifact.id).count() == 1


def test_admin_artifact_link_immutable_once_recorded(clean_db):
    """Matches WorkItemArtifactAdmin's own precedent: has_change_permission
    is False, so a superuser (whose has_view_permission is independently
    True — Django checks the user's view/change perms directly, not this
    method) sees the change page rendered READ-ONLY rather than editable.
    Confirmed by the absence of a save button, the same signal
    test_admin_status_transition_on_jira_mode_project_is_rejected uses for
    a read-only field."""
    client = admin_client('artifact-link-admin-4')
    artifact = make_artifact()
    item = make_work_item(project=PROJECT)
    result = store.add_artifact_link(item.id, artifact.id)

    resp = client.get(f"/django-admin/workitems/workitemartifactlink/{result['id']}/change/")
    assert resp.status_code == 200
    assert b'name="_save"' not in resp.content, 'an immutable association must not offer a save action'


def test_admin_work_item_change_page_shows_both_references_read_only(clean_db):
    """Visibility on the work item's own change page — read-only inlines,
    same split as WorkItemArtifactInline/WorkItemCommentInline: the actual
    write happens through the dedicated admins above."""
    client = admin_client('visibility-admin')
    artifact = make_artifact()
    other_artifact = make_artifact()
    item = make_work_item(project=PROJECT)
    store.record_specification_link(item.id, artifact.id, 'REQ-18')
    store.add_artifact_link(item.id, other_artifact.id)

    resp = client.get(f'/django-admin/workitems/workitem/{item.id}/change/')
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'REQ-18' in html
    assert str(other_artifact.id) in html or str(other_artifact.id)[:8] in html


def test_admin_add_work_item_form_excludes_specification_link_inline(clean_db):
    """Regression-shaped test mirroring
    test_admin_add_form_excludes_story_and_release_detail_inlines: the
    specification link is the SAME OneToOneField(primary_key=True) shape
    as story_detail/release_detail, and must be hidden on add for the
    identical reason (WorkItemAdmin.get_inlines)."""
    client = admin_client('exclude-admin')
    resp = client.get('/django-admin/workitems/workitem/add/')
    assert resp.status_code == 200
    assert 'specification_link-TOTAL_FORMS' not in resp.content.decode()


def test_admin_add_work_item_form_includes_artifact_link_inline(clean_db):
    """The mirror image of the above: artifact links are a normal FK
    (like artifacts/comments), so unlike specification_link they DO render
    on the add form."""
    client = admin_client('include-admin')
    resp = client.get('/django-admin/workitems/workitem/add/')
    assert resp.status_code == 200
    assert 'artifact_links-TOTAL_FORMS' in resp.content.decode()
