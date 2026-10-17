"""Corpus Indexing REQ-04: indexing is driven by Core's events, not by
polling Core; a stopped indexer's backlog is indexed without loss on
restart; a re-upload is followed, its replaced text never presented as
current in between; and the freshness bound, measured here (provisional,
build brief §4: measured locally, against a fake Core)."""

import time
from datetime import timedelta

import pytest

from extraction.models import ExtractionRecord
from django.utils import timezone

from index.models import IndexState, PendingArtifact
from index.status import index_status
from tests.index_support import (  # noqa: F401 - fixtures
    Background, archive_api, asst, chained, checkout, core, extraction, index_once, prompt, redis_client, search,
    stream, text_block, upload_event, wait_until, work_item_event,
)
from tests.support import Session as Src, cost_state, enqueue

pytestmark = pytest.mark.django_db(transaction=True)

ARTIFACTS = 'aigang:artifacts:events'


@pytest.fixture
def ready(core, redis_client, archive_api, checkout):
    index_once()
    assert not index_status()['incomplete']
    return redis_client


def test_req04_a_record_is_indexed_from_its_event_and_not_by_polling_core(ready, core):
    redis_client = ready
    redis_client.xadd(stream(), work_item_event('work_item.updated'))  # the project's stream exists
    started = timezone.now()
    with Background(seconds=12):
        # The indexer asks for a catch-up of every class when it starts:
        # wait for its loop to run (a heartbeat) and that catch-up to end.
        wait_until(lambda: IndexState.objects.filter(heartbeat_at__gt=started).exists()
                   and not index_status()['pendingCatchUps'])
        item = core.work_item('Grease the pelican hinge')     # committed, but no event is published
        time.sleep(2.5)                                       # several passes of the loop
        assert search('pelican hinge') == []
        redis_client.xadd(stream(), work_item_event('work_item.created', item['id']))
        wait_until(lambda: search('pelican hinge'), timeout=5)


def test_req04_a_stopped_indexers_backlog_is_indexed_without_loss_on_restart(ready, core):
    redis_client = ready
    item = core.work_item('Replace the tern bearings')
    note = core.comment(item['id'], 'The tern bearings arrived damaged.')
    task = core.a2a_task(['Tern bearings are on order.'])
    for event_type in ('work_item.created', 'work_item.comment_added', 'a2a.message_recorded'):
        redis_client.xadd(stream(), work_item_event(event_type, item['id']))
    status = index_status()
    assert status['incomplete'] and status['streams'][stream()]['unread'] == 3

    index_once()
    assert {u.source_class for u in search('tern bearings')} == {'work_item', 'work_item_comment', 'a2a_message'}
    assert note and task and not index_status()['incomplete']


def test_req04_entries_read_but_not_finished_before_a_stop_are_finished_after_it(ready, core):
    """The indexer read entries and stopped before acknowledging them: on
    restart under the same consumer name it finishes them first."""
    redis_client = ready
    item = core.work_item('Oil the gannet winch')
    redis_client.xgroup_create(stream(), 'memory-indexer', id='0', mkstream=True)  # as discover() does
    redis_client.xadd(stream(), work_item_event('work_item.created', item['id']))
    taken = redis_client.xreadgroup('memory-indexer', 'test-indexer', {stream(): '>'}, count=10)
    assert taken and redis_client.xpending(stream(), 'memory-indexer')['pending'] == 1
    assert index_status()['incomplete']

    index_once()
    assert search('gannet winch')
    assert redis_client.xpending(stream(), 'memory-indexer')['pending'] == 0


def test_req04_a_new_projects_stream_is_joined_by_name(ready, core):
    redis_client = ready
    item = core.work_item('Paint the puffin shed', project='second')
    redis_client.xadd(stream('second'), work_item_event('work_item.created', item['id'], project='second'))
    index_once()
    assert search('puffin shed')


def test_req04_a_reupload_is_followed_and_replaced_text_is_never_current_in_between(ready):
    redis_client = ready
    artifact_id, first = extraction(text='Rates: the kestrel rate is four percent.\n')
    redis_client.xadd(ARTIFACTS, upload_event(artifact_id, first))
    index_once()
    assert [u.current for u in search('kestrel rate')] == [True]

    second = first + timedelta(minutes=5)
    redis_client.xadd(ARTIFACTS, upload_event(artifact_id, second, action='replaced'))
    index_once()
    old = search('kestrel rate')
    assert old and not any(u.current for u in old)          # PRD §7.3: not presented as current
    assert 'library: 1 uploaded file(s) awaiting extraction' in index_status()['reasons']

    extraction(artifact_id, text='Rates: the merlin rate is nine percent.\n', updated_at=second)
    index_once()
    assert search('kestrel rate') == []
    assert [u.current for u in search('merlin rate')] == [True]
    assert search('merlin rate')[0].reference['stamp']['updatedAt'] == second.isoformat()
    assert not PendingArtifact.objects.exists() and not index_status()['incomplete']


