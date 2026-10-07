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
 2. Asks write_gate.route() what to do with the write, BEFORE it opens that
    transaction or takes a lock, and acts on the answer.
 3. Recomputes the parent rollup synchronously, in the same
    transaction, when a status transition lands on a work item with a
    non-null parent_id — in local mode. In Jira mode the rollup and the
    dependent unblock are pushed to Jira instead and reach this store only
    from Jira's webhook.

**From v5.2 the write gate is a router** (canonical-delivery-state.md REQ-09,
"The routing layer"). The four functions that used to call
`write_gate.assert_gated_write_allowed(mode, origin)` after reading the
project's mode themselves now call `write_gate.route(project, origin)`, which
reads the mode for them and answers `record`, `push` or `refuse`:

  record  today's atomic code, unchanged;
  push    every validation this module runs in local mode apart from the
          gate, then jira_writer.py makes the equivalent Jira write and
          NOTHING is recorded — the change arrives later, as a Jira webhook;
  refuse  WriteGateRejectedError, and nothing is written.

So no function here reads a project's mode, and none branches a write on it:
`append_comment` is the one comment path in every mode, `transition_status`,
`assign_work_item` and `create_link` are the other three routing handlers
(`materialize.materialize_decomposition` is the fifth), and the two derived
writes — `_recompute_parent_rollup` and `_unblock_dependents` — call `route`
themselves with origin ROLLUP rather than going through `transition_status`.
`create_work_item` is deliberately NOT routed: a Jira-mode work item is
created in Jira, so it refuses every origin but JIRA_WEBHOOK, `push`
included.

Catalog-backed assignment validation is reused via workitems/assignment.py
— a Python reimplementation of services/scrummaster/src/assignment.js reading the
SAME on-disk catalog, not a require() (impossible cross-language) and not
a runtime HTTP call — see assignment.py/registry.py's own module comments
for the full tradeoff.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

from django.db import transaction
from django.utils import timezone

from artifacts.models import Artifact

from . import assignment, jira_writer, project_config, status_vocabulary, write_gate
from .models import (
    OutboxEvent, WorkItem, WorkItemArtifact, WorkItemArtifactLink, WorkItemComment, WorkItemHistory, WorkItemLink,
    WorkItemReleaseDetail, WorkItemSpecificationLink, WorkItemStoryDetail,
)

logger = logging.getLogger(__name__)

