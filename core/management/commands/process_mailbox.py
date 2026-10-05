import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.email_intake import deliver_reply, process_email
from core.mailbox import GraphMailbox, IMAPMailbox
from core.models import EmailReply, InboundEmail, MailboxConfiguration


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
            for config in boxes:
                transport = None
                try:
                    transport = GraphMailbox(config) if config.transport == "GRAPH" else IMAPMailbox(config)
                    transport.poll(options["limit"])
                    config.last_sync_at = timezone.now()
                    config.last_error = ""
                except Exception as exc:
                    config.last_error = str(exc)
                    self.stderr.write(f"Mailbox {config.pk}: {exc}")
                finally:
                    if isinstance(transport, GraphMailbox):
                        transport.close()
                    config.save(update_fields=["last_sync_at", "last_error", "updated_at"])
                # Messages persist before extraction, so an OCR/server failure can be retried after a restart.
                for email_id in InboundEmail.objects.filter(mailbox=config, processing_state=InboundEmail.State.RECEIVED).order_by("pk").values_list("pk", flat=True)[:options["limit"]]:
                    try:
                        result = process_email(email_id)
                        self.stdout.write(f"{result.reference}: {result.processing_state}")
                    except Exception as exc:
                        self.stderr.write(f"Email {email_id}: {exc}")
                for reply_id in EmailReply.objects.filter(email__mailbox=config, sent_at=None).order_by("pk").values_list("pk", flat=True)[:options["limit"]]:
                    deliver_reply(reply_id)
            if not options["watch"]:
                break
            time.sleep(options["interval"])
