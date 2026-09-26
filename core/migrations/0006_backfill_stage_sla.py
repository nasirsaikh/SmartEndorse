from django.db import migrations


def backfill_stage_sla(apps, schema_editor):
    Policy = apps.get_model("core", "Policy")
    for policy in Policy.objects.all().iterator():
        updates = {}
        if not policy.intake_sla_id and policy.client_query_sla_id:
            updates["intake_sla_id"] = policy.client_query_sla_id
        if not policy.validation_sla_id and policy.insurer_sla_id:
            updates["validation_sla_id"] = policy.insurer_sla_id
        if updates:
            Policy.objects.filter(pk=policy.pk).update(**updates)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_staged_workflow_profile_tpa"),
    ]

    operations = [
        migrations.RunPython(backfill_stage_sla, migrations.RunPython.noop),
    ]
