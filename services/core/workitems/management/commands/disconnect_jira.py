"""
`disconnect_jira <project>` — canonical-delivery-state.md REQ-10.

**The only path back to local mode**, now that `ProjectConfigAdmin` shows a
project's mode read-only and deletes no row (REQ-10; Shovel Ready Pass 5,
decision 6.2). It calls `project_config.revert_to_local` and **makes no
Jira write at all**: a project's Jira issues are left exactly as they are,
and the project's recorded `jira_project_key` is kept, which is what lets
`connect_jira` re-sync the same Jira project afterwards.

Run the same way as `connect_jira`, with the project quiesced.

**It refuses, changing nothing, while any `release` work item of the project
is open** (Pass 6, decision 4.1), for the same reason `connect_jira` does:
while the project is local `core` ignores the Release's Jira webhooks, so a
Release left open across the disconnect could only be closed by a person in
the admin, which publishes a release event (REQ-08). §4 records the cost —
a Jira-mode Release stops being open by reaching Done, which promotes it,
or by being abandoned (release-mode-parity.md REQ-14: the person moves the
ticket to Abandoned, which `core` maps to `cancelled`), so while one is open
no work item of its Target Project can be repaired through the
disconnect/edit/connect path.

**This is also the repair path.** The Django admin refuses every edit to a
Jira-mode work item, so an operator who has to fix an `external_key` or
delete a stray item runs this command, edits in the admin, and runs
`connect_jira` again — whose re-sync sets each keyed item's mapped Jira
status and pushes no other field (§4).
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from workitems import project_config
from workitems.models import ProjectConfig

from .connect_jira import open_releases


class Command(BaseCommand):
    help = (
        "Return a project to local mode (canonical-delivery-state.md REQ-10). Makes no Jira write. "
        "Run with ScrumMaster stopped and the project otherwise left alone."
    )

    def add_arguments(self, parser):
        parser.add_argument('project', help='the AI Gang project name')

    def handle(self, *args, **options):
        project = options['project']

        open_release_items = open_releases(project)
        if open_release_items:
            raise CommandError(
                'refusing to disconnect: these release work items are still open — '
                + ', '.join(f'{item.display_name} ({item.id}, {item.status})'
                             for item in open_release_items)
                + '. A Release in flight depends on its project staying in the mode it was cut in; '
                'close it first.'
            )

        project_config.revert_to_local(project)

        # The row is read for its KEY, not its mode: `REQ-09/mode-readers`
        # makes "no `get_mode` call outside the mode layer" a property of
        # the built Source, and a success message is no reason to add one —
        # `revert_to_local` is what the mode now is.
        row = ProjectConfig.objects.filter(project=project).first()
        recorded_key = (row.jira_project_key if row is not None else None) or 'none'
        self.stdout.write(self.style.SUCCESS(
            f'{project} is now in local mode. Its recorded Jira key ({recorded_key}) is kept, '
            'and no Jira issue was touched. Start ScrumMaster again.'
        ))
