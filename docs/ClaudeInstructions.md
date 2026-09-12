# AI Gang - Claude Instructions

**Purpose**: Guide for Claude to walk a human through setting up a new AI Gang project and providing ongoing support. This is an AI-facing document.

**Scope**: From first conversation through initial end-to-end test, plus continuing support for running projects.

**Intended audience**: Claude Code. Steps marked `[HUMAN]` require manual action in a web UI and cannot be performed via script or API.

**Two different repos, don't conflate them**: the **platform repo**
(`ai-gang` — this codebase: ScrumMaster, Jenkins config, setup scripts, agent
definitions) is cloned once per Dev VM before Phase 0 begins. The **project
repo** (e.g. `hello-desktop`) is a separate, brand-new repo created in Phase
3.0 that holds the actual application being built — credentials and
clone/push access for one never carry over to the other.

**Which branch to clone**: per `docs/git-strategy.md`, this repo's permanent
branches are `dev`, `beta`, and `prod`; there is no `main`. `beta` holds a
release candidate under validation and is not for operators. Ask the human
which of the other two they want before cloning:

- `prod` — latest released code. Default choice for most operators.
- `dev` — latest in-progress work, not yet released. Choose this only if you
  specifically need an unreleased feature or fix.

```bash
git clone -b prod https://github.com/crashtest00/ai-gang.git ~/ai-gang   # or: -b dev
```

---

## How to Use This Guide

Read through Phase 0 first — it determines the project topology, which shapes every subsequent step. Phases 1 and 2 have skip conditions; always check them. Phase 3 repeats for each repo in the project. Phase 4 is written per-project based on Phase 0 output.

**Before running any stretch of this unattended** (no human present to answer permission prompts): Claude Code's default (Manual) permission mode prompts on first use of each tool, which stalls with nobody there to approve it. Switch the session to an auto-approving permission mode first, or be prepared to approve prompts individually for the rest of the run. Which mode fits depends on how disposable the environment is — the per-repo container from Phase 3.2 is recreated per project, so `bypassPermissions` (`--dangerously-skip-permissions`) is a reasonable fit there; the Dev VM itself (Phase 2) is always-on and shared, not disposable, so prefer `auto` or `dontAsk` with a pre-approved allowlist (`--allowedTools`) instead. This is the operator's own call to make before starting — nothing in this guide or the graphs it links to generates, writes, or deletes any `.claude/settings*` file on your behalf.

---

## Platform Startup

*This is how AI Gang itself is brought up on a machine. Everything after
it — Phases 0 to 4 — is how a customer project is set up on a running AI
Gang. Platform startup performs that project setup once, unattended, for
the one project its configuration names.*

An operator with Docker installed, an Anthropic API key, and an empty
GitHub repository already created for the project reaches a running AI
Gang in six steps and one command:

1. Clone AI Gang.
2. `cp ai-gang.config.template.json ai-gang.config.json` and fill it in.
3. `cp .env.template .env` and fill it in.
4. `docker compose up` at the repository root, in the foreground.
5. Wait. Initialization streams to the terminal until it finishes.
6. Open Django admin at `http://127.0.0.1:9100/django-admin/` and write
   stories.

There is no command to run between steps 4 and 6.

### What step 4 starts

`docker compose up` builds and starts exactly one container, the AI Gang
container. It is a client of the operator's own Docker daemon, not a host
for a second one: the root `docker-compose.yml` gives it the daemon's
socket and mounts the checkout at the checkout's own host path, with the
working directory to match, so that the per-service compose files'
relative bind mounts resolve on the host. Every other container AI Gang
runs is created by this container's initialization, as a sibling on that
same daemon.

Its entrypoint (`scripts/startup/entrypoint.sh`) validates the
configuration and the environment file before anything else exists, then
runs the Initialization Agent — Claude Code, unsupervised, with
permission prompts bypassed — against the ordered steps below. The
container exits when initialization finishes, and does not restart.

### The ordered steps

Each of these is a script. The agent invokes them in this order; it does
not decide what they do. `scripts/startup/steps.sh` is the list, and is
what the container builds the agent's own instructions from.

| # | Script | What it does |
| --- | --- | --- |
| 1 | `scripts/startup/create-network.sh` | Create the ai-gang Docker network if it does not already exist |
| 2 | `scripts/startup/start-redis.sh` | Start Redis |
| 3 | `scripts/startup/start-work-item-service.sh` | Build and start the work-item service and apply its database migrations |
| 4 | `scripts/startup/create-admin.sh` | Create the Django admin account from .env |
| 5 | `scripts/startup/start-scrummaster.sh` | Build and start ScrumMaster |
| 6 | `scripts/startup/initialize-project.sh` | Initialize the configured project from the configuration |
| 7 | `scripts/startup/install-project-dockerfile.sh` | Install the project container's Dockerfile from its stack's template |
| 8 | `scripts/startup/start-project.sh` | Build and start the project container |
| 9 | `scripts/startup/confirm-health.sh` | Confirm every service is healthy and record the admin address |

