# Artifact Ingress

The Django application that implements
`strategy/v4.0/features/artifact-ingress.md` (Management repository) in the
internal work-item service.

An artifact is a file a human hands to a running AI Gang. This app stores
it, fixes its path, gives it a canonical id, and hands the bytes back when
something names that id. It records identity and nothing about meaning: no
type, no status, no parsing, and no requirement that a file be shaped a
particular way to be accepted.

## Storage layout

Artifact files live on a mounted volume rooted at `settings.ARTIFACT_ROOT`
(environment variable `ARTIFACT_ROOT`, default `/var/lib/aigang/artifacts`).
The path of an artifact under that root is derived from its canonical id
and from nothing else:

```
<ARTIFACT_ROOT>/<first two characters of the id>/<the id>
```

For example, artifact `3f2c1e5a-7b41-4f0a-9d2e-6c8b0a1d4e77` is the file:

```
/var/lib/aigang/artifacts/3f/3f2c1e5a-7b41-4f0a-9d2e-6c8b0a1d4e77
```

Three properties of that layout are load-bearing:

- **Derived from the id alone.** A human or an agent on the host finds the
  file for an id without consulting the service, which is what in-place
  editing depends on (REQ-02).
- **No extension, and no trace of the original filename.** The name the
  uploader's client supplied never reaches the filesystem (REQ-05). It is
  kept verbatim on the record as an opaque string, and nowhere else.
- **Two-character shard.** So the root does not become one directory
  holding every artifact in the instance. `3f` above is simply the id's
  first two characters.

The path is recorded on the artifact record (the `path` column) as well as
being derivable, and the two are the same string: `Artifact.file.name` *is*
that column.

### Editing a stored artifact in place

Every read opens the file on the volume during the request; the service
keeps no copy of the bytes. Writing to the file above changes what the next
retrieval returns, with no upload and no restart.

An in-place edit publishes **no** event — the platform does not observe it.
That asymmetry is a property of the design, not an oversight (REQ-06, PRD
§14).

To find the host path of the volume in a Compose deployment:

```
docker volume inspect work-item-service_artifact-data
```

## Upload

Upload is the Django Admin Panel and nothing else:

```
/django-admin/artifacts/artifact/add/
```

The uploader is authenticated, and the admin user's username is the actor
recorded on the artifact. There is no unauthenticated HTTP upload endpoint,
and no self-declared actor header is honoured.

Re-uploading is editing the existing artifact in the admin and choosing a
new file: same id, same path, new bytes (REQ-04). Prior versions are not
retained (PRD §9).

**Any file is accepted.** No extension check, no MIME check, no content
check, no field validator.

### Size limits

None are imposed by this app; Django's own defaults apply, and the open
question the specification left (§6.2) is settled as "no limit added".
In practice that means:

- `DATA_UPLOAD_MAX_MEMORY_SIZE` (Django default 2.5 MB) does **not** bound
  an upload — Django excludes file-part bytes from that check.
- `FILE_UPLOAD_MAX_MEMORY_SIZE` (Django default 2.5 MB) is not a limit
  either; it is the threshold above which an upload is spooled to a temp
  file instead of held in memory. Both paths write the same file at the
  same path.
- The real bound is free space on the artifact volume.

## Retrieval

Retrieval is a direct synchronous GET, unauthenticated like the service's
other reads, and recorded in the `artifact_access_log` table rather than on
Streams (`internal-work-item-service.md` REQ-04).

| Method and path | Returns |
| --- | --- |
| `GET /artifacts/<id>` | The artifact's bytes, as `application/octet-stream` |
| `GET /artifacts/<id>?record=true` | The record: `id`, `path`, `originalFilename`, `uploadedBy`, `createdAt`, `updatedAt` |

Both are defined in `artifacts/urls.py`; this app registers no other route.

- An id with no record is `404`.
- A record whose file is gone from the volume is also `404` — the record
  says where a copy was put, the filesystem says whether it is still there,
  and the filesystem wins (PRD §8).
- The content type is never guessed from the bytes or from the original
  filename: V4 does not interpret content.
- `X-Actor` on a read names the reader in the access log, the same
  convention `workitems`' read endpoints use. It is an access-log label,
  not authentication.

## The upload event

One Redis Streams entry per upload and per re-upload (REQ-06):

| | |
| --- | --- |
| Stream | `aigang:artifacts:events` |
| Envelope `kind` | `artifact_event` |
| Envelope `project` | `_instance` |
| Payload `eventType` | `artifact.uploaded` |
| Payload `action` | `created` on a first upload, `replaced` on a re-upload |

Payload fields: `eventType`, `artifactId`, `path`, `action`, `actor`,
`uploadedAt`.

The stream is **instance-wide**, not per-project: an upload names no
project, so there is no `{project}` segment to fill, and artifacts are not
a work-item feature — nothing here publishes under
`aigang:workitems:{project}:events`. No fixed consumer group is created;
an interested subscriber creates its own, as with the work-item event
stream.

The envelope is the wire shape `workitems/envelope.py` defines (single
`data` field, `msg-<uuid>` message id, `schemaVersion` 1). Its `kind`,
`artifact_event`, is deliberately **not** in that module's `VALID_KINDS`,
because adding it belongs to the track that owns `workitems/`; a consumer
using `workitems.envelope.from_stream_fields` would reject these entries
until it is added. There is no such consumer today.

The event is published from `transaction.on_commit`, so an upload whose
record did not commit announces nothing. The converse — a committed upload
whose publish then fails — is not covered by a transactional outbox here;
see the build report.

## Tables

| Table | Holds |
| --- | --- |
| `artifact` | The artifact record: id, path, original filename, uploader, timestamps |
| `artifact_access_log` | One row per retrieval, per `internal-work-item-service.md` REQ-04 |

Both are in the same Postgres instance as canonical work items, so the
work-item side's "does this artifact id resolve" check is a local read
(spec §4), and both are visible in the Django Admin Panel.

`artifact_access_log` is this app's own rather than a row in `workitems`'
`access_log`, whose subject column is `work_item_id` — an artifact id is
not a work-item id.

## Deployment

`docker-compose.yml` mounts the named volume `artifact-data` at
`/var/lib/aigang/artifacts` on the `api` service, which is the only
service that **writes** artifact bytes — upload is the admin's, and the
admin is `api` (PRD §7.1, "upload is the only way in"). Since the
librarian merged, the `librarian` service mounts that same volume
**read-only** at the same path and copies out of it
(`librarian/delivery.py`); `librarian/README.md` states that side.
`docker-compose.test.yml` mounts nothing: the test suite runs on the host
with `ARTIFACT_ROOT` pointed at a per-test temporary directory.

## Tests

`tests/test_artifacts_*.py`, with shared helpers in
`tests/artifacts_support.py`. They drive the real surfaces — the admin
add/change views through a logged-in Django test client, the retrieval URL
through a plain GET, the event stream through XRANGE against the test
Redis — rather than calling this app's own functions in place of them.
