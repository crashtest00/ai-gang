# Librarian

The Django application that implements
`strategy/v4.0/features/librarian.md` (Management repository) in the
internal work-item service.

An agent that needs an artifact present in a repository **asks** for it.
The librarian copies it in once, and answers with the path the file
actually occupies. A second request returns that same path rather than a
second copy. Nothing is ever pushed at a container on the platform's
initiative (PRD §8, "ask, don't push").

There is **no search, no listing and no browse**. A requester names an
artifact id it was already given; this app offers no way to discover one
(spec §2, PRD §7.4).

## The request/response contract

This section is the contract. An agent-facing helper that publishes a
request and blocks for the reply is written against it.

### Streams

| | Stream | Consumer group |
| --- | --- | --- |
| Request (in) | `aigang:librarian:requests` | `librarian` |
| Response (out) | `aigang:librarian:responses` | none — see below |
| Failed request | `aigang:librarian:requests:dead` | operator/replay tooling only |

Defined in `librarian/stream_topology.py`; nothing else names them.

Three naming decisions, each load-bearing:

- **Not `aigang:gateway:{project}`.** ScrumMaster is that stream's
  consumer group and dead-letters kinds it does not know, so a delivery
  request published there would be destroyed rather than delivered.
- **Not under `aigang:workitems:`.** Delivery is not a work-item command,
  and that service's command consumer would reject it the same way. The
  Django instance holds more than work items now; this app has its own
  name, its own table and its own stream namespace.
- **No `{project}` segment.** One librarian serves every repository on
  the machine, and a request names its destination repository in the
  payload. `aigang:<domain>` with the project segment absent is the shape
  `redis-streams.md`'s topology table leaves for a stream that is not
  per-project, and it is what `artifacts/events.py` already does.

The response stream has **no consumer group** deliberately: a response has
exactly one interested reader — the requester waiting for it — and a group
would hand it to whichever member read first.

### Envelope

The envelope is `redis-streams.md`'s, byte-for-byte the shape
`workitems/envelope.py` defines (`schemaVersion` `"1"`, `msg-<uuid>`
message ids, the whole envelope JSON-encoded under the stream entry's
single `data` field). Two kinds are this app's:

| `kind` | Direction |
| --- | --- |
| `artifact_delivery_request` | requester → librarian |
| `artifact_delivery_response` | librarian → requester |

Both are in `workitems.envelope.VALID_KINDS`, so the shared envelope
validates a delivery message exactly as it validates a work-item command,
and the subscriber is `workitems.streams`' own consumer with no envelope
override of its own. `librarian/envelope.py` adds only what is this app's
and not the platform's: the `project` sentinel below and the field
spelling.

`project` carries the fixed sentinel `_instance`: the envelope requires a
non-empty project, and these streams are instance-wide. The destination
repository is a payload field, so there is exactly one place it is stated.

### Request payload

| Field | Required | Meaning |
| --- | --- | --- |
| `requestedBy` | yes | Who is asking. Recorded on the delivery record. |
| `artifactId` | yes | The artifact's canonical id, from artifact ingress. |
| `destinationRepo` | yes | The repository to deliver into — see "Repositories" below. |
| `requestedPath` | yes | Where the requester *wants* it, repository-relative. Not where it ends up: that is the answer (REQ-04). |
| `taskId` | no | The work item or A2A task this is for. Echoed back, and recorded. |

`librarian.md` REQ-01 names these fields `requested_by`, `artifact_id`,
`destination_repo`, `requested_path` and `task_id`. Every other payload on
this platform is camelCase, so the librarian **publishes camelCase and
accepts either spelling** on the way in. A requester written from the
specification's words works, and so does one written to the convention.

### Response payload

Every response carries `status`, plus the request's `artifactId`,
`destinationRepo`, `requestedBy` and `taskId` echoed back.

**`status: "delivered"`** adds:

| Field | Meaning |
| --- | --- |
| `path` | Repository-relative path the artifact occupies, read back from the operation performed. |
| `action` | `copied` — the bytes were written now. `already_present` — it was already there. `relocated` — it had been moved, and the record was corrected. |
| `deliveredAt` | When this artifact first reached this repository. |

