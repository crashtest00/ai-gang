# AI Gang - Jira Configuration Specification

**Purpose**: Defines the complete Jira **instance and project configuration** an AI Gang deployment needs in order to connect a project to Jira — custom fields, story-schema fields, statuses, project structure, automation rules, webhook registration and the Agent field's roster. Intended as a setup reference for human administrators.

**This document describes configuration, not behaviour.** It is not a behavioural reference for any agent. What the platform does with a work item is defined by the canonical work model and, for ScrumMaster, by `SCRUMMASTER_SPEC_v1.md`; from V5.1 ScrumMaster is not a Jira client and reads and writes nothing in Jira (V5.1 REQ-01, REQ-04). Where this document has to name a component, it names Django/`core`, the running platform's only Jira client, and says plainly where the behaviour does not exist yet.

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

Jira is an **optional tracker front-end**, configured per project. The single
source of truth for all work in the AI Gang system is the canonical work item
in Django/`core`; a Jira ticket is a projection of one, for a project the
operator has put in Jira mode.

Jira mode is **off for V5.1's release**: every project runs in local mode, so
nothing in the configuration below is exercised at runtime yet. It is set up
now because it is one-time instance work, and because v5.2 — which builds the
outbound writer and turns Jira mode back on — needs it already in place.

What the configuration supports, once a project is in Jira mode:

```
Human creates Story in Jira
  → core's webhook consumer interprets the payload and records a canonical
    work item
  → Refinement Agent decomposes it into subtasks
  → Dev agents receive subtasks from core's events, routed by ScrumMaster
  → the work item closes on merge
```

Story-field validation and the missing-fields comment are **v5.2's**: the
required-field list below describes what a Story should carry, and from v5.1
no component blocks a ticket or comments on one for missing fields.

### Jira Access by Role

| Role | Jira Access | Notes |
|------|-------------|-------|
| Human | Full | Creates tickets, responds to blockers, clears Blocked field |
| Django/`core` | Full read/write | The running platform's only Jira client (V5.1 REQ-01). Inbound: receives the instance webhook. Outbound: no running consumer calls the client until v5.2's writer, besides `ensure_jira_webhook`'s own registration |
| ScrumMaster | None | Not a Jira client. Reads canonical work items from `core` and publishes canonical commands back to it (V5.1 REQ-04) |
| Dev Agents | None | Submit to the ScrumMaster gateway Redis Stream (`aigang:gateway:{project-name}`); ScrumMaster publishes the canonical command to `core` on their behalf |
| Refinement Agent | None | Same as dev agents |
| Jenkins | Comment + status | Posts pipeline results directly via its own Jira plugin, keyed by the branch name's ticket key. This path is unchanged in V5.1 and retires in v5.2 |

---

## Instance Configuration

**Host**: Atlassian Cloud
**URL**: `https://your-org.atlassian.net` *(replace with actual org URL)*
**Authentication**: API token — held in the platform `.env` as `JIRA_TOKEN`, which `scripts/startup/derive-env.sh` carries into `services/core/.env` for Django/`core`

### API Tokens Required

| Consumer | Purpose | Scope |
|----------|---------|-------|
| Django/`core` | Register the instance webhook; read tickets, post comments, update fields and create subtasks from v5.2's outbound writer | All projects |
| Jenkins | Post comments, update status | All projects |
| Provisioning scripts | Create and reconcile instance fields and per-project configuration | All projects |

One token serves all of them. It is generated at `https://id.atlassian.com/manage-profile/security/api-tokens` and set once in the platform `.env` as `JIRA_TOKEN`; `derive-env.sh` carries it to `core`, and Jenkins reads it from its own credential (`jira-token`). See DevOps Handbook for secrets storage procedure.

---

## Custom Fields

These fields are **instance-level** — created once for the whole Jira instance via `scripts/create-jira-fields.sh`, which records the IDs it created in `services/scrummaster/.env`. From V5.1 no running service reads that output; the operator copies the IDs into the platform `.env`, which `derive-env.sh` carries to `core`. The IDs are stable. When adding a new project, these fields are applied to the project's screens by `scripts/init-project.sh`.

### Field: Agent

**Purpose**: Identifies which agent owns a ticket. `core` carries its value onto the canonical work item it records from the webhook; routing to an agent container is then decided from the canonical work item, not from Jira.