# canonical-delivery-state.md REQ-02 — the association kinds
# `models.py`'s WorkItemArtifact documents ("'commit', 'pull_request',
# 'ci_build', 'deployment' at minimum"). The same column also carries
# `canonical-work-model.md` REQ-19's durable per-item completion markers,
# whose value is a STEP KEY rather than an artifact kind, so the resolver
# below matches only these four and `record_completion_marker` refuses a
# step key equal to any of them — no marker can ever answer the resolver.
ASSOCIATION_KINDS = ('commit', 'pull_request', 'ci_build', 'deployment')


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
    """A release request — a local Release's `proposed` -> `in-progress`
    (release-mode-parity.md REQ-09) — refused by the one release gate
    (`release_request_refusal`, REQ-11). `outstanding` holds the ids of the
    work items still awaiting acceptance on beta, empty when the cause is
    something else."""
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
    handling) — mirroring `_add_artifact_link`'s identical-payload early
    return below, this returns the existing row unchanged (no write, no
    second event) when it already holds the same (artifact_id,
    requirement_id); a different pair still replaces and emits."""
    _assert_artifact_resolves(artifact_id)
    existing = WorkItemSpecificationLink.objects.filter(
        work_item=item, artifact_id=artifact_id, requirement_id=requirement_id,
    ).first()
    if existing:
        return existing

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

    # NOT routed (REQ-09, "The routing layer"): in Jira mode a work item is
    # created in Jira — by story intake, REQ-10's push or REQ-11's mirror —
    # so a create is refused on `push` as well as on `refuse`, leaving
    # JIRA_WEBHOOK as the one origin that creates one there. That is
    # `canonical-work-model.md` REQ-10's single write path. (Before v5.2 the
    # gate also let ROLLUP through, and nothing ever created with it.)
    if write_gate.route(input['project'], origin) != write_gate.RECORD:
        write_gate.refuse(origin, 'creating a work item')

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

def _assert_assignment_valid(item: WorkItem, agent_id: str, work_item_id) -> None:
    result = assignment.validate_assignment(item.project, agent_id)
    if not result['ok']:
        raise AssignmentRejectedError(
            f'assignment of "{agent_id}" to {work_item_id} rejected: {result["code"]}', result,
        )


def assign_work_item(work_item_id, agent_id: str, *, actor: Optional[str] = None,
                      origin: str = write_gate.Origins.DIRECT,
                      completion_key: Optional[str] = None):
    """REQ-09's assignment handler. The `route` call moved out of the
    `@transaction.atomic` function into this undecorated entry — the shape
    `transition_status` already had over `_transition_status_core` — because
    a handler must ask the router before it opens a transaction or takes a
    lock, and because the Jira call a `push` makes must not run inside
    one."""
    item = get_work_item(work_item_id)
    if not item:
        raise ValidationError(f'assignWorkItem: no work item {work_item_id}')

    verdict = write_gate.route(item.project, origin)
    if verdict == write_gate.REFUSE:
        write_gate.refuse(origin, 'changing a work item\'s assignment')
    if verdict == write_gate.PUSH:
        # Every validation local mode runs apart from the gate, with no row
        # locked and nothing written, and then the Jira write.
        _assert_assignment_valid(item, agent_id, work_item_id)
        return jira_writer.push_assignment(item, agent_id, completion_key=completion_key)

    return _assign_work_item_core(work_item_id, agent_id, actor=actor)


@transaction.atomic
def _assign_work_item_core(work_item_id, agent_id: str, *, actor: Optional[str] = None) -> WorkItem:
    item = WorkItem.objects.select_for_update().filter(id=work_item_id).first()
    if not item:
        raise ValidationError(f'assignWorkItem: no work item {work_item_id}')

    _assert_assignment_valid(item, agent_id, work_item_id)

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


def inward_blockers_of(item: WorkItem) -> list[dict]:
    """Every inward blocker of a work item: its canonical `blocks` links,
    PLUS the Blocks pairs REQ-11's decomposition record holds with it as
    the dependent, "a pair whose blocker has no row yet" counting as not
    `done` (canonical-delivery-state.md REQ-11).

    One rule, in every mode, for the two readers that need it: this
    module's `_unblock_dependents` and `webhook_consumer.py`'s Sub-task
    mirror, which derives a Backlog subtask's status from exactly this set.
    A Jira-mode decomposition's Blocks link reaches `core` as a Jira link
    event that may land after the subtask it names, so a record pair is an
    inward blocker the canonical graph does not hold yet.

    The record row is found by the item's own Jira key where it has one,
    falling back to its canonical id — the proposal id the mirror normally
    gives a mirrored subtask, and §4 records the one case where it does not
    (a Sub-task webhook landing before the writer recorded its key).

    Each entry is `{'blocker_id', 'blocker_status'}`, with `blocker_status`
    None for a blocker with no canonical row, which is not `done`."""
    blockers = _blockers_of(item.id)
    seen = {str(entry['blocker_id']) for entry in blockers}

    record = (jira_writer.proposal_record_for_key(item.external_key) if item.external_key else None) \
        or jira_writer.proposal_record(item.id)
    for pair in (record.blocks_pairs or []) if record is not None else []:
        blocker_key = pair.get('blockerKey')
        blocker_proposal_id = pair.get('blockerProposalId')
        blocker = None
        if blocker_key:
            blocker = WorkItem.objects.filter(external_key=blocker_key).first()
        if blocker is None and blocker_proposal_id:
            blocker = WorkItem.objects.filter(id=blocker_proposal_id).first()
        blocker_id = str(blocker.id) if blocker is not None else (blocker_proposal_id or blocker_key)
        if str(blocker_id) in seen:
            continue
        seen.add(str(blocker_id))
        blockers.append({'blocker_id': blocker_id,
                          'blocker_status': blocker.status if blocker is not None else None})
    return blockers


def _apply_status_change(item: WorkItem, new_status: str, actor: Optional[str],
                          origin: str = write_gate.Origins.DIRECT) -> Optional[dict]:
    """Internal: applies a status transition with no gating/validation of
    its own — caller has already validated. Used both by the public
    transition_status and by the rollup recomputation below, inside the
    SAME transaction.

    `origin` is carried onto the `work_item.status_changed` event
    (canonical-delivery-state.md REQ-09, "Canonical events with a Jira side
    effect": "Every `work_item.status_changed` MUST still carry its write's
    `origin`"). Before v5.2 the event carried none, so a derived write was
    indistinguishable from the transition that triggered it."""
    old_status = item.status
    if old_status == new_status:
        return None  # no-op: if there's no change, nothing to record or publish.

    item.status = new_status
    item.updated_at = timezone.now()
    item.save(update_fields=['status', 'updated_at'])
    _append_history(item.id, 'status', old_status, new_status, actor)
    _write_outbox_event(
        project=item.project, event_type='work_item.status_changed', work_item_id=item.id,
        payload={'id': str(item.id), 'status': new_status, 'previous': old_status, 'origin': origin},
    )
    return {'id': item.id, 'status': new_status, 'previous': old_status}


def _recompute_parent_rollup(parent_id, actor: Optional[str]) -> None:
    """Recompute a parent's canonical status against its
    children's CURRENT states, synchronously, in the same transaction as
    the triggering child transition. Default policy: all children done =>
    parent done. Any child cancelled/failed => parent holds its last
    non-terminal status (no automatic resolution). Recurses if the parent
    itself has a parent."""
    parent = WorkItem.objects.filter(id=parent_id).first()
    if not parent:
        return

    # REQ-09, "Derived writes go through the router too": this is a write
    # like any other, so it asks the router rather than writing past the
    # gate through `_apply_status_change` as it did up to v5.1. It does NOT
    # call `transition_status` — the rollup is not a caller's transition and
    # must not re-run its release gate or its release event.
    verdict = write_gate.route(parent.project, write_gate.Origins.ROLLUP)
    if verdict == write_gate.RECORD:
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
        if verdict == write_gate.PUSH:
            # No canonical change: the writer's `transition_issue` is
            # registered with `transaction.on_commit`, and `core` records the
            # parent's `done` from that issue's own webhook. A grandparent's
            # rollup runs then, not now. §4 states the cost: the chain is one
            # webhook round trip longer, and a lost webhook or a failed push
            # stalls it.
            jira_writer.register_derived_status_push(parent, target)
            return
        changed = _apply_status_change(parent, target, 'system:rollup', write_gate.Origins.ROLLUP)
        if changed and parent.parent_id:
            _recompute_parent_rollup(parent.parent_id, actor)


RELEASE_EVENT_TYPE = 'work_item.jira_release_event'
RELEASE_CANDIDATE_RECORDED_EVENT_TYPE = 'work_item.release_candidate_recorded'


def publish_release_event(item: WorkItem, kind: str, *, stream_project: str) -> None:
    """**The one writer of every `work_item.jira_release_event`**
    (release-mode-parity.md REQ-10), in both modes: `requested`, `done` or
    `abandoned`. Its two callers are `_transition_status_core` (a Release
    change whose origin is not JIRA_WEBHOOK) and `webhook_consumer.py`'s
    release handlers, and each passes the stream the outbox row goes on:
    `item.project` in local mode, the Release ticket's own containing
    project in Jira mode (v5.2 Pass 9). The payload's `project` is always
    the Release's Target Project, `item.project`.

    Must be called inside the caller's `transaction.atomic()` block, beside
    the change the event describes, so a crash between them loses neither."""
    _write_outbox_event(
        project=stream_project, event_type=RELEASE_EVENT_TYPE, work_item_id=item.id,
        payload={'kind': kind, 'workItemId': str(item.id), 'project': item.project},
    )


def _release_event_kind(previous_status: str, new_status: str) -> Optional[str]:
    """Which release event a recorded change of a Release publishes, keyed on
    the MOVE rather than on the new status alone (REQ-10): `requested` on
    `proposed` -> `in-progress` only — the move the release gate guards — so
    a Release moved back to `in-progress` from `in-review` cuts no second
    candidate; `done` on any change to `done`; `abandoned` on any change to
    `cancelled`."""
    if previous_status == 'proposed' and new_status == 'in-progress':
        return 'requested'
    if new_status == 'done':
        return 'done'
    if new_status == 'cancelled':
        return 'abandoned'
    return None


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


class ReleaseRefusal:
    """What `release_request_refusal` returns when a release request is
    refused: the message, and the work items still awaiting acceptance on
    beta when the beta queue is the cause (empty otherwise)."""

    def __init__(self, message: str, outstanding: Optional[list] = None):
        self.message = message
        self.outstanding = list(outstanding or [])


RELEASE_REJECTION_AUTHOR = 'system'


def release_request_refusal(item: WorkItem, *, target_project_set: bool = True) -> Optional[ReleaseRefusal]:
    """**The one release gate** (release-mode-parity.md REQ-11), in both
    modes. Runs the two checks in turn — the Target Project is set, then
    the Target Project's beta queue is clear — writes nothing, and returns
    the refusal, or None when the request may proceed.

    `target_project_set` is the caller's: `_handle_release_requested` passes
    whether `parse_release_fields` found the field, read before
    `_materialize_release`'s fallback to the ticket's own project (v5.2
    REQ-08); a local Release's project is its Target Project, so local mode
    passes nothing. Only the first failing check is reported, so a request
    is refused for one reason at a time."""
    if not target_project_set:
        return ReleaseRefusal('Target Project is not set')
    outstanding = _release_beta_queue_outstanding(item.project)
    if outstanding:
        return ReleaseRefusal(f'{len(outstanding)} work item(s) are still awaiting acceptance on beta', outstanding)
    return None


def _release_rejection_text(refusal: ReleaseRefusal) -> str:
    """v5.2's rejection format ("Rejections, in every mode"), the one text a
    refused release request gets in either mode: the rejection line, the
    outstanding items when the beta queue is the cause, and the closing
    instruction (REQ-11)."""
    body = f'[system] release request rejected: {refusal.message}'
    if refusal.outstanding:
        body += '\n\n' + '\n'.join(
            f'  - {o.display_name} ({o.external_key or o.id})' for o in refusal.outstanding
        )
    return body + '\n\nResolve this and request the release again.'


def post_release_rejection(item: WorkItem, refusal: ReleaseRefusal, *, source_message_id: Optional[str]) -> dict:
    """**The one poster of a release refusal** (REQ-11): one comment, through
    the one comment path, keyed `source_message_id` so a redelivered request
    adds none — `<completion_key>:release-gate` from `check_release_request`,
    `<messageId>:release-check` from the Jira webhook consumer. Called with
    no transaction open by both, so a Jira-mode post is made in-process and
    its failure fails the caller's message."""
    return append_comment(
        item.id, RELEASE_REJECTION_AUTHOR, _release_rejection_text(refusal),
        source_message_id=source_message_id, origin=write_gate.Origins.DIRECT,
    )


def check_release_request(item: WorkItem, *, source_message_id: Optional[str]) -> None:
    """Local mode's release gate (REQ-11): runs `release_request_refusal`
    and, on a refusal, posts it with `post_release_rejection` and then raises
    `ReleaseGateError`. `transition_status` calls it OUTSIDE
    `_transition_status_core`'s atomic block, so the comment survives the
    raise that refuses the transition."""
    refusal = release_request_refusal(item)
    if refusal is None:
        return
    post_release_rejection(item, refusal, source_message_id=source_message_id)
    raise ReleaseGateError(
        f'{item.id}: release request rejected: {refusal.message}',
        [str(o.id) for o in refusal.outstanding],
    )


def transition_status(work_item_id, new_status: str, *, actor: Optional[str] = None,
                       origin: str = write_gate.Origins.DIRECT,
                       completion_key: Optional[str] = None):
    """`origin` per write_gate.Origins — webhook_consumer.py passes
    JIRA_WEBHOOK after its own pre-validation; direct callers
    (Streams commands, admin UI in local mode) pass DIRECT/ADMIN_UI.

    A Release's request — its `proposed` -> `in-progress` move
    (release-mode-parity.md REQ-09) — additionally passes the one release
    gate, `check_release_request` (REQ-11), which comments and raises on a
    refusal. It runs HERE, outside `_transition_status_core`'s atomic
    block, because a comment written inside the same atomic block as a
    subsequent raise would roll back along with it. Every internal-API
    surface (Django Admin, external HTTP API, Streams command) reaches this
    same gate, since all three call this function rather than the atomic
    core directly.

    `completion_key`, when given, is the command or event's `messageId`
    (REQ-09, step 5): on `push` the writer skips any step its completion
    record already shows done, and the release gate's comment is keyed
    `<completion_key>:release-gate` so a redelivered command adds none. The
    admin and the external API pass none.

    For a Release, `origin=JIRA_WEBHOOK` runs no release gate and (in
    `_transition_status_core`) publishes no release event
    (canonical-delivery-state.md REQ-08, kept by release-mode-parity.md
    REQ-10): `webhook_consumer.py`'s release handlers are Jira mode's
    publishers, and a Jira status echo must not publish a second event.
    This keys on the origin, not the mode: `JIRA_WEBHOOK` reaches here only
    for a project in Jira mode (`webhook_consumer.py` only passes it after
    its own mode check)."""
    item = get_work_item(work_item_id)
    if not item:
        raise ValidationError(f'transitionStatus: no work item {work_item_id}')

    verdict = write_gate.route(item.project, origin)
    if verdict == write_gate.REFUSE:
        write_gate.refuse(origin, 'changing a work item\'s status')

    if (item.type == 'release' and item.status == 'proposed' and new_status == 'in-progress'
            and origin != write_gate.Origins.JIRA_WEBHOOK):
        check_release_request(
            item, source_message_id=f'{completion_key}:release-gate' if completion_key else None,
        )

    if verdict == write_gate.PUSH:
        # Every validation local mode runs apart from the gate (the release
        # gate above included), with no row locked and nothing written, then
        # the Jira write. The change reaches this store only from Jira's own
        # webhook, so nothing is recorded here.
        _validate_transition(item, new_status)
        return jira_writer.push_status(item, new_status, completion_key=completion_key)

    return _transition_status_core(work_item_id, new_status, actor=actor, origin=origin)


def _validate_transition(item: WorkItem, new_status: str) -> dict:
    """Every validation a status transition runs apart from the gate
    itself: the status vocabulary, a story's required fields, and the
    dependency gate. Shared by the recording path (inside its transaction,
    under its row lock) and the pushing path (which locks nothing and
    writes nothing), so a Jira-mode push is refused by exactly what refuses
    the same write in local mode."""
    custom_statuses = project_config.get_custom_statuses(item.project)
    validity = status_vocabulary.validate_status(new_status, custom_statuses)
    if not validity['ok']:
        raise ValidationError(f'transitionStatus: {validity["reason"]}')

    if item.type == 'story' and validity['baseline'] != 'proposed':
        detail = WorkItemStoryDetail.objects.filter(work_item_id=item.id).first()
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
        blockers = _blockers_of(item.id)
        incomplete = [b for b in blockers if b['blocker_status'] != 'done']
        if incomplete:
            raise DependencyGateError(
                f'{item.id} cannot transition to "{new_status}" — {len(incomplete)} blocker(s) not yet done',
                [b['blocker_id'] for b in incomplete],
            )

    return validity


@transaction.atomic
def _transition_status_core(work_item_id, new_status: str, *, actor: Optional[str] = None,
                             origin: str = write_gate.Origins.DIRECT) -> WorkItem:
    item = WorkItem.objects.select_for_update().filter(id=work_item_id).first()
    if not item:
        raise ValidationError(f'transitionStatus: no work item {work_item_id}')

    validity = _validate_transition(item, new_status)

    changed = _apply_status_change(item, new_status, actor or 'system', origin)
    if changed and item.parent_id:
        _recompute_parent_rollup(item.parent_id, actor)
    if changed and validity['baseline'] == 'done':
        _unblock_dependents(work_item_id, actor)
    if changed and item.type == 'release' and origin != write_gate.Origins.JIRA_WEBHOOK:
        # The first of `publish_release_event`'s two callers (REQ-10). The
        # other is `webhook_consumer.py`'s release handlers, which apply a
        # Jira-mode Release's change through this function with
        # `origin=JIRA_WEBHOOK`; this branch publishes nothing for that
        # origin, so a Jira status echo cuts no second candidate and
        # promotes nothing twice (canonical-delivery-state.md REQ-08).
        kind = _release_event_kind(changed['previous'], new_status)
        if kind:
            publish_release_event(item, kind, stream_project=item.project)

    return get_work_item(work_item_id)


# ---------------------------------------------------------------------------
# Release-candidate writeback
# ---------------------------------------------------------------------------

def release_candidate_note(candidate_sha: str, build_identifier: Optional[str] = None,
                            preview_url: Optional[str] = None) -> str:
    """The one candidate note, in both modes (release-mode-parity.md
    REQ-12): the build and preview lines only when their values are
    present, and the release PR, which is built from the SHA."""
    first = f'Release candidate cut: {candidate_sha}'
    if build_identifier:
        first += f' (build {build_identifier})'
    lines = [first]
    if preview_url:
        lines.append(f'Preview: {preview_url}')
    lines.append(f'Release PR: release/{candidate_sha} → prod')
    return '\n'.join(lines)


def record_release_candidate_step(item: WorkItem, *, candidate_sha: str, build_identifier: Optional[str] = None,
                                   preview_url: Optional[str] = None, author: str = 'jenkins') -> None:
    """**The candidate's recording step** (REQ-13): publish
    `work_item.release_candidate_recorded` and post REQ-12's note. One step,
    two callers: `record_release_candidate` in local mode, and
    `webhook_consumer.py`'s Release sync in Jira mode, once the webhook has
    recorded a new, non-empty candidate SHA.

    Must be called inside the caller's `transaction.atomic()` block, beside
    the recorded fields, so the fields and their event commit together: a
    field recorded without its event would never publish, since the
    redelivered webhook then changes nothing. The note goes through the one
    comment path, which records it in the same transaction in local mode
    and, in Jira mode, registers its post for after the commit (no Jira call
    is made inside an open transaction)."""
    _write_outbox_event(
        project=item.project, event_type=RELEASE_CANDIDATE_RECORDED_EVENT_TYPE, work_item_id=item.id,
        payload={'id': str(item.id), 'candidateSha': candidate_sha, 'buildIdentifier': build_identifier,
                 'previewUrl': preview_url},
    )
    append_comment(item.id, author, release_candidate_note(candidate_sha, build_identifier, preview_url))


def record_release_candidate(work_item_id, *, candidate_sha: str, build_identifier: Optional[str] = None,
                              preview_url: Optional[str] = None, actor: Optional[str] = None) -> WorkItem:
    """Local mode's record of a candidate the release-candidate job reports
    (REQ-13, "In local mode the endpoint records directly"): the three
    fields, then the recording step, in one transaction.
    Write-once-per-candidate: a new candidate replaces the three fields, it
    does not append.

    In Jira mode the report records nothing (`report_release_candidate`
    pushes the fields to Jira) and the same recording step runs from the
    Release's webhook instead."""
    with transaction.atomic():
        item = _record_release_candidate_fields(
            work_item_id, candidate_sha=candidate_sha, build_identifier=build_identifier,
            preview_url=preview_url,
        )
        record_release_candidate_step(
            item, candidate_sha=candidate_sha, build_identifier=build_identifier,
            preview_url=preview_url, author=actor or 'jenkins',
        )
    return get_work_item(work_item_id)


def _get_release(work_item_id, what: str) -> WorkItem:
    item = get_work_item(work_item_id)
    if not item or item.type != 'release':
        raise ValidationError(f'{what}: no release work item {work_item_id}')
    return item


def _record_release_candidate_fields(work_item_id, *, candidate_sha: str,
                                      build_identifier: Optional[str] = None,
                                      preview_url: Optional[str] = None) -> WorkItem:
    item = _get_release(work_item_id, 'recordReleaseCandidate')

    detail = WorkItemReleaseDetail.objects.filter(work_item_id=item.id).first() \
        or WorkItemReleaseDetail(work_item=item)
    detail.candidate_sha = candidate_sha
    detail.build_identifier = build_identifier
    detail.preview_url = preview_url
    detail.save()
    return item


def native_build_comment_text(native_build_url: Optional[str], native_build_status: Optional[str]) -> Optional[str]:
    """REQ-12's native-build comment, `Native build (<status>): <URL>`, or
    None when the report carries neither field (the release-candidate job
    sends both only when a native build ran)."""
    if not native_build_url and not native_build_status:
        return None
    return f'Native build ({native_build_status or "unknown"}): {native_build_url or "(no URL)"}'


def report_release_candidate(work_item_id, *, candidate_sha: str, build_identifier: Optional[str] = None,
                              preview_url: Optional[str] = None, native_build_url: Optional[str] = None,
                              native_build_status: Optional[str] = None, actor: Optional[str] = None,
                              origin: str = write_gate.Origins.EXTERNAL_API):
    """The release-candidate job's report (`/admin/work-items/<id>/release-candidate`),
    routed like every other machine write (release-mode-parity.md REQ-13,
    "The report"; v5.2 REQ-09's `write_gate.route`). Called with no
    transaction open.

      push    (Jira mode) the writer sends the three candidate fields to the
              Release ticket in ONE edit, then the native-build comment is
              posted through the comment path (REQ-12), and NOTHING is
              recorded: the fields come back on the ticket's webhook, which
              runs the recording step. A failed push raises to the job, which
              fails visibly. Returns the writer's posted result.
      record  (local mode) the native-build comment, then the fields and the
              recording step (`record_release_candidate`), then the move to
              `in-review` (REQ-09), origin `origin` — the same order Jira mode
              reaches them in — all in ONE transaction that first re-checks
              `in-progress` under the Release's row lock. Returns the work
              item.

    The native-build comment is keyed `release-candidate:<sha>:native-build`,
    so a repeated report with the same SHA posts it once, in either mode.

    **A report only for an `in-progress` Release, in both modes** (REQ-09).
    The report is accepted only while `core` holds the Release at
    `in-progress`. At any other status (`proposed`, `in-review`, `done`,
    `cancelled`) it raises `ValidationError` before anything else happens —
    before the write gate, the native-build comment, the Jira push and the
    local record — so nothing is recorded, no comment is posted, nothing is
    pushed, and the endpoint returns a 4xx the job's `curl -sf` fails on. A
    late report therefore never reopens a finished Release, and the next
    Done never publishes a second `done`. A new candidate for an `in-review`
    Release needs it moved back to `in-progress` first, a move that
    publishes no release event (`_release_event_kind`).

    In local mode the same check is made again under the Release's row lock,
    in the transaction that posts the native-build comment, records the
    candidate and makes the `in-review` move, so a cancel or Done that
    commits after the first check refuses the report the same way and
    leaves nothing of it behind."""
    item = _get_release(work_item_id, 'reportReleaseCandidate')
    _refuse_report_unless_in_progress(item)

    verdict = write_gate.route(item.project, origin)
    if verdict == write_gate.REFUSE:
        write_gate.refuse(origin, 'recording a release candidate')

    native_text = native_build_comment_text(native_build_url, native_build_status)
    author = actor or 'jenkins'

    def post_native_build_comment():
        if native_text is not None:
            append_comment(item.id, author, native_text,
                           source_message_id=f'release-candidate:{candidate_sha}:native-build', origin=origin)

    if verdict == write_gate.PUSH:
        result = jira_writer.push_release_candidate_fields(
            item, candidate_sha=candidate_sha, build_identifier=build_identifier, preview_url=preview_url,
        )
        post_native_build_comment()
        return result

    with transaction.atomic():
        # The guard above ran unlocked. A cancel or a Done that commits
        # after it would otherwise be overwritten by the move below, which
        # checks no transition graph; so the status is checked again here
        # under the Release's row lock, and the comment, the record and the
        # move land together on an `in-progress` Release or not at all.
        locked = WorkItem.objects.select_for_update().filter(id=item.id).first()
        _refuse_report_unless_in_progress(locked)
        post_native_build_comment()
        record_release_candidate(
            work_item_id, candidate_sha=candidate_sha, build_identifier=build_identifier,
            preview_url=preview_url, actor=author,
        )
        # The atomic core, not `transition_status`: `in-progress` ->
        # `in-review` runs no release gate (that is `proposed` ->
        # `in-progress` only), and this branch is already routed RECORD, so
        # `transition_status` would add only a second `write_gate.route`,
        # whose PUSH branch makes a Jira call — never to be made inside this
        # open transaction, should the project's mode change mid-report.
        return _transition_status_core(work_item_id, 'in-review', actor=author, origin=origin)


def _refuse_report_unless_in_progress(item: WorkItem) -> None:
    """REQ-09's precondition on a release-candidate report: `core` holds the
    Release at `in-progress`. The advice to move the Release back to
    `in-progress` is given only for `in-review`, the one status REQ-09 says
    takes a new candidate that way; for `done` or `cancelled` that move
    would lead to a second `done`, and for `proposed` it is the release
    request itself."""
    if item.status == 'in-progress':
        return
    message = (f'reportReleaseCandidate: release {item.id} is at "{item.status}"; a candidate is '
               'reported only for a release at "in-progress"')
    if item.status == 'in-review':
        message += ' — move it back to "in-progress" first'
    elif item.status == 'proposed':
        message += ' — the release must be requested first'
    raise ValidationError(message)


def _unblock_dependents(blocker_work_item_id, actor: Optional[str]) -> None:
    """The dependency graph's "Done Handler" logic, ported to operate against
    this service's own graph instead of Jira issue links. Stateless and
    idempotent: a dependent only moves if it is CURRENTLY
    'waiting-on-dependency' with every blocker done, which becomes false
    the instant it has already moved.

    **The dependents are the canonical `blocks` links PLUS the Blocks pairs
    REQ-11's decomposition record holds, in every mode** (§4's "Jira-mode
    unblocking from the decomposition record" row; REQ-11:
    "`_unblock_dependents` applies the same rule in every mode and reads no
    mode"). In Jira mode a decomposition's Blocks link reaches `core` as a
    Jira link event, which may land after the subtask it names, so the
    canonical link may not exist yet when the blocker reaches `done` — and
    the record, written by the writer as it created each link, is what
    stops the dependent being stranded. Only a Jira-mode decomposition
    writes that record, so a project that was never in Jira mode has no
    pair to count; a project returned to local mode by `disconnect_jira`
    keeps its pairs, and they still count, so an operator who deletes a
    blocker the record names moves its dependent by hand (REQ-11).

    No mode is read to decide this: the union is the same in both modes,
    and each dependent is routed by `write_gate.route` exactly as before."""
    linked_ids = list(WorkItemLink.objects.filter(
        from_work_item_id=blocker_work_item_id, link_type='blocks',
    ).values_list('to_work_item_id', flat=True))
    recorded_ids = jira_writer.dependent_proposal_ids_for_blocker(blocker_work_item_id)

    seen: set = set()
    dependent_ids = []
    for dependent_id in [*linked_ids, *recorded_ids]:
        if str(dependent_id) in seen:
            continue
        seen.add(str(dependent_id))
        dependent_ids.append(dependent_id)

    for dependent_id in dependent_ids:
        dependent = WorkItem.objects.filter(id=dependent_id).first()
        if not dependent:
            continue

        # REQ-09, "Derived writes go through the router too" — the same rule
        # the parent rollup follows, for the same reason.
        verdict = write_gate.route(dependent.project, write_gate.Origins.ROLLUP)
        if verdict == write_gate.RECORD:
            dependent = WorkItem.objects.select_for_update().filter(id=dependent_id).first()
            if not dependent:
                continue
        if dependent.status != 'waiting-on-dependency':
            continue

        # The same inward-blocker rule the mirror applies, so a dependent
        # whose Blocks pair the record holds but whose canonical link has
        # not landed yet is not moved early (REQ-11).
        blockers = inward_blockers_of(dependent)
        all_done = all(b['blocker_status'] == 'done' for b in blockers)
        if not all_done:
            continue

        if verdict == write_gate.PUSH:
            # No canonical change: `core` records the dependent's `ready`
            # from its own Jira webhook. One webhook can register two
            # pushes of the same status for one dependent (this unblock and
            # REQ-11's link reconciliation); the second is harmless —
            # either Jira re-applies a transition whose webhook changes
            # nothing here, or the writer finds the issue already there
            # with its Blocked flag clear and counts it done.
            jira_writer.register_derived_status_push(dependent, 'ready')
            continue

        changed = _apply_status_change(dependent, 'ready', 'system:unblock', write_gate.Origins.ROLLUP)
        if changed and dependent.parent_id:
            _recompute_parent_rollup(dependent.parent_id, actor)


# ---------------------------------------------------------------------------
# Artifact association (not subject to the write-gate)
# ---------------------------------------------------------------------------

def normalize_reference(reference: Optional[str]) -> Optional[str]:
    """canonical-delivery-state.md REQ-01 — "REQ-02 and REQ-03 normalize a
    reference the same way before matching or writing it: lower-case scheme,
    host, owner and repository; no trailing slash, query or fragment."

    A pull request is identified by its web URL,
    `https://github.com/<owner>/<repo>/pull/<number>` — the form
    `gh pr create` prints and an agent passes as `--pull-request`, and the
    form Jenkins emits as a PR's `html_url` (or `CHANGE_URL`). The two
    producers can differ in case or a trailing slash and mean the same pull
    request, so both are normalized here, by one function, rather than at
    each site.

    A reference that is not a URL — a bare commit SHA — has no scheme or
    host to lower-case and is returned stripped and unchanged, so the
    completion-marker reuse of the same column (REQ-19) is unaffected."""
    if reference is None:
        return None
    text = str(reference).strip()
    if not text:
        return text
    parsed = urlsplit(text)
    if not parsed.scheme or not parsed.netloc:
        return text

    segments = parsed.path.rstrip('/').split('/')
    # '' / '<owner>' / '<repo>' / 'pull' / '<number>' — the owner and the
    # repository are case-insensitive on GitHub; the rest of the path (a PR
    # number, a ref name) is not and is left exactly as given.
    for index in (1, 2):
        if len(segments) > index:
            segments[index] = segments[index].lower()
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), '/'.join(segments), '', ''))