Before the first of them, the entrypoint has already run
`scripts/startup/validate-config.sh`, `scripts/startup/validate-env.sh`,
`scripts/startup/config-identity.sh` and `scripts/startup/derive-env.sh`
— validation, the checkout's recorded configuration, and each service's
own environment file derived from the platform `.env`.

### Where judgment belongs

The agent's judgment is for a step that fails, and for nothing else. A
routine step is the script's job.

- Do not re-ask or change the project name, deployment target, stack
  profile or repository URL. They were validated before the run started.
- When a step fails, diagnose it from its own output and from
  `docker logs` / `docker ps`. If the cause is something that can genuinely
  be put right — a transient pull failure, a container that needs another
  moment, a stale container from an earlier run — put it right and re-run
  that same script. Every script is safe to re-run.
- Never build a service by hand. Never write a compose file, a Dockerfile
  or an environment file yourself, and never substitute a different image,
  name or port for the one a script uses. A service that cannot be started
  by its script is a failure to report.
- When it cannot be put right, run
  `./scripts/startup/status.sh fail "<one-line reason>"` and stop. The run
  ends nonzero. That is the correct outcome; a hand-built substitute is
  not.

### Reading a run

Two things in the checkout, outside every container, and readable from a
second shell while the run is in progress:

- `.ai-gang/status.json` — the step in progress, every step's state, each
  service's health, and, on completion, the Django admin address. No
  credential is ever written here.
- `.ai-gang/startup.log` — each step's own progress lines, tailed live to
  the container's stdout. It is not the whole of what the container
  prints there: two banner lines print before that tail starts and are
  gone by the time it does, and the Initialization Agent's own output —
  its `claude --print` transcript — streams straight to the container's
  stdout and is never written to this file.

`.ai-gang/config-identity.json` records the configuration this checkout
was initialized with. Running `docker compose up` again against an
already-initialized checkout verifies the existing services and
reconnects, creating no second project, network, account or container. A
run whose configuration differs from that record is refused before
anything changes.

All three stay in the checkout after the container exits, and nothing in
the flow deletes them — a failed run's record and log are still there
afterwards, and are what a later reader diagnoses it from. Starting
again does not overwrite them either: a new run moves the previous run's
record and log to `.ai-gang/previous/` first. `.ai-gang/startup.log` is
the step log that survives the container, not a full copy of everything
the container printed — see above for what it leaves out.

### Which phases below this flow covers

Included, performed by the steps above: **2.1** (Redis), **2.2**
(ScrumMaster), **3.1** (project initialisation), **3.2** (container
setup), **3.3** (the project map stub `init-project.sh` writes — filling
it in is still the operator's), **3.4** (Claude Code and git access in the
project container, exercised by the end-to-end test below) and **3.5**
(the Redis subscriber, which the project container's own entrypoint
starts on every start).

Not included, and left exactly as they are for an operator to add
afterwards: **Phase 1** (Jira), **2.0** (Cloudflare Tunnel), **2.3**
(Jenkins), **2.4** (security), **2.5** (the Beta VM), **3.6** (the Jenkins
pipeline) and **3.7** (release promotion).

**Phase 3.0 is the operator's prerequisite, not a step of this flow.** The
project's GitHub repository must already exist, empty, before step 2: its
URL is what `repository.url` in `ai-gang.config.json` names.

**Phase 4 is replaced, for this flow, by the Django-admin end-to-end test**
at the end of this document. Phase 4 as written is a Jira scenario, and
Jira is outside this flow.

---

## Phase 0: Project Type Discovery

Before any setup begins, gather enough information to determine the container topology and deployment strategy.

### Questions to ask the human

1. **Project name** — lowercase letters, numbers, and hyphens (e.g. `hello-world`, `my-app`)
2. **How many repos does this project have?**
   - One repo: monorepo, single fullstack app (e.g. Next.js), single service, or MCP suite
   - Two or more repos: separate frontend + backend, separate mobile + API, etc.
   - Let the human know: **they can start with one repo and add more later** — see Continuing Support
3. **For each repo**: what is the tech stack? (Node/React, Python/FastAPI, React Native/Expo, plain HTML, etc.)
4. **For each repo**: what is the deployment target?
   - Web: static hosting, SSR, or container PaaS
   - Mobile: Expo/EAS (cloud build + OTA updates)
   - Desktop app: web frontend plus a native shell
     - Ask the human to choose **Electron** or **Tauri**; do not infer the framework
     - Record the choice in the topology (for example, `deploy target: desktop (Tauri)`)
     - The browser build is the default dev/beta acceptance surface; native Windows,
       macOS, and Linux builds are supplementary validation requested through the
       Release ticket flow
   - Chrome extension: packaged artifact
   - MCP package: published to npm or PyPI
   - Other: reason from first principles (see below)

### Derive the container topology

Apply the one-container-per-repo principle:

- **One repo → one container.** The container subscribes to all agent channels for the project.
- **N repos → N containers.** Each container subscribes only to its own agent channel.

