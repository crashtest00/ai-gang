"""
URL configuration for workitemservice.

- /django-admin/ — django.contrib.admin, REQ-08's human-facing admin UI
  (the "capability that comes with the framework" §4's Implementation
  intent refers to).
- everything else, INCLUDING /admin/work-items... — workitems.urls, the
  REQ-04/REQ-08 direct query/admin-write HTTP interface, preserving the
  Node reference implementation's src/httpApi.js paths byte-for-byte
  (`/admin/work-items`, `/admin/work-items/:id/transition`,
  `/admin/work-items/:id/comments`).

Deliberate routing choice, called out in the final report: the Node
service's own REQ-08 write API already used the path prefix `/admin/...`
before Django's own admin site (which conventionally also wants `/admin/`)
entered the picture. Rather than change the REQ-08 HTTP contract's paths
(which services/work-item-service/test/httpApi.test.js and this port's own
tests/test_views_http_api.py assert against verbatim), django.contrib.admin
is mounted at /django-admin/ instead — the one URL-space decision this
port made differently from where django-admin's tutorials conventionally
put it, and the reason is preserving an existing contract, not avoiding
django.contrib.admin. services/scrummaster/src/canonicalWorkItems.js itself never
calls anything under /admin/ (only /projects/:project/mode and
/work-items/:id), so this has no effect on scrummaster's own contract
either way.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('django-admin/', admin.site.urls),
    path('', include('workitems.urls')),
]
