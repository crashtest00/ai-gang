"""
The Django admin UI — the concrete capability this rebuild exists to
add (the Node build's own tracked gap: "Node/Express doesn't get one for
free"). These tests exercise the ACTUAL admin views (not just admin.py's
configuration) through a logged-in Django test Client, confirming that an
admin-UI-originated write still goes through store.py's write-gate/
history/outbox path exactly like every other write.
"""

from __future__ import annotations

import uuid

from django.contrib.auth import get_user_model
from django.test import Client

from workitems import admin as admin_module, project_config, store, write_gate
from workitems.models import AccessLog, OutboxEvent, WorkItem, WorkItemHistory

PROJECT = 'admin-test-project'


def _admin_client(db_dep) -> Client:
    User = get_user_model()
    User.objects.create_superuser('admin', 'admin@example.com', 'password123')
    client = Client()
    assert client.login(username='admin', password='password123')
    return client


def test_admin_login_and_changelist_render(clean_db):
    client = _admin_client(clean_db)
    resp = client.get('/django-admin/workitems/workitem/')
    assert resp.status_code == 200


def test_admin_add_work_item_routes_through_store_and_produces_history_and_outbox(clean_db):
    client = _admin_client(clean_db)
    item_id = uuid.uuid4()

    resp = client.post('/django-admin/workitems/workitem/add/', data={
        'id': str(item_id),
        'project': PROJECT,
        'type': 'task',
        'display_name': 'Created via admin',
        'description': '',
        'status': 'proposed',
        'assignee_agent_id': '',
        'priority': '0',
        'writes_files': '',
        'writes_services': '',
        'parent': '',
        'external_key': '',
        # inline formset management forms (empty — none added)
        'story_detail-TOTAL_FORMS': '0', 'story_detail-INITIAL_FORMS': '0',
        'story_detail-MIN_NUM_FORMS': '0', 'story_detail-MAX_NUM_FORMS': '1',
        'release_detail-TOTAL_FORMS': '0', 'release_detail-INITIAL_FORMS': '0',
        'release_detail-MIN_NUM_FORMS': '0', 'release_detail-MAX_NUM_FORMS': '1',
        'links_from-TOTAL_FORMS': '0', 'links_from-INITIAL_FORMS': '0',
        'links_from-MIN_NUM_FORMS': '0', 'links_from-MAX_NUM_FORMS': '1000',
        'links_to-TOTAL_FORMS': '0', 'links_to-INITIAL_FORMS': '0',
        'links_to-MIN_NUM_FORMS': '0', 'links_to-MAX_NUM_FORMS': '1000',
        'artifacts-TOTAL_FORMS': '0', 'artifacts-INITIAL_FORMS': '0',
        'artifacts-MIN_NUM_FORMS': '0', 'artifacts-MAX_NUM_FORMS': '1000',
        'comments-TOTAL_FORMS': '0', 'comments-INITIAL_FORMS': '0',
        'comments-MIN_NUM_FORMS': '0', 'comments-MAX_NUM_FORMS': '1000',
        'history-TOTAL_FORMS': '0', 'history-INITIAL_FORMS': '0',
        'history-MIN_NUM_FORMS': '0', 'history-MAX_NUM_FORMS': '1000',
        'artifact_links-TOTAL_FORMS': '0', 'artifact_links-INITIAL_FORMS': '0',
        'artifact_links-MIN_NUM_FORMS': '0', 'artifact_links-MAX_NUM_FORMS': '1000',
    })
    assert resp.status_code == 302, f'expected a redirect after a successful add, got {resp.status_code}: {getattr(resp, "context", None) and resp.context["errors"] if hasattr(resp, "context") else ""}'

    item = WorkItem.objects.get(id=item_id)
    assert item.display_name == 'Created via admin'
    assert item.status == 'proposed'

    assert WorkItemHistory.objects.filter(work_item_id=item_id, field='status').exists()
    assert OutboxEvent.objects.filter(work_item_id=item_id, event_type='work_item.created').exists()


