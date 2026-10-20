"""Track 5: hybrid ranking through the real route (Knowledge Retrieval
REQ-01, REQ-09). Full-text search and the embedding ranking are fused by
reciprocal rank fusion; a question is answered with nothing only when
full-text search's floor finds nothing and no unit's similarity reaches the
declared cutoff; and when the floor finds nothing but the embedding does,
the answer is the embedding ranking alone."""

import pytest

from index.models import IndexUnit
from retrieval import ranking, service
from tests import retrieval_evalset as ev
from tests.index_support import redis_client  # noqa: F401 - fixture
from tests.retrieval_support import ask, closed_core, passages  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db(transaction=True)

# Shares no content word with the specification it is about (s4: "A refund
# above five hundred dollars requires approval from a second administrator
# before it is issued").
PARAPHRASE = 'Who has to sign off before we pay a customer back a large sum?'
UNANSWERABLE = 'Who won the football match last night?'


@pytest.fixture
def evaluated(redis_client, closed_core):  # noqa: F811
    ev.seed()
    return {u.id: u.source_key.split(':', 1)[1] for u in IndexUnit.objects.all()}


def test_a_question_sharing_no_word_with_its_answer_is_answered_from_the_embedding(client, evaluated):
    found, present = ranking._lexical(PARAPHRASE, None, ranking.Params())
    assert ranking._floored(found, present, ranking.Params()) == []   # full-text search alone abstains
    result = passages(client, PARAPHRASE)
    assert evaluated[result[0]['unitId']] == 's4'


def test_when_the_floor_finds_nothing_the_answer_is_the_embedding_ranking_alone(client, evaluated):
    similar = [(unit_id, session) for unit_id, sim, session in ranking._similar(PARAPHRASE, None, 20)
               if sim >= ranking.EMBEDDING_CUTOFF]
    places, counts = {}, {True: 0, False: 0}
    for unit_id, session in similar:
        counts[session] += 1
        places[unit_id] = counts[session]
    result = passages(client, PARAPHRASE, sessionWeight=1)
    assert result
    # Each passage scores exactly its place in the embedding ranking: no
    # full-text match (all rejected by the floor) adds to any score.
    for passage in result:
        assert passage['unitId'] in places
        assert passage['score'] == round(1 / (ranking.RRF_K + places[passage['unitId']]), 4)


def test_a_unit_below_the_cutoff_is_no_embedding_match(client, evaluated):
    question = 'Why do prices drift by a cent?'
    below = {unit_id for unit_id, sim, _ in ranking._similar(question, None, 20) if sim < ranking.EMBEDDING_CUTOFF}
    found, _ = ranking._lexical(question, None, ranking.Params())
    lexical = {c.unit_id for c in found}
    assert below - lexical
    returned = {p['unitId'] for p in passages(client, question, limit=50)}
    assert not returned & (below - lexical)


def test_an_unanswerable_question_returns_nothing(client, evaluated):
    status, body = ask(client, UNANSWERABLE)
    # Not "nothing relevant" here only because this fixture never marks the
    # index caught up (REQ-07); the result is empty and nothing was withheld.
    assert status == 200 and body['passages'] == [] and body['withheld'] == []
    top = ranking._similar(UNANSWERABLE, None, 1)[0][1]
    assert top < ranking.EMBEDDING_CUTOFF


def test_abstention_needs_both_the_floor_and_the_cutoff(client, evaluated, monkeypatch):
    real = ranking.rank
    monkeypatch.setattr(service.ranking, 'rank',
                        lambda *a, **k: real(*a, **{**k, 'params': ranking.Params(embedding_cutoff=-1.0)}))
    assert passages(client, UNANSWERABLE)            # the floor alone does not abstain on it
    monkeypatch.setattr(service.ranking, 'rank',
                        lambda *a, **k: real(*a, **{**k, 'params': ranking.Params(embedding_cutoff=1.01)}))
    assert passages(client, PARAPHRASE) == []        # nor, here, does the embedding alone answer
    assert passages(client, 'Why do prices drift by a cent?')   # but the floor still answers a lexical one


