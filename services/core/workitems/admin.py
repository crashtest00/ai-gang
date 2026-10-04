"""
The Django admin UI — this service's human-facing
admin interface. Django is expected to also
host the human-facing admin UI, since that capability
comes with the framework at effectively no additional build cost. This
module is that capability; the Node/Express implementation explicitly
could NOT provide this for free (its own design notes
flagged the missing admin UI as a tracked gap) — wiring it up here is one
of the concrete reasons the product owner chose to rebuild in Django.

Every gated write made through this admin (status transition, assignment)
routes through store.py, so it gets the SAME write-gate, history
recording, and outbound event as a Streams-originated write
— never a raw ORM save that would bypass any of that. Non-gated fields
(display_name, description, priority, writes_files/services, external_key)
are saved via a small helper that still records history + emits an
outbound event, since every such write must still
be recorded and must still produce an outbound event, and that's not scoped
only to the gated fields — it applies to an admin-UI edit
generally.

WorkItemHistory, AccessLog, and OutboxEvent are registered read-only:
they are this service's own append-only/operational records, not things a
human should hand-edit.

**work-items.md REQ-03/REQ-08 — settled (V4).** REQ-03 requires that
recording either reference "from an agent, or from any component acting
on an agent's behalf, MUST travel as a durably queued Streams command
through the core service's existing write path
(`internal-work-item-service.md` REQ-03)," while "[t]he service's own
human-facing administration interface MAY record them directly in the
datastore, as `internal-work-item-service.md` REQ-08 permits for
interactions with no agent party, applying the same resolution check
(REQ-04)." WorkItemSpecificationLinkAdmin/WorkItemArtifactLinkAdmin
(below) are exactly that carve-out: an authenticated admin session has no
agent party, so they call store.record_specification_link/
store.add_artifact_link directly — the SAME established precedent every
other admin-UI write in this module follows — which still enforces
REQ-04's resolution check (both via the real foreign key and via that
function's own pre-check) and still emits the same outbound event a
Streams-originated write produces.

*The requirement's earlier wording read "whether the caller is a
human-facing interface or an agent," which contradicted this carve-out and
every existing admin write in the service; amended September 20, 2026
(product owner decision 1-1, V4 doc-vs-code audit row 6) to the text
quoted above. What this module's admin classes do was never in question —
only the requirement's own wording was.*
"""

from __future__ import annotations

import uuid

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponseRedirect
from django.utils import timezone

from . import store, write_gate
from .models import (
    AccessLog, OutboxEvent, ProjectConfig, ProjectStatusConfig, WebhookFailure, WorkItem, WorkItemArtifact,
    WorkItemArtifactLink, WorkItemComment, WorkItemHistory, WorkItemLink, WorkItemReleaseDetail,
    WorkItemSpecificationLink, WorkItemStoryDetail,
)


def _actor(request) -> str:
    username = getattr(request.user, 'get_username', lambda: None)()
    return username or 'admin-ui'


