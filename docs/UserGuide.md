# AI Gang - User Guide

**Purpose**: How the AI Gang platform works and how to create a new project workspace.

---

## Overview

AI Gang HQ is a cloud VM that hosts isolated Docker containers — one per repo. Each container has Claude Code installed and mounts the shared agent definitions from `~/ai-gang/setup/`. Agents run inside these containers and interact with Jira and GitHub on your behalf.

Three shared services run alongside the project containers, and platform
startup brings up all three: **Redis** (message broker), the **work-item
service** (the datastore and the Django admin panel you write stories in,
at `http://127.0.0.1:9100/django-admin/`), and **ScrumMaster** (work
router). **Jenkins** (CI/CD — auto-merges to `dev` on test pass, promotes
to `beta` when a ticket moves to Done) is a later addition an operator
sets up separately; so are Jira, Cloudflare and the Beta VM.

Each project gets:

- Its own directory under `~/ai-gang/projects/`
- Its own Docker container (isolated from all others)
- Access to shared agent definitions via `/agent-docs`
- Its own git workspace at `/workspace`

---

## Prerequisites

- AI Gang is running. If it is not, see "Starting AI Gang" below — one
  command brings the whole platform up.
- You can reach the machine it runs on (if that is a server rather than
  your own machine, e.g. `ssh ai-gang-cloud`).

---

## API Tokens and Access

### Jira API Token

Atlassian Cloud API tokens have **no scope selection** — they always inherit the full permissions of the account that created them. Do not use a personal account. Instead, create a dedicated service account (see `ClaudeInstructions.md` Phase 1) and generate the token from that account. The service account's project role (Member, not Admin) is what limits access, not the token itself.

### GitHub Fine-Grained PAT

Do not use classic GitHub PATs — they are poorly scoped and can grant broad access to all your repositories. Use a **fine-grained PAT** for each project:

1. GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**
2. **Resource owner**: your org or personal account
3. **Repository access**: this repo only
4. **Permissions required**:
   - Contents: Read and Write (agents push branches)
   - Pull requests: Read and Write (agents create and merge PRs)
   - Metadata: Read (required by GitHub)
   - Commit statuses: Read and Write (Jenkins posts build results to PRs)

