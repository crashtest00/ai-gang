"""The corpus index — strategy/v6.0/v6.1/features/corpus-indexing.md.

    models.py     units (text, class, scope, reference) and the index's own
                  bookkeeping: what each source was indexed from, cursors,
                  pending uploads, specification links, failures (REQ-02,
                  REQ-05, REQ-08, REQ-09)
    reads.py      the owners' read interfaces: Core's enumerations and
                  memory-api's archive reads (§4)
    events.py     the consumer of Core's event streams (REQ-04, REQ-07)
    follow.py     indexing each class, catch-up, rebuild and retry (REQ-01,
                  REQ-03, REQ-04, REQ-07, REQ-09)
    render.py     Core's records rendered to text, each value named by its
                  field in Core's read (REQ-02; Knowledge Retrieval REQ-04)
    sessions.py   a transcript cut into turns (REQ-10)
    chunking.py   a text cut into located units (§4)
    embedding.py  the local embedding model each unit and question is
                  embedded by (REQ-03, REQ-06)
    platform.py   the platform corpus, read at the checkout's commit (REQ-08)
    status.py     the lag signal retrieval consumes (REQ-05)
    store.py      writing units, removals and failures (REQ-02, REQ-07–09)

``manage.py run_indexer`` (the ``memory-indexer`` container) runs it;
``rebuild_index``, ``index_status`` and ``index_retry`` are its operator
commands.

Values recorded at build (the specification leaves each to the build, and
it records them once measured):

    index structure    two per unit, in memory-postgres. PostgreSQL
                       full-text search: a generated ``tsvector``
                       (``english`` configuration) over the text, with a GIN
                       index. And an embedding (``embedding.py``): the
                       text's vectors from a local ONNX model, packed in
                       ``IndexUnit.embedding``, compared in process. No
                       PostgreSQL extension. The weights are baked into the
                       image and never fetched at runtime (I-08)
    embedding model    Alibaba-NLP/gte-modernbert-base at e7f32e3, its 8-bit
                       ONNX export (150 MB), under ONNX Runtime on CPU:
                       768 dimensions, CLS pooling, an 8,192-token input
                       (every unit fits one window; longer text would be
                       cut into overlapping windows, never truncated).
                       Chosen over BAAI/bge-small-en-v1.5 on the frozen
                       paraphrase set through the real route
                       (tests/paraphrase_gate.py): paraphrase hit@3 0.375
                       against 0.225, lexical 0.95 for both. Also tried,
                       outside the route: jina-embeddings-v2-small-en,
                       snowflake-arctic-embed-s and -m-v1.5 (8-bit), and
                       bge-base-en-v1.5
    embedding cost     measured on a 4-vCPU host, no GPU: the frozen set's
                       543 passages embed in 652 s (1.2 s per unit of
                       about 1,200 characters), so a 20,000-unit rebuild
                       takes about 6.5 hours (1.24 s per unit measured over
                       2,000 units, tests/scale_bench.py); route latency
                       645 / 1,182 ms median / p95 over 543 units, and
                       2.84 / 5.19 s over 20,000 synthetic units against
                       2.55 / 4.57 s for full-text search alone on the same
                       corpus and questions (gate 6: +0.29 s median). The
                       full-text half is the floor there (2.32 s median);
                       the embedding half takes 0.19 s once the process
                       holds the vectors; peak resident memory 424 MB (543 units)
                       and 885 MB (20,000). Scale assumption: a project's
                       index of tens of thousands of units, compared in
                       process, with no vector index
    unit size          at most 2,000 characters (chunking.UNIT_MAX_CHARS);
                       a session turn's unit at most 4,000
                       (sessions.TURN_UNIT_MAX_CHARS)
    session unit       one turn: one model response with the results of its
                       tool calls, led by the prompt it answers (REQ-10;
                       sessions.py)
    split rule         an oversized turn is cut at record boundaries, then
                       lines, then whitespace (sessions.py)
    stub threshold     a tool result or tool input above 2,000 characters
                       (sessions.STUB_THRESHOLD_CHARS)
    rendering          sessions.py and render.py
    freshness bound    60 seconds from a record's commit (Core's classes and
                       a deletion), from the archive copy of a run's end
                       record or of a session's transition to quiet
                       (sessions), from a note's archive copy, or from a
                       library file's extraction record, to its units being
                       in the index. A library file's bound from its upload
                       adds the extraction bound (5 minutes, provisional),
                       and a file only the sweep reaches adds the sweep
                       interval (30 minutes) too. Provisional: measured
                       locally against a fake Core (test_index_events.py,
                       at most 5.5 seconds, most of it the 5-second follow
                       interval), not yet on a deployment. A work-item
                       stream that did not exist when the indexer started
                       is joined within 30 seconds
                       (MEMORY_INDEX_STREAM_DISCOVERY_SECONDS)
"""