Assign an `AGENT_CHANNEL_SUFFIX` to each container based on its role. Common values: `frontend`, `backend`, `mobile`, `api`, `web`. The suffix must match the agent's `routing.channelSuffix` entry in `agents.json` (the canonical agent catalog).

Every container uses `AGENT_CHANNEL_SUFFIX` — there is no special handling for single-repo projects. A single-repo project sets one suffix (e.g. `AGENT_CHANNEL_SUFFIX=api`).

**Novel project types**: If the project doesn't fit a familiar pattern, derive the topology from the principle. Explain your reasoning to the human before proceeding — e.g. "This is a monorepo with three packages; since it's one repo, it gets one container. The agent suffix will be `packages`." Get confirmation before continuing.

### Record the topology

Write down before moving to Phase 1:

```
Project name: <name>
Jira key: <key>
Repos:
  - <repo-name>: <tech stack>, deploy target: <target>, suffix: <suffix>
  - (repeat)
```

This shapes Phase 3 (one pass per repo) and Phase 4 (E2E test scenario).

---

## Phase 1: Jira Instance Setup

*These steps are scoped to the Jira instance, not the project. Skip any step that is already done for this Jira instance.*

This phase has been converted to graph form —
`setup/graphs/jira-instance-setup.graph.yaml` — covering, as an
`escalation` node (a human's declared preference, not a check on observable
state), whether this AI Gang deployment will connect any project to Jira at
all before doing the instance-level setup below, per
`setup/graphs/migration-status.md`. This is not the same decision as the
per-project Jira-vs-local mode switch, which always defaults to local mode
at project initialization and connects Jira later, separately, per
project. **Walk the graph — do not follow the steps below directly.** They
describe the same underlying procedure for reference only; running them
directly skips the escalation gate above and performs Jira-instance setup
unconditionally, which is exactly the violation this graph exists to
prevent. Only fall back to them manually if the graph engine itself is
unavailable, and note that deviation in the run's log:

### 1.0 Jira Service Account

Create a dedicated Atlassian account for AI Gang. Jira API tokens inherit the full permissions of the account that created them — using a personal account means a departing team member can break the entire pipeline.

- `[HUMAN]` Create a new Atlassian account — e.g. `ai-gang-bot@your-domain.com` (use a shared mailbox or email alias)
- `[HUMAN]` Invite it to your Jira instance: **Jira Settings → User Management → Invite Users**
- `[HUMAN]` Log in as the service account → **Manage Account → Security → Create and manage API tokens** → create a token named `ai-gang-hq`
- `[HUMAN]` Add to `services/scrummaster/.env`:
  ```
  JIRA_USER_EMAIL=ai-gang-bot@your-domain.com
  JIRA_API_TOKEN=<token from above>
  ```
- `[HUMAN]` Also add to `~/ai-gang/.env` (used by `init-project.sh`):
  ```
  JIRA_EMAIL=ai-gang-bot@your-domain.com
  JIRA_TOKEN=<same token>
  JIRA_URL=https://your-org.atlassian.net
  ```

**Note:** Atlassian API tokens have no scope selection — the service account's project role (Member, not Admin) is the scope control.

Verify access:
```bash
source ~/ai-gang/.env
curl -u "$JIRA_EMAIL:$JIRA_TOKEN" "$JIRA_URL/rest/api/3/myself"
# Should return the service account info as JSON
```

### 1.1 Jira Custom Fields

Custom fields are Jira instance-level resources — create them once, their IDs are stable and reused by every project.

```bash
cd ~/ai-gang && ./scripts/create-jira-fields.sh
```

This creates:
- **Agent** (single-select: `refinement-agent`, `frontend-agent`, `backend-agent`, `devops-agent`)
- **Blocked** (single-select: `Yes` / null)
- 7 story schema fields (paragraph type): `Value Hypothesis`, `Test & Measurement`, `Behavior`, `Acceptance Criteria`, `Constraints`, `Edge Cases`, `Out of Scope`
- Writes all 9 `JIRA_*_FIELD_ID` vars automatically to `services/scrummaster/.env`

Verify: `grep JIRA_.*_FIELD_ID ~/ai-gang/services/scrummaster/.env | wc -l` should return `9`.

When adding a new project, apply existing fields via `init-project.sh` — do not recreate them.

---

## Phase 2: VM Infrastructure Setup

*These steps run on every new project VM. All infrastructure is per-project.*

### Prerequisites — gather before starting

- `[HUMAN]` Cloudflare account with your domain added (free tier is fine)
- `[HUMAN]` Cloudflare API token — go to Cloudflare dashboard → **Create Token** → use the **"Edit zone DNS"** template, scoped to your specific zone, plus **Account → Cloudflare Tunnel → Edit** (account resources) so the token can create the tunnel itself via the API (see 2.0 below — no browser login step needed). If you will also set `PREVIEW_SUBDOMAIN` or `BETA_DOMAIN` (enables Cloudflare Access), add the **Account → Access: Apps and Policies → Edit** permission to the same token — the DNS template alone does not cover it, and Access application creation fails without it.
- `[HUMAN]` Zone ID and Account ID — both found on the Cloudflare dashboard overview for your domain (right sidebar)
- `[HUMAN]` Add to `~/ai-gang/.env`:
  ```
  CF_API_KEY=<token from above>
  CF_ZONE_ID=<zone id>
  CF_ACCOUNT_ID=<account id>
  HQ_SUBDOMAIN=aigang.yourdomain.com
  JENKINS_SUBDOMAIN=jenkins.yourdomain.com
  ```

