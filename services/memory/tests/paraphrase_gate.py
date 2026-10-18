"""Track 5's paraphrase gate (build brief §1b, gate 2): the frozen
paraphrase and long-document set, run through Memory's real path and scored
by that set's README rule.

The set is not in this repository: it is the Management repository's
``strategy/v6.0/v6.1/eval-paraphrase/`` (frozen at ``b50a8b8``), whose
passages are Management's own planning documents. This module reads it
from ``MEMORY_PARAPHRASE_SET`` and fails, rather than skips, when that is
not set. It is named outside ``test_*.py``, so the suite does not collect
it: the run embeds 543 long passages, which takes minutes. Run it by name,
under the suite lock:

    MEMORY_PARAPHRASE_SET=<eval-paraphrase dir> [MEMORY_PARAPHRASE_OUT=<file.json>] \\
      flock /tmp/v4-wis-suite.lock timeout 30m ./run_tests.sh -q tests/paraphrase_gate.py -s

The path is the production one: every passage is written by
``index.store.replace_units`` (as the indexer writes a library file's
units, which embeds them), and every question is asked through
``POST /retrieve`` with the route's defaults (``limit`` 10). A question
whose result holds no passage has abstained.

Gate (brief §1b): lexical hit@3 >= 0.95, paraphrase hit@3 >= 0.30, and at
most 2 of the 120 answerable questions wrongly abstained.
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

PLANNING = {'v6-prd', 'v5-prd', 'v4-prd', 'a2a-record', 'delivery-state'}
TIERS = ('lexical', 'paraphrase', 'concept')
GATE = {'lexical_hit3': 0.95, 'paraphrase_hit3': 0.30, 'false_abstentions': 2}
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
    return {'n': n, 'hit3': round(sum(m['hit3'] for m in ms) / n, 3),
            'hit10': round(sum(m['hit10'] for m in ms) / n, 3), 'mrr': round(sum(m['rr'] for m in ms) / n, 3)}


def _p95(values):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]


def score(questions, results) -> dict:
    """README's scoring rule, steps 3–6."""
    answerable = [q for q in questions if q['tier'] != 'none']
    nones = [q for q in questions if q['tier'] == 'none']
    summary = {'overall': _agg(results, answerable)}
    for tier in TIERS:
        in_tier = [q for q in answerable if q['tier'] == tier]
        summary[tier] = _agg(results, in_tier)
        summary[f'{tier}/planning'] = _agg(results, [q for q in in_tier if q['doc'] in PLANNING])
        summary[f'{tier}/research'] = _agg(results, [q for q in in_tier if q['doc'] not in PLANNING])

    def abstained(subset):
        return sum(1 for q in subset if not results[q['qid']])
    summary['abstention'] = {
        'none_all': abstained(nones), 'none_near': abstained([q for q in nones if 'near' in q['fact_id']]),
        'none_para': abstained([q for q in nones if 'para' in q['fact_id']]), 'none_n': len(nones),
        'false_abstentions': abstained(answerable), 'answerable_n': len(answerable),
        'false_abstentions_by_tier': {t: abstained([q for q in answerable if q['tier'] == t]) for t in TIERS}}
    return summary


def test_paraphrase_gate_through_the_route(client, redis_client, closed_core):  # noqa: F811
    directory = os.environ.get('MEMORY_PARAPHRASE_SET')
    assert directory, 'set MEMORY_PARAPHRASE_SET to the frozen eval-paraphrase directory'
    passages = _load(directory, 'passages.jsonl')
    questions = _load(directory, 'questions.jsonl')
    assert len(passages) == 543 and len(questions) == 132

    rss_start = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    embedding.model()  # loaded before the clock starts, as a running indexer has it
    t0 = time.perf_counter()
    for p in passages:
        store.replace_units(f'eval:{p["id"]}', CLASS_LIBRARY, {'eval': p['id']},
                            [{'reference': _reference(p['id'], p['text']), 'text': p['text']}])
    build_seconds = time.perf_counter() - t0
    by_unit = {u.id: u.source_key.split(':', 1)[1] for u in IndexUnit.objects.all()}
    assert len(by_unit) == 543

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

    summary = score(questions, results)
    out = {'model': f'{embedding.MODEL.name}@{embedding.MODEL.revision}', 'summary': summary,
           'index_build_seconds': round(build_seconds, 1),
           'route_latency_ms': {'median': round(statistics.median(latency), 1), 'p95': round(_p95(latency), 1)},
           'peak_rss_mb': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
           'rss_at_start_mb': round(rss_start / 1024, 1), 'per_question': per_question}
    if os.environ.get('MEMORY_PARAPHRASE_OUT'):
        with open(os.environ['MEMORY_PARAPHRASE_OUT'], 'w') as f:
            json.dump(out, f, indent=1)
    print('PARAPHRASE GATE', json.dumps({k: v for k, v in out.items() if k != 'per_question'}, indent=1))

    assert summary['lexical']['hit3'] >= GATE['lexical_hit3'], summary['lexical']
    assert summary['paraphrase']['hit3'] >= GATE['paraphrase_hit3'], summary['paraphrase']
    assert summary['abstention']['false_abstentions'] <= GATE['false_abstentions'], summary['abstention']
