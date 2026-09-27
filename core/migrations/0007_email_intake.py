from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0006_insurer_review_approval_type"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EmailIntakeMailbox",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=120, unique=True)),
                ("email_address", models.EmailField(max_length=254)),
                ("provider", models.CharField(choices=[("MICROSOFT_GRAPH", "Microsoft 365 / Graph"), ("IMAP", "IMAP")], default="MICROSOFT_GRAPH", max_length=30)),
                ("is_active", models.BooleanField(default=True)),
                ("auto_submit", models.BooleanField(default=True, help_text="When enabled, successfully extracted email endorsements are immediately validated and routed into the normal workflow.")),
                ("mark_as_read", models.BooleanField(default=True)),
                ("folder", models.CharField(default="Inbox", max_length=120)),
                ("poll_interval_seconds", models.PositiveIntegerField(default=60)),
                ("max_messages_per_poll", models.PositiveSmallIntegerField(default=25)),
                ("imap_host", models.CharField(blank=True, max_length=255)),
                ("imap_port", models.PositiveIntegerField(default=993)),
                ("imap_username", models.CharField(blank=True, max_length=255)),
                ("imap_password", models.TextField(blank=True)),
                ("imap_use_ssl", models.BooleanField(default=True)),
                ("graph_tenant_id", models.CharField(blank=True, max_length=120)),
                ("graph_client_id", models.CharField(blank=True, max_length=120)),
                ("graph_client_secret", models.TextField(blank=True)),
                ("last_polled_at", models.DateTimeField(blank=True, null=True)),
                ("default_requester", models.ForeignKey(blank=True, help_text="Fallback requester only when no sender route or matching portal user is available.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="default_email_intake_mailboxes", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="EmailIntakeRoute",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("sender_pattern", models.CharField(help_text="Exact sender email or domain pattern such as hr@client.com or @client.com.", max_length=255)),
                ("default_endorsement_type", models.CharField(blank=True, choices=[("", "Detect from email"), ("ADDITION", "Addition"), ("DELETION", "Deletion")], max_length=20)),
                ("priority", models.PositiveSmallIntegerField(default=100)),
                ("is_active", models.BooleanField(default=True)),
                ("default_policy", models.ForeignKey(blank=True, help_text="Optional fallback policy when the email does not state a policy number.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="email_intake_routes", to="core.policy")),
                ("mailbox", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="routes", to="core.emailintakemailbox")),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="email_intake_routes", to="core.organization")),
                ("requester", models.ForeignKey(blank=True, help_text="Optional fixed requester. If blank, SmartEndorse first matches the sender to an active portal user in this organization.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="email_intake_routes", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-priority", "id")},
        ),
        migrations.CreateModel(
            name="EmailIntakeMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("provider_message_id", models.CharField(max_length=500)),
                ("internet_message_id", models.CharField(blank=True, max_length=500)),
                ("sender_email", models.EmailField(blank=True, max_length=254)),
                ("sender_name", models.CharField(blank=True, max_length=255)),
                ("subject", models.CharField(blank=True, max_length=500)),
                ("body_text", models.TextField(blank=True)),
                ("received_at", models.DateTimeField(blank=True, null=True)),
                ("status", models.CharField(choices=[("RECEIVED", "Received"), ("PROCESSING", "Processing"), ("PROCESSED", "Processed"), ("NEEDS_REVIEW", "Needs review"), ("FAILED", "Failed"), ("IGNORED", "Ignored")], default="RECEIVED", max_length=20)),
                ("attachment_count", models.PositiveIntegerField(default=0)),
                ("error", models.TextField(blank=True)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("mailbox", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="messages", to="core.emailintakemailbox")),
                ("request", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="email_intake_messages", to="core.endorsementrequest")),
            ],
            options={"ordering": ("-received_at", "-created_at")},
        ),
        migrations.AddConstraint(
            model_name="emailintakemessage",
            constraint=models.UniqueConstraint(fields=("mailbox", "provider_message_id"), name="uq_email_intake_provider_message"),
        ),
    ]