### Docker

- Run `./scripts/install-docker.sh` — installs Docker Engine and the Compose
  plugin via Docker's own convenience script (`get.docker.com`), so this
  never has to track Docker's release process itself. Enables the service
  and adds the current user to the `docker` group; if you were just added,
  log out and back in (or run `newgrp docker`) before continuing.

### 2.0 Cloudflare Tunnel

**The tunnel must be live before Jenkins starts.** Jenkins registers its GitHub webhook at first boot — if the tunnel isn't routing when Jenkins boots, the registration fails silently and must be done manually afterward.

This phase has been converted to graph form —
`setup/graphs/cloudflare-setup.graph.yaml` — covering account/
token presence, tunnel existence, the subdomain-var combinations, and
`CF_ACCOUNT_ID` presence as decision nodes with their own remediation, per
`setup/graphs/migration-status.md`. **Walk the graph — do not follow the
steps below directly.** They describe the same underlying procedure for
reference only; running them directly bypasses the graph's decision and
remediation nodes and its guarantee of no Jira coupling (the script's own
"next steps" message references Jira-webhook auto-registration, which does
not belong to this phase). Only fall back to them manually if the graph
engine itself is unavailable, and note that deviation in the run's log:

- Run `./scripts/setup-cloudflare-tunnel.sh`
  - Creates a named tunnel (`ai-gang`) via Cloudflare's account-scoped
    Tunnel REST API, authenticated by `CF_API_KEY`/`CF_ACCOUNT_ID` —
    **no interactive `cloudflared tunnel login` browser step** (superseded
    the previous manual step this section used to list here)
  - Adds DNS records for HQ and Jenkins subdomains via Cloudflare API
  - Writes `~/.cloudflared/config.yml`
  - Installs and starts `cloudflared` as a systemd service
  - Updates `HQ_URL` and `JENKINS_URL` in `~/ai-gang/.env`
- Verify: `curl https://aigang.yourdomain.com/health` → `{"status":"ok"}`

### 2.1 Redis

```bash
cd ~/ai-gang/services/redis && docker compose up -d
docker compose exec ai-gang-redis redis-cli ping  # should return PONG
```

### 2.2 ScrumMaster

```bash
cp ~/ai-gang/services/scrummaster/.env.example ~/ai-gang/services/scrummaster/.env
# Fill in .env: Jira credentials and WEBHOOK_SECRET
# All JIRA_*_FIELD_ID vars are already written by Phase 1.1 — do not overwrite them
cd ~/ai-gang/services/scrummaster && docker compose up -d
curl http://localhost:9000/health  # should return {"status":"ok"}
```

### 2.3 Jenkins

**Init order matters**: Cloudflare tunnel (Phase 2.0) must be live before running this step.

```bash
./scripts/init-jenkins.sh
```

This builds and starts the Jenkins container, installs plugins, and applies JCasC config. Key behaviors:
- Sources `.env` before `docker compose up` so JCasC env vars (`JIRA_URL`, `JENKINS_URL`, etc.) are available at first boot
- Waits for "Jenkins is fully up and running" in logs before declaring success

**Critical configuration decisions**:

- **Credential type**: Use `usernamePassword`, never `string`, for GitHub PAT credentials. The Git plugin cannot use a bare `string` secret for HTTPS authentication. For a GitHub PAT: `username: x-access-token`, `password: <PAT>`. This is already set correctly in `jenkins.yaml` — do not change it.

- **JCasC live reload**: `POST /configuration-as-code/reload` works for most config changes but cannot change an existing credential's type. If you need to change a credential type, wipe the volume and restart clean: `docker volume rm jenkins_jenkins-data && docker compose up -d`.

- **Required plugins**: `github-branch-source` is required for multibranch pipeline jobs (PR discovery, automatic webhook registration). It is not included in `workflow-aggregator`. Confirm it is in `plugins.txt`.

- **`GITHUB_TOKEN` permissions**: this one credential backs commit-status checks, PR auto-merge, the frozen `release/<sha> → prod` merge, and (for desktop projects) triggering GitHub Actions and pushing release tags. See `setup/JenkinsConfig.md` § GITHUB_TOKEN combined permission requirements for the full, authoritative scope list — do not re-derive it here or elsewhere. Missing the commit-status scope specifically shows up as: builds run and pass but GitHub PRs show no status check, with `Resource not accessible by personal access token` in the console.

After Jenkins is up:

- `[HUMAN]` Verify UI: `curl http://localhost:8080`
- `[HUMAN]` Verify Jira connection: **Manage Jenkins → System → Jira → Test Connection**
- Wire `JENKINS_URL` into ScrumMaster config so ScrumMaster can trigger builds