def resolve_work_items_for_reference(reference: str, *, message: Optional[str] = None) -> list[str]:
    """canonical-delivery-state.md REQ-02 — the reverse lookup: a commit
    SHA or pull-request reference to the work item(s) that recorded it.

    Matches only `ASSOCIATION_KINDS`, never a completion marker's step key,
    and matches the NORMALIZED reference, so an agent's `--pull-request`
    URL and Jenkins' `html_url` for the same pull request resolve to each
    other even when they differ only in case or a trailing slash.

    Resolution is total in its failure handling: a reference with no
    association is logged at error level, naming the reference and the
    message that carried it, rather than dropped silently — a silently
    dropped deployment is the class of defect V5 exists to remove. No table
    holds it (Shovel Ready Pass 5, decision 5.3).

    The query is answered by `idx_work_item_artifact_ref`, the
    `(artifact_type, reference)` index this requirement's migration adds."""
    normalized = normalize_reference(reference)
    rows = WorkItemArtifact.objects.filter(
        artifact_type__in=ASSOCIATION_KINDS, reference=normalized,
    ).values_list('work_item_id', flat=True)
    work_item_ids = list(dict.fromkeys(str(row) for row in rows))

    if not work_item_ids:
        logger.error(
            '[store] Unresolved delivery reference "%s" (normalized "%s") carried by %s — '
            'no work item recorded it, so nothing could be attributed to it',
            reference, normalized, message or '(no message named)',
        )
    return work_item_ids


