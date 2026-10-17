"""Extraction jobs: enqueueing, claiming, the stamp protocol and completion
(artifact-text-extraction.md REQ-01, REQ-02, REQ-03, REQ-06, REQ-07).

**The stamp protocol (REQ-02).** For each attempt the worker reads Core's
record, then the bytes, then the record again. Only when the two record
reads agree on ``updatedAt`` is the pair (that ``updatedAt``, the SHA-256 of
the bytes read) a stamp; otherwise the bytes are discarded and the reads
start over. ``updatedAt`` is the only currency key (REQ-03; brief I-05): a
stamp that pairs an old ``updatedAt`` with new bytes, possible because a
re-upload writes its bytes before its row commits, is replaced when the
``replaced`` event's job reads the new ``updatedAt``.

**Idempotence (REQ-07).** A job whose stamp equals the extraction record's
stamp ends ``done`` without extracting, so resuming or repeating a job never
produces a second extraction for the same stamp, and the record table's
primary key (the artifact id) allows only one record per artifact.

**Completion and the superseded rule (REQ-03, REQ-07).** Before a result is
written, Core's record is read once more. If its ``updatedAt`` is no longer
the stamp's, or the extraction record already holds a later stamp, the job
ends ``superseded`` and its result is discarded: it never displaces text
from newer content.

**Attempts (REQ-06, REQ-07; brief I-26).** A job whose state lives in the
database survives the worker: a worker killed mid-file leaves its job
``running`` under a lease, and the next worker (or the same one, restarted)
claims it again. An attempt is counted each time a file is handed to the
extractor; an attempt that ends without a result (the extractor or the
worker stopped mid-file) is tried again. When MEMORY_EXTRACTION_ATTEMPT_LIMIT
attempts on the same stamp have ended that way, the file is the likely
cause, and it is recorded as a failure with that reason rather than tried
forever. A crash the extractor itself observes (its per-file child died) is
a failure on the first attempt.
"""

from __future__ import annotations

import logging
import socket
import uuid
from datetime import timedelta
from typing import Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import core_client
from .models import (
    JOB_DONE, JOB_PENDING, JOB_RUNNING, JOB_SUPERSEDED, OPEN_JOB_STATES, OUTCOME_EXTRACTED, OUTCOME_FAILED,
    ExtractionJob, ExtractionRecord,
)
from .spool import NO_RESULT, UNCLAIMED, Spool

log = logging.getLogger('memory.extraction')

# How many times one attempt re-reads when the record moves between its two
# reads (REQ-02's "otherwise it retries") before releasing the job.
STAMP_TRIES = 5
# How long a job waits after the extractor did not pick a file up, or after
# Core could not be read.
RELEASE_BACKOFF_SECONDS = 15


def worker_id() -> str:
    return settings.MEMORY_EXTRACTION_WORKER_ID or socket.gethostname()


def _lease_seconds() -> float:
    return settings.MEMORY_EXTRACTION_TIME_LIMIT_SECONDS + 3 * settings.MEMORY_EXTRACTION_GRACE_SECONDS


def enqueue(artifact_id, trigger: str) -> Optional[ExtractionJob]:
    """Ask for an extraction of the artifact as it is now. A job already
    pending for it covers this request (it has not read anything yet), so
    none is added; a running job does not, because it may have read the
    content this request is about."""
    now = timezone.now()
    try:
        with transaction.atomic():
            return ExtractionJob.objects.create(artifact_id=artifact_id, trigger=trigger, state=JOB_PENDING,
                                                not_before=now, created_at=now)
    except IntegrityError:  # extraction_job_one_pending_per_artifact
        return None