**Troubleshooting: `git push` returns 403 despite `permissions.push: true`.**
An account's collaborator/push grant and a fine-grained token's repository
allow-list are independent gates. Granting collaborator access does not add
the repository to an already-issued token's selected repositories. Check
the account's repository permission, then inspect the token actually used
by Git: its resource owner, selected repository, and Contents write permission.
Update the token's repository selection where supported; also confirm Git
and `gh` are using the intended credentials. See GitHub's
[token access and repository selection guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

For a failing REST request, `X-Accepted-GitHub-Permissions` identifies the
endpoint's required permissions; it does **not** report the token's actual
grants or prove that its repository allow-list is correct. Compare it with
the token's settings using GitHub's
[permission reference](https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens).
If the repository cannot be selected, check the documented fine-grained-token
limitations for outside/repository collaborators; another collaborator grant
alone will not resolve that limitation.

The same token is used by FE/BE agents (push branches + open PRs) and by Jenkins (auto-merge to `dev` on test pass). Jenkins is the merge authority for `dev` — agents never merge directly. Branch protection on `dev`, `beta`, and `prod` with **"Do not allow bypassing the above settings"** enforces the full flow: agents push feature branches → Jenkins merges to `dev` on green tests → Jenkins promotes to `beta` when a ticket moves to Done → human opens a PR to `prod`. No code reaches `prod` without human sign-off.

---

## Starting AI Gang

If AI Gang is not running yet, this is how you start it — on your own
machine or on a server you have Docker on. One command does all of it.

### What you need first

- **Docker**, with the Compose plugin. `./scripts/install-docker.sh`
  installs both if you do not have them.
- **An Anthropic API key** — <https://console.anthropic.com/settings/keys>.
- **An empty GitHub repository** for the project you want built, and a
  fine-grained PAT for it (see "GitHub Fine-Grained PAT" above). AI Gang
  does not create the repository; it pushes into the one you made.

### The six steps

```bash
git clone https://github.com/crashtest00/ai-gang.git ~/ai-gang
cd ~/ai-gang

cp ai-gang.config.template.json ai-gang.config.json   # then edit it
cp .env.template .env                                 # then edit it

docker compose up
```

`ai-gang.config.json` is four values — what to build, and where its code
lives:

```json
{
  "schemaVersion": 1,
  "project": {
    "name": "hello-world",
    "type": "web",
    "stack": "node-express"
  },
  "repository": {
    "url": "https://github.com/your-org/hello-world.git"
  }
}
```

`name` is lowercase letters, numbers and hyphens. `type` and `stack` come
from a fixed list of supported profiles — today that is `web` with
`node-express`, and startup tells you the current list if you get it
wrong. `repository.url` is that repository's plain HTTPS URL, with no
username or token in it — the PAT goes in `.env`, as `GH_TOKEN`.

`.env` is where every secret goes, and the only place any of them goes.
The first section of `.env.template` is what startup needs: your Anthropic
key, the project's `GH_TOKEN`, the Django admin account you will sign in
with, and a PostgreSQL password and Django secret key you invent
(`openssl rand -hex 24` twice). Everything below that first section
belongs to Jira, Cloudflare, Jenkins and the Beta VM, which you add later
if you want them — leave them blank.

### What happens then

`docker compose up` builds and starts one container, and streams what it
is doing. That container brings up the rest — Redis, the work-item
service, ScrumMaster, and your project's own container — and then exits.
When it exits, AI Gang is up. Nothing to run in between.

A first run builds four images and takes a while. Run it in the
foreground and watch; if you would rather watch from elsewhere,
`.ai-gang/status.json` in the checkout says which step is in progress and
how each service is doing, and `.ai-gang/startup.log` is the same log.

If something goes wrong, the run stops and says what failed. Fix it and
run `docker compose up` again — re-running is safe, and picks up where it
left off rather than starting a second copy of anything.

Both files stay in the checkout after the run ends, whether it succeeded
or failed, and nothing deletes them — `.ai-gang/startup.log` is the same
output you watched `docker compose up` print, kept after the container
is gone. Running again moves the previous run's log and status record to
`.ai-gang/previous/` rather than overwriting them, so you still have the
failed run to look at.

### Writing your first story

Open <http://127.0.0.1:9100/django-admin/> and sign in with the
`AIGANG_ADMIN_USER` and `AIGANG_ADMIN_PASSWORD` you put in `.env`.

Under **Workitems → Work items**, add a work item with your project's
name, **Type** `story`, an **Assignee agent id** of `refinement-agent`,
and **Status** `proposed`, leaving **External key** empty. It takes three
saves: save the work item, re-open it and fill in the story fields
(Behavior, Acceptance Criteria, Constraints, Edge Cases, Out of Scope) and
save, then re-open it once more and set **Status** to `ready`. The story
fields have to be saved before the status moves, not with it.

That is the whole loop. The story is routed to the Refinement Agent, which
breaks it into subtasks for the agents your project has; each of those
runs in your project's container and opens a pull request on the
repository you named.

### Changing your mind later

The configuration is recorded the first time it succeeds, in
`.ai-gang/config-identity.json`. Changing `project.name` or
`repository.url` afterwards and re-running is refused, with nothing
touched — otherwise you would quietly get a second project rather than a
renamed one. To build something else, start from a fresh checkout.

Adding a second repository to a project you already have is a different
thing, and is the "Adding a repo to an existing project" path in
`ClaudeInstructions.md`.

---

## Creating a New Project

*Platform startup already creates the project your `ai-gang.config.json`
names — you do not need this section for that one. This is how to add
another project by hand to an AI Gang that is already running.*

### Before you run the script

One thing must exist before `init-project.sh` can finish:

1. **GitHub repository** — create it on GitHub first (empty is fine). The script will prompt for its HTTPS URL, your `GH_TOKEN` (fine-grained PAT — entered securely, not echoed to the terminal), and will push the initial commit automatically. No deploy key setup required.

### The fast path: ask Claude

The easiest way to create a new project is to ask Claude Code to do it. Claude will run `scripts/init-project.sh` on your behalf and prompt you through the steps:

> "Set up a new AI Gang project called hello-world"

Claude will:

1. Prompt for the project name, Jira key, and GitHub HTTPS URL
2. Create `projects/hello-world/` with a `docker-compose.yml`, `.env` stub, and initialised git repo
3. Create the Jira project and apply all custom fields to its screens

### The manual path: run the script directly

```bash
ssh ai-gang-cloud
cd ~/ai-gang
./scripts/init-project.sh
```

The script will prompt for:

- **Project name** — lowercase letters, numbers, and hyphens (e.g. `hello-world`)
- **Jira project key** — suggested automatically, or enter your own (e.g. `HW`)
- **GitHub HTTPS URL** — e.g. `https://github.com/org/repo.git` (leave blank to skip)
- **GH_TOKEN** — fine-grained PAT (entered securely, written to `.env`; leave blank if skipping GitHub)

It creates:

- `projects/hello-world/docker-compose.yml` — pre-configured with the `ai-gang` network, env file, and agent-docs mount; container startup runs `gh auth setup-git` using `GH_TOKEN`
- `projects/hello-world/.env` — with `PROJECT_NAME`, `REDIS_HOST`, `ANTHROPIC_API_KEY`, `GITHUB_URL`, and `GH_TOKEN`
- `projects/hello-world/src/CLAUDE.md` — project map stub; fill in before the first agent run
- The Jira project (company-managed Kanban) with all custom fields applied to every screen

---

## After init-project.sh

> **Manual step required — configure board columns**
>
> `init-project.sh` applies the AI Gang Kanban workflow (`Backlog → Shovel Ready → In Progress → In Review → Done`) to your Jira project, but Jira's board column configuration cannot be updated via API. Without this step your board will show the wrong columns and statuses will appear unmapped.
>
> Go to your board → **Board Settings → Columns** and:
>
> - **Add** columns for **Shovel Ready** and **In Review**
> - **Remove** the default **Selected for Development** column
>
> This is a one-time step per project and takes about two minutes.

### Step 1: Add a Dockerfile

Choose the appropriate template for your tech stack:

```bash
cd ~/ai-gang/projects/<project-name>
cp ~/ai-gang/Dockerfile-node.template ./Dockerfile
# or
cp ~/ai-gang/Dockerfile-python.template ./Dockerfile
```

Customize as needed for your project's dependencies.

### Step 2: Build and start the container

```bash
docker compose build
docker compose up -d
docker ps  # verify it's running
```

### Step 3: Verify Claude Code

```bash
docker compose exec dev claude --version
docker compose exec dev ls /agent-docs  # should show agent definition files
```

### Step 4: Start the Redis subscriber

The subscriber listens for work from ScrumMaster and invokes Claude Code for each task. Use PM2 so it restarts reliably after crashes or dependency changes:

```bash
docker compose exec dev pm2 start /agent-docs/subscriber.js --name subscriber
```

For quick debugging you can also run it directly:

```bash
docker compose exec dev node /agent-docs/subscriber.js &
```

---

## Multi-repo projects (FE + BE)

When a project has separate frontend and backend repos, each repo gets its own container on the shared `ai-gang` network.

### Directory layout

```
projects/myapp/
  docker-compose.yml     ← two services: frontend + backend
  frontend/              ← /workspace in FE container, cloned from myapp-frontend repo
  backend/               ← /workspace in BE container, cloned from myapp-backend repo
```

### Per-container .env

Each container needs its own `.env` with `AGENT_CHANNEL_SUFFIX` set to its role:

```
# frontend/.env
PROJECT_NAME=myapp
AGENT_CHANNEL_SUFFIX=frontend
REDIS_HOST=ai-gang-redis
ANTHROPIC_API_KEY=...
GH_TOKEN=...   # fine-grained PAT scoped to myapp-frontend only

# backend/.env
PROJECT_NAME=myapp
AGENT_CHANNEL_SUFFIX=backend
REDIS_HOST=ai-gang-redis
ANTHROPIC_API_KEY=...
GH_TOKEN=...   # fine-grained PAT scoped to myapp-backend only
```

ScrumMaster routes `frontend-agent` tasks to `agent:myapp-frontend` and `backend-agent` tasks to `agent:myapp-backend`. Containers only receive work relevant to their repo.

### Agent definition mounts

Mount only the definitions each container needs in `docker-compose.yml`:

```yaml
frontend:
  volumes:
    - ./frontend:/workspace
    - ../../setup/frontend-agent.md:/agent-docs/frontend-agent.md:ro
    - ../../setup/refinement-agent.md:/agent-docs/refinement-agent.md:ro
    - ../../setup/subscriber.js:/agent-docs/subscriber.js:ro

backend:
  volumes:
    - ./backend:/workspace
    - ../../setup/backend-agent.md:/agent-docs/backend-agent.md:ro
    - ../../setup/refinement-agent.md:/agent-docs/refinement-agent.md:ro
    - ../../setup/devops-agent.md:/agent-docs/devops-agent.md:ro
    - ../../setup/subscriber.js:/agent-docs/subscriber.js:ro
```

### Integration testing

Start the BE dev server under PM2 so the FE container can call it at `http://backend:PORT`:

```bash
docker compose exec backend pm2 start npm -- run dev
```

The FE container's tests hit a real running API — no mocks needed.

---

## Connecting VS Code (Optional)

If you want to develop interactively inside the container:

1. Open VS Code on your laptop
2. `Cmd+Shift+P` → "Remote-SSH: Connect to Host" → select `ai-gang-cloud`
3. Open folder: `/home/aigang/ai-gang/projects/<project-name>`
4. Open a terminal — you're now on the HQ droplet inside the project directory

---

## Troubleshooting

**Container won't build**

```bash
docker compose build --no-cache
```

**Container exits immediately**

```bash
docker compose logs
# Verify docker-compose.yml has: command: tail -f /dev/null
```

**Agent docs not visible inside container**

```bash
docker compose exec dev ls /agent-docs
# If empty, check the volume mount in docker-compose.yml:
# - ../../setup:/agent-docs:ro
```

**Claude Code not found inside container**

```bash
# Rebuild — Claude Code is installed at image build time
docker compose build --no-cache
docker compose up -d
```

**ScrumMaster not routing to this project**

- Confirm `PROJECT_NAME` in `.env` matches the Jira project name
- Confirm the Redis subscriber is running: `docker compose exec dev ps aux | grep subscriber`
- Check ScrumMaster logs: `cd ~/ai-gang/services/scrummaster && docker compose logs`

---

**See also**: `ClaudeInstructions.md` for full system setup (Jira, webhook, Redis, ScrumMaster, Jenkins).
