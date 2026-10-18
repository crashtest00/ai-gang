"""Track 5's paraphrase gate (build brief §1b, gates 2 and 7): a frozen
paraphrase and long-document set, run through Memory's real path and scored
by that set's README rule.

The default set is not in this repository: it is the Management repository's
``strategy/v6.0/v6.1/eval-paraphrase/`` (frozen at ``b50a8b8``), whose
passages are Management's own planning documents. This module reads a set
(``passages.jsonl`` and ``questions.jsonl``) from ``MEMORY_PARAPHRASE_SET``
and fails, rather than skips, when that is not set. It is named outside
``test_*.py``, so the suite does not collect it: a run embeds hundreds of
long passages, which takes minutes. Run it by name, under the suite lock:

    MEMORY_PARAPHRASE_SET=<set dir> [MEMORY_PARAPHRASE_OUT=<file.json>] \\
      flock /tmp/v4-wis-suite.lock timeout 30m ./run_tests.sh -q tests/paraphrase_gate.py -s

It accepts any set of that shape. Passage and question counts are read from
the files; tiers are every tier in the questions except ``none``, in a stable
order (lexical, paraphrase, concept first, then others alphabetically).
Optional environment variables:

- ``MEMORY_PARAPHRASE_EXPECT=<passages>,<questions>``: fail if the counts differ
  (the frozen set is ``543,132``).
- ``MEMORY_PARAPHRASE_TIERS=a,b,c``: score exactly these tiers.
- ``MEMORY_PARAPHRASE_GATE=lexical=0.95,paraphrase=0.30,false_abstentions=2``:
  override thresholds. ``<tier>=<x>`` is that tier's minimum hit@3 and
  ``false_abstentions=<n>`` the most answerable questions allowed to abstain;
  the values shown are the defaults, and keys given replace those defaults only
  for the keys named. A threshold naming a tier the set lacks fails the gate.
- ``MEMORY_PARAPHRASE_PLANNING=doc1,doc2``: question ``doc`` ids counted as
  planning in the ``<tier>/planning`` and ``<tier>/research`` splits. The set
  cannot say which documents are planning, so the default is the frozen set's
  list (``DEFAULT_PLANNING``). The split is reported only when the questions
  carry ``doc``; without ``fact_id`` the near/para abstention counts are 0.

The path is the production one: every passage is written by
``index.store.replace_units`` (as the indexer writes a library file's
units, which embeds them), and every question is asked through
``POST /retrieve`` with the route's defaults (``limit`` 10). A question
whose result holds no passage has abstained.

Gate (brief §1b), by default: lexical hit@3 >= 0.95, paraphrase hit@3 >= 0.30,
and at most 2 answerable questions wrongly abstained.
"""

import json
import os
import resource
import statistics
import time
import uuid

import pytest

from index import embedding, store
from index.models import CLASS_LIBRARY, IndexUnit
from tests.index_support import redis_client  # noqa: F401 - fixture
from tests.retrieval_support import ask, closed_core  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db(transaction=True)

DEFAULT_PLANNING = frozenset({'v6-prd', 'v5-prd', 'v4-prd', 'a2a-record', 'delivery-state'})
DEFAULT_GATE = {'lexical': 0.95, 'paraphrase': 0.30, 'false_abstentions': 2}
_TIER_ORDER = ('lexical', 'paraphrase', 'concept')
NS = uuid.UUID('6c2c3f4e-0f2e-4b9a-9d41-2d6d6c3a7e10')


def _load(directory, name):
    with open(os.path.join(directory, name)) as f:
        return [json.loads(line) for line in f if line.strip()]


def _reference(pid, text):
    return {'artifactId': str(uuid.uuid5(NS, pid)),
            'stamp': {'updatedAt': '2026-10-01T00:00:00+00:00', 'sha256': 'e' * 64},
            'lineStart': 1, 'lineEnd': 1, 'offset': 0, 'end': len(text)}


def _metrics(ranked, answers):
    pos = next((i + 1 for i, p in enumerate(ranked[:10]) if p in answers), None)
    return {'hit3': int(pos is not None and pos <= 3), 'hit10': int(pos is not None),
            'rr': 1.0 / pos if pos else 0.0, 'rank': pos}


def _agg(results, subset):
    ms = [_metrics(results[q['qid']], set(q['answers'])) for q in subset]
    n = len(ms)
    if not n:
        return {'n': 0, 'hit3': None, 'hit10': None, 'mrr': None}
    return {'n': n, 'hit3': round(sum(m['hit3'] for m in ms) / n, 3),
            'hit10': round(sum(m['hit10'] for m in ms) / n, 3), 'mrr': round(sum(m['rr'] for m in ms) / n, 3)}


def _p95(values):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]


def tiers_of(questions):
    """Every tier but 'none', the usual three first, the rest alphabetical."""
    found = {q['tier'] for q in questions} - {'none'}
    return sorted(found, key=lambda t: (_TIER_ORDER.index(t) if t in _TIER_ORDER else len(_TIER_ORDER), t))


