"""
The direct, synchronous query
interface. Read access must be recorded in the service's own API/access
logs, not in Streams. Every function here logs to AccessLog before
returning, and none of them go through Streams or the outbox — a read has
no state-changing or notifying effect. Direct port of the Node
service's src/readStore.js.
"""

from __future__ import annotations

from typing import Optional

from django.utils import timezone

from .models import (
    AccessLog, WorkItem, WorkItemArtifact, WorkItemComment, WorkItemHistory, WorkItemLink, WorkItemReleaseDetail,
    WorkItemStoryDetail,
)


def log_access(operation: str, *, project: Optional[str] = None, work_item_id=None, actor: Optional[str] = None) -> None:
    AccessLog.objects.create(operation=operation, project=project, work_item_id=work_item_id, actor=actor,
                              occurred_at=timezone.now())


def get_work_item(work_item_id, *, actor: Optional[str] = None) -> Optional[WorkItem]:
    item = WorkItem.objects.filter(id=work_item_id).first()
    log_access('getWorkItem', work_item_id=work_item_id, project=item.project if item else None, actor=actor)
    return item


def get_work_item_full(work_item_id, *, actor: Optional[str] = None) -> Optional[dict]:
    item = get_work_item(work_item_id, actor=actor)
    if not item:
        return None

    detail = WorkItemStoryDetail.objects.filter(work_item_id=work_item_id).first()
    release_detail = WorkItemReleaseDetail.objects.filter(work_item_id=work_item_id).first()
    links = list(WorkItemLink.objects.filter(models_q_from_or_to(work_item_id)))
    history = list(WorkItemHistory.objects.filter(work_item_id=work_item_id).order_by('occurred_at'))
    artifacts = list(WorkItemArtifact.objects.filter(work_item_id=work_item_id).order_by('created_at'))
    comments = list(WorkItemComment.objects.filter(work_item_id=work_item_id).order_by('created_at'))

    return {
        'item': item,
        'storyDetail': detail,
        'releaseDetail': release_detail,
        'links': links,
        'history': history,
        'artifacts': artifacts,
        'comments': comments,
    }


def models_q_from_or_to(work_item_id):
    from django.db.models import Q
    return Q(from_work_item_id=work_item_id) | Q(to_work_item_id=work_item_id)


def list_work_items(*, project: Optional[str] = None, status: Optional[str] = None,
                     assignee_agent_id: Optional[str] = None, parent_id=None,
                     actor: Optional[str] = None) -> list[WorkItem]:
    """In Jira mode, the internal canonical store must remain
    queryable, using its own last-synced state regardless of whether
    Jira is currently reachable. Nothing here calls Jira at all — every
    read is served from this service's own datastore unconditionally, in
    both modes."""
    qs = WorkItem.objects.all()
    if project:
        qs = qs.filter(project=project)
    if status:
        qs = qs.filter(status=status)
    if assignee_agent_id:
        qs = qs.filter(assignee_agent_id=assignee_agent_id)
    if parent_id:
        qs = qs.filter(parent_id=parent_id)

    from django.db.models import Case, When, Value, IntegerField
    # Most-urgent-first
    # (1 -> 4), 0/No-priority sorts LAST (not first — an untriaged item
    # must not jump ahead of anything a human explicitly triaged),
    # creation timestamp as the tie-break.
    qs = qs.annotate(
        _priority_sort=Case(
            When(priority=0, then=Value(999)),
            default='priority',
            output_field=IntegerField(),
        )
    ).order_by('_priority_sort', 'created_at')

    rows = list(qs)
    log_access('listWorkItems', project=project, actor=actor)
    return rows


def get_history(work_item_id, *, actor: Optional[str] = None) -> list[WorkItemHistory]:
    rows = list(WorkItemHistory.objects.filter(work_item_id=work_item_id).order_by('occurred_at'))
    log_access('getHistory', work_item_id=work_item_id, actor=actor)
    return rows
