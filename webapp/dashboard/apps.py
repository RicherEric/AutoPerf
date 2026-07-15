from django.apps import AppConfig
from django.conf import settings


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "dashboard"

    def ready(self) -> None:
        from autoperf.storage import Storage

        Storage(settings.AUTOPERF_DB_PATH).initialize()
