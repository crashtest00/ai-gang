"""Track 5's cost measurements (build brief §1b, gate 4): index build time
and query latency at a synthetic 20,000-unit scale. Not collected by the
suite (it runs for many minutes); run it by name, under the suite lock:

    MEMORY_PARAPHRASE_SET=<eval-paraphrase dir> [MEMORY_BENCH_EMBEDDED=2000] \\
      flock /tmp/v4-wis-suite.lock timeout 90m ./run_tests.sh -q tests/scale_bench.py -s

1. **Build.** ``MEMORY_BENCH_EMBEDDED`` units (default 2,000) are written
   through ``index.store.replace_units``, the indexer's own write, which
   embeds each: real build cost per unit. Their texts are the frozen set's
   long passages, cycled, each made distinct by a numbered first line.
2. **Scale.** The index is then filled to 20,000 units by copying those
   rows (text and embedding) under new source keys, a third of them in the
   ``session`` class, so the query path reads 20,000 units' vectors without
   embedding 20,000 texts.
3. **Query.** 50 of the set's questions are asked through ``POST /retrieve``
   with the route's defaults; median and p95 latency are reported, with the
   process's peak resident memory. They are asked again with the route's
   ranker replaced by full-text search alone (its relevance floor, no
   embedding): the baseline gate 6 compares the hybrid against (median at
   most 0.5 s above it). Each question's time in the full-text and the
   embedding halves of the ranker is reported too.
"""

import json
import os
import resource
import statistics
import time
import uuid

import pytest

from index import embedding, store
from index.models import CLASS_LIBRARY, CLASS_SESSION, IndexUnit
from tests.index_support import redis_client  # noqa: F401 - fixture
from tests.retrieval_support import ask, closed_core  # noqa: F401 - fixture

pytestmark = pytest.mark.django_db(transaction=True)

TOTAL = 20_000
NS = uuid.UUID('6c2c3f4e-0f2e-4b9a-9d41-2d6d6c3a7e10')


def _load(directory, name):
    with open(os.path.join(directory, name)) as f:
        return [json.loads(line) for line in f if line.strip()]


def _library_reference(key, text):
    return {'artifactId': str(uuid.uuid5(NS, key)),
            'stamp': {'updatedAt': '2026-10-01T00:00:00+00:00', 'sha256': 'e' * 64},
            'lineStart': 1, 'lineEnd': 1, 'offset': 0, 'end': len(text)}


def _session_reference(key, text):
    ident = str(uuid.uuid5(NS, key))
    return {'sessionId': ident, 'file': f'sessions/{ident}.jsonl', 'turn': 1, 'lines': [1],
            'lineStart': 1, 'lineEnd': 1, 'offset': 0, 'end': len(text)}


def _p95(values):
    values = sorted(values)
    return values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]


def test_scale(client, redis_client, closed_core):  # noqa: F811
    directory = os.environ.get('MEMORY_PARAPHRASE_SET')
    assert directory, 'set MEMORY_PARAPHRASE_SET to the frozen eval-paraphrase directory'
    passages = _load(directory, 'passages.jsonl')
    questions = [q for q in _load(directory, 'questions.jsonl')][:50]
    embedded = int(os.environ.get('MEMORY_BENCH_EMBEDDED', '2000'))

    embedding.model()
    texts = [f'Unit {n}.\n' + passages[n % len(passages)]['text'] for n in range(embedded)]
    t0 = time.perf_counter()
    for n, text in enumerate(texts):
        key = f'bench:{n}'
        store.replace_units(key, CLASS_LIBRARY, {'bench': n},
                            [{'reference': _library_reference(key, text), 'text': text}])
    build_seconds = time.perf_counter() - t0
    chars = sum(len(t) for t in texts)

    seeds = list(IndexUnit.objects.order_by('id').values_list('text', 'embedding'))
    rows = []
    from django.utils import timezone
    now = timezone.now()
    for n in range(embedded, TOTAL):
        text, vector = seeds[n % len(seeds)]
        key = f'bench:{n}'
        session = n % 3 == 0
        rows.append(IndexUnit(source_class=CLASS_SESSION if session else CLASS_LIBRARY, scope='project',
                              source_key=key, text=text, embedding=bytes(vector), indexed_at=now,
                              reference=(_session_reference if session else _library_reference)(key, text)))
    IndexUnit.objects.bulk_create(rows, batch_size=2000)
    assert IndexUnit.objects.count() == TOTAL

    ask(client, 'warm up query text', limit=10)
    latency = []
    for q in questions:
        t1 = time.perf_counter()
        status, body = ask(client, q['question'], limit=10)
        latency.append((time.perf_counter() - t1) * 1000)
        assert status == 200, body

    from retrieval import ranking, service

    halves = {'lexical': [], 'embedding': []}
    for q in questions:
        t1 = time.perf_counter()
        ranking._lexical(q['question'], None, ranking.Params())
        halves['lexical'].append((time.perf_counter() - t1) * 1000)
        t1 = time.perf_counter()
        ranking._similar(q['question'], None, 20)
        halves['embedding'].append((time.perf_counter() - t1) * 1000)

    def full_text_only(question, *, scope=None, limit=10, params=ranking.Params()):
        found, present = ranking._lexical(question, scope, params)
        return ranking._floored(found, present, params)[:limit]

    real = service.ranking.rank
    service.ranking.rank = full_text_only
    baseline = []
    try:
        for q in questions:
            t1 = time.perf_counter()
            status, body = ask(client, q['question'], limit=10)
            baseline.append((time.perf_counter() - t1) * 1000)
            assert status == 200, body
    finally:
        service.ranking.rank = real

    out = {
        'model': f'{embedding.MODEL.name}@{embedding.MODEL.revision}',
        'embedded_units': embedded, 'embedded_chars_mean': round(chars / embedded),
        'build_seconds': round(build_seconds, 1), 'build_seconds_per_unit': round(build_seconds / embedded, 3),
        'build_seconds_extrapolated_to_20000': round(build_seconds / embedded * TOTAL),
        'units_in_index': TOTAL, 'queries': len(latency),
        'route_latency_ms': {'median': round(statistics.median(latency), 1), 'p95': round(_p95(latency), 1)},
        'route_latency_full_text_only_ms': {'median': round(statistics.median(baseline), 1),
                                            'p95': round(_p95(baseline), 1)},
        'ranker_halves_ms': {k: {'median': round(statistics.median(v), 1), 'p95': round(_p95(v), 1)}
                             for k, v in halves.items()},
        'peak_rss_mb': round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
    }
    print('SCALE BENCH', json.dumps(out, indent=1))
