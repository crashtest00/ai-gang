# Jenkins Configuration Log

Setup checklist and configuration record for the AI Gang Jenkins master. Work through each section in order. Check off steps as completed.

**Reference:** the DevOps handbook, for architectural context. `JenkinsConfig.md` stays on the
`/agent-docs` mount (V5.0 Agent Commons Amendment 1, decision 14-2), but the handbook does not: a
dispatched DevOps session reads its own snapshot copy of it, at the path its dispatch context
gives — not `DEVOPS_HANDBOOK_v1.md` beside this file on the mount.

---

## 1. Jenkins Master Container

Handled by `scripts/init-jenkins.sh`. Prerequisites before running:

- `GITHUB_TOKEN` set in `~/ai-gang/.env`
- `VERCEL_TOKEN` set in `~/ai-gang/.env` (optional — can be added later)
- Docker and `docker compose` installed on the droplet

Jenkins itself holds no Jira credential (`canonical-delivery-state.md`
REQ-06): connecting a project to Jira is `core`'s `connect_jira` command,
which reads `JIRA_URL`, `JIRA_EMAIL` and `JIRA_TOKEN` from
`services/core/.env` (REQ-10) — unrelated to this section.

```bash
./scripts/init-jenkins.sh
```

- [ ] Script completes without errors
- [ ] `docker ps | grep jenkins` shows container running
- [ ] UI reachable at `http://localhost:8080`

---

## 2. Plugins

Installed automatically at Docker image build time via `jenkins/plugins.txt`. No manual step needed.

Installed plugins:
- **Generic Webhook Trigger** — receives the release-job invocations ScrumMaster sends, carrying a canonical `workItemId`
- **Docker Pipeline** — runs pipeline steps inside project containers
- **GitHub** — PR webhook integration and status reporting
- **Credentials Binding** — injects secrets into pipeline steps
- **Configuration as Code** — JCasC config applied on container start
- **Job DSL** — `release-candidate`, `production-promote`, and `release-preview-teardown` jobs created on first boot (see §6); `jenkins-cache-retention-nightly` and `jenkins-disk-usage-sweep` jobs also created on first boot (see §10)

No Jira plugin: Jenkins makes no Jira write in any mode — `core`'s outbound
writer does, through v5.1's client (`canonical-delivery-state.md` REQ-06).

---

## 3. Credentials

Injected automatically via JCasC (`jenkins/jenkins.yaml`) from environment variables in `~/ai-gang/.env`. No manual Jenkins UI step needed.

| Credential ID | Env var | Purpose |
|---|---|---|
| `github-token` | `GITHUB_TOKEN` | PR auto-merge (`gh pr merge`), release/prod PRs |

Deploy credentials will be added here once deployment targets are decided.

To rotate a credential: update the value in `~/ai-gang/.env` and restart:
```bash
docker compose -f ~/ai-gang/jenkins/docker-compose.yml restart
```

### `GITHUB_TOKEN` combined permission requirements

One credential, several consumers — this is the authoritative combined list.
Without any one of these, the corresponding step fails silently or with a
generic permissions error, not an obvious one:

| Permission | Classic PAT scope | Fine-grained PAT permission | Needed for |
|---|---|---|---|
| Read/write commit statuses | `repo:status` | Commit statuses: Read and write | PR checks showing up at all (§2.3 above) |
| Merge PRs | `repo` | Pull requests: Read and write | Auto-merge to `dev`; merging the frozen `release/<sha> → prod` PR |
| Trigger workflows | `workflow` | Actions: Read and write | `gh workflow run build-desktop.yml` |
| Push tags | `repo` | Contents: Read and write | Pushing the `vX.Y.Z` release tag on production approval |

`prod` branch protection (§7 below) still requires its status checks and
review rules regardless of token scope — this table only covers what
Jenkins' own credential needs to act at all, not what GitHub then permits it
to do once it tries.

---

## 4. Jira Integration — retired, v5.2

**Nothing to configure here.** Through v5.1 this section documented Jenkins'
own Jira plugin, credential and connection test. `canonical-delivery-state.md`
REQ-06 retires all of it: Jenkins holds no Jira credential and writes to no
tracker in any mode. A project's Jira connection is `core`'s
`connect_jira`/`disconnect_jira` management commands (REQ-10), run in
`core`'s own environment — see `setup/JIRA_SPEC_v1.md` and
`DEVOPS_HANDBOOK_v1.md`'s Jira Integration section for that configuration;
neither is a Jenkins step.

---

## 5. GitHub Webhook (per project repo)

Registered automatically by `scripts/init-project.sh` when a project is created.

