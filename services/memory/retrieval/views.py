"""memory-api's retrieval route (Knowledge Retrieval REQ-01, REQ-03).

- ``POST /retrieve`` with a JSON body, or ``GET /retrieve?question=…``, the
  same fields either way: ``question`` (required), ``scope`` (``project`` or
  ``platform``; none searches both), ``limit``, ``sessionWeight`` and
  ``sessionCap``. A person, a test harness and Dispatch Context Assembly
  call the same route and get the same shape. A request declaring any other
  scope, or any field outside those, is refused with 400 and the reason.
- ``GET /retrieve/queries/<id>`` — a recorded query and exactly what it
  returned (REQ-03).

Memory adds no caller check (as for every memory-api read), and every
read is logged to the container log.
"""

import json

from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from archive.views import logged

from . import service
from .models import QueryLog


def _refused(message):
    return JsonResponse({'error': message}, status=400)


@logged
@require_http_methods(['GET', 'POST'])
def retrieve(request):
    if request.method == 'POST':
        try:
            body = json.loads(request.body or b'{}')
        except ValueError:
            return _refused('the body must be JSON')
        if not isinstance(body, dict):
            return _refused('the body must be a JSON object')
    else:
        body = {key: value for key, value in request.GET.items()}
        for numeric in ('limit', 'sessionCap'):
            if numeric in body:
                try:
                    body[numeric] = int(body[numeric])
                except ValueError:
                    return _refused(f'{numeric} must be a whole number')
        if 'sessionWeight' in body:
            try:
                body['sessionWeight'] = float(body['sessionWeight'])
            except ValueError:
                return _refused('sessionWeight must be a number')
    try:
        return JsonResponse(service.answer(body))
    except service.Refused as err:
        return _refused(str(err))


@logged
@require_http_methods(['GET'])
def query_entry(request, query_id):
    entry = QueryLog.objects.filter(pk=query_id).first()
    if entry is None:
        return JsonResponse({'error': f'no recorded query {query_id}'}, status=404)
    return JsonResponse({
        'queryId': entry.id, 'recordedAt': entry.created_at.isoformat(), 'question': entry.question,
        'scopeDeclared': entry.scope_declared, 'scopesSearched': entry.scopes_searched,
        'parameters': entry.parameters, 'indexIncomplete': entry.index_incomplete, 'response': entry.response})
