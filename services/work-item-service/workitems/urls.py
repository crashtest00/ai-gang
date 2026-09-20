"""
URL routing for the HTTP interface. Paths and methods match
the Node service's src/httpApi.js exactly — see that mapping preserved
here so services/scrummaster/src/canonicalWorkItems.js's requests resolve
unchanged:

  GET  /health
  GET  /projects/<project>/mode
  GET  /work-items/<uuid>[?full=true]
  GET  /work-items?project=&status=&assigneeAgentId=&parentId=
  POST /admin/work-items
  POST /admin/work-items/<uuid>/transition
  POST /admin/work-items/<uuid>/comments
  POST /admin/work-items/<uuid>/release-candidate  — the release-candidate
    writeback target, new in V2.1

  POST /webhooks/jira  — amended
    2026-09-09: the Jira webhook endpoint, moved here from
    services/scrummaster/src/server.js's deleted `/webhook/jira` route now that
    this service is AI Gang's sole external-facing surface.
"""

from django.urls import path

from . import views

urlpatterns = [
    path('health', views.health),
    path('projects/<str:project>/mode', views.project_mode),
    path('work-items', views.list_work_items),
    path('work-items/<uuid:work_item_id>', views.get_work_item),
    path('admin/work-items', views.admin_create_work_item),
    path('admin/work-items/<uuid:work_item_id>/transition', views.admin_transition_work_item),
    path('admin/work-items/<uuid:work_item_id>/comments', views.admin_add_comment),
    path('admin/work-items/<uuid:work_item_id>/release-candidate', views.admin_record_release_candidate),
    path('webhooks/jira', views.jira_webhook),
]