For manual registration or verification:
1. Go to repo → **Settings → Webhooks**
2. Confirm a webhook exists pointing at `http://<droplet-ip>:8080/github-webhook/`
3. Events: **Pull requests** and **Pushes**

Repos configured:

- [ ] _(populated by init-project.sh as projects are created)_

---

## 6. Release Flow (dev → beta automatic, beta → prod via a Release work item)

Earlier iteration: a single `jira-done-promote` job fired on *every* Done
transition and promoted `dev → beta` per ticket. That doesn't scale — N
stories means N promotions and N approvals — so promotion is now split into
the three jobs described below (`dev → beta` automatic, `release-candidate`,
and `production-promote`), batching many tickets' work into one release
approval. There is no Jira automation rule driving any of it — ScrumMaster
calls Jenkins directly for a release in either mode, the same way, via these
different jobs: a Jira-mode release event triggers its job exactly as a
local-mode one does, naming only the canonical work-item id
(`canonical-delivery-state.md` REQ-08).

### dev → beta (automatic, with no tracker call of any kind)

Handled entirely inside each project's own per-project Jenkinsfile (see
`setup/Jenkinsfile.template`) — not by any tracker transition, and not by a
separate Jenkins job. It is two builds of that one file, because the PR
build *is* the status check that unlocks the merge, and GitHub only merges
after that build has ended:

1. **PR build** (`PR-N`): Install → Test → Build, then `gh pr merge --auto`.
   GitHub squash-merges into `dev` the moment the build's final status lands.
   Nothing beta-related happens here; the work item's status is left as it
   is.
2. **`dev` build**, triggered by the push that merge makes: Install → Test →
   Build on the merged tip, fast-forward `beta` to exactly that commit,
   deploy it to the Beta VM, then publish a canonical event naming every
   pull request promoted since `beta` was last promoted (found via GitHub's
   commit → PR association over `origin/beta..dev`), the deployed SHA, the
   build identifier and the Beta URL — carrying no tracker identifier.
   `core` resolves each PR to its work item, posts the Beta URL + SHA +
   build identifier as its evidence comment, and moves it to In Review,
   through the outbound writer for a Jira-mode project, directly otherwise
   (`canonical-delivery-state.md` REQ-01, REQ-04, REQ-05).

Because the `dev` build works off `origin/beta..dev`, it copes with several
PRs merging before it gets an executor, with a human merging from the GitHub
UI, and with re-runs (nothing to promote → no-op).

