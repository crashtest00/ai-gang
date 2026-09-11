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

ScrumMaster is a persistent service running on the AI Gang HQ droplet. It has two jobs: listen for events from Jira and route them to the correct agent, and listen for messages from agents and act on Jira on their behalf.

No agent except the Refinement Agent has direct access to Jira. All Jira reads and writes for dev agents flow through ScrumMaster. This gives a single point of control for all Jira interactions — formatting, error handling, rate limiting, and audit logging live here.

ScrumMaster does not make decisions about work. It routes, fetches context, constructs prompts, and relays. The Refinement Agent makes decisions about tickets. Dev agents make decisions about code.

---

## Responsibilities

**ScrumMaster owns:**

- Receiving and processing all inbound Jira webhooks
- Fetching full ticket context from the Jira API when building agent prompts
- Routing inbound events to the correct agent via Redis
- Receiving outbound messages from all agents via Redis
- Writing to Jira on behalf of all agents (comments, field updates, subtask creation)
- Enforcing comment formatting standards before posting to Jira
- Including the correct agent definition path in every Claude Code invocation prompt

**ScrumMaster does NOT own:**

- Ticket content decisions (Refinement Agent)
- Code (dev agents)
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
│  │   - Fetches full ticket from Jira API                 │  │
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
│  │   - Executes the corresponding Jira API call            │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │ Redis (Streams + consumer groups — durable, ack'd)    │  │
│  │                                                       │  │
│  │  Jira webhook ingestion (Jira -> ScrumMaster):        │  │
│  │   aigang:webhooks:{project}      group: scrummaster   │  │
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

ScrumMaster validates Behavior, Acceptance Criteria, Constraints, Edge Cases, and Out of Scope on every `jira:issue_created` event before dispatching to the Refinement Agent. See Handler 1.

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

ScrumMaster registers to receive Jira webhooks for the following events:

| Event                 | Trigger Condition                        | Handler                                         |
| --------------------- | ---------------------------------------- | ----------------------------------------------- |
| Ticket Created        | Any new ticket in any watched project    | Build prompt, durably dispatch to Refinement Agent's Stream |
| Status → Shovel Ready | Ticket status changes to Shovel Ready    | Build prompt, invoke assigned dev agent         |
| Blocked field cleared | Blocked field changes from true to false | Fetch ticket context, invoke assigned dev agent |

---

## Inbound Webhook Handling

### Handler 1: Story Created

Fired when a new Story is created in any watched Jira project.

ScrumMaster owns story readiness. The Refinement Agent only ever receives a story when all required schema fields are non-empty. This check is hardcoded in ScrumMaster — the Refinement Agent does not validate fields.

**Action:**

1. Validate webhook secret
2. Set Agent field to `refinement-agent`
3. Post acknowledgement comment: "Ticket received. Assigned to Refinement Agent for decomposition."
4. Fetch full ticket context from Jira API (including all story schema fields)
5. **Validate required fields**: Behavior, Acceptance Criteria, Constraints, Edge Cases, Out of Scope
   - If any are missing/empty: post comment listing missing fields, set Blocked field to `Yes`, stop
   - If all present: continue
6. Construct Claude Code prompt including the 5 required schema fields
7. Durably dispatch prompt to `aigang:agent:{project}:{suffix}`
8. Log event

### Handler 2: Status → Shovel Ready

Fired when the Refinement Agent moves a subtask ticket to Shovel Ready.

**Action:**

1. Validate webhook payload
2. Read ticket key, Agent field value, and project key
3. Look up agent definition path from agent catalog using Agent field value
4. Fetch full ticket context from Jira API:

   - Title
   - Description
   - Acceptance criteria
   - Full comment thread
   - Parent ticket if subtask
5. Construct Claude Code prompt (see Prompt Construction)
6. Durably dispatch prompt to `aigang:agent:{project}:{suffix}`
7. Update ticket status: Shovel Ready → In Progress
8. Log event

### Handler 3: Blocked Field Cleared

Fired when a human clears the Blocked field on a ticket, indicating a response has been provided.

**Action:**

1. Validate webhook payload
2. Read ticket key, Agent field value, and project key
3. Look up agent definition path from agent catalog
4. Fetch full ticket context from Jira API including the full comment thread
5. Search project codebase for `BLOCKED {ticket-key}` marker to identify resume point
6. Construct Claude Code prompt (see Prompt Construction) including:

   - Full ticket and comment thread
   - File path and line reference of BLOCKED marker if found
7. Durably dispatch prompt to `aigang:agent:{project}:{suffix}`
8. Log event

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

All comments posted to Jira by ScrumMaster on behalf of an agent must follow this format:

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

ScrumMaster enforces this format. If an agent submits a comment that does not include a reference field, ScrumMaster logs a warning and posts the comment with the reference field omitted rather than dropping the message.

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
"jira_operation"` — see A2A
Messaging. There is no separate
legacy `type`/`prompt` contract on this path.

`taskId` = the ticket's own Jira issue key; `contextId` = its parent ticket
key (or the ticket's own key when there is no parent) — minted the first time
ScrumMaster sees the root ticket, and shared by every subtask created under
it. A Task's identity is stable for its whole lifecycle: one Jira ticket ↔ one
Task, from initial dispatch through completion. Unblocking a ticket, a
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
`jiraProjectName` metadata recorded at dispatch time. An agent cannot claim a
different project by writing a different value into its submission — the
stream is the authority, and an envelope whose declared project doesn't match
is rejected and dead-lettered without any Jira effect.

Envelope `kind: "jira_operation"`. `payload` is one A2A submission:

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
| `working` | `create_subtask` (`data.summary`, `data.description`, `data.agentFieldValue`) | Create subtask under the sending Task's own ticket (agent value validated against the roster), then dispatch it |
| `working` | *(none)* | Plain progress comment (the text Part is posted as-is) |
| `input-required` / `auth-required` | *(none — state carries the meaning)* | Set Blocked field + comment |
| `completed` with a `pull-request` artifact | — | Post PR-opened comment only — ticket stays In Progress; Jenkins owns the In Review transition |
| `completed` without an artifact | — | Post the closing comment, if any |
| `failed` / `canceled` / `rejected` | — | Set Blocked field + comment identifying the terminal failure |

An optional `data.reference: { "file": "...", "function": "..." }` sibling
field is supported on any operation.

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
   /agent-docs/{agent-definition-file}.md"

2. TICKET CONTEXT
   Ticket: {ticket_key} — {ticket_title}
   Parent ticket: {parent_key}  (if subtask)

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

3. COMMENT THREAD (if present)
   The following clarifications have been provided:
   [{timestamp}] {author}: {body}

4. RESUME POINT (unblock flow only)
   File: {file path}
   Line: {line number}
   Your note: {blocked marker text}
   Continue from this point using the clarification provided above.

5. A2A TASK CONTEXT
   Task ID: {task.id}
   Context ID: {task.contextId}
   Last Message ID: {message.messageId}
   Jira issue key: {issue.key}

   These identify the Task this dispatch belongs to. The agent must echo them
   back (with a freshly generated messageId per submission, referencing the
   previous one) in every gateway submission it sends for this ticket — see
   A2A Messaging.

6. INSTRUCTIONS
   - Read your agent definition fully before taking any action
   - All Jira interactions must go through the ScrumMaster gateway stream as
     a canonical A2A submission (see Redis Message Contract) — never a bare
     {"type": ...} payload
   - Use: node /agent-docs/lib/gateway-publish.js $PROJECT_NAME <path-to-json-file>
   - If you need clarification, publish a submission with "state" set to
     input-required (missing information) or auth-required (missing
     authorization) and the exact file/function reference in the message
   - Do not block without a precise, located question
   - When your work is complete, publish a submission with "state" set to
     completed and a summary text Part
```

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
      "definitionPath": "/agent-docs/backend-agent.md",
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

- Network access to Atlassian Cloud (outbound HTTPS)
- Network access to Redis container (Docker network)
- Read access to project container workspaces (for BLOCKED marker search)
- Jira API token with read/write access to all watched projects
- Exposed port for receiving Jira webhooks

**Environment variables required:**

```
JIRA_BASE_URL                    # https://your-org.atlassian.net
JIRA_API_TOKEN                   # Stored in secrets, not in compose file
JIRA_USER_EMAIL                  # Account email associated with API token

# Custom field IDs — written automatically by scripts/create-jira-fields.sh
JIRA_AGENT_FIELD_ID              # customfield_XXXXX
JIRA_BLOCKED_FIELD_ID            # customfield_XXXXX
JIRA_VALUE_HYPOTHESIS_FIELD_ID   # customfield_XXXXX
JIRA_TEST_MEASUREMENT_FIELD_ID   # customfield_XXXXX
JIRA_BEHAVIOR_FIELD_ID           # customfield_XXXXX
JIRA_AC_FIELD_ID                 # customfield_XXXXX
JIRA_CONSTRAINTS_FIELD_ID        # customfield_XXXXX
JIRA_EDGE_CASES_FIELD_ID         # customfield_XXXXX
JIRA_OUT_OF_SCOPE_FIELD_ID       # customfield_XXXXX

REDIS_HOST           # Redis container hostname on Docker network
REDIS_PORT           # Default 6379
WEBHOOK_SECRET        # Shared secret for validating inbound Jira webhooks
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