"""The evaluation set's own checks (Knowledge Retrieval REQ-09): it covers
what REQ-09 says it must, and its metric scores a perfect ranker 1.0 and a
useless one low. Nothing here ranks."""

import pytest

from retrieval import evalset as ev


def test_covers_all_seven_classes_and_both_scopes():
    answerable = [ev.PASSAGES[q.relevant[0]] for q in ev.QUESTIONS if q.relevant]
    assert {p.source_class for p in answerable} >= set(ev.SEVEN_CLASSES)
    assert {p.scope for p in answerable} == {'project', 'platform'}


def test_session_class_outnumbers_each_authored_class():
    counts = {}
    for p in ev.CORPUS:
        counts[p.source_class] = counts.get(p.source_class, 0) + 1
    assert all(counts[ev.SESSION] > n for cls, n in counts.items() if cls != ev.SESSION)


def test_has_questions_whose_answer_is_that_nothing_is_relevant():
    assert sum(1 for q in ev.QUESTIONS if not q.relevant) >= 5


def test_every_relevant_passage_exists_and_ids_are_unique():
    ids = [p.id for p in ev.CORPUS]
    assert len(ids) == len(set(ids))
    assert all(pid in ev.PASSAGES for q in ev.QUESTIONS for pid in q.relevant)


def test_crowded_questions_have_session_passages_saying_the_same():
    crowded = [q for q in ev.QUESTIONS if q.note == 'crowded by sessions']
    assert len(crowded) >= 2 and all(ev.PASSAGES[q.relevant[0]].source_class == ev.SPEC for q in crowded)


def test_a_perfect_ranker_scores_one():
    truth = {q.text: list(q.relevant) for q in ev.QUESTIONS}
    result = ev.evaluate(lambda question: truth[question])
    assert result.scores == {'hit_at_3': 1.0, 'mrr': 1.0, 'abstention': 1.0, 'class_hit': 1.0}
    assert result.passes()


def test_a_ranker_that_answers_everything_fails_on_abstention():
    truth = {q.text: list(q.relevant) or ['s1'] for q in ev.QUESTIONS}
    result = ev.evaluate(lambda question: truth[question])
    assert result.scores['abstention'] == 0.0 and not result.passes()


def test_a_ranker_that_returns_nothing_fails_on_hits():
    result = ev.evaluate(lambda question: [])
    assert result.scores['hit_at_3'] == 0.0 and not result.passes()


@pytest.mark.django_db
def test_seed_writes_every_passage_with_a_reference_and_its_scope():
    from index.models import IndexUnit
    ev.seed()
    assert IndexUnit.objects.count() == len(ev.CORPUS)
    assert set(IndexUnit.objects.filter(scope='platform').values_list('source_key', flat=True)) == {
        ev.source_key(p.id) for p in ev.CORPUS if p.scope == 'platform'}
