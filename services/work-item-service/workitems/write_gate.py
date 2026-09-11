"""
canonical-work-model.md REQ-10 — write-gating in Jira mode. "While a
project is in Jira mode, the internal work-item API MUST reject a direct
write (status change, assignment, dependency edit) that does not originate
from a validated Jira event... This applies uniformly regardless of caller
— including ScrumMaster's own automation."

Direct port of the Node service's src/writeGate.js. This is the single
enforcement point store.py calls before any of the three gated mutations
(status transition, assignment, link create/edit). Comments (REQ-18) and
artifact attachment (REQ-06) are NOT gated — REQ-10 names only "status
change, assignment, dependency edit."
"""


class Origins:
    # A Streams command from an agent, ScrumMaster automation, or any other
    # internal-API caller that is not itself a validated Jira-originated event.
    DIRECT = 'direct'
    # REQ-11: a Jira webhook event, already evaluated against internal rules
    # by webhook_consumer.py before store.py is ever called.
    JIRA_WEBHOOK = 'jira-webhook'
    # REQ-09 (internal-work-item-service.md) / REQ-20: a rollup
    # recomputation triggered synchronously by an already-accepted child
    # transition. Not a second independent write path.
    ROLLUP = 'rollup'
    # REQ-08: a write made through workitems/admin.py — django.contrib.admin
    # session-authenticated, actor resolved from request.user. Permitted
    # only for local-mode projects when it targets a gated field; REQ-08's
    # Jira-mode restriction (added 2026-09-07) otherwise applies.
    ADMIN_UI = 'admin-ui'
    # REQ-08's "other" non-agent-facing interface: workitems/views.py's raw
    # HTTP handlers. Distinct from ADMIN_UI because these endpoints carry no
    # caller-identity check of their own (see
    # the hardening-sandbox-gaps design) — actor is whatever a
    # caller's X-Actor header claims, unlike ADMIN_UI's authenticated
    # request.user. Gated identically to ADMIN_UI for now (REQ-08 draws no
    # behavioral distinction between the two), kept separate so this origin
    # is never mistaken in code or logs for an authenticated admin action.
    EXTERNAL_API = 'external-api'


class WriteGateRejectedError(Exception):
    code = 'WRITE_GATE_REJECTED'


def assert_gated_write_allowed(mode: str, origin: str) -> None:
    """`mode`: 'local' | 'jira' (ProjectConfig.mode). `origin`: one of
    Origins. Raises WriteGateRejectedError if the write must be rejected."""
    if mode != 'jira':
        return  # local mode: internal API accepts writes directly (REQ-07).
    if origin in (Origins.JIRA_WEBHOOK, Origins.ROLLUP):
        return
    raise WriteGateRejectedError(
        f'project is in Jira mode — a direct write to status, assignment, or a dependency link is rejected '
        f'(origin "{origin}"); the only path is a validated Jira-originated event (REQ-10, REQ-11)'
    )