def test_req04_a_change_no_event_announces_reaches_the_index_through_the_extraction_record(ready):
    """An in-place edit or a lost event reaches the index through the
    extraction feature's sweep: its new extraction record is followed."""
    artifact_id, at = extraction(text='Before: the shrike count.\n')
    index_once()
    assert search('shrike count')
    record = ExtractionRecord.objects.get(artifact_id=artifact_id)
    record.text, record.source_sha256 = 'After: the vireo count.\n', 'f' * 64
    record.save()
    index_once()
    assert search('shrike count') == [] and search('vireo count')


def test_req04_freshness_measured_for_every_class(ready, core, roots, copier, capsys):
    """Measured locally with the default follow intervals; the declared
    bound is in index/__init__.py and is provisional until measured on a
    deployment (build brief §4)."""
    redis_client = ready
    harness, _ = roots
    measured = {}
    with Background(seconds=25):
        wait_until(lambda: not index_status()['incomplete'], timeout=10)
        item = core.work_item('Tune the condor valve')
        redis_client.xadd(stream(), work_item_event('work_item.created', item['id']))
        measured['work_item'] = wait_until(lambda: search('condor valve'))
        core.comment(item['id'], 'The condor gasket leaks.')
        redis_client.xadd(stream(), work_item_event('work_item.comment_added', item['id']))
        measured['comment'] = wait_until(lambda: search('condor gasket'))
        core.delivery(item['id'], 'pr', 'https://example.test/pull/7 condor fix')
        redis_client.xadd(stream(), work_item_event('work_item.artifact_attached', item['id']))
        measured['delivery_artifact'] = wait_until(lambda: search('condor fix'))
        core.a2a_task(['The condor review is approved.'])
        redis_client.xadd(stream(), work_item_event('a2a.message_recorded', item['id']))
        measured['a2a_message'] = wait_until(lambda: search('condor review'))
        s = Src(harness)
        s.append([enqueue()] + chained(prompt('Go.'), asst('m1', text_block('The condor run ended.')))
                 + [cost_state()])
        copier.reconcile()
        measured['session'] = wait_until(lambda: search('condor run'))
        (s.notes_dir() / 'condor.md').write_bytes(b'The condor note.\n')
        copier.reconcile()
        measured['note'] = wait_until(lambda: search('condor note'))
        artifact_id, at = extraction(text='The condor manual.\n')
        redis_client.xadd(ARTIFACTS, upload_event(artifact_id, at))
        measured['library_file'] = wait_until(lambda: search('condor manual'))
    with capsys.disabled():
        print('\nfreshness, seconds from commit (or archive copy, or extraction record) to indexed:',
              {k: round(v, 2) for k, v in measured.items()})
    assert all(seconds < 15 for seconds in measured.values()), measured


# --- Upload events read out of order (REQ-04) -------------------------------------------

def test_req04_an_earlier_uploads_event_read_late_leaves_the_current_text_current(ready):
    """The second upload's extraction is indexed before the first upload's
    event is read: that event is older than what the units were built
    from, so they stay current and retrievable."""
    from retrieval import ranking
    redis_client = ready
    artifact_id, first = extraction(text='Rates: the kestrel rate is four percent.\n')
    second = first + timedelta(minutes=5)
    extraction(artifact_id, text='Rates: the merlin rate is nine percent.\n', updated_at=second)
    redis_client.xadd(ARTIFACTS, upload_event(artifact_id, second, action='replaced'))
    index_once()
    assert [u.current for u in search('merlin rate')] == [True]

    redis_client.xadd(ARTIFACTS, upload_event(artifact_id, first))      # the first upload's event, late
    index_once()
    assert [u.current for u in search('merlin rate')] == [True]
    assert [c.source_key for c in ranking.rank('merlin rate')] == [f'artifact:{artifact_id}']
    assert not PendingArtifact.objects.exists() and not index_status()['incomplete']