class GatedAdminMixin:
    """canonical-delivery-state.md REQ-09, "The admin is a person's edit
    interface, so in Jira mode it changes nothing".

    Two things every admin over a work item or one of its children needs,
    and only `WorkItemAdmin` had either of them before v5.2:

    1. **Error on save.** Django's admin machinery has no handling for an
       exception raised out of `save_model`/`save_formset`:
       `ModelAdmin._changeform_view` calls them with no surrounding
       try/except, so a refusal from any admin but `WorkItemAdmin` was an
       unhandled server error — a bare 500, and with DEBUG off, no
       traceback either.
    2. **Error on delete.** A refused delete must show the refusal on the
       delete page or the change list and delete nothing, single or bulk.
       A bulk delete that selects any Jira-mode row is refused WHOLE, its
       local rows included: a half-applied bulk delete is worse than a
       refused one, and the operator can narrow the selection.

    Both are implemented by raising Django's own `ValidationError` from the
    write and catching it in the view that wraps it — `changeform_view`,
    `delete_view` and `changelist_view` (which is where the `delete_selected`
    action runs). Each of those views wraps its call in `transaction.atomic`,
    so by the time the message is flashed nothing has been written.

    The admin reads no mode: `write_gate.route` answers, and in local mode
    every one of these paths saves or deletes exactly as it did (Pass 4
    decision 5.2 — the fields look editable and the refusal comes on
    save; the read-only display is parked)."""

    def gated_project(self, obj) -> str | None:
        """The project whose mode governs this row. Overridden where the
        row reaches its work item by another name."""
        work_item = getattr(obj, 'work_item', None)
        if work_item is not None:
            return work_item.project
        return getattr(obj, 'project', None)

    def _refuse_if_not_recorded(self, obj, what: str) -> None:
        project = self.gated_project(obj)
        if project is None:
            return
        if write_gate.route(project, write_gate.Origins.ADMIN_UI) != write_gate.RECORD:
            try:
                write_gate.refuse(write_gate.Origins.ADMIN_UI, what)
            except Exception as err:  # noqa: BLE001 - re-raised as the form error the views catch
                _raise_as_form_error(err)

    def _flash(self, request, err: DjangoValidationError, destination: str):
        for message in err.messages:
            messages.error(request, message)
        return HttpResponseRedirect(destination)

    def changeform_view(self, request, object_id=None, form_url='', extra_context=None):
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except DjangoValidationError as err:
            return self._flash(request, err, request.path)

    def delete_view(self, request, object_id, extra_context=None):
        try:
            return super().delete_view(request, object_id, extra_context)
        except DjangoValidationError as err:
            return self._flash(request, err, request.path)

    def changelist_view(self, request, extra_context=None):
        # Where `delete_selected` runs (ModelAdmin.response_action), so where
        # a refused BULK delete has to surface.
        try:
            return super().changelist_view(request, extra_context)
        except DjangoValidationError as err:
            return self._flash(request, err, request.get_full_path())

    def delete_model(self, request, obj):
        self._refuse_if_not_recorded(obj, 'deleting this row')
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            self._refuse_if_not_recorded(obj, 'deleting the selected rows (the whole selection is refused)')
        super().delete_queryset(request, queryset)


def _raise_as_form_error(err: Exception):
    """store.py's own exceptions (ValidationError, AssignmentRejectedError,
    DependencyGateError, write_gate.WriteGateRejectedError) all carry a
    `.code`. Re-raised as Django's ValidationError so it carries a type
    WorkItemAdmin.changeform_view (below) specifically catches — Django's
    admin machinery has no handling of its own for an exception raised out
    of save_model(): ModelAdmin._changeform_view calls it with no
    surrounding try/except, so whatever it raises otherwise propagates all
    the way out as an unhandled exception (a bare 500, and one with no
    traceback to go on once DEBUG is off)."""
    raise DjangoValidationError(str(err)) from err


class WorkItemAdminForm(forms.ModelForm):
    """A blank External key must be normalized to NULL before Django's own
    model-level uniqueness check runs (ModelForm._post_clean, during
    form.is_valid(), before save_model is ever reached) — otherwise a
    second work item saved with the field left blank is rejected as a
    duplicate of the first: the browser submits a left-blank TextField as
    '', and '' is a value like any other for a unique constraint, where
    only NULL is guaranteed never to collide with another row. See also
    WorkItem.save(), which applies the same normalization for a write that
    does not go through this form."""

    class Meta:
        model = WorkItem
        fields = '__all__'

    def clean_external_key(self):
        return self.cleaned_data.get('external_key') or None


NON_GATED_FIELDS = ('display_name', 'description', 'priority', 'writes_files', 'writes_services', 'external_key')


class WorkItemStoryDetailInline(admin.StackedInline):
    """Story schema field contract. Plain ORM-backed inline: the
    Node reference implementation never tracked per-field history for
    story-detail edits either (store.js's history entries are limited to
    `status`/`assignee_agent_id`/`work_item_link`), so this mirrors that
    same scope rather than inventing new tracking that was never part of
    the contract being ported."""

    model = WorkItemStoryDetail
    can_delete = False
    extra = 0


class WorkItemReleaseDetailInline(admin.StackedInline):
    """Release schema field contract. `candidate_sha`/
    `build_identifier`/`preview_url` are rendered but not meant for direct
    hand-editing — they're written by store.record_release_candidate's
    writeback path; left editable here anyway rather than
    read-only, matching WorkItemStoryDetailInline's precedent of not
    inventing field-level history/permission machinery the Node reference
    implementation never had either."""

    model = WorkItemReleaseDetail
    can_delete = False
    extra = 0