**Release flow jobs**: `release-candidate`, `production-promote`, and `release-preview-teardown` exist in Jenkins and are ready to receive triggers — ScrumMaster calls them directly (see `setup/JenkinsConfig.md` §6). There is no Jira webhook to configure for any of this: dev → beta deploys automatically on merge (no Jira involvement at all), and the two Release-ticket jobs are called by ScrumMaster's `handleReleaseRequested`/`handleDone`/`handleReleaseAbandoned`, not by a Jira automation rule.

**Jenkinsfile template**: Copy `setup/Jenkinsfile.template` into the project's repo root and fill in its four TODO blocks (install, test, build, deploy-to-Beta-VM) based on the project's tech stack and deployment target. It already implements the test gate, auto-merge to `dev`, and automatic `beta` promotion + deploy — only the project-specific commands are missing.

### 2.4 Security

**Webhook secret validation**:
- `WEBHOOK_SECRET` is auto-generated by `init-project.sh` and stored in `services/scrummaster/.env`
- Embedded in the Jira webhook URL as `?secret=<value>`
- ScrumMaster validates `req.query.secret` on every `/webhook` request (returns 401 on mismatch)

**Network exposure**:
- Port 9000 (ScrumMaster) must be bound to `localhost` only
- Verify: `ss -tlnp | grep 9000` should show `127.0.0.1:9000`, not `0.0.0.0:9000`

**Branch protection** (see `setup/JenkinsConfig.md` §7 for the full settings and `gh api` commands):
- `dev`: require status checks to pass + branch up to date + do not allow bypassing
- `beta`: no direct pushes — Jenkins only, fast-forward from `dev` only
- `prod`: require PR + status checks + restrict merges to Jenkins' `github-token` identity + do not allow bypassing. No required human PR review — the human approval gate is moving the Release ticket to Done, not a GitHub review.

### 2.5 Beta VM Bootstrap

Must be done once per environment before the first project reaches Phase 3.6 (its first Jenkins-driven `dev → beta` deploy).

**Choose Beta's topology first.** The Dev VM itself has no topology choice — it's always the one persistent VM this platform runs on. Beta does: either its own dedicated VM, or a container on the Dev VM instead of a second machine.

- **Beta as its own VM** (documented and scripted below). Per `beta-vm/README.md`, this is a separate machine from the Dev VM — provisioning the box itself (droplet/instance, DNS, base OS) is a human/Cloud Engineer concern outside this repo's scripts, same as the Dev VM.
- **Beta as a container on the Dev VM.** Lighter-weight, no second machine to provision. No script exists for this path yet — it isn't built, not merely undocumented; skip ahead to Phase 3 only once an equivalent to `setup-beta-vm.sh` exists for it.

The rest of this phase assumes the own-VM path; the steps below are that path's, not both.

- `[HUMAN]` The Beta VM provisioned, reachable from the Dev VM over LAN/VPC (see Phase 2.0's `BETA_VM_HOST`)
- `[HUMAN]` SSH access to the Beta VM as an existing sudo-capable user — the script below reuses this once, to install a scoped deploy key and generate its own restricted SSH credential; the sudo-capable access itself never needs to persist afterward
- `[HUMAN]` Add to `~/ai-gang/.env`:
  ```
  BETA_VM_ADMIN_USER=<your existing sudo-capable SSH user on the Beta VM>
  ```
  (`BETA_VM_HOST`, `BETA_VM_TRAEFIK_PORT`, `PREVIEW_DOMAIN`, `BETA_DOMAIN` are already set from Phase 2.0)

```bash
./scripts/setup-beta-vm.sh
```

This creates the unprivileged `beta-deploy` account with rootless Docker, generates a dedicated Jenkins→beta-deploy SSH keypair and installs it in `authorized_keys` with the `command=` forced-command restriction (never an unrestricted shell), creates the `beta` Docker network Traefik's compose file expects to already exist, copies `beta-vm/deploy/*` and `beta-vm/traefik/` to the VM, writes `/opt/beta-deploy/env`, adds a firewall rule scoping Traefik's port to the Dev VM's IP, and starts Traefik.

**Not covered by this script** (still separate steps):
- The read-only GitHub deploy key for `beta-deploy` — per-project, done during Phase 3 for each repo, not part of this one-time bootstrap
- Registering the generated private key (`~/.ssh/beta-deploy-jenkins` by default) as a Jenkins SSH credential — see `setup/JenkinsConfig.md`'s deploy-credentials TODO
- A Prod equivalent — Prod's deploy path isn't fully specified yet. Once it is, Prod's topology choice will be three-way rather than Beta's two: its own dedicated VM, a container on the Dev VM, or — only reachable if Beta above was provisioned as its own VM — a container on the Beta VM. Until any of those paths has a script, repeat the manual `beta-vm/README.md` steps with `prod-deploy`/`PROD_VM_HOST` against whichever host you've chosen

Verify: `curl http://$BETA_VM_HOST:$BETA_VM_TRAEFIK_PORT` reaches Traefik (a 404 is expected — no routes exist until the first `deploy.sh`/`preview-deploy.sh` runs).

---

## Phase 3: Per-Repo Container Setup

*Repeat this phase for each repo identified in Phase 0.*

### 3.0 Create Project Repo

This is a **different repo from the `aigang` platform repo** cloned in Phase
0's prerequisites — a brand-new, empty repo that will hold the actual
application being built. `init-project.sh` (3.1 below) only pushes into an
existing repo, it never creates one.

- `[HUMAN]` Create the repo:
  ```bash
  gh repo create <org-or-user>/<project-name> --private
  ```

### 3.1 Project Initialisation

The supported way to initialize a project whose name, deployment target,
and stack are already decided is `--config`, pointing at a JSON file:

```bash
cd ~/ai-gang && ./scripts/init-project.sh --config <file>
```

Those three decisions come from that file's `project.name`, `project.type`,
and `project.stack` fields. They are validated up front and then bound —
not prompted for, and the agent must not re-ask them or change them once
the file has validated. A runnable example is
`scripts/init-project.example.json`; the currently supported `type`/`stack`
values are published in `setup/graphs/engine/lib/config/catalog.js`.
`GitHub HTTPS URL` and `GH_TOKEN` are still prompted for either way.

Without `--config`, the alternative is the fully interactive flow:

```bash
cd ~/ai-gang && ./scripts/init-project.sh
```

This prompts for: project name, GitHub HTTPS URL, `GH_TOKEN`, and
deployment target. Initializes in **local mode by default** — local mode
is the unconditional default, Jira mode cannot be chosen at init — no Jira
project key is asked for and no Jira API call is made. Deployment-target
boilerplate selection (currently `web` or `desktop`) is also represented
as a graph node — `setup/graphs/deployment-target-boilerplate.graph.yaml`
— which resolves the target from the project's
`.aigang-config-identity.json` file's `type` field when the project was
initialized with `--config`, rather than asking again; an unsupported
target reaches that graph's remediation node (or this script's own
equivalent guidance) rather than an empty, unexplained repository.

