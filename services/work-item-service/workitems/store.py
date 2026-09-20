"""
This service's internal work-item write path. Every
function here is the ONLY place canonical state is mutated — sole
owner of the datastore — every caller (Streams command consumer, Jira
webhook consumer, admin UI) goes through this module, never the ORM
directly.

Direct, function-for-function port of the Node implementation's src/store.js
onto Django's ORM + transaction.atomic(). Every mutating function:
 1. Opens one transaction (transaction.atomic()) covering the datastore
    change, its WorkItemHistory row(s), and its OutboxEvent row(s), which
    are therefore always atomic with the change itself.
 2. Applies the write-gate before touching a gated field (status,
    assignment, dependency link).
 3. Recomputes the parent rollup synchronously, in the same
    transaction, when a status transition lands on a work item with a
    non-null parent_id.

Catalog-backed assignment validation is reused via workitems/assignment.py
— a Python reimplementation of services/scrummaster/src/assignment.js reading the
SAME on-disk catalog, not a require() (impossible cross-language) and not
a runtime HTTP call — see assignment.py/registry.py's own module comments
for the full tradeoff.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from django.db import transaction
from django.utils import timezone

from artifacts.models import Artifact

from . import assignment, project_config, status_vocabulary, write_gate
from .models import (
    OutboxEvent, WorkItem, WorkItemArtifact, WorkItemArtifactLink, WorkItemComment, WorkItemHistory, WorkItemLink,
    WorkItemReleaseDetail, WorkItemSpecificationLink, WorkItemStoryDetail,
)


class DependencyGateError(Exception):
    code = 'DEPENDENCY_GATE_REJECTED'

    def __init__(self, message: str, blockers: list):
        super().__init__(message)
        self.blockers = blockers


class ValidationError(Exception):
    code = 'VALIDATION_ERROR'


class AssignmentRejectedError(Exception):
    code = 'ASSIGNMENT_REJECTED'

    def __init__(self, message: str, result: dict):
        super().__init__(message)
        self.result = result


class UnresolvedArtifactError(Exception):
    """work-items.md REQ-04 — a specification link or artifact link named
    an artifact canonical id that does not resolve to a registered
    artifact. The schema itself already refuses this (WorkItemSpecificationLink.artifact
    / WorkItemArtifactLink.artifact are real foreign keys to artifacts.Artifact
    with on_delete=PROTECT — see models.py), so this exception exists to
    turn that into a clean, well-formed rejection at the application
    boundary instead of a raw IntegrityError, the same way ValidationError
    turns other rejections into a clean failure through the command path."""
    code = 'UNRESOLVED_ARTIFACT'


class ReleaseGateError(Exception):
    """A release work item's
    `proposed` -> `in-review` transition (candidate cut) is rejected
    because the target project's beta queue is not clean."""
    code = 'RELEASE_GATE_REJECTED'

    def __init__(self, message: str, outstanding: list):
        super().__init__(message)
        self.outstanding = outstanding


def write_outbox_event(*, project: str, event_type: str, work_item_id: Optional[str], payload: dict) -> str:
    """Write an outbox row in the SAME transaction as the
    datastore change it describes. Callers MUST already be inside a
    transaction.atomic() block. Public: reused directly by catchup.py and
    admin.py (both need to emit an outbound event for a write that isn't
    one of this module's own named mutations)."""
    event = OutboxEvent.objects.create(
        project=project, event_type=event_type, work_item_id=work_item_id, payload=payload,
    )
    return str(event.id)


# Back-compat internal alias — every call site within this module.
_write_outbox_event = write_outbox_event


def _append_history(work_item_id, field: str, old_value, new_value, actor: str) -> None:
    WorkItemHistory.objects.create(
        work_item_id=work_item_id, field=field, old_value=old_value, new_value=new_value,
        actor=actor, occurred_at=timezone.now(),
    )


def get_work_item(work_item_id) -> Optional[WorkItem]:
    return WorkItem.objects.filter(id=work_item_id).first()


# ---------------------------------------------------------------------------
# Canonical identity + create (full local-mode lifecycle)
# ---------------------------------------------------------------------------

