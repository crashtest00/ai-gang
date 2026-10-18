"""Scoring helpers of the paraphrase gate on tiny synthetic sets: no model,
no network, no MEMORY_PARAPHRASE_SET (the gate module loads the embedding
model lazily, so importing it is cheap)."""

import pytest

from tests import paraphrase_gate as pg


def q(qid, tier, answer='p1', doc='d', fact='f'):
    return {'qid': qid, 'tier': tier, 'answers': [answer], 'doc': doc, 'fact_id': fact}


def test_no_concept_tier_and_no_division_by_zero():
    qs = [q('1', 'lexical'), q('2', 'paraphrase'), q('3', 'none', fact='x-near')]
    res = {'1': ['p1'], '2': ['a', 'b', 'c', 'p1'], '3': []}
    assert pg.tiers_of(qs) == ['lexical', 'paraphrase']
    s = pg.score(qs, res)
    assert 'concept' not in s
    assert s['lexical']['hit3'] == 1.0 and s['paraphrase']['hit3'] == 0.0
    assert s['paraphrase']['hit10'] == 1.0 and s['paraphrase']['mrr'] == 0.25
    assert s['lexical/planning'] == {'n': 0, 'hit3': None, 'hit10': None, 'mrr': None}
    assert s['abstention']['none_all'] == 1 and s['abstention']['none_near'] == 1


def test_extra_tier_is_scored_and_ordered_last():
    qs = [q('1', 'zeta'), q('2', 'concept'), q('3', 'lexical'), q('4', 'alpha')]
    res = {'1': ['p1'], '2': [], '3': ['p1'], '4': ['x']}
    assert pg.tiers_of(qs) == ['lexical', 'concept', 'alpha', 'zeta']
    s = pg.score(qs, res)
    assert s['zeta']['hit3'] == 1.0 and s['alpha']['hit3'] == 0.0
    assert s['abstention']['false_abstentions'] == 1
    assert s['abstention']['false_abstentions_by_tier'] == {'lexical': 0, 'concept': 1, 'alpha': 0, 'zeta': 0}


def test_no_none_questions():
    s = pg.score([q('1', 'lexical')], {'1': ['p1']})
    assert s['abstention']['none_n'] == 0 and s['abstention']['none_all'] == 0
    assert s['overall']['n'] == 1


def test_missing_doc_and_fact_id_do_not_crash():
    qs = [{'qid': '1', 'tier': 'lexical', 'answers': ['p1']}, {'qid': '2', 'tier': 'none', 'answers': []}]
    s = pg.score(qs, {'1': ['p1'], '2': ['z']})
    assert 'lexical/planning' not in s and s['lexical']['hit3'] == 1.0
    assert s['abstention']['none_near'] == 0


def test_planning_split_uses_given_docs():
    qs = [q('1', 'lexical', doc='a'), q('2', 'lexical', doc='b')]
    s = pg.score(qs, {'1': ['p1'], '2': ['x']}, planning={'a'})
    assert s['lexical/planning']['hit3'] == 1.0 and s['lexical/research']['hit3'] == 0.0


def test_gate_pass_fail_and_missing_tier():
    qs = [q('1', 'lexical'), q('2', 'paraphrase')]
    s = pg.score(qs, {'1': ['p1'], '2': ['p1']})
    assert pg.gate_failures(s, pg.parse_gate()) == []
    failures = pg.gate_failures(s, pg.parse_gate('concept=0.5'))
    assert len(failures) == 1 and "'concept'" in failures[0]
    bad = pg.score(qs, {'1': [], '2': ['x']})
    assert len(pg.gate_failures(bad, pg.parse_gate())) == 2  # lexical and paraphrase; 1 abstention is within 2


def test_parse_gate_overrides_only_named_keys():
    g = pg.parse_gate('paraphrase=0.5, false_abstentions=0')
    assert g == {'lexical': 0.95, 'paraphrase': 0.5, 'false_abstentions': 0.0}
    assert pg.parse_gate('') == pg.DEFAULT_GATE


def test_empty_tier_aggregate():
    assert pg._agg({}, []) == {'n': 0, 'hit3': None, 'hit10': None, 'mrr': None}
