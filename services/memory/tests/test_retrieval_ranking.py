"""Knowledge Retrieval REQ-09 and REQ-10: the ranker is measured against
the fixed evaluation set (tests/retrieval_evalset.py), through the real route, and
the evaluation fails when the ranker is deliberately degraded. The
`session` class is weighted so it cannot crowd out authored sources."""

import pytest

from index.models import IndexUnit
from tests import retrieval_evalset as ev
from retrieval import ranking, service
from tests.index_support import redis_client  # noqa: F401 - fixture
from tests.retrieval_support import ask, closed_core, passages  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def evaluated(redis_client, closed_core):
    ev.seed()
    return {u.id: u.source_key.split(':', 1)[1] for u in IndexUnit.objects.all()}


def route_ranker(client, by_unit, **fields):
    def rank(question):
        return [by_unit[p['unitId']] for p in passages(client, question, **fields)]
    return rank


def test_req09_the_ranker_meets_the_declared_threshold_through_the_route(client, evaluated):
    result = ev.evaluate(route_ranker(client, evaluated))
    assert result.passes(), (result.misses(), result.failures)
    # Recorded at build (Open Question 1); these are the values the report states.
    print('REQ-09 scores', {k: round(v, 3) for k, v in result.scores.items()})


def test_req09_a_ranker_that_returns_something_for_everything_fails_the_evaluation(client, evaluated, monkeypatch):
    real = ranking.rank
    # Neither full-text search's floor nor the embedding cutoff holds anything back.
    no_floor = ranking.Params(min_coverage=0.0, min_terms=1, relative_floor=0.0, embedding_cutoff=-1.0)
    monkeypatch.setattr(service.ranking, 'rank', lambda *a, **k: real(*a, **{**k, 'params': no_floor}))
    result = ev.evaluate(route_ranker(client, evaluated))
    print('REQ-09 degraded (no relevance floor)', {k: round(v, 3) for k, v in result.scores.items()})
    assert not result.passes() and result.scores['abstention'] < ev.THRESHOLDS['abstention']


def test_req09_a_ranker_that_puts_the_best_passage_last_fails_the_evaluation(client, evaluated, monkeypatch):
    real = ranking.rank
    wide = ranking.Params(min_coverage=0.0, min_terms=1, relative_floor=0.0, embedding_cutoff=-1.0)
    monkeypatch.setattr(service.ranking, 'rank',
                        lambda *a, **k: list(reversed(real(*a, **{**k, 'params': wide, 'limit': 50}))))
    result = ev.evaluate(route_ranker(client, evaluated))
    print('REQ-09 degraded (reversed order)', {k: round(v, 3) for k, v in result.scores.items()})
    assert not result.passes() and result.scores['hit_at_3'] < ev.THRESHOLDS['hit_at_3']


CROWDED = ('How many times does the gateway retry a failed delivery before giving up?', 's1', 'specification')


def _sessions(found):
    return [p for p in found if p['class'] == 'session']


def test_req10_the_default_weighting_answers_a_specification_question_from_the_specification(client, evaluated):
    question, wanted, _ = CROWDED
    found = passages(client, question)
    # The corpus holds 8 session passages that say the same thing (x5 … x12).
    assert len(_sessions(found)) >= 2
    top3 = [evaluated[p['unitId']] for p in found[:3]]
    assert wanted in top3


def test_req10_changing_the_declared_weighting_changes_how_many_session_passages_appear(client, evaluated):
    question = CROWDED[0]
    counts = {weight: len(_sessions(passages(client, question, sessionWeight=weight, limit=5)))
              for weight in (0, ranking.SESSION_WEIGHT, 3)}
    assert counts[0] == 0
    assert counts[0] < counts[3]
    assert counts[0] <= counts[ranking.SESSION_WEIGHT] <= counts[3]


def test_req10_a_cap_bounds_the_session_passages_in_a_result(client, evaluated):
    question = CROWDED[0]
    for cap in (0, 1, 2):
        found = passages(client, question, sessionWeight=3, sessionCap=cap, limit=8)
        assert len(_sessions(found)) <= cap
    assert len(_sessions(passages(client, question, sessionWeight=3, limit=8))) > 2


def test_req10_more_session_units_than_the_candidate_window_do_not_cut_the_specification(client, redis_client,
                                                                                         closed_core):
    # More session turns than the ranker takes candidates, each matching more of
    # the question's words than the specification does: ranked together with
    # it, every one of them would outrank it, so a window or a place taken
    # across classes would drop it.
    question, wanted, _ = CROWDED
    crowd = [ev.Passage(f'crowd{n}', ev.SESSION,
                        f'Run {n}: how many times did the gateway retry the failed delivery before giving up? '
                        'It retried three times.')
             for n in range(ranking.CANDIDATES + 50)]
    ev.seed(ev.CORPUS + crowd)
    by_unit = {u.id: u.source_key.split(':', 1)[1] for u in IndexUnit.objects.all()}
    assert sum(1 for k in by_unit.values() if k.startswith('crowd')) > ranking.CANDIDATES
    found = passages(client, question)
    assert wanted in [by_unit[p['unitId']] for p in found[:3]]
    # The crowd does outrank it when sessions are weighted up: the weight, not
    # the window, decides.
    favoured = passages(client, question, sessionWeight=3, limit=3)
    assert all(by_unit[p['unitId']].startswith('crowd') for p in favoured)


def test_req05_a_passage_whose_source_states_no_written_time_says_so(client, evaluated):
    # The evaluation corpus is seeded without written times.
    found = passages(client, CROWDED[0])
    assert found
    assert all(p['writtenAt'] is None and p['ageSeconds'] is None and p['writtenAtKnown'] is False for p in found)


def test_req01_a_question_need_not_match_every_word(client, evaluated):
    found = passages(client, 'Please tell me, if you know it, why the shipment emails were sent twice to customers')
    assert found and evaluated[found[0]['unitId']] == 'w1'


def test_req06_scope_is_a_condition_of_the_query_that_finds_the_units(evaluated):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    with CaptureQueriesContext(connection) as queries:
        found = ranking.rank('database', scope='platform')
    selects = [q['sql'] for q in queries if q['sql'].startswith('SELECT id, source_class')]
    assert selects and all('AND scope = ' in sql for sql in selects)
    assert found and {c.scope for c in found} == {'platform'}


def test_the_evaluation_runs_the_same_ranker_the_route_does(evaluated):
    # The route's defaults are the declared ones (REQ-10: declared, not tuned per query).
    assert service.parse({'question': 'x'})['params'] == ranking.Params()