def test_a_unit_first_in_both_rankings_leads_with_the_fused_score(client, evaluated):
    question = 'Why do prices drift by a cent?'
    found, _ = ranking._lexical(question, None, ranking.Params())
    assert evaluated[found[0].unit_id] == 'c2'
    assert evaluated[ranking._similar(question, None, 1)[0][0]] == 'c2'
    top = passages(client, question)[0]
    assert evaluated[top['unitId']] == 'c2'
    assert top['score'] == round(2 / (ranking.RRF_K + 1), 4)


def test_session_weight_applies_to_the_fused_score(client, evaluated):
    question = 'What stopped the out-of-memory kills of the workers?'
    full = {evaluated[p['unitId']]: p['score'] for p in passages(client, question, sessionWeight=1)}
    half = {evaluated[p['unitId']]: p['score'] for p in passages(client, question, sessionWeight=0.5)}
    assert 'x1' in full and 'x1' in half
    assert half['x1'] == pytest.approx(full['x1'] * 0.5, abs=1e-4)
    authored = [k for k in full if not k.startswith('x')]
    assert all(half[k] == full[k] for k in authored if k in half)


def test_a_replaced_unit_is_ranked_by_its_new_embedding_not_a_kept_one(client, evaluated):
    # The process keeps vectors it has read (ranking._VectorCache); a unit
    # whose text changes is a new row, so its new vector is read.
    question = 'Who has to sign off before we pay a customer back a large sum?'
    assert evaluated[passages(client, question)[0]['unitId']] == 's4'
    s4 = ev.PASSAGES['s4']
    replaced = ev.Passage('s4', s4.source_class, 'REQ-30: Lighthouse lamps are cleaned every winter by the harbour crew.')
    ev.seed([replaced])
    by_unit = {u.id: u.source_key.split(':', 1)[1] for u in IndexUnit.objects.all()}
    found = [by_unit[p['unitId']] for p in passages(client, question)]
    assert 's4' not in found[:1]
    assert by_unit[passages(client, 'When are the lighthouse lamps cleaned?')[0]['unitId']] == 's4'



def _s4_is_embedding_only(unit_id):
    """The precondition of the two tests below, so neither passes vacuously:
    the unit shares no word with the question (full-text search does not
    find it, floor or no floor) and its similarity clears the cutoff."""
    found, _ = ranking._lexical(PARAPHRASE, None, ranking.Params())
    assert unit_id not in {c.unit_id for c in found}
    sims = {u: sim for u, sim, _ in ranking._similar(PARAPHRASE, None, 50)}
    assert sims[unit_id] >= ranking.EMBEDDING_CUTOFF


def test_req06_a_unit_matching_only_by_embedding_outside_the_declared_scope_is_not_returned(client, evaluated):
    s4 = next(u for u, key in evaluated.items() if key == 's4')
    _s4_is_embedding_only(s4)
    assert IndexUnit.objects.get(pk=s4).scope == 'project'
    assert s4 in {p['unitId'] for p in passages(client, PARAPHRASE, limit=50)}                # control
    assert s4 in {p['unitId'] for p in passages(client, PARAPHRASE, scope='project', limit=50)}
    assert s4 not in {p['unitId'] for p in passages(client, PARAPHRASE, scope='platform', limit=50)}


def test_req04_a_unit_matching_only_by_embedding_that_is_not_current_is_not_returned(client, evaluated):
    s4 = next(u for u, key in evaluated.items() if key == 's4')
    _s4_is_embedding_only(s4)
    assert s4 in {p['unitId'] for p in passages(client, PARAPHRASE, limit=50)}                # control
    IndexUnit.objects.filter(pk=s4).update(current=False)
    assert s4 not in {p['unitId'] for p in passages(client, PARAPHRASE, limit=50)}
