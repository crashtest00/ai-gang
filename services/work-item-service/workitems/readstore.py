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
    AccessLog, WorkItem, WorkItemArtifact, WorkItemArtifactLink, WorkItemComment, WorkItemHistory, WorkItemLink,
    WorkItemReleaseDetail, WorkItemSpecificationLink, WorkItemStoryDetail,
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
    specification_link = get_specification_link(work_item_id)
    artifact_links = list_artifact_links(work_item_id)

    return {
        'item': item,
        'storyDetail': detail,
        'releaseDetail': release_detail,
        'links': links,
        'history': history,
        'artifacts': artifacts,
        'comments': comments,
        'specificationLink': specification_link,
        'artifactLinks': artifact_links,
    }


def get_specification_link(work_item_id, *, actor: Optional[str] = None) -> Optional[WorkItemSpecificationLink]:
    """work-items.md REQ-01. Not separately access-logged when called from
    get_work_item_full/the bare get_work_item view (the top-level
    'getWorkItem' log entry already covers that read), matching how
    storyDetail/history are folded into the same read without their own
    log_access call."""
    return WorkItemSpecificationLink.objects.filter(work_item_id=work_item_id).first()


def list_artifact_links(work_item_id, *, actor: Optional[str] = None) -> list[WorkItemArtifactLink]:
    """work-items.md REQ-02 — "in the order recorded"."""
    return list(WorkItemArtifactLink.objects.filter(work_item_id=work_item_id).order_by('position'))


def get_specification_link_for_delivery_artifact(work_item_artifact_id, *, actor: Optional[str] = None) -> Optional[dict]:
    """work-items.md REQ-06 (AC-04) — backward traceability. Starting from
    a `work_item_artifact` association (`canonical-work-model.md` REQ-06 —
    what a work item PRODUCED), resolve its work item's specification link
    (what authorized it) in ONE lookup: a single SELECT, via
    select_related, that joins work_item_artifact -> work_item ->
    work_item_specification_link — distinct from the several separate
    queries get_work_item_full issues, and the literal "single lookup"
    REQ-06's acceptance names. (The one access-log INSERT below it is the
    same read-side bookkeeping every function in this module performs —
    see log_access's own module comment — not a second lookup.)

    Returns None only when the delivery-artifact association itself does
    not exist (views.py maps that to 404). When it exists but the work item
    carries no specification link, `specArtifactId`/`requirementId` are
    both None — a real, distinct outcome from "no such association"."""
    wia = WorkItemArtifact.objects.select_related('work_item__specification_link').filter(
        id=work_item_artifact_id,
    ).first()
    log_access(
        'resolveSpecificationLinkForDeliveryArtifact',
        work_item_id=wia.work_item_id if wia else None, actor=actor,
    )
    if not wia:
        return None

    link = getattr(wia.work_item, 'specification_link', None)
    return {
        'workItemId': str(wia.work_item_id),
        'deliveryArtifactId': str(wia.id),
        'specArtifactId': str(link.artifact_id) if link else None,
        'requirementId': link.requirement_id if link else None,
    }


def models_q_from_or_to(work_item_id):
    from django.db.models import Q
    return Q(from_work_item_id=work_item_id) | Q(to_work_item_id=work_item_id)


def list_work_items(*, project: Optional[str] = None, status: Optional[str] = None,
                     assignee_agent_id: Optional[str] = None, parent_id=None,
                     spec_artifact_id=None, requirement_id: Optional[str] = None,
                     external_key: Optional[str] = None,
                     actor: Optional[str] = None) -> list[WorkItem]:
    """In Jira mode, the internal canonical store must remain
    queryable, using its own last-synced state regardless of whether
    Jira is currently reachable. Nothing here calls Jira at all — every
    read is served from this service's own datastore unconditionally, in
    both modes.

    `spec_artifact_id`/`requirement_id` are work-items.md REQ-06's forward
    query: given an (artifact id, requirement id) pair, enumerate every
    work item recording that specification link. A direct read, like every
    other filter this function already applies — no Streams involved.

    `external_key` is work-items.md REQ-05's canonical-id lookup (V4 audit
    Pass 2 row 33): a dispatched agent's prompt carries the issue key
    (`services/scrummaster/src/prompt.js`), never the work item's canonical
    id, and in Jira mode that key is this column's exact value — an
    unambiguous filter, since `WorkItem.external_key` is unique. Combinable
    with `project`, though the uniqueness already narrows the match to at
    most one row."""
    qs = WorkItem.objects.all()
    if project:
        qs = qs.filter(project=project)
    if status:
        qs = qs.filter(status=status)
    if assignee_agent_id:
        qs = qs.filter(assignee_agent_id=assignee_agent_id)
    if parent_id:
        qs = qs.filter(parent_id=parent_id)
    if external_key:
        qs = qs.filter(external_key=external_key)
    if spec_artifact_id:
        qs = qs.filter(specification_link__artifact_id=spec_artifact_id)
    if requirement_id:
        qs = qs.filter(specification_link__requirement_id=requirement_id)
    if spec_artifact_id or requirement_id:
        # "every delivery artifact associated with each [work item] is
        # enumerable" (REQ-06) — the caller (views.list_work_items) needs
        # each result's work_item_artifact rows too, so fetch them
        # together rather than forcing a query per row.
        qs = qs.select_related('specification_link').prefetch_related('artifacts')
    if external_key:
        # REQ-05: "any agent handling that work item can read them by its
        # canonical id" — the whole point of this filter is to let a
        # dispatched agent read the REQ-01/REQ-02 references it needs, so
        # fetch them together with the match rather than forcing the agent
        # into a second request per reference.
        qs = qs.select_related('specification_link').prefetch_related('artifact_links')

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
