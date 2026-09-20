# Jenkins Workspace and Docker Cache Retention

Resolves a real "No space left on device" build failure caused by
unpruned `jenkins-data` workspaces and host Docker image/layer cache.

## What runs, and when

Two Jenkins system jobs (defined in `jenkins/jenkins.yaml`, not tied to any
project's Jenkinsfile — the surfaces they clean are shared across every
project's pipeline):

| Job | Schedule | Does |
| --- | --- | --- |
| `jenkins-cache-retention-nightly` | `cron('H 2 * * *')` — nightly | Prunes stale `jenkins-data` workspaces and the host Docker image/layer cache |
| `jenkins-disk-usage-sweep` | `cron('H/30 * * * *')` — every ≥30 min | Checks disk usage; if ≥85%, re-runs the same two prunes repeatedly until usage is back below 70% |

Both call the exact same underlying logic (`jenkins/scripts/prune-workspaces.js`,
`jenkins/scripts/prune-docker-cache.js`) — the threshold sweep is a safety
net on top of the nightly cadence, not a separate policy: the nightly
cadence alone can still lose the race if enough builds land between runs,
which is why the threshold sweep is what actually closes the gap the
original incident exposed.

## Retention policy

- **Workspaces**: a job's on-disk workspace is pruned once it's
  older than 14 days, or once it's beyond the 5 most-recently-used
  workspaces for that job (superseded branches/PRs), whichever comes first.
- **Docker cache**: `docker system prune -f --filter until=72h` —
  images/layers/build cache untouched for 72+ hours, never anything backing
  an existing container.
- **In-progress builds are never touched**: a workspace whose
  job/branch Jenkins reports as currently building is excluded from every
  prune, regardless of age or count. Docker's own `system prune` semantics
  (no `-a`) never remove an image/cache layer attached to an existing
  container, running or stopped, which gives the Docker side of this
  guarantee for free.

Values are starting defaults, not yet tuned against this host's real disk
size or churn.

## Where to look: current disk usage and prune history

Two places, both without SSHing in:

1. **Each job's own Jenkins console output** — `jenkins-cache-retention-nightly`
   and `jenkins-disk-usage-sweep`'s build history in the Jenkins UI. Every
   run prints what it found, what it removed, how many bytes were
   reclaimed, and (for the sweep) the disk-usage reading before and after
   each iteration. A red `jenkins-disk-usage-sweep` build means the sweep
   hit its iteration cap without getting back under the low watermark —
   that needs operator attention, since it means there wasn't enough
   prunable content to relieve the pressure automatically.

2. **`/var/jenkins_home/retention/prune-history.jsonl`** inside the Jenkins
   container (the `jenkins-data` volume, so it survives container
   recreation) — one JSON line per prune run, from any of the three
   triggers (`scheduled-workspace`, `scheduled-docker`, `threshold-sweep`),
   with a timestamp, what was removed, and bytes reclaimed. Written by
   `jenkins/scripts/lib/retention-log.js`. Override the path with the
   `RETENTION_LOG_PATH` env var if needed.

## Scripts (`jenkins/scripts/`)

- `lib/retention-policy.js` — pure decision logic (what's prunable, age/
  count/threshold math, the in-progress exclusion rule). No filesystem or
  Docker access. Unit tested in `jenkins/test/retention-policy.test.js`.
- `lib/jenkins-api.js` — asks the Jenkins REST API which jobs are
  multibranch parents and which job/branches are currently building.
- `lib/workspace-fs.js` — walks `jenkins-data`'s workspace tree and deletes
  directories.
- `lib/disk-usage.js` — reads `df` output for the current usage percent.
- `lib/retention-log.js` — the append-only prune-history log.
- `prune-workspaces.js`, `prune-docker-cache.js` — orchestration entry
  points invoked by both jobs above.
- `disk-usage-sweep.js` — the threshold-check-and-loop entry point.

Run `npm test` from `jenkins/` for the full suite (`jenkins/test/`, using
Node's built-in `node --test`, matching `services/scrummaster/`'s convention).

## Known limitation: not exercised against a live Jenkins controller or Docker daemon

Every piece of decision logic and filesystem orchestration has real
automated test coverage (see `jenkins/test/`, and the coverage breakdown in
the branch's commit history / PR description). What is **not** covered by
an automated test, and would need manual verification against a real
deployment before trusting the schedule end to end, is:

- The Job DSL / JCasC groovy embedded in `jenkins/jenkins.yaml` actually
  parsing and registering both jobs correctly inside a real Jenkins
  controller (verified here only by parsing the surrounding YAML and
  checking the Groovy against the syntax of the existing, already-working
  job definitions in the same file — there's no Jenkins instance available
  in this environment to boot and register jobs against).
- The real Jenkins REST API response shape for this specific Jenkins/
  plugin version (`lib/jenkins-api.js` is tested against a hand-built fake
  response matching the documented `tree=` projection, not a captured
  response from a live controller).
- A real `docker system prune` invocation against the real bind-mounted
  host socket (`lib/retention-policy.js`'s command construction and output
  parsing are unit tested; `prune-docker-cache.js`'s orchestration is
  integration-tested against a fake CLI, not a live Docker daemon).

Recommended manual check after deploying: trigger both jobs once by hand
from the Jenkins UI, confirm the console output and
`prune-history.jsonl` look as documented above, then let the schedule take
over.
