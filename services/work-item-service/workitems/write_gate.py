"""
Write-gating in Jira mode. While a
project is in Jira mode, the internal work-item API rejects a direct
write (status change, assignment, dependency edit) that does not originate
from a validated Jira event. This applies uniformly regardless of caller
— including ScrumMaster's own automation.

Direct port of the Node service's src/writeGate.js. This is the single
enforcement point store.py calls before any of the three gated mutations
(status transition, assignment, link create/edit). Comments and
artifact attachment are NOT gated — only status
change, assignment, and dependency edit are.
"""


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
    # session-authenticated, actor resolved from request.user. Permitted
    # only for local-mode projects when it targets a gated field; the
    # Jira-mode restriction (added 2026-09-07) otherwise applies.
    ADMIN_UI = 'admin-ui'
    # The "other" non-agent-facing interface: workitems/views.py's raw
    # HTTP handlers. Distinct from ADMIN_UI because these endpoints carry no
    # caller-identity check of their own — actor is whatever a
    # caller's X-Actor header claims, unlike ADMIN_UI's authenticated
    # request.user. Gated identically to ADMIN_UI for now (no
    # behavioral distinction between the two), kept separate so this origin
    # is never mistaken in code or logs for an authenticated admin action.
    EXTERNAL_API = 'external-api'


class WriteGateRejectedError(Exception):
    code = 'WRITE_GATE_REJECTED'


def assert_gated_write_allowed(mode: str, origin: str) -> None:
    """`mode`: 'local' | 'jira' (ProjectConfig.mode). `origin`: one of
    Origins. Raises WriteGateRejectedError if the write must be rejected."""
    if mode != 'jira':
        return  # local mode: internal API accepts writes directly.
    if origin in (Origins.JIRA_WEBHOOK, Origins.ROLLUP):
        return
    raise WriteGateRejectedError(
        f'project is in Jira mode — a direct write to status, assignment, or a dependency link is rejected '
        f'(origin "{origin}"); the only path is a validated Jira-originated event'
    )
