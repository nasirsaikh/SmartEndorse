from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import EndorsementRequest, Organization, PlatformConfiguration, WorkflowEvent


class Command(BaseCommand):
    help = "Run SLA breach detection and escalation notifications. Schedule every 10-15 minutes."

    def handle(self, *args, **options):
        config, _ = PlatformConfiguration.objects.get_or_create(name="Default")
        if not config.enable_sla_escalations:
            self.stdout.write("SLA escalation is disabled in Admin.")
            return

        open_requests = EndorsementRequest.objects.exclude(
            status__in=[
                EndorsementRequest.Status.COMPLETED,
                EndorsementRequest.Status.CANCELLED,
                EndorsementRequest.Status.REJECTED,
            ]
        ).filter(current_sla_due_at__lt=timezone.now()).select_related(
            "policy",
            "policy__insurer",
            "policy__tpa",
            "requester_organization",
        )

        sent = 0
        for endorsement in open_requests:
            if WorkflowEvent.objects.filter(
                request=endorsement,
                event_type="SLA_BREACH",
                payload__sla_due_at=endorsement.current_sla_due_at.isoformat(),
            ).exists():
                continue

            WorkflowEvent.objects.create(
                request=endorsement,
                event_type="SLA_BREACH",
                from_status=endorsement.status,
                to_status=endorsement.status,
                description="SLA target breached.",
                payload={"sla_due_at": endorsement.current_sla_due_at.isoformat()},
            )

            recipients = set()
            recipients.add(endorsement.policy.insurer.notification_email)
            if endorsement.policy.tpa_id:
                recipients.add(endorsement.policy.tpa.notification_email)
            recipients.add(endorsement.requester_organization.notification_email)
            recipients.discard("")

            if config.enable_email_notifications and recipients:
                send_mail(
                    subject=f"SLA breached - {endorsement.reference}",
                    message=(
                        f"Endorsement {endorsement.reference} breached its SLA at "
                        f"{endorsement.current_sla_due_at:%Y-%m-%d %H:%M}. "
                        f"Current status: {endorsement.get_status_display()}."
                    ),
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=sorted(recipients),
                    fail_silently=True,
                )
            sent += 1

        self.stdout.write(self.style.SUCCESS(f"Processed {sent} new SLA breach escalation(s)."))