def _assert_story_fields_present(detail: Optional[dict]) -> None:
    required = ['behavior', 'acceptanceCriteria', 'constraints', 'edgeCases', 'outOfScope']
    missing = [f for f in required if not (detail and str(detail.get(f) or '').strip())]
    if missing:
        raise ValidationError(
            f'Story is missing required fields and cannot leave \'proposed\' status: {", ".join(missing)}'
        )


def _assert_artifact_resolves(artifact_id) -> None:
    """work-items.md REQ-04. `WorkItemSpecificationLink.artifact`/
    `WorkItemArtifactLink.artifact` are real foreign keys to
    `artifacts.Artifact` (models.py), so an unresolved id would fail at the
    database level regardless — this check exists to turn that into a
    clean UnresolvedArtifactError before the INSERT is even attempted,
    the same role `_assert_story_fields_present` plays for REQ-17."""
    if not Artifact.objects.filter(id=artifact_id).exists():
        raise UnresolvedArtifactError(f'artifact {artifact_id} does not resolve to a registered artifact')


def _record_specification_link(item: WorkItem, artifact_id, requirement_id: str) -> WorkItemSpecificationLink:
    """Shared by create_work_item (recorded at creation) and
    record_specification_link (recorded/replaced later) — same
    REQ-04 check, same upsert, same outbound event, regardless of when the
    link is set. Upsert rather than create-once: work-items.md does not
    make the link immutable, and a Streams command redelivered with the
    SAME (artifactId, requirementId) must be a safe no-op, per REQ-03's
    Streams delivery semantics (`redis-streams.md`'s idempotency
    handling)."""
    _assert_artifact_resolves(artifact_id)
    link, _created = WorkItemSpecificationLink.objects.update_or_create(
        work_item=item, defaults={'artifact_id': artifact_id, 'requirement_id': requirement_id, 'updated_at': timezone.now()},
    )
    _write_outbox_event(
        project=item.project, event_type='work_item.specification_link_recorded', work_item_id=item.id,
        payload={'workItemId': str(item.id), 'artifactId': str(artifact_id), 'requirementId': requirement_id},
    )
    return link


def _add_artifact_link(item: WorkItem, artifact_id) -> tuple[WorkItemArtifactLink, bool]:
    """Shared by create_work_item and add_artifact_link. Idempotent on
    (work_item, artifact) — a redelivered addArtifactLink command must not
    create a duplicate list entry or disturb existing positions, mirroring
    create_link's own dedupe-by-unique-edge precedent. `position` is
    assigned here (count of existing links), never supplied by a caller —
    REQ-02's "the order recorded"."""
    _assert_artifact_resolves(artifact_id)
    existing = WorkItemArtifactLink.objects.filter(work_item=item, artifact_id=artifact_id).first()
    if existing:
        return existing, True

    next_position = WorkItemArtifactLink.objects.filter(work_item=item).count()
    link = WorkItemArtifactLink.objects.create(
        work_item=item, artifact_id=artifact_id, position=next_position, created_at=timezone.now(),
    )
    _write_outbox_event(
        project=item.project, event_type='work_item.artifact_link_added', work_item_id=item.id,
        payload={'id': str(link.id), 'workItemId': str(item.id), 'artifactId': str(artifact_id), 'position': next_position},
    )
    return link, False


