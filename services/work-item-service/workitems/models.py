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
        ]

    def __str__(self) -> str:
        return f'{self.artifact_type}:{self.reference}'


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
    relay leaves behind for the next relay run to pick back up."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.TextField()
    event_type = models.TextField()
    work_item_id = models.UUIDField(null=True, blank=True)
    payload = models.JSONField()
    created_at = models.DateTimeField(default=timezone.now)
    published_at = models.DateTimeField(null=True, blank=True)
    stream_entry_id = models.TextField(null=True, blank=True)

    class Meta:
        db_table = 'outbox_event'
        indexes = [
            models.Index(fields=['created_at'], name='idx_outbox_unpublished',
                         condition=models.Q(published_at__isnull=True)),
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
