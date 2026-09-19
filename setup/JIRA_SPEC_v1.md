# AI Gang - Jira Configuration Specification

**Purpose**: Defines the complete Jira configuration required to support the AI Gang automated development lifecycle. Intended as a setup reference for human administrators and a behavioral reference for the Refinement Agent and ScrumMaster.

**Date**: March 25, 2026
**Version**: 1.1
**Status**: Current — reflects implemented configuration

---

## Table of Contents
1. [Overview](#overview)
2. [Instance Configuration](#instance-configuration)
3. [Custom Fields](#custom-fields)
4. [Story Schema Fields](#story-schema-fields)
5. [Workflow Statuses](#workflow-statuses)
6. [Project Structure](#project-structure)
7. [Automation Rules](#automation-rules)
8. [Webhook Configuration](#webhook-configuration)
9. [Agent Roster](#agent-roster)

---

## Overview

Jira is the single source of truth for all work in the AI Gang system. Every piece of code written by an agent traces back to a Jira ticket. The configuration defined here supports a fully automated lifecycle:

```
Human creates Story in Jira
  → ScrumMaster validates required story fields
  → If incomplete: ticket blocked with comment listing missing fields
  → If complete: Refinement Agent decomposes into subtasks
  → Dev agents receive subtasks via ScrumMaster
  → Ticket closes on merge
```

### Jira Access by Role

| Role | Jira Access | Notes |
|------|-------------|-------|
| Human | Full | Creates tickets, responds to blockers, clears Blocked field |
| ScrumMaster | Full read/write | All Jira interactions for agents are proxied through ScrumMaster |
| Dev Agents | None | Submit to the ScrumMaster gateway Redis Stream (`aigang:gateway:{project-name}`); ScrumMaster executes on their behalf |
| Refinement Agent | None | Same as dev agents — Jira access via ScrumMaster only |
| Jenkins | Comment + status | Posts pipeline results directly via Jira plugin |

---

## Instance Configuration

**Host**: Atlassian Cloud
**URL**: `https://your-org.atlassian.net` *(replace with actual org URL)*
**Authentication**: API token — ScrumMaster holds the single service token

### API Tokens Required

| Consumer | Purpose | Scope |
|----------|---------|-------|
| ScrumMaster | Read tickets, post comments, update fields, create subtasks | All projects |
| Jenkins | Post comments, update status | All projects |

Tokens are generated at `https://id.atlassian.com/manage-profile/security/api-tokens` and stored in each service's `.env` file. See DevOps Handbook for secrets storage procedure.

---

## Custom Fields

These fields are **instance-level** — created once for the whole Jira instance via `scripts/create-jira-fields.sh`. Their IDs are stable and written automatically to `services/scrummaster/.env`. When adding a new project, these fields are applied to the project's screens by `scripts/init-project.sh`.

### Field: Agent

**Purpose**: Identifies which agent owns a ticket. Used by ScrumMaster to route inbound webhook events to the correct agent container via Redis.

| Attribute | Value |
|-----------|-------|
| Field name | `Agent` |
| Field type | Single-select |
| Scope | All projects |
| Set by | ScrumMaster — `refinement-agent` on story creation; agent field value on subtasks |
| Read by | ScrumMaster — determines routing target |
| Env var | `JIRA_AGENT_FIELD_ID` |

**Allowed values**: `refinement-agent`, `frontend-agent`, `backend-agent`, `devops-agent`. See [Agent Roster](#agent-roster). Values must match Agent Registry entries in ScrumMaster exactly.

---

### Field: Blocked

**Purpose**: Signals that an agent is waiting for human clarification. Setting this field causes ScrumMaster to post a system comment. Clearing it triggers the unblock flow — ScrumMaster fetches ticket context and re-dispatches the assigned agent.

| Attribute | Value |
|-----------|-------|
| Field name | `Blocked` |
| Field type | Single-select (one option: `Yes`) |
| Scope | All projects |
| Set by | ScrumMaster (on behalf of dev agent, or when story fails validation) |
| Cleared by | Human only |
| Read by | ScrumMaster — `jira:issue_updated` webhook detects when field is cleared |
| Env var | `JIRA_BLOCKED_FIELD_ID` |

To set: `{ "value": "Yes" }`. To clear: `null`.

---

## Story Schema Fields

These fields define the required structure of a Story before it can be refined. ScrumMaster validates that all required fields are non-empty before dispatching to the Refinement Agent. If any required field is missing, ScrumMaster blocks the ticket and posts a comment listing what needs to be filled in.

All story schema fields are paragraph (textarea) type. All are **instance-level** custom fields created by `scripts/create-jira-fields.sh`.

| Field Name | Required | Env Var | Purpose |
|-----------|----------|---------|---------|
| `Value Hypothesis` | No | `JIRA_VALUE_HYPOTHESIS_FIELD_ID` | Why this story matters; expected behavioral change |
| `Test & Measurement` | No | `JIRA_TEST_MEASUREMENT_FIELD_ID` | Metric, time window, success threshold |
| `Behavior` | **Yes** | `JIRA_BEHAVIOR_FIELD_ID` | Declarative statements of system behavior — drives subtask creation |
| `Acceptance Criteria` | **Yes** | `JIRA_AC_FIELD_ID` | Deterministic, testable conditions the implementation must satisfy |
| `Constraints` | **Yes** | `JIRA_CONSTRAINTS_FIELD_ID` | Explicit limits, formats, validation rules. Enter `N/A` if none. |
| `Edge Cases` | **Yes** | `JIRA_EDGE_CASES_FIELD_ID` | Invalid input, partial failure, duplicates, timeouts, state inconsistencies. Enter `N/A` if none. |
| `Out of Scope` | **Yes** | `JIRA_OUT_OF_SCOPE_FIELD_ID` | Explicitly what is NOT included in this story. Enter `N/A` if none. |

**Validation gate**: ScrumMaster checks Behavior, Acceptance Criteria, Constraints, Edge Cases, and Out of Scope on every `jira:issue_created` event. Value Hypothesis and Test & Measurement are informational and not validated.

**Refinement Agent context**: ScrumMaster includes Behavior, Acceptance Criteria, Constraints, Edge Cases, and Out of Scope in the prompt sent to the Refinement Agent. Value Hypothesis and Test & Measurement are not passed through.

---

## Workflow Statuses

The following statuses apply to all projects. The board displays one column per status.

| Status | Meaning | Transition Trigger |
|--------|---------|-------------------|
| `Backlog` | Ticket created, not yet refined | Human creates ticket |
| `Shovel Ready` | Refined, subtasks assigned, ready for dev | Refinement Agent |
| `In Progress` | Agent actively working | ScrumMaster (on Shovel Ready webhook) |
| `In Review` | PR open, pipeline running | Jenkins (on PR open) |
| `Done` | Merged and deployed to staging | Jenkins (on merge to main) |

`Blocked` is a field state (the Blocked custom field set to `Yes`), not a standalone workflow status. A ticket can be `In Progress` and blocked simultaneously.

### Status Transition Rules

```
Backlog → Shovel Ready        Refinement Agent (via jira-gateway)
Shovel Ready → In Progress    ScrumMaster only (automated on Shovel Ready webhook)
In Progress → In Review       Jenkins only (on PR open)
In Review → In Progress       Jenkins only (on pipeline failure)
In Review → Done              Jenkins only (on merge to main)
```

---

## Project Structure

### One Jira Project Per Application

Each application in the AI Gang system has its own Jira project with its own ticket key prefix.

Example:
```
Project: User Service        Key prefix: US
Project: Landing Page        Key prefix: LP
Project: Analytics Pipeline  Key prefix: AP
```

### Routing

ScrumMaster routes issues to the correct container using the **Jira project name**, which `init-project.sh` sets to match the `PROJECT_NAME` env var in the container (e.g. `hello-world`). No Component field is required.

### Board Configuration

Each project board is configured identically:
- One column per workflow status
- Blocked field surfaced as a visual indicator on ticket cards
- Agent field visible on ticket cards
- Swimlanes by Agent field value (optional but recommended for visibility)

### Ticket Hierarchy

```
Epic
  └── Story                  (created by human — must include all required schema fields)
        └── Subtask           (created by Refinement Agent via jira-gateway)
              └── assigned to a single dev agent via Agent field
```

The Refinement Agent creates subtasks under the parent story. Each subtask is assigned to exactly one agent via the Agent field. The subtask description contains the full build prompt written by the Refinement Agent. ScrumMaster operates at the subtask level for dev agent dispatch.

---

## Automation Rules

### Rule 1: PR Merged → Done

| Attribute | Value |
|-----------|-------|
| Trigger | GitHub pull request merged (via GitHub for Jira app) |
| Condition | Branch name contains issue key |
| Action | Transition issue to Done |
| Notes | Requires GitHub for Jira app installed and connected |

### Rule 2: Blocked Field Set → Add Board Indicator

| Attribute | Value |
|-----------|-------|
| Trigger | Blocked field changed to `Yes` |
| Condition | None |
| Action | Add label `blocked` to ticket for board visibility |
| Notes | Visual only — does not affect workflow status |

### Rule 3: Blocked Field Cleared → Remove Board Indicator

| Attribute | Value |
|-----------|-------|
| Trigger | Blocked field cleared (set to null) |
| Condition | None |
| Action | Remove label `blocked` from ticket |
| Notes | ScrumMaster webhook fires separately and re-dispatches the assigned agent |

> Note: Agent field assignment on new tickets is handled by ScrumMaster, not a Jira automation rule.

---

## Webhook Configuration

One instance-level webhook. ScrumMaster routes all events internally — no per-project webhook registration needed.

### Webhook Endpoint

```
http://{hq-droplet-ip}:9000/webhook/jira
```

All inbound requests must include the shared secret as a header: `X-Webhook-Secret: {value}`. Requests missing or mismatching the secret are rejected with 401.

### Registered Events

| Event | Jira `webhookEvent` value | ScrumMaster Handler |
|-------|--------------------------|---------------------|
| Story created | `jira:issue_created` | Validate schema fields; assign to Refinement Agent; dispatch or block |
| Status → Shovel Ready | `jira:issue_updated` (status change) | Fetch context; build prompt; dispatch dev agent; transition to In Progress |
| Blocked field cleared | `jira:issue_updated` (field change) | Fetch context + BLOCKED marker; build prompt; re-dispatch agent |

### Event Filtering

ScrumMaster discards events that do not match a handled trigger. Unhandled events are logged and dropped — no error response. Webhook JQL filter can be left empty; ScrumMaster handles filtering internally.

---

## Agent Roster

The Agent field is a human-visible projection of the canonical agent catalog
(`services/scrummaster/config/agents.json`) — not an independent source of truth. Its
options are generated and reconciled from that catalog by
`scripts/create-jira-fields.sh` (initial provisioning) and
`scripts/reconcile-agent-field.sh` (ongoing sync as the catalog changes), and
audited for drift by ScrumMaster at startup and every 24 hours. Jira is never
consulted to decide whether an agent identity is valid for runtime
assignment — that is decided solely by the catalog plus each project's
`services/scrummaster/config/projects.json` entry.

New agents added to the system require: (1) a new entry in
`services/scrummaster/config/agents.json`, (2) a definition file in `setup/`, (3) the
project's `services/scrummaster/config/projects.json` entry updated if that project
should be able to assign it, and (4) `scripts/reconcile-agent-field.sh` run to
add the corresponding Jira option. Retiring an agent removes it from
`agents.json`'s `agents` array and adds it to `retiredAgents` — its Jira
option is disabled, not deleted, so historical work referencing it stays
readable.

---

**Author**: AI Gang Team
**Version**: 1.1
**Date**: March 25, 2026
**Status**: Current — reflects implemented configuration
