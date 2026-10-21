"""A Core-class passage returned by the embedding ranking alone (a question
sharing no word with it) resolves to its source like any other (Knowledge
Retrieval REQ-04, AC-07), through the real route and the real indexer."""

import pytest

from retrieval import ranking
from tests.index_support import (  # noqa: F401 - fixtures
    archive_api, checkout, core, extraction, index_once, redis_client, seeded,
)
from tests.retrieval_support import passages, read_source, status_core, unexplained  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db(transaction=True)

COMMENT = 'Customers were charged twice because the payment webhook handler was registered two times.'
# Shares no content word with COMMENT, and the real model puts it above the cutoff.
QUESTION = 'Why did shoppers get billed again for a single purchase?'


def test_ac07_a_core_passage_found_only_by_the_embedding_resolves_against_its_read(client, seeded, status_core,
                                                                                   core):
    comment = core.comment(seeded['item']['id'], COMMENT)
    index_once()
    # The precondition, so the test cannot pass by a lexical match: the
    # floor finds nothing, and the comment is no full-text candidate at all.
    found, present = ranking._lexical(QUESTION, None, ranking.Params())
    assert ranking._floored(found, present, ranking.Params()) == []
    assert not any(COMMENT in c.text for c in found)
    top = ranking._similar(QUESTION, None, 1)[0]
    assert top[1] >= ranking.EMBEDDING_CUTOFF

    result = passages(client, QUESTION)
    passage = result[0]
    assert passage['class'] == 'work_item_comment' and COMMENT in passage['text']
    fields = passage['reference']['fields']
    assert fields and passage['source']['location'] == {'fields': fields}
    body = read_source(core, passage['source'])
    assert unexplained(passage['text'], fields, body) == []
    comment_id = passage['reference']['commentId']
    assert ['comments', {'id': comment_id}, 'body'] in [f['path'] for f in fields]
    assert [c['body'] for c in body['comments'] if c['id'] == comment_id] == [COMMENT]
