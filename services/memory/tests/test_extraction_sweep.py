"""The reconciliation sweep (REQ-04 (b), (c), (d)): files no event announced
are still found, through Core's listing and byte reads."""

import pytest
from django.db import connection

from extraction import jobs
from extraction.events import STREAM
from extraction.models import ExtractionJob, ExtractionRecord
from extraction.sweep import sweep
from tests.extraction_support import (  # noqa: F401 - fixtures
    PNG, core, drain, extractor, make_pdf, process_env, redis_events, spool_dir, start_worker, stop, upload_event,
    wait_for,
)

pytestmark = pytest.mark.django_db(transaction=True)


def _record(artifact_id):
    return ExtractionRecord.objects.filter(artifact_id=artifact_id).first()


def test_req04b_a_file_uploaded_while_the_consumer_was_stopped_is_extracted_within_the_sweep_interval(
        core, spool_dir, extractor, redis_events, settings):
    settings.MEMORY_EXTRACTION_SWEEP_SECONDS = 2
    env = process_env(settings, connection.settings_dict['NAME'])
    worker = start_worker(env)
    assert wait_for(lambda: redis_events.exists(STREAM), 30)
    stop(worker)  # the consumer is stopped
    announced = core.upload(b'Uploaded while stopped; its event survives.\n', 'a.txt')
    redis_events.xadd(STREAM, upload_event(announced))
    lost = core.upload(b'Uploaded while stopped; its event was lost.\n', 'b.txt')
    worker = start_worker(env)
    try:
        assert wait_for(lambda: _record(announced) is not None and _record(lost) is not None, 30)
    finally:
        stop(worker)
    assert _record(lost).text == 'Uploaded while stopped; its event was lost.\n'
    assert ExtractionJob.objects.get(artifact_id=lost).trigger == 'sweep:missing'


def test_req04c_an_in_place_edit_is_re_extracted_by_the_sweep(core, spool_dir, extractor):
    artifact_id = core.upload(b'Before the edit.\n', 'e.txt')
    jobs.enqueue(artifact_id, 'created')
    drain(spool_dir)
    updated_at = _record(artifact_id).source_updated_at
    core.edit_in_place(artifact_id, b'After the edit on the volume.\n')  # no event, same updatedAt
    counts = sweep()
    assert counts['sweep:changed'] == 1
    drain(spool_dir)
    record = _record(artifact_id)
    assert record.text == 'After the edit on the volume.\n'
    assert record.source_updated_at == updated_at


def test_req04d_after_one_sweep_every_pre_existing_file_has_a_record_or_a_recorded_failure(
        core, spool_dir, extractor):
    library = [core.upload(b'Plain file %d.\n' % n, f'{n}.txt') for n in range(5)]
    library.append(core.upload(make_pdf('An old PDF'), ''))
    library.append(core.upload(PNG, 'old.png'))
    counts = sweep()
    assert counts == {'artifacts': 7, 'sweep:missing': 7, 'sweep:updated': 0, 'sweep:changed': 0}
    drain(spool_dir)
    outcomes = {str(r.artifact_id): r.outcome for r in ExtractionRecord.objects.all()}
    assert set(outcomes) == set(library)
    assert sorted(outcomes.values()).count('failed') == 1


def test_the_sweep_leaves_unchanged_files_alone_and_retries_a_failure_only_when_its_bytes_change(
        core, spool_dir, extractor):
    text_id = core.upload(b'Steady.\n', 's.txt')
    image_id = core.upload(PNG, 'p.png')
    sweep()
    drain(spool_dir)
    assert sweep() == {'artifacts': 2, 'sweep:missing': 0, 'sweep:updated': 0, 'sweep:changed': 0}
    assert _record(image_id).outcome == 'failed'
    core.edit_in_place(image_id, b'Now it is text.\n')
    assert sweep()['sweep:changed'] == 1
    drain(spool_dir)
    assert _record(image_id).text == 'Now it is text.\n'
    assert _record(text_id).text == 'Steady.\n'


def test_the_sweep_finds_a_reupload_whose_event_was_lost(core, spool_dir, extractor):
    artifact_id = core.upload(b'One.\n', 'r.txt')
    sweep()
    drain(spool_dir)
    core.reupload(artifact_id, b'Two.\n')
    assert sweep()['sweep:updated'] == 1
    drain(spool_dir)
    assert _record(artifact_id).text == 'Two.\n'


def test_the_sweep_skips_an_artifact_whose_job_is_open(core, spool_dir):
    artifact_id = core.upload(b'Queued.\n', 'q.txt')
    jobs.enqueue(artifact_id, 'created')
    assert sweep() == {'artifacts': 1, 'sweep:missing': 0, 'sweep:updated': 0, 'sweep:changed': 0}
    assert core.byte_reads.get(artifact_id, 0) == 0


def test_the_sweep_pages_through_the_whole_listing(core, spool_dir, monkeypatch):
    from extraction import core_client
    ids = [core.upload(b'%d' % n, '') for n in range(7)]
    real = core_client.list_artifacts
    monkeypatch.setattr(core_client, 'list_artifacts', lambda: real(page_size=3))
    assert sweep()['sweep:missing'] == 7
    assert {str(a) for a in ExtractionJob.objects.values_list('artifact_id', flat=True)} == set(ids)
