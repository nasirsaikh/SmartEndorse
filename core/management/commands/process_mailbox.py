import time

from django.core.management.base import BaseCommand, CommandError
from core.email_polling import poll_correction_mailboxes
from core.models import MailboxConfiguration


class Command(BaseCommand):
    help = "Poll active IMAP/Microsoft 365 mailboxes, process authorized corrections and retry pending replies."

    def add_arguments(self, parser):
        parser.add_argument("--mailbox", type=int, help="Process one active mailbox ID.")
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--watch", action="store_true", help="Keep polling; run this process as a worker/service.")
        parser.add_argument("--interval", type=int, default=60)

    def handle(self, *args, **options):
        if options["limit"] < 1 or options["interval"] < 10:
            raise CommandError("Use a positive limit and an interval of at least 10 seconds.")
        while True:
            boxes = MailboxConfiguration.objects.filter(is_active=True)
            if options["mailbox"]:
                boxes = boxes.filter(pk=options["mailbox"])
            if not boxes.exists():
                raise CommandError("No active mailbox matched. Configure and activate a mailbox in Django Admin.")
            result = poll_correction_mailboxes(limit=options["limit"], mailbox_id=options["mailbox"])
            self.stdout.write(
                f"Correction intake: {result['mailboxes']} mailbox(es), {result['messages']} fetched, "
                f"{result['processed']} processed, {result['replies_sent']} replies sent, {result['failed']} failed/pending."
            )
            if not options["watch"]:
                break
            time.sleep(options["interval"])
