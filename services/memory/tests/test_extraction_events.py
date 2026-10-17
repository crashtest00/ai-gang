"""The ``artifact.uploaded`` consumer (REQ-03, REQ-04(a)), through the real
worker process reading the real stream name on Redis."""

import time

import pytest
from django.db import connection

from extraction.events import GROUP, STREAM, EventConsumer
from extraction.models import ExtractionJob, ExtractionRecord
from tests.extraction_support import (  # noqa: F401 - fixtures
    REDIS_TEST_URL, core, extractor, process_env, redis_events, spool_dir, start_worker, stop, worker_log, upload_event,
    wait_for,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _record(artifact_id):
    return ExtractionRecord.objects.filter(artifact_id=artifact_id).first()


def test_req04a_an_uploaded_file_is_extracted_from_its_event_within_the_bound(
        core, spool_dir, extractor, redis_events, settings):
    settings.MEMORY_EXTRACTION_SWEEP_SECONDS = 3600
    worker = start_worker(process_env(settings, connection.settings_dict['NAME']))
    try:
        assert wait_for(lambda: redis_events.exists(STREAM) and redis_events.xinfo_groups(STREAM), 30)
        artifact_id = core.upload(b'Fresh upload about kingfishers.\n', 'k.txt')
        committed = time.monotonic()
        redis_events.xadd(STREAM, upload_event(artifact_id, 'created'))
        assert wait_for(lambda: _record(artifact_id) is not None, 60), worker_log(worker)
        elapsed = time.monotonic() - committed
    finally:
        stop(worker)
    assert _record(artifact_id).text == 'Fresh upload about kingfishers.\n'
    assert ExtractionJob.objects.get(artifact_id=artifact_id).trigger == 'created'
    # The declared extraction bound is five minutes (REQ-04); this measures
    # the event path for a small file on an idle worker.
    print(f'\nextraction bound measurement (event to record, small text file): {elapsed:.2f}s')
    assert elapsed < 300


def test_req03_a_replaced_event_causes_a_new_extraction(core, spool_dir, extractor, redis_events, settings):
    settings.MEMORY_EXTRACTION_SWEEP_SECONDS = 3600
    artifact_id = core.upload(b'Version one.\n', 'v.txt')
    redis_events.xadd(STREAM, upload_event(artifact_id, 'created'))
    worker = start_worker(process_env(settings, connection.settings_dict['NAME']))
    try:
        assert wait_for(lambda: _record(artifact_id) is not None, 60)
        new_updated_at = core.reupload(artifact_id, b'Version two.\n')
        redis_events.xadd(STREAM, upload_event(artifact_id, 'replaced'))
        assert wait_for(lambda: _record(artifact_id).source_updated_at == new_updated_at, 60)
    finally:
        stop(worker)
    assert _record(artifact_id).text == 'Version two.\n'
    assert list(ExtractionJob.objects.filter(artifact_id=artifact_id).order_by('id')
                .values_list('trigger', flat=True)) == ['created', 'replaced']


def test_an_entry_is_acknowledged_only_after_its_job_is_committed(core, redis_events):
    artifact_id = core.upload(b'x', 'x.txt')
    redis_events.xadd(STREAM, upload_event(artifact_id))
    # A worker that created the group and read the entry, but stopped before
    # its job committed: the entry stays pending to that consumer, and its
    # next run reads it.
    redis_events.xgroup_create(STREAM, GROUP, id='0')
    redis_events.xreadgroup(GROUP, 'w1', {STREAM: '>'}, count=10)
    assert redis_events.xpending(STREAM, GROUP)['pending'] == 1
    assert not ExtractionJob.objects.exists()
    restarted = EventConsumer(REDIS_TEST_URL, 'w1')
    assert restarted.poll(block_ms=10) == 1
    assert ExtractionJob.objects.get().artifact_id.hex == artifact_id.replace('-', '')
    assert redis_events.xpending(STREAM, GROUP)['pending'] == 0
    assert restarted.poll(block_ms=10) == 0


def test_a_first_start_queues_every_upload_the_stream_still_holds(core, redis_events):
    ids = [core.upload(f'file {n}'.encode(), f'{n}.txt') for n in range(3)]
    for artifact_id in ids:
        redis_events.xadd(STREAM, upload_event(artifact_id))
    consumer = EventConsumer(REDIS_TEST_URL, 'w1')
    consumer.poll(block_ms=10)  # its own pending list: empty
    consumer.poll(block_ms=10)
    assert sorted(str(j) for j in ExtractionJob.objects.values_list('artifact_id', flat=True)) == sorted(ids)


def test_entries_that_are_not_upload_events_are_skipped_and_acknowledged(redis_events):
    redis_events.xadd(STREAM, {'data': 'not json'})
    redis_events.xadd(STREAM, {'data': '{"payload": {"eventType": "artifact.deleted", "artifactId": "x"}}'})
    redis_events.xadd(STREAM, {'other': 'field'})
    consumer = EventConsumer(REDIS_TEST_URL, 'w1')
    consumer.poll(block_ms=10)
    assert consumer.poll(block_ms=10) == 3
    assert not ExtractionJob.objects.exists()
    assert redis_events.xpending(STREAM, GROUP)['pending'] == 0
