"""
canonical-work-model.md REQ-15 — connecting Jira performs a one-time,
idempotent, batched export of a project's existing local canonical work
items into Jira. Direct port of the Node service's src/catchup.js.

Design decision (carried over unchanged from the Node implementation):
this service has no Jira client of its own (internal-work-item-service.md
REQ-09's own resolution — "this service has no Jira client of its own...
and never calls Jira directly"). The catch-up push is therefore split at
the same boundary REQ-09's rollup push already uses: this module does the
idempotent, resumable SELECTION of what still needs a Jira issue (skip
anything with external_key already set) and emits one outbound event per
remaining item via the normal outbox/Streams path; a downstream
Jira-facing consumer (ScrumMaster's jiraCatchupConsumer.js) creates the
actual Jira issue and reports the resulting key back via the
recordExternalKey Streams command (command_consumer.py ->
record_external_key below), itself idempotent.
"""

from __future__ import annotations

from typing import Optional

from django.db import transaction
from django.utils import timezone

from . import project_config
from .models import WorkItem, WorkItemHistory
from .store import write_outbox_event as _write_outbox_event


def start_catchup_push(project: str, *, batch_size: int = 50) -> dict:
    """Selects every work item in `project` still missing an external_key
    and emits a `work_item.jira_catchup_requested` outbound event for each,
    via the SAME outbox/relay mechanism as every other outbound event.
    Resumable by construction."""
    with transaction.atomic():
        rows = list(
            WorkItem.objects.select_for_update()
            .filter(project=project, external_key__isnull=True)
            .order_by('created_at')[:batch_size]
        )

        queued = []
        for row in rows:
            _write_outbox_event(
                project=project, event_type='work_item.jira_catchup_requested', work_item_id=row.id,
                payload={'workItemId': str(row.id), 'type': row.type, 'displayName': row.display_name},
            )
            queued.append(str(row.id))
        return {'queued': queued, 'remaining': len(queued) == batch_size}


def record_external_key(work_item_id, external_key: str, *, actor: str = 'jira-catchup') -> dict:
    """The idempotent half of REQ-15's contract: a no-op if external_key is
    already set — "skipped rather than re-created"."""
    with transaction.atomic():
        item = WorkItem.objects.select_for_update().filter(id=work_item_id).first()
        if not item:
            raise ValueError(f'recordExternalKey: no work item {work_item_id}')
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
    """REQ-14: the actual mode flip. Called once the catch-up push has
    been initiated (not necessarily fully drained)."""
    project_config.set_mode(project, project_config.JIRA, jira_project_key=jira_project_key)


def disconnect_jira(project: str) -> None:
    project_config.revert_to_local(project)
