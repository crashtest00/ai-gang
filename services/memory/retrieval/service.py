"""Answering a question (Knowledge Retrieval REQ-01 … REQ-07).

``answer`` is what the ``memory-api`` route calls: validate the request,
rank within the declared scope (``ranking.rank``), mark each passage derived
with its age and source status (``sources``), say whether the index may be
incomplete (``index.status.index_status``, REQ-07), and record the query and
exactly what was returned (``QueryLog``, REQ-03).

Retrieval writes nothing to the corpus (REQ-02): the only write is the
query-log row, a table the index never reads.
"""

from __future__ import annotations

from typing import Optional

from django.db import transaction
from django.utils import timezone

from index.models import SCOPES
from index.status import index_status

from . import ranking, sources
from .models import QueryLog

DEFAULT_LIMIT = 10
MAX_LIMIT = 50

NOTICE = ('Each passage is derived from a source record and is not authoritative. Where a passage and the '
          'source record disagree, the source record governs: check sourceStatus, and read the source '
          'through source.read before acting on a passage.')

# The fields a request may carry. There is deliberately no project, tenant
# or instance field (REQ-06): an instance hosts one project, so a request
# naming one is refused rather than ignored.
REQUEST_FIELDS = ('question', 'scope', 'limit', 'sessionWeight', 'sessionCap')


class Refused(Exception):
    """The request is refused, with the reason; it is not searched."""


def parse(body: dict) -> dict:
    """Validate a request body into ``question``, ``scope``, ``limit`` and
    the ranking parameters, or raise ``Refused``."""
    unknown = sorted(set(body) - set(REQUEST_FIELDS))
    if unknown:
        raise Refused(f"unknown field(s) {', '.join(unknown)}; a request has only "
                      f"{', '.join(REQUEST_FIELDS)}. An instance hosts one project, so no request can name "
                      f"another project, tenant or instance")
    question = body.get('question')
    if not isinstance(question, str) or not question.strip():
        raise Refused('question is required: a question or a description of the context, as text')
    scope = body.get('scope')
    if scope is not None and scope not in SCOPES:
        raise Refused(f"scope {scope!r} is not a retrieval scope; declare {' or '.join(repr(s) for s in SCOPES)}, "
                      f"or declare none to search both")
    limit = body.get('limit', DEFAULT_LIMIT)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise Refused(f'limit must be a whole number from 1 to {MAX_LIMIT}')
    weight = body.get('sessionWeight', ranking.SESSION_WEIGHT)
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight < 0:
        raise Refused('sessionWeight must be a number, zero or more')
    cap = body.get('sessionCap')
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or cap < 0):
        raise Refused('sessionCap must be a whole number, zero or more')
    return {'question': question, 'scope': scope, 'limit': limit,
            'params': ranking.Params(session_weight=float(weight), session_cap=cap)}


def _passage(rank_number: int, candidate: ranking.Candidate, status: Optional[dict], now) -> dict:
    written = candidate.written_at
    return {
        'unitId': candidate.unit_id,
        'rank': rank_number,
        'score': round(candidate.score, 4),
        'text': candidate.text,
        'class': candidate.source_class,
        'scope': candidate.scope,
        'reference': candidate.reference,
        'source': sources.address(candidate.source_class, candidate.reference),
        'writtenAt': written.isoformat() if written else None,
        'ageSeconds': int((now - written).total_seconds()) if written else None,
        'derived': True,
        'authoritative': False,
        'sourceStatus': status,
    }


def answer(request: dict, *, core: Optional[sources.CoreReads] = None) -> dict:
    parsed = parse(request)
    core = core or sources.CoreReads()
    lag = index_status()
    now = timezone.now()
    params = parsed['params']
    # Ranked with room to spare: a withheld passage must not leave the
    # result short of passages that exist.
    candidates = ranking.rank(parsed['question'], scope=parsed['scope'], limit=parsed['limit'] * 2, params=params)

    passages, withheld = [], []
    for candidate in candidates:
        if len(passages) == parsed['limit']:
            break
        if not sources.resolvable(candidate.source_class, candidate.reference):
            withheld.append({'unitId': candidate.unit_id, 'reason': 'its reference does not resolve to a source'})
            continue
        status = sources.source_status(candidate.source_class, candidate.reference, core)
        if status is not None and status.get('current') is False:
            withheld.append({'unitId': candidate.unit_id, 'class': candidate.source_class,
                             'reference': candidate.reference,
                             'reason': 'the passage is from an extraction that is not current against the '
                                       "artifact's present record" if status['state'] == 'known'
                                       else 'its source no longer exists'})
            continue
        passages.append(_passage(len(passages) + 1, candidate, status, now))

    incomplete = bool(lag['incomplete'])
    body = {
        'question': parsed['question'],
        'scope': parsed['scope'],
        'scopesSearched': [parsed['scope']] if parsed['scope'] else list(SCOPES),
        'passages': passages,
        'withheld': withheld,
        'incomplete': incomplete,
        'incompleteReasons': lag['reasons'],
        # REQ-07: an empty result means "nothing relevant" only when the
        # index was not behind.
        'nothingRelevant': not passages and not incomplete,
        'statusUnavailable': any((p['sourceStatus'] or {}).get('state') == 'unavailable' for p in passages),
        'notice': NOTICE,
    }
    with transaction.atomic():
        entry = QueryLog.objects.create(
            question=parsed['question'], scope_declared=parsed['scope'], scopes_searched=body['scopesSearched'],
            parameters={'limit': parsed['limit'], 'sessionWeight': params.session_weight,
                        'sessionCap': params.session_cap},
            response={}, index_incomplete=incomplete)
        body['queryId'] = entry.id
        entry.response = body
        entry.save(update_fields=['response'])
    return body
