"""
Delivery records in the Django Admin Panel — PRD §3: "every record they
keep lives in the same Postgres instance, visible in the Django Admin
Panel humans already use."

Read-only, because the librarian is this table's only writer (spec §4) and
an operator editing a row here would be editing the index rather than the
filesystem it indexes — which the next request would simply correct.

This is not a browse interface over artifacts (spec §2's non-goal): it is
staff-only, it is the Django admin rather than anything an agent can
reach, and it lists deliveries that have happened, not artifacts that
could be requested.
"""

from __future__ import annotations

from django.contrib import admin

from .models import ArtifactDelivery


@admin.register(ArtifactDelivery)
class ArtifactDeliveryAdmin(admin.ModelAdmin):
    list_display = ('artifact_id', 'repository', 'path', 'requested_by', 'task_id', 'delivered_at', 'updated_at')
    list_filter = ('repository',)
    readonly_fields = ('artifact', 'repository', 'path', 'requested_by', 'task_id', 'delivered_at', 'updated_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
