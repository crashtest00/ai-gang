"""REQ-07 (resumable, idempotent jobs; superseded results) and brief I-26
(the attempt limit that tells a file that keeps killing the extractor from a
worker that died mid-file once), through the real worker process
(``manage.py run_extraction``) and the real extractor process."""

import subprocess
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from extraction import jobs
from extraction.models import ExtractionJob, ExtractionRecord
from tests.extraction_support import (  # noqa: F401 - fixtures
    core, drain, extractor, heavy_html, process_env, redis_events, spool_dir, start_extractor, start_worker, stop, worker_log,
    wait_for,
)

pytestmark = pytest.mark.django_db(transaction=True)


def test_repeating_a_job_for_the_same_stamp_produces_no_second_extraction(core, spool_dir, extractor):
    artifact_id = core.upload(b'Once only.\n', 'once.txt')
    jobs.enqueue(artifact_id, 'created')
    drain(spool_dir)
    first = ExtractionRecord.objects.get(artifact_id=artifact_id)
    jobs.enqueue(artifact_id, 'created')  # the same event delivered twice
    jobs.enqueue(artifact_id, 'sweep:missing')  # coalesced: one job is already pending
    assert ExtractionJob.objects.filter(artifact_id=artifact_id, state='pending').count() == 1
    assert drain(spool_dir) == ['done']
    again = ExtractionJob.objects.filter(artifact_id=artifact_id).order_by('-id').first()
    assert again.note.startswith('unchanged')
    assert ExtractionRecord.objects.filter(artifact_id=artifact_id).count() == 1
    assert ExtractionRecord.objects.get(artifact_id=artifact_id).extracted_at == first.extracted_at


def test_a_reupload_while_the_first_extraction_is_in_flight_supersedes_its_result(core, spool_dir, extractor):
    artifact_id = core.upload(b'Draft one mentions the heron.\n', 'draft.txt')

    def reupload_once_the_stamp_is_taken(read_id, nth):
        if read_id == artifact_id and nth == 2:  # the protocol's second record read
            core.reupload(artifact_id, b'Draft two is about egrets.\n')

    core.after_record_read = reupload_once_the_stamp_is_taken
    first = jobs.enqueue(artifact_id, 'created')
    assert drain(spool_dir) == ['superseded']
    first.refresh_from_db()
    assert 'result discarded' in first.note
    assert not ExtractionRecord.objects.filter(artifact_id=artifact_id).exists()
    jobs.enqueue(artifact_id, 'replaced')  # the re-upload's event
    assert drain(spool_dir) == ['done']
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert record.text == 'Draft two is about egrets.\n'
    assert record.source_updated_at == core.updated_at(artifact_id)


def test_an_event_for_an_artifact_whose_job_is_running_queues_another_job(core, spool_dir):
    artifact_id = core.upload(b'x', 'x.txt')
    first = jobs.enqueue(artifact_id, 'created')
    assert jobs.enqueue(artifact_id, 'replaced') is None  # pending: it has read nothing yet
    assert jobs.claim('test-worker').id == first.id
    second = jobs.enqueue(artifact_id, 'replaced')  # running: it may hold the old content
    assert second is not None and second.state == 'pending'
    assert jobs.claim('test-worker') is None  # not while the first is running


def test_a_result_older_than_the_record_never_displaces_it(core, spool_dir, extractor):
    artifact_id = core.upload(b'Newer text.\n', 'n.txt')
    jobs.enqueue(artifact_id, 'created')
    drain(spool_dir)
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    # Core's record moves back in time (a restored backup, say): a job for
    # that older content must not overwrite the newer record.
    core.artifacts[artifact_id]['updatedAt'] = record.source_updated_at - timedelta(hours=1)
    core.edit_in_place(artifact_id, b'Older text.\n')
    job = jobs.enqueue(artifact_id, 'sweep:updated')
    assert drain(spool_dir) == ['superseded']
    job.refresh_from_db()
    assert 'newer content' in job.note
    assert ExtractionRecord.objects.get(artifact_id=artifact_id).text == 'Newer text.\n'