**`status: "failed"`** adds `reason` — one of the five below — and
`detail`, a sentence naming the specific value.

| `reason` | Raised when |
| --- | --- |
| `missing_field` | A required field is absent or blank. `detail` names it. |
| `unknown_artifact` | No artifact is registered under that id, or the id is not a canonical id. |
| `unknown_destination_repo` | No such directory under the projects root, the name is not a single directory name, or the project directory has no `PROJECTS_REPO_SUBDIR` (`src`) working tree. |
| `path_outside_repository` | `requestedPath` is absolute, escapes the repository, names a directory, is under `.git` or `node_modules`, or resolves outside the repository through a symlink. |
| `copy_failed` | The artifact has no file on the volume, the destination cannot be written, the file created turned out to be outside the repository, or the librarian could not complete the request. |

Defined in `librarian/failures.py`. There are no others.

### How a requester gets its one answer

Correlation is by `correlationId`, which carries the **request envelope's
`messageId`** — a value the requester minted itself, so it knows what to
look for before it publishes.

1. Record the response stream's current last entry id:
   `XINFO STREAM aigang:librarian:responses` → `last-generated-id`, or
   `0-0` if the stream does not exist yet. **Before** publishing, so an
   answer that arrives immediately cannot be missed.
2. `XADD aigang:librarian:requests * data '<the request envelope JSON>'`.
3. `XREAD BLOCK <ms> STREAMS aigang:librarian:responses <the recorded id>`
   in a loop, advancing the id past every entry read, until one arrives
   whose `correlationId` equals your `messageId`. That is your answer, and
   there is exactly one of it.

`tests/librarian_support.py`'s `publish_request` / `await_response` are
that sequence, and the librarian tests use nothing else to talk to it.

