import os
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from django.contrib.auth.models import User
from django.core.mail import get_connection
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.forms.models import model_to_dict
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from .admin import EmailIntakeMailboxAdminForm, MailboxConfigurationAdminForm
from .credentials import MailboxCredentialError, credential
from .email_polling import poll_correction_mailboxes
from .mailbox import GraphMailbox, IMAPMailbox, smtp_connection
from .models import EmailIntakeMailbox, MailboxConfiguration


class CredentialResolutionTests(SimpleTestCase):
    def test_admin_secrets_are_literal_values_including_environment_like_names(self):
        for value in ('test.secret$-with=punctuation', 'ENVIRONMENT_LIKE_PASSWORD', ' leading-and-trailing-spaces '):
            with self.subTest(value=value), patch('os.getenv', side_effect=AssertionError('Mailbox secrets must come from Admin')):
                self.assertEqual(credential(value), value)

    def test_blank_secret_is_reported_without_environment_instructions(self):
        for value in ('', '  ', None):
            with self.subTest(value=value), self.assertRaises(MailboxCredentialError) as error:
                credential(value, label='Graph client secret')
            self.assertIn('Admin > Mailbox configurations', str(error.exception))
            self.assertNotIn('environment', str(error.exception))
            self.assertNotIn('.env', str(error.exception))

    def test_graph_uses_admin_secret_value_and_ignores_environment(self):
        secret = 'ADMIN_SECRET_VALUE'
        config = SimpleNamespace(graph_client_secret=secret, graph_tenant_id='tenant', graph_client_id='client', email_address='intake@example.com')
        with patch.dict(os.environ, {secret: 'wrong-env-secret'}), patch('core.mailbox.httpx.Client') as create:
            create.return_value.headers = {}
            create.return_value.post.return_value = httpx.Response(200, json={'access_token': 'test-token'}, request=httpx.Request('POST', 'https://login.microsoftonline.com/tenant/oauth2/v2.0/token'))
            mailbox = GraphMailbox(config)
            self.assertEqual(create.return_value.post.call_args.kwargs['data']['client_secret'], secret)
            self.assertEqual(mailbox.client.headers['Authorization'], 'Bearer test-token')
            mailbox.close()

    def test_imap_logs_in_with_admin_password(self):
        config = SimpleNamespace(imap_password='ADMIN_IMAP_PASSWORD', imap_host='imap.example.com', imap_port=993, imap_username='intake@example.com', email_address='intake@example.com', folder='INBOX', use_oauth=False, cursor={})
        with patch.dict(os.environ, {'ADMIN_IMAP_PASSWORD': 'wrong-env-password'}), patch('core.mailbox.imaplib.IMAP4_SSL') as create:
            client = create.return_value.__enter__.return_value
            client.select.return_value = ('OK', [])
            client.response.return_value = ('UIDVALIDITY', [b'100'])
            client.uid.return_value = ('OK', [b''])
            self.assertEqual(IMAPMailbox(config).poll(), [])
            client.login.assert_called_once_with(config.imap_username, config.imap_password)

    def test_missing_credential_fails_before_opening_graph_or_imap_connections(self):
        config = SimpleNamespace(graph_client_secret='', imap_password='')
        with patch('core.mailbox.httpx.Client') as graph, patch('core.mailbox.imaplib.IMAP4_SSL') as imap:
            with self.assertRaises(MailboxCredentialError):
                GraphMailbox(config)
            with self.assertRaises(MailboxCredentialError):
                IMAPMailbox(config).poll()
            graph.assert_not_called()
            imap.assert_not_called()

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend', EMAIL_HOST='wrong.example.com', EMAIL_HOST_USER='wrong-user', EMAIL_HOST_PASSWORD='wrong-password', EMAIL_USE_TLS=False)
    def test_smtp_reply_connection_uses_saved_admin_settings(self):
        config = SimpleNamespace(reply_backend='SMTP', smtp_host='smtp.example.com', smtp_port=587, smtp_username='admin-smtp-user', smtp_password='admin.smtp-secret', smtp_use_tls=True, smtp_use_ssl=False, smtp_timeout=12)
        with patch('django.core.mail.backends.smtp.smtplib.SMTP') as transport:
            backend = smtp_connection(config)
            try:
                self.assertTrue(backend.open())
                self.assertEqual(transport.call_args.args[:2], (config.smtp_host, config.smtp_port))
                self.assertEqual(transport.call_args.kwargs['timeout'], 12)
                transport.return_value.starttls.assert_called_once()
                transport.return_value.login.assert_called_once_with(config.smtp_username, config.smtp_password)
            finally:
                backend.close()


