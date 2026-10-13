"""
The canonical work-item schema as Django models. Table names, column
names, and constraints mirror the Node implementation's migrations/001-010
exactly (same `db_table`/`db_column` throughout) so the physical schema
this service stands up is unchanged from the reference implementation —
only the framework/ORM producing it is different (the datastore
engine and its exact table shape were never a cross-service contract, but
matching it anyway keeps this port auditable column-for-column against the
schema and the Node migrations it replaces).
"""

from __future__ import annotations

import uuid

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.db.models.fields.json import KeyTextTransform
from django.utils import timezone


class WorkItem(models.Model):
    """Canonical identity, status, assignment,
    and write-scope. `id` is NEVER minted by this service — it is the
    Refinement Agent's own proposal UUID (or any other AI-Gang-issued id),
    issued and owned by AI Gang."""

    # editable (with a generated default) rather than editable=False: every
    # non-admin caller (Streams commands, the Jira webhook consumer) still
    # goes through store.create_work_item, which requires the caller to
    # supply an id explicitly — never minted by this service on a
    # caller's behalf — and ignores this default entirely. The default only
    # matters for the one interface where a human, not an upstream agent,
    # is the id's origin: the Django admin "Add work item" form —
    # letting it pre-fill a fresh UUID there is a human-admin UX convenience,
    # not a relaxation of that rule for any agent-facing path.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    external_key = models.TextField(unique=True, null=True, blank=True)
    type = models.TextField()
    display_name = models.TextField()
    description = models.TextField(null=True, blank=True)
    # No literal CHECK constraint (status is validated against per-project
    # status configuration at write time instead — see status_vocabulary.py
    # / ProjectStatusConfig).
    status = models.TextField()
    assignee_agent_id = models.TextField(null=True, blank=True, db_column='assignee_agent_id')
    priority = models.SmallIntegerField(default=0)
    writes_files = ArrayField(models.TextField(), null=True, blank=True)
    writes_services = ArrayField(models.TextField(), null=True, blank=True)
    parent = models.ForeignKey(
        'self', null=True, blank=True, on_delete=models.DO_NOTHING,
        db_column='parent_id', related_name='children',
    )
    # Not in the canonical schema's own column list — a necessary,
    # non-speculative addition documented the same way the Node migration
    # documented it: every other table is scoped to a project only
    # indirectly (via work_item), and mode/status-vocabulary/dependency
    # validation are all per-project, so there is no way to know which
    # project's configuration governs a work item without this column.
    project = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'work_item'
        indexes = [
            models.Index(fields=['project', 'status'], name='idx_work_item_project_status'),
            models.Index(fields=['parent'], name='idx_work_item_parent',
                         condition=models.Q(parent__isnull=False)),
            models.Index(fields=['assignee_agent_id'], name='idx_work_item_assignee',
                         condition=models.Q(assignee_agent_id__isnull=False)),
        ]
        constraints = [
            models.CheckConstraint(check=models.Q(priority__in=[0, 1, 2, 3, 4]), name='ck_work_item_priority'),
        ]

    def __str__(self) -> str:
        return f'{self.display_name} ({self.id})'

    def save(self, *args, **kwargs):
        # A blank External key must be stored as NULL, never as ''. The
        # unique constraint on this column treats '' as a value like any
        # other, so two work items both left without an external key would
        # collide on the second one — NULL is the only value a unique
        # constraint never matches against another row, including another
        # NULL. Normalized here (not only on the admin form, see
        # WorkItemAdminForm.clean_external_key) so every writer that builds
        # a WorkItem directly, not through that form, gets the same
        # guarantee.
        if self.external_key == '':
            self.external_key = None
        super().save(*args, **kwargs)


class WorkItemStoryDetail(models.Model):
    """Story schema field contract. 1:1 optional child table
    (class-table inheritance): a row exists only for `type = 'story'`."""

    work_item = models.OneToOneField(
        WorkItem, primary_key=True, on_delete=models.CASCADE,
        db_column='work_item_id', related_name='story_detail',
    )
    behavior = models.TextField()
    acceptance_criteria = models.TextField()
    constraints = models.TextField()
    edge_cases = models.TextField()
    out_of_scope = models.TextField()
    value_hypothesis = models.TextField(null=True, blank=True)
    test_measurement = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'work_item_story_detail'

    def __str__(self) -> str:
        return f'story detail for {self.work_item_id}'