Creates:
- `projects/<name>/docker-compose.yml` — network, env file, agent-docs mount
- `projects/<name>/.env` — `PROJECT_NAME`, `REDIS_HOST`, `ANTHROPIC_API_KEY`, `GITHUB_URL`, `GH_TOKEN`
- `projects/<name>/src/CLAUDE.md` — project map stub
- GitHub branches `dev`, `beta`, `prod` with branch protection rules

Pass `--connect-jira` to additionally prompt for a Jira project key and
perform one-time Jira-instance bootstrapping for this project (creates the
Jira project, the AI Gang Kanban workflow, and applies custom fields to its
screens) — this is always an explicit, separate opt-in, never offered by
the default flow above.

**Before running this**: `JENKINS_GITHUB_USER` must be set in `~/ai-gang/.env` (the platform `.env`, not the project's) — see `.env.template`. Without it, `dev` branch protection is still applied but `beta`/`prod` protection is skipped with a warning. Also, applying branch protection at all requires the `GH_TOKEN` you provide here to include **Administration: Read and write** on top of its Contents/Pull requests/Metadata scopes (see 3.2 below) — without it, the branch-creation step still succeeds but each protection call gets a `403` and prints a warning to configure it manually per `setup/JenkinsConfig.md` §7.

**Manual step after init, only if `--connect-jira` was used**:
- `[HUMAN]` In Jira: **Project Settings → People → Add Member** — add the service account with role Member
- `[HUMAN]` Configure board columns: **Board Settings → Columns** — add Shovel Ready and In Review, remove Selected for Development

### 3.2 Container Setup

```bash
cd ~/ai-gang/projects/<name>
cp ~/ai-gang/Docker\ Templates/Dockerfile-node.template ./Dockerfile   # or python/tauri template
```

Which user-management syntax the chosen template needs (Alpine's
`adduser`/`deluser` vs. Debian/Ubuntu's `useradd`) is a base-image-family
branch point, also represented as a graph —
`setup/graphs/base-image-family.graph.yaml` — walkable against a
Dockerfile to confirm which family it's in before customising it further.

Customise the Dockerfile for the repo's tech stack, then:

```bash
docker compose build && docker compose up -d
docker ps  # verify running
```

**Each container's `.env` must have**:
- `ANTHROPIC_API_KEY` — Claude Code auth
- `PROJECT_NAME` — must match the project name used at 3.1 (and the Jira project name, if `--connect-jira` was used)
- `REDIS_HOST=ai-gang-redis`
- `AGENT_CHANNEL_SUFFIX=<role>` — e.g. `backend`, `frontend`, `mobile`, `api`
- `GH_TOKEN` — fine-grained PAT scoped to this repo only, needing Contents/Pull requests/Metadata read-write plus **Administration: Read and write** (the last one is only needed at Phase 3.1's branch-protection step, above — a token missing it still works for everything else)

For multi-repo projects, each container also needs its own agent definition mounts in `docker-compose.yml` (mount only the definitions relevant to this repo's role).

### 3.3 Project Map (CLAUDE.md)

`init-project.sh` generates a stub at `projects/<name>/src/CLAUDE.md` (visible inside the container as `/workspace/CLAUDE.md`). Fill it in before the first agent run.

- `[HUMAN]` Complete each section:
  - **Framework / Runtime** — e.g. `Node 22 + Express`, `plain HTML/CSS`, `React + Vite`
  - **Key Directories** — what lives where
  - **Entry Points** — e.g. `src/index.html`, `src/index.js`, `app.py`
  - **Conventions** — anything non-obvious: CSS modules, naming patterns, auth approach
  - **Test Framework** — what's configured, or `none`
  - **Available Agents** — list the agent roles and container suffixes active in this project (e.g. `frontend (suffix: frontend)`, `backend (suffix: backend)`). The Refinement Agent uses this list to know which subtask types to create — it will not create subtasks for agents that don't exist.

**TODO**: Formalise the "Available Agents" field in the CLAUDE.md template in `init-project.sh`. Until then, add it manually.

### 3.4 Verify Claude Code and Git Access

```bash
docker compose exec dev claude --print "say hello" --dangerously-skip-permissions
docker compose exec dev gh auth status
```

### 3.5 Start the Redis Subscriber

```bash
docker compose exec dev pm2 start /agent-docs/subscriber.js --name subscriber
docker compose exec dev pm2 list  # verify running
```

### 3.6 Jenkins Pipeline

Configure a multibranch pipeline job for this repo in Jenkins:

- Job scans for branches + PRs containing a Jenkinsfile
- GitHub PAT credential (`github-token`) must be set as `usernamePassword`
- The job auto-registers a GitHub webhook on first scan — tunnel must be live

For deployment target-specific pipeline steps:
- **Web (static)**: `npm run build`, deploy artifacts to hosting
- **Web (SSR/container)**: build and push Docker image
- **Mobile (Expo EAS)**: `eas update` on merge to `dev`/`beta`; `eas build` + `eas submit` is human-triggered
- **MCP package**: `npm publish` or `pip publish` on merge to `beta`/`prod`

**Prompt the DevOps agent** to write the Jenkinsfile for this project by copying `setup/Jenkinsfile.template` and filling in the TODO blocks above — see Phase 2.3.

### 3.7 Release Promotion (Release ticket, not manual)

There is no manual `beta → prod` PR for a human to open — `prod` only changes
via the Release-ticket flow:
1. Human creates a Jira Release ticket (Target Project field required) once
   enough has accumulated on `beta`
2. Jenkins checks `beta`'s queue is clean, pins the candidate SHA, cuts
   `release/<sha>`, opens the `release/<sha> → prod` PR, and deploys a
   private preview — link posted back to the ticket
3. Human reviews the preview, then moves the Release ticket to **Done**
4. Jenkins merges the frozen PR and redeploys that exact artifact to
   production — no manual Jenkins or GitHub action required

The PR from step 2 stays open the whole time in case a human wants to
inspect or merge it manually (e.g. from GitHub's mobile app) — Done is
sufficient on its own, GitHub is available but never required.

---

## Phase 4: End-to-End Test

*Write this test based on the topology from Phase 0. The scenario below is a template — adapt it to the actual repos and agent roles in the project.*

### Create the test story

In Jira, create a story with all required schema fields filled in. Example for a backend-only project:

- **Summary**: `[TEST] Hello World endpoint`
- **Behavior**: `The API exposes a GET /hello endpoint that returns { "message": "Hello, World!" } with status 200`
- **Acceptance Criteria**: `GET /hello returns 200 with body { "message": "Hello, World!" }`
- **Constraints**: `Response must be JSON. No authentication required.`
- **Edge Cases**: `N/A`
- **Out of Scope**: `Authentication, rate limiting, logging`
- *(Leave Agent field blank — ScrumMaster sets it)*

Adapt the story to match what the project's actual agents can implement.

### Validation legs

**ScrumMaster leg**:
- Verify ScrumMaster receives the webhook: `docker compose logs scrummaster`
- Verify Agent field on the story is set to `refinement-agent`
- Verify a comment appears: "Ticket received. Assigned to Refinement Agent for decomposition."
- Verify story passes schema validation (no block comment)

**Refinement leg**:
- Verify a message is published to the project's Redis channel
- Verify the container subscriber picks it up and invokes Claude
- Verify Refinement Agent creates subtasks in Jira — **only for agent types that exist in this project**
- Verify a completion comment is posted on the parent story

**Dev agent leg** (repeat for each subtask):
- Verify ScrumMaster dispatches to the correct agent channel
- Verify subtask transitions to In Progress
- Verify agent reads the correct definition from `/agent-docs` and works within `/workspace`
- Verify subtask transitions to In Review with a completion comment

**Jenkins leg**:
- Verify Jenkins detects the PR
- Verify tests run and pass
- Verify Jenkins auto-merges to `dev`

---

## End-to-End Test: Platform Startup

*The end-to-end test for an installation brought up by Platform Startup.
Phase 4 above is a Jira scenario and Jira is outside that flow, so this is
a separate test, not a variation of it. Run it once, after `docker compose up`
reports the platform is up.*

The leg this proves is the one that matters: a story written by hand in
Django admin reaches the project container's agent.

### Write the story

Open `http://127.0.0.1:9100/django-admin/` and sign in as the
`AIGANG_ADMIN_USER` account from `.env`.

Under **Workitems → Work items**, add a work item:

- **Project**: the `project.name` from `ai-gang.config.json`
- **Type**: `story`
- **Display name**: `[TEST] Hello World endpoint`
- **Status**: `proposed`
- **Assignee agent id**: `refinement-agent`
- **External key**: leave it empty. It is the Jira issue key a work item
  mirrors, and setting it routes dispatch through Jira, which this flow
  does not set up.

It takes three saves, in this order, and the order matters:

1. **Save the work item.** The **Work item story detail** section is not on
   the add form at all — it belongs to the saved object.
2. **Re-open it, fill in the story schema fields** (Behavior, Acceptance
   Criteria, Constraints, Edge Cases, Out of Scope) and save again,
   leaving the status at `proposed`.
3. **Re-open it once more, change Status to `ready`, and save.**

Filling in the story fields and moving to `ready` in the same save does
not work: the admin saves the work item before its story detail, so the
status change is rejected for the fields it cannot see yet.

`ready` with an assignee is what makes a work item eligible for dispatch;
nothing is dispatched before that.

### Validation legs

**Work-item service leg** — the write was recorded and published:

```bash
docker logs workitem-relay --tail 50     # the outbox row was published
docker exec ai-gang-redis redis-cli XLEN aigang:workitems:<project>:events
```

The stream length increases by at least one when the story reaches
`ready`.

**ScrumMaster leg** — the event was consumed and dispatched:

```bash
docker logs scrummaster --tail 50
docker exec ai-gang-redis redis-cli XLEN aigang:agent:<project>:refinement
```

ScrumMaster's log names the work item and the agent it dispatched to. If
the event stream grew but nothing was dispatched, check that the project
is listed in `services/scrummaster/config/projects.json` and that
ScrumMaster has been restarted since it was added — it reads that file
once, at startup.

**Project container leg** — the agent ran:

```bash
docker exec <project>-dev pm2 list             # subscriber: online
docker exec <project>-dev pm2 logs subscriber --lines 50 --nostream
```

The subscriber's log shows the task being received and Claude Code being
invoked. This is also the first proof that the container's Claude Code and
its `gh` credentials work — Phase 3.4's verification, done for real rather
than as a separate hello-world call.

**Deliverable leg** — the work reached GitHub:

The agent's branch and pull request appear on the repository named by
`repository.url` in `ai-gang.config.json`. A pull request there is the
deliverable of a story.

### If a leg fails

Each leg names the container whose log explains it. `.ai-gang/status.json`
records what initialization believed about every service's health at the
moment it finished; a service healthy there but silent here has stopped
since, and `docker ps` will say so.

---

## Continuing Support

### Adding a repo to an existing project

**TODO: Define this path fully.** Known steps:
1. Create the new GitHub repo
2. Run `init-project.sh` for the new repo (it can be run multiple times for the same project)
3. Follow Phase 3 for the new container
4. Add the new agent to `agents.json` and, if the project should be able to assign it, to its entry in `projects.json`
5. `[HUMAN]` Update each existing container's `/workspace/CLAUDE.md` to add the new agent to the Available Agents list — existing agents need to know the new role exists

Existing containers are unaffected; the new container is independent.

### Rotating secrets / updating tokens

Update the value in the relevant `.env` file, then restart the service that uses it:

```bash
# ScrumMaster (e.g. JIRA_API_TOKEN)
cd ~/ai-gang/services/scrummaster && docker compose restart

# Jenkins (update via JCasC reload)
curl -X POST http://localhost:8080/configuration-as-code/reload \
  -u "admin:$JENKINS_ADMIN_PASSWORD" -H "$(get_crumb)"

# App container (e.g. ANTHROPIC_API_KEY)
cd ~/ai-gang/projects/<name> && docker compose restart
```

### Updating agent definitions

Agent definitions in `~/ai-gang/setup/` are mounted as live volumes into all running containers. A `git pull` on `~/ai-gang` is sufficient — no container restart required.

```bash
cd ~/ai-gang && git pull
```

### Restarting services

```bash
# ScrumMaster or Jenkins — config change (no image rebuild)
cd ~/ai-gang/services/scrummaster && docker compose restart
cd ~/ai-gang/jenkins && docker compose restart

# Any service — code change requiring image rebuild
docker compose up -d --build
```

### Monitoring

```bash
# ScrumMaster activity
cd ~/ai-gang/services/scrummaster && docker compose logs -f

# Redis subscriber (inside a project container)
docker compose exec dev pm2 logs subscriber

# Jenkins build history
open https://jenkins.yourdomain.com
```
