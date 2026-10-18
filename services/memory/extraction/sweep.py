"""The reconciliation sweep (artifact-text-extraction.md REQ-04, Decision 2).

The upload event does not reach every file: the relay can still lose one in
its inherited window, files uploaded before V6 never announced themselves to
this feature, and an in-place edit on the volume announces nothing and does
not move ``updatedAt``. So every MEMORY_EXTRACTION_SWEEP_SECONDS the sweep
walks Core's artifact listing and compares each artifact with its extraction
record, queueing a job for any artifact that

    has no extraction record                         (``sweep:missing``)
    has a record whose ``updatedAt`` differs         (``sweep:updated``)
    has a record whose hash differs from a hash of
    the bytes Core serves now                        (``sweep:changed``)

The last needs the bytes, so the sweep hashes every artifact without an open
job, streaming them through Core's retrieval by canonical id. A failure
record is compared the same way, so a failed file is retried when, and only
when, its bytes change. An artifact with an open job is skipped: that job
reads the content as it is when it runs.
"""

from __future__ import annotations

import logging

from . import core_client, jobs
from .models import ExtractionRecord

log = logging.getLogger('memory.extraction')


def sweep() -> dict:
    """One full pass over the library. Returns counts by finding."""
    counts = {'artifacts': 0, 'sweep:missing': 0, 'sweep:updated': 0, 'sweep:changed': 0}
    for artifact_id, updated_at in core_client.list_artifacts():
        counts['artifacts'] += 1
        if jobs.has_open_job(artifact_id):
            continue
        record = ExtractionRecord.objects.filter(artifact_id=artifact_id).first()
        if record is None:
            finding = 'sweep:missing'
        elif not record.is_current_for(updated_at):  # REQ-03's currency test
            finding = 'sweep:updated'
        else:
            try:
                sha = core_client.read_bytes(artifact_id, None)
            except core_client.FileMissing:
                sha = None
            except FileNotFoundError:
                continue  # deleted since the listing was read
            if sha == record.source_sha256:
                continue
            finding = 'sweep:changed'
        jobs.enqueue(artifact_id, finding)
        counts[finding] += 1
    log.info('extraction sweep: %s', counts)
    return counts
