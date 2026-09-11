# About AI Gang

## Version 2.1

AI Gang is a self-hosted platform for running Claude Code agents as a software development team. You describe what to build in Jira; agents design, code, test, and ship — with humans in the loop at the right moments.

---

## What It Is

Each project gets its own cloud VM. On that VM, every code repository runs inside an isolated Docker container with Claude Code installed. Agents work inside these containers the same way a developer would: they read files, write code, run tests, commit, and open pull requests.

Three shared services coordinate the work:

- **ScrumMaster** — receives Jira webhooks and routes work to the right agent container via durable Redis Streams. All Jira reads and writes for agents flow through ScrumMaster; agents never talk to Jira directly.
- **Redis** — the message broker between ScrumMaster and agent containers.
- **Jenkins** — the CI/CD pipeline. It runs tests when agents open pull requests, auto-merges to `dev` on green, and promotes `dev` to `beta` when a ticket moves to Done.

---

## How It Works

```
PM creates a Story in Jira (all required schema fields filled in)
        ↓
ScrumMaster receives Jira webhook
  → validates required fields (blocks ticket with comment if incomplete)
  → assigns Agent = refinement-agent, dispatches to container
        ↓
Refinement Agent decomposes the story into subtasks in Jira
  → sets the Agent field on each subtask (frontend-agent, backend-agent, etc.)
        ↓
Each subtask triggers a new webhook → ScrumMaster dispatches the right dev agent
        ↓
Dev agent works in /workspace
  → reads agent definition from /agent-docs
  → writes code, runs tests, opens a pull request
  → reports back via jira-gateway Redis channel (ScrumMaster posts comment + transitions ticket)
        ↓
Jenkins picks up the PR
  → runs tests → auto-merges to dev on green
  → on Jira Done: promotes dev → beta
        ↓
Human reviews in dev, moves ticket to Done → Jenkins promotes to beta
Human opens PR from beta → prod (requires 1 approving review)
```

---

## The Four Agent Roles

| Role                 | Responsibility                                                                  |
| -------------------- | ------------------------------------------------------------------------------- |
| **Refinement Agent** | Reads a PM story, creates subtasks in Jira, assigns the right dev agent to each |
| **Frontend Agent**   | Implements UI subtasks in the frontend container                                |
| **Backend Agent**    | Implements API/service subtasks in the backend container                        |
| **DevOps Agent**     | Jenkins setup and maintenance; not on the automated path                        |

Agent definitions live in `~/ai-gang/setup/` and are mounted read-only into every container at `/agent-docs`. Updating an agent definition is a `git pull` — running containers see the change immediately.

---

## Container Topology

**One container per repo.** A repo is the natural unit of context, dependencies, and deployment. Containers are isolated: they see only their own codebase, carry only the credentials they need, and receive only the tickets relevant to their concern.

For projects with multiple repos (e.g. a separate frontend and backend), each repo gets its own container on a shared Docker network. Containers communicate at `http://service-name:PORT` — a real running API, no mocks.

Every container sets an `AGENT_CHANNEL_SUFFIX` (e.g. `backend`, `frontend`) that ScrumMaster uses to route the right subtasks to the right container.

---

## What Humans Do

The platform automates code → test → PR → merge → deploy. Humans handle the parts that require judgement or external access:

- Writing Jira stories (the PM's job — AI Gang doesn't write requirements)
- One-time setup: Jira service account and custom fields (done once per Jira instance)
- Per-project setup: Cloudflare tunnel, infrastructure services, containers (guided by `ClaudeInstructions.md`)
- Filling in `/workspace/CLAUDE.md` — the project map agents use to navigate the codebase
- Reviewing in the `dev` environment and moving tickets to Done
- Approving the final `beta → prod` pull request

See `UserGuide.md` for day-to-day operations and `ClaudeInstructions.md` for full setup guidance.