**Every request that can be decoded is answered exactly once, and then
acknowledged** — including when the answer is a failure (REQ-06: "no
request completes without a response"). A failed delivery is reported to
the requester rather than retried into a dead-letter the requester would
never see; the requester decides whether to ask again. An entry that is
not a librarian request envelope at all carries no correlation id and no
requester, so there is nobody to answer: it goes to
`aigang:librarian:requests:dead` through the same machinery
`workitems/streams.py` uses for every other stream in this service.

## Repositories

A repository is named by **one directory name directly under
`settings.PROJECTS_ROOT`** (environment variable `PROJECTS_ROOT`, default
`/var/lib/aigang/projects`). That is what `destinationRepo` names. A name
that is not a directory there is `unknown_destination_repo`; there are no
per-repository volumes and no registry lookup.

In a Compose deployment the projects root is the Source checkout's own
`projects/` directory, so `destinationRepo` is the project name:
`projects/hello-web` is `hello-web`.

**The repository is the project's working tree, one level further down.**
`scripts/init-project.sh` gives every project a `projects/<name>/src`
directory, makes it the git root, and bind-mounts it into that project's
container as `/workspace`. `projects/<name>/` itself holds the project's
own `docker-compose.yml`, `Dockerfile` and `.env` — deployment
scaffolding, not repository content. The librarian delivers into
`projects/<name>/src`, and **every path in a request, in an answer, in the
content search and in the delivery record is relative to that directory,
which is exactly the agent's `/workspace`.** A requester that wants
`designs/mockup.png` in its working tree asks for `designs/mockup.png`.

Which subdirectory that is comes from `settings.PROJECTS_REPO_SUBDIR`
(environment variable `PROJECTS_REPO_SUBDIR`, default `src`). A project
directory that does not have one is `unknown_destination_repo`: there is
no working tree to deliver into, and the project directory is not a
substitute for it — a file written there is invisible to both git and the
agent, and a delivery named `docker-compose.override.yml` would be merged
into the project's own Compose configuration on its next `up`.

`requestedPath` must resolve inside the repository once normalized and
once symlinks are followed — the containment check in `librarian/paths.py`
is what makes `..`, an absolute path, a NUL byte and a symlink out of the
tree all the same single failure. None of its components, at any depth,
may be `.git` or `node_modules`: the content search prunes both at every
level of its walk, not only the top (see "The content search" below), so
a file delivered under either — even nested arbitrarily deep, as a
submodule's own `.git` or a vendored package's `node_modules` would be —
could never be found again, and `.git` is the repository's own object
store. The write rule and the search rule are one list,
`SKIPPED_DIRECTORIES` in `librarian/content.py`.

The containment check resolves symlinks at one moment and the write
happens at another, so the file's real path is checked **again** after it
is created. A file that a symlink planted in between put outside the
repository is removed and the request answered `copy_failed`; no
confirmation ever names a path in a repository the requester did not ask
for.

## Resolution order

`librarian.md` REQ-03, implemented in `librarian/delivery.py`:

1. Is there a delivery record for this artifact and repository?
2. If so, at what path was it delivered?
3. Is the artifact still at that path? If yes, answer with that path.
4. If not, search the destination repository for the artifact by content.
   If found, answer with the path it was found at and correct the record.
5. If there is no record, or the artifact is not in the repository, copy
   it to `requestedPath` and answer with the resulting path.

Two readings this implementation fixes, because both change behaviour:

- **Step 3 is an existence check, not a content comparison.** A delivered
  file a human then edited is still the delivered file; re-delivering over
  their edit would be the opposite of what REQ-03 asks.
- **Step 4 runs whenever steps 1–3 did not answer, including when there is
  no record at all.** Step 5's "or the artifact is not in the repository"
  is a fact about the repository, and the content search is the only thing
  that establishes it. Skipping it on the no-record path would let a
  repository that already holds the artifact receive a second copy at a
  second path, which is what REQ-02 forbids.

The filesystem is authoritative throughout (PRD §8, "the file is the
record"). The delivery record is an index that makes the common case fast;
where the two disagree, the record is corrected.

### The content search

- **sha256**, over the artifact's *current* bytes — so an artifact edited
  in place on the volume (ingress REQ-02) is matched on what it now holds.
  The specification asks only for "any collision-resistant digest"
  (§6.2); sha256 is that, and is in the standard library.
- **Size first.** A file of a different size cannot be a byte-for-byte
  copy, so only same-sized candidates are hashed.
- **Skipped:** `.git` (a committed artifact's blob lives there
  byte-for-byte, and answering with a path inside `.git` would be worse
  than useless), `node_modules` (vendored dependencies, not repository
  content), and **symlinks**, which are never followed, so the search can
  neither leave the repository nor loop. Nothing else is skipped.
- **Deterministic.** The walk is sorted at every level and the first match
  wins, so two librarians searching the same repository return the same
  path.

### The name-collision rule (REQ-04)

The requested path is used unchanged when nothing occupies it. When
something unrelated does — a file with different content, or a directory
of that name — `-1`, `-2`, … is inserted **before the extension**, and the
first free name wins:

```
designs/mockup.png -> designs/mockup-1.png -> designs/mockup-2.png
NOTES              -> NOTES-1              -> NOTES-2
.gitignore         -> .gitignore-1         -> .gitignore-2
archive.tar.gz     -> archive.tar-1.gz
```

The extension is the last dot in the file's name, and a leading dot is not
one. "Something unrelated" is the only case this can be: a file whose
bytes *are* the artifact was already found and answered by step 4. After
200 taken names the request is answered `copy_failed` rather than counting
further.

The response always names the path the librarian produced, read back from
the file it created. It never echoes `requestedPath`.

## One copy under concurrent requests (REQ-07)

Two mechanisms, either of which alone would leave a hole:

- **A Postgres advisory lock keyed on the (artifact, repository) pair**,
  taken for the duration of the transaction that resolves the request
  (`pg_advisory_xact_lock`, released by the commit). A hash of the pair
  rather than a row id, because the serialization has to hold *before* the
  delivery record exists. It is honoured across processes and machines, so
  two librarian containers serialize against each other and not merely two
  threads in one.
- **An atomic create.** The bytes go to a temporary name in the same
  directory, are flushed to disk, and are then given their real name with
  `os.link`, which fails rather than overwriting. No reader sees a
  partially written file, and no unrelated file — written by an agent the
  librarian does not serialize with — is ever clobbered. On a filesystem
  that cannot hard-link, the fallback claims the name with
  `O_CREAT|O_EXCL`, which refuses to overwrite for the same reason, and
  renames over its own placeholder.

## The delivery record

| Table | Holds |
| --- | --- |
| `artifact_delivery` | One row per (artifact, repository): the delivered path, the requester, `task_id`, and the times. |

Written synchronously inside the transaction that handles the request
(REQ-05), which is not an exception to `internal-work-item-service.md`
REQ-03: every request still *arrives* as a Streams message, which is the
discipline that requirement is about, and this app is its table's only
writer.

REQ-02 — an artifact occupies at most one path per repository — is
enforced at the schema level by the UNIQUE constraint
`uniq_artifact_delivery_pair` in `librarian/migrations/0001_initial.py`.
Postgres refuses a second row for a pair; it is not a rule the application
merely promises to keep.

`artifact_id` is a foreign key to `artifact` (artifact ingress's table, in
the same Postgres instance), `ON DELETE CASCADE`: a delivery record is
meaningless without the artifact it indexes. The delivered *file* is
unaffected either way, which is the record-versus-filesystem asymmetry
REQ-05 is about.

The table is visible read-only in the Django Admin Panel (PRD §3). It is
read-only because the librarian is its only writer, and because editing a
row would be editing the index rather than the filesystem it indexes —
which the next request would simply correct.

## Deployment

`docker-compose.yml` runs the subscriber as its own service:

| | |
| --- | --- |
| Service | `librarian` (container `workitem-librarian`) |
| Command | `python manage.py run_librarian` |
| User | `1000:1000` — the uid project images run as (`Docker Templates/Dockerfile-node.template`) and the owner of the host's `projects/*`. A delivered file has to be one the agent can edit, delete and `git clean`; as root it would not be. |
| `artifact-data:/var/lib/aigang/artifacts` | **read-only** — the librarian only ever copies out of it. Upload is the admin's, on `api` (PRD §7.1, "upload is the only way in"). |
| `../../projects:/var/lib/aigang/projects` | read-write — delivery writes into `<project>/src`, the repository's working tree. |
| `PROJECTS_REPO_SUBDIR=src` | The working-tree subdirectory of each project directory — see "Repositories". |

Its own container because it is the one service that mounts the project
repositories, which is a privilege the rest of the instance does not need
(PRD §14: "the librarian and the repositories must share a machine").

`docker-compose.test.yml` adds nothing: the suite runs on the host with
`ARTIFACT_ROOT` and `PROJECTS_ROOT` pointed at per-test temporary
directories, and the subscriber running in the test process.

## Tests

`tests/test_librarian_*.py`, with shared helpers in
`tests/librarian_support.py`. Every one of them publishes a real envelope
onto the real request stream in the test Redis, lets the real consumer
loop handle it, and asserts on the filesystem, the `artifact_delivery`
row and the response stream. Artifacts are seeded through artifact
ingress's own admin upload view. No test calls the delivery function in
place of any of that.

| File | Covers |
| --- | --- |
| `test_librarian_contract.py` | REQ-01 — the field contract, both spellings, correlation, dead-lettering |
| `test_librarian_resolution.py` | REQ-02, REQ-03, REQ-05, AC-06 — every branch of the resolution order |
| `test_librarian_collision.py` | REQ-04 — the adjustment rule and the read-back |
| `test_librarian_failures.py` | REQ-06 — every failure reason, and exactly one answer per request |
| `test_librarian_concurrency.py` | REQ-07 — two subscribers, contention observed in `pg_locks` |
| `test_librarian_atomic_create.py` | REQ-07 — a racing writer never loses its file, and the no-hard-link fallback |
