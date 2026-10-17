"""The extraction record and the job store (artifact-text-extraction.md
REQ-02, REQ-03, REQ-06, REQ-07), in Memory's PostgreSQL.

**The extraction record** is keyed by the artifact's canonical id (its
primary key), so an artifact has exactly one: after a re-upload is
re-extracted it holds the new text only (REQ-03). It carries the source
stamp (REQ-02): ``source_updated_at``, the artifact record's ``updatedAt``
as read just before the bytes were read, and ``source_sha256``, the SHA-256
of the bytes actually read. ``outcome`` is ``extracted`` or ``failed``; a
successful extraction with no text is ``extracted`` with ``text = ''``, so
it can never be mistaken for a failure, and a failure always carries its
reason and never any text (REQ-06). The database enforces all of that:
``extraction_record_*`` CHECK constraints in ``Meta`` and migration 0001.

**Currency** (REQ-03) is not stored: a record is current only while its
``source_updated_at`` equals the ``updatedAt`` on Core's present record,
which a consumer reads from Core. ``ExtractionRecord.is_current_for`` is
that comparison, and the job's completion uses it to refuse a stale result
(REQ-07).

**The job store** tracks each extraction as a job whose state survives
the process running it (REQ-07). A job is ``pending``, ``running`` (claimed,
with a lease), ``done`` or ``superseded`` (it finished after its artifact
was replaced, so its result was discarded). At most one job per artifact is
pending and at most one is running: partial unique indexes
``extraction_job_one_pending_per_artifact`` and
``extraction_job_one_running_per_artifact``.
"""

from __future__ import annotations

from django.db import models
from django.db.models import Q

OUTCOME_EXTRACTED = 'extracted'
OUTCOME_FAILED = 'failed'
OUTCOMES = (OUTCOME_EXTRACTED, OUTCOME_FAILED)

JOB_PENDING = 'pending'
JOB_RUNNING = 'running'
JOB_DONE = 'done'
JOB_SUPERSEDED = 'superseded'
JOB_STATES = (JOB_PENDING, JOB_RUNNING, JOB_DONE, JOB_SUPERSEDED)
OPEN_JOB_STATES = (JOB_PENDING, JOB_RUNNING)


class ExtractionRecord(models.Model):
    artifact_id = models.UUIDField(primary_key=True)
    # The source stamp (REQ-02).
    source_updated_at = models.DateTimeField()
    # Null only for a failure where no bytes could be read (Core's record
    # names a file that is missing from the volume).
    source_sha256 = models.CharField(max_length=64, null=True, blank=True)
    outcome = models.CharField(max_length=16)
    # The format the extractor decided from the content (REQ-06); null only
    # for a failure that happened before a format was decided.
    format = models.CharField(max_length=255, null=True, blank=True)
    text = models.TextField(null=True, blank=True)
    failure_reason = models.TextField(null=True, blank=True)
    # Core's recorded original filename at read time, which the format
    # decision may use; opaque, as V4 keeps it.
    original_filename = models.TextField(blank=True, default='')
    extracted_at = models.DateTimeField()

    class Meta:
        db_table = 'extraction_record'
        constraints = [
            models.CheckConstraint(check=Q(outcome__in=OUTCOMES), name='extraction_record_outcome_valid'),
            # REQ-06: a success has text (possibly empty) and no reason; a
            # failure has a reason and no text. Neither can pose as the other.
            models.CheckConstraint(
                check=(Q(outcome=OUTCOME_EXTRACTED, text__isnull=False, failure_reason__isnull=True,
                         source_sha256__isnull=False, format__isnull=False)
                       | Q(outcome=OUTCOME_FAILED, text__isnull=True, failure_reason__isnull=False)),
                name='extraction_record_success_or_failure'),
            models.CheckConstraint(check=~Q(failure_reason=''), name='extraction_record_reason_not_empty'),
        ]

    def stamp(self):
        return (self.source_updated_at, self.source_sha256)

    def is_current_for(self, core_updated_at) -> bool:
        """REQ-03: current only while the stamp's ``updatedAt`` equals the
        ``updatedAt`` on the artifact's present Core record."""
        return core_updated_at is not None and self.source_updated_at == core_updated_at


class ExtractionJob(models.Model):
    artifact_id = models.UUIDField()
    state = models.CharField(max_length=16, default=JOB_PENDING)
    # What asked for it: the upload event's action (`created`, `replaced`)
    # or the sweep's finding (`sweep:missing`, `sweep:updated`,
    # `sweep:changed`).
    trigger = models.CharField(max_length=32)
    # Attempts submitted to the extractor for the current stamp; reset when
    # the stamp changes. At MEMORY_EXTRACTION_ATTEMPT_LIMIT the file is
    # recorded as a failure (brief I-26).
    attempts = models.PositiveIntegerField(default=0)
    not_before = models.DateTimeField()
    claimed_by = models.CharField(max_length=255, null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    # The stamp of the attempt in hand, once the two record reads agreed.
    stamp_updated_at = models.DateTimeField(null=True, blank=True)
    stamp_sha256 = models.CharField(max_length=64, null=True, blank=True)
    # How it ended, or why it was released (human-readable).
    note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'extraction_job'
        constraints = [
            models.CheckConstraint(check=Q(state__in=JOB_STATES), name='extraction_job_state_valid'),
            models.CheckConstraint(
                check=(Q(state__in=OPEN_JOB_STATES, finished_at__isnull=True)
                       | ~Q(state__in=OPEN_JOB_STATES) & Q(finished_at__isnull=False)),
                name='extraction_job_finished_iff_closed'),
            models.CheckConstraint(
                check=~Q(state=JOB_RUNNING) | Q(claimed_by__isnull=False, lease_expires_at__isnull=False),
                name='extraction_job_running_has_lease'),
            models.UniqueConstraint(fields=['artifact_id'], condition=Q(state=JOB_PENDING),
                                    name='extraction_job_one_pending_per_artifact'),
            models.UniqueConstraint(fields=['artifact_id'], condition=Q(state=JOB_RUNNING),
                                    name='extraction_job_one_running_per_artifact'),
        ]
        indexes = [models.Index(fields=['state', 'not_before'], name='idx_extraction_job_ready')]