class WorkItemReleaseDetail(models.Model):
    """Release schema field
    contract. 1:1 optional child table (class-table inheritance): a row
    exists only for `type = 'release'`, same pattern as
    WorkItemStoryDetail. Target Project is NOT a column here — it maps
    onto the work item's own `project`, the same field every
    other type already carries. `candidate_sha`/`build_identifier`/
    `preview_url` are automation-populated and empty until candidate cut;
    write-once-per-candidate, not appended."""

    work_item = models.OneToOneField(
        WorkItem, primary_key=True, on_delete=models.CASCADE,
        db_column='work_item_id', related_name='release_detail',
    )
    release_notes = models.TextField(null=True, blank=True)
    candidate_sha = models.TextField(null=True, blank=True)
    build_identifier = models.TextField(null=True, blank=True)
    preview_url = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'work_item_release_detail'

    def __str__(self) -> str:
        return f'release detail for {self.work_item_id}'


class WorkItemLink(models.Model):
    """Dependency graph. Directional typed edge."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    from_work_item = models.ForeignKey(
        WorkItem, on_delete=models.DO_NOTHING, db_column='from_work_item_id', related_name='links_from',
    )
    to_work_item = models.ForeignKey(
        WorkItem, on_delete=models.DO_NOTHING, db_column='to_work_item_id', related_name='links_to',
    )
    # 'blocks' / 'is_blocked_by' at minimum; open vocabulary, not a fixed enum.
    link_type = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'work_item_link'
        indexes = [
            models.Index(fields=['from_work_item', 'link_type'], name='idx_work_item_link_from'),
            models.Index(fields=['to_work_item', 'link_type'], name='idx_work_item_link_to'),
        ]
        constraints = [
            # Not in the canonical schema's column list, but createLink's
            # idempotent-redelivery requirement must be able to
            # avoid creating a duplicate identical link on retry/redelivery.
            models.UniqueConstraint(
                fields=['from_work_item', 'to_work_item', 'link_type'],
                name='idx_work_item_link_unique',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.from_work_item_id} -{self.link_type}-> {self.to_work_item_id}'


class WorkItemHistory(models.Model):
    """Append-only history. Rows are append-only: store.py
    never issues an UPDATE/DELETE against this table, and as of migration
    0002 that's also enforced at the database level — a trigger rejects
    any UPDATE/DELETE against work_item_history regardless of caller,
    closing the gap the Node implementation carried forward."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    work_item = models.ForeignKey(
        WorkItem, on_delete=models.DO_NOTHING, db_column='work_item_id', related_name='history',
    )
    field = models.TextField()
    old_value = models.TextField(null=True, blank=True)
    new_value = models.TextField(null=True, blank=True)
    actor = models.TextField()
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'work_item_history'
        indexes = [
            models.Index(fields=['work_item', 'occurred_at'], name='idx_work_item_history_item'),
        ]

    def __str__(self) -> str:
        return f'{self.work_item_id}: {self.field} {self.old_value!r} -> {self.new_value!r}'