@transaction.atomic
def create_work_item(input: dict, *, actor: Optional[str] = None, origin: str = write_gate.Origins.DIRECT) -> WorkItem:
    """input: { id (REQUIRED), project, type, displayName, description,
    status, priority, parentId, writesFiles, writesServices,
    storyDetail: {...} (required if type === 'story' and status is leaving
    'proposed'), specificationLink: {artifactId, requirementId} (optional,
    work-items.md REQ-01 — "a Refinement Agent records them when it
    creates the work item"), artifactLinks: [artifactId, ...] (optional,
    REQ-02, order preserved) }"""
    if not input or not input.get('id'):
        raise ValidationError('createWorkItem requires an id (canonical identity)')
    if not input.get('project'):
        raise ValidationError('createWorkItem requires a project')
    if not input.get('type'):
        raise ValidationError('createWorkItem requires a type')
    if not input.get('displayName'):
        raise ValidationError('createWorkItem requires a displayName')

    status = input.get('status') or 'proposed'

    mode = project_config.get_mode(input['project'])
    write_gate.assert_gated_write_allowed(mode['mode'], origin)

    custom_statuses = project_config.get_custom_statuses(input['project'])
    validity = status_vocabulary.validate_status(status, custom_statuses)
    if not validity['ok']:
        raise ValidationError(f'createWorkItem: {validity["reason"]}')

    if input.get('type') == 'story' and validity['baseline'] != 'proposed':
        _assert_story_fields_present(input.get('storyDetail'))

    item = WorkItem.objects.create(
        id=input['id'],
        external_key=input.get('externalKey'),
        type=input['type'],
        display_name=input['displayName'],
        description=input.get('description'),
        status=status,
        assignee_agent_id=input.get('assigneeAgentId'),
        priority=input.get('priority') if input.get('priority') is not None else 0,
        writes_files=input.get('writesFiles'),
        writes_services=input.get('writesServices'),
        parent_id=input.get('parentId'),
        project=input['project'],
        created_at=timezone.now(),
        updated_at=timezone.now(),
    )

    story_detail = input.get('storyDetail')
    if input.get('type') == 'story' and story_detail:
        WorkItemStoryDetail.objects.create(
            work_item=item,
            behavior=story_detail.get('behavior') or '',
            acceptance_criteria=story_detail.get('acceptanceCriteria') or '',
            constraints=story_detail.get('constraints') or '',
            edge_cases=story_detail.get('edgeCases') or '',
            out_of_scope=story_detail.get('outOfScope') or '',
            value_hypothesis=story_detail.get('valueHypothesis'),
            test_measurement=story_detail.get('testMeasurement'),
        )

    spec_link_input = input.get('specificationLink')
    if spec_link_input:
        _record_specification_link(item, spec_link_input['artifactId'], spec_link_input['requirementId'])

    for artifact_id in (input.get('artifactLinks') or []):
        _add_artifact_link(item, artifact_id)

    _append_history(item.id, 'status', None, status, actor or 'system')
    if input.get('assigneeAgentId'):
        _append_history(item.id, 'assignee_agent_id', None, input['assigneeAgentId'], actor or 'system')

    _write_outbox_event(
        project=input['project'], event_type='work_item.created', work_item_id=item.id,
        payload={'id': str(item.id), 'type': item.type, 'displayName': item.display_name, 'status': status, 'project': item.project},
    )

    return get_work_item(item.id)


# ---------------------------------------------------------------------------
# Canonical assignment authority
# ---------------------------------------------------------------------------

@transaction.atomic
def assign_work_item(work_item_id, agent_id: str, *, actor: Optional[str] = None,
                      origin: str = write_gate.Origins.DIRECT) -> WorkItem:
    item = WorkItem.objects.select_for_update().filter(id=work_item_id).first()
    if not item:
        raise ValidationError(f'assignWorkItem: no work item {work_item_id}')

    mode = project_config.get_mode(item.project)
    write_gate.assert_gated_write_allowed(mode['mode'], origin)

    result = assignment.validate_assignment(item.project, agent_id)
    if not result['ok']:
        raise AssignmentRejectedError(
            f'assignment of "{agent_id}" to {work_item_id} rejected: {result["code"]}', result,
        )

    old_value = item.assignee_agent_id
    item.assignee_agent_id = agent_id
    item.updated_at = timezone.now()
    item.save(update_fields=['assignee_agent_id', 'updated_at'])
    _append_history(work_item_id, 'assignee_agent_id', old_value, agent_id, actor or 'system')
    _write_outbox_event(
        project=item.project, event_type='work_item.assigned', work_item_id=work_item_id,
        payload={'id': str(work_item_id), 'assigneeAgentId': agent_id, 'previous': old_value},
    )

    return get_work_item(work_item_id)


# ---------------------------------------------------------------------------
# Status lifecycle + dependency gating + parent rollup
# ---------------------------------------------------------------------------

def _blockers_of(work_item_id) -> list[dict]:
    rows = WorkItemLink.objects.filter(
        to_work_item_id=work_item_id, link_type='blocks',
    ).select_related('from_work_item').values('from_work_item_id', 'from_work_item__status')
    return [{'blocker_id': r['from_work_item_id'], 'blocker_status': r['from_work_item__status']} for r in rows]