class MailboxCredentialValidationTests(TestCase):
    def setUp(self):
        self.mailbox = MailboxConfiguration.objects.create(name='Admin mailbox', email_address='intake@example.com', transport='GRAPH', graph_tenant_id='tenant', graph_client_id='client', graph_client_secret='old.graph-secret', imap_host='imap.example.com', imap_password='old.imap-password', smtp_host='smtp.example.com', smtp_username='smtp-user', smtp_password='old.smtp-password')

    def data(self, **updates):
        data = model_to_dict(self.mailbox)
        data['trusted_authserv_ids'] = json.dumps(self.mailbox.trusted_authserv_ids)
        data.update({field: '' for field in MailboxConfigurationAdminForm.secret_fields})
        data.update(updates)
        return data

    def test_secret_widgets_do_not_render_stored_values_and_blank_save_preserves_them(self):
        initial = {field: getattr(self.mailbox, field) for field in MailboxConfigurationAdminForm.secret_fields}
        display = MailboxConfigurationAdminForm(instance=self.mailbox)
        for field, value in initial.items():
            self.assertNotIn(value, str(display[field]))
            self.assertIn('type="password"', str(display[field]))
        form = MailboxConfigurationAdminForm(data=self.data(name='Edited name'), instance=self.mailbox)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.mailbox.refresh_from_db()
        self.assertEqual(self.mailbox.name, 'Edited name')
        for field, value in initial.items():
            self.assertEqual(getattr(self.mailbox, field), value)

    def test_secret_can_be_replaced_with_punctuation_or_cleared_explicitly(self):
        value = ' replacement.secret$~with=punctuation '
        form = MailboxConfigurationAdminForm(data=self.data(graph_client_secret=value), instance=self.mailbox)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.mailbox.refresh_from_db()
        self.assertEqual(self.mailbox.graph_client_secret, value)
        form = MailboxConfigurationAdminForm(data=self.data(clear_graph_client_secret=True), instance=self.mailbox)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.mailbox.refresh_from_db()
        self.assertEqual(self.mailbox.graph_client_secret, '')

    def test_active_mailbox_cannot_clear_required_secret(self):
        form = MailboxConfigurationAdminForm(data=self.data(is_active=True, require_sender_authentication=False, clear_graph_client_secret=True), instance=self.mailbox)
        self.assertFalse(form.is_valid())
        self.assertIn('graph_client_secret', form.errors)

    def test_clear_and_replace_together_is_rejected_without_echoing_secret(self):
        value = 'test-only.new-secret'
        form = MailboxConfigurationAdminForm(data=self.data(graph_client_secret=value, clear_graph_client_secret=True), instance=self.mailbox)
        self.assertFalse(form.is_valid())
        self.assertIn('graph_client_secret', form.errors)
        self.assertNotIn(value, str(form.errors))

    def test_route_mailbox_form_also_hides_and_preserves_saved_secrets(self):
        box = EmailIntakeMailbox.objects.create(name='Route mailbox', email_address='route@example.com', imap_password='route.imap-secret', graph_client_secret='route.graph-secret')
        display = EmailIntakeMailboxAdminForm(instance=box)
        self.assertNotIn(box.imap_password, str(display['imap_password']))
        self.assertNotIn(box.graph_client_secret, str(display['graph_client_secret']))
        data = model_to_dict(box)
        data.update(imap_password='', graph_client_secret='', name='Edited route mailbox')
        form = EmailIntakeMailboxAdminForm(data=data, instance=box)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        box.refresh_from_db()
        self.assertEqual(box.imap_password, 'route.imap-secret')
        self.assertEqual(box.graph_client_secret, 'route.graph-secret')

    def test_actual_admin_change_page_exposes_direct_password_fields(self):
        user = User.objects.create_user('secret-admin', is_staff=True, is_superuser=True)
        self.client.force_login(user)
        response = self.client.get(f'/admin/core/mailboxconfiguration/{self.mailbox.pk}/change/')
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for field in MailboxConfigurationAdminForm.secret_fields:
            self.assertIn(f'name="{field}"', html)
            self.assertNotIn(getattr(self.mailbox, field), html)
        self.assertNotIn('name="graph_secret_reference"', html)
        self.assertNotIn('name="credential_reference"', html)

    def test_polling_recovers_using_new_admin_secret_without_worker_restart(self):
        self.mailbox.graph_client_secret = ''
        self.mailbox.is_active = True
        self.mailbox.save()
        with patch('core.mailbox.httpx.Client') as create, self.assertLogs('core.email_polling', level='WARNING') as logs:
            self.assertEqual(poll_correction_mailboxes()['failed'], 1)
        create.assert_not_called()
        self.assertNotIn('environment', '\n'.join(logs.output))
        self.assertNotIn('Traceback', '\n'.join(logs.output))
        self.mailbox.graph_client_secret = 'test-only.new-secret'
        self.mailbox.save()
        with patch('core.mailbox.httpx.Client') as create:
            create.return_value.headers = {}
            create.return_value.post.return_value = httpx.Response(200, json={'access_token': 'test-token'}, request=httpx.Request('POST', 'https://login.microsoftonline.com/tenant/oauth2/v2.0/token'))
            delta = 'https://graph.microsoft.com/v1.0/users/intake@example.com/mailFolders/INBOX/messages/delta'
            create.return_value.get.return_value = httpx.Response(200, json={'value': [], '@odata.deltaLink': delta}, request=httpx.Request('GET', delta))
            self.assertEqual(poll_correction_mailboxes()['failed'], 0)
            self.assertEqual(create.return_value.post.call_args.kwargs['data']['client_secret'], self.mailbox.graph_client_secret)
        self.mailbox.refresh_from_db()
        self.assertEqual(self.mailbox.last_error, '')
        self.assertIsNotNone(self.mailbox.last_sync_at)


