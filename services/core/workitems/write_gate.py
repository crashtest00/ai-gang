"""
The mode router. From v5.2 this module does not merely *refuse* a write in
Jira mode — it decides, for every write the platform makes on a work item's
behalf, which of three things happens to it
(canonical-delivery-state.md REQ-09, "The routing layer";
TechnicalSpecification.md Key Principle 6, "one handler per kind of write"):

  record  the canonical store applies it, as it always has in local mode;
  push    the outbound writer (jira_writer.py) makes the equivalent Jira
          write and nothing is recorded — the change reaches the canonical
          store only through Jira's own webhook;
  refuse  the write raises WriteGateRejectedError and nothing happens.

`route(project, origin)` is the one function a handler calls, and
`mode_of(project)` is the one outbound reader of a project's mode: the five
routing handlers (store.py's status, assignment, link and comment functions,
and materialize.py's decomposition), the derived writes (the parent rollup
and the dependent unblock) and the admin's other write and delete paths all
act on `route`'s answer and read no mode themselves. That is the property
the specification states as the `REQ-09/mode-readers` search.

`assert_gated_write_allowed(mode, origin)`, which every caller used up to
v5.1, is gone: it took the mode as an argument, which forced every caller to
read the mode for itself, and it had only two answers.
"""

from __future__ import annotations

from . import project_config


class Origins:
    # A Streams command from an agent, ScrumMaster automation, or any other
    # internal-API caller that is not itself a validated Jira-originated event.
    DIRECT = 'direct'
    # A Jira webhook event, already evaluated against internal rules
    # by webhook_consumer.py before store.py is ever called.
    JIRA_WEBHOOK = 'jira-webhook'
    # A rollup
    # recomputation triggered synchronously by an already-accepted child
    # transition. Not a second independent write path.
    ROLLUP = 'rollup'
    # A write made through workitems/admin.py — django.contrib.admin
    # session-authenticated, actor resolved from request.user. The admin is
    # a person's edit interface, and in a Jira-mode project a person edits
    # work items in Jira, so this is the one origin `route` refuses there
    # (REQ-09, "The admin is a person's edit interface, so in Jira mode it
    # changes nothing").
    ADMIN_UI = 'admin-ui'
    # The "other" non-agent-facing interface: workitems/views.py's raw
    # HTTP handlers. Distinct from ADMIN_UI because these endpoints carry no
    # caller-identity check of their own — actor is whatever a
    # caller's X-Actor header claims, unlike ADMIN_UI's authenticated
    # request.user. A machine caller (at the pin, Jenkins' four posts to
    # core's HTTP API), so it is routed like any other machine origin, not
    # refused like the admin.
    EXTERNAL_API = 'external-api'


# `route`'s three answers.
RECORD = 'record'
PUSH = 'push'
REFUSE = 'refuse'


class WriteGateRejectedError(Exception):
    code = 'WRITE_GATE_REJECTED'


def mode_of(project: str) -> str:
    """The one outbound reader of a project's mode: `route` below and
    jira_writer.py both call this, and nothing else in `services/core`
    outside the mode layer reads the mode at all (REQ-09, "Where the mode
    is read is a property, stated as a search")."""
    return project_config.get_mode(project)['mode']


def route(project: str, origin: str) -> str:
    """`project`: the work item's own project. `origin`: one of Origins.
    Returns RECORD, PUSH or REFUSE. Reads the mode itself, so no caller
    holds a `get_mode` call of its own, and receives no payload — it
    decides from the project and the origin alone.

    A write with no Jira equivalent (a create, and the admin's other
    writes) is refused on PUSH too; that is the caller's rule, not this
    function's, because PUSH is the correct answer for every write that
    does have one."""
    if mode_of(project) != project_config.JIRA:
        return RECORD  # local mode: the canonical store is the write authority.
    if origin == Origins.JIRA_WEBHOOK:
        return RECORD  # the one write path into `core` in Jira mode.
    if origin == Origins.ADMIN_UI:
        return REFUSE
    return PUSH  # every machine origin, ROLLUP included.


def refuse(origin: str, what: str = 'a write to status, assignment, a dependency link or a comment') -> None:
    """Raise the refusal `route` asked for, with the wording every refused
    path shares so an operator sees one message whichever admin or caller
    produced it."""
    raise WriteGateRejectedError(
        f'project is in Jira mode — {what} from origin "{origin}" is refused; '
        'a person edits a Jira-mode work item in Jira, and every machine write is '
        'made through Jira and reaches this service by its webhook'
    )
