"""Memory's manifest (Durable Agent Session Record REQ-04) and note records.

The schema is Memory's own and the database enforces it: the closed sets of
`status`, `quiet_by` and a commit's `role` are CHECK constraints, and a
session's pull requests and commits are unique per session (see `Meta`
below and migration 0001).
"""

import uuid

from django.db import connection, models


STATUS_ACTIVE = 'active'
STATUS_QUIET = 'quiet'
STATUS_REWRITTEN = 'rewritten'
STATUSES = (STATUS_ACTIVE, STATUS_QUIET, STATUS_REWRITTEN)

QUIET_BY_END_RECORD = 'end_record'
QUIET_BY_INTERVAL = 'interval'

ROLE_AUTHORED = 'authored'
ROLE_MERGED = 'merged'


def next_change_seq():
    """The next value of the change sequence REQ-09's and the notes read's
    cursors are positions in. A database sequence, so it only rises."""
    with connection.cursor() as cur:
        cur.execute("SELECT nextval('archive_change_seq')")
        return cur.fetchone()[0]


class ArchiveEpoch(models.Model):
    """One row, written by migration 0001. A cursor carries this database's
    epoch; after the database is dropped and re-migrated the epoch differs,
    so a cursor issued before the rebuild is refused (§4)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    epoch = models.UUIDField(default=uuid.uuid4, editable=False)

    class Meta:
        constraints = [models.CheckConstraint(check=models.Q(id=1), name='archive_epoch_single_row')]


class Session(models.Model):
    session_id = models.CharField(max_length=255, primary_key=True)
    project = models.CharField(max_length=255)
    source_path = models.CharField(max_length=1024)
    first_seen = models.DateTimeField()
    last_growth = models.DateTimeField()
    harness_versions = models.JSONField(default=list)
    status = models.CharField(max_length=16, default=STATUS_ACTIVE)
    record_counts = models.JSONField(default=dict)
    quiet_by = models.CharField(max_length=16, null=True, blank=True)
    # Run state of the session's own transcript (REQ-05, REQ-06): whether
    # the newest run has ended, and the harness version (REQ-05's rule) of
    # the transcript's last record, which a transition to quiet is counted
    # against.
    run_ended = models.BooleanField(default=False)
    last_record_version = models.CharField(max_length=64, default='unknown')
    change_seq = models.BigIntegerField(default=0, db_index=True)

    class Meta:
        constraints = [
            models.CheckConstraint(check=models.Q(status__in=STATUSES), name='session_status_valid'),
            models.CheckConstraint(
                check=models.Q(quiet_by__isnull=True) | models.Q(quiet_by__in=(QUIET_BY_END_RECORD, QUIET_BY_INTERVAL)),
                name='session_quiet_by_valid'),
        ]


class SessionFile(models.Model):
    """One copied file of a session. `path` is relative to the harness
    volume's root; the archive keeps the same relative path under its own
    root, so `archive_path` equals it."""

    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name='files')
    path = models.CharField(max_length=1024)
    archive_path = models.CharField(max_length=1024)
    bytes_read = models.BigIntegerField(default=0)
    digest = models.CharField(max_length=80)
    partial_tail = models.BooleanField(default=False)
    # The largest size the source has been seen at, and its mtime then:
    # growth is the source getting larger (REQ-06), whether or not the new
    # bytes can be copied yet.
    seen_size = models.BigIntegerField(default=0)
    seen_mtime_ns = models.BigIntegerField(default=0)
    # Set when the source shrank or its copied prefix stopped matching:
    # the copy is frozen as it was (REQ-03, spike decision 6).
    frozen = models.BooleanField(default=False)
    # How far REQ-05/REQ-07 have read this file's archive copy, and the
    # state they carry between reads (see `Copier._parse` in copier.py, which advances `parsed_to` and
    # stores its carried state in `parse_state`).
    parsed_to = models.BigIntegerField(default=0)
    parse_state = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['path'], name='session_file_path_unique'),
            models.CheckConstraint(check=models.Q(bytes_read__gte=0), name='session_file_bytes_read_nonnegative'),
        ]


class SessionPullRequest(models.Model):
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name='pull_requests')
    repository = models.CharField(max_length=255)
    number = models.IntegerField()
    url = models.CharField(max_length=1024, blank=True, default='')
    first_seen = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['session', 'repository', 'number'], name='session_pr_unique'),
        ]
        indexes = [models.Index(fields=['repository', 'number'])]


class SessionCommit(models.Model):
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name='commits')
    hash = models.CharField(max_length=40, db_index=True)
    role = models.CharField(max_length=16)
    time = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['session', 'hash'], name='session_commit_unique'),
            models.CheckConstraint(check=models.Q(role__in=(ROLE_AUTHORED, ROLE_MERGED)), name='session_commit_role_valid'),
        ]


class Note(models.Model):
    """A project's `memory/` note at its latest version (REQ-03, REQ-04)."""

    project = models.CharField(max_length=255)
    path = models.CharField(max_length=1024)
    archive_path = models.CharField(max_length=1024)
    digest = models.CharField(max_length=80)
    read_at = models.DateTimeField()
    removed = models.BooleanField(default=False)
    seen_size = models.BigIntegerField(default=0)
    seen_mtime_ns = models.BigIntegerField(default=0)
    change_seq = models.BigIntegerField(default=0, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['path'], name='note_path_unique')]


class HarnessVersionTotals(models.Model):
    """The counts read's totals per harness version (REQ-08): REQ-05's
    unknown and unparseable records, REQ-06's transitions to quiet."""

    version = models.CharField(max_length=64, unique=True)
    unknown = models.BigIntegerField(default=0)
    unparseable = models.BigIntegerField(default=0)
    quiet_end_record = models.BigIntegerField(default=0)
    quiet_interval = models.BigIntegerField(default=0)
