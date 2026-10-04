# AI Gang - DevOps Handbook
## Continuous Integration & Delivery for Containerized Workspaces

**Purpose**: Reference guide for the AI Gang DevOps agent. Covers CI/CD architecture, tooling decisions, and operational procedures for maintaining the AI Gang development lifecycle.

**Date**: March 19, 2026
**Version**: 1.0 - Jenkins CI/CD Foundation

---

## Table of Contents
1. [DevOps Philosophy](#devops-philosophy)
2. [CI/CD Architecture](#cicd-architecture)
3. [Jenkins Setup](#jenkins-setup)
4. [Jira Integration](#jira-integration)
5. [The Full Lifecycle](#the-full-lifecycle)
6. [Troubleshooting](#troubleshooting)

---

## DevOps Philosophy

### Guiding Principles

The AI Gang DevOps layer is designed around the same core value as the development layer: **isolation without friction**. Each project container is self-contained during development; the CI/CD pipeline must honor that boundary while still providing centralized visibility and control.

Key principles:

**1. One Project, One Droplet**
Each project runs on its own droplet with its own Jenkins instance. There is no shared CI/CD infrastructure between projects. The `ai-gang` repo is the canonical source — cloning it on a fresh droplet and running the bootstrap scripts is all that is needed to stand up a fully working project. Zero external dependencies on other projects or shared services.

**2. Tickets Drive Everything**
Work originates as a canonical work item in Django/`core`, the platform's record of all work. The Refinement Agent interprets work items and breaks them into subtasks. Nothing gets built that doesn't trace back to a work item.

**3. Build Once, Promote**
Artifacts built in a project container are promoted through environments (beta → production), never rebuilt. What passes tests is exactly what gets deployed.

**4. Humans Gate Quality, Jenkins Gates Correctness**
Tests are an automated gate — Jenkins enforces them without exception. Human review happens on the Beta VM, where a merge to `dev` is deployed, not in the PR. When the human marks a work item Done, they are saying "this is correct" — Jenkins then promotes it. Production promotion is always a deliberate human action.

---

## CI/CD Architecture

### The Complete Picture

Each project lives entirely on its own droplet. There is no shared CI/CD infrastructure between projects.

```
┌──────────────────────────────────────────────────────────────┐
│ Django/core — the canonical work record                      │
│  - Work items created / updated                              │
│  - ScrumMaster reads core’s events, routes work to dev agents│
│  - Human creates a Release work item → ScrumMaster → Jenkins │
│  - Human moves the Release to done → ScrumMaster → Jenkins   │
│  Jira is optional, off for a new project. Where one is in    │
│  Jira mode, core — never ScrumMaster — is its only client.   │
└─────────────────┬──────────────────────────────────────────┬─┘
                  │ Canonical events / commands               │ Release requested / done
                  ↓                                          ↓
┌─────────────────────────────────┐   ┌────────────────────────────────────┐
│ GitHub                          │   │ Project Droplet — Jenkins          │
│  - Branch created per ticket    │   │  - release-candidate: pins beta's  │
│  - Dev agent commits code       │   │    SHA, cuts release/<sha>, opens  │
│  - PR opened → triggers Jenkins │   │    the frozen PR, deploys preview  │
└────────────────┬────────────────┘   │  - production-promote: merges the │
                                       │    frozen PR, redeploys the same  │
                                       │    artifact                       │
                                       └────────────────────────────────────┘
                 │ Webhook (PR events)
                 ↓
┌─────────────────────────────────────────────────────────────┐
│ Project Droplet (one per project)                           │
│                                                             │
│  ┌──────────────────────────────────────┐                   │
│  │ Jenkins Container                    │                   │
│  │  - Config generated from ai-gang     │                   │
│  │  - Receives GitHub PR webhooks       │                   │
│  │  - Auto-merges on green tests        │                   │
│  │  - Reports failures to core, no Jira │                   │
│  └──────────────┬───────────────────────┘                   │
│                 │ Executes inside project container          │
│                 ↓                                           │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ Project Containers (fe, be, etc.)                    │   │
│  │  ├── Install dependencies                            │   │
│  │  ├── Run tests                                       │   │
│  │  └── Build artifact                                  │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │ ScrumMaster + Redis                                  │   │
│  └──────────────────────────────────────────────────────┘   │
└────────────────┬────────────────────────────────────────────┘
                 │ Deploy on merge (automatic)
                 ↓
              [dev] ──ff──▶ [beta] ──Release ticket──▶ [prod]
                            (every merge)   (candidate cut, then
                                              Done = production-promote)
```

### Why Jenkins Per Project

| Approach | Verdict |
|----------|---------|
| Shared Jenkins across projects | ❌ Creates cross-project dependency — one droplet failure affects all projects |
| **Jenkins per project droplet** | ✅ **Chosen** — fully self-contained, zero external dependencies |

The core constraint is that each project must be bootstrappable from a fresh droplet by cloning `ai-gang` and running the init scripts. A shared Jenkins would break that guarantee. Jenkins config lives in `ai-gang` as the canonical template and is generated into each project at bootstrap time — the codebase stays DRY but each deployment is independent.

### Repo Count: Decide at Project Initiation

> **Architectural decision required before running `init-project.sh`.**

A project may have one repo (`hello-world`) or multiple (`hello-world-fe`, `hello-world-be`, `hello-world-ext`). This decision affects how many times `init-repo.sh` must be run and how the Jenkinsfile is structured. It is difficult to change mid-project without rework.

- **Single repo** — run `init-repo.sh` once after `init-project.sh`
- **Multiple repos** — run `init-repo.sh` once per repo; each gets its own branches, branch protection, GitHub webhook, and Jenkinsfile

---

## Jenkins Setup

### Jenkins Container (Per Project)

Jenkins runs as its own persistent container on the project droplet, alongside the project's app containers, ScrumMaster, and Redis.

**Directory layout:**
```
~/ai-gang/
├── setup/                    # Agent definitions (read-only mounts)
├── jenkins/                  # Canonical Jenkins template (source of truth)
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── jenkins.yaml          # JCasC — credentials, job DSL
│   └── plugins.txt           # Plugins baked into image at build time
├── projects/
│   └── hello-world/          # Generated by init-project.sh
│       ├── jenkins/          # Generated from ~/ai-gang/jenkins/ template
│       │   ├── Dockerfile
│       │   ├── docker-compose.yml
│       │   ├── jenkins.yaml
│       │   └── plugins.txt
│       ├── src/              # Project source (git root)
│       ├── docker-compose.yml
│       └── .env
└── scripts/
    ├── init-project.sh       # Bootstrap: Jira + Jenkins + project folder
    └── init-repo.sh          # Per-repo: branches, protection, webhook, Jenkinsfile
```

Jenkins config is generated from the canonical template at `~/ai-gang/jenkins/` when `init-project.sh` runs. The template is never modified directly for a specific project — edit the template, re-run bootstrap to apply.

**Start Jenkins** (handled automatically by `init-project.sh`):
```bash
# Jenkins is started as part of project bootstrap — not manually.
# To check status after bootstrap:
docker ps | grep jenkins

# Access UI
# http://localhost:8080
```

See `jenkins/Dockerfile`, `jenkins/docker-compose.yml`, and `jenkins/jenkins.yaml` for the canonical config. See `setup/JenkinsConfig.md` for the full setup checklist.

### Jenkins Plugins

Plugins are baked into the Docker image at build time via `jenkins/plugins.txt` — no manual install step. See `jenkins/plugins.txt` for the full list.

### Jenkinsfile (Per Project)

Each project has a `Jenkinsfile` in its root directory, copied from the
canonical template at `setup/Jenkinsfile.template` with its TODO blocks
(install, test, build, deploy-to-Beta-VM) filled in for that project's tech
stack. Jenkins reads this file to know how to build, test, and deploy that
specific project. Don't hand-roll a Jenkinsfile from scratch or from an old
copy of this handbook — the template is the source of truth and already
implements the shared parts (auto-merge to `dev`, promotion to `beta`,
Jira comment/transition on pass or fail).

What the template does NOT cover — release candidates and production
promotion — lives centrally in `jenkins/jenkins.yaml` instead
(`release-candidate`, `production-promote`, `release-preview-teardown`; see
`setup/JenkinsConfig.md` §6), because those are triggered by ScrumMaster from
a canonical Release work item batching work across many projects' pipeline
runs, not by anything project-specific.

### GitHub Webhook Configuration

Registered automatically by `init-repo.sh` for each repo. For manual registration or verification:

1. Go to repository → Settings → Webhooks
2. Confirm a webhook exists pointing at `http://<droplet-ip>:8080/github-webhook/`
3. Content type: `application/json`
4. Events: **Pull requests** + **Pushes**

---

## Jira Integration

**Retired, v5.2 (Canonical Delivery State REQ-06).** Through v5.1, Jenkins
kept its own Jira connection and commented on, and transitioned, the ticket
its branch name named by grepping a Jira-key regex out of it. From v5.2
Jenkins holds no Jira credential and writes to no tracker at all: it
publishes a canonical event (a successful deployment, or a failed build's
promoted pull requests), and `core`'s outbound writer makes the Jira write,
for a project in Jira mode, through v5.1's client. This section now
describes that writer's connection, not Jenkins'.

Jira is optional, per project. A project connects through the `connect_jira`
management command, run in `core`'s environment (`canonical-delivery-state.md`
REQ-10), which holds the client's credentials — `JIRA_URL`, `JIRA_EMAIL` and
`JIRA_TOKEN` in the platform `.env`, carried into `services/core/.env` by
`scripts/startup/derive-env.sh`. Nothing is configured in Jenkins: there is no
Jira plugin, no `jira-token` credential and no JCasC site block in
`jenkins/jenkins.yaml` to set up, and no connection test to run there.

### Branch Naming Convention

Branch names carry a work item's `<reference>` — its `External key` when the
prompt shows one, its `Work item id` otherwise (`setup/agents/devops-agent.md`
§Key Conventions) — not a Jira-key regex. Jenkins extracts no key from a
branch name for any tracker purpose: both sites that used to do so are
removed, not relocated (REQ-01).

### Workflow States

Status lives on the canonical work item in `core`. Where a project is in
Jira mode, `core`'s outbound writer makes the equivalent Jira write and the
canonical status then follows from Jira's webhook — no canonical row changes
before the webhook returns.

```
backlog → in-progress → in-review → done
            ↑               ↑           ↑
     ScrumMaster       core, on a     Human accepts on beta
     dispatches the    beta           (no promotion fires —
     subtask           deployment     beta already has it)
                       Jenkins
                       reports
```

| Transition | Who / What |
|---|---|
| backlog → in-progress | ScrumMaster, on dispatching the subtask to a dev agent — published to `core` as a canonical command, through the writer for a Jira-mode project |
| in-progress → in-review | `core`, on the canonical beta-deployment event Jenkins publishes (no tracker identifier) — through the writer for a Jira-mode project (REQ-04, REQ-05) |
| in-review → done | Human (reviewing the change on the **Beta VM**, or moving the ticket in Jira) — means "accepted on beta," not a promotion trigger |

No component moves a ticket back from `in-review` on a failed build: Jenkins'
failure handler appends the failure comment through `core`'s comment path and
leaves the work item's status as it is (REQ-01). Story/Sub-task done never
triggers Jenkins. Production only moves via a separate Release work item —
see The Full Lifecycle and Production Promotion below for the full flow.

---

## The Full Lifecycle

### End-to-End Flow

```
1. WORK ITEM CREATED
   Human creates a work item in `core` (e.g. "Add password reset flow")
   ScrumMaster assigns to Refinement Agent

2. REFINEMENT AGENT
   - Reads ticket
   - Decomposes into subtasks (backend API, frontend UI, tests)
   - Sets the assigned agent on each subtask
   - Moves subtasks to ready

3. SCRUMMASTER
   - Receives the canonical work-item event from `core` on its events stream
   - Reads the work item's full context from `core` — it never calls Jira;
     Django/`core` is the platform's only Jira client (V5.1 REQ-01)
   - Constructs prompt including agent definition path
   - Durably dispatches to the agent's Redis Stream (aigang:agent:{project}:{suffix})

4. SPECIALIST AGENTS WORK
   - Container's Streams consumer (setup/subscriber.js) invokes Claude Code with prompt
   - Agent reads definition file from this dispatch's own snapshot of
     /agent-docs, not from the mount: every dispatch copies the definitions,
     the role handbooks and the agent commons into a snapshot of its own and
     rewrites the prompt's paths onto it. So editing anything under
     /agent-docs on the host takes effect at the next dispatch and never
     inside a running session — nothing an agent reads changes under it
     mid-session. Each session logs the snapshot's content hash as
     AIGANG_COMMONS_VERSION, which is the record of what it actually read.
   - Creates branch: feature/GANG-42-password-reset
   - Writes code inside project container
   - Opens PR on GitHub

5. JENKINS TRIGGERED (via GitHub webhook on PR open)
   - Reads Jenkinsfile from branch (copied from setup/Jenkinsfile.template)
   - Executes pipeline inside project container:
     ├── Install dependencies
     ├── Run tests
     └── Build artifact

   ON FAILURE:
   - Writes to no tracker. Publishes a `pipeline_retry` message naming the
     promoted pull requests (no ticket key); `core` resolves each to a
     work item, appends the failure comment there through its own comment
     path, and publishes a retry with the canonical work-item id —
     ScrumMaster redispatches that item's recorded owner from it
     (canonical-delivery-state.md REQ-01). The work item's status is left
     as it is

   ON SUCCESS (PR build):
   - Queues the PR for auto-merge; GitHub squash-merges it into dev as soon
     as this build's status lands (i.e. after the build has ended)

   ON SUCCESS (dev build, triggered by that merge):
   - Re-runs install/test/build on dev's merged tip
   - Promotes that exact commit to beta (fast-forward)
   - Deploys beta's new build to the Beta VM automatically — no human step
   - Publishes a canonical event naming the promoted pull requests, the
     deployed SHA, the build identifier and the Beta URL, with no tracker
     identifier; `core` resolves each to a work item, posts the Beta URL
     and commit SHA as its evidence comment, and moves it to In Review —
     through the outbound writer for a Jira-mode project, directly
     otherwise (REQ-04, REQ-05)

6. HUMAN REVIEW
   - Human reviews the change on the Beta VM
   - If satisfied: moves the work item to done — this means "accepted on beta,"
     full stop. It does not trigger production promotion.

7. CUTTING A RELEASE (batched, deliberate)
   - When enough has accumulated on beta, a human creates a Release work
     item in `core` (Target Project required)
   - `core` confirms beta's queue is clean and publishes the release event;
     ScrumMaster then triggers the release-candidate Jenkins job with the
     Release's canonical work item id
   - Jenkins pins beta's SHA, cuts release/<sha>, opens the frozen
     release/<sha> → prod PR, deploys a private SHA-pinned preview to the
     Beta VM, and posts the preview link back to the Release

8. PROMOTION TO PRODUCTION (Release ticket Done — the only approval gate)
   - Human opens the preview, reviews the exact candidate that would ship
   - Human moves the Release ticket to Done
   - Jenkins merges the frozen PR and redeploys that exact already-built
     artifact to production — never rebuilding
   - See Production Promotion section below
```

### Branch Strategy

Each project maintains three long-lived branches:

```
prod       ← production, advanced only by merging a frozen release/<sha> PR
beta       ← beta environment, fast-forwarded from dev on every merge
dev        ← integration branch; a merge here promotes to beta and deploys
  └── feature/GANG-42-password-reset   ← one branch per ticket
  └── bugfix/GANG-99-fix-login         ← bug fixes same pattern
  └── chore/GANG-7-deps-update         ← maintenance work

release/<sha>  ← cut from beta's HEAD when a Release ticket is created;
                 frozen — never receives further commits; deleted once
                 its PR merges to prod
```

Feature branches are short-lived and trace to a work item. They are deleted after merge to `dev`. The `dev`, `beta`, and `prod` branches are permanent and never deleted.

### Jenkinsfile Branch Gates

Jenkins pipeline behavior differs by trigger source. The first row runs
inside each project's own Jenkinsfile; the other two are the centrally
defined jobs in `jenkins/jenkins.yaml`, triggered by ScrumMaster from a
canonical Release work item's release event, not by any branch event:

| Trigger | Pipeline | On Success | On Failure |
|---|---|---|---|
| PR opened/updated against `dev` | Install, Test, Build, then queue auto-merge to `dev` | GitHub merges once the build's status lands; work item stays as it is | No tracker write; `pipeline_retry` names the promoted pull requests, `core` resolves them and comments, ScrumMaster redispatches |
| Push to `dev` (the merge above) | Install, Test, Build, then promote to beta + deploy | Publishes a canonical event naming the promoted pull requests; `core` comments the Beta URL + SHA and moves each resolved work item to In Review | No tracker write; `pipeline_retry` names the promoted pull requests, `core` resolves them and comments, ScrumMaster redispatches |
| Release requested (`release-candidate` job) | Pin SHA, cut `release/<sha>`, open frozen PR, deploy preview | Comment preview link + SHA on the Release work item in `core` | Comment failure on the Release work item |
| Release → done (`production-promote` job) | Merge frozen PR, redeploy same artifact, teardown preview | Comment confirmation on the Release work item | Comment failure — frozen PR stays open for manual merge |

Nothing here re-tests: the dev pipeline is the only quality gate. What
reaches the Beta VM, the preview, and production is exactly the artifact
that passed there — built once, never rebuilt.

---

## Production Promotion

### Release Promotion (beta → release/<sha> → prod)

The release promotion flow:

1. Human creates a Release work item in `core` (Target Project required) once
   enough has landed on beta
2. `core` confirms beta's queue is clean and publishes the release event;
   ScrumMaster triggers Jenkins' `release-candidate` job, which pins
   beta's HEAD as the candidate SHA, cuts `release/<sha>`, opens the
   `release/<sha> → prod` PR, and deploys a private SHA-pinned preview to
   the Beta VM — link posted back to the Release work item
3. Human opens the preview, reviews the exact candidate, and moves the
   Release work item to **done**
4. Jenkins' `production-promote` job merges the frozen PR (squash — no merge
   commit pulling in unrelated history) and redeploys that same
   already-built artifact to production, never rebuilding

No test gate is applied at promotion time — the dev pipeline already
validated this exact artifact. Production promotion is a deliberate human
decision, executed entirely through `core`'s work items; the frozen PR stays open and
mergeable the whole time in case a human wants to inspect or merge it
manually instead.

### Reference Point for Rollback

`release/<sha>` (kept around after merge, not deleted) is the reference
point for rollback — it names the exact commit that shipped, with a PR
history showing exactly what it contained. A separate version-tag scheme is
not needed for the hosted web/server lane; desktop production builds are
tagged separately for GitHub Releases distribution — see
`/agent-docs/DESKTOP_HANDBOOK_v1.md`.

---

## Rollback Procedures

### When to Roll Back

Roll back production when a deployment causes an incident and a forward fix cannot be shipped quickly. Rolling back is always preferable to leaving a broken deploy live.

### Standard Rollback: Revert via a New Release

`prod`'s branch protection (see `setup/JenkinsConfig.md` §7) requires every
change to go through a PR merged by Jenkins' own credential — there is no
path for a direct `git push origin prod`, on purpose, so rollback goes
through the same Release work item mechanism as any other promotion, not around it:

```bash
# Identify the bad commit and revert it on beta (or dev, if beta has already
# moved on — either way this becomes a normal merge into beta)
git checkout beta
git revert <bad-commit-hash>
git push origin beta
```

Then create a new Release work item as usual. Its candidate SHA will
include the revert; review the preview, move it to done, and
`production-promote` ships the reverted state — no manual Jenkins step,
no direct push to `prod`.

### Emergency Rollback: Redeploy a Previous `release/<sha>`

If you need to restore production immediately without waiting for a new
Release work item to work through the normal flow, redeploy a previous
candidate directly using the same restricted deploy path production-promote
uses — this still goes to the Beta VM remote-deploy mechanism
(`beta-vm/deploy/deploy.sh`), not a raw push to `prod`:

```bash
# Find the last known good release branch
git branch -r --list 'origin/release/*' --sort=-committerdate | head -5

# Redeploy that exact artifact to production (reuses the image if it still
# exists on the Beta VM; rebuilds from that exact SHA otherwise)
ssh prod-deploy@$PROD_VM_HOST deploy <project> <last-good-sha>
```

`prod`'s branch tip is intentionally left behind `release/<sha>` until a
proper revert-and-release closes the gap — the emergency path restores
service first; it does not rewrite branch history. Do not force-push `prod`.

### After Any Rollback

1. Confirm production is healthy
2. Open a bug work item documenting what failed and why
3. Assign to the relevant agent for a proper fix
4. Do not re-promote beta to production until the fix has been validated on beta

---

## Troubleshooting

### Jenkins Can't Reach Docker Socket

```bash
# Check Docker socket permissions
ls -la /var/run/docker.sock
```

A build-time `RUN usermod -aG docker jenkins` in the Dockerfile is not enough:
`docker.sock` is bind-mounted from the host, so its group GID is whatever the
*host* assigned it, which almost never matches the GID apt gives the `docker`
group inside the image. The jenkins container resolves this at startup
instead — see `jenkins/docker-entrypoint.sh`, which looks up whichever group
actually owns the socket's GID (creating one if none exists) and adds
`jenkins` to it before dropping privileges and starting Jenkins.

### Webhook Not Triggering Jenkins

```bash
# Check Jenkins is reachable from GitHub
curl http://<droplet-ip>:8080/github-webhook/

# Check firewall allows port 8080
ufw status

# Open if needed
ufw allow 8080
```

### Jira Comments Not Posting

Jenkins holds no Jira credential and makes no Jira call (REQ-06); a missing
Jira comment or transition is `core`'s outbound writer, not Jenkins.

```bash
# Verify core's own Jira credentials — JIRA_URL, JIRA_EMAIL, JIRA_TOKEN —
# are set, non-blank and not a placeholder in the running core container's
# environment (canonical-delivery-state.md REQ-10)
docker exec <core-container> env | grep JIRA_

# Confirm the project is actually in Jira mode and check the webhook
# registration connect_jira requires
docker exec <core-container> python manage.py shell -c \
  "from workitems import project_config; print(project_config.get_mode('<project>'))"

# A failed or missing Jira write is recorded as a webhook failure naming
# the work item and the step (canonical-delivery-state.md REQ-09) — check
# core's WebhookFailure records for the work item before assuming Jenkins
# or the network dropped something
```

### Pipeline Runs in Wrong Container

```bash
# Check Jenkinsfile docker image matches project tech stack
# Node project should use node:22-alpine
# Python project should use python:3.11-slim

# Verify volume mount path in Jenkinsfile args matches project directory
```

### Jenkins Out of Disk Space

```bash
# Check disk usage
df -h /var/lib/docker

# Clean old builds in Jenkins UI
# Manage Jenkins → Workspace Cleanup

# Or prune Docker
docker system prune -a
```

---

## Summary

### What the DevOps Agent Owns

✅ Jenkins master container — health, plugins, credentials
✅ Jenkinsfile templates — maintained and versioned
✅ GitHub webhook configuration — per project
✅ Deployment credentials — stored securely in Jenkins
✅ Pipeline failures — triage and resolution

### What the DevOps Agent Does NOT Own

❌ Project application code (specialist agents own this)
❌ Work item content (the Refinement Agent owns this)
❌ Production platform configuration (cloud engineering agent owns this)

### Key Files

| File | Location | Purpose |
|------|----------|---------|
| `Dockerfile` | `~/ai-gang/jenkins/` | Canonical Jenkins image template |
| `docker-compose.yml` | `~/ai-gang/jenkins/` | Canonical Jenkins container config template |
| `jenkins.yaml` | `~/ai-gang/jenkins/` | JCasC — credentials, job DSL |
| `plugins.txt` | `~/ai-gang/jenkins/` | Plugins baked into image at build time |
| `init-project.sh` | `~/ai-gang/scripts/` | Bootstrap: Jira + Jenkins + project folder |
| `init-repo.sh` | `~/ai-gang/scripts/` | Per-repo: branches, protection, webhook, Jenkinsfile |
| `Jenkinsfile` | Each repo root (generated) | Per-repo pipeline definition |
| This handbook | `~/ai-gang/setup/` | DevOps agent reference |

---

**Author**: AI Gang - DevOps Team
**Version**: 1.0 - Jenkins CI/CD Foundation
**Date**: March 19, 2026
**Status**: Draft — Jenkins topology decided, implementation pending