def _apply_status_change(item: WorkItem, new_status: str, actor: Optional[str]) -> Optional[dict]:
    """Internal: applies a status transition with no gating/validation of
    its own — caller has already validated. Used both by the public
    transition_status and by the rollup recomputation below, inside the
    SAME transaction."""
    old_status = item.status
    if old_status == new_status:
        return None  # no-op: if there's no change, nothing to record or publish.

    item.status = new_status
    item.updated_at = timezone.now()
    item.save(update_fields=['status', 'updated_at'])
    _append_history(item.id, 'status', old_status, new_status, actor)
    _write_outbox_event(
        project=item.project, event_type='work_item.status_changed', work_item_id=item.id,
        payload={'id': str(item.id), 'status': new_status, 'previous': old_status},
    )
    return {'id': item.id, 'status': new_status, 'previous': old_status}


def _recompute_parent_rollup(parent_id, actor: Optional[str]) -> None:
    """Recompute a parent's canonical status against its
    children's CURRENT states, synchronously, in the same transaction as
    the triggering child transition. Default policy: all children done =>
    parent done. Any child cancelled/failed => parent holds its last
    non-terminal status (no automatic resolution). Recurses if the parent
    itself has a parent."""
    parent = WorkItem.objects.select_for_update().filter(id=parent_id).first()
    if not parent:
        return

    custom_statuses = project_config.get_custom_statuses(parent.project)
    children = list(WorkItem.objects.filter(parent_id=parent_id))
    if not children:
        return

    baselines = [status_vocabulary.baseline_of(c.status, custom_statuses) for c in children]

    all_done = all(b == 'done' for b in baselines)
    any_terminal_not_done = any(b in ('cancelled', 'failed') for b in baselines)

    target = None
    if all_done:
        target = 'done'
    elif any_terminal_not_done:
        target = None  # "the parent holds at its last non-terminal status" — no change.

    if target and target != parent.status:
        changed = _apply_status_change(parent, target, actor or 'system:rollup')
        if changed and parent.parent_id:
            _recompute_parent_rollup(parent.parent_id, actor)


RELEASE_EVENT_TYPE = 'work_item.jira_release_event'
_RELEASE_EVENT_KIND_BY_STATUS = {'in-review': 'requested', 'done': 'done', 'cancelled': 'abandoned'}


def _release_beta_queue_outstanding(project: str) -> list[WorkItem]:
    """Local-mode equivalent of
    `services/scrummaster/src/handlers.js`'s `handleReleaseRequested` JQL check
    (`issuetype != Release AND status = "In Review"`): any non-release work
    item on the target project still sitting in tester-acceptance review.
    Literal status comparison rather than baseline resolution, matching
    `_blockers_of`'s precedent elsewhere in this module — this check doesn't
    ask for baseline resolution and the Jira-mode check it mirrors doesn't
    do it either."""
    return list(WorkItem.objects.filter(project=project, status='in-review').exclude(type='release'))


def _publish_release_event(item: WorkItem, kind: str) -> None:
    _write_outbox_event(
        project=item.project, event_type=RELEASE_EVENT_TYPE, work_item_id=item.id,
        payload={'kind': kind, 'workItemId': str(item.id), 'project': item.project},
    )


def transition_status(work_item_id, new_status: str, *, actor: Optional[str] = None,
                       origin: str = write_gate.Origins.DIRECT) -> WorkItem:
    """`origin` per write_gate.Origins — webhook_consumer.py passes
    JIRA_WEBHOOK after its own pre-validation; direct callers
    (Streams commands, admin UI in local mode) pass DIRECT/ADMIN_UI.

    A `release` work item's
    `proposed` -> `in-review` transition (candidate cut) additionally
    requires the target project's beta queue to be clean. Checked and, if
    rejected, commented on HERE — outside `_transition_status_core`'s
    atomic block — because a comment written inside the same atomic block
    as a subsequent raise would roll back along with it; this way the
    rejection comment survives even though the transition itself doesn't.
    Every internal-API surface (Django Admin, external HTTP API, Streams
    command) reaches this same gate, since all three call this function
    rather than the atomic core directly."""
    item = get_work_item(work_item_id)
    if not item:
        raise ValidationError(f'transitionStatus: no work item {work_item_id}')

    if item.type == 'release' and item.status == 'proposed' and new_status == 'in-review':
        outstanding = _release_beta_queue_outstanding(item.project)
        if outstanding:
            names = ', '.join(f'{o.display_name} ({o.id})' for o in outstanding)
            append_comment(
                work_item_id, actor or 'system',
                'Cannot cut a release candidate — the following work items are still '
                f'awaiting tester acceptance on beta: {names}. Resolve these (move to '
                '\'done\' or otherwise off the beta queue) and retry.',
            )
            raise ReleaseGateError(
                f'{work_item_id} cannot cut a release candidate — {len(outstanding)} '
                'work item(s) still in \'in-review\'',
                [str(o.id) for o in outstanding],
            )

    return _transition_status_core(work_item_id, new_status, actor=actor, origin=origin)


