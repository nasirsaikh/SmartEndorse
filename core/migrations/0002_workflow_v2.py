from decimal import Decimal
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile", name="portal_theme",
            field=models.CharField(choices=[
                ("default", "Default"), ("brite", "Brite"), ("cerulean", "Cerulean"), ("cosmo", "Cosmo"),
                ("cyborg", "Cyborg"), ("darkly", "Darkly"), ("flatly", "Flatly"), ("journal", "Journal"),
                ("litera", "Litera"), ("lumen", "Lumen"), ("lux", "Lux"), ("materia", "Materia"),
                ("minty", "Minty"), ("morph", "Morph"), ("pulse", "Pulse"), ("quartz", "Quartz"),
                ("sandstone", "Sandstone"), ("simplex", "Simplex"), ("sketchy", "Sketchy"), ("slate", "Slate"),
                ("solar", "Solar"), ("spacelab", "Spacelab"), ("superhero", "Superhero"), ("united", "United"),
                ("vapor", "Vapor"), ("yeti", "Yiti / Yeti")
            ], default="default", max_length=30),
        ),
        migrations.AddField(
            model_name="userprofile", name="color_mode",
            field=models.CharField(choices=[("light", "Light"), ("dark", "Dark"), ("auto", "System")], default="auto", max_length=10),
        ),
        migrations.AddField(
            model_name="platformconfiguration", name="enable_portal_notifications",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="platformconfiguration", name="endorsement_expiry_cutoff_days",
            field=models.PositiveSmallIntegerField(default=30, help_text="Block new/processing endorsements when policy has fewer than this many days remaining. Policy-level override takes precedence."),
        ),
        migrations.AddField(
            model_name="platformconfiguration", name="tpa_amount_approval_party",
            field=models.CharField(choices=[("INSURER", "Insurance company"), ("CLIENT", "Client")], default="INSURER", help_text="Who must approve a TPA premium/refund amount changed from the system-calculated amount.", max_length=12),
        ),
        migrations.AddField(
            model_name="policy", name="endorsement_expiry_cutoff_days",
            field=models.PositiveSmallIntegerField(blank=True, help_text="Optional override of global cutoff. Example 30 or 60. Zero allows processing until policy expiry.", null=True),
        ),
        migrations.AlterField(
            model_name="slaprofile", name="stage",
            field=models.CharField(choices=[("CLIENT_RESPONSE", "Client response"), ("INSURER_REVIEW", "Insurer review"), ("TPA_PROCESSING", "TPA processing"), ("QUERY_RESPONSE", "Query response"), ("CORE_PROCESSING", "Core-system processing"), ("APPROVAL", "Approval")], max_length=30),
        ),
        migrations.AlterField(
            model_name="endorsementrequest", name="status",
            field=models.CharField(choices=[
                ("DRAFT", "Draft"), ("VALIDATING", "Validating"), ("NEEDS_INFO", "Needs information"),
                ("SUBMITTED", "Submitted"), ("PENDING_INSURER_APPROVAL", "Pending insurer approval"),
                ("PENDING_AMOUNT_APPROVAL", "Pending amount approval"), ("AUTO_APPROVED", "Auto approved"),
                ("SENT_TO_TPA", "Sent to TPA"), ("TPA_IN_PROGRESS", "TPA in progress"), ("TPA_QUERY", "TPA query"),
                ("CORE_DISPATCHED", "Dispatched to insurer core"), ("COMPLETED", "Completed"), ("REJECTED", "Rejected"),
                ("CANCELLED", "Cancelled"), ("FAILED", "Automation failed")
            ], default="DRAFT", max_length=30),
        ),
        migrations.AddField(
            model_name="endorsementitem", name="validation_status",
            field=models.CharField(choices=[("PENDING", "Pending validation"), ("VALID", "Correct"), ("ERROR", "Error"), ("EXISTING", "Existing record"), ("APPROVAL_REQUIRED", "Approval required")], default="PENDING", max_length=24),
        ),
        migrations.AddField(model_name="endorsementitem", name="is_existing_record", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="endorsementitem", name="requires_insurer_approval", field=models.BooleanField(default=False)),
        migrations.AddField(model_name="endorsementitem", name="resolution_data", field=models.JSONField(blank=True, default=dict)),
        migrations.AddField(model_name="endorsementitem", name="card_number", field=models.CharField(blank=True, max_length=100)),
        migrations.AddField(model_name="endorsementitem", name="tpa_premium_amount", field=models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True)),
        migrations.AddField(model_name="attachment", name="is_supplemental", field=models.BooleanField(default=False)),
        migrations.CreateModel(
            name="RecoveryUpload",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("reference", models.CharField(editable=False, max_length=32, unique=True)),
                ("file", models.FileField(upload_to="recovery/%Y/%m/")),
                ("original_name", models.CharField(max_length=255)),
                ("kind", models.CharField(choices=[("EXCEL", "Excel / CSV"), ("PDF", "PDF"), ("IMAGE", "Image"), ("OTHER", "Other")], default="OTHER", max_length=20)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("PROCESSED", "Processed"), ("PARTIAL", "Partially matched"), ("FAILED", "Failed")], default="PENDING", max_length=20)),
                ("resolved_count", models.PositiveIntegerField(default=0)),
                ("ambiguous_count", models.PositiveIntegerField(default=0)),
                ("unmatched_count", models.PositiveIntegerField(default=0)),
                ("processing_error", models.TextField(blank=True)),
                ("extracted_payload", models.JSONField(blank=True, default=dict)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recovery_uploads", to="core.organization")),
                ("uploaded_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="recovery_uploads", to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name="PortalNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("title", models.CharField(max_length=180)),
                ("message", models.TextField()),
                ("level", models.CharField(choices=[("INFO", "Info"), ("SUCCESS", "Success"), ("WARNING", "Warning"), ("DANGER", "Danger")], default="INFO", max_length=12)),
                ("link", models.CharField(blank=True, max_length=500)),
                ("is_read", models.BooleanField(default=False)),
                ("browser_notified", models.BooleanField(default=False)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="portal_notifications", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="EndorsementApproval",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("approval_type", models.CharField(choices=[("EXISTING_MEMBER", "Existing member exception"), ("TPA_AMOUNT_CHANGE", "TPA amount change")], max_length=30)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("APPROVED", "Approved"), ("REJECTED", "Rejected")], default="PENDING", max_length=20)),
                ("reason", models.TextField(blank=True)),
                ("old_amount", models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True)),
                ("new_amount", models.DecimalField(blank=True, decimal_places=3, max_digits=14, null=True)),
                ("decided_at", models.DateTimeField(blank=True, null=True)),
                ("decision_comment", models.TextField(blank=True)),
                ("assigned_organization", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="endorsement_approvals", to="core.organization")),
                ("decided_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="decided_endorsement_approvals", to=settings.AUTH_USER_MODEL)),
                ("item", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="approvals", to="core.endorsementitem")),
                ("request", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="approvals", to="core.endorsementrequest")),
                ("requested_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="requested_endorsement_approvals", to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
