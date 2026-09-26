from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_backfill_stage_sla"),
    ]

    operations = [
        migrations.AlterField(
            model_name="endorsementrequest",
            name="status",
            field=models.CharField(
                choices=[
                    ("DRAFT", "Draft"),
                    ("VALIDATING", "Validating"),
                    ("NEEDS_INFO", "Needs information"),
                    ("SUBMITTED", "Submitted"),
                    ("PENDING_INSURER_APPROVAL", "Pending insurer approval"),
                    ("PENDING_AMOUNT_APPROVAL", "Pending amount approval"),
                    ("AUTO_APPROVED", "Auto approved"),
                    ("SENT_TO_TPA", "Sent to TPA"),
                    ("TPA_IN_PROGRESS", "TPA in progress"),
                    ("TPA_QUERY", "TPA query"),
                    ("READY_FOR_CORE", "Ready for insurer processing"),
                    ("CORE_DISPATCHED", "Dispatched to insurer core"),
                    ("COMPLETED", "Completed"),
                    ("REJECTED", "Rejected"),
                    ("CANCELLED", "Cancelled"),
                    ("FAILED", "Automation failed"),
                ],
                default="DRAFT",
                max_length=30,
            ),
        ),
        migrations.AlterField(
            model_name="endorsementapproval",
            name="approval_type",
            field=models.CharField(
                choices=[
                    ("INSURER_REVIEW", "Insurer manual review"),
                    ("EXISTING_MEMBER", "Existing member exception"),
                    ("TPA_AMOUNT_CHANGE", "TPA amount change"),
                ],
                max_length=30,
            ),
        ),
    ]
