"""Corpus Indexing REQ-07, the indexer's half: a record deleted from its
authoritative home is removed from the index, learnt from Core's
``work_item.record_deleted`` outbox event (track 1's half), and a direct
search of the index for its content finds nothing. Only Core's admin
delete writes that event; a work item deleted in the tracker has no path
here (build brief I-06)."""

import pytest

from index.models import IndexSource
from tests.index_support import (  # noqa: F401 - fixtures
    archive_api, checkout, core, extraction, index_once, redis_client, search, stream, units, work_item_event,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def indexed(core, redis_client, archive_api, checkout):
    spec_id, _ = extraction(text='The gharial requirement.')
    item = core.work_item('Move the gharial tank', 'Gharial tank relocation plan.', spec_artifact=spec_id)
    comment = core.comment(item['id'], 'The gharial tank leaked overnight.')
    delivery = core.delivery(item['id'], 'commit', 'f00dbad gharial tank fix')
    other = core.work_item('Feed the caiman')
    index_once()
    assert search('gharial tank leaked') and search('gharial tank fix') and search('caiman')
    return {'item': item, 'comment': comment, 'delivery': delivery, 'spec': spec_id, 'other': other}


def _deleted(record_class, record_id, work_item_id):
    return work_item_event('work_item.record_deleted', work_item_id,
                           {'recordClass': record_class, 'id': record_id, 'workItemId': work_item_id,
                            'deletedBy': 'admin'})


def test_req07_a_deleted_comment_is_gone_from_the_index(indexed, core, redis_client):
    comment = indexed['comment']
    core.delete('comments', comment['id'])
    redis_client.xadd(stream(), _deleted('comment', comment['id'], indexed['item']['id']))
    index_once()
    assert search('gharial tank leaked') == []
    assert not IndexSource.objects.filter(pk=f"comment:{comment['id']}").exists()
    assert search('gharial tank fix') and search('gharial relocation')


def test_req07_a_deleted_delivery_artifact_is_gone_from_the_index(indexed, core, redis_client):
    delivery = indexed['delivery']
    core.delete('delivery', delivery['id'])
    redis_client.xadd(stream(), _deleted('delivery_artifact', delivery['id'], indexed['item']['id']))
    index_once()
    assert search('f00dbad') == [] and search('gharial tank fix') == []


def test_req07_a_deleted_work_item_takes_its_comments_and_delivery_artifacts_with_it(indexed, core, redis_client):
    item = indexed['item']
    for kind, record in (('comments', indexed['comment']), ('delivery', indexed['delivery']),
                         ('work_items', item)):
        core.delete(kind, record['id'])
    redis_client.xadd(stream(), _deleted('work_item', item['id'], item['id']))
    index_once()
    for words in ('gharial relocation', 'gharial tank leaked', 'gharial tank fix', 'move gharial'):
        assert search(words) == [], words
    assert not [u for u in units() if u.work_item_id is not None and str(u.work_item_id) == item['id']]
    assert search('caiman')
    # The library file it named is no longer a specification.
    assert {u.source_class for u in search('gharial requirement')} == {'library_file'}


def test_req07_a_deleted_specification_link_reclasses_the_file_it_named(indexed, redis_client):
    assert {u.source_class for u in search('gharial requirement')} == {'specification'}
    redis_client.xadd(stream(), _deleted('specification_link', 'link-1', indexed['item']['id']))
    index_once()
    assert {u.source_class for u in search('gharial requirement')} == {'library_file'}


def test_req07_a_later_catch_up_does_not_bring_a_deleted_record_back(indexed, core, redis_client):
    comment = indexed['comment']
    core.delete('comments', comment['id'])
    redis_client.xadd(stream(), _deleted('comment', comment['id'], indexed['item']['id']))
    redis_client.xadd(stream(), work_item_event('work_item.comment_added', indexed['item']['id']))
    index_once()
    index_once()
    assert search('gharial tank leaked') == []


def test_req07_a_failed_comment_does_not_come_back_after_its_work_item_is_deleted(core, redis_client, archive_api,
                                                                                    checkout, monkeypatch):
    """A comment that failed to index has no unit or source row naming its
    work item, only its failure. Deleting the work item removes that
    failure too, so no retry indexes the comment again from it."""
    from django.core.management import call_command

    from index import render
    from index.models import IndexFailure
    real = render.comment

    def comment(record):
        if 'dingo' in record['body']:
            raise ValueError('cannot render this comment')
        return real(record)

    monkeypatch.setattr(render, 'comment', comment)
    item = core.work_item('Fence the dingo yard')
    bad = core.comment(item['id'], 'The dingo burrowed under the wire.')
    index_once()
    failure = IndexFailure.objects.get(source_key=f"comment:{bad['id']}")
    assert failure.payload['workItemId'] == item['id']

    core.delete('comments', bad['id'])
    core.delete('work_items', item['id'])
    redis_client.xadd(stream(), _deleted('work_item', item['id'], item['id']))
    monkeypatch.setattr(render, 'comment', real)    # a retry would now succeed, if one were attempted
    index_once()                                    # the deletion, then memory-indexer's own retry pass
    call_command('index_retry')
    assert not IndexFailure.objects.exists()
    assert not units(source_key=f"comment:{bad['id']}")
    assert search('dingo burrowed') == []