def claim(worker: str) -> Optional[ExtractionJob]:
    """Claim the oldest job that is ready: pending and due, or running under
    a lease that has expired, or running under this worker's own name (a
    previous run of this worker stopped mid-job). Skips artifacts another
    live worker is extracting."""
    now = timezone.now()
    busy = ExtractionJob.objects.filter(state=JOB_RUNNING, lease_expires_at__gt=now).exclude(claimed_by=worker)
    with transaction.atomic():
        job = (ExtractionJob.objects.select_for_update(skip_locked=True)
               .filter(Q(state=JOB_PENDING, not_before__lte=now)
                       | Q(state=JOB_RUNNING, lease_expires_at__lte=now)
                       | Q(state=JOB_RUNNING, claimed_by=worker))
               .exclude(artifact_id__in=busy.values('artifact_id'))
               .order_by('created_at', 'id').first())
        if job is None:
            return None
        if job.state == JOB_PENDING and ExtractionJob.objects.filter(
                artifact_id=job.artifact_id, state=JOB_RUNNING).exists():
            return None  # its artifact's running job is resumed first, in created order
        job.state = JOB_RUNNING
        job.claimed_by = worker
        job.lease_expires_at = now + timedelta(seconds=_lease_seconds())
        job.save(update_fields=['state', 'claimed_by', 'lease_expires_at'])
    return job


def _finish(job: ExtractionJob, state: str, note: str) -> None:
    job.state = state
    job.note = note
    job.finished_at = timezone.now()
    job.claimed_by = None
    job.lease_expires_at = None
    job.save(update_fields=['state', 'note', 'finished_at', 'claimed_by', 'lease_expires_at',
                            'stamp_updated_at', 'stamp_sha256', 'attempts'])


def _release(job: ExtractionJob, note: str, delay: float = 0) -> None:
    """Return the job to pending, to be tried again (REQ-07). If a newer
    pending job for the artifact exists, it covers this one, which ends."""
    job.claimed_by = None
    job.lease_expires_at = None
    job.note = note
    job.not_before = timezone.now() + timedelta(seconds=delay)
    if ExtractionJob.objects.filter(artifact_id=job.artifact_id, state=JOB_PENDING).exists():
        job.state = JOB_DONE
        job.finished_at = timezone.now()
        job.note = f'{note}; covered by a newer pending job'
    else:
        job.state = JOB_PENDING
    job.save()


def _read_stamp(job: ExtractionJob, spool: Spool, name: str):
    """REQ-02: record, bytes, record. Returns ``(record, sha256)`` with the
    bytes staged under ``name``, or a string saying why there is no stamp."""
    for _ in range(STAMP_TRIES):
        first = core_client.get_record(job.artifact_id)
        if first is None:
            return 'gone'
        staged = spool.stage(name)
        try:
            sha = core_client.read_bytes(job.artifact_id, staged / 'content')
        except core_client.FileMissing:
            sha = None
        except FileNotFoundError:
            spool.discard(name)
            return 'gone'
        second = core_client.get_record(job.artifact_id)
        if second is None:
            spool.discard(name)
            return 'gone'
        if first.updated_at == second.updated_at:
            return first, sha
        spool.discard(name)  # re-uploaded between the reads: no stamp for this read
    return 'moving'


def _complete(job: ExtractionJob, record: core_client.ArtifactRecord, sha: Optional[str], *, outcome: str,
              fmt: Optional[str], text: Optional[str] = None, reason: Optional[str] = None) -> str:
    """Write the result unless it is no longer current (REQ-03, REQ-07).
    Returns the job's final state."""
    present = core_client.get_record(job.artifact_id)
    if present is None:
        _finish(job, JOB_DONE, 'artifact no longer in Core; nothing recorded')
        return JOB_DONE
    if present.updated_at != record.updated_at:
        _finish(job, JOB_SUPERSEDED,
                f'superseded: artifact re-uploaded (updatedAt {present.updated_at.isoformat()}) '
                f'while content of {record.updated_at.isoformat()} was extracted; result discarded')
        return JOB_SUPERSEDED
    with transaction.atomic():
        existing = ExtractionRecord.objects.select_for_update().filter(artifact_id=job.artifact_id).first()
        if existing is not None and existing.source_updated_at > record.updated_at:
            _finish(job, JOB_SUPERSEDED, 'superseded: the extraction record already holds newer content')
            return JOB_SUPERSEDED
        ExtractionRecord.objects.update_or_create(
            artifact_id=job.artifact_id,
            defaults={
                'source_updated_at': record.updated_at, 'source_sha256': sha, 'outcome': outcome,
                'format': fmt, 'text': text if outcome == OUTCOME_EXTRACTED else None,
                'failure_reason': reason if outcome == OUTCOME_FAILED else None,
                'original_filename': record.original_filename, 'extracted_at': timezone.now(),
            })
        _finish(job, JOB_DONE, f'recorded: {outcome}' + (f' ({reason})' if reason else ''))
    return JOB_DONE


