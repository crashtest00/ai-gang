"""Corpus Indexing REQ-09: a record that fails to index leaves a recorded
failure naming the record and the reason, is absent from the index, counts
toward REQ-05's signal, and is retried without a rebuild: by
``manage.py index_retry`` and by memory-indexer's own pass."""

import pytest
from django.core.management import call_command

from index import render
from index.models import IndexFailure
from index.status import index_status
from tests.index_support import (  # noqa: F401 - fixtures
    archive_api, checkout, core, index_once, redis_client, search,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def broken(core, redis_client, archive_api, checkout, monkeypatch):
    """Rendering fails for one comment and works for the rest."""
    real = render.comment

    def comment(record):
        if 'jackal' in record['body']:
            raise ValueError('cannot render this comment')
        return real(record)

    monkeypatch.setattr(render, 'comment', comment)
    item = core.work_item('Check the hyena fence')
    bad = core.comment(item['id'], 'The jackal gate is open.')
    core.comment(item['id'], 'The hyena fence is fine.')
    index_once()
    return {'bad': bad, 'undo': lambda: monkeypatch.setattr(render, 'comment', real)}


def test_req09_a_failure_is_recorded_with_record_and_reason_and_the_record_is_absent(broken):
    failure = IndexFailure.objects.get()
    assert failure.source_key == f"comment:{broken['bad']['id']}"
    assert failure.source_class == 'work_item_comment'
    assert 'cannot render this comment' in failure.reason
    assert search('jackal gate') == []
    assert search('hyena fence')                     # the rest of the page was indexed
    status = index_status()
    assert status['incomplete'] and '1 record(s) failed to index' in status['reasons']
    assert status['failures'][0]['source'] == failure.source_key


def test_req09_a_failed_record_is_retried_without_a_rebuild(broken, capsys):
    broken['undo']()
    call_command('index_retry')
    assert '1 record(s) indexed on retry' in capsys.readouterr().out
    assert not IndexFailure.objects.exists()
    assert search('jackal gate') and not index_status()['incomplete']


def test_req09_a_retry_that_fails_again_counts_its_attempts(broken):
    before = IndexFailure.objects.get().attempts
    call_command('index_retry')
    assert IndexFailure.objects.get().attempts == before + 1


def test_req09_memory_indexers_own_pass_retries_failures(broken):
    broken['undo']()
    index_once()
    assert not IndexFailure.objects.exists() and search('jackal gate')
