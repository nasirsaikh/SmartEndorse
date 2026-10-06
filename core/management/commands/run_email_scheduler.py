import signal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.email_worker import create_email_scheduler


class Command(BaseCommand):
    help = "Run APScheduler to pull and process active email mailboxes and retry pending correction replies."

    def add_arguments(self, parser):
        parser.add_argument("--interval", type=int, default=settings.EMAIL_INTAKE_POLL_SECONDS, help="Poll interval in seconds (at least 5).")
        parser.add_argument("--limit", type=int, default=settings.EMAIL_INTAKE_BATCH_SIZE, help="Maximum messages, pending corrections and replies per correction mailbox per cycle.")

    def handle(self, *args, **options):
        if options["interval"] < 5 or options["limit"] < 1:
            raise CommandError("Use an interval of at least 5 seconds and a positive limit.")
        scheduler = create_email_scheduler(background=False, interval=options["interval"], limit=options["limit"])
        self.stdout.write(self.style.SUCCESS(
            f"APScheduler email worker started: every {options['interval']} seconds; correction batch limit {options['limit']}."
        ))

        def terminate(signum, frame):
            raise KeyboardInterrupt

        previous_handler = signal.signal(signal.SIGTERM, terminate)
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=True)
            signal.signal(signal.SIGTERM, previous_handler)
        self.stdout.write("Email scheduler stopped.")