class _ReadOnlyLinkInline(admin.TabularInline):
    """canonical-delivery-state.md REQ-09 — read-only, in EVERY mode. These
    two inlines saved through the ORM and so bypassed `store.create_link`
    entirely: no write gate, no history row, no outbound event, local mode
    included. That is the same reasoning `WorkItemCommentInline` and
    `WorkItemArtifactInline` already carried, applied to links; an operator
    adds a link from the link page instead (RELEASE §6)."""

    model = WorkItemLink
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class WorkItemLinkFromInline(_ReadOnlyLinkInline):
    fk_name = 'from_work_item'
    fields = ('to_work_item', 'link_type', 'created_at')
    readonly_fields = fields
    verbose_name = 'Outgoing link (this item blocks / relates to)'
    verbose_name_plural = 'Outgoing links'


class WorkItemLinkToInline(_ReadOnlyLinkInline):
    fk_name = 'to_work_item'
    fields = ('from_work_item', 'link_type', 'created_at')
    readonly_fields = fields
    verbose_name = 'Incoming link (this item is blocked by / related from)'
    verbose_name_plural = 'Incoming links'


class WorkItemHistoryInline(admin.TabularInline):
    model = WorkItemHistory
    extra = 0
    can_delete = False
    fields = ('field', 'old_value', 'new_value', 'actor', 'occurred_at')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class WorkItemArtifactInline(admin.TabularInline):
    model = WorkItemArtifact
    extra = 0
    can_delete = False
    fields = ('artifact_type', 'reference', 'created_at')
    readonly_fields = ('created_at',)

    def has_add_permission(self, request, obj=None):
        # Adding an artifact via the inline would bypass store.attach_artifact's
        # outbox-event publication — use the dedicated
        # WorkItemArtifact admin (routes through store.py) to add one.
        return False


class WorkItemCommentInline(admin.TabularInline):
    model = WorkItemComment
    extra = 0
    can_delete = False
    fields = ('author', 'body', 'created_at')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False  # see WorkItemArtifactInline's comment — use WorkItemComment admin.


class WorkItemSpecificationLinkInline(admin.StackedInline):
    """work-items.md REQ-01. Read-only for visibility on the work item's
    own change page, same reasoning as WorkItemArtifactInline just above:
    adding/changing it here would bypass store.record_specification_link's
    REQ-04 check and outbox-event publication — use the dedicated
    WorkItemSpecificationLink admin (below), which routes through
    store.py."""

    model = WorkItemSpecificationLink
    can_delete = False
    extra = 0
    fields = ('artifact', 'requirement_id', 'created_at', 'updated_at')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


