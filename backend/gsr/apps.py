from django.apps import AppConfig


class GsrConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "gsr"

    def ready(self) -> None:
        from . import checks  # noqa: F401
