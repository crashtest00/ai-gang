"""
Retrieval by canonical id — artifact-ingress.md REQ-03.

A direct synchronous GET, the same shape ``workitems/views.py``'s read
endpoints have (same ``X-Actor`` convention for the access log, same
``?flag=true`` query-parameter convention as ``?full=true``). Unauthenticated,
like those reads: the PRD accepts that "any requester that can name an id
can have the artifact" (§14) for this version.

There is deliberately NO upload endpoint here. Uploads go through the
Django Admin Panel, where the actor is the authenticated user rather than
a self-declared header (product owner, September 20, 2026).
"""

from __future__ import annotations

from django.http import FileResponse, JsonResponse
from django.views.decorators.http import require_http_methods

from . import readstore
from .models import Artifact

# Never sniffed from the bytes and never taken from the original filename:
# V4 does not interpret content (PRD §9), and a guessed content type is an
# interpretation the platform would be asserting on the file's behalf.
ARTIFACT_CONTENT_TYPE = 'application/octet-stream'


def _serialize(artifact: Artifact) -> dict:
    return {
        'id': str(artifact.id),
        'path': artifact.path,
        'originalFilename': artifact.original_filename,
        'uploadedBy': artifact.uploaded_by,
        'createdAt': artifact.created_at.isoformat(),
        'updatedAt': artifact.updated_at.isoformat(),
    }


@require_http_methods(['GET'])
def get_artifact(request, artifact_id):
    """``GET /artifacts/<id>`` — the bytes.
    ``GET /artifacts/<id>?record=true`` — the record.

    The bytes are streamed from a handle opened on the volume during THIS
    request (REQ-02): the service keeps no copy, so a file edited in place
    on the host is returned in its edited form by the next call, with no
    upload and no restart.
    """
    actor = request.headers.get('X-Actor', 'http-client')
    artifact = readstore.get_artifact(artifact_id, actor=actor)
    if artifact is None:
        return JsonResponse({'error': 'not found'}, status=404)

    if request.GET.get('record') == 'true':
        return JsonResponse(_serialize(artifact))

    try:
        handle = artifact.file.storage.open(artifact.file.name, 'rb')
    except FileNotFoundError:
        # The record says where the file is; the filesystem says whether it
        # is still there, and the filesystem wins (PRD §8).
        return JsonResponse({'error': 'artifact file missing', 'path': artifact.path}, status=404)

    return FileResponse(handle, content_type=ARTIFACT_CONTENT_TYPE)
