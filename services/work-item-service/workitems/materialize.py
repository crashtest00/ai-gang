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

from typing import Any, Optional

from django.db import transaction

from . import assignment, store, write_gate


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


def materialize_decomposition(message: dict, project: str, *, actor: str = 'refinement-agent') -> dict:
    """message: { parentWorkItemId, subtasks: [{ id, displayName,
    description, agent, "Blocked By": [] }, ...] } — same shape
    the decomposition contract already defines, with
    `parentJiraIssueKey` renamed to `parentWorkItemId` (a canonical id, not
    a Jira key)."""
    parent_work_item_id = message and message.get('parentWorkItemId')
    subtasks = message and message.get('subtasks')

    if not isinstance(subtasks, list):
        raise ValueError('materializeDecomposition: message is missing a subtasks array')

    # Assignment integrity: reject the whole decomposition atomically on
    # any invalid owner — nothing is written for a rejected decomposition.
    validation = assignment.validate_decomposition(project, subtasks)
    if not validation['ok']:
        raise MaterializationValidationError(validation['rejected'], validation['permittedAgents'])

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
    try:
        with transaction.atomic():
            # Already-materialized proposals (redelivery/retry recovery) — this
            # service's store IS the durable record, so "recovery" here is
            # simply "the id already exists".
            for proposal in subtasks:
                existing = store.get_work_item(proposal['id'])
                if existing:
                    id_to_work_item_id[proposal['id']] = proposal['id']  # canonical id === proposal id.

            linked_pairs: set[str] = set()

            root_proposal_ids = {s['id'] for s in subtasks if not s.get('Blocked By')}

            pending = list(subtasks)
            progressed = True

            # Order-independent multi-pass materialization: a subtask created
            # earlier in THIS pass counts as existing for a later one in the
            # same pass.
            while pending and progressed:
                progressed = False

                for i in range(len(pending) - 1, -1, -1):
                    proposal = pending[i]
                    blocked_by = proposal.get('Blocked By') or []

                    all_resolved = all(blocker_id in id_to_work_item_id for blocker_id in blocked_by)
                    if not all_resolved:
                        continue

                    if proposal['id'] not in id_to_work_item_id:
                        is_root = len(blocked_by) == 0
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
                            }, actor=actor, origin=write_gate.Origins.DIRECT)
                        except store.UnresolvedArtifactError as err:
                            # carry-forward 6 — store.py's own exception
                            # names only the artifact id; this is the one
                            # place that still knows which subtask was being
                            # materialized, so the subtask id is added here
                            # instead of editing store.py.
                            raise MaterializationUnresolvedArtifactError(proposal['id'], str(err)) from err
                        id_to_work_item_id[proposal['id']] = proposal['id']

                    for blocker_id in blocked_by:
                        pair_key = f'{blocker_id}->{proposal["id"]}'
                        if pair_key not in linked_pairs:
                            store.create_link(blocker_id, proposal['id'], 'blocks', actor=actor, origin=write_gate.Origins.DIRECT)
                            linked_pairs.add(pair_key)

                    del pending[i]
                    progressed = True

            if pending:
                unresolved = [
                    {'id': p['id'], 'displayName': p.get('displayName'), 'blockedBy': p.get('Blocked By') or []}
                    for p in pending
                ]
                raise MaterializationNoProgressError(unresolved)

            # Only root (independent) subtasks enter 'ready' — dependents stay
            # 'waiting-on-dependency', gated by the blockers check in
            # store.py.
            for proposal_id in root_proposal_ids:
                store.transition_status(proposal_id, 'ready', actor=actor, origin=write_gate.Origins.DIRECT)
    except MaterializationUnresolvedArtifactError as err:
        # v4.1 REQ-01 / carry-forward 3 — written AFTER the atomic block
        # above has fully rolled back (transaction.atomic's context manager
        # rolls back on exception exit), on the same pattern
        # store.transition_status:395-418 uses for a refused
        # release-candidate cut: a comment written inside the same atomic
        # block as the raise would roll back along with it. Only reported
        # when there is a parent to report it on — handleCreateSubtask
        # (gateway.js) always supplies one; materialize_decomposition's own
        # unit tests that omit parentWorkItemId never reach this path.
        if parent_work_item_id:
            store.append_comment(
                parent_work_item_id, actor,
                f'[system] Subtask {err.subtask_id} could not be created: {err.artifact_error}. '
                'No subtask was created for it; fix the reference and retry the decomposition.',
            )
            store.transition_status(
                parent_work_item_id, 'needs-clarification', actor=actor, origin=write_gate.Origins.DIRECT,
            )
        raise

    return {'idToWorkItemId': id_to_work_item_id}