@transaction.atomic
def _transition_status_core(work_item_id, new_status: str, *, actor: Optional[str] = None,
                             origin: str = write_gate.Origins.DIRECT) -> WorkItem:
    item = WorkItem.objects.select_for_update().filter(id=work_item_id).first()
    if not item:
        raise ValidationError(f'transitionStatus: no work item {work_item_id}')

    mode = project_config.get_mode(item.project)
    write_gate.assert_gated_write_allowed(mode['mode'], origin)

    custom_statuses = project_config.get_custom_statuses(item.project)
    validity = status_vocabulary.validate_status(new_status, custom_statuses)
    if not validity['ok']:
        raise ValidationError(f'transitionStatus: {validity["reason"]}')

    if item.type == 'story' and validity['baseline'] != 'proposed':
        detail = WorkItemStoryDetail.objects.filter(work_item_id=work_item_id).first()
        _assert_story_fields_present(detail and {
            'behavior': detail.behavior,
            'acceptanceCriteria': detail.acceptance_criteria,
            'constraints': detail.constraints,
            'edgeCases': detail.edge_cases,
            'outOfScope': detail.out_of_scope,
        })

    # A dependent MUST NOT reach 'ready' until every declared
    # blocker has reached 'done'.
    if validity['baseline'] == 'ready':
        blockers = _blockers_of(work_item_id)
        incomplete = [b for b in blockers if b['blocker_status'] != 'done']
        if incomplete:
            raise DependencyGateError(
                f'{work_item_id} cannot transition to "{new_status}" — {len(incomplete)} blocker(s) not yet done',
                [b['blocker_id'] for b in incomplete],
            )

    changed = _apply_status_change(item, new_status, actor or 'system')
    if changed and item.parent_id:
        _recompute_parent_rollup(item.parent_id, actor)
    if changed and validity['baseline'] == 'done':
        _unblock_dependents(work_item_id, actor)
    if changed and item.type == 'release':
        # Publish the
        # SAME canonical event Jira-mode candidate-cut/production-promote/
        # abandonment already publish (webhook_consumer.py's
        # _handle_release_requested/_handle_release_done/
        # _handle_release_abandoned), so ScrumMaster's existing
        # dispatchConsumer.js consumer executes both modes through one
        # code path, with no new Django-side handler. No
        # `jiraIssueKey` in the payload — dispatchConsumer.js/handlers.js
        # branch on its absence to read canonical fields instead of
        # calling Jira.
        kind = _RELEASE_EVENT_KIND_BY_STATUS.get(new_status)
        if kind:
            _publish_release_event(item, kind)

    return get_work_item(work_item_id)


# ---------------------------------------------------------------------------
# Release-candidate writeback
# ---------------------------------------------------------------------------