def test_a_job_another_worker_left_running_is_claimed_once_its_lease_expires(core, spool_dir):
    now = timezone.now()
    job = ExtractionJob.objects.create(artifact_id='00000000-0000-0000-0000-000000000001', trigger='created',
                                       state='running', claimed_by='gone-worker', created_at=now, not_before=now,
                                       lease_expires_at=now + timedelta(minutes=5))
    assert jobs.claim('test-worker') is None  # still leased
    ExtractionJob.objects.filter(id=job.id).update(lease_expires_at=now - timedelta(seconds=1))
    claimed = jobs.claim('test-worker')
    assert claimed.id == job.id and claimed.claimed_by == 'test-worker'


def test_killing_the_worker_mid_file_leaves_an_incomplete_job_a_later_run_completes(
        core, spool_dir, redis_events, settings):
    settings.MEMORY_EXTRACTION_GRACE_SECONDS = 60  # the file waits in the spool: the worker is mid-file
    settings.MEMORY_EXTRACTION_SWEEP_SECONDS = 3600
    artifact_id = core.upload(b'Survives a dead worker.\n', 'w.txt')
    job = jobs.enqueue(artifact_id, 'created')
    env = process_env(settings, connection.settings_dict['NAME'])

    worker = start_worker(env)
    try:
        assert wait_for(lambda: any((spool_dir / 'requests').glob(f'{job.id}-*')), 30), worker_log(worker)
    finally:
        stop(worker)  # SIGKILL, mid-file

    job.refresh_from_db()
    assert job.state == 'running' and job.finished_at is None  # recorded as incomplete
    assert not ExtractionRecord.objects.filter(artifact_id=artifact_id).exists()

    extractor = start_extractor(spool_dir)
    worker = start_worker(env)  # the same container restarted: same worker id
    try:
        assert wait_for(lambda: ExtractionJob.objects.get(id=job.id).state == 'done', 60)
    finally:
        stop(worker)
        stop(extractor)
    job.refresh_from_db()
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert record.text == 'Survives a dead worker.\n'
    assert ExtractionRecord.objects.filter(artifact_id=artifact_id, source_updated_at=record.source_updated_at,
                                           source_sha256=record.source_sha256).count() == 1
    assert job.attempts == 2  # the killed attempt and the one that finished


def _kill_extractor(process, spool):
    stop(process)
    # Its per-file child runs in a session of its own; a container kill
    # takes it too, so the test does the same.
    subprocess.run(['pkill', '-KILL', '-f', f'extractor.child {spool}'], check=False)


def test_i26_a_file_the_extractor_dies_on_at_every_attempt_is_recorded_as_a_failure_at_the_limit(
        core, spool_dir, redis_events, settings):
    settings.MEMORY_EXTRACTION_SWEEP_SECONDS = 3600
    settings.MEMORY_EXTRACTION_ATTEMPT_LIMIT = 3
    artifact_id = core.upload(heavy_html(), 'killer.html')
    job = jobs.enqueue(artifact_id, 'created')
    worker = start_worker(process_env(settings, connection.settings_dict['NAME']))
    seen = set()

    def new_attempt_in_hand():
        return {p.name for p in (spool_dir / 'work').glob(f'{job.id}-*')} - seen

    try:
        for _attempt in range(3):
            extractor = start_extractor(spool_dir)
            # The extractor dies while this file is in its hands, as an OOM
            # kill of the whole container would.
            assert wait_for(new_attempt_in_hand, 30)
            seen.update(new_attempt_in_hand())
            _kill_extractor(extractor, spool_dir)
        extractor = start_extractor(spool_dir)  # reports the third attempt lost
        try:
            assert wait_for(lambda: ExtractionRecord.objects.filter(artifact_id=artifact_id).exists(), 60)
        finally:
            _kill_extractor(extractor, spool_dir)
    finally:
        stop(worker)
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert record.outcome == 'failed'
    assert record.failure_reason.startswith('extraction did not complete in 3 attempts')
    assert 'the extractor stopped while this file was being parsed' in record.failure_reason
    job.refresh_from_db()
    assert (job.state, job.attempts) == ('done', 3)
