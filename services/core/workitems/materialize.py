"""
The internal dependency graph store, porting the order-independent
multi-pass materialization and no-progress handling to
operate against it. Direct port of the Node service's src/materialize.js,
itself a port of services/scrummaster/src/dependencies.js's materializeDecomposition
algorithm — same order-independent multi-pass loop, same no-progress
detection, same atomic all-or-nothing assignment validation — retargeted
at this service's own store (work_item/work_item_link) instead of Jira
issues. Used for a project in LOCAL mode.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from django.db import transaction

from . import assignment, jira_writer, store, write_gate

logger = logging.getLogger(__name__)


class MaterializationValidationError(Exception):
    code = 'MATERIALIZATION_VALIDATION_ERROR'

    def __init__(self, rejected, permitted_agents):
        super().__init__('Decomposition rejected — one or more subtasks reference an agent not permitted for this project')
        self.rejected = rejected
        self.permitted_agents = permitted_agents


class MaterializationNoProgressError(Exception):
    code = 'MATERIALIZATION_NO_PROGRESS'

    def __init__(self, unresolved):
        super().__init__('Decomposition materialization made no progress — unresolved blocker reference, self-dependency, or a cycle')
        self.unresolved = unresolved


class MaterializationUnresolvedArtifactError(Exception):
    """v4.1 agent-artifact-automation.md REQ-01 (build brief §1b
    carry-forward 6). store.create_work_item's own UnresolvedArtifactError
    (store.py's _assert_artifact_resolves) names only the artifact id;
    REQ-01's acceptance also needs the offending subtask id, which this
    wraps in at the one place — this per-proposal loop — that still knows
    which subtask was being materialized when the check failed. store.py is
    not edited. `code` matches store.UnresolvedArtifactError's own so
    command_consumer.py's existing PERMANENT_REJECTION_CODES entry for
    UNRESOLVED_ARTIFACT covers this too, with no change there."""
    code = 'UNRESOLVED_ARTIFACT'

    def __init__(self, subtask_id, artifact_error: str):
        super().__init__(f'materializeDecomposition: subtask {subtask_id} could not be created — {artifact_error}')
        self.subtask_id = subtask_id
        self.artifact_error = artifact_error




def plan_decomposition(subtasks: list[dict], *, resolved_ids=frozenset()) -> dict:
    """The order-independent multi-pass resolution of a decomposition's
    proposal graph, which **writes nothing**.

    Extracted from this module's own write loop
    (canonical-delivery-state.md REQ-11: "computed on the proposal graph
    (extracted from `materialize.py`'s write loop ... into a function that
    writes nothing) before any Jira call"), because a Jira-mode
    decomposition has to know the graph is sound — every `Blocked By`
    naming a proposal in the same command, no self-dependency and no cycle
    — before it creates a single Jira Sub-task, and "creates nothing on a
    rejection" is not something a Jira create can be rolled back into.

    `resolved_ids` are proposals that already exist and therefore count as
    resolvable blockers without being created again: in local mode the ids
    this service's own store already holds, in Jira mode the proposals the
    decomposition record already holds a Jira key for.

    Returns `{'order': [...], 'pairs': [(blocker_id, dependent_id), ...],
    'rootIds': {...}}` — `order` is every proposal in an order in which
    each proposal's blockers come first, `pairs` the Blocks edges to create
    (deduplicated), `rootIds` the proposals with no blocker at all, which
    are the ones that enter `ready`/Shovel Ready.

    Raises `MaterializationNoProgressError` naming every proposal still
    unresolved, exactly as the write loop did when its pass made no
    progress.

    The multi-pass shape is kept as it was — reverse iteration within a
    pass, removing each resolved proposal — so the order this returns is
    the order the write loop already created in."""
    resolved: set = set(resolved_ids)
    order: list[dict] = []
    pairs: list[tuple] = []
    linked_pairs: set[str] = set()

    pending = list(subtasks)
    progressed = True

    # Order-independent multi-pass resolution: a subtask resolved earlier in
    # THIS pass counts as resolvable for a later one in the same pass.
    while pending and progressed:
        progressed = False

        for i in range(len(pending) - 1, -1, -1):
            proposal = pending[i]
            blocked_by = proposal.get('Blocked By') or []

            if not all(blocker_id in resolved for blocker_id in blocked_by):
                continue

            order.append(proposal)
            resolved.add(proposal['id'])

            for blocker_id in blocked_by:
                pair_key = f'{blocker_id}->{proposal["id"]}'
                if pair_key not in linked_pairs:
                    pairs.append((blocker_id, proposal['id']))
                    linked_pairs.add(pair_key)

            del pending[i]
            progressed = True

    if pending:
        raise MaterializationNoProgressError([
            {'id': p['id'], 'displayName': p.get('displayName'), 'blockedBy': p.get('Blocked By') or []}
            for p in pending
        ])

    return {
        'order': order,
        'pairs': pairs,
        'rootIds': {s['id'] for s in subtasks if not s.get('Blocked By')},
    }


def _assert_proposal_artifacts_resolve(proposal: dict) -> None:
    """The artifact resolution `create_work_item` runs for us in local mode
    (`store._assert_artifact_resolves`, reached from
    `_record_specification_link` and `_add_artifact_link`), run explicitly
    and with no write for the pushing path — REQ-11 requires it "before any
    Jira call". The error is wrapped in the same
    `MaterializationUnresolvedArtifactError` the recording path raises, so
    both modes reject an unresolved artifact identically: one permanent
    rejection, one comment, the parent's `needs-clarification` or Blocked
    flag."""
    try:
        spec_link = proposal.get('specificationLink')
        if spec_link:
            store._assert_artifact_resolves(spec_link['artifactId'])
        for artifact_id in (proposal.get('artifactLinks') or []):
            store._assert_artifact_resolves(artifact_id)
    except store.UnresolvedArtifactError as err:
        raise MaterializationUnresolvedArtifactError(proposal['id'], str(err)) from err


def materialize_decomposition(message: dict, project: str, *, actor: str = 'refinement-agent',
                               origin: str = write_gate.Origins.DIRECT,
                               completion_key: Optional[str] = None) -> dict:
    """message: { parentWorkItemId, subtasks: [{ id, displayName,
    description, agent, "Blocked By": [] }, ...] } — same shape
    the decomposition contract already defines, with
    `parentJiraIssueKey` renamed to `parentWorkItemId` (a canonical id, not
    a Jira key).

    **One of REQ-09's five routing handlers.** It calls `write_gate.route`
    first, before it opens a transaction, and acts on the answer
    (canonical-delivery-state.md REQ-09, "The routing layer"):

      record  this module's own write loop, as in every version before
              v5.2 — the canonical subtasks, their Blocks links and the
              root subtasks' `ready`;
      push    every validation the recording path runs apart from the gate,
              computed on the proposal graph with nothing written, and then
              REQ-09's writer, which executes the decomposition against
              Jira: a Sub-task per proposal under the parent's issue, a
              Blocks link per edge, and the project's Shovel Ready status
              on the roots (REQ-11). Nothing is recorded here: each
              subtask reaches `core` through its own `jira:issue_created`
              webhook, which REQ-11's mirror materializes with the
              proposal id the writer's record holds;
      refuse  the Django admin, which reaches this handler through no
              caller today, refused for symmetry with the other four.

    `origin` is the caller's, per `write_gate.Origins`, and is what `route`
    answers on: `ADMIN_UI` is refused in a Jira-mode project, as it is for
    every other routed write. Every store call the recording path makes
    carries it on, so a local-mode decomposition records the origin its
    caller had rather than an assumed `DIRECT`.

    `completion_key` is the command envelope's `messageId`, which
    `command_consumer.handle_command` passes to every routed handler but
    `append_comment` (REQ-09, step 5). On `push` the root transitions skip
    any step the completion record already shows done; the creates and
    links are made idempotent by the decomposition record itself, which
    holds each proposal's Jira key and each Blocks pair."""
    parent_work_item_id = message and message.get('parentWorkItemId')
    subtasks = message and message.get('subtasks')

    if not isinstance(subtasks, list):
        raise ValueError('materializeDecomposition: message is missing a subtasks array')

    # REQ-09, step 1: `route` first, before any transaction or lock, and the
    # handler states no mode.
    verdict = write_gate.route(project, origin)
    if verdict == write_gate.REFUSE:
        write_gate.refuse(origin, 'materializing a decomposition')

    # Assignment integrity: reject the whole decomposition atomically on
    # any invalid owner — nothing is written for a rejected decomposition,
    # in either mode.
    validation = assignment.validate_decomposition(project, subtasks)
    if not validation['ok']:
        raise MaterializationValidationError(validation['rejected'], validation['permittedAgents'])

    try:
        if verdict == write_gate.PUSH:
            return _push_decomposition(project, parent_work_item_id, subtasks,
                                        completion_key=completion_key)
        return _record_decomposition(project, parent_work_item_id, subtasks, actor=actor, origin=origin)
    except MaterializationUnresolvedArtifactError as err:
        # v4.1 REQ-01 / carry-forward 3 — written AFTER the recording
        # path's atomic block has fully rolled back, and before any Jira
        # call on the pushing path, on the same pattern
        # store.transition_status uses for a refused release-candidate cut:
        # a comment written inside the same atomic block as a subsequent
        # raise would roll back along with it. Only reported when there is
        # a parent to report it on — handleCreateSubtask (gateway.js)
        # always supplies one; materialize_decomposition's own unit tests
        # that omit parentWorkItemId never reach this path.
        #
        # From v5.2 the COMMENT is not written here. Every rejection
        # `core` makes leaves exactly one comment, in one format, appended
        # by `command_consumer`'s handler after the permanent rejection and
        # keyed `<messageId>:rejection` (canonical-delivery-state.md
        # REQ-09, "Rejections, in every mode"); this module's own
        # unresolved-artifact comment is restated there, with the same two
        # ids in it. What stays here is the parent's status write, which no
        # other path can make: in local mode it records
        # `needs-clarification`, and in Jira mode `write_gate.route` sends
        # the same call to the writer, which sets the Blocked flag — the
        # flag Jira shows for all three of those statuses (REQ-09), and the
        # one Jira write a rejected decomposition makes besides its comment.
        #
        # The write below is best-effort: this report exists to help a
        # human find the parent, it must never replace `err` itself, which
        # is what command_consumer.py's dead-letter reason and
        # is_permanent_rejection need intact (the latter via err.code) to
        # dead-letter the command once instead of retrying it forever, so a
        # failure just logs and moves on.
        #
        # transition_status's own story-detail gate
        # (`_assert_story_fields_present`) normally can't refuse this call:
        # reaching materialize_decomposition at all means the parent
        # already sits at 'ready', which that same gate already required
        # the story fields for. If dispatch eligibility ever changes so a
        # parent can decompose before 'ready', this transition could start
        # being refused too — the try/except below is what keeps that
        # refusal from masking `err` if that happens.
        if parent_work_item_id:
            try:
                store.transition_status(
                    parent_work_item_id, 'needs-clarification', actor=actor, origin=origin,
                )
            except Exception:  # noqa: BLE001 - best-effort report; `err` below is what must propagate
                logger.warning(
                    'materialize_decomposition: could not transition parent %s to needs-clarification for '
                    'subtask %s', parent_work_item_id, err.subtask_id, exc_info=True,
                )
        raise


def _push_decomposition(project: str, parent_work_item_id, subtasks: list[dict], *,
                         completion_key: Optional[str] = None) -> dict:
    """REQ-09's `push` for a decomposition (REQ-11, "The writer executes a
    Jira-mode decomposition"). Every validation the recording path runs
    apart from the gate, with no row locked and nothing written, and then
    the writer.

    The no-progress rule and the artifact resolution both run here rather
    than inside the writer, because they are `materialize.py`'s rules and
    REQ-11 requires them "before any Jira call", with "nothing created on
    a rejection"."""
    parent = store.get_work_item(parent_work_item_id) if parent_work_item_id else None
    if parent is None:
        raise store.ValidationError(
            'materializeDecomposition: a Jira-mode decomposition needs its parent work item, '
            f'and {parent_work_item_id} does not exist — the Jira Sub-tasks are created under '
            "the parent's own issue"
        )

    already_keyed = jira_writer.keyed_proposal_ids(subtasks)
    plan = plan_decomposition(subtasks, resolved_ids=already_keyed)
    for proposal in plan['order']:
        _assert_proposal_artifacts_resolve(proposal)

    return jira_writer.execute_decomposition(
        project, parent, plan, completion_key=completion_key,
    )


def _record_decomposition(project: str, parent_work_item_id, subtasks: list[dict], *,
                           actor: str, origin: str = write_gate.Origins.DIRECT) -> dict:
    """REQ-09's `record` — local mode's write loop, unchanged in behaviour
    from every version before v5.2 except that the pass order now comes
    from `plan_decomposition` above rather than being recomputed inline."""
    # Idiomatic improvement over the Node original: materialize.js does not
    # wrap its multi-pass loop in a single transaction (each store.js call
    # commits on its own), so a batch that hits MaterializationNoProgressError
    # partway through can leave some subtasks durably created. Wrapping the
    # whole pass in one transaction.atomic() here makes a no-progress batch
    # all-or-nothing, which is strictly safer and does not change any tested
    # behavior (the "atomic rejection" test's invalid-agent case is already
    # rejected before any writes, by validate_decomposition above; the
    # "redelivery is idempotent" test's two calls are sequential, each fully
    # committing before the next begins).
    id_to_work_item_id: dict[str, str] = {}
    with transaction.atomic():
        # Already-materialized proposals (redelivery/retry recovery) — this
        # service's store IS the durable record, so "recovery" here is
        # simply "the id already exists".
        for proposal in subtasks:
            if store.get_work_item(proposal['id']):
                id_to_work_item_id[proposal['id']] = proposal['id']  # canonical id === proposal id.

        plan = plan_decomposition(subtasks, resolved_ids=set(id_to_work_item_id))

        for proposal in plan['order']:
            if proposal['id'] not in id_to_work_item_id:
                is_root = proposal['id'] in plan['rootIds']
                try:
                    store.create_work_item({
                        'id': proposal['id'],
                        'project': project,
                        'type': proposal.get('type') or 'task',
                        'displayName': proposal.get('displayName'),
                        'description': proposal.get('description'),
                        'assigneeAgentId': proposal.get('agent'),
                        'parentId': parent_work_item_id or None,
                        # The minimum status vocabulary distinguishes
                        # 'waiting-on-dependency' from 'proposed'; a
                        # dependent subtask starts life already known to be
                        # gated. Root subtasks stay 'proposed' here and are
                        # transitioned to 'ready' explicitly below.
                        'status': 'proposed' if is_root else 'waiting-on-dependency',
                        # v4.1 agent-artifact-automation.md REQ-01 —
                        # the same two optional references `create`
                        # already accepts (work-items.md REQ-01,
                        # REQ-02), forwarded unchanged from the
                        # create_subtask/materializeDecomposition
                        # subtask entry. `.get()` is None/omitted for
                        # a proposal that carries neither, which
                        # create_work_item already treats as "no
                        # link" — no behavior change for a
                        # decomposition that doesn't use them.
                        'specificationLink': proposal.get('specificationLink'),
                        'artifactLinks': proposal.get('artifactLinks'),
                    }, actor=actor, origin=origin)
                except store.UnresolvedArtifactError as err:
                    # carry-forward 6 — store.py's own exception
                    # names only the artifact id; this is the one
                    # place that still knows which subtask was being
                    # materialized, so the subtask id is added here
                    # instead of editing store.py.
                    raise MaterializationUnresolvedArtifactError(proposal['id'], str(err)) from err
                id_to_work_item_id[proposal['id']] = proposal['id']

        for blocker_id, dependent_id in plan['pairs']:
            store.create_link(blocker_id, dependent_id, 'blocks', actor=actor, origin=origin)

        # Only root (independent) subtasks enter 'ready' — dependents stay
        # 'waiting-on-dependency', gated by the blockers check in
        # store.py.
        for proposal_id in plan['rootIds']:
            store.transition_status(proposal_id, 'ready', actor=actor, origin=origin)

    return {'idToWorkItemId': id_to_work_item_id}
