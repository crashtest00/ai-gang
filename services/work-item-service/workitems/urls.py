"""
URL routing for the HTTP interface. Paths and methods match
the Node service's src/httpApi.js exactly — see that mapping preserved
here so services/scrummaster/src/canonicalWorkItems.js's requests resolve
unchanged:

  GET  /health
  GET  /projects/<project>/mode
  GET  /work-items/<uuid>[?full=true]  — work-items.md REQ-01/REQ-02/REQ-05:
    both the specification link and the ordered artifact links appear on
    this read, bare or full.
  GET  /work-items?project=&status=&assigneeAgentId=&parentId=
       [&specArtifactId=&requirementId=]  — the trailing pair is
    work-items.md REQ-06's forward query: every work item recording that
    (artifact id, requirement id) specification link, each with its own
    REQ-06 delivery associations included. A direct read (REQ-04), not a
    Streams command — same access-logging as every other read here.
  GET  /work-item-artifacts/<uuid>/specification-link  — work-items.md
    REQ-06's backward query (AC-04): resolve the specification link of the
    work item a `work_item_artifact` delivery association belongs to, in
    one lookup. Addressed by that association's OWN id, not by a work
    item's id.
  POST /admin/work-items
  POST /admin/work-items/<uuid>/transition
  POST /admin/work-items/<uuid>/comments
  POST /admin/work-items/<uuid>/release-candidate  — the release-candidate
    writeback target, new in V2.1

  POST /webhooks/jira  — amended
    2026-09-09: the Jira webhook endpoint, moved here from
    services/scrummaster/src/server.js's deleted `/webhook/jira` route now that
    this service is AI Gang's sole external-facing surface.

There is deliberately NO write route here for the specification link or the
artifact links (work-items.md REQ-03) — recording either travels only as a
Streams command (workitems/command_consumer.py) or the Django admin
(workitems/admin.py); this module exposes reads only for both.
"""

from django.urls import path

from . import views

urlpatterns = [
    path('health', views.health),
    path('projects/<str:project>/mode', views.project_mode),
    path('work-items', views.list_work_items),
    path('work-items/<uuid:work_item_id>', views.get_work_item),
    path('work-item-artifacts/<uuid:work_item_artifact_id>/specification-link',
         views.get_specification_link_for_delivery_artifact),
    path('admin/work-items', views.admin_create_work_item),
    path('admin/work-items/<uuid:work_item_id>/transition', views.admin_transition_work_item),
    path('admin/work-items/<uuid:work_item_id>/comments', views.admin_add_comment),
    path('admin/work-items/<uuid:work_item_id>/release-candidate', views.admin_record_release_candidate),
    path('webhooks/jira', views.jira_webhook),
]
