import base64
import csv
import io
import json
import os
import tempfile
from datetime import timedelta
from email.message import EmailMessage
from email import policy as email_policy
from email.parser import BytesParser
from unittest.mock import MagicMock, patch

import httpx
from django.contrib.auth.models import Group
from django.core import mail
from django.core.mail import get_connection
from django.test import Client, TestCase, override_settings

from .ai import AIService
from .admin import EmailAuthorityAdminForm
from .email_intake import authenticated_sender, deliver_reply, ingest_message, process_email
from .mailbox import GraphMailbox, IMAPMailbox
from .models import (
    AIProviderConfig, Attachment, EmailAuthority, EmailReply, EndorsementItem,
    EndorsementRequest, InboundEmail, MailboxConfiguration, PolicyAccess, User, UserProfile, WorkflowEvent,
)
from .tests import BaseInsuranceTest


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class EmailCorrectionTests(BaseInsuranceTest):
    def setUp(self):
        super().setUp()
        self.media = tempfile.TemporaryDirectory()
        self.media_override = override_settings(MEDIA_ROOT=self.media.name)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)
        self.addCleanup(self.media.cleanup)
        self.requester.email = "requester@client.example"
        self.requester.save()
        PolicyAccess.objects.create(policy=self.policy, organization=self.client, can_create=True)
        self.mailbox = MailboxConfiguration.objects.create(name="Test", email_address="intake@insurer.example", imap_host="imap.insurer.example", smtp_host="smtp.insurer.example", trusted_authserv_ids=["mx.insurer.example"], is_active=True)
        smtp = patch("core.mailbox.get_connection", side_effect=lambda **kwargs: get_connection(backend="django.core.mail.backends.locmem.EmailBackend"))
        smtp.start()
        self.addCleanup(smtp.stop)
        self.authority = EmailAuthority.objects.create(name="Policy sender", email_address=self.requester.email, organization=self.client, policy=self.policy, processing_user=self.requester)
        self.counter = 0

    def row(self, **updates):
        return {"employee_no": "E1", "full_name": "Correct Member", "date_of_birth": "1990-01-01", "gender": "Male", "relationship": "Employee", "plan_code": self.plan.code, **updates}

    def receive(self, body="", subject=None, sender=None, files=(), authenticate=True, html=False):
        self.counter += 1
        message = EmailMessage()
        message["From"] = sender or self.requester.email
        message["To"] = self.mailbox.email_address
        message["Subject"] = subject or f"Addition endorsement policy {self.policy.policy_number}"
        message["Message-ID"] = f"<message-{self.counter}@client.example>"
        if authenticate:
            domain = (sender or self.requester.email).rsplit("@", 1)[1]
            message["Authentication-Results"] = f"mx.insurer.example; dmarc=pass header.from={domain}"
        message.set_content(body, subtype="html" if html else "plain")
        for name, content in files:
            message.add_attachment(content, maintype="application", subtype="octet-stream", filename=name)
        return ingest_message(self.mailbox, message.as_bytes(), str(self.counter))

    def initial(self, rows=None, files=()):
        rows = [self.row(), self.row(employee_no="E2", full_name="Error Member", date_of_birth=None)] if rows is None else rows
        body = f"Effective date: {self.today.isoformat()}\nBEGIN MEMBERS\n{json.dumps(rows)}\nEND MEMBERS"
        email = self.receive(body, files=files)
        return process_email(email.pk)

    def test_reply_separates_members_and_stays_in_the_received_thread(self):
        original = self.initial()
        self.assertEqual(original.processing_state, InboundEmail.State.NEEDS_INFO)
        self.assertEqual(original.endorsement.items.count(), 2)
        self.assertTrue(deliver_reply(original.reply.pk))
        self.assertEqual(len(mail.outbox), 1)
        outgoing = mail.outbox[0]
        self.assertIn(original.reference, outgoing.subject)
        self.assertEqual(outgoing.to, [self.requester.email])
        self.assertEqual(outgoing.extra_headers["In-Reply-To"], original.message_id)
        self.assertEqual(outgoing.extra_headers["X-SmartEndorse-Reference"], original.reference)
        self.assertIn("CORRECT MEMBERS (1)", outgoing.body)
        self.assertIn("ERROR MEMBERS / NEEDS CORRECTION (1)", outgoing.body)
        self.assertIn("Date Of Birth is required", outgoing.body)
        self.assertEqual(len(outgoing.attachments), 1)
        deliver_reply(original.reply.pk)
        self.assertEqual(len(mail.outbox), 1)

    def test_authorized_reply_corrects_error_and_updates_accepted_member_without_duplicates(self):
        original = self.initial()
        correct, error = list(original.endorsement.items.order_by("pk"))
        rows = [{"row_reference": f"ITEM-{error.pk}", "date_of_birth": "1992-02-03"}, {"row_reference": f"ITEM-{correct.pk}", "full_name": "Corrected Accepted Name", "employee_no": "E1-UPDATED"}]
        reply = self.receive("BEGIN MEMBERS\n" + json.dumps(rows) + "\nEND MEMBERS", subject=f"Re: [SE: {original.reference}] Addition")
        result = process_email(reply.pk)
        self.assertEqual(result.processing_state, InboundEmail.State.PROCESSED, result.processing_error)
        self.assertEqual(result.endorsement_id, original.endorsement_id)
        self.assertEqual(result.endorsement.items.count(), 2)
        correct.refresh_from_db(); error.refresh_from_db()
        self.assertEqual(correct.full_name, "Corrected Accepted Name")
        self.assertEqual(correct.employee_no, "E1-UPDATED")
        self.assertEqual(correct.date_of_birth.isoformat(), "1990-01-01")
        self.assertEqual(error.date_of_birth.isoformat(), "1992-02-03")
        self.assertEqual(correct.validation_status, EndorsementItem.ValidationStatus.VALID)
        event = WorkflowEvent.objects.get(request=original.endorsement, event_type="EMAIL_CORRECT_MEMBER_CHANGED")
        self.assertEqual(event.payload["before"]["full_name"], "Correct Member")
        self.assertEqual(event.payload["after"]["full_name"], "Corrected Accepted Name")
        self.assertFalse(result.endorsement.approvals.exists())
        process_email(reply.pk)
        self.assertEqual(result.endorsement.items.count(), 2)

    def test_filled_csv_reply_merges_by_stable_member_reference(self):
        original = self.initial()
        data = list(csv.DictReader(io.StringIO(original.reply.correction_csv)))
        data[1]["date_of_birth"] = "1992-01-01"
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=data[0].keys()); writer.writeheader(); writer.writerows(data)
        correction = self.receive(subject=f"Re: {original.reference}", files=[("corrections.csv", output.getvalue().encode())])
        result = process_email(correction.pk)
        self.assertEqual(result.processing_state, InboundEmail.State.PROCESSED, result.processing_error)
        self.assertEqual(result.endorsement.items.count(), 2)

    def test_html_reply_reads_both_correct_and_error_member_tables(self):
        original = self.initial()
        correct, error = list(original.endorsement.items.order_by("pk"))
        html = f"""<h3>Correct members</h3><table>
        <tr><th>Member Ref</th><th>Full Name</th><th>DOB</th></tr>
        <tr><td>ITEM-{correct.pk}</td><td>Changed Correct Name</td><td></td></tr></table>
        <h3>Error members</h3><table>
        <tr><th>Member Ref</th><th>Full Name</th><th>DOB</th></tr>
        <tr><td>ITEM-{error.pk}</td><td></td><td>1992-02-03</td></tr></table>
        <blockquote>{original.reply.body_html}</blockquote>"""
        reply = self.receive(html, subject=f"Re: {original.reference}", html=True)
        result = process_email(reply.pk)
        self.assertEqual(result.processing_state, InboundEmail.State.PROCESSED, result.processing_error)
        self.assertEqual(result.endorsement.items.count(), 2)
        correct.refresh_from_db(); error.refresh_from_db()
        self.assertEqual(correct.full_name, "Changed Correct Name")
        self.assertEqual(correct.date_of_birth.isoformat(), "1990-01-01")
        self.assertEqual(error.full_name, "Error Member")
        self.assertEqual(error.date_of_birth.isoformat(), "1992-02-03")

    def test_distinct_people_sharing_employee_number_are_not_overwritten(self):
        original = self.initial(rows=[self.row(national_id="N1"), self.row(national_id="N2", full_name="Dependent Member", relationship="Child", date_of_birth="2015-02-03")])
        self.assertEqual(original.endorsement.items.count(), 2)
        self.assertEqual(set(original.endorsement.items.values_list("full_name", flat=True)), {"Correct Member", "Dependent Member"})

    def test_helper_columns_do_not_send_blank_correction_fields_to_ai(self):
        original = self.initial()
        item = original.endorsement.items.first()
        AIProviderConfig.objects.create(name="Configured text provider", provider="OPENAI", model_name="configured-model", is_active=True)
        body = f"BEGIN MEMBERS\nMember Ref,Full Name,DOB\nITEM-{item.pk},Updated Name,\nEND MEMBERS"
        reply = self.receive(body, subject=f"Re: {original.reference}")
        with patch("core.ai.AIService.normalize_structured_rows") as mapping:
            result = process_email(reply.pk)
        mapping.assert_not_called()
        item.refresh_from_db()
        self.assertEqual(item.full_name, "Updated Name")
        self.assertEqual(item.date_of_birth.isoformat(), "1990-01-01")
        self.assertEqual(result.endorsement.items.count(), 2)

    def test_correction_updates_salary_and_keeps_plan_derived_sum_assured(self):
        from decimal import Decimal
        self.plan.sum_assured = Decimal("10000"); self.plan.save()
        original = self.initial(rows=[self.row(annual_salary="12000")])
        item = original.endorsement.items.get()
        reply = self.receive("BEGIN MEMBERS\n" + json.dumps([{"row_reference": f"ITEM-{item.pk}", "annual_salary": "18000", "sum_assured": "999999"}]) + "\nEND MEMBERS", subject=f"Re: {original.reference}")
        result = process_email(reply.pk)
        item.refresh_from_db()
        self.assertEqual(result.processing_state, InboundEmail.State.PROCESSED, result.processing_error)
        self.assertEqual(item.annual_salary, Decimal("18000"))
        self.assertEqual(item.sum_assured, Decimal("10000"))
        self.assertIn("18000", result.reply.correction_csv)

    def test_ocr_failure_queues_reply_and_document_reference_can_replace_failed_read(self):
        with patch("core.email_intake.FileIntakeService._extract", side_effect=RuntimeError("OCR unreadable")):
            original = self.initial(rows=[], files=[("passport.png", b"bad image")])
        self.assertEqual(original.processing_state, InboundEmail.State.NEEDS_INFO)
        failed = original.endorsement.attachments.get()
        self.assertIn(f"DOC-{failed.pk}", original.reply.body_text)
        self.assertIn("OCR unreadable", original.reply.body_text)
        row = self.row(row_reference=f"DOC-{failed.pk}")
        correction = self.receive("BEGIN MEMBERS\n" + json.dumps([row]) + "\nEND MEMBERS", subject=f"Re: {original.reference}")
        result = process_email(correction.pk)
        self.assertEqual(result.processing_state, InboundEmail.State.PROCESSED, result.processing_error)
        failed.refresh_from_db()
        self.assertEqual(failed.processing_error, "OCR unreadable")
        self.assertEqual(failed.extracted_payload["superseded_by_email_id"], correction.pk)

    def test_unrelated_correct_row_does_not_discard_a_failed_document(self):
        with patch("core.email_intake.FileIntakeService._extract", side_effect=RuntimeError("OCR unreadable")):
            original = self.initial(rows=[self.row()], files=[("passport.png", b"bad")])
        item = original.endorsement.items.get()
        reply = self.receive("BEGIN MEMBERS\n" + json.dumps([{"row_reference": f"ITEM-{item.pk}", "full_name": "Updated"}]) + "\nEND MEMBERS", subject=f"Re: {original.reference}")
        result = process_email(reply.pk)
        self.assertEqual(result.processing_state, InboundEmail.State.NEEDS_INFO)
        self.assertIn("OCR unreadable", result.processing_error)

    def test_document_reference_can_confirm_an_already_extracted_member(self):
        with patch("core.email_intake.FileIntakeService._extract", side_effect=RuntimeError("OCR unreadable")):
            original = self.initial(rows=[self.row(national_id="N1")], files=[("passport.png", b"bad")])
        failed = original.endorsement.attachments.get()
        reply = self.receive("BEGIN MEMBERS\n" + json.dumps([self.row(national_id="N1", row_reference=f"DOC-{failed.pk}")]) + "\nEND MEMBERS", subject=f"Re: {original.reference}")
        result = process_email(reply.pk)
        self.assertEqual(result.processing_state, InboundEmail.State.PROCESSED, result.processing_error)
        self.assertEqual(result.endorsement.items.count(), 1)
        failed.refresh_from_db()
        self.assertEqual(failed.extracted_payload["superseded_by_email_id"], reply.pk)

    def test_unauthorized_reference_does_not_disclose_or_change_member_data(self):
        original = self.initial()
        item = original.endorsement.items.first()
        reply = self.receive("BEGIN MEMBERS\n" + json.dumps([{"row_reference": f"ITEM-{item.pk}", "full_name": "Intruder"}]) + "\nEND MEMBERS", subject=f"Re: {original.reference}", sender="unknown@client.example")
        result = process_email(reply.pk)
        self.assertEqual(result.processing_state, InboundEmail.State.UNAUTHORIZED)
        self.assertFalse(EmailReply.objects.filter(email=result).exists())
        item.refresh_from_db(); self.assertNotEqual(item.full_name, "Intruder")

    def test_spoofed_or_untrusted_authentication_headers_are_rejected(self):
        email = self.receive(authenticate=False)
        email.headers = {"authentication-results": ["attacker.example; dmarc=pass header.from=client.example"]}
        email.save()
        self.assertFalse(authenticated_sender(email))
        self.assertEqual(process_email(email.pk).processing_state, InboundEmail.State.UNAUTHORIZED)
        self.assertFalse(EmailReply.objects.filter(email=email).exists())

    def test_deactivated_user_and_expired_authority_cannot_process(self):
        self.requester.is_active = False; self.requester.save()
        self.assertEqual(self.initial().processing_state, InboundEmail.State.UNAUTHORIZED)
        self.requester.is_active = True; self.requester.save()
        self.authority.valid_until = self.today - timedelta(days=1); self.authority.save()
        self.assertEqual(self.initial().processing_state, InboundEmail.State.UNAUTHORIZED)

    def test_group_authority_requires_active_matching_portal_member(self):
        self.authority.delete()
        group = Group.objects.create(name="Policy Correction Team")
        EmailAuthority.objects.create(name="Policy group", group=group, policy=self.policy, organization=self.client)
        self.requester.groups.add(group)
        self.assertEqual(self.initial(rows=[self.row()]).processing_state, InboundEmail.State.PROCESSED)
        self.requester.groups.remove(group)
        self.assertEqual(self.initial(rows=[self.row()]).processing_state, InboundEmail.State.UNAUTHORIZED)

    def test_exact_address_uses_explicit_processing_user_instead_of_sender_account(self):
        self.authority.email_address = "external@client.example"
        self.authority.save()
        sender_user = User.objects.create_user("external", email=self.authority.email_address)
        UserProfile.objects.create(user=sender_user, organization=self.client, role=UserProfile.Role.CLIENT_VIEWER)
        body = f"Effective date: {self.today.isoformat()}\nBEGIN MEMBERS\n{json.dumps([self.row()])}\nEND MEMBERS"
        result = process_email(self.receive(body, sender=self.authority.email_address).pk)
        self.assertEqual(result.processing_state, InboundEmail.State.PROCESSED, result.processing_error)
        self.assertEqual(result.endorsement.requester, self.requester)

    def test_ineligible_explicit_delegate_does_not_fall_back_to_sender_account(self):
        self.authority.processing_user = self.manager
        self.authority.save()
        result = self.initial(rows=[self.row()])
        self.assertEqual(result.processing_state, InboundEmail.State.UNAUTHORIZED)
        self.assertIn("different organization", result.processing_error)
        self.assertFalse(EndorsementRequest.objects.exists())
        self.assertFalse(EmailReply.objects.exists())

    def test_legacy_whitespace_and_case_in_grant_and_user_addresses_are_normalized(self):
        self.authority.email_address = "  REQUESTER@CLIENT.EXAMPLE  "
        self.authority.processing_user = None
        self.authority.save()
        self.requester.email = " Requester@Client.Example "
        self.requester.save()
        body = f"Effective date: {self.today.isoformat()}\nBEGIN MEMBERS\n{json.dumps([self.row()])}\nEND MEMBERS"
        result = process_email(self.receive(body, sender="requester@client.example").pk)
        self.assertEqual(result.processing_state, InboundEmail.State.PROCESSED, result.processing_error)

    def test_admin_requires_eligible_actor_for_external_email_grant(self):
        data = {"name": "External HR", "email_address": "external@client.example", "policy": self.policy.pk,
                "organization": self.client.pk, "permitted_types": "[]", "is_active": "on"}
        form = EmailAuthorityAdminForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("Processing user", str(form.errors["processing_user"]))
        data["processing_user"] = self.requester.pk
        form = EmailAuthorityAdminForm(data=data)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        PolicyAccess.objects.filter(policy=self.policy, organization=self.client).update(can_create=False)
        form = EmailAuthorityAdminForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn("Policy access", str(form.errors["processing_user"]))

    def test_missing_policy_creation_access_is_explained_and_blocks_data_and_replies(self):
        PolicyAccess.objects.filter(policy=self.policy, organization=self.client).update(can_create=False)
        result = self.initial(rows=[self.row()])
        self.assertEqual(result.processing_state, InboundEmail.State.UNAUTHORIZED)
        self.assertIn("Policy access", result.processing_error)
        self.assertFalse(EndorsementRequest.objects.exists())
        self.assertFalse(EmailReply.objects.exists())

    def test_user_and_group_grants_do_not_delegate_around_sender_role(self):
        self.authority.delete()
        group = Group.objects.create(name="Corrections")
        self.requester.groups.add(group)
        profile = self.requester.profile
        profile.role = UserProfile.Role.CLIENT_VIEWER
        profile.save()
        self.manager.profile.organization = self.client
        self.manager.profile.save()
        for identity in ({"user": self.requester}, {"group": group}):
            with self.subTest(identity=identity):
                grant = EmailAuthority.objects.create(name="Restricted sender", policy=self.policy, organization=self.client,
                                                       processing_user=self.manager, **identity)
                result = self.initial(rows=[self.row()])
                self.assertEqual(result.processing_state, InboundEmail.State.UNAUTHORIZED)
                self.assertIn("role cannot create", result.processing_error)
                grant.delete()
        self.assertFalse(EndorsementRequest.objects.exists())

    def test_grant_restrictions_report_specific_reason_and_later_valid_grant_can_match(self):
        for updates, reason in (({"is_active": False}, "inactive"),
                                ({"valid_from": self.today + timedelta(days=1)}, "not valid today"),
                                ({"permitted_types": ["DELETION"]}, "does not permit ADDITION")):
            with self.subTest(updates=updates):
                EmailAuthority.objects.filter(pk=self.authority.pk).update(is_active=True, valid_from=None, permitted_types=[])
                EmailAuthority.objects.filter(pk=self.authority.pk).update(**updates)
                result = self.initial(rows=[self.row()])
                self.assertEqual(result.processing_state, InboundEmail.State.UNAUTHORIZED)
                self.assertIn(reason, result.processing_error)
        EmailAuthority.objects.create(name="Second valid grant", email_address=self.requester.email, policy=self.policy,
                                      organization=self.client, processing_user=self.requester)
        self.assertEqual(self.initial(rows=[self.row()]).processing_state, InboundEmail.State.PROCESSED)

    def test_trusted_dmarc_supports_comments_quoted_values_and_normalized_server_ids(self):
        self.mailbox.trusted_authserv_ids = [" MX.INSURER.EXAMPLE. "]
        self.mailbox.save()
        email = self.receive(authenticate=False)
        email.headers = {"authentication-results": ['(receiver) "mx.insurer.example" 1; dmarc=pass (aligned) header.from="client.example"']}
        email.save()
        self.assertTrue(authenticated_sender(email))
        self.assertEqual(process_email(email.pk).processing_state, InboundEmail.State.NEEDS_INFO)

    def test_dmarc_claims_inside_comments_or_quoted_reasons_are_rejected(self):
        for result in ("mx.insurer.example; dkim=fail (dmarc=pass header.from=client.example)",
                       'mx.insurer.example; dmarc=pass reason="header.from=client.example"',
                       'mx.insurer.example; dkim=fail reason="bad; dmarc=pass header.from=client.example"',
                       "mx.insurer.example; dmarc=pass header.from=other.example"):
            with self.subTest(result=result):
                email = self.receive(authenticate=False)
                email.headers = {"authentication-results": [result]}
                email.save()
                self.assertFalse(authenticated_sender(email))
                processed = process_email(email.pk)
                self.assertEqual(processed.processing_state, InboundEmail.State.UNAUTHORIZED)
                self.assertEqual(processed.status_label, "Sender verification failed")
                self.assertFalse(EmailReply.objects.filter(email=processed).exists())

    def test_microsoft_headers_without_server_id_have_actionable_verification_error(self):
        self.mailbox.transport = MailboxConfiguration.Transport.GRAPH
        self.mailbox.save()
        email = self.receive(authenticate=False)
        email.headers = {"authentication-results": ["spf=pass smtp.mailfrom=client.example; dkim=pass header.d=client.example; dmarc=pass header.from=client.example; compauth=pass reason=100"]}
        email.save()
        result = process_email(email.pk)
        self.assertEqual(result.processing_state, InboundEmail.State.UNAUTHORIZED)
        self.assertIn("no receiving server ID", result.processing_error)
        self.assertIn("Admin", result.processing_error)
        self.assertFalse(EmailReply.objects.exists())

    def test_upstream_verification_option_still_requires_a_policy_authority(self):
        self.mailbox.require_sender_authentication = False
        self.mailbox.save()
        email = self.receive(authenticate=False)
        self.assertEqual(process_email(email.pk).processing_state, InboundEmail.State.NEEDS_INFO)
        email = self.receive(sender="unknown@client.example", authenticate=False)
        result = process_email(email.pk)
        self.assertEqual(result.processing_state, InboundEmail.State.UNAUTHORIZED)
        self.assertIn("No Email authority matches From address", result.processing_error)
        self.assertFalse(EmailReply.objects.filter(email=result).exists())

    def test_verification_failure_is_visible_in_portal_and_admin_retry_reports_block(self):
        email = process_email(self.receive(authenticate=False).pk)
        self.manager.is_staff = self.manager.is_superuser = True
        self.manager.save()
        http = Client()
        http.force_login(self.manager)
        response = http.get(f"/inbound-emails/{email.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sender verification failed")
        self.assertContains(response, "no Authentication-Results header")
        response = http.post("/admin/core/inboundemail/", {"action": "retry_intake", "_selected_action": [email.pk]}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sender verification failed: 1")
        self.assertFalse(EmailReply.objects.exists())

    def test_provider_setting_action_unblocks_authorized_headerless_mail_on_next_poll(self):
        from .email_polling import poll_correction_mailboxes
        body = f"Effective date: {self.today.isoformat()}\nBEGIN MEMBERS\n{json.dumps([self.row()])}\nEND MEMBERS"
        original = process_email(self.receive(body, authenticate=False).pk)
        intruder = process_email(self.receive(body, sender="intruder@client.example", authenticate=False).pk)
        self.assertEqual(original.processing_state, InboundEmail.State.UNAUTHORIZED)
        self.assertIn("Uncheck Require sender authentication", original.processing_error)
        self.assertFalse(EndorsementRequest.objects.exists())
        self.manager.is_staff = self.manager.is_superuser = True
        self.manager.save()
        http = Client()
        http.force_login(self.manager)
        response = http.post("/admin/core/mailboxconfiguration/", {
            "action": "use_provider_verification", "_selected_action": [self.mailbox.pk],
        }, follow=True)
        self.assertEqual(response.status_code, 200)
        with patch("core.email_polling.IMAPMailbox") as transport:
            transport.return_value.poll.return_value = []
            result = poll_correction_mailboxes()
            original.refresh_from_db()
            intruder.refresh_from_db()
            self.assertEqual(result["rechecked"], 2)
            self.assertEqual(original.processing_state, InboundEmail.State.PROCESSED, original.processing_error)
            self.assertEqual(original.endorsement.items.count(), 1)
            self.assertEqual(intruder.processing_state, InboundEmail.State.UNAUTHORIZED)
            self.assertIn("No Email authority matches", intruder.processing_error)
            self.assertIsNone(intruder.endorsement_id)
            self.assertFalse(EmailReply.objects.filter(email=intruder).exists())
            self.assertEqual(len(mail.outbox), 1)
            poll_correction_mailboxes()
            self.assertEqual(original.endorsement.items.count(), 1)
            self.assertEqual(len(mail.outbox), 1)

    def test_automatic_recheck_does_not_reapply_an_already_processed_message(self):
        email = self.initial(rows=[self.row()])
        email.body_text = f"BEGIN MEMBERS\n{json.dumps([self.row(full_name='Unexpected replay')])}\nEND MEMBERS"
        email.save()
        self.authority.is_active = False
        self.authority.save()
        result = process_email(email.pk, recheck_authorization=True)
        self.assertEqual(result.processing_state, InboundEmail.State.PROCESSED)
        self.assertEqual(result.endorsement.items.get().full_name, "Correct Member")

    def test_reply_authorization_is_rechecked_with_actionable_denial(self):
        email = self.initial(rows=[self.row()])
        self.authority.is_active = False
        self.authority.save()
        self.assertFalse(deliver_reply(email.reply.pk))
        email.reply.refresh_from_db()
        self.assertIn("inactive", email.reply.last_error)
        self.assertFalse(mail.outbox)

    def test_mail_failure_remains_pending_and_can_retry_once(self):
        email = self.initial()
        with patch("core.email_intake.EmailMultiAlternatives.send", side_effect=RuntimeError("SMTP unavailable")):
            self.assertFalse(deliver_reply(email.reply.pk))
        reply = EmailReply.objects.get(pk=email.reply.pk)
        self.assertIsNone(reply.sent_at)
        self.assertIn("SMTP unavailable", reply.last_error)
        self.assertTrue(deliver_reply(reply.pk))
        self.assertEqual(len(mail.outbox), 1)

    def test_reference_without_policy_details_can_be_completed_later(self):
        original = self.receive("Please add members under P1.")
        original = process_email(original.pk)
        self.assertEqual(original.processing_state, InboundEmail.State.NEEDS_INFO)
        self.assertIsNone(original.endorsement_id)
        self.assertIn("effective date", original.reply.body_text)
        correction = self.receive(f"Effective date: {self.today.isoformat()}\nBEGIN MEMBERS\n" + json.dumps([self.row()]) + "\nEND MEMBERS", subject=f"Re: {original.reference}")
        result = process_email(correction.pk)
        self.assertEqual(result.processing_state, InboundEmail.State.PROCESSED, result.processing_error)
        original.refresh_from_db()
        self.assertEqual(original.endorsement_id, result.endorsement_id)

    def test_completed_or_dispatched_request_is_not_changed_by_late_reply(self):
        original = self.initial(rows=[self.row()])
        request = original.endorsement; request.status = EndorsementRequest.Status.COMPLETED; request.save()
        item = request.items.get()
        reply = self.receive("BEGIN MEMBERS\n" + json.dumps([{"row_reference": f"ITEM-{item.pk}", "full_name": "Changed"}]) + "\nEND MEMBERS", subject=f"Re: {original.reference}")
        result = process_email(reply.pk)
        self.assertEqual(result.processing_state, InboundEmail.State.NEEDS_REVIEW)
        item.refresh_from_db(); self.assertEqual(item.full_name, "Correct Member")

    def test_quoted_original_is_not_reapplied_as_new_data(self):
        email = self.receive("Thanks\nOn Monday someone wrote:\nBEGIN MEMBERS\n" + json.dumps([self.row()]) + "\nEND MEMBERS")
        self.assertEqual(email.body_text, "Thanks")

    def test_duplicate_message_id_is_ingested_once_even_with_different_uid(self):
        email = self.receive("hello")
        with email.raw_message.open("rb") as f:
            again = ingest_message(self.mailbox, f.read(), "other-uid")
        self.assertEqual(again.pk, email.pk)

    def test_older_replay_cannot_overwrite_newer_corrections_or_sent_reply(self):
        original = self.initial()
        deliver_reply(original.reply.pk)
        sent_body = original.reply.body_text
        correct, error = list(original.endorsement.items.order_by("pk"))
        rows = [{"row_reference": f"ITEM-{correct.pk}", "full_name": "Latest Name"}, {"row_reference": f"ITEM-{error.pk}", "date_of_birth": "1992-02-03"}]
        correction = self.receive("BEGIN MEMBERS\n" + json.dumps(rows) + "\nEND MEMBERS", subject=f"Re: {original.reference}")
        self.assertEqual(process_email(correction.pk).processing_state, InboundEmail.State.PROCESSED)
        replay = process_email(original.pk, force=True)
        self.assertEqual(replay.processing_state, InboundEmail.State.NEEDS_REVIEW)
        self.assertIn("newer email correction", replay.processing_error)
        correct.refresh_from_db(); error.refresh_from_db(); original.reply.refresh_from_db()
        self.assertEqual(correct.full_name, "Latest Name")
        self.assertEqual(error.date_of_birth.isoformat(), "1992-02-03")
        self.assertEqual(original.reply.body_text, sent_body)

    @patch("core.mailbox.imaplib.IMAP4_SSL")
    def test_imap_cursor_advances_only_after_ingestion_and_fetch_retry_is_safe(self, imap):
        self.mailbox.imap_password = "test-secret"; self.mailbox.save()
        message = EmailMessage()
        message["From"] = self.requester.email
        message["Message-ID"] = "<imap-message@client.example>"
        message.set_content("Retained MIME")
        client = imap.return_value.__enter__.return_value
        client.select.return_value = ("OK", [])
        client.response.return_value = ("UIDVALIDITY", [b"100"])
        client.uid.side_effect = [("OK", [b"1 2"]), ("OK", [(b"1", message.as_bytes())]), ("NO", [])]
        with self.assertRaisesRegex(RuntimeError, "cursor retained"):
            IMAPMailbox(self.mailbox).poll()
        self.mailbox.refresh_from_db()
        self.assertEqual(self.mailbox.cursor, {"uidvalidity": "100", "last_uid": 1})
        self.assertEqual(self.mailbox.messages.get().message_uid, "100:1")
        client.uid.side_effect = [("OK", [b"1 2"]), ("OK", [(b"2", message.as_bytes())])]
        IMAPMailbox(self.mailbox).poll()
        self.assertEqual(self.mailbox.cursor["last_uid"], 2)
        self.assertEqual(self.mailbox.messages.count(), 1)
        client.response.return_value = ("UIDVALIDITY", [None])
        with self.assertRaisesRegex(RuntimeError, "UIDVALIDITY"):
            IMAPMailbox(self.mailbox).poll()

    @patch("core.mailbox.httpx.Client")
    def test_graph_pending_page_survives_partial_failure(self, client_type):
        self.mailbox.graph_tenant_id = "tenant"
        self.mailbox.graph_client_id = "client"
        self.mailbox.graph_client_secret = "test-secret"
        self.mailbox.save()
        client = client_type.return_value
        root_url = GraphMailbox.ROOT
        def response(data=None, content=None, status=200):
            return httpx.Response(status, json=data, content=content, request=httpx.Request("GET", root_url))
        client.post.return_value = response({"access_token": "test-token"})
        message = EmailMessage(); message["From"] = self.requester.email; message["Message-ID"] = "<graph-1@client.example>"; message.set_content("First")
        delta = root_url + "/users/intake/messages/delta?token=next"
        client.get.side_effect = [response({"value": [{"id": "a"}, {"id": "b"}], "@odata.deltaLink": delta}), response(content=message.as_bytes()), response(status=503)]
        graph = GraphMailbox(self.mailbox)
        with self.assertRaises(httpx.HTTPStatusError):
            graph.poll(limit=2)
        self.mailbox.refresh_from_db()
        self.assertEqual(self.mailbox.cursor, {"delta_link": delta, "pending_ids": ["b"]})
        message.replace_header("Message-ID", "<graph-2@client.example>")
        client.get.side_effect = [response(content=message.as_bytes())]
        self.assertEqual(len(graph.poll(limit=1)), 1)
        self.assertEqual(self.mailbox.messages.count(), 2)
        self.assertEqual(self.mailbox.cursor["pending_ids"], [])
        self.assertTrue(client.get.call_args.args[0].endswith("/messages/b/$value"))
        with self.assertRaisesRegex(ValueError, "invalid delta link"):
            graph._checked_link("https://attacker.example/v1.0/messages")
        graph.close()

    @patch("core.mailbox.httpx.Client")
    def test_graph_native_reply_preserves_headers_and_does_not_resend_sent_draft(self, client_type):
        self.mailbox.transport = "GRAPH"
        self.mailbox.graph_tenant_id = "tenant"
        self.mailbox.graph_client_id = "client"
        self.mailbox.graph_client_secret = "test-secret"
        self.mailbox.save()
        original = self.initial()
        client = client_type.return_value
        def response(data):
            return httpx.Response(200, json=data, request=httpx.Request("POST", GraphMailbox.ROOT))
        sent = []
        def post(url, **kwargs):
            if url.endswith("/token"):
                return response({"access_token": "test-token"})
            if url.endswith("/createReply"):
                mime = BytesParser(policy=email_policy.default).parsebytes(base64.b64decode(kwargs["content"]))
                self.assertEqual(kwargs["headers"]["Content-Type"], "text/plain")
                self.assertEqual(mime["X-SmartEndorse-Reference"], original.reference)
                self.assertEqual(mime["In-Reply-To"], original.message_id)
                self.assertIn(original.reference, mime["Subject"])
                self.assertEqual(mime["To"], self.requester.email)
                return response({"id": "saved-draft"})
            if url.endswith("/send"):
                sent.append(url)
                raise RuntimeError("Response lost after server accepted reply")
            self.fail(url)
        client.post.side_effect = post
        client.get.side_effect = [response({"isDraft": True}), response({"value": [{"name": f"corrections-{original.reference}.csv"}]}), response({"isDraft": False})]
        client.patch.return_value = response({})
        self.assertFalse(deliver_reply(original.reply.pk))
        original.reply.refresh_from_db()
        self.assertEqual(original.reply.graph_draft_id, "saved-draft")
        self.assertTrue(deliver_reply(original.reply.pk))
        self.assertEqual(len(sent), 1)
        self.assertEqual(client.close.call_count, 2)
        self.assertEqual(client.patch.call_args.kwargs["json"]["toRecipients"], [{"emailAddress": {"address": self.requester.email}}])

    def test_portal_email_is_scoped_and_body_is_escaped(self):
        email = self.initial()
        email.body_text = "<script>bad()</script>"; email.save()
        from django.test import Client
        browser = Client(); browser.force_login(self.requester)
        response = browser.get(f"/inbound-emails/{email.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "&lt;script&gt;bad()&lt;/script&gt;")
        browser.force_login(self.tpa_user)
        self.assertEqual(browser.get(f"/inbound-emails/{email.pk}/").status_code, 404)


class AIProviderAPITests(TestCase):
    def provider(self, **values):
        return AIProviderConfig.objects.create(name="Provider", provider="HUGGINGFACE", model_name="org/model", is_active=True, **values)

    @patch("core.ai.httpx.Client")
    @patch.dict(os.environ, {"HF_TEST_TOKEN": "test-secret"})
    def test_huggingface_router_token_provider_suffix_and_model_json(self, client):
        cfg = self.provider(secret_reference="HF_TEST_TOKEN", inference_provider="fastest")
        response = client.return_value.__enter__.return_value.post.return_value
        response.raise_for_status.return_value.json.return_value = {"choices": [{"message": {"content": '{"items":[{"full_name":"Member"}]}'}}]}
        rows = AIService(config=cfg).extract_text_rows("Member Name: Member")
        call = client.return_value.__enter__.return_value.post.call_args
        self.assertEqual(call.args[0], "https://router.huggingface.co/v1/chat/completions")
        self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer test-secret")
        self.assertEqual(call.kwargs["json"]["model"], "org/model:fastest")
        self.assertEqual(rows[0]["full_name"], "Member")

    def test_custom_v1_and_full_endpoint_are_not_duplicated(self):
        cfg = self.provider(base_url="https://inference.example/v1")
        service = AIService(config=cfg)
        self.assertEqual(service._api_url("/v1/chat/completions"), "https://inference.example/v1/chat/completions")
        cfg.base_url = "https://inference.example/v1/chat/completions"
        self.assertEqual(service._api_url("/v1/chat/completions"), cfg.base_url)

    def test_priority_keeps_text_and_vision_providers_separate(self):
        vision = self.provider(priority=1, supports_vision=True)
        text = AIProviderConfig.objects.create(name="Text", provider="OPENAI", model_name="configurable-model", priority=10, is_active=True)
        self.assertEqual(AIService().config, text)
        service = AIService(); service._require_provider(vision=True)
        self.assertEqual(service.config, vision)

    def test_missing_secret_or_malformed_member_response_fails_clearly(self):
        cfg = self.provider(secret_reference="MISSING_TOKEN_FOR_TEST")
        with self.assertRaisesRegex(RuntimeError, "not set"):
            AIService(config=cfg)._headers()
        with self.assertRaisesRegex(ValueError, "JSON object"):
            AIService._parse_json_array('["not a row"]')
        with self.assertRaisesRegex(ValueError, "Invalid value"):
            AIService._parse_json_array('[{"full_name": ["nested"]}]')
        with self.assertRaisesRegex(ValueError, "no identifiable"):
            AIService._parse_json_array('[{}]')

    @patch("core.ai.httpx.Client")
    def test_huggingface_vision_uses_image_url_blocks(self, client):
        cfg = self.provider(supports_vision=True)
        response = client.return_value.__enter__.return_value.post.return_value
        response.raise_for_status.return_value = response
        response.json.return_value = {"choices": [{"message": {"content": '{"items":[{"full_name":"Member"}]}'}}]}
        AIService(config=cfg)._vision("Extract", "aW1hZ2U=", "image/png")
        call = client.return_value.__enter__.return_value.post.call_args
        self.assertEqual(call.args[0], "https://router.huggingface.co/v1/chat/completions")
        self.assertEqual(call.kwargs["json"]["messages"][0]["content"][1]["image_url"]["url"], "data:image/png;base64,aW1hZ2U=")

    @patch("core.ai.httpx.Client")
    def test_openai_claude_and_local_provider_protocols_and_parameters(self, client):
        for provider, base, endpoint, response in [
            ("OPENAI", "https://api.openai.com/v1", "/chat/completions", {"choices": [{"message": {"content": '[{"full_name":"Member"}]'}}]}),
            ("ANTHROPIC", "https://api.anthropic.com/v1", "/messages", {"content": [{"type": "text", "text": '[{"full_name":"Member"}]'}]}),
            ("OLLAMA", "http://localhost:11434", "/api/chat", {"message": {"content": '[{"full_name":"Member"}]'}}),
            ("OPENAI_COMPATIBLE", "http://localhost:8000/v1", "/chat/completions", {"choices": [{"message": {"content": '[{"full_name":"Member"}]'}}]}),
        ]:
            with self.subTest(provider=provider):
                cfg = AIProviderConfig.objects.create(name=provider, provider=provider, model_name="configured-model", base_url=base, api_key="test-key", options={"request_parameters": {"temperature": None, "max_tokens": 4096}})
                client.return_value.__enter__.return_value.post.return_value.raise_for_status.return_value.json.return_value = response
                self.assertEqual(AIService(config=cfg).extract_text_rows("Member name: Member")[0]["full_name"], "Member")
                call = client.return_value.__enter__.return_value.post.call_args
                self.assertEqual(call.args[0], base + endpoint)
                self.assertEqual(call.kwargs["json"]["max_tokens"], 4096)
                self.assertNotIn("temperature", call.kwargs["json"])
                if provider == "ANTHROPIC":
                    self.assertEqual(call.kwargs["headers"]["x-api-key"], "test-key")
                    self.assertEqual(call.kwargs["headers"]["anthropic-version"], "2023-06-01")
                else:
                    self.assertEqual(call.kwargs["headers"]["Authorization"], "Bearer test-key")