| Attribute | Value |
|-----------|-------|
| Field name | `Agent` |
| Field type | Single-select |
| Scope | All projects |
| Set by | `core`'s outbound writer, for a Jira-mode project (canonical-delivery-state.md REQ-09); a human or the provisioning scripts may also set it |
| Read by | `core`'s webhook interpretation, which carries the value onto the canonical work item |
| Env var | `JIRA_AGENT_FIELD_ID` |

**Allowed values**: `refinement-agent`, `frontend-agent`, `backend-agent`, `devops-agent`. See [Agent Roster](#agent-roster). Values must match the canonical agent catalog's entries exactly.

---

### Field: Blocked

**Purpose**: Signals that an agent is waiting for human clarification. A human clears it; `core` interprets the clearing webhook and records the canonical change, from which the assigned agent is re-dispatched.

| Attribute | Value |
|-----------|-------|
| Field name | `Blocked` |
| Field type | Single-select (one option: `Yes`) |
| Scope | All projects |
| Set by | `core`'s outbound writer, for a Jira-mode project (canonical-delivery-state.md REQ-09); a human may also set it |
| Cleared by | Human only |
| Read by | `core` — the `jira:issue_updated` webhook tells it the field was cleared |
| Env var | `JIRA_BLOCKED_FIELD_ID` |

To set: `{ "value": "Yes" }`. To clear: `null`.

---

## Story Schema Fields

These fields define the required structure of a Story before it can be refined. **Nothing enforces them in V5.1**: no component validates them, blocks a ticket or comments on one for missing fields. v5.2's outbound writer posts the missing-fields comment; until then the list is a convention for whoever writes the Story.

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

**Which fields the validation gate will cover** (v5.2): Behavior, Acceptance Criteria, Constraints, Edge Cases, and Out of Scope. Value Hypothesis and Test & Measurement are informational and are not part of it.

**Refinement Agent context**: Behavior, Acceptance Criteria, Constraints, Edge Cases, and Out of Scope reach the Refinement Agent's prompt, from the canonical work item's own story fields. Value Hypothesis and Test & Measurement are not passed through.

---

## Workflow Statuses

The following statuses must exist on every project's workflow, and the board
displays one column per status. They are the Jira statuses a project's mapping
projects the canonical work item's status onto; the canonical statuses
themselves are `backlog`, `ready`, `in-progress`, `in-review` and `done`.

| Status | Meaning |
|--------|---------|
| `Backlog` | Ticket created, not yet refined |
| `Shovel Ready` | Refined, subtasks assigned, ready for dev |
| `In Progress` | Agent actively working |
| `In Review` | PR open, pipeline running |
| `Done` | Merged and deployed to beta |

`Blocked` is a field state (the Blocked custom field set to `Yes`), not a standalone workflow status. A ticket can be `In Progress` and blocked simultaneously.

### Who Moves a Ticket

Status changes are made on the canonical work item; `core`'s outbound writer
(Canonical Delivery State REQ-09) makes the equivalent Jira write for a
project in Jira mode, and the canonical status follows from Jira's webhook —
no canonical row changes before the webhook returns. Jenkins holds no Jira
credential and writes to no tracker: it reports a beta deployment as a
canonical event carrying no ticket key, and `core` makes the transition
(REQ-04, REQ-05, REQ-06). The transitions this configuration must permit, and
who makes each:

| Transition | Who / What | Written to Jira |
|---|---|---|
| `Backlog` → `Shovel Ready` | Refinement Agent, via the ScrumMaster gateway, through `core`'s writer | Yes |
| `Shovel Ready` → `In Progress` | ScrumMaster, on dispatch, as a canonical command to `core`, through the writer | Yes |
| `In Progress` → `In Review` | `core`, on a beta deployment Jenkins reports as a canonical event, through the writer | Yes |
| `In Review` → `Done` | A human, in Jira — moving a ticket to Done is no longer any automation rule's action | By the human directly |

A human may of course move a ticket in Jira directly; `core` interprets the
resulting webhook for a project in Jira mode. From v5.2 no component moves a
ticket back out of `In Review` on a failed build: Jenkins' pipeline-failure
handler appends the failure comment through `core`'s comment path and
leaves the ticket's status as it is (REQ-01); only a human, or a later
beta deployment reaching `In Review` again, changes it further.

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

`core` resolves a webhook to the right project using the **Jira project name**, which `init-project.sh` sets to match the `PROJECT_NAME` env var in the container (e.g. `hello-world`). Routing to a container is then decided from the canonical work item. No Component field is required.

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
        └── Subtask           (created by the Refinement Agent through the
                               gateway stream, aigang:gateway:{project})
              └── assigned to a single dev agent via Agent field
```

The Refinement Agent creates subtasks under the parent story — as canonical work items, which v5.2's writer mirrors into Jira. Each subtask is assigned to exactly one agent. The subtask description contains the full build prompt written by the Refinement Agent. Dev agent dispatch operates at the subtask level.

---

## Automation Rules

**Retired, v5.2 (Canonical Delivery State REQ-04; product owner, October 2,
2026, preliminary review decision 7.1).** The instance ran three automation
rules through v5.1; none is enabled from v5.2, and `core` takes over none of
them — the live Jira-mode proof (§5 of the specification) confirms no rule is
enabled. They are kept below as a record of what the instance no longer
runs, not as current configuration.

Rule 1 moved a ticket to Done at merge, before its human review on beta —
before `core`'s writer existed to make that transition deliberately, and
colliding with the beta-queue-clean check once one did. Done remains a
human's move, made in Jira and read back through the webhook ("Who Moves a
Ticket", above). Rules 2 and 3 only added and removed a label for board
visibility; the board now shows the Blocked field itself on ticket cards
(see Board Configuration, above), so no label mirror is needed.

### Rule 1: PR Merged → Done (retired)

| Attribute | Value |
|-----------|-------|
| Trigger | GitHub pull request merged (via GitHub for Jira app) |
| Condition | Branch name contains issue key |
| Action | Transition issue to Done |
| Notes | Requires GitHub for Jira app installed and connected |

### Rule 2: Blocked Field Set → Add Board Indicator (retired)

| Attribute | Value |
|-----------|-------|
| Trigger | Blocked field changed to `Yes` |
| Condition | None |
| Action | Add label `blocked` to ticket for board visibility |
| Notes | Visual only — does not affect workflow status |

### Rule 3: Blocked Field Cleared → Remove Board Indicator (retired)

| Attribute | Value |
|-----------|-------|
| Trigger | Blocked field cleared (set to null) |
| Condition | None |
| Action | Remove label `blocked` from ticket |
| Notes | `core` interprets the same change separately, and the assigned agent is re-dispatched from the canonical work item |

> Note: the Agent field is not maintained by a Jira automation rule. `core`'s
> outbound writer writes it back to Jira, for a project in Jira mode
> (Canonical Delivery State REQ-09).

---

## Webhook Configuration

One instance-level webhook, registered on Django/`core` — no per-project
webhook registration needed. Registration is not a manual step and is not a
shell script's job either: `init-project.sh --connect-jira` runs `core`'s own
`ensure_jira_webhook` management command, which is idempotent.

### Webhook Endpoint

```
${HQ_URL}/webhooks/jira?secret=${WEBHOOK_SECRET}
```

`core` compares the `secret` query parameter against its own
`WEBHOOK_SECRET`; a missing or mismatched value is rejected. The operator sets
`WEBHOOK_SECRET` once in the platform `.env` and `derive-env.sh` carries it
into `services/core/.env`.

### Registered Events

Two, both set by `ensure_jira_webhook`:

| Event | Jira `webhookEvent` value | What `core` does with it |
|-------|--------------------------|---------------------|
| Story created | `jira:issue_created` | Interprets the payload and records the canonical work item |
| Any issue update — status change, Blocked field cleared, Release transition | `jira:issue_updated` | Interprets the change and applies it to the canonical work item |

From V5.1 `core` applies a Jira webhook only to a project in Jira mode, and
never applies a Jira change to a work item of a local-mode project (V5.1
REQ-10). With every project local, as V5.1 ships, each delivered event is
recorded and nothing is applied.

### Event Filtering

`core` records an event it has no handler for and applies nothing — no error
response. The webhook's JQL filter can be left empty; filtering happens in
`core`.

---

## Agent Roster

The Agent field is a human-visible projection of the canonical agent catalog
(`services/scrummaster/config/agents.json`) — not an independent source of truth. Its
options are generated and reconciled from that catalog by
`scripts/create-jira-fields.sh` (initial provisioning) and
`scripts/reconcile-agent-field.sh` (ongoing sync as the catalog changes).
Nothing audits the Jira options for drift against the catalog — running the
reconcile script after a catalog change is what keeps them in step. Jira is never
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