@transaction.atomic
def attach_artifact(work_item_id, artifact_type: str, reference: str, *, actor: Optional[str] = None) -> dict:
    """Not routed by the write gate: an association is neither a status,
    an assignment, a dependency link nor a comment, and Jira carries no
    field for one (§4).

    REQ-03 makes this idempotent for an association kind: ScrumMaster
    records the `pull-request` Artifact's URL on every completion, and
    `publishCommand`'s `dedupeKey` expires, so a repeated completion must
    not leave a second row. The reference is normalized first (REQ-01), so
    the same pull request under a different case or a trailing slash is the
    same row. A completion marker (REQ-19), whose `artifact_type` is a step
    key rather than an association kind, is unaffected — `record_completion_marker`
    has its own marker check and refuses a step key that collides with these
    kinds."""
    item = get_work_item(work_item_id)
    if not item:
        raise ValidationError(f'attachArtifact: no work item {work_item_id}')

    if artifact_type in ASSOCIATION_KINDS:
        reference = normalize_reference(reference)
        existing = WorkItemArtifact.objects.filter(
            work_item=item, artifact_type=artifact_type, reference=reference,
        ).first()
        if existing:
            return {'id': str(existing.id), 'workItemId': str(work_item_id),
                    'artifactType': artifact_type, 'reference': reference, 'deduped': True}

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
    """canonical-delivery-state.md REQ-02 — a step key equal to one of the
    association kinds is refused, and no row is written. The marker reuses
    `work_item_artifact.artifact_type` for a value that is a step key rather
    than an artifact kind, and REQ-02's resolver queries that same column:
    were a marker allowed to use one of the four kinds as its step key, the
    resolver would resolve a deployment to whatever work item happened to
    carry that marker."""
    if step_key in ASSOCIATION_KINDS:
        raise ValidationError(
            f'recordCompletionMarker: "{step_key}" is one of the delivery-association kinds '
            f'({", ".join(ASSOCIATION_KINDS)}) and cannot be used as a completion-marker step key — '
            'it would be matched by the commit/pull-request resolver'
        )
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


