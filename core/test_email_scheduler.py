import io
import json
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from apscheduler.events import EVENT_JOB_MAX_INSTANCES
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from . import email_worker
from .email_intake import EmailIntakeService, ingest_message, process_email
from .email_polling import poll_correction_mailboxes
from .models import EmailAuthority, EmailIntakeMailbox, EmailReply, EndorsementItem, InboundEmail, MailboxConfiguration, PolicyAccess
from .tests import BaseInsuranceTest


class EmailSchedulerTests(SimpleTestCase):
    def setUp(self):
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media.name, EMAIL_INTAKE_LOCK_FILE="")
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(email_worker.stop_email_worker)

    def test_both_pipelines_run_and_failed_pipeline_retries_without_blocking_other(self):
        stats = {"mailboxes": 0}
        with patch.object(email_worker, "poll_correction_mailboxes", side_effect=[RuntimeError("offline"), stats]) as corrections, patch.object(EmailIntakeService, "poll_all", return_value=stats) as intake, patch.object(email_worker, "close_old_connections") as close:
            with self.assertLogs("core.email_worker", level="ERROR"):
                failed = email_worker.poll_emails(limit=7)
            recovered = email_worker.poll_emails(limit=7)
        self.assertIsNone(failed["corrections"])
        self.assertEqual(failed["intake"], stats)
        self.assertEqual(recovered["corrections"], stats)
        self.assertFalse(recovered["skipped"])
        self.assertEqual(corrections.call_count, 2)
        corrections.assert_called_with(limit=7)
        self.assertEqual(intake.call_count, 2)
        intake.assert_called_with(force=False, exclude_correction_mailboxes=True)
        self.assertEqual(close.call_count, 4)

    def test_a_different_process_holding_poll_lock_skips_tick_and_releases_on_exit(self):
        lock_path = Path(self.media.name) / "email-intake-poll.lock"
        marker = Path(self.media.name) / "acquired"
        script = (
            "import sys\nfrom pathlib import Path\nfrom filelock import FileLock\n"
            "with FileLock(sys.argv[1]):\n"
            "    Path(sys.argv[2]).write_text('ready')\n"
            "    sys.stdin.readline()\n"
        )
        child = subprocess.Popen([sys.executable, "-c", script, str(lock_path), str(marker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "Child process could not acquire the polling lock.")
            with patch.object(email_worker, "poll_correction_mailboxes", return_value={"mailboxes": 0}) as corrections, patch.object(EmailIntakeService, "poll_all", return_value={"mailboxes": 0}) as intake:
                self.assertTrue(email_worker.poll_emails()["skipped"])
                corrections.assert_not_called()
                intake.assert_not_called()
                child.communicate(input="release\n", timeout=5)
                self.assertEqual(child.returncode, 0)
                self.assertFalse(email_worker.poll_emails()["skipped"])
                corrections.assert_called_once()
                intake.assert_called_once()
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)

    def test_background_scheduler_starts_once_polls_immediately_repeats_and_stops(self):
        tick = threading.Event()
        with patch.object(email_worker, "poll_emails", side_effect=lambda **kwargs: tick.set()) as poll:
            scheduler = email_worker.start_email_worker()
            try:
                self.assertIs(email_worker.start_email_worker(), scheduler)
                self.assertTrue(tick.wait(3), "First polling job did not run immediately.")
                tick.clear()
                scheduler.reschedule_job(email_worker.EMAIL_JOB_ID, trigger="interval", seconds=0.05)
                self.assertTrue(tick.wait(3), "Interval polling job did not repeat.")
            finally:
                email_worker.stop_email_worker()
            self.assertFalse(scheduler.running)
            self.assertGreaterEqual(poll.call_count, 2)
            tick.clear()
            restarted = email_worker.start_email_worker()
            try:
                self.assertIsNot(restarted, scheduler)
                self.assertTrue(tick.wait(3), "Restarted scheduler did not poll.")
            finally:
                email_worker.stop_email_worker()

    def test_slow_poll_cannot_overlap_another_scheduled_instance(self):
        started, release, skipped = threading.Event(), threading.Event(), threading.Event()

        def slow_poll(**kwargs):
            started.set()
            release.wait(5)

        with patch.object(email_worker, "poll_emails", side_effect=slow_poll) as poll:
            scheduler = email_worker.create_email_scheduler()
            scheduler.add_listener(lambda event: skipped.set(), EVENT_JOB_MAX_INSTANCES)
            try:
                scheduler.start()
                self.assertTrue(started.wait(3))
                job = scheduler.get_job(email_worker.EMAIL_JOB_ID)
                self.assertTrue(job.coalesce)
                self.assertEqual(job.max_instances, 1)
                scheduler.reschedule_job(job.id, trigger="interval", seconds=0.05)
                self.assertTrue(skipped.wait(3), "A slow job did not prevent an overlapping scheduled run.")
                self.assertEqual(poll.call_count, 1)
            finally:
                release.set()
                scheduler.shutdown(wait=True)

    def test_foreground_command_shuts_down_after_interrupt_and_validates_options(self):
        output = io.StringIO()
        with patch("core.management.commands.run_email_scheduler.create_email_scheduler") as create:
            scheduler = create.return_value
            scheduler.start.side_effect = KeyboardInterrupt
            scheduler.running = True
            call_command("run_email_scheduler", interval=10, limit=7, stdout=output)
            create.assert_called_once_with(background=False, interval=10, limit=7)
            scheduler.shutdown.assert_called_once_with(wait=True)
            self.assertIn("Email scheduler stopped.", output.getvalue())
            for options in ({"interval": 1}, {"limit": 0}):
                with self.subTest(options=options), self.assertRaises(CommandError):
                    call_command("run_email_scheduler", stdout=output, **options)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ScheduledCorrectionTests(BaseInsuranceTest):
    def setUp(self):
        super().setUp()
        self.media = tempfile.TemporaryDirectory()
        self.addCleanup(self.media.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.media.name, EMAIL_INTAKE_LOCK_FILE="")
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        # TestCase holds an atomic transaction; a scheduler thread normally owns a separate connection.
        cleanup = patch.object(email_worker, "close_old_connections")
        cleanup.start()
        self.addCleanup(cleanup.stop)
        self.requester.email = "hr@client.example"
        self.requester.save()
        PolicyAccess.objects.create(policy=self.policy, organization=self.client, can_create=True)
        self.mailbox = MailboxConfiguration.objects.create(name="Corrections", email_address="intake@insurer.example", imap_host="imap.insurer.example", trusted_authserv_ids=["mx.insurer.example"], is_active=True)
        EmailAuthority.objects.create(name="Authorized HR", email_address=self.requester.email, organization=self.client, policy=self.policy, processing_user=self.requester)
        self.counter = 0

    def receive(self, rows, subject=None):
        self.counter += 1
        message = EmailMessage()
        message["From"] = self.requester.email
        message["To"] = self.mailbox.email_address
        message["Subject"] = subject or f"Addition policy {self.policy.policy_number}"
        message["Message-ID"] = f"<scheduler-{self.counter}@client.example>"
        message["Authentication-Results"] = "mx.insurer.example; dmarc=pass header.from=client.example"
        message.set_content(f"Effective date: {self.today.isoformat()}\nBEGIN MEMBERS\n{json.dumps(rows)}\nEND MEMBERS")
        return ingest_message(self.mailbox, message.as_bytes(), str(self.counter))

    def test_scheduled_cycle_processes_corrections_and_replies_even_when_fetch_is_offline(self):
        row = {"employee_no": "E1", "full_name": "First Member", "date_of_birth": "1990-01-01", "gender": "Male", "relationship": "Employee", "plan_code": self.plan.code}
        original = self.receive([row, {**row, "employee_no": "E2", "date_of_birth": ""}])
        with patch("core.email_polling.IMAPMailbox") as transport:
            transport.return_value.poll.side_effect = RuntimeError("IMAP offline")
            with self.assertLogs("core.email_polling", level="ERROR"):
                first = email_worker.poll_emails()
            original.refresh_from_db()
            self.assertEqual(original.processing_state, InboundEmail.State.NEEDS_INFO)
            self.assertEqual(first["corrections"]["processed"], 1)
            self.assertEqual(first["corrections"]["replies_sent"], 1)
            self.assertEqual(first["corrections"]["failed"], 1)
            self.mailbox.refresh_from_db()
            self.assertEqual(self.mailbox.last_error, "IMAP offline")
            self.assertIn(original.reference, mail.outbox[0].subject)
            self.assertIn("CORRECT MEMBERS (1)", mail.outbox[0].body)
            self.assertIn("ERROR MEMBERS / NEEDS CORRECTION (1)", mail.outbox[0].body)

            correct, error = list(original.endorsement.items.order_by("pk"))
            correction = self.receive([
                {"row_reference": f"ITEM-{error.pk}", "date_of_birth": "1992-02-03"},
                {"row_reference": f"ITEM-{correct.pk}", "full_name": "Updated Accepted Member"},
            ], subject=f"Re: {original.reference}")
            transport.return_value.poll.side_effect = None
            transport.return_value.poll.return_value = []
            second = email_worker.poll_emails()
            correction.refresh_from_db()
            self.assertEqual(correction.processing_state, InboundEmail.State.PROCESSED)
            self.assertEqual(second["corrections"]["replies_sent"], 1)
            correct.refresh_from_db()
            error.refresh_from_db()
            self.assertEqual(correct.full_name, "Updated Accepted Member")
            self.assertEqual(error.validation_status, EndorsementItem.ValidationStatus.VALID)
            self.assertEqual(original.endorsement.items.count(), 2)
            self.assertEqual(mail.outbox[1].extra_headers["X-SmartEndorse-Reference"], original.reference)
            self.assertEqual(email_worker.poll_emails()["corrections"]["replies_sent"], 0)
            self.assertEqual(len(mail.outbox), 2)

    def test_pending_reply_is_retried_on_the_next_poll_after_smtp_failure(self):
        original = self.receive([{"employee_no": "E1", "full_name": "Missing DOB"}])
        process_email(original.pk)
        reply = EmailReply.objects.get(email=original)
        with patch("core.email_polling.IMAPMailbox") as transport, patch("django.core.mail.EmailMultiAlternatives.send", side_effect=[RuntimeError("SMTP offline"), 1]):
            transport.return_value.poll.return_value = []
            self.assertEqual(poll_correction_mailboxes()["replies_sent"], 0)
            reply.refresh_from_db()
            self.assertIsNone(reply.sent_at)
            self.assertEqual(reply.last_error, "SMTP offline")
            self.assertEqual(reply.attempts, 1)
            self.assertEqual(poll_correction_mailboxes()["replies_sent"], 1)
        reply.refresh_from_db()
        self.assertIsNotNone(reply.sent_at)
        self.assertEqual(reply.last_error, "")
        self.assertEqual(reply.attempts, 2)

    def test_batch_limit_applies_to_pending_processing_and_delivery(self):
        for number in range(3):
            self.receive([{"employee_no": f"E{number}", "full_name": f"Member {number}"}])
        with patch("core.email_polling.IMAPMailbox") as transport:
            transport.return_value.poll.return_value = []
            first = poll_correction_mailboxes(limit=1)
            self.assertEqual(first["processed"], 1)
            self.assertEqual(first["replies_sent"], 1)
            self.assertEqual(InboundEmail.objects.filter(processing_state=InboundEmail.State.RECEIVED).count(), 2)
            second = poll_correction_mailboxes(limit=1)
            self.assertEqual(second["processed"], 1)
            self.assertEqual(second["replies_sent"], 1)
            self.assertEqual(InboundEmail.objects.filter(processing_state=InboundEmail.State.RECEIVED).count(), 1)
        self.assertEqual(len(mail.outbox), 2)


class MailboxPollingIsolationTests(TestCase):
    def test_scheduler_routes_duplicate_mailbox_address_to_correction_workflow_only(self):
        MailboxConfiguration.objects.create(name="Corrections", email_address="intake@example.com", is_active=True)
        EmailIntakeMailbox.objects.create(name="Duplicate", email_address="INTAKE@example.com")
        with patch.object(EmailIntakeService, "poll_mailbox") as poll, self.assertLogs("core.email_automation", level="WARNING"):
            result = EmailIntakeService.poll_all(exclude_correction_mailboxes=True)
        poll.assert_not_called()
        self.assertEqual(result["mailboxes"], 0)

    def test_broken_correction_mailbox_does_not_block_other_active_mailbox(self):
        broken = MailboxConfiguration.objects.create(name="Broken", email_address="broken@example.com", is_active=True)
        healthy = MailboxConfiguration.objects.create(name="Healthy", email_address="healthy@example.com", is_active=True)
        MailboxConfiguration.objects.create(name="Inactive", email_address="inactive@example.com", is_active=False)
        stats = {"messages": 1, "processed": 1, "replies_sent": 1, "failed": 0}
        with patch("core.email_polling.poll_correction_mailbox", side_effect=[RuntimeError("offline"), stats]) as poll, self.assertLogs("core.email_polling", level="ERROR"):
            result = poll_correction_mailboxes(limit=7)
        self.assertEqual([call.args[0].pk for call in poll.call_args_list], [broken.pk, healthy.pk])
        self.assertEqual(result, {"mailboxes": 2, **stats, "failed": 1})

    def test_route_polling_respects_mailbox_interval_and_continues_after_failure(self):
        broken = EmailIntakeMailbox.objects.create(name="Broken", email_address="broken@example.com")
        healthy = EmailIntakeMailbox.objects.create(name="Healthy", email_address="healthy@example.com", last_polled_at=timezone.now() - timedelta(seconds=90))
        EmailIntakeMailbox.objects.create(name="Not due", email_address="notdue@example.com", last_polled_at=timezone.now(), poll_interval_seconds=300)
        EmailIntakeMailbox.objects.create(name="Inactive", email_address="inactive@example.com", is_active=False)
        stats = {"messages": 1, "processed": 1, "failed": 0}
        with patch.object(EmailIntakeService, "poll_mailbox", side_effect=[RuntimeError("offline"), stats]) as poll, self.assertLogs("core.email_automation", level="ERROR"):
            result = EmailIntakeService.poll_all()
        self.assertEqual([call.args[0].pk for call in poll.call_args_list], [broken.pk, healthy.pk])
        self.assertEqual(result, {"mailboxes": 2, "messages": 1, "processed": 1, "failed": 1})
