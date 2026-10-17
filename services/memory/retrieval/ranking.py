"""Ranking over the corpus index (Knowledge Retrieval REQ-01, REQ-06,
REQ-09, REQ-10).

The index is PostgreSQL full-text search (``index.models.IndexUnit.search``,
a GIN-indexed ``tsvector``); there is no embedding model, so no weights and
nothing leaves the deployment (REQ-08). A question is natural language, so
it is not required to match in full. Its content words (the ``english``
configuration drops stop words and stems the rest) are matched with OR
semantics, and each matched word counts for how distinctive it is in the
current corpus: its inverse document frequency, the BM25 form

    idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))

A unit's score is the sum of the idf of the question's words it contains,
times its class weight (REQ-10). Its *coverage* is that sum over the idf the
whole question could have earned, counting a word found nowhere in the
corpus at the highest idf. A unit below ``MIN_COVERAGE`` is not a relevant
answer, so a question nothing answers returns nothing (REQ-09's
"nothing relevant exists"), and the same floor applies to units after the
first, relative to the best.

Candidates are the best ``CANDIDATES`` session units and, separately, the
best ``CANDIDATES`` of every other class, so the session weight and cap
act before any candidate limit could drop an authored unit (REQ-10).

Scope is a ``WHERE`` clause of the one query that finds the units, so it is
enforced where the query runs and never by filtering results afterwards
(REQ-06). Units whose ``current`` flag is false (a library file's replaced
text, Corpus Indexing REQ-04) are not candidates.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Optional

from django.db import connection

from index.models import CLASS_SESSION

# Declared defaults, measured against tests/retrieval_evalset.py (REQ-09, REQ-10).
# Per-query overrides exist for the session weight and cap; the rest are the
# ranker's own.
SESSION_WEIGHT = 0.6
MIN_COVERAGE = 0.34
RELATIVE_FLOOR = 0.4
MIN_TERMS = 2
UNSEEN_WEIGHT = 0.5
CANDIDATES = 200


@dataclass(frozen=True)
class Params:
    session_weight: float = SESSION_WEIGHT
    # At most this many session passages in a result; None: no cap.
    session_cap: Optional[int] = None
    min_coverage: float = MIN_COVERAGE
    relative_floor: float = RELATIVE_FLOOR
    # How many distinct question words a unit must contain (fewer if the
    # question has fewer in the corpus), and how much a word found nowhere
    # in the corpus counts toward what a unit could have matched.
    min_terms: int = MIN_TERMS
    unseen_weight: float = UNSEEN_WEIGHT


@dataclass
class Candidate:
    unit_id: int
    source_class: str
    scope: str
    source_key: str
    work_item_id: Optional[str]
    reference: dict
    text: str
    written_at: object
    score: float
    coverage: float
    terms: int


def _quote(lexeme: str) -> str:
    return "'" + lexeme.replace('\\', '\\\\').replace("'", "''") + "'"


def question_terms(question: str) -> list[str]:
    """The question's stemmed content words, in the index's own tokenization."""
    with connection.cursor() as cur:
        cur.execute("SELECT DISTINCT lexeme FROM unnest(to_tsvector('english', %s)) ORDER BY lexeme", [question])
        return [row[0] for row in cur.fetchall()]


def _document_frequencies(terms: list[str]) -> tuple[int, dict]:
    with connection.cursor() as cur:
        cur.execute('SELECT count(*) FROM index_unit WHERE current')
        total = cur.fetchone()[0]
        dfs = {}
        for term in terms:
            cur.execute("SELECT count(*) FROM index_unit WHERE current AND search @@ to_tsquery('simple', %s)",
                        [_quote(term)])
            dfs[term] = cur.fetchone()[0]
    return total, dfs


def idf(total: int, df: int) -> float:
    return math.log(1 + (total - df + 0.5) / (df + 0.5))


def rank(question: str, *, scope: Optional[str] = None, limit: int = 10,
         params: Params = Params()) -> list[Candidate]:
    """The units most relevant to ``question``, best first, at most
    ``limit``. ``scope`` is ``project``, ``platform`` or None for both; the
    caller has validated it."""
    terms = question_terms(question)
    if not terms:
        return []
    total, dfs = _document_frequencies(terms)
    weights = {t: idf(total, df) for t, df in dfs.items()}
    present = [t for t in terms if dfs[t] > 0]
    possible = sum(w for t, w in weights.items() if dfs[t] > 0) + params.unseen_weight * sum(
        w for t, w in weights.items() if dfs[t] == 0)
    if not present or possible <= 0:
        return []

    match_sql = ' + '.join("%s * (search @@ to_tsquery('simple', %s))::int" for _ in present)
    match_args = [v for t in present for v in (weights[t], _quote(t))]
    count_sql = ' + '.join("(search @@ to_tsquery('simple', %s))::int" for _ in present)
    match_args += [_quote(t) for t in present]
    # REQ-10: the candidate window is taken per weight class, so session
    # units cannot fill it and cut an authored unit before the class weight
    # and the session cap are applied. Within one partition every unit has
    # the same weight, so its best CANDIDATES by ``matched`` are exactly its
    # best CANDIDATES under any weighting.
    inner = (f'SELECT id, source_class, scope, source_key, work_item_id, reference, text, written_at, '
             f'({match_sql}) AS matched, ({count_sql}) AS terms FROM index_unit '
             "WHERE current AND search @@ to_tsquery('simple', %s) ")
    args = match_args + [' | '.join(_quote(t) for t in present)]
    if scope is not None:
        inner += 'AND scope = %s '
        args.append(scope)
    sql = ('SELECT id, source_class, scope, source_key, work_item_id, reference, text, written_at, matched, terms '
           'FROM (SELECT *, row_number() OVER (PARTITION BY source_class = %s ORDER BY matched DESC, id) AS place '
           f'FROM ({inner}) AS found) AS windowed WHERE place <= %s ORDER BY matched DESC, id')
    # Placeholders in text order: the partition's class, the inner query's, the window.
    args = [CLASS_SESSION] + args + [CANDIDATES]
    with connection.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()

    found = []
    for (unit_id, source_class, unit_scope, source_key, work_item_id, reference, text, written_at,
         matched, matched_terms) in rows:
        weight = params.session_weight if source_class == CLASS_SESSION else 1.0
        matched = float(matched)
        coverage = matched / possible
        found.append(Candidate(unit_id, source_class, unit_scope, source_key,
                               str(work_item_id) if work_item_id else None,
                               json.loads(reference) if isinstance(reference, str) else reference, text, written_at,
                               score=matched * weight, coverage=coverage, terms=matched_terms))
    needed = min(params.min_terms, len(present))
    found = [c for c in found if c.coverage >= params.min_coverage and c.terms >= needed]
    found.sort(key=lambda c: (-c.score, c.unit_id))
    if not found:
        return []
    best = found[0].score
    found = [c for c in found if c.score >= best * params.relative_floor]

    out, sessions = [], 0
    for candidate in found:
        if candidate.source_class == CLASS_SESSION:
            if params.session_cap is not None and sessions >= params.session_cap:
                continue
            sessions += 1
        out.append(candidate)
        if len(out) == limit:
            break
    return out