def test_admin_add_work_item_uses_admin_ui_origin(clean_db, monkeypatch):
    """Origin split: a write made through the real, session-
    authenticated Django admin must reach store.py as Origins.ADMIN_UI,
    never Origins.EXTERNAL_API (reserved for views.py's unauthenticated
    HTTP handlers)."""
    captured = {}
    real_create = store.create_work_item

    def spy(payload, *, actor, origin):
        captured['origin'] = origin
        return real_create(payload, actor=actor, origin=origin)

    monkeypatch.setattr(admin_module.store, 'create_work_item', spy)

    client = _admin_client(clean_db)
    item_id = uuid.uuid4()
    resp = client.post('/django-admin/workitems/workitem/add/', data={
        'id': str(item_id), 'project': PROJECT, 'type': 'task', 'display_name': 'Created via admin',
        'description': '', 'status': 'proposed', 'assignee_agent_id': '', 'priority': '0',
        'writes_files': '', 'writes_services': '', 'parent': '', 'external_key': '',
        'story_detail-TOTAL_FORMS': '0', 'story_detail-INITIAL_FORMS': '0',
        'story_detail-MIN_NUM_FORMS': '0', 'story_detail-MAX_NUM_FORMS': '1',
        'release_detail-TOTAL_FORMS': '0', 'release_detail-INITIAL_FORMS': '0',
        'release_detail-MIN_NUM_FORMS': '0', 'release_detail-MAX_NUM_FORMS': '1',
        'links_from-TOTAL_FORMS': '0', 'links_from-INITIAL_FORMS': '0',
        'links_from-MIN_NUM_FORMS': '0', 'links_from-MAX_NUM_FORMS': '1000',
        'links_to-TOTAL_FORMS': '0', 'links_to-INITIAL_FORMS': '0',
        'links_to-MIN_NUM_FORMS': '0', 'links_to-MAX_NUM_FORMS': '1000',
        'artifacts-TOTAL_FORMS': '0', 'artifacts-INITIAL_FORMS': '0',
        'artifacts-MIN_NUM_FORMS': '0', 'artifacts-MAX_NUM_FORMS': '1000',
        'comments-TOTAL_FORMS': '0', 'comments-INITIAL_FORMS': '0',
        'comments-MIN_NUM_FORMS': '0', 'comments-MAX_NUM_FORMS': '1000',
        'history-TOTAL_FORMS': '0', 'history-INITIAL_FORMS': '0',
        'history-MIN_NUM_FORMS': '0', 'history-MAX_NUM_FORMS': '1000',
        'artifact_links-TOTAL_FORMS': '0', 'artifact_links-INITIAL_FORMS': '0',
        'artifact_links-MIN_NUM_FORMS': '0', 'artifact_links-MAX_NUM_FORMS': '1000',
    })
    assert resp.status_code == 302
    assert captured.get('origin') == write_gate.Origins.ADMIN_UI


_EMPTY_FORMSETS = {
    'links_from-TOTAL_FORMS': '0', 'links_from-INITIAL_FORMS': '0',
    'links_from-MIN_NUM_FORMS': '0', 'links_from-MAX_NUM_FORMS': '1000',
    'links_to-TOTAL_FORMS': '0', 'links_to-INITIAL_FORMS': '0',
    'links_to-MIN_NUM_FORMS': '0', 'links_to-MAX_NUM_FORMS': '1000',
    'artifacts-TOTAL_FORMS': '0', 'artifacts-INITIAL_FORMS': '0',
    'artifacts-MIN_NUM_FORMS': '0', 'artifacts-MAX_NUM_FORMS': '1000',
    'comments-TOTAL_FORMS': '0', 'comments-INITIAL_FORMS': '0',
    'comments-MIN_NUM_FORMS': '0', 'comments-MAX_NUM_FORMS': '1000',
    'history-TOTAL_FORMS': '0', 'history-INITIAL_FORMS': '0',
    'history-MIN_NUM_FORMS': '0', 'history-MAX_NUM_FORMS': '1000',
    # work-items.md REQ-02 — WorkItemArtifactLinkInline (read-only, see
    # admin.py); present on both add and change (a normal FK, not a
    # same-table pk like story_detail/release_detail/specification_link).
    'artifact_links-TOTAL_FORMS': '0', 'artifact_links-INITIAL_FORMS': '0',
    'artifact_links-MIN_NUM_FORMS': '0', 'artifact_links-MAX_NUM_FORMS': '1000',
}

