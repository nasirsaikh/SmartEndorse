import os
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from django.forms import modelform_factory
from django.test import SimpleTestCase, TestCase

from .credentials import MailboxCredentialError, credential
from .email_polling import poll_correction_mailboxes
from .mailbox import GraphMailbox, IMAPMailbox
from .models import MailboxConfiguration


class CredentialResolutionTests(SimpleTestCase):
    def test_invalid_references_are_rejected_without_echoing_the_input(self):
        for submitted in ("example-secret.with-punctuation", "secret with spaces", "0_SECRET", "SECRET\nOTHER"):
            with self.subTest(submitted=submitted), self.assertRaises(MailboxCredentialError) as error:
                credential(submitted, label="Graph secret reference", example="ENDORSEMENT_GRAPH_CLIENT_SECRET")
            self.assertNotIn(submitted, str(error.exception))
            self.assertIn("environment variable name", str(error.exception))
            self.assertIn("ENDORSEMENT_GRAPH_CLIENT_SECRET", str(error.exception))

    def test_missing_variable_does_not_echo_a_secret_that_looks_like_a_variable_name(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(MailboxCredentialError) as error:
            credential("ThisCouldBeAnAlphanumericSecret", label="Graph secret reference", example="ENDORSEMENT_GRAPH_CLIENT_SECRET")
        message = str(error.exception)
        self.assertNotIn("ThisCouldBeAnAlphanumericSecret", message)
        self.assertIn("missing or empty", message)
        self.assertIn(".env next to manage.py", message)
        self.assertIn("restart", message)

    def test_blank_reference_or_empty_value_is_reported_as_configuration_error(self):
        for reference, variables in (("", {}), ("TEST_MAIL_SECRET", {"TEST_MAIL_SECRET": ""}), ("TEST_MAIL_SECRET", {"TEST_MAIL_SECRET": "  "})):
            with self.subTest(reference=reference), patch.dict(os.environ, variables, clear=True), self.assertRaises(MailboxCredentialError):
                credential(reference)

    def test_graph_uses_resolved_secret_value_in_token_request(self):
        secret = "test-only.secret-with=punctuation"
        config = SimpleNamespace(graph_secret_reference="TEST_GRAPH_SECRET", graph_tenant_id="tenant", graph_client_id="client", email_address="intake@example.com")
        with patch.dict(os.environ, {"TEST_GRAPH_SECRET": secret}), patch("core.mailbox.httpx.Client") as create:
            create.return_value.headers = {}
            create.return_value.post.return_value = httpx.Response(200, json={"access_token": "test-token"}, request=httpx.Request("POST", "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"))
            mailbox = GraphMailbox(config)
            self.assertEqual(create.return_value.post.call_args.kwargs["data"]["client_secret"], secret)
            self.assertEqual(mailbox.client.headers["Authorization"], "Bearer test-token")
            mailbox.close()

    def test_missing_credential_fails_before_opening_graph_or_imap_connections(self):
        config = SimpleNamespace(graph_secret_reference="MISSING_TEST_SECRET", credential_reference="MISSING_TEST_SECRET")
        with patch.dict(os.environ, {}, clear=True), patch("core.mailbox.httpx.Client") as graph, patch("core.mailbox.imaplib.IMAP4_SSL") as imap:
            with self.assertRaises(MailboxCredentialError):
                GraphMailbox(config)
            with self.assertRaises(MailboxCredentialError):
                IMAPMailbox(config).poll()
            graph.assert_not_called()
            imap.assert_not_called()


class MailboxCredentialValidationTests(TestCase):
    def test_admin_model_form_rejects_secret_values_and_accepts_environment_references(self):
        fields = ["name", "email_address", "transport", "imap_host", "credential_reference", "graph_tenant_id", "graph_client_id", "graph_secret_reference"]
        form_class = modelform_factory(MailboxConfiguration, fields=fields)
        for transport, reference_field in ((MailboxConfiguration.Transport.GRAPH, "graph_secret_reference"), (MailboxConfiguration.Transport.IMAP, "credential_reference")):
            data = {"name": "Test", "email_address": "intake@example.com", "transport": transport, "imap_host": "imap.example.com", "graph_tenant_id": "tenant", "graph_client_id": "client", reference_field: "example-secret.value"}
            with self.subTest(transport=transport):
                form = form_class(data=data)
                self.assertFalse(form.is_valid())
                self.assertIn(reference_field, form.errors)
                self.assertNotIn(data[reference_field], str(form.errors))
                data[reference_field] = "ENDORSEMENT_GRAPH_CLIENT_SECRET" if transport == MailboxConfiguration.Transport.GRAPH else "ENDORSEMENT_IMAP_PASSWORD"
                valid = form_class(data=data)
                self.assertTrue(valid.is_valid(), valid.errors)

    def test_polling_reports_safe_warning_and_resolves_fixed_reference_on_next_cycle(self):
        submitted = "example-secret.value-with-punctuation"
        config = MailboxConfiguration.objects.create(name="Misconfigured Graph", email_address="intake@example.com", transport=MailboxConfiguration.Transport.GRAPH, graph_tenant_id="tenant", graph_client_id="client", graph_secret_reference=submitted, is_active=True)
        with patch("core.mailbox.httpx.Client") as create, self.assertLogs("core.email_polling", level="WARNING") as logs:
            result = poll_correction_mailboxes()
        create.assert_not_called()
        self.assertEqual(result["failed"], 1)
        config.refresh_from_db()
        self.assertNotIn(submitted, config.last_error)
        self.assertIn("ENDORSEMENT_GRAPH_CLIENT_SECRET", config.last_error)
        self.assertNotIn(submitted, "\n".join(logs.output))
        self.assertNotIn("Traceback", "\n".join(logs.output))

        config.graph_secret_reference = "TEST_GRAPH_SECRET"
        config.save()
        with patch.dict(os.environ, {"TEST_GRAPH_SECRET": "test-secret"}), patch("core.mailbox.httpx.Client") as graph:
            graph.return_value.headers = {}
            graph.return_value.post.return_value = httpx.Response(200, json={"access_token": "test-token"}, request=httpx.Request("POST", "https://login.microsoftonline.com/tenant/oauth2/v2.0/token"))
            delta = "https://graph.microsoft.com/v1.0/users/intake@example.com/mailFolders/INBOX/messages/delta"
            graph.return_value.get.return_value = httpx.Response(200, json={"value": [], "@odata.deltaLink": delta}, request=httpx.Request("GET", delta))
            recovered = poll_correction_mailboxes()
        self.assertEqual(recovered["failed"], 0)
        graph.return_value.close.assert_called_once()
        config.refresh_from_db()
        self.assertEqual(config.last_error, "")
        self.assertIsNotNone(config.last_sync_at)
