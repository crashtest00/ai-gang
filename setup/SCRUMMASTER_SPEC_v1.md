# AI Gang - ScrumMaster Service

## Technical Specification

**Purpose**: Defines the architecture, responsibilities, and behavior of the ScrumMaster service — the exclusive communication bridge between Jira and the AI Gang agent ecosystem.

**Date**: March 25, 2026
**Version**: 1.1
**Status**: Current — reflects implemented service

---

## Table of Contents

1. [Overview](#overview)
2. [Responsibilities](#responsibilities)
3. [Architecture](#architecture)
4. [Jira Configuration Dependencies](#jira-configuration-dependencies)
5. [Inbound Webhook Handling](#inbound-webhook-handling)
6. [Outbound Jira Operations](#outbound-jira-operations)
7. [Redis Message Contract](#redis-message-contract)
8. [Prompt Construction](#prompt-construction)
9. [Agent Catalog and Assignment Validation](#agent-catalog-and-assignment-validation)
10. [Deployment](#deployment)
11. [Constraints & Boundaries](#constraints--boundaries)

---

## Overview

ScrumMaster is a persistent service running on the AI Gang HQ droplet. It has two jobs: listen for canonical work-item events from Django/`core` and route them to the correct agent, and listen for messages from agents and publish the canonical commands they imply back to `core`.

No agent, and no ScrumMaster module, has direct access to Jira: Django/`core` is the running platform's only Jira client (V5.1 REQ-01), and no running consumer calls it until v5.2's outbound writer. ScrumMaster reads canonical work items from `core` and publishes canonical commands back to it on agents' behalf — formatting, error handling, rate limiting, and audit logging for that path live here.

ScrumMaster does not make decisions about work. It routes, fetches context, constructs prompts, and relays. The Refinement Agent makes decisions about tickets. Dev agents make decisions about code.

---

## Responsibilities

**ScrumMaster owns:**

- Reading canonical work-item context from `core` when building agent prompts
- Routing inbound events to the correct agent via Redis
- Receiving outbound messages from all agents via Redis
- Publishing the canonical commands agents' comments, field updates and subtask creation imply, to `core`, on their behalf
- Enforcing comment formatting standards before publishing a comment command
- Including the correct agent definition path in every Claude Code invocation prompt

**ScrumMaster does NOT own:**

- Ticket content decisions (Refinement Agent)
- Code (dev agents)
- Receiving or interpreting Jira webhooks, or calling the Jira API (Django/`core`, V5.1 REQ-01)
- CI/CD pipeline results (Jenkins posts directly to Jira)
- Infrastructure (Cloud Engineering)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ Atlassian Cloud (Jira)                                      │
│  - Fires webhooks on configured field/status changes        │
│  - Receives API calls from ScrumMaster                      │
└────────────────┬────────────────────────────────────────────┘
                 │ HTTPS webhooks (inbound)
                 │ HTTPS API calls (outbound)
                 ↓
┌─────────────────────────────────────────────────────────────┐
│ HQ Droplet                                                  │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ ScrumMaster Service                                   │  │
│  │                                                       │  │
│  │  Webhook Receiver                                     │  │
│  │   - Validates webhook signatures                      │  │
│  │   - Durably enqueues the event to its project's       │  │
│  │     webhook Stream before responding 200               │  │
│  │                                                       │  │
│  │  Webhook Consumer                                      │  │
│  │   - Reads the webhook Stream via a consumer group      │  │
│  │   - Routes to the correct handler, exactly once per     │  │
│  │     messageId                                          │  │
│  │                                                       │  │
│  │  Context Builder                                      │  │
│  │   - Reads the full work item from core's read API      │  │
│  │   - Resolves agent definition path from registry      │  │
│  │   - Searches codebase for BLOCKED markers             │  │
│  │   - Constructs Claude Code prompt                     │  │
│  │                                                       │  │
│  │  Stream Dispatcher                                     │  │
│  │   - Durably XADDs task envelopes to each project's      │  │
│  │     per-agent Stream                                   │  │
│  │                                                       │  │
│  │  Gateway Stream Consumer                                │  │
│  │   - Reads each project's gateway Stream via a consumer  │  │
│  │     group, exactly once per messageId                  │  │
│  │   - Publishes the corresponding canonical command       │  │
│  │     to core                                             │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ Redis (Streams + consumer groups — durable, ack'd)    │  │
│  │                                                       │  │
│  │  Inbound (core -> ScrumMaster):                       │  │
│  │   aigang:workitems:{project}:events  group: dispatch  │  │
│  │                                                       │  │
│  │  Inbound (ScrumMaster -> agents):                     │  │
│  │   aigang:agent:{project}:{suffix} group: agent-{suffix}│  │
│  │                                                       │  │
│  │  Outbound (agents -> ScrumMaster):                    │  │
│  │   aigang:gateway:{project}       group: scrummaster   │  │
│  │                                                       │  │
│  │  Failed messages: <source-stream>:dead                │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ Project Containers                                    │  │
│  │                                                       │  │
│  │  Each container runs:                                 │  │
│  │   - Claude Code (invoked by ScrumMaster prompt)       │  │
│  │   - Streams consumer (setup/subscriber.js), reading    │  │
│  │     its project's explicit agent Stream(s)             │  │
│  │   - /workspace (project codebase)                     │  │
│  │   - /agent-docs (agent definitions, read-only)        │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

Durable delivery, acknowledgment, retry, and dead-lettering semantics are
defined in full in Durable Agent Messaging with Redis
Streams; this document covers
what ScrumMaster does with the content of each message, not the transport
guarantees.

---

## Jira Configuration Dependencies

ScrumMaster depends on the following Jira configuration being in place before it can operate. These are not ScrumMaster's responsibility to create — they are prerequisites defined in PDI-8.

### Custom Fields

All custom fields are instance-level resources created by `scripts/create-jira-fields.sh`. Their IDs are written to `services/scrummaster/.env` automatically.

**Routing and control fields:**

| Field Name | Type | Purpose |
|-----------|------|---------|
| `Agent` | Single-select | Identifies which agent owns the ticket. ScrumMaster uses this to determine routing target. |
| `Blocked` | Single-select (`Yes` / null) | Set by ScrumMaster when an agent blocks. Cleared by human to trigger unblock flow. |

**Story schema fields (all paragraph/textarea type):**

| Field Name | Required | Purpose |
|-----------|----------|---------|
| `Value Hypothesis` | No | Why this story matters |
| `Test & Measurement` | No | Success metric and threshold |
| `Behavior` | **Yes** | Declarative system behavior — drives subtask creation |
| `Acceptance Criteria` | **Yes** | Deterministic, testable conditions |
| `Constraints` | **Yes** | Limits, formats, validation rules |
| `Edge Cases` | **Yes** | Invalid input, partial failures, timeouts |
| `Out of Scope` | **Yes** | Explicitly what is NOT included |

Django/`core` validates Behavior, Acceptance Criteria, Constraints, Edge Cases, and Out of Scope on every `jira:issue_created` event (`workitems/webhook_consumer.py`'s `_handle_story_created`); the `story_intake` side effect it records from that check has no ScrumMaster consumer until v5.2's outbound writer (see Inbound Webhook Handling).

### Workflow Statuses

| Status         | Meaning                                                                           |
| -------------- | --------------------------------------------------------------------------------- |
| `Backlog`      | Ticket created, not yet refined                                                   |
| `Shovel Ready` | Refined, subtasks assigned, agent can begin work                                  |
| `In Progress`  | Agent actively working                                                            |
| `Blocked`      | Agent waiting for human clarification — note: also reflected in the Blocked field |
| `In Review`    | PR open, awaiting review                                                          |
| `Done`         | Merged and deployed                                                               |

### Webhook Triggers

ScrumMaster does not register for or receive Jira webhooks at all: Django/`core`
is the sole recipient and interpreter of every Jira webhook (`workitems/views.py`,
`workitems/webhook_consumer.py`; V5.1 REQ-01). ScrumMaster instead reacts to the
canonical work-item events `core` publishes from its own interpretation of
those webhooks (and from every other ingress — Admin Panel writes, the HTTP
API — on the same path):

| Canonical event                                      | Trigger Condition                                            | Handler                                 |
| ------------------------------------------------------ | -------------------------------------------------------------- | ---------------------------------------- |
| `work_item.created` / `work_item.status_changed`       | A work item's status reaches `ready` with an assigned agent     | Build prompt, durably dispatch to that agent's Stream |
| `work_item.jira_side_effect` (kind `blocked_cleared`)  | `core` clears a dev-agent ticket's Blocked side effect          | Search for BLOCKED marker, redispatch assigned agent |

A `work_item.jira_side_effect` of kind `story_intake` — `core`'s record of a
newly-created Story and whether its schema fields are complete — has no
ScrumMaster consumer in v5.1; posting the acknowledgement or
missing-fields comment it implies is v5.2's outbound writer's job.

---

## Inbound Webhook Handling

ScrumMaster has no Jira webhook handler: every Jira webhook is received,
validated, and interpreted entirely inside Django/`core`
(`workitems/webhook_consumer.py`), including the story-readiness check
V1's Handler 1 ran here. What follows are the two canonical events `core`
publishes that ScrumMaster still acts on.

### Handler 1: Dispatch on a dispatch-eligible work item

Fired on `work_item.created` or `work_item.status_changed` for a work item
whose status is `ready` and which has an assigned agent (`dispatchConsumer.js`'s
`maybeDispatch`).

**Action:**

1. Read the full canonical work item from `core`'s read API
2. Look up the assigned agent's definition path from the agent catalog
3. Construct Claude Code prompt (see Prompt Construction) from the work
   item's own canonical fields — behavior, acceptance criteria, comment
   thread, parent, external key if any
4. Durably dispatch prompt to `aigang:agent:{project}:{suffix}`
5. Log event

### Handler 2: Blocked side effect cleared

Fired on a `work_item.jira_side_effect` of kind `blocked_cleared` — `core`'s
record that a dev-agent ticket's block was lifted (`dispatchConsumer.js`'s
`handleBlockedClearedSideEffect`).

**Action:**

1. Read the full canonical work item from `core`'s read API
2. Look up the assigned agent's definition path from the agent catalog
3. Search project codebase for a `BLOCKED {work item id}` marker, keyed on
   the work item's own canonical id, to identify the resume point
4. Construct Claude Code prompt (see Prompt Construction) including the
   comment thread and the marker's file/line, if found
5. Durably dispatch prompt to `aigang:agent:{project}:{suffix}`
6. Log event

---

## Outbound Jira Operations

ScrumMaster runs one durable Streams consumer per project on that project's gateway stream (`aigang:gateway:{project-name}`, consumer group `scrummaster`) and processes outbound messages from that project's container(s). Project identity is derived from which stream a consumer is bound to — never from message content. All messages must conform to the message contract defined below, and each is processed at most once per messageId even if redelivered.

### Supported Operations

**Post Comment**
Posts a formatted comment to the specified ticket on behalf of the named agent.

ScrumMaster enforces comment formatting before posting. See Agent Comment Standard below.

**Set Blocked Field**
Sets the Blocked field to true on the specified ticket. ScrumMaster also posts a system comment noting which agent set the field and at what time.

**Set Agent Field**
Updates the Agent field on a ticket. Used by the Refinement Agent to assign subtasks to dev agents.

**Create Subtask**
Creates a subtask under a specified parent ticket. Used by the Refinement Agent to decompose stories. ScrumMaster enforces that the subtask is created within the same project as the parent ticket.

All four operations above are now carried as canonical A2A envelopes rather
than bare `{"type": ...}` messages — see [Redis Message
Contract](#redis-message-contract) below for the full contract.

### Agent Comment Standard

Every comment command ScrumMaster publishes to `core` on behalf of an agent must follow this format:

```
[{Agent Name}] {comment body}

Reference: {file path and function/line if applicable}
Ticket: {ticket key}
```

Example:

```
[Backend Agent] Uncertain which endpoint to use for doAuth() in auth.py

Reference: src/auth.py → doAuth()
Ticket: GANG-42
```

ScrumMaster enforces this format. The reference field is optional: no submission path can be rejected or flagged for leaving it out. When it is absent, ScrumMaster posts the comment with the `Reference:` line omitted, logging only `Posted comment on {ticket}` — no warning is logged.

---

## Redis Message Contract

Every message travels as a versioned transport envelope on a Redis Stream —
`schemaVersion`, `messageId`, `kind`, `project`, `taskId`/`contextId`,
`createdAt`, and a `payload` — durably appended (XADD), consumed through a
consumer group, and acknowledged only after its effect is durable. The full
envelope schema, stream/group topology, retry, dead-letter, and idempotency
rules are defined in Durable Agent Messaging with Redis
Streams. This section documents
the canonical A2A shape of `payload` for `kind: "task"` and `kind:
"gateway_operation"` — see A2A
Messaging. There is no separate
legacy `type`/`prompt` contract on this path.

`taskId` = the work item's own canonical id, in both modes; `contextId` =
its parent work item's canonical id (or its own id when there is no parent)
— minted the first time ScrumMaster sees the root work item, and shared by
every subtask created under it. A Jira issue key MAY ride along as
`metadata.externalKey` for display; no ScrumMaster module reads it to
address anything (V5.1 REQ-06, REQ-07). A Task's identity is stable for its
whole lifecycle: one work item ↔ one Task, from initial dispatch through
completion. Unblocking a ticket, a
Jenkins pipeline retry, and a human-requested rework redispatch are all
continuations of the *same* Task and context — none of them creates a new
assignment. Terminal states (`completed`, `failed`, `canceled`, `rejected`)
cannot accept further messages.

### Inbound: ScrumMaster → Agent (`aigang:agent:{project-name}:{suffix}`)

Envelope `kind: "task"`. `payload` is a single A2A Message (`role: "client"`):

```json
{
  "kind": "message",
  "messageId": "msg-<uuid>",
  "taskId": "GANG-42",
  "contextId": "GANG-40",
  "role": "client",
  "referenceMessageId": "msg-<uuid-of-prior-message>",
  "parts": [ { "kind": "text", "text": "<full Claude Code prompt>" } ]
}
```

The text Part is the full Claude Code prompt for this dispatch or
continuation — ticket context, schema fields, comment thread, the Task's own
`taskId`/`contextId`/`messageId` (for the agent to echo back in its replies),
and instructions are all embedded in that prompt text. The container-side
`subscriber.js` extracts the text Part(s) and pipes them to `claude --print
--dangerously-skip-permissions -` via stdin; it does not otherwise parse the
message. On completion, `subscriber.js` durably publishes a terminal
`task_status` envelope (`completed` or, after retries are exhausted,
`failed`) back on the gateway stream; this is a
Streams-level execution-outcome signal, not agent-authored A2A content.

### Outbound: Agent → ScrumMaster (`aigang:gateway:{project-name}`)

ScrumMaster derives project identity from which stream a message arrived on,
not from any field in the payload, and cross-checks it against the Task's own
`projectName` metadata recorded at dispatch time. An agent cannot claim a
different project by writing a different value into its submission — the
stream is the authority, and an envelope whose declared project doesn't match
is rejected and dead-lettered without ever reaching `core`.

Envelope `kind: "gateway_operation"`. `payload` is one A2A submission:

```json
{
  "state": "working | input-required | auth-required | completed",
  "message": {
    "kind": "message",
    "messageId": "msg-<uuid>",
    "taskId": "GANG-42",
    "contextId": "GANG-40",
    "role": "agent",
    "referenceMessageId": "msg-<uuid-of-prior-message>",
    "parts": [
      { "kind": "text", "text": "<human-readable content>" },
      { "kind": "data", "data": { "operation": "comment | reassign | create_subtask" } }
    ]
  },
  "artifacts": [
    {
      "kind": "artifact",
      "artifactId": "artifact-<uuid>",
      "taskId": "GANG-42",
      "name": "pull-request",
      "parts": [
        { "kind": "file", "file": { "name": "pull-request", "mimeType": "text/uri-list", "uri": "https://github.com/org/repo/pull/7" } },
        { "kind": "text", "text": "Implements password reset endpoint" }
      ]
    }
  ]
}
```

`artifacts` is present only alongside a `completed` submission that reports a
durable output (currently: an opened pull request). Jira identifiers and
other AI Gang integration fields live in the Task's own metadata (tracked
server-side, keyed by `taskId`) — never as fields on the message or artifact
themselves.

ScrumMaster reads `state` and the message's `data` Part (if any) to decide
what to do:

| `state` | `data.operation` | Behavior |
| --- | --- | --- |
| `working` | `comment` | Post comment |
| `working` | `reassign` (`data.agentFieldValue`) | Set Agent field (validated against the agent roster) |
| `working` | `create_subtask` (`data.summary`, `data.description`, `data.agentFieldValue` — required; `data.specificationLink`, `data.artifactLinks` — optional, forwarded onto the created subtask's own record unchanged) | Create subtask under the sending Task's own ticket (agent value validated against the roster), then dispatch it |
| `working` | *(none)* | Plain progress comment (the text Part is posted as-is) |
| `working` | any other value | Refused: a comment naming the unsupported operation and the ones that are supported, and the ticket is left Blocked |
| `input-required` / `auth-required` | *(none — state carries the meaning)* | Set Blocked field + comment |
| `completed` with a `pull-request` artifact | — | Post PR-opened comment only — no transition is made here, and the log reports the status the ticket actually holds; Jenkins owns the In Review transition |
| `completed` without an artifact | — | Post the closing comment, if any |
| `failed` / `canceled` / `rejected` | — | Set Blocked field + comment identifying the terminal failure |

An optional `data.reference: { "file": "...", "function": "..." }` sibling
field is read on the three submissions that consume it: a `comment` operation,
a plain progress note that sets no `data.operation`, and the `input-required` /
`auth-required` states.

A `create_subtask` request that omits `data.agentFieldValue` is not dropped.
ScrumMaster first tries to recover the id from the summary's own
`<Role>: ...` prefix, and uses it only when that prefix names exactly one
agent the project has, other than the agent that sent the request — never a
default, and never the requester itself, which would route the subtask
straight back to the agent that asked for it. If it cannot, nothing is created and
the parent ticket receives a comment naming the missing field, the requested
summary, and the project's permitted agent ids.

Every request ScrumMaster refuses to act on — a missing field, an agent that
failed catalog validation, an operation it has no handler for — also leaves
the ticket Blocked (canonically, `needs-clarification`). A refused request
leaves the parent with no subtask and no reassignment, so it must not go on
reading as ready to work on, and the refusal must not leave the ticket in a
different state depending on which refusal it was. Either way the outcome is
visible: a Task with a rejected submission is recorded and logged as failed,
even when the agent's own container reports that it exited cleanly.

`materializeDecomposition`
and `pipeline_retry` (Jenkins-originated) are
separate, non-A2A payload shapes carried on this same gateway stream — the
former owns its own structured-data contract and the latter is not
agent-authored, so neither follows the A2A envelope contract described above.

---

## Prompt Construction

Every prompt ScrumMaster constructs for Claude Code invocation must include the following sections in order:

```
1. ROLE
   "You are the {agent name}. Read your full agent definition before taking any action:
   /agent-docs/agents/{agent-definition-file}.md"

2. TICKET CONTEXT
   Ticket: {ticket_key} — {ticket_title}
   Parent ticket: {parent_key}  (if subtask)

   Task dispatch only:
     ### Behavior
     {behavior field}

     ### Acceptance Criteria
     {acceptance_criteria field}

     ### Constraints
     {constraints field}

     ### Edge Cases
     {edge_cases field}

     ### Out of Scope
     {out_of_scope field}

     (Story schema fields only — present for Refinement Agent prompts.
      Dev agent prompts use the subtask description written by the Refinement Agent.)

   Unblock flow only:
     Description:
     {subtask description, or "(no description provided)"}

   Retry flow adds neither block: TICKET CONTEXT is just the ticket/parent lines.

3. COMMENT THREAD and RESUME POINT — order depends on flow; task dispatch
   renders neither RESUME POINT nor a distinct COMMENT THREAD ordering issue
   (it has no RESUME POINT at all):

   Unblock flow: COMMENT THREAD (if present), then RESUME POINT.
   Retry flow: RESUME POINT, then COMMENT THREAD (if present).

   COMMENT THREAD (if present) — the lead-in line differs by flow:
     Task dispatch:
       The following clarifications have been provided:
     Unblock and retry flows:
       The following clarifications have been provided (most recent last):
   Then, in every flow, one line per comment:
   [{timestamp}] {author}: {body}

   RESUME POINT (unblock and retry flows; task dispatch has none)
   Unblock:
     You previously stopped work on this ticket and left a BLOCKED marker.
     File: {file path}
     Line: {line number}
     Your note: {blocked marker text}
     Continue from this point using the clarification provided above.
   Retry, pipeline failure:
     The project pipeline failed on your pull request for this ticket.
     Build log: {build_url}
     Build number: {build_number}
     Diagnose the failure, fix it, and push the fix to the same branch/PR.
   Retry, human rework:
     A human reviewer requested rework after reviewing this ticket on beta.
     Read the comment thread below for the specific rework requested, address
     it, and push the fix to the same branch/PR.

4. ALLOWED AGENTS (only when the dispatch supplies an allowed-agent set,
   which task dispatches do and continuations and retries do not)
   - {agent id}: {agent card description}   (one line per permitted agent)

   Project data for delegation, rendered from the canonical catalog filtered
   to what the project enables — not an instruction about messaging. The
   Refinement Agent names one of these ids as the agent a subtask is for.

5. WORK ITEM REFERENCES
   Work item id: {canonical id}
   External key: {tracker key}                            — only when set (Jira mode)
   Specification link: {artifact id} ({requirement id})   — or "none"
   Artifact links: {artifact id}, {artifact id}           — or "none"

   The work item's own references, which the agent reads to ask the librarian
   for an artifact. AI Gang canonical ids and, in Jira mode only, a
   display-only tracker key — never a delivered path, and never read back by
   any ScrumMaster module (V5.1 REQ-06, REQ-07). A work item carrying neither
   specification nor artifact reference renders "none" rather than omitting
   the line (v4.1 agent-artifact-automation.md REQ-04).
```

No prompt instructs an agent how to construct a submission, and none carries
an identifier for an agent to copy into one. An agent submits through the
constructor in the agent commons, which the `a2a-submit` skill describes; the
task, the context and the chain reach it as environment variables the
subscriber exports into the session (V5.0 Deterministic Gateway Message
Tooling REQ-04, Agent Commons REQ-02, REQ-03).

---

## Agent Catalog and Assignment Validation

ScrumMaster loads two version-controlled configuration files at startup,
failing fast if either is malformed (`src/registry.js`):

- `config/agents.json` — the canonical catalog of registered agent identities:
  a stable `id`, `displayName`, `definitionPath`, `routing.channelSuffix`, and
  `agentCard` (name + description) per entry, plus an optional
  `retiredAgents` list for formally retired ids whose historical Jira values
  must remain readable.
- `config/projects.json` — each project's effective allowed-agent subset,
  referencing `agents.json` ids only; an unknown id fails startup validation.

```json
// config/agents.json
{
  "agents": [
    {
      "id": "backend-agent",
      "displayName": "Backend Agent",
      "definitionPath": "/agent-docs/agents/backend-agent.md",
      "routing": { "channelSuffix": "backend" },
      "agentCard": { "name": "Backend Agent", "description": "Implements APIs, business logic, and data persistence." }
    }
  ],
  "retiredAgents": []
}
```

```json
// config/projects.json
{
  "projects": [
    { "name": "hello-world", "jiraProjectKey": "HW", "agents": ["refinement-agent", "backend-agent", "frontend-agent", "devops-agent"] }
  ]
}
```

Each registry entry also carries a nested `agentCard` block (name, description,
version, skills, and so on) from which ScrumMaster derives an A2A AgentCard —
see `services/scrummaster/src/a2a/agentCard.js`. Registry load
fails fast if any entry cannot produce a valid AgentCard, or if the catalog or
project configuration itself is malformed.

`src/assignment.js` is the sole catalog-backed validator: every path that
creates or changes agent responsibility (initial refinement dispatch, Shovel
Ready dispatch, blocked-clear redispatch, and the Refinement Agent's
decomposition tool, plus the gateway's `reassign` and `create_subtask` A2A
operations) calls it rather than writing a literal agent id to Jira or
dispatching an agent directly. An invalid assignment is never silently
dropped — it produces a durable, visible Jira comment naming the requested
agent, the failure reason, and the permitted ids for that project, and the
ticket is left Blocked rather than reported as dispatched.

When a new agent type is added to the system, add an entry to `agents.json`
(including its `agentCard`), a definition file in `setup/`, and (if
applicable) the agent's id to the relevant project's entry in
`projects.json` — no ScrumMaster code changes are required. See Agent
Catalog and Assignment
Integrity for the full
contract, including Jira Agent-field provisioning/reconciliation and the
periodic drift audit.

---

## Deployment

ScrumMaster runs as a persistent container on the HQ droplet alongside Jenkins and Redis.

**Directory layout:**

```
~/ai-gang/
├── setup/          # Agent definitions
├── projects/       # Project containers
├── jenkins/        # Jenkins master
└── services/
    ├── redis/          # Redis container
    └── scrummaster/    # ScrumMaster service
        ├── Dockerfile
        ├── docker-compose.yml
        ├── config/
        │   ├── agents.json
        │   └── projects.json
        └── src/
```

**Runtime requirements:**

- Network access to Django/`core` (Docker network) — ScrumMaster's source of
  canonical work-item events and destination for canonical commands
- Network access to Redis container (Docker network)
- Read access to project container workspaces (for BLOCKED marker search)
- No network access to Atlassian Cloud and no exposed port: Django/`core` is
  the platform's only Jira client and its only externally-reachable surface
  (V5.1 REQ-01)

**Environment variables required:**

```
REDIS_HOST           # Redis container hostname on Docker network
REDIS_PORT           # Default 6379
AGENTS_CATALOG_PATH   # Path to agents.json (canonical agent catalog)
PROJECTS_CONFIG_PATH  # Path to projects.json (per-project allowed-agent set)
PROJECTS_BASE_PATH    # Path to ~/ai-gang/projects/ for BLOCKED marker search

# Redis Streams tuning
STREAM_RETENTION_DAYS      # Default 7 — acknowledged stream history retention
DEAD_LETTER_RETENTION_DAYS # Default 30 — dead-letter entry retention
```

---

## Constraints & Boundaries

**ScrumMaster does not:**

- Make decisions about ticket content or decomposition
- Interact with GitHub or Jenkins
- Execute code or run tests
- Store persistent state beyond what Redis provides — it is stateless between webhook events
- Trust project identity claims in message payloads — project scope is derived from the Stream a message was consumed from only

**ScrumMaster assumes:**

- Jira custom fields (Agent, Blocked) are configured before first run
- All watched Jira project webhooks are registered and point to ScrumMaster's endpoint
- The agent catalog (agents.json) and project configuration (projects.json) are accurate and up to date

**On message delivery:**
Redis Streams durably persist every accepted message. A project container does not need to be running when ScrumMaster dispatches work — the entry waits in its stream, pending in the container's consumer group, until that container's `subscriber.js` starts (or resumes after a restart) and consumes it. Redis persistence (AOF) must be enabled so this durability survives a Redis restart too — see Durable Agent Messaging with Redis Streams for retry, dead-letter, and retention behavior.

---

**Author**: AI Gang Team
**Version**: 1.1
**Date**: March 25, 2026
**Status**: Current — reflects implemented service