@transaction.atomic
def record_release_candidate(work_item_id, *, candidate_sha: str, build_identifier: Optional[str] = None,
                              preview_url: Optional[str] = None, actor: Optional[str] = None) -> WorkItem:
    """Writes the release-candidate Jenkins job's results back onto a
    release work item's canonical fields once the candidate is cut, and
    posts a comment recording it — the local-mode equivalent of what
    Jenkins already does directly to a Jira Release ticket's custom fields
    today (`scripts/create-release-fields.sh`). Write-once-per-candidate: a
    new candidate cut replaces these three fields, it does not append."""
    item = get_work_item(work_item_id)
    if not item or item.type != 'release':
        raise ValidationError(f'recordReleaseCandidate: no release work item {work_item_id}')

    detail = WorkItemReleaseDetail.objects.filter(work_item_id=item.id).first() \
        or WorkItemReleaseDetail(work_item=item)
    detail.candidate_sha = candidate_sha
    detail.build_identifier = build_identifier
    detail.preview_url = preview_url
    detail.save()

    _write_outbox_event(
        project=item.project, event_type='work_item.release_candidate_recorded', work_item_id=item.id,
        payload={'id': str(item.id), 'candidateSha': candidate_sha, 'buildIdentifier': build_identifier,
                 'previewUrl': preview_url},
    )
    note = f'Release candidate cut: {candidate_sha}'
    if build_identifier:
        note += f' (build {build_identifier})'
    if preview_url:
        note += f'\nPreview: {preview_url}'
    append_comment(work_item_id, actor or 'jenkins', note)

    return get_work_item(work_item_id)


def _unblock_dependents(blocker_work_item_id, actor: Optional[str]) -> None:
    """The dependency graph's "Done Handler" logic, ported to operate against
    this service's own graph instead of Jira issue links. Stateless and
    idempotent: a dependent only moves if it is CURRENTLY
    'waiting-on-dependency' with every blocker done, which becomes false
    the instant it has already moved."""
    dependent_ids = WorkItemLink.objects.filter(
        from_work_item_id=blocker_work_item_id, link_type='blocks',
    ).values_list('to_work_item_id', flat=True)

    for dependent_id in dependent_ids:
        dependent = WorkItem.objects.select_for_update().filter(id=dependent_id).first()
        if not dependent or dependent.status != 'waiting-on-dependency':
            continue

        blockers = _blockers_of(dependent_id)
        all_done = all(b['blocker_status'] == 'done' for b in blockers)
        if not all_done:
            continue

        changed = _apply_status_change(dependent, 'ready', actor or 'system:unblock')
        if changed and dependent.parent_id:
            _recompute_parent_rollup(dependent.parent_id, actor)


# ---------------------------------------------------------------------------
# Artifact association (not subject to the write-gate)
# ---------------------------------------------------------------------------

@transaction.atomic
def attach_artifact(work_item_id, artifact_type: str, reference: str, *, actor: Optional[str] = None) -> dict:
    item = get_work_item(work_item_id)
    if not item:
        raise ValidationError(f'attachArtifact: no work item {work_item_id}')

    artifact = WorkItemArtifact.objects.create(
        work_item=item, artifact_type=artifact_type, reference=reference, created_at=timezone.now(),
    )
    _write_outbox_event(
        project=item.project, event_type='work_item.artifact_attached', work_item_id=work_item_id,
        payload={'id': str(artifact.id), 'workItemId': str(work_item_id), 'artifactType': artifact_type, 'reference': reference},
    )
    return {'id': str(artifact.id), 'workItemId': str(work_item_id), 'artifactType': artifact_type, 'reference': reference}


# ---------------------------------------------------------------------------
# work-items.md REQ-01/REQ-02 — specification link and artifact links (not
# subject to the write-gate, same as artifact association/comments above:
# neither is a status/assignment/dependency field write_gate.py governs).
# ---------------------------------------------------------------------------

@transaction.atomic
def record_specification_link(work_item_id, artifact_id, requirement_id: str, *, actor: Optional[str] = None) -> dict:
    """REQ-01. Sets (or replaces) the work item's single specification
    link. Called from command_consumer.py (Streams path) and admin.py
    (the admin-UI path — see that module's own comment on why this is a
    direct write rather than a Streams round-trip)."""
    item = get_work_item(work_item_id)
    if not item:
        raise ValidationError(f'recordSpecificationLink: no work item {work_item_id}')

    link = _record_specification_link(item, artifact_id, requirement_id)
    return {'workItemId': str(work_item_id), 'artifactId': str(link.artifact_id), 'requirementId': link.requirement_id}


@transaction.atomic
def add_artifact_link(work_item_id, artifact_id, *, actor: Optional[str] = None) -> dict:
    """REQ-02. Appends one artifact link to the work item's ordered list.
    Same two callers as record_specification_link above."""
    item = get_work_item(work_item_id)
    if not item:
        raise ValidationError(f'addArtifactLink: no work item {work_item_id}')

    link, deduped = _add_artifact_link(item, artifact_id)
    return {
        'id': str(link.id), 'workItemId': str(work_item_id), 'artifactId': str(link.artifact_id),
        'position': link.position, 'deduped': deduped,
    }