class AdminCredentialMigrationTests(TransactionTestCase):
    def test_renaming_reference_fields_preserves_mailbox_values_cursors_and_emails(self):
        executor = MigrationExecutor(connection)
        executor.migrate([('core', '0009_mailbox_credential_references')])
        try:
            old = executor.loader.project_state([('core', '0009_mailbox_credential_references')]).apps
            box = old.get_model('core', 'MailboxConfiguration').objects.create(name='Migration mailbox', email_address='intake@example.com', credential_reference='old.imap-secret', graph_secret_reference='old.graph-secret', cursor={'last_uid': 9})
            source = old.get_model('core', 'InboundEmail').objects.create(mailbox_id=box.pk, reference='EML-MIGRATION-TEST', message_uid='100:9', sender='hr@example.com', subject='Retained message')
            executor = MigrationExecutor(connection)
            executor.migrate([('core', '0011_admin_mailbox_delivery')])
            new = executor.loader.project_state([('core', '0011_admin_mailbox_delivery')]).apps
            migrated = new.get_model('core', 'MailboxConfiguration').objects.get(pk=box.pk)
            self.assertEqual(migrated.imap_password, 'old.imap-secret')
            self.assertEqual(migrated.graph_client_secret, 'old.graph-secret')
            self.assertEqual(migrated.cursor, {'last_uid': 9})
            self.assertEqual(new.get_model('core', 'InboundEmail').objects.get(pk=source.pk).mailbox_id, box.pk)
        finally:
            MigrationExecutor(connection).migrate([('core', '0011_admin_mailbox_delivery')])