class WorkItemArtifactLinkInline(admin.TabularInline):
    """work-items.md REQ-02. Same read-only-plus-dedicated-admin split as
    WorkItemArtifactInline."""

    model = WorkItemArtifactLink
    fk_name = 'work_item'
    extra = 0
    can_delete = False
    fields = ('artifact', 'position', 'created_at')
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(WorkItem)
class WorkItemAdmin(GatedAdminMixin, admin.ModelAdmin):
    list_display = ('display_name', 'type', 'project', 'status', 'assignee_agent_id', 'priority', 'external_key', 'updated_at')
    list_filter = ('project', 'type', 'status')
    search_fields = ('=id', 'external_key', 'display_name', 'description')
    readonly_fields = ('created_at', 'updated_at')
    form = WorkItemAdminForm
    inlines = [WorkItemStoryDetailInline, WorkItemReleaseDetailInline, WorkItemLinkFromInline, WorkItemLinkToInline,
               WorkItemArtifactInline, WorkItemCommentInline, WorkItemHistoryInline,
               WorkItemSpecificationLinkInline, WorkItemArtifactLinkInline]
    fields = ('id', 'project', 'type', 'display_name', 'description', 'status', 'assignee_agent_id',
               'priority', 'writes_files', 'writes_services', 'parent', 'external_key', 'created_at', 'updated_at')

    def get_inlines(self, request, obj):
        """WorkItemStoryDetail/WorkItemReleaseDetail are 1:1 child tables
        keyed by the PARENT's own primary key (OneToOneField(primary_key=
        True)) — and this admin's `id` field is human-editable on add
        (models.py: "a human-admin UX convenience," not a relaxation of
        that rule). Those two facts don't compose: Django's `_changeform_view`
        builds every inline formset from `form.instance` BEFORE
        `form.is_valid()` runs, so each inline's hidden pk_field bakes in
        the PARENT's pre-validation random-default id — never whatever a
        human types into the visible `id` field. The mismatch (or, when
        the hidden field is simply omitted, a silent `None`) corrupts
        `form.instance.id` by the time `save_model` runs, so the work item
        gets created under the WRONG id entirely, silently.
        Story/Release detail can only be added correctly once the object
        already has a real, saved pk (i.e., on the change/edit form, where
        `_create_formsets` builds inlines from an existing `obj` and there
        is no pre/post-validation id mismatch) — so they're hidden here on
        add. WorkItemSpecificationLinkInline has the exact same shape
        (OneToOneField(primary_key=True) to WorkItem) and is hidden on add
        for the identical reason. Every other inline here (links, artifacts,
        artifact links, comments, history) is a normal FK, not a same-table
        pk, and unaffected."""
        if obj is None:
            return [i for i in self.inlines
                    if i not in (WorkItemStoryDetailInline, WorkItemReleaseDetailInline, WorkItemSpecificationLinkInline)]
        return self.inlines

    def get_readonly_fields(self, request, obj=None):
        """canonical-delivery-state.md REQ-09 — the 2026-09-07 Jira-mode
        rule that rendered `status` and `assignee_agent_id` read-only is
        GONE, with the `get_mode` call behind it. The admin reads no mode:
        in Jira mode every change is refused on save, not hidden before it
        (Pass 4 decision 5.2), and showing a subset of the fields read-only
        while every other field is refused anyway was the worse of the two
        (showing them all read-only up front is parked:
        `parking-lot/remote-mode-admin-read-only-display.md`). This also
        supersedes `internal-work-item-service.md` REQ-08's "render
        read-only in Jira mode"."""
        ro = list(self.readonly_fields)
        if obj is not None:
            # Canonical identity is never reissued once created.
            ro = ['id'] + ro
        return ro

    def save_model(self, request, obj, form, change):
        actor = _actor(request)

        if not change:
            self._create(request, obj, form, actor)
            return

        changed = set(form.changed_data)
        try:
            # Every other admin write to a Jira-mode work item is refused
            # too, not only the gated fields (REQ-09): `transition_status`
            # and `assign_work_item` ask the router themselves, but the
            # non-gated edit below is a raw ORM save with no handler to ask
            # for it.
            non_gated_changed = changed.intersection(NON_GATED_FIELDS + ('parent',))
            if non_gated_changed:
                self._refuse_if_not_recorded(obj, 'editing a work item')

            if 'status' in changed:
                updated = store.transition_status(obj.pk, obj.status, actor=actor, origin=write_gate.Origins.ADMIN_UI)
                self._copy_fields(obj, updated)
            if 'assignee_agent_id' in changed and obj.assignee_agent_id:
                updated = store.assign_work_item(obj.pk, obj.assignee_agent_id, actor=actor, origin=write_gate.Origins.ADMIN_UI)
                self._copy_fields(obj, updated)

            if non_gated_changed:
                self._apply_non_gated_edit(obj, non_gated_changed, actor)
        except DjangoValidationError:
            raise
        except Exception as err:
            _raise_as_form_error(err)

    def save_formset(self, request, form, formset, change):
        """The story- and release-detail inlines are edited through here,
        and they are plain ORM saves. REQ-09 refuses them in Jira mode like
        any other admin write to the item (no inline on this page offers
        delete any more, now that the two link inlines are read-only)."""
        self._refuse_if_not_recorded(form.instance, 'editing a work item\'s detail')
        super().save_formset(request, form, formset, change)

    def _create(self, request, obj, form, actor):
        payload = {
            'id': obj.id or uuid.uuid4(),
            'project': obj.project,
            'type': obj.type,
            'displayName': obj.display_name,
            'description': obj.description,
            'status': obj.status or 'proposed',
            'priority': obj.priority if obj.priority is not None else 0,
            'assigneeAgentId': obj.assignee_agent_id,
            'parentId': obj.parent_id,
            'writesFiles': obj.writes_files,
            'writesServices': obj.writes_services,
            'externalKey': obj.external_key,
        }
        try:
            created = store.create_work_item(payload, actor=actor, origin=write_gate.Origins.ADMIN_UI)
        except Exception as err:
            _raise_as_form_error(err)
            return
        self._copy_fields(obj, created)

    @staticmethod
    def _copy_fields(obj, source: WorkItem) -> None:
        for f in ('id', 'external_key', 'type', 'display_name', 'description', 'status', 'assignee_agent_id',
                  'priority', 'writes_files', 'writes_services', 'parent_id', 'project', 'created_at', 'updated_at'):
            setattr(obj, f, getattr(source, f))

    @staticmethod
    def _apply_non_gated_edit(obj: WorkItem, changed_fields, actor: str) -> None:
        """Every such write must still be recorded in the
        append-only history, and must still produce an outbound event,
        so it remains visible to the rest of the platform after the fact
        even though the request itself did not arrive as a Streams
        command. Applies to any admin write, not only the gated
        fields — display_name/description/priority/writes_files/
        writes_services/external_key/parent are not individually tracked
        in work_item_history (mirroring store.js's own scope, which never
        tracked these either), but an outbound event still fires so a
        subscriber sees the change."""
        from django.db import transaction

        with transaction.atomic():
            obj.updated_at = timezone.now()
            update_fields = list(changed_fields) + ['updated_at']
            # `parent` is the ORM accessor; the DB column is `parent_id`.
            update_fields = ['parent_id' if f == 'parent' else f for f in update_fields]
            obj.save(update_fields=update_fields)
            store.write_outbox_event(
                project=obj.project, event_type='work_item.updated', work_item_id=obj.id,
                payload={'id': str(obj.id), 'changed': sorted(changed_fields)},
            )


