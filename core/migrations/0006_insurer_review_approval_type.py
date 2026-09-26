from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0005_profile_photo_tpa_effective_date"),
    ]

    operations = [
        migrations.AlterField(
            model_name="endorsementapproval",
            name="approval_type",
            field=models.CharField(
                choices=[
                    ("EXISTING_MEMBER", "Existing member exception"),
                    ("INSURER_REVIEW", "Insurer manual review"),
                    ("TPA_AMOUNT_CHANGE", "TPA amount change"),
                ],
                max_length=30,
            ),
        ),
    ]
