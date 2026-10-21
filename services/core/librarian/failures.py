"""
The failure vocabulary — librarian.md REQ-06.

"Each request MUST receive one response: a confirmation with the delivered
path, or a failure naming its reason — unknown artifact id, unknown
destination repository, a ``requested_path`` that does not resolve inside
that repository, or a copy that failed."

Those four, plus the one REQ-01's acceptance adds ("a request missing a
required field is answered with a failure naming the missing field"), are
the whole list. Each is a stable machine-readable code on the response's
``reason`` field; the human sentence goes in ``detail``.
"""

from __future__ import annotations

# REQ-01's acceptance: the failure names the missing field, which `detail`
# carries.
MISSING_FIELD = 'missing_field'

# REQ-06's four.
UNKNOWN_ARTIFACT = 'unknown_artifact'
UNKNOWN_DESTINATION_REPO = 'unknown_destination_repo'
PATH_OUTSIDE_REPOSITORY = 'path_outside_repository'
COPY_FAILED = 'copy_failed'

REASONS = frozenset({
    MISSING_FIELD, UNKNOWN_ARTIFACT, UNKNOWN_DESTINATION_REPO, PATH_OUTSIDE_REPOSITORY, COPY_FAILED,
})


class DeliveryFailure(Exception):
    """A request that cannot be satisfied, carrying the reason the response
    will name. Raised by the resolution path and turned into exactly one
    failure response by ``consumer.py``."""

    def __init__(self, reason: str, detail: str):
        if reason not in REASONS:
            raise ValueError(f'unknown failure reason {reason!r}')
        super().__init__(f'{reason}: {detail}')
        self.reason = reason
        self.detail = detail