def run(job: ExtractionJob, spool: Spool) -> str:
    """Run one claimed job to an end state or back to pending. Returns the
    job's state afterwards."""
    # Unique per attempt, so a result an older attempt's extractor run writes
    # late can never be read as this attempt's.
    attempt_name = f'{job.id}-{uuid.uuid4().hex}'
    spool.clear(job.id)
    try:
        stamp = _read_stamp(job, spool, attempt_name)
    except core_client.CoreUnavailable as err:
        spool.discard(attempt_name)
        _release(job, f'Core unavailable: {err}', RELEASE_BACKOFF_SECONDS)
        return job.state
    if stamp == 'gone':
        _finish(job, JOB_DONE, 'artifact no longer in Core; nothing recorded')
        return JOB_DONE
    if stamp == 'moving':
        _release(job, 'the record changed between its reads on every try; no stamp recorded')
        return job.state
    record, sha = stamp

    try:
        existing = ExtractionRecord.objects.filter(artifact_id=job.artifact_id).first()
        if existing is not None and existing.stamp() == (record.updated_at, sha):
            spool.discard(attempt_name)
            _finish(job, JOB_DONE, 'unchanged: the record already holds this stamp')
            return JOB_DONE

        if sha is None:
            spool.discard(attempt_name)
            return _complete(job, record, None, outcome=OUTCOME_FAILED, fmt=None,
                             reason='the stored file is missing from the artifact volume')

        if (job.stamp_updated_at, job.stamp_sha256) != (record.updated_at, sha):
            job.stamp_updated_at, job.stamp_sha256, job.attempts = record.updated_at, sha, 0
        limit = settings.MEMORY_EXTRACTION_ATTEMPT_LIMIT
        if job.attempts >= limit:
            spool.discard(attempt_name)
            return _complete(job, record, sha, outcome=OUTCOME_FAILED, fmt=None, reason=(
                f'extraction did not complete in {limit} attempts: the extractor or its worker stopped '
                f'while this file was being parsed each time ({job.note or "no result"})'))

        job.attempts += 1
        job.lease_expires_at = timezone.now() + timedelta(seconds=_lease_seconds())
        job.save(update_fields=['stamp_updated_at', 'stamp_sha256', 'attempts', 'lease_expires_at'])

        time_limit = settings.MEMORY_EXTRACTION_TIME_LIMIT_SECONDS
        spool.submit(attempt_name, original_filename=record.original_filename, time_limit=time_limit)
        result = spool.await_result(attempt_name, time_limit=time_limit, grace=settings.MEMORY_EXTRACTION_GRACE_SECONDS)

        if result == UNCLAIMED:
            job.attempts -= 1  # never reached the extractor
            _release(job, 'the extractor did not pick the file up', RELEASE_BACKOFF_SECONDS)
            return job.state
        if result == NO_RESULT or result.get('outcome') == 'lost':
            why = 'no result from the extractor' if result == NO_RESULT else result.get('reason', 'lost')
            _release(job, f'attempt {job.attempts} ended without a result: {why}')
            return job.state
        if result.get('outcome') == OUTCOME_EXTRACTED:
            return _complete(job, record, sha, outcome=OUTCOME_EXTRACTED, fmt=result.get('format'),
                             text=result.get('text', ''))
        return _complete(job, record, sha, outcome=OUTCOME_FAILED, fmt=result.get('format'),
                         reason=result.get('reason') or 'extraction failed with no reason given')
    except core_client.CoreUnavailable as err:
        _release(job, f'Core unavailable: {err}', RELEASE_BACKOFF_SECONDS)
        return job.state


def has_open_job(artifact_id) -> bool:
    return ExtractionJob.objects.filter(artifact_id=artifact_id, state__in=OPEN_JOB_STATES).exists()
