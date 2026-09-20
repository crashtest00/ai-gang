"""
The external HTTP API: a direct query interface, plus
human-/external-facing interfaces that may write directly to the datastore,
subject to the Jira-mode restriction on canonical fields. Direct
port of the Node service's src/httpApi.js — intentionally a thin HTTP
surface: every request still goes through store.py/readstore.py, which is
where access logging, write-gating, and
history all actually happen.

Preserves services/scrummaster/src/canonicalWorkItems.js's expected HTTP contract
EXACTLY: same paths, same methods, same status codes, same JSON response
shapes (see serializers.py's module comment) — see the final report for
confirmation this was achieved with no endpoint requiring a scrummaster-
side change.
"""

from __future__ import annotations

import json
import os

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import project_config, readstore, registry, relay, store, write_gate
from .envelope import Kind, build_envelope
from .serializers import (
    serialize_work_item, serialize_work_item_full, serialize_work_item_with_associations,
    serialize_work_item_with_references,
)
from .streams import classify_health, health as stream_health, publish

CLIENT_ERROR_CODES = {
    'VALIDATION_ERROR', 'ASSIGNMENT_REJECTED', 'DEPENDENCY_GATE_REJECTED', 'WRITE_GATE_REJECTED',
    'RELEASE_GATE_REJECTED',
}


def _error_response(err: Exception):
    code = getattr(err, 'code', None)
    if code in CLIENT_ERROR_CODES:
        status = 409 if code == 'WRITE_GATE_REJECTED' else 400
        return JsonResponse({'error': code, 'message': str(err)}, status=status)
    print(f'[views] unexpected error: {err!r}')
    return JsonResponse({'error': 'INTERNAL_ERROR'}, status=500)


def health(request):
    """Report on this service's own inbound-webhook consumer group
    health (the Jira ingestion path, now that this service
    owns it — see jira_webhook below) and the outbox relay's
    health, mirroring the depth services/scrummaster/src/server.js's own /health
    already provides for its streams. Previously a bare {"status": "ok"}
    stub."""
    from .webhook_consumer import WEBHOOK_GROUP

    client = registry_redis_client()
    reports = []
    for project_name in registry.get_project_names():
        webhook = stream_health(client, registry.webhook_stream_name(project_name), WEBHOOK_GROUP)
        reports.append({**webhook, 'status': classify_health(webhook)})

    relay_report = relay_health(client)
    reports.append(relay_report)

    overall = 'unhealthy' if any(r['status'] == 'unhealthy' for r in reports) \
        else 'degraded' if any(r['status'] == 'degraded' for r in reports) \
        else 'ok'
    status_code = 503 if overall == 'unhealthy' else 200
    return JsonResponse({'status': overall, 'streams': reports}, status=status_code)


def registry_redis_client():
    from .redis_client import get_client
    return get_client()


def relay_health(client) -> dict:
    """The outbox relay has no consumer group of its own (it's a
    plain poll-and-publish loop, not a Streams consumer) — health is
    instead "is there a growing backlog of unpublished rows," using the
    oldest unpublished row's age as the same kind of staleness signal
    stream_health's oldestPendingAgeMs already reports for a consumer
    group."""
    from django.utils import timezone

    from .models import OutboxEvent

    oldest = OutboxEvent.objects.filter(published_at__isnull=True).order_by('created_at').first()
    unpublished_count = OutboxEvent.objects.filter(published_at__isnull=True).count()
    age_ms = None
    if oldest is not None:
        age_ms = int((timezone.now() - oldest.created_at).total_seconds() * 1000)

    threshold_ms = 10 * 60 * 1000
    status = 'unhealthy' if client is None else 'degraded' if (age_ms or 0) > threshold_ms else 'healthy'
    return {'component': 'outbox-relay', 'unpublishedCount': unpublished_count, 'oldestUnpublishedAgeMs': age_ms, 'status': status}


