"""
Explicit, reversible, per-project mode
selection. Direct port of the Node service's src/projectConfig.js. Mode is
validated project configuration, never an implicit consequence of Jira
credentials being present. A project with no row in ProjectConfig is local
mode by definition — callers never need to pre-create a row before a
project's first write.
"""

from __future__ import annotations

from typing import Optional

from django.db import transaction
from django.utils import timezone

from .models import ProjectConfig, ProjectStatusConfig

LOCAL = ProjectConfig.LOCAL
JIRA = ProjectConfig.JIRA


def get_mode(project: str) -> dict:
    row = ProjectConfig.objects.filter(project=project).first()
    if row is None:
        return {'project': project, 'mode': LOCAL, 'jiraProjectKey': None}
    return {'project': project, 'mode': row.mode, 'jiraProjectKey': row.jira_project_key}


def set_mode(project: str, mode: str, *, jira_project_key: Optional[str] = None) -> None:
    """Jira mode must not be selectable during project
    initialization; a project must complete initialization in local mode
    and connect Jira, if at all, as a separate, later operation. This
    function does not enforce that ordering itself — the caller (the
    connect-Jira operation, catchup.py) is the only code path that ever
    sets mode='jira', and it is never invoked as part of project
    creation."""
    if mode not in (LOCAL, JIRA):
        raise ValueError(f'invalid mode "{mode}" — must be "{LOCAL}" or "{JIRA}"')

    with transaction.atomic():
        row, created = ProjectConfig.objects.select_for_update().get_or_create(
            project=project,
            defaults={'mode': mode, 'jira_project_key': jira_project_key, 'updated_at': timezone.now()},
        )
        if not created:
            row.mode = mode
            if jira_project_key is not None:
                row.jira_project_key = jira_project_key
            row.updated_at = timezone.now()
            row.save(update_fields=['mode', 'jira_project_key', 'updated_at'])


def revert_to_local(project: str) -> None:
    """Switching from Jira mode back to local mode must be
    supported and must require no data reconciliation beyond re-enabling
    direct writes."""
    set_mode(project, LOCAL)


def get_custom_statuses(project: str) -> list[dict]:
    return list(ProjectStatusConfig.objects.filter(project=project).values('status', 'baseline_status', 'jira_status_name'))


def declare_custom_status(project: str, status: str, baseline_status: str, jira_status_name: Optional[str] = None) -> None:
    ProjectStatusConfig.objects.update_or_create(
        project=project, status=status,
        defaults={'baseline_status': baseline_status, 'jira_status_name': jira_status_name},
    )