# work-items.md REQ-01 — WorkItemSpecificationLinkInline's management form.
# Hidden on ADD (get_inlines) for the same 1:1-pk-corruption reason as
# story_detail/release_detail, so only CHANGE-view POSTs need this.
_SPECIFICATION_LINK_EMPTY_FORMSET = {
    'specification_link-TOTAL_FORMS': '0', 'specification_link-INITIAL_FORMS': '0',
    'specification_link-MIN_NUM_FORMS': '0', 'specification_link-MAX_NUM_FORMS': '1',
}


def test_admin_add_form_excludes_story_and_release_detail_inlines(clean_db):
    """Regression test for the bug `test_admin_edit_saves_story_detail_via_inline`
    et al.'s docstrings describe: WorkItemAdmin.get_inlines hides
    WorkItemStoryDetailInline/WorkItemReleaseDetailInline on add (obj is
    None) precisely so their formsets are never built against a
    not-yet-real parent id. Confirms the add page itself doesn't render
    them, independent of whatever a POST to it does."""
    client = _admin_client(clean_db)
    resp = client.get('/django-admin/workitems/workitem/add/')
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'story_detail-TOTAL_FORMS' not in html
    assert 'release_detail-TOTAL_FORMS' not in html


def test_admin_add_then_edit_saves_story_detail_via_inline(clean_db):
    """The bug this replaces (see git history): filling in story_detail on
    the ADD form corrupted the parent's id, because Django builds every
    inline formset from the parent's PRE-validation instance —
    WorkItemAdmin.get_inlines now hides these two inlines on add for
    exactly that reason (see its docstring). This is the correct two-step
    flow: add the work item first (no detail inlines involved at all),
    then edit it — by which point it has a real, saved pk, so the same
    inline mechanism that broke on add works correctly on change."""
    client = _admin_client(clean_db)
    item_id = uuid.uuid4()

    add_resp = client.post('/django-admin/workitems/workitem/add/', data={
        'id': str(item_id), 'project': PROJECT, 'type': 'story', 'display_name': 'A story',
        'description': '', 'status': 'proposed', 'assignee_agent_id': '', 'priority': '0',
        'writes_files': '', 'writes_services': '', 'parent': '', 'external_key': '',
        **_EMPTY_FORMSETS,
    })
    assert add_resp.status_code == 302, f'expected a redirect after add, got {add_resp.status_code}'
    assert WorkItem.objects.filter(id=item_id).exists(), 'the work item must be created under the SUBMITTED id'

    edit_resp = client.post(f'/django-admin/workitems/workitem/{item_id}/change/', data={
        'id': str(item_id), 'project': PROJECT, 'type': 'story', 'display_name': 'A story',
        'description': '', 'status': 'proposed', 'assignee_agent_id': '', 'priority': '0',
        'writes_files': '', 'writes_services': '', 'parent': '', 'external_key': '',
        'story_detail-TOTAL_FORMS': '1', 'story_detail-INITIAL_FORMS': '0',
        'story_detail-MIN_NUM_FORMS': '0', 'story_detail-MAX_NUM_FORMS': '1',
        'story_detail-0-behavior': 'b', 'story_detail-0-acceptance_criteria': 'ac',
        'story_detail-0-constraints': 'c', 'story_detail-0-edge_cases': 'e',
        'story_detail-0-out_of_scope': 'oos', 'story_detail-0-value_hypothesis': '',
        'story_detail-0-test_measurement': '',
        'release_detail-TOTAL_FORMS': '0', 'release_detail-INITIAL_FORMS': '0',
        'release_detail-MIN_NUM_FORMS': '0', 'release_detail-MAX_NUM_FORMS': '1',
        **_EMPTY_FORMSETS,
        **_SPECIFICATION_LINK_EMPTY_FORMSET,
    })
    assert edit_resp.status_code == 302, f'expected a redirect after edit, got {edit_resp.status_code}: {getattr(edit_resp, "context", None) and edit_resp.context.get("errors")}'

    from workitems.models import WorkItemStoryDetail
    detail = WorkItemStoryDetail.objects.get(work_item_id=item_id)
    assert detail.behavior == 'b'
    assert detail.acceptance_criteria == 'ac'

    # store.py's own module docstring: "Every function here is the ONLY
    # place canonical state is mutated... every caller goes through this
    # module, never the ORM directly." The inline formset still saves via
    # Django's default ModelAdmin.save_formset (a direct .save() on the
    # WorkItemStoryDetail instance) rather than through store.py — so this
    # write does not append to work_item_history or produce its own
    # outbox_event, unlike every other write in this module. Documenting
    # the actual behavior rather than asserting what history/outbox parity
    # would imply; retrofitting the inline to route through store.py is out of
    # this feature's scope.
    from workitems.models import OutboxEvent
    events = OutboxEvent.objects.filter(work_item_id=item_id)
    assert list(events.values_list('event_type', flat=True)) == ['work_item.created'], (
        'the inline story-detail save produced no outbox event of its own — bypasses store.py'
    )