def score(questions, results, tiers=None, planning=DEFAULT_PLANNING) -> dict:
    """README's scoring rule, steps 3-6, for any tier list."""
    tiers = list(tiers) if tiers is not None else tiers_of(questions)
    answerable = [q for q in questions if q['tier'] != 'none']
    nones = [q for q in questions if q['tier'] == 'none']
    summary = {'overall': _agg(results, answerable)}
    split = any('doc' in q for q in questions)
    for tier in tiers:
        in_tier = [q for q in answerable if q['tier'] == tier]
        summary[tier] = _agg(results, in_tier)
        if split:
            summary[f'{tier}/planning'] = _agg(results, [q for q in in_tier if q.get('doc') in planning])
            summary[f'{tier}/research'] = _agg(results, [q for q in in_tier if q.get('doc') not in planning])

    def abstained(subset):
        return sum(1 for q in subset if not results[q['qid']])

    def fact(q):
        return q.get('fact_id', '')
    summary['abstention'] = {
        'none_all': abstained(nones), 'none_near': abstained([q for q in nones if 'near' in fact(q)]),
        'none_para': abstained([q for q in nones if 'para' in fact(q)]), 'none_n': len(nones),
        'false_abstentions': abstained(answerable), 'answerable_n': len(answerable),
        'false_abstentions_by_tier': {t: abstained([q for q in answerable if q['tier'] == t]) for t in tiers}}
    return summary


def parse_gate(text=None):
    """DEFAULT_GATE with the ``key=value,...`` overrides in ``text`` applied."""
    gate = dict(DEFAULT_GATE)
    for part in (text or '').split(','):
        if part.strip():
            key, _, value = part.partition('=')
            gate[key.strip()] = float(value)
    return gate


def gate_failures(summary, gate) -> list:
    """What the gate rejects, as messages; empty means pass. A threshold naming a
    tier the set lacks (or that has no questions) is itself a failure."""
    failures = []
    for key, limit in gate.items():
        if key == 'false_abstentions':
            if summary['abstention']['false_abstentions'] > limit:
                failures.append(f'false abstentions {summary["abstention"]} exceed {limit}')
        elif key not in summary or not summary[key]['n']:
            failures.append(f'gate names tier {key!r}, which the set has no questions for')
        elif summary[key]['hit3'] < limit:
            failures.append(f'{key} hit@3 {summary[key]} is below {limit}')
    return failures


def test_paraphrase_gate_through_the_route(client, redis_client, closed_core):  # noqa: F811
    directory = os.environ.get('MEMORY_PARAPHRASE_SET')
    assert directory, 'set MEMORY_PARAPHRASE_SET to the frozen eval-paraphrase directory'
    passages = _load(directory, 'passages.jsonl')
    questions = _load(directory, 'questions.jsonl')
    expect = os.environ.get('MEMORY_PARAPHRASE_EXPECT')
    if expect:
        want = tuple(int(x) for x in expect.split(','))
        assert (len(passages), len(questions)) == want, (len(passages), len(questions), want)
    tiers = [t for t in os.environ.get('MEMORY_PARAPHRASE_TIERS', '').split(',') if t] or tiers_of(questions)
    planning = frozenset(t for t in os.environ.get('MEMORY_PARAPHRASE_PLANNING', '').split(',') if t) or DEFAULT_PLANNING

    rss_start = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    embedding.model()  # loaded before the clock starts, as a running indexer has it
    t0 = time.perf_counter()
    for p in passages:
        store.replace_units(f'eval:{p["id"]}', CLASS_LIBRARY, {'eval': p['id']},
                            [{'reference': _reference(p['id'], p['text']), 'text': p['text']}])
    build_seconds = time.perf_counter() - t0
    by_unit = {u.id: u.source_key.split(':', 1)[1] for u in IndexUnit.objects.all()}
    assert len(by_unit) == len(passages)

    ask(client, 'warm up query text', limit=10)
    results, latency, per_question = {}, [], {}
    for q in questions:
        t1 = time.perf_counter()
        status, body = ask(client, q['question'], limit=10)
        latency.append((time.perf_counter() - t1) * 1000)
        assert status == 200, body
        results[q['qid']] = [by_unit[p['unitId']] for p in body['passages']]
        per_question[q['qid']] = {'tier': q['tier'], 'ranked': results[q['qid']],
                                  'rank': _metrics(results[q['qid']], set(q['answers']))['rank']}

    summary = score(questions, results, tiers, planning)
    out = {'model': f'{embedding.MODEL.name}@{embedding.MODEL.revision}', 'summary': summary,
           'index_build_seconds': round(build_seconds, 1),
           'route_latency_ms': {'median': round(statistics.median(latency), 1), 'p95': round(_p95(latency), 1)},
           'peak_rss_mb': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
           'rss_at_start_mb': round(rss_start / 1024, 1), 'per_question': per_question}
    if os.environ.get('MEMORY_PARAPHRASE_OUT'):
        with open(os.environ['MEMORY_PARAPHRASE_OUT'], 'w') as f:
            json.dump(out, f, indent=1)
    print('PARAPHRASE GATE', json.dumps({k: v for k, v in out.items() if k != 'per_question'}, indent=1))

    failures = gate_failures(summary, parse_gate(os.environ.get('MEMORY_PARAPHRASE_GATE')))
    assert not failures, failures
