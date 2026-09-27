"""
URL routing for artifact ingress.

  GET /artifacts/<uuid>            — the artifact's bytes (REQ-02, REQ-03)
  GET /artifacts/<uuid>?record=true — the artifact's record (REQ-03)

Retrieval only. Upload is the Django Admin Panel
(/django-admin/artifacts/artifact/add/), not an endpoint here.
"""

from django.urls import path

from . import views

urlpatterns = [
    path('artifacts/<uuid:artifact_id>', views.get_artifact),
]
