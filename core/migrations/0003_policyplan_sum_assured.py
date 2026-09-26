from django.db import migrations, models
from django.core.validators import MinValueValidator
from decimal import Decimal


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_workflow_v2"),
    ]

    operations = [
        migrations.AddField(
            model_name="policyplan",
            name="sum_assured",
            field=models.DecimalField(
                blank=True,
                decimal_places=3,
                help_text="Optional plan-level sum assured. When configured, endorsement member sum assured is derived from the selected plan.",
                max_digits=14,
                null=True,
                validators=[MinValueValidator(Decimal("0"))],
            ),
        ),
    ]
