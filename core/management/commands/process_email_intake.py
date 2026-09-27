import time

from django.core.management.base import BaseCommand

from core.email_intake import EmailIntakeService
from core.models import EmailIntakeMailbox


class Command(BaseCommand):
    help = "Poll configured inbound endorsement mailboxes and convert emails into SmartEndorse requests."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep polling continuously.")
        parser.add_argument("--interval", type=int, default=60, help="Loop sleep interval in seconds.")
        parser.add_argument("--mailbox", type=int, help="Poll only this EmailIntakeMailbox primary key.")
        parser.add_argument("--force", action="store_true", help="Ignore mailbox poll interval / last-polled time.")

    def handle(self, *args, **options):
        while True:
            if options["mailbox"]:
                mailbox = EmailIntakeMailbox.objects.get(pk=options["mailbox"], is_active=True)
                stats = EmailIntakeService.poll_mailbox(mailbox)
                result = {"mailboxes": 1, **stats}
            else:
                result = EmailIntakeService.poll_all(force=options["force"])

            self.stdout.write(
                self.style.SUCCESS(
                    "Email intake: "
                    f"{result['mailboxes']} mailbox(es), "
                    f"{result['messages']} message(s), "
                    f"{result['processed']} processed, "
                    f"{result['failed']} failed/review."
                )
            )
            if not options["loop"]:
                break
            time.sleep(max(options["interval"], 5))
