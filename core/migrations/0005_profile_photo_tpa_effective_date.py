from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0004_ai_extraction_training"),
    ]

    operations = [
        migrations.AddField(
            model_name="userprofile",
            name="photo",
            field=models.ImageField(blank=True, null=True, upload_to="profiles/%Y/%m/"),
        ),
        migrations.AddField(
            model_name="endorsementitem",
            name="tpa_effective_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