class WorkItemArtifact(models.Model):
    """Artifact association."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    work_item = models.ForeignKey(
        WorkItem, on_delete=models.DO_NOTHING, db_column='work_item_id', related_name='artifacts',
    )
    # 'commit', 'pull_request', 'ci_build', 'deployment' at minimum; also
    # reused as the per-item completion-marker "stepKey" (store.py).
    artifact_type = models.TextField()
    reference = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'work_item_artifact'
        indexes = [
            models.Index(fields=['work_item'], name='idx_work_item_artifact_item'),
            # canonical-delivery-state.md REQ-02 — the reverse lookup. V4's
            # only index on this table is by `work_item`, which answers
            # "what did this item produce"; the resolver asks the opposite
            # question, "which item produced this commit or pull request",
            # and Jenkins knows only the reference. Without this index that
            # query is a sequential scan of every association row AND every
            # REQ-19 completion marker sharing the column.
            models.Index(fields=['artifact_type', 'reference'], name='idx_work_item_artifact_ref'),
        ]

    def __str__(self) -> str:
        return f'{self.artifact_type}:{self.reference}'


class WorkItemSpecificationLink(models.Model):
    """work-items.md REQ-01 — a work item's link BACK to the artifact and
    requirement id it was created to satisfy. The opposite direction from
    `WorkItemArtifact` (REQ-06, above), which records what a work item
    PRODUCED; deliberately a separate table rather than reusing that one
    (work-items.md §4, "Why not reuse REQ-06's table"). At most one per
    work item (work-items.md §4, "Cardinality"), so this is a 1:1 optional
    child table keyed by the parent's own primary key — the same shape as
    `WorkItemStoryDetail`/`WorkItemReleaseDetail`, not new in kind, just new
    in this version. Not part of the original Node port (module docstring);
    added by V4.

    `artifact` is a real foreign key into the ingress app's own table
    (`artifacts.Artifact`) rather than a bare UUID column — this is the
    schema-level "a referenced artifact must resolve" guarantee (REQ-04):
    the database itself refuses a row naming an artifact id that does not
    exist. `on_delete=PROTECT`, not CASCADE: V4 never deletes an artifact
    record, but if that ever changed, silently dropping a work item's
    specification link because someone deleted the artifact it points to
    would be the wrong failure mode — refusing the delete is.
    """

    work_item = models.OneToOneField(
        WorkItem, primary_key=True, on_delete=models.CASCADE,
        db_column='work_item_id', related_name='specification_link',
    )
    artifact = models.ForeignKey(
        # No reverse accessor onto Artifact (related_name='+'): the
        # artifacts app owns Artifact's own field set (see its own
        # test_no_record_field_is_derived_from_content, which enumerates
        # it exactly) and nothing in this app ever queries FROM an
        # artifact backward — every lookup here starts from the
        # work-item side.
        'artifacts.Artifact', on_delete=models.PROTECT, db_column='artifact_id', related_name='+',
    )
    # Opaque, unvalidated string (e.g. "REQ-18") — work-items.md §4, "The
    # requirement id is a string by design".
    requirement_id = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'work_item_specification_link'
        indexes = [
            # Backs the REQ-06 forward query: enumerate every work item
            # recording a given (artifact id, requirement id) pair.
            models.Index(fields=['artifact', 'requirement_id'], name='idx_wi_spec_link_artifact_req'),
        ]

    def __str__(self) -> str:
        return f'{self.work_item_id} -> {self.artifact_id} ({self.requirement_id})'


class WorkItemArtifactLink(models.Model):
    """work-items.md REQ-02 — the ordered list of artifacts that INFORM a
    work item (what it depends on), as distinct from `WorkItemArtifact`
    (what it produced). A small child table with a `position` column,
    per work-items.md §4's open storage choice — the natural fit for
    "returns all three by its canonical id, in the order recorded"
    (REQ-02's acceptance). Not part of the original Node port; added by V4.

    Same schema-level REQ-04 guarantee as `WorkItemSpecificationLink`: a
    real foreign key to `artifacts.Artifact`, `on_delete=PROTECT`.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    work_item = models.ForeignKey(
        WorkItem, on_delete=models.DO_NOTHING, db_column='work_item_id', related_name='artifact_links',
    )
    artifact = models.ForeignKey(
        # See WorkItemSpecificationLink.artifact's comment on related_name='+'.
        'artifacts.Artifact', on_delete=models.PROTECT, db_column='artifact_id', related_name='+',
    )
    # 0-based, assigned as len(existing links) at insertion time — "the
    # order recorded" (REQ-02), not a value callers supply.
    position = models.PositiveIntegerField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'work_item_artifact_link'
        ordering = ['position']
        indexes = [
            models.Index(fields=['work_item', 'position'], name='idx_wi_artifact_link_item'),
        ]
        constraints = [
            # The same artifact is not linked to the same work item twice;
            # also the idempotent-redelivery guard `add_artifact_link`
            # relies on, mirroring `WorkItemLink`'s own unique-edge
            # constraint (store.create_link's precedent).
            models.UniqueConstraint(fields=['work_item', 'artifact'], name='idx_wi_artifact_link_unique'),
        ]

    def __str__(self) -> str:
        return f'{self.work_item_id} -> artifact {self.artifact_id} (#{self.position})'


class WorkItemComment(models.Model):
    """Comment-thread communication contract.
    `source_message_id` is the idempotency key for redelivered
    comment-producing messages."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    work_item = models.ForeignKey(
        WorkItem, on_delete=models.DO_NOTHING, db_column='work_item_id', related_name='comments',
    )
    author = models.TextField()
    body = models.TextField()
    reference_file = models.TextField(null=True, blank=True)
    reference_function = models.TextField(null=True, blank=True)
    source_message_id = models.TextField(unique=True, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'work_item_comment'
        indexes = [
            models.Index(fields=['work_item', 'created_at'], name='idx_work_item_comment_item'),
        ]

    def __str__(self) -> str:
        return f'{self.author}: {self.body[:40]}'


class ProjectConfig(models.Model):
    """Explicit, reversible, per-project
    mode selection. local mode is the unconditional default (a project
    with no row here is treated as local mode — see project_config.py); a
    row is written the first time a project is touched or its mode
    changes."""

    LOCAL = 'local'
    JIRA = 'jira'
    MODE_CHOICES = [(LOCAL, 'Local'), (JIRA, 'Jira')]

    project = models.TextField(primary_key=True)
    mode = models.TextField(choices=MODE_CHOICES, default=LOCAL)
    jira_project_key = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'project_config'
        constraints = [
            models.CheckConstraint(check=models.Q(mode__in=['local', 'jira']), name='ck_project_config_mode'),
        ]

    def __str__(self) -> str:
        return f'{self.project} ({self.mode})'


class ProjectStatusConfig(models.Model):
    """A project's *additional* declared
    custom statuses and their required baseline-status refinement and Jira
    status-name mapping. The minimum ten canonical statuses are fixed and
    never stored here (status_vocabulary.py hardcodes them)."""

    project = models.TextField()
    status = models.TextField()
    baseline_status = models.TextField()
    jira_status_name = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'project_status_config'
        constraints = [
            models.UniqueConstraint(fields=['project', 'status'], name='pk_project_status_config'),
        ]

    def __str__(self) -> str:
        return f'{self.project}:{self.status} -> {self.baseline_status}'


class OutboxEvent(models.Model):
    """Transactional outbox. Written
    in the SAME transaction as the datastore write it describes; relayed
    to Redis Streams by a separate process (workitems/management/commands/
    relay.py) that marks each row published_at once XADD has durably
    succeeded. A row with published_at IS NULL is "not yet confirmed
    published" — exactly what the relay polls for and what a killed-mid-run
    relay leaves behind for the next relay run to pick back up.

    Two kinds of row share the table (durable-artifact-event-history.md
    REQ-02). A work-item row names no ``stream`` and stores no
    ``envelope``: the relay derives both from ``project``, as it always
    has. An announcement row names the stream it is destined for and
    stores the envelope its publisher built, which the relay publishes
    verbatim; its ``project``, ``event_type`` and ``payload`` are copied
    from that envelope by ``store.write_outbox_event`` so the admin list
    stays meaningful. The check constraint ``outbox_stream_iff_envelope``
    refuses a row with one and not the other.

    Nothing deletes a row: the table is the announcement history
    (REQ-06), and the admin refuses deletes (``OutboxEventAdmin``)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.TextField()
    event_type = models.TextField()
    work_item_id = models.UUIDField(null=True, blank=True)
    payload = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)
    published_at = models.DateTimeField(null=True, blank=True)
    stream_entry_id = models.TextField(null=True, blank=True)
    stream = models.TextField(null=True, blank=True)
    envelope = models.JSONField(null=True, blank=True)

    class Meta:
        db_table = 'outbox_event'
        indexes = [
            models.Index(fields=['created_at'], name='idx_outbox_unpublished',
                         condition=models.Q(published_at__isnull=True)),
        ]
        constraints = [
            models.CheckConstraint(
                check=(models.Q(stream__isnull=True, envelope__isnull=True)
                       | models.Q(stream__isnull=False, envelope__isnull=False)),
                name='outbox_stream_iff_envelope',
            ),
            # One answer per librarian request (durable-artifact-event-
            # history.md REQ-04): at most one row destined for the
            # librarian's response stream per request, keyed on the stored
            # envelope's correlationId (the request's messageId). The
            # librarian's lookup under its advisory lock is what keeps a
            # second insert from being attempted; this is its backstop.
            models.UniqueConstraint(
                KeyTextTransform('correlationId', 'envelope'),
                condition=models.Q(stream='aigang:librarian:responses'),
                name='uniq_outbox_librarian_answer',
            ),
        ]

    def __str__(self) -> str:
        return f'{self.event_type} ({self.id})'