def append_comment(work_item_id, author: str, body: str, *, reference_file: Optional[str] = None,
                    reference_function: Optional[str] = None, source_message_id: Optional[str] = None,
                    origin: str = write_gate.Origins.DIRECT) -> dict:
    """**The one comment path, in every mode** (REQ-09, "One comment path,
    in every mode"). Every comment on a work item from any caller — the
    `appendComment` command, the admin, the external API, and `core`'s own
    logic (REQ-01's failure comment, REQ-05's evidence,
    `record_release_candidate`'s note, story intake's comments, REQ-08's
    release rejection, and the rejection comments) — comes through here.

    It is the only code on a caller's path whose comment write differs by
    mode, and it reads no mode: `write_gate.route` decides.

      record  the comment is recorded atomically, as today, deduplicated on
              `source_message_id`;
      push    the writer posts `[<author>] <body>` to Jira and NOTHING is
              recorded — the comment reaches `core` on its `comment_created`
              webhook, origin JIRA_WEBHOOK, which `_handle_comment_event`
              records through this same function. `source_message_id` is
              then the completion record's key instead of the row's dedupe
              key, because there is no row;
      refuse  origin ADMIN_UI only, because in a Jira-mode project a person
              comments in Jira.

    On `push` this function opens NO transaction. It posts, or — when its
    caller already holds one (`connection.in_atomic_block`) — registers the
    post with `transaction.on_commit` and returns at once, so the caller
    keeps its own ordering (REQ-05's evidence-before-transition) and no Jira
    call is ever made inside an open transaction."""
    item = get_work_item(work_item_id)
    if not item:
        raise ValidationError(f'appendComment: no work item {work_item_id}')

    verdict = write_gate.route(item.project, origin)
    if verdict == write_gate.REFUSE:
        write_gate.refuse(origin, 'commenting on a work item')
    if verdict == write_gate.PUSH:
        return jira_writer.push_comment(item, author, body, completion_key=source_message_id)

    return _append_comment_core(
        work_item_id, author, body, reference_file=reference_file,
        reference_function=reference_function, source_message_id=source_message_id,
    )