def test_admin_add_then_edit_saves_release_detail_via_inline(clean_db):
    client = _admin_client(clean_db)
    item_id = uuid.uuid4()

    add_resp = client.post('/django-admin/workitems/workitem/add/', data={
        'id': str(item_id), 'project': PROJECT, 'type': 'release', 'display_name': 'A release',
        'description': '', 'status': 'proposed', 'assignee_agent_id': '', 'priority': '0',
        'writes_files': '', 'writes_services': '', 'parent': '', 'external_key': '',
        **_EMPTY_FORMSETS,
    })
    assert add_resp.status_code == 302, f'expected a redirect after add, got {add_resp.status_code}'
    assert WorkItem.objects.filter(id=item_id).exists(), 'the work item must be created under the SUBMITTED id'

    edit_resp = client.post(f'/django-admin/workitems/workitem/{item_id}/change/', data={
        'id': str(item_id), 'project': PROJECT, 'type': 'release', 'display_name': 'A release',
        'description': '', 'status': 'proposed', 'assignee_agent_id': '', 'priority': '0',
        'writes_files': '', 'writes_services': '', 'parent': '', 'external_key': '',
        'story_detail-TOTAL_FORMS': '0', 'story_detail-INITIAL_FORMS': '0',
        'story_detail-MIN_NUM_FORMS': '0', 'story_detail-MAX_NUM_FORMS': '1',
        'release_detail-TOTAL_FORMS': '1', 'release_detail-INITIAL_FORMS': '0',
        'release_detail-MIN_NUM_FORMS': '0', 'release_detail-MAX_NUM_FORMS': '1',
        'release_detail-0-release_notes': 'Notes here', 'release_detail-0-candidate_sha': '',
        'release_detail-0-build_identifier': '', 'release_detail-0-preview_url': '',
        **_EMPTY_FORMSETS,
        **_SPECIFICATION_LINK_EMPTY_FORMSET,
    })
    assert edit_resp.status_code == 302, f'expected a redirect after edit, got {edit_resp.status_code}: {getattr(edit_resp, "context", None) and edit_resp.context.get("errors")}'

    from workitems.models import WorkItemReleaseDetail
    detail = WorkItemReleaseDetail.objects.get(work_item_id=item_id)
    assert detail.release_notes == 'Notes here'


def test_admin_add_two_work_items_with_blank_external_key_both_succeed(clean_db):
    """Regression test: a TextField left blank on the admin's change form
    is submitted by the browser as '', not None — and '' is a value like
    any other for external_key's unique constraint, so a second work item
    added with the field left blank used to collide with the first. Both
    adds must now succeed, and the column must hold NULL, not ''."""
    client = _admin_client(clean_db)

    for _ in range(2):
        item_id = uuid.uuid4()
        resp = client.post('/django-admin/workitems/workitem/add/', data={
            'id': str(item_id), 'project': PROJECT, 'type': 'task', 'display_name': 'No external key',
            'description': '', 'status': 'proposed', 'assignee_agent_id': '', 'priority': '0',
            'writes_files': '', 'writes_services': '', 'parent': '', 'external_key': '',
            **_EMPTY_FORMSETS,
        })
        assert resp.status_code == 302, f'expected a redirect after add, got {resp.status_code}'
        item = WorkItem.objects.get(id=item_id)
        assert item.external_key is None, "a blank External key must be stored as NULL, not ''"