@admin.register(WorkItemLink)
class WorkItemLinkAdmin(GatedAdminMixin, admin.ModelAdmin):
    list_display = ('from_work_item', 'link_type', 'to_work_item', 'created_at')
    autocomplete_fields = ('from_work_item', 'to_work_item')
    readonly_fields = ('created_at',)

    def gated_project(self, obj):
        return obj.from_work_item.project if obj.from_work_item_id else None

    def has_change_permission(self, request, obj=None):
        """canonical-delivery-state.md REQ-09 — an edit is refused, in
        every mode. The dependency graph's contract has no "edit a link"
        concept: a link is created or it isn't, `create_link` is store.py's
        only mutator, and the change form here saved through the ORM past
        it. So a link is created only through `save_model`'s add path, and
        this page is add/delete/view."""
        return False

    def save_model(self, request, obj, form, change):
        try:
            result = store.create_link(obj.from_work_item_id, obj.to_work_item_id, obj.link_type,
                                        actor=_actor(request), origin=write_gate.Origins.ADMIN_UI)
        except Exception as err:
            _raise_as_form_error(err)
            return
        obj.id = uuid.UUID(result['id'])
        if result.get('deduped'):
            messages.info(request, 'An identical link already existed — no duplicate was created.')


@admin.register(WorkItemArtifact)
class WorkItemArtifactAdmin(GatedAdminMixin, admin.ModelAdmin):
    list_display = ('work_item', 'artifact_type', 'reference', 'created_at')
    autocomplete_fields = ('work_item',)
    readonly_fields = ('created_at',)

    def save_model(self, request, obj, form, change):
        # An association is not one of the router's five kinds of write
        # (Jira carries no field for one), so there is no handler to ask
        # for it — REQ-09 refuses it here instead, as it does every other
        # admin write to a Jira-mode work item.
        self._refuse_if_not_recorded(obj, 'attaching an artifact association')
        if change:
            obj.save()
            return
        try:
            result = store.attach_artifact(obj.work_item_id, obj.artifact_type, obj.reference, actor=_actor(request))
        except Exception as err:
            _raise_as_form_error(err)
            return
        obj.id = uuid.UUID(result['id'])

    def has_change_permission(self, request, obj=None):
        return False  # artifacts are immutable associations once recorded.