@transaction.atomic
def _append_comment_core(work_item_id, author: str, body: str, *, reference_file: Optional[str] = None,
                          reference_function: Optional[str] = None,
                          source_message_id: Optional[str] = None) -> dict:
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

def create_link(from_work_item_id, to_work_item_id, link_type: str, *, actor: Optional[str] = None,
                 origin: str = write_gate.Origins.DIRECT,
                 completion_key: Optional[str] = None) -> dict:
    """REQ-09's link handler. Like `assign_work_item`, the `route` call
    moved out of the `@transaction.atomic` function into this undecorated
    entry. On `push` the result names the DEPENDENT (`to_work_item_id`) —
    the item whose history records a link in local mode."""
    from_item = get_work_item(from_work_item_id)
    to_item = get_work_item(to_work_item_id)
    if not from_item or not to_item:
        raise ValidationError('createLink: both work items must exist')
    if from_item.project != to_item.project:
        raise ValidationError('createLink: work items must belong to the same project')

    verdict = write_gate.route(from_item.project, origin)
    if verdict == write_gate.REFUSE:
        write_gate.refuse(origin, 'creating a dependency link')
    if verdict == write_gate.PUSH:
        return jira_writer.push_link(from_item, to_item, completion_key=completion_key)

    return _create_link_core(from_work_item_id, to_work_item_id, link_type, actor=actor)


@transaction.atomic
def _create_link_core(from_work_item_id, to_work_item_id, link_type: str, *,
                       actor: Optional[str] = None) -> dict:
    from_item = get_work_item(from_work_item_id)
    to_item = get_work_item(to_work_item_id)
    if not from_item or not to_item:
        raise ValidationError('createLink: both work items must exist')

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
