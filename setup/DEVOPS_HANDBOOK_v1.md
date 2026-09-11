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
Work originates in Jira. The Engineering Lead agent interprets tickets and breaks them into subtasks. Nothing gets built that doesn't trace back to a ticket.

**3. Build Once, Promote**
Artifacts built in a project container are promoted through environments (dev → staging → production), never rebuilt. What passes tests is exactly what gets deployed.

**4. Humans Gate Quality, Jenkins Gates Correctness**
Tests are an automated gate — Jenkins enforces them without exception. Human review happens in the dev environment, not in the PR. When the human moves a ticket to Done, they are saying "this is correct" — Jenkins then promotes it. Production promotion is always a deliberate human action.

---

## CI/CD Architecture

### The Complete Picture

Each project lives entirely on its own droplet. There is no shared CI/CD infrastructure between projects.

```
┌──────────────────────────────────────────────────────────────┐
│ Atlassian Cloud (Jira)                                       │
│  - Tickets created / updated                                 │
│  - ScrumMaster routes work to dev agents                     │
│  - Human creates a Release ticket → ScrumMaster → Jenkins    │
│  - Human moves Release ticket to Done → ScrumMaster → Jenkins│
└─────────────────┬──────────────────────────────────────────┬─┘
                  │ Webhook / API                             │ Release created / Done
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
│  │  - Posts failures back to Jira       │                   │
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
│   ├── jenkins.yaml          # JCasC — credentials, Jira config, job DSL
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
`setup/JenkinsConfig.md` §6), because those are triggered by a Jira Release
ticket batching work across many projects' pipeline runs, not by anything
project-specific.

### GitHub Webhook Configuration

Registered automatically by `init-repo.sh` for each repo. For manual registration or verification:

1. Go to repository → Settings → Webhooks
2. Confirm a webhook exists pointing at `http://<droplet-ip>:8080/github-webhook/`
3. Content type: `application/json`
4. Events: **Pull requests** + **Pushes**

---

## Jira Integration

### Connection Setup

In Jenkins: Manage Jenkins → System → Jira

| Field | Value |
|-------|-------|
| Site name | `ai-gang-jira` |
| URL | `https://your-org.atlassian.net` |
| Credentials | API token (stored in Jenkins credentials store) |

Generate a Jira API token at: `https://id.atlassian.com/manage-profile/security/api-tokens`

### Ticket → Branch Naming Convention

Branch names must encode the Jira ticket key so the pipeline can update the right ticket automatically.

**Convention:**
```
feature/GANG-42-short-description
bugfix/GANG-99-fix-login-redirect
chore/GANG-7-update-dependencies
```

The Jenkinsfile extracts the ticket key (`GANG-42`) from the branch name and uses it to post status updates back to Jira.

### Jira Workflow States

```
Backlog → In Progress → In Review → Done
           ↑                ↑           ↑
     Scrum Master      Jenkins merges  Human accepts on beta
     assigns subtask   to dev,         (no promotion fires —
                       auto-deploys    beta already has it)
                       to beta
```

| Transition | Who / What |
|---|---|
| Backlog → In Progress | Scrum Master (assigns to dev agent) |
| In Progress → In Review | Jenkins (on test pass, merge to `dev`, and automatic deploy to beta) |
| In Review → In Progress | Jenkins (on test failure — posts comment first) |
| In Review → Done | Human (reviewing the change on the **Beta VM**) — means "accepted on beta," not a promotion trigger |

Story/Sub-task Done never triggers Jenkins. Production only moves via a
separate Release ticket type — see The Full Lifecycle and Production
Promotion below, and the release-workflow design for the full
design.

---

## The Full Lifecycle

### End-to-End Flow

```
1. TICKET CREATED
   Human creates Jira ticket (e.g. GANG-42: "Add password reset flow")
   ScrumMaster assigns to Refinement Agent

2. REFINEMENT AGENT
   - Reads ticket
   - Decomposes into subtasks (backend API, frontend UI, tests)
   - Sets Agent field on each subtask
   - Moves subtasks to Shovel Ready

3. SCRUMMASTER
   - Receives Shovel Ready webhook from Jira
   - Fetches full ticket context from Jira API
   - Constructs prompt including agent definition path
   - Durably dispatches to the agent's Redis Stream (aigang:agent:{project}:{suffix})

4. SPECIALIST AGENTS WORK
   - Container's Streams consumer (setup/subscriber.js) invokes Claude Code with prompt
   - Agent reads definition file from /agent-docs
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
   - Posts comment to Jira ticket with failure details (ticket stays In Progress)
   - Publishes a pipeline_retry message to ScrumMaster, which redispatches
     the ticket's recorded owner

   ON SUCCESS (PR build):
   - Queues the PR for auto-merge; GitHub squash-merges it into dev as soon
     as this build's status lands (i.e. after the build has ended)

   ON SUCCESS (dev build, triggered by that merge):
   - Re-runs install/test/build on dev's merged tip
   - Promotes that exact commit to beta (fast-forward)
   - Deploys beta's new build to the Beta VM automatically — no human step
   - Comments the Beta VM URL + commit SHA onto every ticket whose PR landed
     since beta was last promoted, then moves each to In Review

6. HUMAN REVIEW
   - Human reviews the change on the Beta VM
   - If satisfied: moves Jira ticket to Done — this means "accepted on beta,"
     full stop. It does not trigger production promotion.

7. CUTTING A RELEASE (batched, deliberate — see release-workflow.md)
   - When enough has accumulated on beta, a human creates a Jira Release
     ticket (Target Project required)
   - ScrumMaster checks beta's queue is clean, then triggers the
     release-candidate Jenkins job
   - Jenkins pins beta's SHA, cuts release/<sha>, opens the frozen
     release/<sha> → prod PR, deploys a private SHA-pinned preview to the
     Beta VM, and posts the preview link back to the Release ticket

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
dev        ← dev environment, auto-deployed on test pass
  └── feature/GANG-42-password-reset   ← one branch per ticket
  └── bugfix/GANG-99-fix-login         ← bug fixes same pattern
  └── chore/GANG-7-deps-update         ← maintenance work

release/<sha>  ← cut from beta's HEAD when a Release ticket is created;
                 frozen — never receives further commits; deleted once
                 its PR merges to prod
```

