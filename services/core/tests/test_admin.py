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


_INLINE_PREFIXES = (
    ('story_detail', '1'), ('release_detail', '1'), ('links_from', '1000'), ('links_to', '1000'),
    ('artifacts', '1000'), ('comments', '1000'), ('history', '1000'),
    ('specification_link', '1'), ('artifact_links', '1000'),
)


def _work_item_form(item_id, overrides=None) -> dict:
    """The work-item change form's full POST body for an existing item,
    with `overrides` applied — every field the form renders plus each
    inline's management form, which Django requires on POST whether or not
    the inline accepts input."""
    item = WorkItem.objects.get(id=item_id)
    data = {
        'id': str(item.id),
        'project': item.project,
        'type': item.type,
        'display_name': item.display_name,
        'description': item.description or '',
        'status': item.status,
        'assignee_agent_id': item.assignee_agent_id or '',
        'priority': str(item.priority),
        'writes_files': item.writes_files or '',
        'writes_services': item.writes_services or '',
        'parent': str(item.parent_id) if item.parent_id else '',
        'external_key': item.external_key or '',
    }
    for prefix, max_num in _INLINE_PREFIXES:
        data.update({
            f'{prefix}-TOTAL_FORMS': '0', f'{prefix}-INITIAL_FORMS': '0',
            f'{prefix}-MIN_NUM_FORMS': '0', f'{prefix}-MAX_NUM_FORMS': max_num,
        })
    data.update(overrides or {})
    return data


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


def test_admin_status_transition_on_jira_mode_project_is_refused_on_save(clean_db):
    """canonical-delivery-state.md REQ-09 — the fields look editable and
    the refusal comes on SAVE (Pass 4 decision 5.2). v5.1's
    `get_readonly_fields` rendered `status` and `assignee_agent_id`
    read-only in Jira mode, which is the rule REQ-09 removes: showing two
    fields read-only while every other field on the page is refused anyway
    told an operator less than one clear refusal does, and it cost the
    admin a `get_mode` call outside the mode layer."""
    client = _admin_client(clean_db)
    item_id = uuid.uuid4()
    store.create_work_item({'id': item_id, 'project': PROJECT, 'type': 'task', 'displayName': 'X'})
    project_config.set_mode(PROJECT, 'jira')

    resp = client.get(f'/django-admin/workitems/workitem/{item_id}/change/')
    assert resp.status_code == 200
    assert b'name="status"' in resp.content, 'the field renders editable; the refusal comes on save'

    resp = client.post(f'/django-admin/workitems/workitem/{item_id}/change/', _work_item_form(
        item_id, {'status': 'in-progress'},
    ), follow=True)
    assert resp.status_code == 200
    assert b'Jira mode' in resp.content, resp.content[-2000:]

    item = WorkItem.objects.get(id=item_id)
    assert item.status == 'proposed', 'the save was rolled back whole'


# ---------------------------------------------------------------------------
# v5.2 — canonical-delivery-state.md REQ-09, "The admin is a person's edit
# interface, so in Jira mode it changes nothing". Driven through the real
# admin views, so each assertion is about what an operator sees.
# ---------------------------------------------------------------------------

def _make_item(project=PROJECT, **extra):
    item_id = uuid.uuid4()
    payload = {'id': item_id, 'project': project, 'type': 'task', 'displayName': 'X'}
    payload.update(extra)
    store.create_work_item(payload)
    return item_id


def test_a_jira_mode_non_gated_edit_is_refused_too_not_only_status_and_assignment(clean_db):
    """`NON_GATED_FIELDS` and `parent` are a raw ORM save with no handler to
    ask the router for them, so REQ-09 refuses them in the admin itself."""
    client = _admin_client(clean_db)
    item_id = _make_item()
    project_config.set_mode(PROJECT, 'jira')

    resp = client.post(f'/django-admin/workitems/workitem/{item_id}/change/',
                        _work_item_form(item_id, {'display_name': 'Renamed by hand'}), follow=True)

    assert resp.status_code == 200
    assert b'Jira mode' in resp.content
    assert WorkItem.objects.get(id=item_id).display_name == 'X'


def test_a_jira_mode_work_item_delete_is_refused_and_deletes_nothing(clean_db):
    client = _admin_client(clean_db)
    item_id = _make_item()
    project_config.set_mode(PROJECT, 'jira')

    resp = client.post(f'/django-admin/workitems/workitem/{item_id}/delete/', {'post': 'yes'}, follow=True)

    assert resp.status_code == 200
    assert b'Jira mode' in resp.content
    assert WorkItem.objects.filter(id=item_id).exists()


def test_a_bulk_delete_mixing_local_and_jira_mode_rows_is_refused_whole(clean_db):
    """REQ-09 — "A bulk delete that selects any Jira-mode row is refused
    whole": a half-applied bulk delete is worse than a refused one."""
    client = _admin_client(clean_db)
    local_id = _make_item(project='local-only-project')
    jira_id = _make_item()
    project_config.set_mode(PROJECT, 'jira')

    resp = client.post('/django-admin/workitems/workitem/', {
        'action': 'delete_selected', '_selected_action': [str(local_id), str(jira_id)], 'post': 'yes',
    }, follow=True)

    assert resp.status_code == 200
    assert b'Jira mode' in resp.content
    assert WorkItem.objects.filter(id=local_id).exists(), 'the local rows in the selection survive too'
    assert WorkItem.objects.filter(id=jira_id).exists()