def test_admin_add_work_item_with_duplicate_external_key_is_rejected(clean_db):
    """A non-blank External key is still a real duplicate — normalizing
    blank to NULL must not weaken the unique constraint for an actual
    collision."""
    client = _admin_client(clean_db)
    store.create_work_item({'id': uuid.uuid4(), 'project': PROJECT, 'type': 'task', 'displayName': 'First',
                             'externalKey': 'DUP-1'})

    item_id = uuid.uuid4()
    resp = client.post('/django-admin/workitems/workitem/add/', data={
        'id': str(item_id), 'project': PROJECT, 'type': 'task', 'display_name': 'Second',
        'description': '', 'status': 'proposed', 'assignee_agent_id': '', 'priority': '0',
        'writes_files': '', 'writes_services': '', 'parent': '', 'external_key': 'DUP-1',
        **_EMPTY_FORMSETS,
    })
    assert resp.status_code == 200, 'a genuine duplicate key must redisplay the form with a field error, not redirect'
    assert b'already exists' in resp.content
    assert not WorkItem.objects.filter(id=item_id).exists()
    assert WorkItem.objects.filter(external_key='DUP-1').count() == 1


def test_admin_rejected_status_transition_redisplays_form_instead_of_500(clean_db):
    """Regression test: a gated write store.py rejects (here, a status
    transition to an unrecognized status) used to escape save_model as an
    uncaught exception and reach the operator as a bare 500. It must
    instead redirect back to the change form with the rejection reason
    flashed as a message, and must not apply the rejected change."""
    client = _admin_client(clean_db)
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})

    resp = client.post(f'/django-admin/workitems/workitem/{item_id}/change/', data={
        'id': str(item_id), 'project': PROJECT, 'type': 'task', 'display_name': 'X',
        'description': '', 'status': 'not-a-real-status', 'assignee_agent_id': '', 'priority': '0',
        'writes_files': '', 'writes_services': '', 'parent': '', 'external_key': '',
        'story_detail-TOTAL_FORMS': '0', 'story_detail-INITIAL_FORMS': '0',
        'story_detail-MIN_NUM_FORMS': '0', 'story_detail-MAX_NUM_FORMS': '1',
        'release_detail-TOTAL_FORMS': '0', 'release_detail-INITIAL_FORMS': '0',
        'release_detail-MIN_NUM_FORMS': '0', 'release_detail-MAX_NUM_FORMS': '1',
        **_EMPTY_FORMSETS,
        **_SPECIFICATION_LINK_EMPTY_FORMSET,
    })
    assert resp.status_code == 302, f'a rejected gated write must redirect, not 500 — got {resp.status_code}'

    item = WorkItem.objects.get(id=item_id)
    assert item.status == 'proposed', 'the rejected transition must not have been applied'

    followed = client.get(resp.headers['Location'])
    assert followed.status_code == 200
    assert b'is not one of the minimum canonical statuses' in followed.content


def test_admin_status_transition_on_jira_mode_project_is_rejected(clean_db):
    client = _admin_client(clean_db)
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})
    project_config.set_mode(PROJECT, 'jira')

    # Jira mode: status/assignee render read-only (get_readonly_fields), so
    # the admin change form must not accept a status edit at all — confirm
    # the field is excluded from the (successful) rendered form rather than
    # silently applied.
    resp = client.get(f'/django-admin/workitems/workitem/{item_id}/change/')
    assert resp.status_code == 200
    assert b'name="status"' not in resp.content, 'status must render read-only for a Jira-mode project'

    item = WorkItem.objects.get(id=item_id)
    assert item.status == 'proposed', 'no write should have been possible through the read-only field'
