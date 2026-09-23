from django.apps import AppConfig


class ArtifactsConfig(AppConfig):
    """The artifact-ingress application (`strategy/v4.0/features/artifact-ingress.md`).

    A separate Django app rather than more of `workitems`: the Django
    instance is no longer only about work items, and an artifact is not a
    work-item feature. Its tables, its Streams namespace and its admin
    section are its own.
    """

    default_auto_field = 'django.db.models.BigAutoField'
    name = 'artifacts'
    verbose_name = 'Artifacts'