def test_a_local_mode_bulk_delete_still_deletes(clean_db):
    """The other half of the rule: in local mode a delete is unchanged.
    Asserted on a child row rather than a work item, because a work item
    always has at least one `work_item_history` row and that table's
    foreign key refuses the delete regardless of mode — a pre-existing
    property of the schema, not something this requirement changes."""
    from workitems.models import WorkItemArtifact

    client = _admin_client(clean_db)
    item_id = _make_item()
    artifact = store.attach_artifact(item_id, 'ci_build', 'build-1', actor='tester')

    resp = client.post('/django-admin/workitems/workitemartifact/', {
        'action': 'delete_selected', '_selected_action': [artifact['id']], 'post': 'yes',
    }, follow=True)

    assert resp.status_code == 200
    assert not WorkItemArtifact.objects.filter(id=artifact['id']).exists()


def test_a_jira_mode_child_rows_bulk_delete_is_refused(clean_db):
    from workitems.models import WorkItemArtifact

    client = _admin_client(clean_db)
    item_id = _make_item(externalKey='AT-3')
    artifact = store.attach_artifact(item_id, 'ci_build', 'build-2', actor='tester')
    project_config.set_mode(PROJECT, 'jira')

    resp = client.post('/django-admin/workitems/workitemartifact/', {
        'action': 'delete_selected', '_selected_action': [artifact['id']], 'post': 'yes',
    }, follow=True)

    assert resp.status_code == 200
    assert b'Jira mode' in resp.content
    assert WorkItemArtifact.objects.filter(id=artifact['id']).exists()


def test_the_work_item_pages_link_inlines_are_read_only_in_every_mode(clean_db):
    """REQ-09 — these two inlines saved through the ORM and bypassed
    `store.create_link` in EVERY mode: no gate, no history row, no outbound
    event. This is a local-mode change too (RELEASE §6): an operator adds a
    link only from the link page."""
    for inline in (admin_module.WorkItemLinkFromInline, admin_module.WorkItemLinkToInline):
        assert inline.can_delete is False
        assert inline.has_add_permission(admin_module.WorkItemLinkFromInline, None) is False
        assert set(inline.readonly_fields) == set(inline.fields)


def test_the_work_item_link_admin_refuses_an_edit_in_every_mode(clean_db):
    client = _admin_client(clean_db)
    blocker_id = _make_item()
    dependent_id = _make_item()
    result = store.create_link(blocker_id, dependent_id, 'blocks', actor='tester')

    assert admin_module.WorkItemLinkAdmin.has_change_permission(
        admin_module.WorkItemLinkAdmin, None) is False

    # Django renders the page read-only rather than 404ing when view
    # permission remains, so the proof is that it offers no way to save.
    resp = client.get(f'/django-admin/workitems/workitemlink/{result["id"]}/change/')
    assert resp.status_code == 200
    assert b'name="_save"' not in resp.content, 'no change form — a link is created or it is not'

    # And a POST to it changes nothing.
    client.post(f'/django-admin/workitems/workitemlink/{result["id"]}/change/', {
        'from_work_item': str(dependent_id), 'to_work_item': str(blocker_id), 'link_type': 'relates-to',
    })
    from workitems.models import WorkItemLink
    link = WorkItemLink.objects.get(id=result['id'])
    assert link.link_type == 'blocks'
    assert str(link.from_work_item_id) == str(blocker_id)


def test_the_comment_admin_passes_admin_ui_origin_so_jira_mode_refuses_it(clean_db):
    """REQ-09 — `WorkItemCommentAdmin.save_model` did not pass an origin
    before v5.2, so the one comment path could not tell a person's comment
    from a machine's. In a Jira-mode project a person comments in Jira."""
    client = _admin_client(clean_db)
    item_id = _make_item(externalKey='AT-1')
    project_config.set_mode(PROJECT, 'jira')

    resp = client.post('/django-admin/workitems/workitemcomment/add/', {
        'work_item': str(item_id), 'author': 'an-operator', 'body': 'by hand',
        'reference_file': '', 'reference_function': '',
    }, follow=True)

    assert resp.status_code == 200
    assert b'Jira mode' in resp.content, resp.content[-1500:]
    from workitems.models import WorkItemComment
    assert WorkItemComment.objects.filter(work_item_id=item_id).count() == 0


def test_the_artifact_admin_shows_a_jira_mode_refusal_as_an_error_not_a_server_error(clean_db):
    """REQ-09 — "No other admin has that catch, so a rejection from its
    `save_model` is an unhandled server error today": every admin that can
    refuse now flashes the refusal instead of 500ing."""
    client = _admin_client(clean_db)
    item_id = _make_item(externalKey='AT-2')
    project_config.set_mode(PROJECT, 'jira')

    resp = client.post('/django-admin/workitems/workitemartifact/add/', {
        'work_item': str(item_id), 'artifact_type': 'commit', 'reference': 'abc123',
    }, follow=True)

    assert resp.status_code == 200, 'not a 500'
    assert b'Jira mode' in resp.content
    from workitems.models import WorkItemArtifact
    assert WorkItemArtifact.objects.filter(work_item_id=item_id).count() == 0


def test_in_local_mode_the_admins_saves_are_otherwise_unchanged(clean_db):
    client = _admin_client(clean_db)
    item_id = _make_item()

    resp = client.post('/django-admin/workitems/workitemartifact/add/', {
        'work_item': str(item_id), 'artifact_type': 'ci_build', 'reference': 'build-7',
    })
    assert resp.status_code == 302, resp.content[-1500:]

    resp = client.post('/django-admin/workitems/workitemcomment/add/', {
        'work_item': str(item_id), 'author': 'an-operator', 'body': 'by hand',
        'reference_file': '', 'reference_function': '',
    })
    assert resp.status_code == 302, resp.content[-1500:]

    from workitems.models import WorkItemArtifact, WorkItemComment
    assert WorkItemArtifact.objects.filter(work_item_id=item_id).count() == 1
    assert WorkItemComment.objects.filter(work_item_id=item_id).count() == 1
