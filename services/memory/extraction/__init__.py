"""Artifact text extraction — strategy/v6.0/v6.1/features/artifact-text-extraction.md.

The dispatching half, in Memory (the specification's Open Question 1):

    models.py       the extraction record and the job store (REQ-02, REQ-06,
                    REQ-07), in Memory's PostgreSQL
    core_client.py  Core's artifact reads over HTTP: the record, the bytes
                    and the listing (REQ-01, REQ-04)
    spool.py        the hand-off to the extractor process (REQ-05)
    jobs.py         the stamp protocol, job claiming, resumption and the
                    superseded rule (REQ-02, REQ-03, REQ-07)
    events.py       the `artifact.uploaded` consumer (REQ-03, REQ-04)
    sweep.py        the reconciliation sweep (REQ-04)

``manage.py run_extraction`` (the ``memory-extraction`` container) runs all
of it.

Declared values (REQ-04, REQ-06; the specification's Open Question 2 and the
build brief's I-26), provisional until measured live:

    extraction bound    5 minutes from an upload's commit to its text, for a
                        file within the time limit and no backlog
    sweep interval      30 minutes (MEMORY_EXTRACTION_SWEEP_SECONDS), the
                        detection bound for in-place edits and lost events
    time limit          120 seconds per file
                        (MEMORY_EXTRACTION_TIME_LIMIT_SECONDS)
    attempt limit       3 attempts on the same content that end without a
                        result (MEMORY_EXTRACTION_ATTEMPT_LIMIT) The parsing half is ``services/memory/extractor/``, a separate
process in its own image with no network (REQ-05).
"""
