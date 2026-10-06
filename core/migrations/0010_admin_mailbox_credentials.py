from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0009_mailbox_credential_references")]

    operations = [
        migrations.RenameField(model_name="mailboxconfiguration", old_name="credential_reference", new_name="imap_password"),
        migrations.RenameField(model_name="mailboxconfiguration", old_name="graph_secret_reference", new_name="graph_client_secret"),
    ]
