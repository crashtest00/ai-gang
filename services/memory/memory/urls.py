from django.urls import path

from archive import views

urlpatterns = [
    path('health', views.health),
    path('sessions', views.session_changes),
    path('sessions/<str:session_id>', views.session_manifest),
    path('sessions/<str:session_id>/file', views.session_file),
    path('notes', views.notes_list),
    path('notes/file', views.note_file),
    path('notes/changes', views.note_changes),
    path('counts', views.counts),
]
