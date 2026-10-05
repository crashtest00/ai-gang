"""
What remains of connecting a project to Jira: recording an external key
against a work item.

Django/`core` holds the running platform's only Jira client
(`workitems/jira_client.py`, V5.1 REQ-01); nothing calls it until v5.2's
outbound writer, besides `ensure_jira_webhook.py`'s webhook registration
(BF-02).

**Corrected 2026-10-04 (v5.1 BUGFIXES.md BF-06; v5.2 Canonical Delivery
State REQ-09).** v5.2's writer (`workitems/jira_writer.py`) is now a
running caller of the client, not merely a planned one, and `views.py`
and `webhook_consumer.py` call it directly too (`views.py`'s
`jira_client.get_issue`; `webhook_consumer.py`'s
`get_blocks_link_type_id` and `get_issue`, for Blocks link-type and
issue-key lookups). The Blocked-flag write goes through `jira_writer`
(`set_blocked_field`, called from `jira_writer.py` and `connect_jira.py`
only). The client no longer has zero production callers. The webhook
registration (`ensure_jira_webhook.py`) still has its own HTTP helper,
`_jira_request`, and does not use the client.

The catch-up push that used to live here is gone (v5.1): its Jira-facing half
was ScrumMaster's jiraCatchupConsumer.js, which no longer exists.

**From v5.2 the mode flip is gone from this module too**
(canonical-delivery-state.md REQ-10). A project's mode changes through
exactly two paths, both management commands run with the project quiesced:
`connect_jira <project> <jira-key>`, which pushes the project's keyless work
items into Jira, re-syncs each keyed item's status and links, and only then
switches the mode; and `disconnect_jira <project>`, which calls
`project_config.revert_to_local` and makes no Jira write. `catchup.connect_jira`
switched with no push at all, which is the thing REQ-10 exists to stop, and
`catchup.disconnect_jira` had no caller once the command existed.

What stays here is `record_external_key`, the idempotent recorder
`connect_jira`'s push reports each created issue's key back through.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from .models import WorkItem, WorkItemHistory
from .store import write_outbox_event as _write_outbox_event


def record_external_key(work_item_id, external_key: str, *, actor: str = 'jira-catchup') -> dict:
    """Records a work item's key in an external tracker. Idempotent: a no-op if
    external_key is already set — "skipped rather than re-created"."""
    with transaction.atomic():
        item = WorkItem.objects.select_for_update().filter(id=work_item_id).first()
        if not item:
            raise ValueError(f'record_external_key: no work item {work_item_id}')
        if item.external_key:
            return {'alreadyRecorded': True, 'externalKey': item.external_key}

        item.external_key = external_key
        item.updated_at = timezone.now()
        item.save(update_fields=['external_key', 'updated_at'])
        WorkItemHistory.objects.create(
            work_item_id=work_item_id, field='external_key', old_value=None, new_value=external_key,
            actor=actor, occurred_at=timezone.now(),
        )
        _write_outbox_event(
            project=item.project, event_type='work_item.external_key_recorded', work_item_id=work_item_id,
            payload={'workItemId': str(work_item_id), 'externalKey': external_key},
        )
        return {'alreadyRecorded': False, 'externalKey': external_key}