@csrf_exempt
@require_http_methods(['POST'])
def jira_webhook(request):
    """Amended 2026-09-09 — Django is AI
    Gang's sole external-facing surface; this replaces
    services/scrummaster/src/server.js's deleted `POST /webhook/jira` route.

    Durably enqueues the raw webhook onto the SAME `aigang:webhooks:{project}`
    Redis Stream, using the SAME envelope/dedupe-key shape server.js used to
    produce, before ever acknowledging receipt — this is the
    acceptance test: a Django/work-item-service kill immediately after this
    durable enqueue must not lose the event."""
    secret = os.environ.get('WEBHOOK_SECRET')
    if secret:
        if request.GET.get('secret') != secret:
            return JsonResponse({'error': 'Unauthorized'}, status=401)
    else:
        print('[views] WEBHOOK_SECRET not set — accepting all webhook requests')

    try:
        body = json.loads(request.body or b'{}')
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'Invalid JSON body'}, status=400)

    event = body.get('webhookEvent')
    issue = body.get('issue')
    if not event or not issue or not issue.get('key'):
        return JsonResponse({'error': 'Missing webhookEvent or issue'}, status=400)

    issue_fields = issue.get('fields') or {}
    project_field = issue_fields.get('project') or {}
    project_name = project_field.get('name') or project_field.get('key')
    if not project_name:
        return JsonResponse({'error': 'Missing issue.fields.project'}, status=400)

    # Stable across Jira's own redelivery of the same event — built from
    # fields Jira itself supplies for this delivery, not a value we mint.
    changelog_id = (body.get('changelog') or {}).get('id')
    dedupe_seed = ':'.join(str(part) for part in (event, issue['key'], body.get('timestamp'), changelog_id) if part)

    try:
        client = registry_redis_client()
        stream = registry.webhook_stream_name(project_name)
        envelope = build_envelope(
            Kind.WEBHOOK_EVENT,
            registry.normalize_project_name(project_name),
            payload={'event': event, 'issue': issue, 'body': body},
        )
        result = publish(client, stream, envelope, dedupe_key=f'webhook:{dedupe_seed}')
        return JsonResponse({'received': True, 'deduped': result['deduped']})
    except Exception as err:
        # Do not report success if the event was never durably enqueued —
        # Jira will retry a non-2xx response.
        print(f'[views] Failed to durably enqueue webhook for {issue["key"]}: {err!r}')
        return JsonResponse({'error': 'Failed to durably accept event'}, status=502)


@require_http_methods(['GET'])
def project_mode(request, project: str):
    mode = project_config.get_mode(project)
    return JsonResponse(mode)


@require_http_methods(['GET'])
def get_work_item(request, work_item_id):
    actor = request.headers.get('X-Actor', 'http-client')
    full = request.GET.get('full') == 'true'
    if full:
        result = readstore.get_work_item_full(work_item_id, actor=actor)
        if not result:
            return JsonResponse({'error': 'not found'}, status=404)
        return JsonResponse(serialize_work_item_full(result))

    item = readstore.get_work_item(work_item_id, actor=actor)
    if not item:
        return JsonResponse({'error': 'not found'}, status=404)
    specification_link = readstore.get_specification_link(work_item_id, actor=actor)
    artifact_links = readstore.list_artifact_links(work_item_id, actor=actor)
    return JsonResponse(serialize_work_item_with_references(item, specification_link, artifact_links))


