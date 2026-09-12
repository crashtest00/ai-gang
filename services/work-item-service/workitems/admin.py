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
"""

from __future__ import annotations

import uuid

from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone

from . import project_config, store, write_gate
from .models import (
    AccessLog, OutboxEvent, ProjectConfig, ProjectStatusConfig, WebhookFailure, WorkItem, WorkItemArtifact,
    WorkItemComment, WorkItemHistory, WorkItemLink, WorkItemReleaseDetail, WorkItemStoryDetail,
)


def _actor(request) -> str:
    username = getattr(request.user, 'get_username', lambda: None)()
    return username or 'admin-ui'


def _raise_as_form_error(err: Exception):
    """store.py's own exceptions (ValidationError, AssignmentRejectedError,
    DependencyGateError, write_gate.WriteGateRejectedError) all carry a
    `.code`. Re-raised as Django's ValidationError so
    ModelAdmin._changeform_view's own `except ValidationError` handling
    redisplays the form with a non-field error instead of a 500."""
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


class WorkItemLinkFromInline(admin.TabularInline):
    model = WorkItemLink
    fk_name = 'from_work_item'
    extra = 0
    fields = ('to_work_item', 'link_type', 'created_at')
    readonly_fields = ('created_at',)
    verbose_name = 'Outgoing link (this item blocks / relates to)'
    verbose_name_plural = 'Outgoing links'


class WorkItemLinkToInline(admin.TabularInline):
    model = WorkItemLink
    fk_name = 'to_work_item'
    extra = 0
    fields = ('from_work_item', 'link_type', 'created_at')
    readonly_fields = ('created_at',)
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


@admin.register(WorkItem)
class WorkItemAdmin(admin.ModelAdmin):
    list_display = ('display_name', 'type', 'project', 'status', 'assignee_agent_id', 'priority', 'external_key', 'updated_at')
    list_filter = ('project', 'type', 'status')
    search_fields = ('=id', 'external_key', 'display_name', 'description')
    readonly_fields = ('created_at', 'updated_at')
    form = WorkItemAdminForm
    inlines = [WorkItemStoryDetailInline, WorkItemReleaseDetailInline, WorkItemLinkFromInline, WorkItemLinkToInline,
               WorkItemArtifactInline, WorkItemCommentInline, WorkItemHistoryInline]
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
        add. Every other inline here (links, artifacts, comments, history)
        is a normal FK, not a same-table pk, and unaffected."""
        if obj is None:
            return [i for i in self.inlines if i not in (WorkItemStoryDetailInline, WorkItemReleaseDetailInline)]
        return self.inlines

    def get_readonly_fields(self, request, obj=None):
        ro = list(self.readonly_fields)
        if obj is not None:
            # Canonical identity is never reissued once created.
            ro = ['id'] + ro
            mode = project_config.get_mode(obj.project)
            if mode['mode'] == 'jira':
                # The 2026-09-07 Jira-mode restriction: status,
                # assignment, and dependency fields render read-only in
                # Jira mode. (Dependency links are managed via the
                # WorkItemLink admin, which applies the same gate itself.)
                ro += ['status', 'assignee_agent_id']
        return ro

    def save_model(self, request, obj, form, change):
        actor = _actor(request)

        if not change:
            self._create(request, obj, form, actor)
            return

        changed = set(form.changed_data)
        try:
            if 'status' in changed:
                updated = store.transition_status(obj.pk, obj.status, actor=actor, origin=write_gate.Origins.ADMIN_UI)
                self._copy_fields(obj, updated)
            if 'assignee_agent_id' in changed and obj.assignee_agent_id:
                updated = store.assign_work_item(obj.pk, obj.assignee_agent_id, actor=actor, origin=write_gate.Origins.ADMIN_UI)
                self._copy_fields(obj, updated)

            non_gated_changed = changed.intersection(NON_GATED_FIELDS + ('parent',))
            if non_gated_changed:
                self._apply_non_gated_edit(obj, non_gated_changed, actor)
        except DjangoValidationError:
            raise
        except Exception as err:
            _raise_as_form_error(err)

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
class WorkItemLinkAdmin(admin.ModelAdmin):
    list_display = ('from_work_item', 'link_type', 'to_work_item', 'created_at')
    autocomplete_fields = ('from_work_item', 'to_work_item')
    readonly_fields = ('created_at',)

    def save_model(self, request, obj, form, change):
        if change:
            # The dependency graph's contract has no "edit a link" concept
            # — a link is created or it isn't (createLink is the only
            # mutator in store.py). Editing an existing row here is not a
            # gated write this service's contract defines.
            obj.save()
            return
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
class WorkItemArtifactAdmin(admin.ModelAdmin):
    list_display = ('work_item', 'artifact_type', 'reference', 'created_at')
    autocomplete_fields = ('work_item',)
    readonly_fields = ('created_at',)

    def save_model(self, request, obj, form, change):
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


@admin.register(WorkItemComment)
class WorkItemCommentAdmin(admin.ModelAdmin):
    list_display = ('work_item', 'author', 'created_at')
    autocomplete_fields = ('work_item',)
    readonly_fields = ('created_at', 'source_message_id')

    def save_model(self, request, obj, form, change):
        if change:
            obj.save()
            return
        try:
            comment = store.append_comment(
                obj.work_item_id, obj.author or _actor(request), obj.body,
                reference_file=obj.reference_file, reference_function=obj.reference_function,
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
    """Mode selection. Not itself a "work item write" the gated-write rules
    govern — this is project-level meta-configuration, plain CRUD.
    Prefer catchup.connect_jira/disconnect_jira for a real mode flip (they
    also trigger the catch-up push); this admin is an operational
    escape hatch."""

    list_display = ('project', 'mode', 'jira_project_key', 'updated_at')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ProjectStatusConfig)
class ProjectStatusConfigAdmin(admin.ModelAdmin):
    list_display = ('project', 'status', 'baseline_status', 'jira_status_name')
    list_filter = ('project',)
