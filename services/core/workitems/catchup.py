"""
What remains of connecting a project to Jira: recording an external key
against a work item, and the mode flip itself.

The catch-up push that used to live here is gone (v5.1): its Jira-facing half
was ScrumMaster's jiraCatchupConsumer.js, which no longer exists, and pushing
a project's existing work items into Jira returns with the outbound writer in
v5.2. record_external_key stays as the idempotent recorder that writer will
report back through, and disconnect_jira stays as the operator's switch back
to local mode.
"""

from __future__ import annotations

from typing import Optional

from django.db import transaction
from django.utils import timezone

from . import project_config
from .models import WorkItem, WorkItemHistory
from .store import write_outbox_event as _write_outbox_event


def record_external_key(work_item_id, external_key: str, *, actor: str = 'jira-catchup') -> dict:
    """Records a work item's key in an external tracker. Idempotent: a no-op if
    external_key is already set — "skipped rather than re-created"."""
    with transaction.atomic():
        item = WorkItem.objects.select_for_update().filter(id=work_item_id).first()
        if not item:
            raise ValueError(f'record_external_key: no work item {work_item_id}')
        if item.external_key:
            return {'alreadyRecorded': True, 'externalKey': item.external_key}

        item.external_key = external_key
        item.updated_at = timezone.now()
        item.save(update_fields=['external_key', 'updated_at'])
        WorkItemHistory.objects.create(
            work_item_id=work_item_id, field='external_key', old_value=None, new_value=external_key,
            actor=actor, occurred_at=timezone.now(),
        )
        _write_outbox_event(
            project=item.project, event_type='work_item.external_key_recorded', work_item_id=work_item_id,
            payload={'workItemId': str(work_item_id), 'externalKey': external_key},
        )
        return {'alreadyRecorded': False, 'externalKey': external_key}


def connect_jira(project: str, jira_project_key: str) -> None:
    """The mode flip itself. Connecting a project is v5.2's, with the outbound
    writer and the catch-up push that goes with it; this is the flip that
    command will call."""
    project_config.set_mode(project, project_config.JIRA, jira_project_key=jira_project_key)


def disconnect_jira(project: str) -> None:
    project_config.revert_to_local(project)
