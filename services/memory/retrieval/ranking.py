"""Ranking over the corpus index (Knowledge Retrieval REQ-01, REQ-06,
REQ-08, REQ-09, REQ-10): two rankings of the same units, fused.

**Full-text search.** The index keeps a PostgreSQL ``tsvector`` per unit
(``index.models.IndexUnit.search``, GIN-indexed). A question is natural
language, so it is not required to match in full. Its content words (the
``english`` configuration drops stop words and stems the rest) are matched
with OR semantics, and each matched word counts for how distinctive it is
in the current corpus: its inverse document frequency, the BM25 form

    idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))

A unit's lexical score is the sum of the idf of the question's words it
contains. Its *coverage* is that sum over the idf the whole question could
have earned, counting a word found nowhere in the corpus at
``UNSEEN_WEIGHT`` of the highest idf. The relevance floor (``_floored``)
keeps a unit only if its coverage reaches ``MIN_COVERAGE``, it holds
``MIN_TERMS`` of the question's words, and its weighted score is within
``RELATIVE_FLOOR`` of the best.

**Embeddings.** The index also keeps each unit's vectors from a local model
(``index.embedding``; nothing leaves the deployment, REQ-08). The question
is embedded by the same model, and a unit's similarity is the cosine of its
best window, computed in process over the units in scope (``_similar``).
Each unit's vector is read from the database once per process and kept
(``_VectorCache``), since a unit's embedding never changes.

**Fusion.** The two rankings, each cut to its best ``FUSION_DEPTH`` units
(more if the caller asks for more), are fused by reciprocal rank fusion:
a unit scores ``sum 1 / (RRF_K + rank)`` over the rankings it is in. The
lexical ranking used here has its floor off, so it contributes recall; the
floor decides abstention instead. A question is answered with nothing
("nothing relevant exists", REQ-09) only when full-text search with its
floor finds nothing *and* no unit's similarity reaches
``EMBEDDING_CUTOFF``, which also bounds the embedding ranking: a unit below
it is no embedding match. When the floor finds nothing but a unit's similarity
does reach the cutoff, the question is answered from the embedding ranking
alone: the lexical matches the floor rejected are not fused (track 5's
recorded choice; on the frozen paraphrase set, fusing them halved the
paraphrase hits).

**Session weighting (REQ-10)** is applied after fusion: a session unit's
fused score is multiplied by the session weight, and the session cap is
applied to the result. Before fusion, each ranking takes its best units of
the session class and, separately, of every other class, and a unit's place
in a ranking is counted within its own class, so session units can neither
fill a ranking nor push an authored unit's place down, however many match.
At an equal fused score, an authored unit comes first.

Scope is a ``WHERE`` clause of each query that finds units, so it is
enforced where the query runs and never by filtering results afterwards
(REQ-06). Units whose ``current`` flag is false (a library file's replaced
text, Corpus Indexing REQ-04) are not candidates.

Scale: similarity reads every in-scope unit's vectors per question, with no
vector index and no PostgreSQL extension. That is sized for a project's
index of tens of thousands of units (measured at 20,000; see
``index/__init__.py``), not millions.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np
from django.db import connection

from index import embedding
from index.models import CLASS_SESSION

# Declared defaults, measured against tests/retrieval_evalset.py (REQ-09, REQ-10).
# Per-query overrides exist for the session weight and cap; the rest are the
# ranker's own. SESSION_WEIGHT multiplies a session unit's fused score
# (track 5). What keeps session volume from crowding out authored sources
# is that a unit's place in each ranking is counted within its own class,
# and that at an equal fused score an authored unit comes first; on top of
# that, every weight below 1.0 tried (0.5, 0.6, 0.7, 0.8, 0.9) pushed a
# session's own best answer out of the top three on REQ-09's set, because
# fused scores lie close together (1/61, 1/62, ...). So the default is 1.0;
# a request may still lower, raise or cap it.
SESSION_WEIGHT = 1.0
MIN_COVERAGE = 0.34
RELATIVE_FLOOR = 0.4
MIN_TERMS = 2
UNSEEN_WEIGHT = 0.5
CANDIDATES = 200
# Fusion (build brief §1b): reciprocal rank fusion with k = 60, as evaluated
# (strategy/v6.0/v6.1/eval-paraphrase/), over each ranking's best
# FUSION_DEPTH units. EMBEDDING_CUTOFF is the cosine below which an
# embedding match alone does not answer a question. It is set on REQ-09's
# evaluation set, never on the paraphrase set: the smallest two-place value
# above the top similarity of every REQ-09 no-answer question full-text
# search abstains on (the highest is 0.624), so no question that set says
# nothing answers is answered by the embedding alone.
RRF_K = 60
FUSION_DEPTH = 10
EMBEDDING_CUTOFF = 0.63


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
    # A question full-text search's floor rejects is still answered when a
    # unit's cosine similarity reaches this.
    embedding_cutoff: float = EMBEDDING_CUTOFF


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
    similarity: Optional[float] = None


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


def _lexical(question: str, scope: Optional[str], params: Params) -> tuple[list[Candidate], int]:
    """Full-text search's candidates, unweighted and unfloored, best first,
    and how many of the question's words the corpus holds. Each candidate's
    ``score`` is its idf sum."""
    terms = question_terms(question)
    if not terms:
        return [], 0
    total, dfs = _document_frequencies(terms)
    weights = {t: idf(total, df) for t, df in dfs.items()}
    present = [t for t in terms if dfs[t] > 0]
    possible = sum(w for t, w in weights.items() if dfs[t] > 0) + params.unseen_weight * sum(
        w for t, w in weights.items() if dfs[t] == 0)
    if not present or possible <= 0:
        return [], 0

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
        matched = float(matched)
        found.append(_candidate(unit_id, source_class, unit_scope, source_key, work_item_id, reference, text,
                                written_at, score=matched, coverage=matched / possible, terms=matched_terms))
    return found, len(present)


def _candidate(unit_id, source_class, unit_scope, source_key, work_item_id, reference, text, written_at,
               **scores) -> Candidate:
    return Candidate(unit_id, source_class, unit_scope, source_key, str(work_item_id) if work_item_id else None,
                     json.loads(reference) if isinstance(reference, str) else reference, text, written_at,
                     **scores)


def _weight(candidate: Candidate, params: Params) -> float:
    return params.session_weight if candidate.source_class == CLASS_SESSION else 1.0


def _floored(found: list[Candidate], present: int, params: Params) -> list[Candidate]:
    """Full-text search with its relevance floor: what it alone would answer."""
    if not found:
        return []
    needed = min(params.min_terms, present)
    kept = [c for c in found if c.coverage >= params.min_coverage and c.terms >= needed]
    kept.sort(key=lambda c: (-c.score * _weight(c, params), c.unit_id))
    if not kept:
        return []
    best = kept[0].score * _weight(kept[0], params)
    return [c for c in kept if c.score * _weight(c, params) >= best * params.relative_floor]


class _VectorCache:
    """Unit vectors already read, by unit id and row version (``xmin``,
    which any rewrite of the row changes). A unit's
    embedding is written once, with the unit, and never changed: new text
    is a new unit (``index.store.replace_units``). So a cached vector is
    always the unit's own, and a question reads only the vectors of units
    written since the last question, not every unit's. Which units are
    candidates is still decided by each question's own query, with its scope
    (REQ-06); the cache only spares reading their vectors again."""

    def __init__(self):
        self.vectors: dict = {}
        self.lock = threading.Lock()
        self.last: tuple = ((), None)

    def matrix(self, keys: list):
        """The stacked window vectors of ``keys`` that are still in the
        index, the row each unit's windows start at, and which keys those
        are."""
        with self.lock:
            if self.last[0] == tuple(keys):
                return self.last[1]
            missing = [k for k in keys if k not in self.vectors]
            for start in range(0, len(missing), 2000):
                chunk = missing[start:start + 2000]
                with connection.cursor() as cur:
                    cur.execute('SELECT id, xmin::text, embedding FROM index_unit WHERE id = ANY(%s)',
                                [[unit_id for unit_id, _ in chunk]])
                    for unit_id, version, blob in cur.fetchall():
                        self.vectors[(unit_id, version)] = embedding.unpack(blob)
            if len(self.vectors) > 2 * max(len(keys), 1000):   # drop units gone from the index
                wanted = set(keys)
                self.vectors = {k: v for k, v in self.vectors.items() if k in wanted}
            # A unit removed between the two reads has no vector: it is no candidate.
            present = [k for k in keys if k in self.vectors]
            blocks = [self.vectors[k] for k in present]
            stacked = None
            if blocks:
                starts = np.cumsum([0] + [len(b) for b in blocks[:-1]])
                stacked = (np.concatenate(blocks), starts, present)
            self.last = (tuple(keys), stacked)
            return stacked


_vectors = _VectorCache()


def _similar(question: str, scope: Optional[str], depth: int) -> list[tuple[int, float, bool]]:
    """The units most similar to the question by embedding, as ``(unit id,
    cosine, is a session unit)``, best first: the best ``depth`` session
    units and the best ``depth`` of every other class (REQ-10)."""
    query = embedding.embed_query(question)
    sql = 'SELECT id, source_class, xmin::text FROM index_unit WHERE current '
    args = []
    if scope is not None:
        sql += 'AND scope = %s '
        args.append(scope)
    sql += 'ORDER BY id'
    with connection.cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    classes = {(row[0], row[2]): row[1] for row in rows}
    stacked = _vectors.matrix([(row[0], row[2]) for row in rows])
    if stacked is None:
        return []
    vectors, starts, present = stacked
    sims = np.maximum.reduceat(vectors @ query, starts)
    rows = [(key[0], classes[key]) for key in present]
    ids = np.array([row[0] for row in rows])
    session = np.array([row[1] == CLASS_SESSION for row in rows])
    best = []
    for part in (session, ~session):
        where = np.flatnonzero(part)
        order = where[np.lexsort((ids[where], -sims[where]))][:depth]
        best.extend((int(ids[i]), float(sims[i]), bool(session[i])) for i in order)
    best.sort(key=lambda found: (-found[1], found[0]))
    return best


def _best_per_partition(found: list[Candidate], depth: int) -> list[Candidate]:
    sessions = [c for c in found if c.source_class == CLASS_SESSION][:depth]
    others = [c for c in found if c.source_class != CLASS_SESSION][:depth]
    return sorted(sessions + others, key=lambda c: (-c.score, c.unit_id))


def _units(ids: list[int], scope: Optional[str]) -> dict:
    sql = ('SELECT id, source_class, scope, source_key, work_item_id, reference, text, written_at '
           'FROM index_unit WHERE current AND id = ANY(%s) ')
    args: list = [ids]
    if scope is not None:
        sql += 'AND scope = %s '
        args.append(scope)
    with connection.cursor() as cur:
        cur.execute(sql, args)
        return {row[0]: row for row in cur.fetchall()}


def rank(question: str, *, scope: Optional[str] = None, limit: int = 10,
         params: Params = Params()) -> list[Candidate]:
    """The units most relevant to ``question``, best first, at most
    ``limit``. ``scope`` is ``project``, ``platform`` or None for both; the
    caller has validated it."""
    depth = max(FUSION_DEPTH, limit)
    found, present = _lexical(question, scope, params)
    similar = _similar(question, scope, depth)
    top_similarity = max((sim for _, sim, _ in similar), default=None)
    lexical_answers = bool(_floored(found, present, params))
    if not lexical_answers and (top_similarity is None or top_similarity < params.embedding_cutoff):
        return []

    # When the floor finds no relevant lexical match, every unit full-text
    # search matched shares only words its floor judged incidental: fused,
    # they would only push the embedding's matches down. Such a question is
    # answered by the embedding ranking alone.
    lexical = _best_per_partition(found, depth) if lexical_answers else []
    # The embedding ranking holds only units whose similarity reaches the
    # cutoff, as full-text search's holds only units sharing a word with the
    # question: a unit similar to nothing asked is not a match, and without
    # this every answered question would be padded to its limit with them.
    similar = [found_unit for found_unit in similar if found_unit[1] >= params.embedding_cutoff]
    # REQ-10: a unit's place in each ranking is counted among its own weight
    # class (session units among session units, the rest among the rest), so
    # however many session units match, an authored unit's place, and so its
    # fused score, is what it would be without them.
    fused: dict = {}
    for ranking in ([(c.unit_id, c.source_class == CLASS_SESSION) for c in lexical],
                    [(unit_id, session) for unit_id, _, session in similar]):
        places = {True: 0, False: 0}
        for unit_id, session in ranking:
            places[session] += 1
            fused[unit_id] = fused.get(unit_id, 0.0) + 1.0 / (RRF_K + places[session])
    similarity = {unit_id: sim for unit_id, sim, _ in similar}
    by_id = {c.unit_id: c for c in lexical}
    missing = [unit_id for unit_id in fused if unit_id not in by_id]
    for unit_id, row in _units(missing, scope).items():
        by_id[unit_id] = _candidate(*row, score=0.0, coverage=0.0, terms=0)

    candidates = []
    for unit_id, score in fused.items():
        candidate = by_id.get(unit_id)
        if candidate is None:  # removed between the two reads
            continue
        candidate.similarity = similarity.get(unit_id)
        candidate.score = score * _weight(candidate, params)
        candidates.append(candidate)
    embedding_place = {unit_id: place for place, (unit_id, _, _) in enumerate(similar)}
    # At an equal fused score an authored unit comes first (REQ-10).
    candidates.sort(key=lambda c: (-c.score, c.source_class == CLASS_SESSION,
                                   embedding_place.get(c.unit_id, len(similar)), c.unit_id))

    out, sessions = [], 0
    for candidate in candidates:
        if candidate.source_class == CLASS_SESSION:
            if params.session_cap is not None and sessions >= params.session_cap:
                continue
            sessions += 1
        out.append(candidate)
        if len(out) == limit:
            break
    return out
