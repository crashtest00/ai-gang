"""Corpus Indexing REQ-05: lag and backlog are observable, and clear when
the index catches up. A library file awaiting extraction counts as "not
indexed yet"; a failed extraction does not (REQ-05; brief I-22). Retrieval's
report of the signal is track 4's (Knowledge Retrieval REQ-07). Here the
signal is read where retrieval will read it: ``index.status.index_status``."""

import pytest
import redis

from index.status import index_status
from tests.index_support import (  # noqa: F401 - fixtures
    archive_api, asst, chained, checkout, core, extraction, index_once, prompt, redis_client, search, stream,
    text_block, upload_event, work_item_event,
)
from tests.support import Session as Src, cost_state, enqueue

pytestmark = pytest.mark.django_db(transaction=True)

ARTIFACTS = 'aigang:artifacts:events'


@pytest.fixture
def ready(core, redis_client, archive_api, checkout):
    index_once()
    assert not index_status()['incomplete']
    return redis_client


def test_req05_events_the_indexer_has_not_read_are_lag(ready, core):
    item = core.work_item('Survey the lynx trail')
    ready.xadd(stream(), work_item_event('work_item.created', item['id']))
    status = index_status()
    assert status['incomplete']
    assert any(stream() in reason and '1 unread' in reason for reason in status['reasons'])
    index_once()
    assert not index_status()['incomplete'] and search('lynx trail')


def test_req05_a_catch_up_core_could_not_serve_stays_visible_until_it_runs(ready, core):
    item = core.work_item('Survey the bobcat trail')
    core.available = False
    ready.xadd(stream(), work_item_event('work_item.created', item['id']))
    index_once()
    status = index_status()
    assert 'core:work_items: a catch-up is pending' in status['reasons']
    assert search('bobcat trail') == []

    core.available = True
    index_once()
    assert not index_status()['incomplete'] and search('bobcat trail')


def test_req05_a_library_file_awaiting_extraction_is_not_indexed_yet_and_a_failed_one_is_not_lag(ready):
    artifact_id, at = extraction(text='placeholder')  # an id and a time; the record is replaced below
    from extraction.models import ExtractionRecord
    ExtractionRecord.objects.filter(artifact_id=artifact_id).delete()
    ready.xadd(ARTIFACTS, upload_event(artifact_id, at))
    index_once()
    assert 'library: 1 uploaded file(s) awaiting extraction' in index_status()['reasons']

    extraction(artifact_id, text='The caracal register.', updated_at=at)
    index_once()
    assert not index_status()['incomplete'] and search('caracal register')

    failed_id, failed_at = extraction(text='x')
    ExtractionRecord.objects.filter(artifact_id=failed_id).delete()
    ready.xadd(ARTIFACTS, upload_event(failed_id, failed_at))
    index_once()
    assert index_status()['incomplete']
    extraction(failed_id, outcome='failed', updated_at=failed_at)
    index_once()
    status = index_status()
    assert not status['incomplete'], status['reasons']
    from index.models import IndexUnit
    assert not IndexUnit.objects.filter(source_key=f'artifact:{failed_id}').exists()


def test_req05_an_extraction_record_not_yet_indexed_is_lag(ready):
    extraction(text='The serval inventory.')
    assert 'library: 1 extraction record(s) not indexed yet' in index_status()['reasons']
    index_once()
    assert not index_status()['incomplete']


def test_req05_archive_changes_not_yet_read_are_lag(ready, roots, copier):
    harness, _ = roots
    s = Src(harness)
    s.append([enqueue()] + chained(prompt('Go.'), asst('m1', text_block('The margay run.'))) + [cost_state()])
    (s.notes_dir() / 'margay.md').write_bytes(b'The margay note.\n')
    copier.reconcile()
    reasons = index_status()['reasons']
    assert 'sessions: the archive has changes the index has not read' in reasons
    assert 'notes: the archive has changes the index has not read' in reasons
    index_once()
    assert not index_status()['incomplete'] and search('margay run') and search('margay note')


def test_req05_unreadable_event_streams_are_reported_as_possible_incompleteness(ready):
    unreachable = redis.Redis.from_url('redis://127.0.0.1:1/0', socket_connect_timeout=0.2)
    status = index_status(unreachable)
    assert status['incomplete'] and any(r.startswith('event streams unreadable') for r in status['reasons'])
