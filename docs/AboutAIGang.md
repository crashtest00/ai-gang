# About AI Gang

## Version 2.1

AI Gang is a self-hosted platform for running Claude Code agents as a software development team. You describe what to build as a work item; agents design, code, test, and ship — with humans in the loop at the right moments. Jira is an optional front end for those work items, off by default.

---

## What It Is

Each project gets its own cloud VM. On that VM, every code repository runs inside an isolated Docker container with Claude Code installed. Agents work inside these containers the same way a developer would: they read files, write code, run tests, commit, and open pull requests.

Four shared services coordinate the work:

- **Django/`core`** — the canonical record of all work, and the platform's only Jira client. Where a project is connected to Jira, `core` is what receives Jira's webhooks; nothing else in the platform talks to Jira.
- **ScrumMaster** — reads canonical work-item events from `core` and routes work to the right agent container via durable Redis Streams, then publishes back to `core` the commands agents' output implies. It is not a Jira client and makes no Jira call; agents never talk to Jira either.
- **Redis** — the message broker between `core`, ScrumMaster and agent containers.
- **Jenkins** — the CI/CD pipeline. It runs tests when agents open pull requests, auto-merges to `dev` on green, and promotes `dev` to `beta` when a work item is accepted.

---

## How It Works

```
PM creates a Story as a work item in core (all required schema fields filled in)
  (with Jira connected, the PM creates it in Jira and core records it from the
   webhook — core is the only component that talks to Jira)
        ↓
core publishes the work-item event; ScrumMaster picks it up
  → assigns the refinement agent, dispatches to its container
        ↓
Refinement Agent decomposes the story into subtasks
  → names the agent for each (frontend-agent, backend-agent, etc.)
        ↓
Each subtask's event → ScrumMaster dispatches the right dev agent
        ↓
Dev agent works in /workspace
  → reads agent definition from this dispatch's own snapshot of /agent-docs
  → writes code, runs tests, opens a pull request
  → reports back via the gateway Redis Stream, aigang:gateway:{project}
    (ScrumMaster publishes the comment and status commands to core)
        ↓
Jenkins picks up the PR
  → runs tests → auto-merges to dev on green
  → promotes dev → beta
        ↓
Human reviews in dev, accepts the work item → Jenkins promotes to beta
Human opens PR from beta → prod (requires 1 approving review)
```

---

## The Four Agent Roles

| Role                 | Responsibility                                                                  |
| -------------------- | ------------------------------------------------------------------------------- |
| **Refinement Agent** | Reads a PM story, creates subtasks, assigns the right dev agent to each |
| **Frontend Agent**   | Implements UI subtasks in the frontend container                                |
| **Backend Agent**    | Implements API/service subtasks in the backend container                        |
| **DevOps Agent**     | Jenkins setup and maintenance; not on the automated path                        |

Agent definitions live in `~/ai-gang/setup/agents/` and are mounted read-only into every container at `/agent-docs`. Updating an agent definition is a `git pull`, and it takes effect at the next dispatch and never inside a running session: every dispatch copies the definitions, the role handbooks and the agent commons into a snapshot of its own and points the session at that copy, so no file an agent reads can change under it mid-session.

---

## Container Topology

**One container per repo.** A repo is the natural unit of context, dependencies, and deployment. Containers are isolated: they see only their own codebase, carry only the credentials they need, and receive only the tickets relevant to their concern.

For projects with multiple repos (e.g. a separate frontend and backend), each repo gets its own container on a shared Docker network. Containers communicate at `http://service-name:PORT` — a real running API, no mocks.

Every container sets an `AGENT_CHANNEL_SUFFIX` (e.g. `backend`, `frontend`) that ScrumMaster uses to route the right subtasks to the right container.

---

## What Humans Do

The platform automates code → test → PR → merge → deploy. Humans handle the parts that require judgement or external access:

- Writing stories (the PM's job — AI Gang doesn't write requirements)
- One-time setup, if you connect Jira: Jira service account and custom fields (done once per Jira instance)
- Per-project setup: Cloudflare tunnel, infrastructure services, containers (guided by `ClaudeInstructions.md`)
- Filling in `/workspace/CLAUDE.md` — the project map agents use to navigate the codebase
- Reviewing in the `dev` environment and accepting the work item
- Approving the final `beta → prod` pull request

See `UserGuide.md` for day-to-day operations and `ClaudeInstructions.md` for full setup guidance.