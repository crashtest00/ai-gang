# Jenkins Configuration Log

Setup checklist and configuration record for the AI Gang Jenkins master. Work through each section in order. Check off steps as completed.

**Reference:** `DEVOPS_HANDBOOK_v1.md` for architectural context.

---

## 1. Jenkins Master Container

Handled by `scripts/init-jenkins.sh`. Prerequisites before running:

- `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `GITHUB_TOKEN` set in `~/ai-gang/.env`
- `VERCEL_TOKEN` set in `~/ai-gang/.env` (optional — can be added later)
- Docker and `docker compose` installed on the droplet

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
- **Generic Webhook Trigger** — receives Jira Done events from ScrumMaster
- **Docker Pipeline** — runs pipeline steps inside project containers
- **GitHub** — PR webhook integration and status reporting
- **Jira** — ticket status updates and comments from pipeline
- **Credentials Binding** — injects secrets into pipeline steps
- **Configuration as Code** — JCasC config applied on container start
- **Job DSL** — `release-candidate`, `production-promote`, and `release-preview-teardown` jobs created on first boot (see §6); `jenkins-cache-retention-nightly` and `jenkins-disk-usage-sweep` jobs also created on first boot (see §10)

---

## 3. Credentials

Injected automatically via JCasC (`jenkins/jenkins.yaml`) from environment variables in `~/ai-gang/.env`. No manual Jenkins UI step needed.

| Credential ID | Env var | Purpose |
|---|---|---|
| `jira-api-token` | `JIRA_API_TOKEN` | Jira plugin auth |
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

## 4. Jira Integration

Configured automatically via JCasC from `JIRA_URL` and `JIRA_API_TOKEN` in `~/ai-gang/.env`.

- [ ] Verify: **Manage Jenkins → System → Jira → Test Connection** shows success
  - If it fails, check `JIRA_URL` format (must include `https://`) and that `JIRA_API_TOKEN` is valid

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

## 6. Release Flow (dev → beta automatic, beta → prod via Release ticket)

Earlier iteration: a single `jira-done-promote` job fired on *every* Done
transition and promoted `dev → beta` per ticket. That doesn't scale — N
stories means N promotions and N approvals — so promotion is now split into
the three jobs described below (`dev → beta` automatic, `release-candidate`,
and `production-promote`), batching many tickets' work into one release
approval. There is no Jira automation rule driving any of it — ScrumMaster
calls Jenkins directly in every case, same as before, just via these
different jobs.

### dev → beta (automatic, no Jira involvement)

Handled entirely inside each project's own per-project Jenkinsfile (see
`setup/Jenkinsfile.template`) — not by any Jira transition, and not by a
separate Jenkins job. It is two builds of that one file, because the PR
build *is* the status check that unlocks the merge, and GitHub only merges
after that build has ended:

1. **PR build** (`PR-N`): Install → Test → Build, then `gh pr merge --auto`.
   GitHub squash-merges into `dev` the moment the build's final status lands.
   Nothing beta- or Jira-status-related happens here; the ticket stays
   In Progress.
2. **`dev` build**, triggered by the push that merge makes: Install → Test →
   Build on the merged tip, fast-forward `beta` to exactly that commit,
   deploy it to the Beta VM, comment the Beta URL + SHA + build identifier
   onto every ticket whose PR landed since `beta` was last promoted (found
   via GitHub's commit → PR association over `origin/beta..dev`), then move
   each of those tickets to In Review.

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

ScrumMaster's `handleReleaseRequested` (fired on `jira:issue_created` when
`issuetype === 'Release'`) runs the beta-queue-clean check itself, then
triggers this job:

```
POST http://<JENKINS_URL>/generic-webhook-trigger/invoke?token=release-candidate
Body: { "issueKey": "REL-3", "projectName": "hello-world" }
```

The job pins `beta`'s current SHA as the candidate, cuts `release/<sha>`,
opens the `release/<sha> → prod` PR, deploys a SHA-pinned preview container
to the Beta VM (reusing the image already built there — no rebuild), writes
Candidate SHA / Build Identifier / Preview URL onto the Release ticket, posts
a summary comment, and moves the ticket to **In Review**.