@admin.register(WorkItemSpecificationLink)
class WorkItemSpecificationLinkAdmin(GatedAdminMixin, admin.ModelAdmin):
    """work-items.md REQ-01/REQ-03 — where a human links a story (or any
    work item) to the requirement it was created to satisfy. A direct
    write, same as WorkItemArtifactAdmin/WorkItemCommentAdmin just above:
    REQ-03's Streams-only rule ("whether the caller is a human-facing
    interface or an agent") is written for the internal-work-item-service.md
    REQ-08 carve-out this admin already operates under for every other
    field — no agent is a party to an admin-UI write, so REQ-08 governs
    here, not REQ-03. See this module's own top-of-file comment and the
    build report for the fuller account of that tension.

    `artifact` uses raw_id_fields rather than autocomplete_fields: the
    Artifact admin (a different app, out of this track's ownership) has no
    search_fields configured, which autocomplete_fields requires.
    save_model still runs store.record_specification_link's REQ-04 check
    on this path (the instance's own admin form validation already
    enforces it too, since `artifact` is a real ForeignKey — this call is
    the belt to that form-level braces, and the same call every other
    write path uses)."""

    list_display = ('work_item', 'artifact', 'requirement_id', 'updated_at')
    autocomplete_fields = ('work_item',)
    raw_id_fields = ('artifact',)
    readonly_fields = ('created_at', 'updated_at')

    def save_model(self, request, obj, form, change):
        self._refuse_if_not_recorded(obj, 'recording a specification link')
        try:
            store.record_specification_link(obj.work_item_id, obj.artifact_id, obj.requirement_id, actor=_actor(request))
        except Exception as err:
            _raise_as_form_error(err)


@admin.register(WorkItemArtifactLink)
class WorkItemArtifactLinkAdmin(GatedAdminMixin, admin.ModelAdmin):
    """work-items.md REQ-02/REQ-03 — where a human links an artifact that
    informs a work item. Same direct-write pattern and same REQ-03/REQ-08
    reasoning as WorkItemSpecificationLinkAdmin above.

    No dedupe-message branch here (contrast WorkItemLinkAdmin above, which
    has one for its own idempotent create_link): WorkItemArtifactLink's
    (work_item, artifact) UniqueConstraint makes Django's ModelForm reject
    a duplicate submission during form validation, before save_model ever
    runs — confirmed by test_work_item_references_admin.py's own dedupe
    test, which observes a 200-with-form-error, never save_model's
    `deduped` branch. store.add_artifact_link's own idempotent-redelivery
    behavior still matters on the Streams command path (command_consumer.py),
    which has no ModelForm to validate against."""

    list_display = ('work_item', 'artifact', 'position', 'created_at')
    autocomplete_fields = ('work_item',)
    raw_id_fields = ('artifact',)
    readonly_fields = ('position', 'created_at')

    def save_model(self, request, obj, form, change):
        self._refuse_if_not_recorded(obj, 'adding an artifact link')
        if change:
            obj.save()
            return
        try:
            result = store.add_artifact_link(obj.work_item_id, obj.artifact_id, actor=_actor(request))
        except Exception as err:
            _raise_as_form_error(err)
            return
        obj.id = uuid.UUID(result['id'])
        obj.position = result['position']

    def has_change_permission(self, request, obj=None):
        return False  # artifact links are immutable associations once recorded, same as WorkItemArtifactAdmin.


@admin.register(WorkItemComment)
class WorkItemCommentAdmin(GatedAdminMixin, admin.ModelAdmin):
    list_display = ('work_item', 'author', 'created_at')
    autocomplete_fields = ('work_item',)
    readonly_fields = ('created_at', 'source_message_id')

    def save_model(self, request, obj, form, change):
        if change:
            obj.save()
            return
        try:
            # REQ-09 — `ADMIN_UI`, which this admin did not pass before
            # v5.2. The comment path is the one place a comment's write
            # differs by mode, and without the origin it could not tell a
            # person's comment from a machine's: in a Jira-mode project a
            # person comments in Jira, so `route` refuses this one.
            comment = store.append_comment(
                obj.work_item_id, obj.author or _actor(request), obj.body,
                reference_file=obj.reference_file, reference_function=obj.reference_function,
                origin=write_gate.Origins.ADMIN_UI,
            )
        except Exception as err:
            _raise_as_form_error(err)
            return
        obj.id = uuid.UUID(comment['id'])

    def has_change_permission(self, request, obj=None):
        return False  # comments are append-only, same as history.


