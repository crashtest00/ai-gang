"""
The upload surface — artifact-ingress.md REQ-01, REQ-04.

Upload is the Django Admin Panel and nothing else (product owner,
September 20, 2026). Humans upload; agents never do (PRD §3). The recorded
actor is the logged-in admin user, so there is no self-declared actor
header to trust and no unauthenticated upload endpoint to protect — the
``@csrf_exempt`` ``/admin/work-items`` HTTP handlers in ``workitems/views.py``
are deliberately NOT the pattern followed here.

Adding an artifact uploads it. Re-uploading is editing the existing record
and choosing a new file (REQ-04): same id, same path, new bytes.

**No validation of the file itself.** No extension check, no MIME check,
no content check, no validator on the field and no size limit beyond
Django's defaults. "Any file MUST be accepted whatever its type, size
class or content, with no metadata supplied alongside it" (REQ-01).
"""

from __future__ import annotations

from functools import partial

from django.contrib import admin
from django.db import models, transaction
from django.utils import timezone

from . import events
from .models import Artifact, ArtifactAccessLog


def _actor(request) -> str:
    username = getattr(request.user, 'get_username', lambda: None)()
    return username or 'admin-ui'


@admin.register(Artifact)
class ArtifactAdmin(admin.ModelAdmin):
    list_display = ('id', 'original_filename', 'path', 'uploaded_by', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    readonly_fields = ('id', 'path', 'uploaded_by', 'created_at', 'updated_at')

    # Django's form-level FileField refuses a zero-byte upload outright
    # ("The submitted file is empty."). That is a judgement about the
    # file's contents, and REQ-01 admits any file whatever its content —
    # an empty file is a file. `allow_empty_file` turns the check off. It
    # is the only adjustment this app makes to Django's file handling;
    # nothing here adds a check of its own.
    formfield_overrides = {
        models.FileField: {'allow_empty_file': True},
    }

    def get_fields(self, request, obj=None):
        if obj is None:
            # An upload requires the file and nothing else (REQ-01); the
            # rest is assigned by the service and has no value to show yet.
            return ('file', 'original_filename')
        return ('file', 'original_filename', 'id', 'path', 'uploaded_by', 'created_at', 'updated_at')

    def has_delete_permission(self, request, obj=None):
        """V4 specifies no artifact deletion, and Django's default delete
        would remove the record while leaving the file on the volume —
        exactly the record/filesystem disagreement PRD §8 rules out. Off
        until deletion is specified."""
        return False

    def save_model(self, request, obj, form, change):
        file_uploaded = 'file' in form.changed_data

        if file_uploaded:
            obj.uploaded_by = _actor(request)
            obj.updated_at = timezone.now()
            if not (obj.original_filename or '').strip():
                uploaded = form.cleaned_data.get('file')
                # Whatever the client called it, kept verbatim and opaque.
                # It is never a path component — artifacts/storage.py's
                # upload_to ignores it outright (REQ-05).
                obj.original_filename = getattr(uploaded, 'name', '') or ''

        # obj.save() is what assigns the stored path (FileField.pre_save ->
        # storage.upload_to), so the event below must be built after it.
        super().save_model(request, obj, form, change)

        if file_uploaded:
            # REQ-06: one event per upload and per re-upload, and none for
            # an edit that did not bring new bytes. on_commit so a rolled
            # back admin save announces nothing.
            transaction.on_commit(partial(events.publish_upload, obj, created=not change))


@admin.register(ArtifactAccessLog)
class ArtifactAccessLogAdmin(admin.ModelAdmin):
    """This app's own operational record — visible, never hand-edited,
    mirroring ``workitems``' AccessLogAdmin."""

    list_display = ('occurred_at', 'operation', 'artifact_id', 'actor')
    ordering = ('-occurred_at',)
    readonly_fields = ('occurred_at', 'operation', 'artifact_id', 'actor')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
