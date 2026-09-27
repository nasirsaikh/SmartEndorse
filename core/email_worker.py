import logging
import threading
import time

from django.conf import settings
from django.db import close_old_connections

from .email_intake import EmailIntakeService


logger = logging.getLogger(__name__)
_started = False
_lock = threading.Lock()


def _worker():
    interval = max(int(getattr(settings, "EMAIL_INTAKE_POLL_SECONDS", 30)), 5)
    while True:
        try:
            close_old_connections()
            EmailIntakeService.poll_all(force=False)
        except Exception:
            logger.exception("SmartEndorse email intake polling failed.")
        finally:
            close_old_connections()
        time.sleep(interval)


def start_email_worker():
    global _started
    with _lock:
        if _started:
            return
        _started = True
        thread = threading.Thread(target=_worker, name="smartendorse-email-intake", daemon=True)
        thread.start()