@admin.register(WorkItemHistory)
class WorkItemHistoryAdmin(admin.ModelAdmin):
    """Append-only. No add/change/delete — this is a read-only
    audit view, matching store.py never issuing UPDATE/DELETE against this
    table."""

    list_display = ('work_item', 'field', 'old_value', 'new_value', 'actor', 'occurred_at')
    list_filter = ('field',)
    search_fields = ('=work_item__id', 'actor')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OutboxEvent)
class OutboxEventAdmin(admin.ModelAdmin):
    """Operational visibility into the transactional outbox — whether
    the relay (`python manage.py relay`) is keeping up. Read-only: rows are
    written exclusively by store.py inside the same transaction as the
    write they describe, and marked published by the relay process."""

    list_display = ('event_type', 'project', 'work_item_id', 'created_at', 'published_at')
    list_filter = ('project', 'event_type')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(AccessLog)
class AccessLogAdmin(admin.ModelAdmin):
    """Read access must be recorded in the service's own API/access
    logs. Read-only audit view."""

    list_display = ('operation', 'project', 'work_item_id', 'actor', 'occurred_at')
    list_filter = ('operation', 'project')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WebhookFailure)
class WebhookFailureAdmin(admin.ModelAdmin):
    """Operator-visible record of a Jira-originated
    webhook event rejected by validation. Read-only except for delete, so
    an operator can clear entries once investigated."""

    list_display = ('project', 'work_item_id', 'external_key', 'reason', 'occurred_at')
    list_filter = ('project',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ProjectConfig)
class ProjectConfigAdmin(admin.ModelAdmin):
    """Mode selection, **read-only from v5.2** (canonical-delivery-state.md
    REQ-10; Shovel Ready Pass 5, decision 6.2).

    Up to v5.1 this was plain CRUD over the row that decides a project's
    mode, described as an operational escape hatch. It is not one any more:
    switching a project to Jira mode without pushing its work items into
    Jira leaves `core` holding items no Jira issue corresponds to, and
    every write for them routed at an empty tracker. So `mode` and
    `jira_project_key` are read-only on ADD as well as on change, and no
    row is deleted here, because a project with no row is local mode by
    definition (`models.py`) — deleting the row of a Jira-mode project
    would switch it back with no `disconnect_jira` and no record of it.

    The two management commands are the only paths that change a project's
    mode: `connect_jira <project> <jira-key>`, which refuses unless the
    credentials, the field ids and the webhook's events are all in place
    and no Release is open, pushes every keyless work item into Jira,
    re-syncs each keyed item's status and Blocks links, and only then
    switches; and `disconnect_jira <project>`, which switches back and
    makes no Jira write. Both are run with ScrumMaster stopped and the
    project otherwise left alone.

    An add form therefore creates a LOCAL row with no key — which is what a
    project's first row always was — and the row's own project name and
    timestamps are all this admin still writes."""

    list_display = ('project', 'mode', 'jira_project_key', 'updated_at')
    _ALWAYS_READONLY = ('mode', 'jira_project_key', 'created_at', 'updated_at')
    readonly_fields = _ALWAYS_READONLY

    def get_readonly_fields(self, request, obj=None):
        # `obj is None` is the ADD form, which Django otherwise renders with
        # every field editable — REQ-10 requires the two mode fields
        # read-only there too, so a new row cannot be created straight into
        # Jira mode.
        return self._ALWAYS_READONLY

    def has_delete_permission(self, request, obj=None):
        """No row is deleted through this admin, single or bulk: Django
        hides the delete button and drops `delete_selected` from the action
        list when this is False, so the refusal is visible rather than a
        rejection on submit."""
        return False

    def delete_model(self, request, obj):
        _raise_as_form_error(write_gate.WriteGateRejectedError(
            'a ProjectConfig row is not deleted through the admin — a project with no row is in local '
            'mode, so deleting one would switch its project back with no disconnect_jira; run '
            'disconnect_jira instead'
        ))

    def delete_queryset(self, request, queryset):
        """Belt to `has_delete_permission`'s braces, for a caller that
        reaches the model admin directly rather than through the change
        list. A selection containing any Jira-mode row is refused WHOLE,
        its local rows included, as every other bulk delete in this module
        is (REQ-09)."""
        self.delete_model(request, None)


@admin.register(ProjectStatusConfig)
class ProjectStatusConfigAdmin(admin.ModelAdmin):
    list_display = ('project', 'status', 'baseline_status', 'jira_status_name')
    list_filter = ('project',)