def test_req04_an_extraction_indexed_after_a_newer_upload_is_announced_is_not_current(ready):
    """The second upload's event is read before the first upload's
    extraction is indexed: that extraction is no longer current, so its
    units are written as not current until the second's is recorded."""
    from retrieval import ranking
    redis_client = ready
    artifact_id, first = extraction(text='Rates: the kestrel rate is four percent.\n')
    second = first + timedelta(minutes=5)
    redis_client.xadd(ARTIFACTS, upload_event(artifact_id, second, action='replaced'))
    index_once()
    old = search('kestrel rate')
    assert old and not any(u.current for u in old)
    assert ranking.rank('kestrel rate') == []
    assert 'library: 1 uploaded file(s) awaiting extraction' in index_status()['reasons']

    extraction(artifact_id, text='Rates: the merlin rate is nine percent.\n', updated_at=second)
    index_once()
    assert search('kestrel rate') == [] and [u.current for u in search('merlin rate')] == [True]
    assert not index_status()['incomplete']


def test_req04_a_rebuild_while_a_newer_upload_is_pending_does_not_make_the_old_text_current(ready):
    from django.core.management import call_command
    redis_client = ready
    artifact_id, first = extraction(text='Rates: the kestrel rate is four percent.\n')
    redis_client.xadd(ARTIFACTS, upload_event(artifact_id, first))
    index_once()
    assert [u.current for u in search('kestrel rate')] == [True]
    redis_client.xadd(ARTIFACTS, upload_event(artifact_id, first + timedelta(minutes=5), action='replaced'))
    index_once()
    call_command('rebuild_index')
    old = search('kestrel rate')
    assert old and not any(u.current for u in old)


# --- Entries the indexer cannot act on (REQ-09) -----------------------------------------

def test_req09_a_malformed_entry_is_recorded_and_acknowledged_and_never_stops_the_indexer(ready, core):
    """Through memory-indexer itself: a malformed ``uploadedAt``, a payload
    that is not an object and data that is not JSON are each recorded as a
    failure naming the entry and why, and acknowledged; the valid entries
    after them on the same streams are indexed."""
    import json

    from index.models import IndexFailure
    redis_client = ready
    artifact_id, at = extraction(text='The jerboa manual.\n')
    bad_time = json.loads(upload_event(artifact_id, at)['data'])
    bad_time['payload']['uploadedAt'] = 'last tuesday'
    bad_time_id = redis_client.xadd(ARTIFACTS, {'data': json.dumps(bad_time)})
    not_object = json.loads(work_item_event('work_item.created')['data'])
    not_object['payload'] = 'work_item.created'
    not_object_id = redis_client.xadd(stream(), {'data': json.dumps(not_object)})
    not_json_id = redis_client.xadd(stream(), {'data': '{not json'})
    item = core.work_item('Count the jerboa burrows')
    redis_client.xadd(stream(), work_item_event('work_item.created', item['id']))
    second = at + timedelta(minutes=5)
    redis_client.xadd(ARTIFACTS, upload_event(artifact_id, second, action='replaced'))

    index_once()                                         # does not raise
    failures = {f.source_key: f for f in IndexFailure.objects.all()}
    assert set(failures) == {f'event:{ARTIFACTS}:{bad_time_id}', f'event:{stream()}:{not_object_id}',
                             f'event:{stream()}:{not_json_id}'}
    assert "uploadedAt 'last tuesday'" in failures[f'event:{ARTIFACTS}:{bad_time_id}'].reason
    assert 'payload is not a JSON object' in failures[f'event:{stream()}:{not_object_id}'].reason
    assert failures[f'event:{stream()}:{not_json_id}'].payload['fields'] == {'data': '{not json'}
    status = index_status()
    assert status['incomplete'] and '3 record(s) failed to index' in status['reasons']
    for name in (ARTIFACTS, stream()):
        assert redis_client.xpending(name, 'memory-indexer')['pending'] == 0
    assert search('jerboa burrows')                                  # the valid entries after them
    assert PendingArtifact.objects.filter(artifact_id=artifact_id, updated_at=second).exists()
    assert not any(u.current for u in search('jerboa manual'))

    attempts = {f.source_key: f.attempts for f in IndexFailure.objects.all()}
    index_once()                                         # a retry fails again, counted, and still is not fatal
    assert {f.source_key: f.attempts - 1 for f in IndexFailure.objects.all()} == attempts
