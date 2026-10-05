import os
import sys

from django.apps import AppConfig
from django.conf import settings


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        if not getattr(settings, "EMAIL_INTAKE_AUTOSTART", False):
            return
        blocked = {"makemigrations", "migrate", "collectstatic", "shell", "test", "createsuperuser"}
        if any(command in sys.argv for command in blocked):
            return
        if "runserver" in sys.argv and os.environ.get("RUN_MAIN") != "true":
            return
        from .email_worker import start_email_worker
        start_email_worker()