For a desktop-lane project (detected by the presence of
`.github/workflows/build-desktop.yml` in the checked-out repo — no separate
config needed), the job also dispatches `build-desktop.yml` for the pinned SHA
via `jenkins/scripts/trigger-native-build.sh` and includes the resulting build
link and status in the same Jira comment as the web preview. See Desktop App
Support. A native build
failure never fails this job — it's supplementary to the web preview.

- [ ] Verify job exists: Jenkins UI → `release-candidate`
- [ ] `BETA_VM_HOST` set in `~/ai-gang/.env` and passed through
      `jenkins/docker-compose.yml`
- [ ] `PREVIEW_DOMAIN` set in `~/ai-gang/.env` (see `scripts/setup-cloudflare-tunnel.sh`)
- [ ] `JIRA_CANDIDATE_SHA_FIELD_ID`, `JIRA_BUILD_IDENTIFIER_FIELD_ID`,
      `JIRA_PREVIEW_URL_FIELD_ID` set (via `scripts/create-release-fields.sh`)
      and passed through `jenkins/docker-compose.yml`
- [ ] Beta VM remote-deploy mechanism installed — see `beta-vm/README.md`

### Release ticket Done → production (`production-promote` job)

ScrumMaster's `handleDone` now branches on issue type: a Story/Sub-task Done
is a no-op (beta already has the code); a Release Done triggers this job —
the single production-approval gate:

```
POST http://<JENKINS_URL>/generic-webhook-trigger/invoke?token=production-promote
Body: { "issueKey": "REL-3", "projectName": "hello-world", "candidateSha": "abc1234..." }
```

The job merges the frozen `release/<sha> → prod` PR (squash, no merge
commit), redeploys the *same already-built* artifact to the Production VM —
never rebuilding — and tears down the preview.

For a desktop-lane project, the job also tags and pushes the next `vX.Y.Z`
at the approved SHA via `jenkins/scripts/tag-desktop-release.sh` — this push
is what triggers `release-desktop.yml` on GitHub, which builds and publishes
the cross-platform GitHub Release. Unlike the native-build stage above, a
failure here fails the job (the tag *is* the desktop production artifact),
and is reported through the same failure Jira comment as any other
production-promote failure.

- [ ] Verify job exists: Jenkins UI → `production-promote`
- [ ] `PROD_VM_HOST` set in `~/ai-gang/.env` and passed through
      `jenkins/docker-compose.yml`
- [ ] `prod` branch protection restricts merges to this frozen PR (see §7)

### Release ticket abandoned → preview teardown (`release-preview-teardown` job)

Fired by `handleReleaseAbandoned` when a Release ticket's resolution is set
to **Abandoned** without shipping:

```
POST http://<JENKINS_URL>/generic-webhook-trigger/invoke?token=release-preview-teardown
Body: { "issueKey": "REL-3", "projectName": "hello-world" }
```

- [ ] Verify job exists: Jenkins UI → `release-preview-teardown`

### End-to-end test

- [ ] Move a Story ticket's PR through: merge → beta auto-deploys → comment
      posted → move ticket to Done (no promotion fires)
- [ ] Create a Release ticket with an open Story still `In Review` on that
      project → confirm it blocks with a comment naming the ticket
- [ ] Resolve that Story, re-create the Release ticket → confirm a preview
      link, SHA, and build identifier land on the ticket and it moves to
      In Review
- [ ] Move the Release ticket to Done → confirm the frozen PR merges and
      production redeploys the previewed artifact
- [ ] For a desktop-lane project (e.g. `hello-desktop`): confirm the release
      candidate's Jira comment includes a native build link/status, and that
      moving its Release ticket to Done pushes a `vX.Y.Z` tag and produces a
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
- [ ] Confirm the ticket receives a comment with the Beta VM URL and commit SHA
- [ ] Move the ticket to Done → confirm nothing fires (acceptance only)
- [ ] Create a Release ticket targeting this project → confirm `release-candidate`
      fires, a preview link lands on the ticket, and it moves to In Review
- [ ] Move the Release ticket to Done → confirm `production-promote` fires,
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