class AccessLog(models.Model):
    """Read access MUST be
    recorded in the service's own API/access logs, not in Streams. A
    dedicated table rather than an application log file: queryable,
    survives container recreation."""

    id = models.BigAutoField(primary_key=True)
    occurred_at = models.DateTimeField(default=timezone.now)
    operation = models.TextField()
    project = models.TextField(null=True, blank=True)
    work_item_id = models.UUIDField(null=True, blank=True)
    actor = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'access_log'
        indexes = [
            models.Index(fields=['occurred_at'], name='idx_access_log_occurred'),
        ]

    def __str__(self) -> str:
        return f'{self.operation} @ {self.occurred_at}'


class WebhookFailure(models.Model):
    """A durable, operator-visible
    failure record for a Jira-originated webhook event that either
    exhausted Streams' own retry/dead-letter handling, or was rejected by
    validation against internal canonical rules."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.TextField()
    work_item_id = models.UUIDField(null=True, blank=True)
    external_key = models.TextField(null=True, blank=True)
    reason = models.TextField()
    payload = models.JSONField(null=True, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'webhook_failure'
        indexes = [
            models.Index(fields=['work_item_id'], name='idx_webhook_failure_item'),
            models.Index(fields=['occurred_at'], name='idx_webhook_failure_occurred'),
        ]

    def __str__(self) -> str:
        return f'{self.reason} ({self.external_key or self.work_item_id})'


class BetaDeploymentRecord(models.Model):
    """canonical-delivery-state.md REQ-01 — `recordBetaDeployment`'s own
    deduplication record, written only once its handler's last step has
    completed. The gateway deduplicates by `messageId` alone
    (`gateway.js`'s `idempotency.once`), so a second `beta_deployed`
    message for the same deployment under a fresh `messageId` would
    otherwise re-post evidence and re-transition. The deployed SHA and the
    build identifier together are what identify one deployment, so that
    pair is the unique key — enforced by the database, not by a read-then-
    write check (`uq_beta_deployment_sha_build` below).

    A redelivery of the *same* message runs its own incomplete steps
    instead (REQ-09 "Redelivery", `JiraWriteCompletion`): this table says
    "this deployment is fully applied", the completion records say "this
    step of this message is done"."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.TextField()
    deployed_sha = models.TextField()
    build_identifier = models.TextField()
    build_url = models.TextField(null=True, blank=True)
    beta_url = models.TextField(null=True, blank=True)
    # The gateway envelope's own messageId, which the relay carries as the
    # command's sourceMessageId (REQ-01) — recorded for an operator tracing
    # one deployment back to the Jenkins build that published it.
    source_message_id = models.TextField(null=True, blank=True)
    # Recorded for an operator tracing a deployment; no code reads it.
    work_item_ids = models.JSONField(default=list)
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'beta_deployment_record'
        constraints = [
            models.UniqueConstraint(fields=['deployed_sha', 'build_identifier'],
                                    name='uq_beta_deployment_sha_build'),
        ]
        indexes = [
            models.Index(fields=['recorded_at'], name='idx_beta_deployment_recorded'),
        ]

    def __str__(self) -> str:
        return f'{self.project} {self.deployed_sha} ({self.build_identifier})'


class JiraWriteCompletion(models.Model):
    """canonical-delivery-state.md REQ-09 "Redelivery" — the outbound
    writer's per-step completion record. Keyed on the command or event's
    `messageId` (what the routing layer calls the `completion_key`), the
    work item, and the step; `completed_at` is set **after** the Jira call
    succeeds, never before, because a claim taken first turns a transient
    Jira failure into a lost write.

    A row with `completed_at IS NULL` is *registered*, not complete: story
    intake's comment steps are the one case registered ahead of their work,
    so `_handle_story_created`'s early return on redelivery knows which
    comment steps that `messageId` owes. A registered step is not skipped.

    Kept at least as long as dead letters (30 days, `streams.py`'s
    `trim_dead_letters` default), so a replayed dead letter still finds its
    own completion record."""

    RETENTION_DAYS = 30

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    completion_key = models.TextField()
    work_item_id = models.UUIDField()
    step = models.TextField()
    registered_at = models.DateTimeField(default=timezone.now)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'jira_write_completion'
        constraints = [
            models.UniqueConstraint(fields=['completion_key', 'work_item_id', 'step'],
                                    name='uq_jira_write_completion_step'),
        ]
        indexes = [
            models.Index(fields=['registered_at'], name='idx_jira_write_completion_age'),
        ]

    def __str__(self) -> str:
        state = 'complete' if self.completed_at else 'registered'
        return f'{self.completion_key}/{self.work_item_id}/{self.step} ({state})'


class JiraDecompositionProposal(models.Model):
    """canonical-delivery-state.md REQ-11 — the proposal-to-key record a
    Jira-mode decomposition writes, one row per proposal: "proposal id,
    project, parent work item id, the proposal's specification and artifact
    links, Jira key, and each Blocks pair created".

    It is what makes a Jira-mode decomposition idempotent and what lets the
    Sub-task mirror give a mirrored subtask its PROPOSAL id rather than a
    fresh one (local mode's behaviour, `materialize.py`'s canonical
    id === proposal id). The writer creates each Jira Sub-task and records
    its key here immediately, and records each Blocks pair immediately
    after linking it, so a redelivery skips every recorded proposal and
    link.

    `blocks_pairs` holds the pairs created with THIS proposal as the
    DEPENDENT — the direction both readers need:

      - the mirror derives a Backlog subtask's status from its inward
        blockers, which for a subtask the record names are "every Blocks
        pair the record holds with its key as the dependent", a pair whose
        blocker has no row yet counting as not `done`;
      - `store._unblock_dependents` finds a blocker's dependents by
        `blockerProposalId`, which equals the dependent-side row's
        blocker's canonical work item id, so it needs no Jira key and reads
        no mode (REQ-11: it "applies the same rule in every mode").

    Each entry is `{'blockerProposalId': str, 'blockerKey': str|None,
    'dependentKey': str|None}`. The keys are recorded for an operator
    reading the row; the proposal ids are what the queries use, since they
    exist before any Jira call does.

    A project that was never in Jira mode has no row here, because only a
    Jira-mode decomposition writes one — which is why `_unblock_dependents`
    can consult it unconditionally. A project returned to local mode by
    `disconnect_jira` keeps its rows, and they still count (REQ-11; §4)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.TextField()
    parent_work_item_id = models.UUIDField(null=True, blank=True)
    proposal_id = models.UUIDField()
    jira_key = models.TextField(null=True, blank=True)
    specification_link = models.JSONField(null=True, blank=True)
    artifact_links = models.JSONField(default=list)
    blocks_pairs = models.JSONField(default=list)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = 'jira_decomposition_proposal'
        constraints = [
            models.UniqueConstraint(fields=['proposal_id'], name='uq_jira_decomposition_proposal'),
        ]
        indexes = [
            models.Index(fields=['jira_key'], name='idx_jira_decomposition_key'),
            models.Index(fields=['project'], name='idx_jira_decomposition_project'),
        ]

    def __str__(self) -> str:
        return f'{self.proposal_id} -> {self.jira_key or "(not yet created)"}'


# ---------------------------------------------------------------------------
# The durable A2A message record (durable-a2a-message-record.md)
# ---------------------------------------------------------------------------
#
# Written by two independent writers and read by one function:
#
#   - `a2a_archive.py`'s `core-a2a-archive` consumer group, on each project's
#     gateway stream and each agent channel stream, writes the Task record,
#     the messages, their artifacts and any content conflict (REQ-01, REQ-02,
#     REQ-03, REQ-05), straight from the raw stream entries;
#   - the command consumer's `recordA2AVerdict` writes ScrumMaster's verdict
#     and outcome rows (REQ-04).
#
# No table here stores a Task's current `state`: `store.a2a_task_record`
# derives it at every read from the messages and verdict rows (REQ-01), so the
# two writers never have to agree on a value they both update.
#
# Every table carries `change_xid`, the 64-bit id of the transaction that
# wrote the row, set by the database itself (`a2a_set_change_xid`, migration
# 0009). It is REQ-11's changed-since cursor: a row is "changed since" a
# cursor when its transaction id is at or above the oldest transaction still
# running when the cursor was taken, so a row whose transaction committed after
# a reader took its cursor is never skipped, however late it commits.


class A2ATask(models.Model):
    """REQ-01 — one durable record per A2A Task, created from the Task's
    first archived message, from whichever stream it was archived. Carries no
    current `state` (derived at read) and no Task `metadata` (never
    transmitted on a stream, §6 question 1).

    ``work_item_id`` is REQ-09's association, resolved from the Task's own
    ``id`` — the canonical work item id in both tracker modes — and never from
    ``external_key``. When it does not resolve it stays null and
    ``association_error`` records why, so the Task is stored, not dropped."""

    id = models.TextField(primary_key=True)
    context_id = models.TextField(null=True, blank=True)
    project = models.TextField()
    work_item_id = models.UUIDField(null=True, blank=True)
    association_error = models.TextField(null=True, blank=True)
    created_from_message_id = models.TextField()
    created_at = models.DateTimeField(default=timezone.now)
    change_xid = models.BigIntegerField(null=True, blank=True, editable=False)

    class Meta:
        db_table = 'a2a_task'
        indexes = [
            models.Index(fields=['change_xid'], name='idx_a2a_task_change'),
        ]

    def __str__(self) -> str:
        return f'A2A task {self.id}'


class A2AMessage(models.Model):
    """REQ-02 — every A2A message the archive group stores, append-only
    (`a2a_message_append_only`, migration 0009), keyed by the A2A message's
    own ``messageId`` (REQ-05: ``unique``, so a redelivery or a dead-letter
    replay never stores it twice).

    ``envelope`` is the stream entry's ``data`` field, parsed; ``message`` is
    the A2A message as it appeared (``payload.message`` on a gateway
    submission, the whole ``payload`` on a dispatch) and the columns beside
    it are read out of it, each null where the message does not carry it.
    ``state`` is a submission's ``payload.state`` when it is a non-empty
    string, null otherwise and for every dispatch.

    Ordered by the entry that carried it: ``entry_ms``, then ``stream``, then
    ``entry_seq`` (REQ-02), never by ``timestamp`` or ``recorded_at``."""

    DISPATCH = 'dispatch'
    SUBMISSION = 'submission'
    DIRECTION_CHOICES = [(DISPATCH, 'Dispatch'), (SUBMISSION, 'Submission')]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message_id = models.TextField(unique=True)
    task = models.ForeignKey(A2ATask, on_delete=models.DO_NOTHING, db_column='task_id', related_name='messages')
    direction = models.TextField(choices=DIRECTION_CHOICES)
    stream = models.TextField()
    entry_id = models.TextField()
    entry_ms = models.BigIntegerField()
    entry_seq = models.BigIntegerField()
    envelope = models.JSONField()
    message = models.JSONField(null=True, blank=True)
    role = models.TextField(null=True, blank=True)
    parts = models.JSONField(null=True, blank=True)
    reference_message_id = models.TextField(null=True, blank=True)
    metadata = models.JSONField(null=True, blank=True)
    timestamp = models.TextField(null=True, blank=True)
    state = models.TextField(null=True, blank=True)
    recorded_at = models.DateTimeField(default=timezone.now)
    change_xid = models.BigIntegerField(null=True, blank=True, editable=False)

    class Meta:
        db_table = 'a2a_message'
        indexes = [
            models.Index(fields=['task', 'entry_ms', 'stream', 'entry_seq'], name='idx_a2a_message_order'),
            models.Index(fields=['task', 'change_xid'], name='idx_a2a_message_change'),
        ]
        constraints = [
            models.CheckConstraint(check=models.Q(direction__in=['dispatch', 'submission']),
                                   name='ck_a2a_message_direction'),
        ]

    def __str__(self) -> str:
        return f'A2A message {self.message_id} ({self.direction})'


class A2AArtifact(models.Model):
    """REQ-03 — one row for each time an archived submission carried an
    artifact with an ``artifactId``, tied to that message. Not unique on
    ``artifact_id``: a repeated id, within one submission or across a Task's
    submissions, is stored once per carrying (``position`` is its index in
    ``payload.artifacts``). An artifact with no ``artifactId`` has no row; it
    stays in its message's stored envelope."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(A2ATask, on_delete=models.DO_NOTHING, db_column='task_id', related_name='artifacts')
    message = models.ForeignKey(A2AMessage, on_delete=models.DO_NOTHING, db_column='message_row_id',
                                related_name='artifacts')
    position = models.PositiveIntegerField()
    artifact_id = models.TextField()
    name = models.TextField(null=True, blank=True)
    parts = models.JSONField(null=True, blank=True)
    reference_artifact_id = models.TextField(null=True, blank=True)
    metadata = models.JSONField(null=True, blank=True)
    artifact = models.JSONField()
    recorded_at = models.DateTimeField(default=timezone.now)
    change_xid = models.BigIntegerField(null=True, blank=True, editable=False)

    class Meta:
        db_table = 'a2a_artifact'
        indexes = [
            models.Index(fields=['task', 'change_xid'], name='idx_a2a_artifact_change'),
        ]
        constraints = [
            models.UniqueConstraint(fields=['message', 'position'], name='uq_a2a_artifact_position'),
        ]

    def __str__(self) -> str:
        return f'A2A artifact {self.artifact_id}'


class A2AConflict(models.Model):
    """REQ-05 — a stream entry whose A2A ``messageId`` was already stored with
    different content (message, state or artifacts). The stored message is
    left as it is; the conflicting entry is recorded here, once per stream
    entry (``uq_a2a_conflict_entry``), under the Task of the stored
    message."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task = models.ForeignKey(A2ATask, on_delete=models.DO_NOTHING, db_column='task_id', related_name='conflicts')
    message_id = models.TextField()
    stream = models.TextField()
    entry_id = models.TextField()
    envelope = models.JSONField()
    recorded_at = models.DateTimeField(default=timezone.now)
    change_xid = models.BigIntegerField(null=True, blank=True, editable=False)

    class Meta:
        db_table = 'a2a_conflict'
        indexes = [
            models.Index(fields=['task', 'change_xid'], name='idx_a2a_conflict_change'),
        ]
        constraints = [
            models.UniqueConstraint(fields=['stream', 'entry_id'], name='uq_a2a_conflict_entry'),
        ]

    def __str__(self) -> str:
        return f'A2A conflict {self.message_id} at {self.stream} {self.entry_id}'


class A2AVerdict(models.Model):
    """REQ-04 — one append-only row (`a2a_verdict_append_only`, migration
    0009) per ``recordA2AVerdict`` command ScrumMaster published: either a
    verdict (``applied``, or ``refused`` with a ``reason``) or an outcome
    update, never both and never neither (``ck_a2a_verdict_one_of``).

    Keyed by the Task ``id`` and the A2A ``messageId``, with no foreign key to
    either: a verdict can arrive before the archive group has stored its
    message or its Task, since the two travel on different streams, and the
    read joins them. ``command_message_id`` is the command envelope's own
    ``messageId``, unique, so a redelivered command stores nothing new."""

    APPLIED = 'applied'
    REFUSED = 'refused'
    VERDICTS = (APPLIED, REFUSED)
    REASONS = ('invalid', 'unknown_task', 'wrong_project', 'wrong_role', 'task_finished', 'dead_lettered')
    OUTCOMES = ('succeeded', 'failed', 'superseded')

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    task_id = models.TextField()
    message_id = models.TextField()
    verdict = models.TextField(null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    finished_state = models.TextField(null=True, blank=True)
    outcome = models.TextField(null=True, blank=True)
    source_message_id = models.TextField(null=True, blank=True)
    actor = models.TextField(null=True, blank=True)
    command = models.JSONField()
    command_message_id = models.TextField(unique=True)
    command_created_at = models.DateTimeField()
    project = models.TextField()
    recorded_at = models.DateTimeField(default=timezone.now)
    change_xid = models.BigIntegerField(null=True, blank=True, editable=False)

    class Meta:
        db_table = 'a2a_verdict'
        indexes = [
            models.Index(fields=['task_id', 'message_id'], name='idx_a2a_verdict_message'),
            models.Index(fields=['task_id', 'change_xid'], name='idx_a2a_verdict_change'),
        ]
        constraints = [
            models.CheckConstraint(
                check=(models.Q(verdict__isnull=False, outcome__isnull=True)
                       | models.Q(verdict__isnull=True, outcome__isnull=False)),
                name='ck_a2a_verdict_one_of',
            ),
            models.CheckConstraint(
                check=models.Q(verdict__isnull=True) | models.Q(verdict__in=['applied', 'refused']),
                name='ck_a2a_verdict_value',
            ),
            models.CheckConstraint(
                check=models.Q(outcome__isnull=True) | models.Q(outcome__in=['succeeded', 'failed', 'superseded']),
                name='ck_a2a_verdict_outcome',
            ),
            models.CheckConstraint(
                check=~models.Q(verdict='refused') | models.Q(reason__isnull=False),
                name='ck_a2a_verdict_refused_reason',
            ),
            models.CheckConstraint(
                check=models.Q(reason__isnull=True) | models.Q(reason__in=[
                    'invalid', 'unknown_task', 'wrong_project', 'wrong_role', 'task_finished', 'dead_lettered',
                ]),
                name='ck_a2a_verdict_reason',
            ),
        ]

    def __str__(self) -> str:
        return f'A2A verdict {self.message_id}: {self.verdict or self.outcome}'
