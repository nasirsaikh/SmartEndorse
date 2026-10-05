import os
import sys
from pathlib import Path

from django.apps import AppConfig
from django.conf import settings


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        if not getattr(settings, "EMAIL_INTAKE_AUTOSTART", False):
            return
        if "runserver" in sys.argv:
            if "--noreload" not in sys.argv and os.environ.get("RUN_MAIN") != "true":
                return
        elif Path(sys.argv[0]).name.lower() in {"manage.py", "django-admin", "django-admin.py"}:
            # Management commands must not start a second polling worker.
            return
        from .email_worker import start_email_worker
        start_email_worker()
