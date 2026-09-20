"""
Stream naming for the librarian — librarian.md REQ-01 and open question 1
("the request and response message types and the stream they use follow
`redis-streams.md`'s existing conventions").

Two streams, both instance-wide, both owned by this app:

    aigang:librarian:requests    consumer group "librarian"   (in)
    aigang:librarian:responses   no fixed group               (out)

Three naming decisions, each with a reason the README states in full:

- **Not** ``aigang:gateway:{project}``. ScrumMaster is that stream's
  consumer group and dead-letters kinds it does not know, so a delivery
  request published there would be destroyed rather than delivered.
- **Not** under ``aigang:workitems:``. Delivery is not a work-item command;
  the work-item service's own command consumer would reject it the same way.
- **No ``{project}`` segment.** One librarian serves every repository on the
  machine and a request names its destination repository in the payload, so
  there is nothing for the segment to carry that the payload does not
  already hold — the same reasoning ``artifacts/events.py`` applies to the
  artifact event stream. ``aigang:<domain>`` with the project segment absent
  is the shape ``redis-streams.md``'s topology table leaves for a stream
  that is not per-project.

The dead-letter stream for the request stream is
``aigang:librarian:requests:dead``, which is ``workitems.streams``'
``dead_letter_stream_name`` applied to the name above — this module does not
restate it.
"""

REQUEST_STREAM = 'aigang:librarian:requests'
RESPONSE_STREAM = 'aigang:librarian:responses'

# One stable group, per redis-streams.md's topology table: a restarted
# librarian rejoins it and reclaims its own stale pending entries instead of
# replaying retained history under a fresh group.
REQUEST_GROUP = 'librarian'