@require_http_methods(['GET'])
def list_work_items(request):
    """`specArtifactId`/`requirementId` are work-items.md REQ-06's forward
    query (see urls.py's module docstring) — when either is supplied, each
    result is serialized with its specification link and REQ-06 delivery
    associations included, per that requirement's acceptance.

    `externalKey` is work-items.md REQ-05's canonical-id lookup (V4 audit
    Pass 2 row 33): a dispatched agent's prompt carries the issue key, not
    the work item's canonical id (`services/scrummaster/src/prompt.js`), so
    this filter's result carries the REQ-01/REQ-02 references too — the
    same shape `get_work_item`'s bare (non-`?full=true`) read already
    returns, via the same `serialize_work_item_with_references`.

    Absent all three, behavior (and response shape) is unchanged."""
    actor = request.headers.get('X-Actor', 'http-client')
    spec_artifact_id = request.GET.get('specArtifactId')
    requirement_id = request.GET.get('requirementId')
    external_key = request.GET.get('externalKey')
    rows = readstore.list_work_items(
        project=request.GET.get('project'),
        status=request.GET.get('status'),
        assignee_agent_id=request.GET.get('assigneeAgentId'),
        parent_id=request.GET.get('parentId'),
        spec_artifact_id=spec_artifact_id,
        requirement_id=requirement_id,
        external_key=external_key,
        actor=actor,
    )
    if spec_artifact_id or requirement_id:
        return JsonResponse([serialize_work_item_with_associations(r) for r in rows], safe=False)
    if external_key:
        return JsonResponse([
            serialize_work_item_with_references(
                r, getattr(r, 'specification_link', None), list(r.artifact_links.all())
            ) for r in rows
        ], safe=False)
    return JsonResponse([serialize_work_item(r) for r in rows], safe=False)


@require_http_methods(['GET'])
def get_specification_link_for_delivery_artifact(request, work_item_artifact_id):
    """work-items.md REQ-06 (AC-04) — backward traceability. Addressed by
    the `work_item_artifact` association's OWN id (the delivery-evidence
    row REQ-06 already links a work item forward to what it produced), not
    by the work item's id — see urls.py's module docstring."""
    actor = request.headers.get('X-Actor', 'http-client')
    result = readstore.get_specification_link_for_delivery_artifact(work_item_artifact_id, actor=actor)
    if result is None:
        return JsonResponse({'error': 'not found'}, status=404)
    return JsonResponse(result)


@csrf_exempt
@require_http_methods(['POST'])
def admin_create_work_item(request):
    try:
        actor = request.headers.get('X-Actor', 'external-api')
        body = json.loads(request.body or b'{}')
        item = store.create_work_item(body, actor=actor, origin=write_gate.Origins.EXTERNAL_API)
        return JsonResponse(serialize_work_item(item), status=201)
    except Exception as err:
        return _error_response(err)


@csrf_exempt
@require_http_methods(['POST'])
def admin_transition_work_item(request, work_item_id):
    try:
        actor = request.headers.get('X-Actor', 'external-api')
        body = json.loads(request.body or b'{}')
        item = store.transition_status(work_item_id, body.get('status'), actor=actor, origin=write_gate.Origins.EXTERNAL_API)
        return JsonResponse(serialize_work_item(item))
    except Exception as err:
        return _error_response(err)


@csrf_exempt
@require_http_methods(['POST'])
def admin_record_release_candidate(request, work_item_id):
    """The local-mode writeback
    target for the release-candidate Jenkins job's results (Candidate SHA,
    Build Identifier, Preview URL), mirroring what Jenkins already writes
    directly onto a Jira Release ticket's custom fields today. Jenkins
    itself does not yet call this — remaining Jenkins-side follow-up is
    tracked separately."""
    try:
        actor = request.headers.get('X-Actor', 'jenkins')
        body = json.loads(request.body or b'{}')
        if not body.get('candidateSha'):
            return JsonResponse({'error': 'VALIDATION_ERROR', 'message': 'candidateSha is required'}, status=400)
        item = store.record_release_candidate(
            work_item_id, candidate_sha=body['candidateSha'], build_identifier=body.get('buildIdentifier'),
            preview_url=body.get('previewUrl'), actor=actor,
        )
        return JsonResponse(serialize_work_item(item))
    except Exception as err:
        return _error_response(err)


@csrf_exempt
@require_http_methods(['POST'])
def admin_add_comment(request, work_item_id):
    try:
        actor = request.headers.get('X-Actor', 'external-api')
        body = json.loads(request.body or b'{}')
        comment = store.append_comment(
            work_item_id, actor, body.get('body'),
            reference_file=body.get('referenceFile'), reference_function=body.get('referenceFunction'),
            source_message_id=body.get('sourceMessageId'),
        )
        return JsonResponse(comment, status=201)
    except Exception as err:
        return _error_response(err)