Feature branches are short-lived and trace to a Jira ticket. They are deleted after merge to `dev`. The `dev`, `beta`, and `prod` branches are permanent and never deleted.

### Jenkinsfile Branch Gates

Jenkins pipeline behavior differs by trigger source. The first row runs
inside each project's own Jenkinsfile; the other two are the centrally
defined jobs in `jenkins/jenkins.yaml`, triggered by ScrumMaster from a Jira
Release ticket, not by any branch event:

| Trigger | Pipeline | On Success | On Failure |
|---|---|---|---|
| PR opened/updated against `dev` | Install, Test, Build, then queue auto-merge to `dev` | GitHub merges once the build's status lands; ticket stays In Progress | Comment on Jira ticket, `pipeline_retry` to ScrumMaster |
| Push to `dev` (the merge above) | Install, Test, Build, then promote to beta + deploy | Comment Beta URL + SHA on each merged ticket, move Jira → In Review | Comment on each affected ticket, `pipeline_retry` to ScrumMaster |
| Release ticket created (`release-candidate` job) | Pin SHA, cut `release/<sha>`, open frozen PR, deploy preview | Comment preview link + SHA, move Release ticket → In Review | Comment failure on Release ticket |
| Release ticket → Done (`production-promote` job) | Merge frozen PR, redeploy same artifact, teardown preview | Comment confirmation on Release ticket | Comment failure — frozen PR stays open for manual merge |

Nothing here re-tests: the dev pipeline is the only quality gate. What
reaches the Beta VM, the preview, and production is exactly the artifact
that passed there — built once, never rebuilt.

---

## Production Promotion

### Release-Ticket Promotion (beta → release/<sha> → prod)

Full design: the release-workflow design. Summary:

1. Human creates a Jira Release ticket (Target Project required) once enough
   has landed on beta
2. Jenkins' `release-candidate` job checks beta's queue is clean, pins
   beta's HEAD as the candidate SHA, cuts `release/<sha>`, opens the
   `release/<sha> → prod` PR, and deploys a private SHA-pinned preview to
   the Beta VM — link posted back to the ticket
3. Human opens the preview, reviews the exact candidate, and moves the
   Release ticket to **Done**
4. Jenkins' `production-promote` job merges the frozen PR (squash — no merge
   commit pulling in unrelated history) and redeploys that same
   already-built artifact to production, never rebuilding

No test gate is applied at promotion time — the dev pipeline already
validated this exact artifact. Production promotion is a deliberate human
decision, executed entirely through Jira; the frozen PR stays open and
mergeable the whole time in case a human wants to inspect or merge it
manually instead.

### Reference Point for Rollback

`release/<sha>` (kept around after merge, not deleted) is the reference
point for rollback — it names the exact commit that shipped, with a PR
history showing exactly what it contained. A separate version-tag scheme is
not needed for the hosted web/server lane; desktop production builds are
tagged separately for GitHub Releases distribution, see
the desktop-app-support design.

---

## Rollback Procedures

### When to Roll Back

Roll back production when a deployment causes an incident and a forward fix cannot be shipped quickly. Rolling back is always preferable to leaving a broken deploy live.

### Standard Rollback: Revert via a New Release

`prod`'s branch protection (see `setup/JenkinsConfig.md` §7) requires every
change to go through a PR merged by Jenkins' own credential — there is no
path for a direct `git push origin prod`, on purpose, so rollback goes
through the same Release-ticket mechanism as any other promotion, not around it:

```bash
# Identify the bad commit and revert it on beta (or dev, if beta has already
# moved on — either way this becomes a normal merge into beta)
git checkout beta
git revert <bad-commit-hash>
git push origin beta
```

Then create a new Jira Release ticket as usual. Its candidate SHA will
include the revert; review the preview, move it to Done, and
`production-promote` ships the reverted state — no manual Jenkins step,
no direct push to `prod`.

### Emergency Rollback: Redeploy a Previous `release/<sha>`

If you need to restore production immediately without waiting for a new
Release ticket to work through the normal flow, redeploy a previous
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
2. Open a Jira bug ticket documenting what failed and why
3. Assign to the relevant agent for a proper fix
4. Do not re-promote staging to production until the fix has been validated on staging

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

```bash
# Verify credentials in Jenkins
# Manage Jenkins → Credentials → check Jira API token exists

# Test Jira connection
# Manage Jenkins → System → Jira → Test Connection

# Check branch name matches convention
# Branch must contain a valid Jira key e.g. GANG-42
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
✅ Jira integration — connection, workflow automation rules
✅ Deployment credentials — stored securely in Jenkins
✅ Pipeline failures — triage and resolution

### What the DevOps Agent Does NOT Own

❌ Project application code (specialist agents own this)
❌ Jira ticket content (Engineering Lead agent owns this)
❌ Production platform configuration (cloud engineering agent owns this)

### Key Files

| File | Location | Purpose |
|------|----------|---------|
| `Dockerfile` | `~/ai-gang/jenkins/` | Canonical Jenkins image template |
| `docker-compose.yml` | `~/ai-gang/jenkins/` | Canonical Jenkins container config template |
| `jenkins.yaml` | `~/ai-gang/jenkins/` | JCasC — credentials, Jira, job DSL |
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
