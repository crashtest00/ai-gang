"""
Plain-dict serialization matching the Node implementation's HTTP response
shapes EXACTLY — this is the contract services/scrummaster/src/canonicalWorkItems.js
depends on (see that module's own comment: "the actual synchronous HTTP
surface"). The Node service returned raw `SELECT * FROM
work_item` rows (node-postgres hands back snake_case column names as
object keys) for every work-item-shaped response; this module reproduces
those exact keys so a JSON diff between the two services' responses is
empty for every field the old service emitted.
"""

from __future__ import annotations

from typing import Any

from .models import (
    WorkItemArtifact, WorkItemArtifactLink, WorkItemComment, WorkItemHistory, WorkItemLink, WorkItemReleaseDetail,
    WorkItemSpecificationLink, WorkItemStoryDetail,
)


def _iso(dt) -> Any:
    return dt.isoformat() if dt is not None else None


def serialize_work_item(item) -> dict:
    return {
        'id': str(item.id),
        'external_key': item.external_key,
        'type': item.type,
        'display_name': item.display_name,
        'description': item.description,
        'status': item.status,
        'assignee_agent_id': item.assignee_agent_id,
        'priority': item.priority,
        'writes_files': item.writes_files,
        'writes_services': item.writes_services,
        'parent_id': str(item.parent_id) if item.parent_id else None,
        'project': item.project,
        'created_at': _iso(item.created_at),
        'updated_at': _iso(item.updated_at),
    }


def serialize_story_detail(detail: WorkItemStoryDetail | None) -> dict | None:
    if detail is None:
        return None
    return {
        'work_item_id': str(detail.work_item_id),
        'behavior': detail.behavior,
        'acceptance_criteria': detail.acceptance_criteria,
        'constraints': detail.constraints,
        'edge_cases': detail.edge_cases,
        'out_of_scope': detail.out_of_scope,
        'value_hypothesis': detail.value_hypothesis,
        'test_measurement': detail.test_measurement,
    }


def serialize_release_detail(detail: WorkItemReleaseDetail | None) -> dict | None:
    if detail is None:
        return None
    return {
        'work_item_id': str(detail.work_item_id),
        'release_notes': detail.release_notes,
        'candidate_sha': detail.candidate_sha,
        'build_identifier': detail.build_identifier,
        'preview_url': detail.preview_url,
    }


def serialize_link(link: WorkItemLink) -> dict:
    return {
        'id': str(link.id),
        'from_work_item_id': str(link.from_work_item_id),
        'to_work_item_id': str(link.to_work_item_id),
        'link_type': link.link_type,
        'created_at': _iso(link.created_at),
    }


def serialize_history(row: WorkItemHistory) -> dict:
    return {
        'id': str(row.id),
        'work_item_id': str(row.work_item_id),
        'field': row.field,
        'old_value': row.old_value,
        'new_value': row.new_value,
        'actor': row.actor,
        'occurred_at': _iso(row.occurred_at),
    }


def serialize_artifact(row: WorkItemArtifact) -> dict:
    return {
        'id': str(row.id),
        'work_item_id': str(row.work_item_id),
        'artifact_type': row.artifact_type,
        'reference': row.reference,
        'created_at': _iso(row.created_at),
    }


def serialize_specification_link(link: WorkItemSpecificationLink | None) -> dict | None:
    """work-items.md REQ-01. `None` when the work item carries no
    specification link — the field is optional (§3, "A work item MAY
    carry one link")."""
    if link is None:
        return None
    return {
        'work_item_id': str(link.work_item_id),
        'artifact_id': str(link.artifact_id),
        'requirement_id': link.requirement_id,
    }


def serialize_artifact_link(link: WorkItemArtifactLink) -> dict:
    """work-items.md REQ-02."""
    return {
        'id': str(link.id),
        'work_item_id': str(link.work_item_id),
        'artifact_id': str(link.artifact_id),
        'position': link.position,
        'created_at': _iso(link.created_at),
    }


def serialize_comment(row: WorkItemComment) -> dict:
    return {
        'id': str(row.id),
        'work_item_id': str(row.work_item_id),
        'author': row.author,
        'body': row.body,
        'reference_file': row.reference_file,
        'reference_function': row.reference_function,
        'source_message_id': row.source_message_id,
        'created_at': _iso(row.created_at),
    }


def serialize_work_item_full(full: dict) -> dict:
    """`full` is readstore.get_work_item_full()'s return shape:
    {'item', 'storyDetail', 'links', 'history', 'artifacts', 'comments',
    'specificationLink', 'artifactLinks'}."""
    body = serialize_work_item(full['item'])
    body['storyDetail'] = serialize_story_detail(full['storyDetail'])
    body['releaseDetail'] = serialize_release_detail(full.get('releaseDetail'))
    body['links'] = [serialize_link(l) for l in full['links']]
    body['history'] = [serialize_history(h) for h in full['history']]
    body['artifacts'] = [serialize_artifact(a) for a in full['artifacts']]
    body['comments'] = [serialize_comment(c) for c in full['comments']]
    body['specification_link'] = serialize_specification_link(full.get('specificationLink'))
    body['artifact_links'] = [serialize_artifact_link(a) for a in full.get('artifactLinks', [])]
    return body


def serialize_work_item_with_references(item, specification_link, artifact_links: list) -> dict:
    """work-items.md REQ-01/REQ-02/REQ-05 — both references appear on the
    BARE (non-`?full=true`) single-item read too, not only the full one:
    "any agent handling that work item can read them by its canonical id"
    (REQ-05) should not require opting into the full record. Used by
    views.get_work_item's non-full branch, and by views.list_work_items'
    `externalKey` branch (V4 audit Pass 2 row 33) for the same reason — a
    dispatched agent resolving its canonical id from the issue key needs
    the references in that same read. The plain, unfiltered `GET
    /work-items` list keeps calling serialize_work_item() directly and is
    therefore unaffected (compatibility, PRD §10)."""
    body = serialize_work_item(item)
    body['specification_link'] = serialize_specification_link(specification_link)
    body['artifact_links'] = [serialize_artifact_link(a) for a in artifact_links]
    return body


def serialize_work_item_with_associations(item) -> dict:
    """work-items.md REQ-06's forward query only (views.list_work_items,
    when `specArtifactId`/`requirementId` are supplied): "every work item
    recording that link, and every delivery artifact associated with each,
    is enumerable" — so this list variant includes the specification link
    and the REQ-06 delivery associations (`work_item_artifact`) alongside
    the bare fields. `item.specification_link`/`item.artifacts` are
    expected to already be select_related/prefetch_related by the caller
    (readstore.list_work_items) — see that function's own comment."""
    body = serialize_work_item(item)
    body['specification_link'] = serialize_specification_link(getattr(item, 'specification_link', None))
    body['artifacts'] = [serialize_artifact(a) for a in item.artifacts.all()]
    return body
