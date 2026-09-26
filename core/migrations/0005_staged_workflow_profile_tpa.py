from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_ai_extraction_training"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="photo",
            field=models.ImageField(blank=True, null=True, upload_to="profiles/%Y/%m/"),
        ),
        migrations.AddField(
            model_name="policy",
            name="auto_approval_rules",
            field=models.JSONField(blank=True, default=dict, help_text="Optional insurer-controlled rules. Supported keys include min_validation_score, max_abs_premium_impact and block_on_risk_flags."),
        ),
        migrations.AddField(
            model_name="policy",
            name="intake_sla",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="intake_policies", to="core.slaprofile"),
        ),
        migrations.AddField(
            model_name="policy",
            name="validation_sla",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="validation_policies", to="core.slaprofile"),
        ),
        migrations.AddField(
            model_name="endorsementitem",
            name="tpa_effective_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="endorsementitem",
            name="tpa_status",
            field=models.CharField(choices=[("PENDING", "Pending"), ("APPROVED", "Approved"), ("REJECTED", "Rejected"), ("QUERY", "Query raised")], default="PENDING", max_length=20),
        ),
        migrations.AddField(
            model_name="endorsementitem",
            name="tpa_decision_comment",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="endorsementitem",
            name="tpa_decided_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="endorsementitem",
            name="tpa_decided_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="tpa_decided_endorsement_items", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AlterField(
            model_name="policy",
            name="auto_stp",
            field=models.BooleanField(default=True, help_text="When enabled, requests that pass validation and auto-approval rules can skip manual insurer approval."),
        ),
    ]
