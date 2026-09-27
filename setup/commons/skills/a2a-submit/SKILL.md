---
name: a2a-submit
description: Report to ScrumMaster over the AI Gang gateway — post a comment or a progress note, ask a blocking question, hand the work item to another agent, request a subtask, or finish the task with or without a pull request. Use whenever a task calls for submitting, reporting, notifying, handing off, blocking on a question, or completing.
---

# Submitting to the gateway

Everything you report on the work item you were dispatched for — a comment, a
progress note, a blocking question, a reassignment, a subtask request, a
completion — is one **submission**, and every submission goes through the
constructor:

```bash
a2a-submit.js <operation> [arguments]
```

It is first on your `PATH`, so invoke it by that bare name: no `node`, no
directory, no path.

**You never author a submission.** There is no shape to fill in, no message to
write out, no JSON to save and hand to a publisher. You name an operation and
give it its fields; the tool builds the message, validates it, and publishes
it. Constructing a submission by hand and publishing it yourself is not a
fallback and not a shortcut — the tool is the only way to submit.

**Nothing you pass is an identifier.** The work item, the project, and which
earlier submission this one continues all come from the dispatch that started
this session; the tool reads them itself and keeps track of the thread on its
own. You will never be asked for one, and there is nothing for you to record,
remember or copy between submissions.

## The operations

| Operation | Use it to | Required | Optional |
| --- | --- | --- | --- |
| `comment` | post a comment on the work item and keep working | `--text` | `--reference-file`, `--reference-function` |
| `progress` | post a plain progress note, with no operation attached | `--text` | `--reference-file`, `--reference-function` |
| `input-required` | stop and ask a human for a clarification only a person can give | `--text` | `--reference-file`, `--reference-function` |
| `auth-required` | stop and ask a human for a credential or an authorization you lack | `--text` | `--reference-file`, `--reference-function` |
| `reassign` | hand the work item's recorded owner to another agent this project permits | `--agent` | `--text` |
| `create-subtask` | request a new subtask under the work item you are working on | `--summary`, `--description`, `--agent` | `--text`, `--specification-artifact` with `--specification-requirement`, `--artifact-link` |
| `completed` | say your work on this task is done | `--text` | `--pull-request`, `--pull-request-summary` |
| `failed` | say something broke that you cannot get past | `--text` | — |
| `canceled` | say this task should not be carried out after all | `--text` | — |
| `rejected` | refuse the task: it is not work this role should do | `--text` | — |

`completed`, `failed`, `canceled` and `rejected` end the task. The rest leave
it open; `input-required` and `auth-required` leave it waiting on a human.

On `reassign` and `create-subtask`, `--text` is recorded on the task rather than
posted as a comment on the work item — so say anything the work item's readers
need in a separate `comment`.

## Pointing at a file

Four operations take a file reference: `comment`, `progress` (a plain progress
note), `input-required` and `auth-required`. They are the gateway's three
reference-consuming paths — the comment path, the plain-progress path, and the
one path the two waiting-on-a-human states share.

- `--reference-file <path>` — the file the submission is about.
- `--reference-function <name>` — optional, and only with `--reference-file`:
  the function, element or line context inside that file.

No other operation takes a file reference; a reference given to one of them
would reach nothing.

## create-subtask

Three fields are required, and two optional ones carry the new subtask's
references:

- `--summary` (required) — the subtask's one-line summary, starting with the
  implementing agent's role and a colon, as in `Backend: add the /health endpoint`.
- `--description` (required) — what that agent has to do, self-contained: the
  agent receives the subtask and not the parent.
- `--agent` (required) — the id of the agent that will implement it, copied
  exactly from the ids your dispatch prompt lists under `## ALLOWED AGENTS`.
  It becomes the subtask's recorded owner.
- `--specification-artifact` (optional) — the artifact id of the subtask's
  `specificationLink`. Give it together with `--specification-requirement`, or
  give neither.
- `--specification-requirement` (optional) — the requirement id of that same
  `specificationLink`.
- `--artifact-link` (optional, repeatable) — one artifact id this subtask needs,
  repeated once per id; they become its `artifactLinks`.

A subtask created without either of the two optional references is created
normally; a reference whose value is malformed is refused before anything is
created, and nothing is published.

## Opening a pull request

Report a pull request on the `completed` submission that finishes the task:
`--pull-request <url>`, and `--pull-request-summary <text>` when the summary of
the pull request is not the summary of your completion. Opening a pull request
neither transitions the work item nor reassigns it — the pipeline does that
once it passes.

With `--pull-request`, the comment posted on the work item is the pull request's
summary — `--pull-request-summary` when you give one, and `--text` when you do
not. `--text` is recorded on the task either way, so when you give both, put
what a reviewer needs in `--pull-request-summary`: nothing else of `--text`
reaches the work item.

## The arguments themselves

The tool documents its own arguments; read them there rather than guessing:

```bash
a2a-submit.js help                 # every operation and its arguments
a2a-submit.js help create-subtask  # one operation's arguments
```

Its own summary of what it does and what an argument is for:

> Submits one A2A message to the ScrumMaster gateway for the task this
> session was dispatched for. The task, the project, the ids and the chain
> are not arguments: this tool reads them from the dispatch.

Both `--flag value` and `--flag=value` work. An unrecognised flag and a value
with no flag are both errors.

## Success and failure

On success the tool prints one line beginning `Accepted:`.

On failure it exits non-zero, having published nothing, and says what was
wrong — the argument or the field, by name. In its own words:

> A non-zero exit means nothing was published. The reason is on stderr.

Read that message and fix the call: a non-zero exit means the work was never
reported, so nothing downstream has seen it.
