"""REQ-05 (a malformed file or result cannot terminate the dispatching
service) and REQ-06 (a failure never poses as an empty success, enforced by
the database), through the real worker process (``manage.py
run_extraction``). The extractor's answer is injected at the spool, the
interface between the two processes, because a crafted file's effect is its
result."""

import json
import os
import threading
from datetime import timezone as dt_timezone
from datetime import datetime

import pytest
from django.db import IntegrityError, connection, transaction

from extraction import jobs
from extraction.models import ExtractionJob, ExtractionRecord
from tests.extraction_support import (  # noqa: F401 - fixtures
    core, extractor, process_env, redis_events, spool_dir, start_worker, stop, wait_for, worker_log,
)

pytestmark = pytest.mark.django_db(transaction=True)


class SpoolAnswerer:
    """Plays the extractor on the spool: claims each request and writes the
    given raw result text, exactly where the real extractor would."""

    def __init__(self, spool_dir, raw: str):
        self.spool, self.raw, self.stop = spool_dir, raw, threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self.stop.is_set():
            for request in (self.spool / 'requests').glob('*'):
                try:
                    os.rename(request, self.spool / 'work' / request.name)
                except OSError:
                    continue
                tmp = self.spool / 'results' / f'.{request.name}.tmp'
                tmp.write_text(self.raw)
                os.replace(tmp, self.spool / 'results' / f'{request.name}.json')
            self.stop.wait(0.05)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(5)


def _run_through_worker(core, spool_dir, settings, raw: str, content=b'x\n', name='f.txt'):
    settings.MEMORY_EXTRACTION_SWEEP_SECONDS = 3600
    artifact_id = core.upload(content, name)
    job = jobs.enqueue(artifact_id, 'created')
    worker = start_worker(process_env(settings, connection.settings_dict['NAME']))
    try:
        with SpoolAnswerer(spool_dir, raw):
            assert wait_for(lambda: ExtractionJob.objects.get(pk=job.pk).state == 'done', 30), worker_log(worker)
        assert worker.poll() is None, worker_log(worker)  # the dispatcher survived
    finally:
        stop(worker)
    return artifact_id


def test_a_lone_surrogate_in_the_result_does_not_terminate_the_worker(core, spool_dir, redis_events, settings):
    raw = json.dumps({'outcome': 'extracted', 'format': 'pdf', 'text': 'before \ud800 after'})
    artifact_id = _run_through_worker(core, spool_dir, settings, raw)
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert (record.outcome, record.text) == ('extracted', 'before ? after')


def test_a_result_that_is_not_an_object_is_recorded_as_a_failure_and_the_worker_survives(
        core, spool_dir, redis_events, settings):
    artifact_id = _run_through_worker(core, spool_dir, settings, json.dumps(['not', 'an', 'object']))
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert record.outcome == 'failed' and record.text is None
    assert 'malformed result' in record.failure_reason


@pytest.mark.parametrize('result', [
    {'outcome': 'bogus', 'format': 'text'},
    {'outcome': 'extracted', 'format': 'text'},  # no text
    {'outcome': 'extracted', 'format': 'text', 'text': 5},
    {'outcome': 'extracted', 'format': 'x' * 300, 'text': 'ok'},
    {'outcome': 'failed', 'format': 'text', 'reason': {'a': 1}},
])
def test_a_result_that_breaks_the_contract_is_a_recorded_failure(core, spool_dir, redis_events, settings, result):
    artifact_id = _run_through_worker(core, spool_dir, settings, json.dumps(result))
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    assert record.outcome == 'failed' and record.text is None
    assert record.failure_reason


def test_the_extractor_child_replaces_a_surrogate_it_cannot_encode(tmp_path, monkeypatch):
    from extractor import child
    from extractor.formats import Extracted

    (tmp_path / 'content').write_bytes(b'x')
    (tmp_path / 'meta.json').write_text('{}')
    monkeypatch.setattr(child, 'decide_format', lambda data, name: ('pdf', None))
    monkeypatch.setattr(child, 'extract', lambda data, name: Extracted(format='pdf', text='a\ud800b\x00c'))
    out = child.run(tmp_path)
    assert out['text'] == 'a?bc'
    out['text'].encode('utf-8')


# --- REQ-06: the database refuses a failure posing as an empty success ------

def _record(**fields):
    base = {'artifact_id': '11111111-1111-1111-1111-111111111111', 'source_updated_at': datetime.now(dt_timezone.utc),
            'source_sha256': 'a' * 64, 'outcome': 'extracted', 'format': 'text', 'text': '', 'failure_reason': None,
            'original_filename': '', 'extracted_at': datetime.now(dt_timezone.utc)}
    base.update(fields)
    return ExtractionRecord(**base)


def test_an_empty_success_is_accepted():
    _record().save()  # the control: empty text is a legitimate success


@pytest.mark.parametrize('fields', [
    {'text': None},  # a success with no text
    {'text': 'x', 'failure_reason': 'but also a reason'},
    {'source_sha256': None},
    {'format': None},
    {'outcome': 'failed', 'text': None, 'failure_reason': None},  # a failure with no reason
    {'outcome': 'failed', 'text': None, 'failure_reason': ''},  # a failure with an empty reason
    {'outcome': 'failed', 'text': '', 'failure_reason': 'why'},  # a failure posing as an empty success
    {'outcome': 'failed', 'text': 'partial', 'failure_reason': 'why'},
    {'outcome': 'unknown', 'text': 'x'},
])
def test_the_database_refuses_a_record_that_is_neither_a_success_nor_a_failure(fields):
    with pytest.raises(IntegrityError), transaction.atomic():
        _record(**fields).save()
    assert not ExtractionRecord.objects.exists()
