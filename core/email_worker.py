"""APScheduler entry points for automatic email intake and correction replies."""
import atexit
import logging
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone
from filelock import FileLock, Timeout

from .email_intake import EmailIntakeService
from .email_polling import poll_correction_mailboxes


logger = logging.getLogger(__name__)
EMAIL_JOB_ID = "smartendorse-email-poll"
_scheduler = None
_lock = threading.Lock()


def poll_emails(limit=None):
    """Run one bounded cycle; another process holding the same lock wins this tick."""
    result = {"skipped": False, "corrections": None, "intake": None}
    try:
        limit = max(int(limit if limit is not None else settings.EMAIL_INTAKE_BATCH_SIZE), 1)
        lock_path = Path(settings.EMAIL_INTAKE_LOCK_FILE or Path(settings.MEDIA_ROOT) / "email-intake-poll.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(lock_path), timeout=0):
            close_old_connections()
            try:
                try:
                    result["corrections"] = poll_correction_mailboxes(limit=limit)
                except Exception:
                    logger.exception("Correction mailbox polling failed; it will retry on the next scheduled run.")
                try:
                    result["intake"] = EmailIntakeService.poll_all(force=False, exclude_correction_mailboxes=True)
                except Exception:
                    logger.exception("Route-based email intake failed; it will retry on the next scheduled run.")
            finally:
                close_old_connections()
        if any(stats and stats["mailboxes"] for stats in (result["corrections"], result["intake"])):
            logger.info("Email polling completed: corrections=%s, intake=%s", result["corrections"], result["intake"])
    except Timeout:
        result["skipped"] = True
        logger.debug("Skipped email polling: another process holds the polling lock.")
    except Exception:
        logger.exception("Email polling cycle failed; it will retry on the next scheduled run.")
    return result


def create_email_scheduler(*, background=True, interval=None, limit=None):
    """Recreate the interval job on startup; mailbox cursors and replies live in Django."""
    interval = max(int(interval if interval is not None else settings.EMAIL_INTAKE_POLL_SECONDS), 5)
    limit = max(int(limit if limit is not None else settings.EMAIL_INTAKE_BATCH_SIZE), 1)
    scheduler_class = BackgroundScheduler if background else BlockingScheduler
    scheduler = scheduler_class(
        timezone=ZoneInfo(settings.TIME_ZONE),
        executors={"default": ThreadPoolExecutor(max_workers=1)},
    )
    scheduler.add_job(
        poll_emails,
        "interval",
        seconds=interval,
        kwargs={"limit": limit},
        id=EMAIL_JOB_ID,
        name="Pull and process endorsement emails and retry pending replies",
        next_run_time=timezone.now(),
        max_instances=1,
        coalesce=True,
        misfire_grace_time=None,
        replace_existing=True,
    )
    return scheduler


def start_email_worker():
    """Start one background scheduler in this serving process."""
    global _scheduler
    with _lock:
        if _scheduler is not None and _scheduler.running:
            return _scheduler
        scheduler = create_email_scheduler()
        scheduler.start()
        _scheduler = scheduler
        logger.info("APScheduler email worker started; polling every %s seconds.", scheduler.get_job(EMAIL_JOB_ID).trigger.interval.total_seconds())
        return scheduler


def stop_email_worker(wait=True):
    global _scheduler
    with _lock:
        scheduler, _scheduler = _scheduler, None
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=wait)


atexit.register(stop_email_worker)