# Durable per-item completion marker check, so an evidence-posting
# step determines completion from this service's own durable record
# instead of a before/after comparison of external state.
def has_completion_marker(work_item_id, step_key: str) -> bool:
    return WorkItemArtifact.objects.filter(work_item_id=work_item_id, artifact_type=step_key).exists()


def record_completion_marker(work_item_id, step_key: str, reference: str, *, actor: Optional[str] = None) -> dict:
    if has_completion_marker(work_item_id, step_key):
        return {'alreadyMarked': True}
    artifact = attach_artifact(work_item_id, step_key, reference, actor=actor)
    return {'alreadyMarked': False, 'artifact': artifact}


# ---------------------------------------------------------------------------
# Comment-thread communication contract (not subject to the write-gate)
# ---------------------------------------------------------------------------

def _row_to_comment(row: WorkItemComment) -> dict:
    return {
        'id': str(row.id), 'workItemId': str(row.work_item_id), 'author': row.author, 'body': row.body,
        'referenceFile': row.reference_file, 'referenceFunction': row.reference_function,
    }


@transaction.atomic
def append_comment(work_item_id, author: str, body: str, *, reference_file: Optional[str] = None,
                    reference_function: Optional[str] = None, source_message_id: Optional[str] = None) -> dict:
    item = get_work_item(work_item_id)
    if not item:
        raise ValidationError(f'appendComment: no work item {work_item_id}')

    if source_message_id:
        existing = WorkItemComment.objects.filter(source_message_id=source_message_id).first()
        if existing:
            # Redelivery of the same message MUST NOT produce a second row.
            return _row_to_comment(existing)

    comment = WorkItemComment.objects.create(
        work_item=item, author=author, body=body, reference_file=reference_file,
        reference_function=reference_function, source_message_id=source_message_id,
        created_at=timezone.now(),
    )
    _write_outbox_event(
        project=item.project, event_type='work_item.comment_added', work_item_id=work_item_id,
        payload={'id': str(comment.id), 'workItemId': str(work_item_id), 'author': author, 'body': body},
    )
    return _row_to_comment(comment)


# ---------------------------------------------------------------------------
# Dependency graph (link create) — gated by the write-gate like status/assignment.
# ---------------------------------------------------------------------------

@transaction.atomic
def create_link(from_work_item_id, to_work_item_id, link_type: str, *, actor: Optional[str] = None,
                 origin: str = write_gate.Origins.DIRECT) -> dict:
    from_item = get_work_item(from_work_item_id)
    to_item = get_work_item(to_work_item_id)
    if not from_item or not to_item:
        raise ValidationError('createLink: both work items must exist')
    if from_item.project != to_item.project:
        raise ValidationError('createLink: work items must belong to the same project')

    mode = project_config.get_mode(from_item.project)
    write_gate.assert_gated_write_allowed(mode['mode'], origin)

    existing = WorkItemLink.objects.filter(
        from_work_item_id=from_work_item_id, to_work_item_id=to_work_item_id, link_type=link_type,
    ).first()
    if existing:
        # Idempotent redelivery: a
        # retry never creates a duplicate edge.
        return {'id': str(existing.id), 'deduped': True}

    link = WorkItemLink.objects.create(
        id=uuid.uuid4(), from_work_item_id=from_work_item_id, to_work_item_id=to_work_item_id,
        link_type=link_type, created_at=timezone.now(),
    )
    _append_history(to_work_item_id, 'work_item_link', None, f'{link_type}:{from_work_item_id}', actor or 'system')
    _write_outbox_event(
        project=from_item.project, event_type='work_item.link_created', work_item_id=to_work_item_id,
        payload={'id': str(link.id), 'fromWorkItemId': str(from_work_item_id), 'toWorkItemId': str(to_work_item_id), 'linkType': link_type},
    )
    return {'id': str(link.id), 'fromWorkItemId': str(from_work_item_id), 'toWorkItemId': str(to_work_item_id),
            'linkType': link_type, 'deduped': False}