Note: "deploy it to the Beta VM" above assumes a provisioned Beta VM.
Per `docs/release-strategy.md`'s Beta section, a project may instead deploy
that same build as a container on the Development VM as an interim/test
configuration before a Beta VM exists — the deploy step's `sh` command
changes (see `setup/Jenkinsfile.template`'s TODO), but the Install → Test →
Build stages and the Beta URL/SHA comment step do not.

- [ ] `setup/Jenkinsfile.template` copied into the project and its TODO
      blocks (install/test/build/deploy commands) filled in

### beta → release candidate (`release-candidate` job)

`core` runs the beta-queue-clean check itself (`store.py`'s
`transition_status`) before publishing a local-mode release's `requested`
event; a Jira-mode release's `requested` is published by the webhook
consumer instead, on the Release ticket's creation, after the same
canonical beta-queue query (`canonical-delivery-state.md` REQ-04, REQ-08).
Either way ScrumMaster's `handleReleaseRequested` reacts to the event and
triggers this job with the release's canonical work-item id — a Jira-mode
release event triggers the same job the same way a local-mode one does:

```
POST http://<JENKINS_URL>/generic-webhook-trigger/invoke?token=release-candidate
Body: { "workItemId": "8c1d4a7e-3b52-4f09-9a6d-2e7f1b508c43", "projectName": "hello-world" }
```

The job pins `beta`'s current SHA as the candidate, cuts `release/<sha>`,
opens the `release/<sha> → prod` PR, deploys a SHA-pinned preview container
to the Beta VM (reusing the image already built there — no rebuild), writes
Candidate SHA / Build Identifier / Preview URL onto the Release work item in
`core`, and posts a summary comment there.

For a desktop-lane project (detected by the presence of
`.github/workflows/build-desktop.yml` in the checked-out repo — no separate
config needed), the job also dispatches `build-desktop.yml` for the pinned SHA
via `jenkins/scripts/trigger-native-build.sh` and includes the resulting build
link and status in the same comment as the web preview. See Desktop App
Support. A native build
failure never fails this job — it's supplementary to the web preview.

- [ ] Verify job exists: Jenkins UI → `release-candidate`
- [ ] `BETA_VM_HOST` set in `~/ai-gang/.env` and passed through
      `jenkins/docker-compose.yml`
- [ ] `PREVIEW_DOMAIN` set in `~/ai-gang/.env` (see `scripts/setup-cloudflare-tunnel.sh`)
- [ ] Beta VM remote-deploy mechanism installed — see `beta-vm/README.md`

The job reports candidate SHA, build identifier and preview URL to
`core`'s `/admin/work-items/<id>/release-candidate` endpoint, in every
mode (`canonical-delivery-state.md` REQ-06) — not to Jira fields Jenkins
holds itself. A Jira-mode project's `JIRA_CANDIDATE_SHA_FIELD_ID`,
`JIRA_BUILD_IDENTIFIER_FIELD_ID` and `JIRA_PREVIEW_URL_FIELD_ID` (created by
`scripts/create-release-fields.sh`) are read by `core`'s outbound writer
from `services/core/.env`, not by Jenkins; nothing passes them through
`jenkins/docker-compose.yml`.

### Release reaches `done` → production (`production-promote` job)

`core` publishes the release event only for a release work item, so
ScrumMaster's `handleDone` receives an already-resolved `release` and branches
on nothing: it reads the Candidate SHA and triggers this job — the single
production-approval gate. A Story or Sub-task reaching `done` publishes no
release event at all, so no trigger exists for it (beta already has the code):

```
POST http://<JENKINS_URL>/generic-webhook-trigger/invoke?token=production-promote
Body: { "workItemId": "8c1d4a7e-3b52-4f09-9a6d-2e7f1b508c43", "projectName": "hello-world", "candidateSha": "abc1234..." }
```

The job merges the frozen `release/<sha> → prod` PR (squash, no merge
commit), redeploys the *same already-built* artifact to the Production VM —
never rebuilding — and tears down the preview.

For a desktop-lane project, the job also tags and pushes the next `vX.Y.Z`
at the approved SHA via `jenkins/scripts/tag-desktop-release.sh` — this push
is what triggers `release-desktop.yml` on GitHub, which builds and publishes
the cross-platform GitHub Release. Unlike the native-build stage above, a
failure here fails the job (the tag *is* the desktop production artifact),
and is reported through the same failure comment as any other
production-promote failure.

- [ ] Verify job exists: Jenkins UI → `production-promote`
- [ ] `PROD_VM_HOST` set in `~/ai-gang/.env` and passed through
      `jenkins/docker-compose.yml`
- [ ] `prod` branch protection restricts merges to this frozen PR (see §7)

### Release abandoned → preview teardown (`release-preview-teardown` job)

Fired by `handleReleaseAbandoned` when a release work item reaches
`cancelled` without shipping — `core` maps that status to the release event's
`abandoned` kind and publishes it, and the handler tears the preview down:

```
POST http://<JENKINS_URL>/generic-webhook-trigger/invoke?token=release-preview-teardown
Body: { "workItemId": "8c1d4a7e-3b52-4f09-9a6d-2e7f1b508c43", "projectName": "hello-world" }
```

- [ ] Verify job exists: Jenkins UI → `release-preview-teardown`

### End-to-end test

These steps run against the canonical work items in `core`. They are written
against a local-mode project; the same sequence on a Jira-mode project's
Release goes through the ticket in Jira instead of the admin, and through
`core`'s outbound writer (`canonical-delivery-state.md` REQ-09), with one
procedural gap: **there is no abandon procedure for a Jira-mode Release.**
No provisioned screen offers the `Abandoned` resolution, and moving a
Jira-mode Release to Done publishes the `done` release event — which
triggers `production-promote` — whatever resolution it is moved to Done
with (REQ-08; this stage's design notes §4). Left to v5.3
(`../v5.3/features/release-engineer-agent.md` OQ-R1). Until then, abandoning
a release by moving it to `cancelled` (the last checklist item below) is a
local-mode-only procedure.

- [ ] Move a Story's PR through: merge → beta auto-deploys → comment posted →
      move the Story to `done` (no promotion fires, and no release event is
      published for it)
- [ ] With a Story still `in-review` on the target project, move a release work
      item from `proposed` to `in-review` → confirm `core` refuses the
      transition and comments on the release naming the outstanding work items
- [ ] Move that Story to `done`, retry the release's `in-review` transition →
      confirm `core` publishes the `requested` release event, ScrumMaster
      triggers `release-candidate`, and a preview link, SHA and build
      identifier land on the release work item
- [ ] Move the release work item to `done` → confirm `core` publishes the
      `done` release event, the frozen PR merges, and production redeploys the
      previewed artifact
- [ ] Move a release work item to `cancelled` instead (**local mode only** —
      see the warning above) → confirm the `abandoned` release event fires
      `release-preview-teardown`
- [ ] For a desktop-lane project (e.g. `hello-desktop`): confirm the release
      candidate's comment includes a native build link/status, and that moving
      its release work item to `done` pushes a `vX.Y.Z` tag and produces a
      GitHub Release with Windows/macOS/Linux installers attached

---

## 7. Branch Protection (per project repo)

### `dev`

Set via **GitHub → repo → Settings → Branches → Add rule**.

| Setting | Value |
|---|---|
| Branch name pattern | `dev` |
| Require status checks to pass | ✅ |
| Status checks required | `continuous-integration/jenkins/pr-merge` |
| Require branches to be up to date | ❌ (see below) |
| Do not allow bypassing | ✅ |

This ensures Jenkins tests must pass before any PR can merge to `dev`. Two
details that are easy to get wrong:

- The required context must be the one a **PR** build posts,
  `continuous-integration/jenkins/pr-merge`. `continuous-integration/jenkins/branch`
  only comes from a direct build of a branch, and branch discovery excludes
  branches that have an open PR (`jenkins/jenkins.yaml`), so a PR could never
  satisfy it.
- "Require branches to be up to date" (`strict`) must be **off**. Auto-merge
  never updates a PR's branch, so with it on, the second of two queued PRs
  stalls forever once the first lands. The `dev` build re-tests the merged
  tip anyway, which is the guarantee `strict` was meant to give.
- The repo needs **Allow auto-merge** enabled (Settings → General), or
  `gh pr merge --auto` is rejected outright.

```bash
gh api -X PUT repos/OWNER/REPO/branches/dev/protection \
  -F 'required_status_checks[strict]=false' \
  -F 'required_status_checks[contexts][]=continuous-integration/jenkins/pr-merge' \
  -F enforce_admins=true -F required_pull_request_reviews=null -F restrictions=null
gh api -X PATCH repos/OWNER/REPO -F allow_auto_merge=true
```

- [ ] `dev` branch protection configured

### `beta`

No direct pushes — only Jenkins' `github-token` credential pushes here, and
only as a fast-forward from `dev` (see `setup/Jenkinsfile.template`).

| Setting | Value |
|---|---|
| Branch name pattern | `beta` |
| Restrict who can push | Jenkins only |
| Require linear history | ✅ |

- [ ] `beta` branch protection configured

### `prod`

The most restrictive of the three — `prod` only ever changes via the
`production-promote` job merging the frozen `release/<sha> → prod` PR, never
an arbitrary PR.

| Setting | Value |
|---|---|
| Branch name pattern | `prod` |
| Require a pull request before merging | ✅ |
| Require status checks to pass | ✅ (inherited — the dev pipeline already validated this SHA) |
| Restrict who can merge | Jenkins' `github-token` identity only |
| Require branches to be up to date | ✅ |
| Do not allow bypassing | ✅ |

GitHub branch protection can't natively restrict merges to *one specific PR*
(only to *which accounts* may merge) — the `release/<sha>` naming convention
plus the `production-promote` job being the only caller of `gh pr merge`
against `prod` is what keeps this to exactly the frozen candidate in
practice. Configure via `gh api`, passing a real JSON body rather than
`-f`/`-F` bracket flags — `-f` sends every value as a JSON string (so
`enforce_admins=true` becomes the string `"true"`, which the API rejects),
and neither `-f` nor `-F` can express a genuinely empty array, so
`restrictions[teams][]='[]'` sends the literal string `"[]"` as a team name
instead of an empty list — both silently fail the call:

```bash
gh api repos/<org>/<repo>/branches/prod/protection -X PUT --input - <<'EOF'
{
  "required_status_checks": null,
  "required_pull_request_reviews": {"required_approving_review_count": 0},
  "enforce_admins": true,
  "restrictions": {"users": ["<jenkins-bot-github-username>"], "teams": [], "apps": []}
}
EOF
```

`./scripts/init-project.sh` applies this automatically (building the same
body with `jq -n`) — this manual form is only needed if applying protection
by hand.

- [ ] `prod` branch protection configured

---

## 8. Jenkinsfile (per project)

Each project needs a `Jenkinsfile` in its root. See handbook `## Jenkins Setup → Jenkinsfile (Per Project)` for the full template.

Projects with Jenkinsfile in place:

- [ ] `hello-world`
- [ ] _(add as created)_

---

## 9. Smoke Test

End-to-end verification after setup is complete.

- [ ] Create a test branch `feature/GANG-TEST-jenkins-smoke`
- [ ] Open a PR against `dev`
- [ ] Confirm Jenkins pipeline triggers and runs
- [ ] Confirm test pass → PR auto-merges to `dev`, `beta` fast-forwards, and the
      Beta VM redeploys automatically — no Jira transition involved
- [ ] Confirm the work item receives a comment with the Beta VM URL and commit SHA
- [ ] Move the work item to `done` → confirm nothing fires (acceptance only)
- [ ] Create a release work item targeting this project and move it to
      `in-review` → confirm `release-candidate` fires and a preview link lands
      on it
- [ ] Move the release work item to `done` → confirm `production-promote` fires,
      the frozen PR merges, and production serves the previewed SHA

---

## 10. Workspace / Docker Cache Retention (system-level, not per-project)

Two Jenkins system jobs, created automatically by Job DSL on first boot from
`jenkins/jenkins.yaml` (see §2) — not tied to any one project's Jenkinsfile,
since the surfaces they clean (`jenkins-data` workspace volume, host Docker
image/layer cache) are shared across every project's pipeline. They exist
because `buildDiscarder(logRotator(...))` (`setup/Jenkinsfile.template`)
only bounds Jenkins' own build-record history, not on-disk workspace or
Docker cache growth, and unpruned growth on those two surfaces has already
caused one hard build failure from disk exhaustion. Full detail:
[`jenkins/CACHE_RETENTION.md`](../jenkins/CACHE_RETENTION.md).

### `jenkins-cache-retention-nightly` job

Runs nightly (`cron('H 2 * * *')`, i.e. once between 2:00 and 2:59am, exact
minute hashed per job). Two stages:

1. **Prune stale workspaces** — `node jenkins/scripts/prune-workspaces.js
   --trigger=scheduled-workspace`. Removes a job's on-disk workspace once
   it's older than 14 days or beyond the 5 most-recently-used workspaces
   kept per job, whichever comes first.
2. **Prune Docker image/layer cache** — `node
   jenkins/scripts/prune-docker-cache.js --trigger=scheduled-docker`. Runs
   `docker system prune -f --filter until=72h` against the host Docker
   daemon (reached through the bind-mounted socket, `jenkins/docker-compose.yml`).

- [ ] Verify job exists: Jenkins UI → `jenkins-cache-retention-nightly`
- [ ] Trigger it once by hand and confirm the console output reports
      workspaces removed / bytes reclaimed and the Docker prune output

### `jenkins-disk-usage-sweep` job

Runs at least every 30 minutes (`cron('H/30 * * * *')`) — `node
jenkins/scripts/disk-usage-sweep.js`. Checks disk usage on the
`jenkins-data` mount; if usage is at or above the 85% high watermark, it
re-invokes the same two prunes above, in a loop, until usage drops back
below the 70% low watermark or a small iteration cap is hit. This is the
mechanism that actually closes the original disk-exhaustion gap — the
nightly cadence alone can still lose that race if enough builds land
between runs.

- [ ] Verify job exists: Jenkins UI → `jenkins-disk-usage-sweep`
- [ ] A build of this job going **red** means it hit its iteration cap
      without getting back under the low watermark — operator attention
      needed (not enough prunable content to relieve pressure automatically)

### Neither job touches an in-progress build

Both jobs exclude any workspace or Docker image/layer a currently-running
build is using, regardless of age/count/threshold (`selectWorkspacesToPrune()`
in `jenkins/scripts/lib/retention-policy.js` for workspaces; Docker's own
`system prune` semantics without `-a` for images, since that never removes
anything attached to an existing container).

### Where to check current disk usage / prune history

No SSH needed:

- Each job's own Jenkins console output/build history (what was removed,
  bytes reclaimed, and — for the sweep — usage before/after each iteration).
- `/var/jenkins_home/retention/prune-history.jsonl` inside the Jenkins
  container (survives container recreation — lives on the `jenkins-data`
  volume). One JSON line per run from any of the three triggers
  (`scheduled-workspace`, `scheduled-docker`, `threshold-sweep`). Override
  the path with the `RETENTION_LOG_PATH` env var if needed.

- [ ] Confirm `prune-history.jsonl` is populated after the first nightly run

---

## Configuration Record

_Log actual values and decisions here as setup is completed._

| Item | Value | Date |
|---|---|---|
| Droplet IP | | |
| Jenkins version | | |
| Jira org URL | | |
| GitHub org | | |
