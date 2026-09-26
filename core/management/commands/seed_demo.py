from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import (
    AIProviderConfig, EndorsementItem, EndorsementQuery, EndorsementRequest,
    IntegrationEndpoint, Organization, PlatformConfiguration, Policy, PolicyAccess,
    PolicyMember, PolicyPlan, SLAProfile, UserProfile, WorkflowEvent,
)


class Command(BaseCommand):
    help = "Create realistic SmartEndorse demo data. Safe to run repeatedly."

    def handle(self, *args, **options):
        PlatformConfiguration.objects.get_or_create(
            name="Default",
            defaults={
                "auto_stp_enabled": True,
                "default_currency": "OMR",
                "maximum_upload_mb": 10,
                "support_email": "smartendorse@insurer.example",
            },
        )

        insurer = self.org("TAOI", "Takaful Oman Insurance", Organization.Type.INSURER, "endorsements@insurer.example")
        client = self.org("ACME", "Acme Manufacturing Oman", Organization.Type.CLIENT, "hr@acme.example")
        tpa = self.org("NEXTCARE", "NextCare Demo TPA", Organization.Type.TPA, "endorsements@nextcare.example")
        broker = self.org("GB001", "Gulf Insurance Brokers", Organization.Type.BROKER, "service@gulfbroker.example")
        agent = self.org("AG001", "Corporate Benefits Agency", Organization.Type.AGENT, "ops@agency.example")

        sla_insurer, _ = SLAProfile.objects.get_or_create(
            name="Insurer STP oversight - 4h",
            stage=SLAProfile.Stage.INSURER_REVIEW,
            defaults={"target_hours": 4, "warning_hours": 1},
        )
        sla_tpa, _ = SLAProfile.objects.get_or_create(
            name="TPA fulfilment - 24h",
            stage=SLAProfile.Stage.TPA_PROCESSING,
            defaults={"target_hours": 24, "warning_hours": 4},
        )
        sla_query, _ = SLAProfile.objects.get_or_create(
            name="Client query response - 8h",
            stage=SLAProfile.Stage.QUERY_RESPONSE,
            defaults={"target_hours": 8, "warning_hours": 2},
        )

        medical, _ = Policy.objects.update_or_create(
            policy_number="MED-2026-001",
            defaults={
                "policy_name": "Acme Group Medical 2026",
                "product": Policy.Product.GROUP_MEDICAL,
                "insurer": insurer,
                "client": client,
                "tpa": tpa,
                "broker_code": "GB001",
                "agent_code": "AG001",
                "effective_from": date(2026, 1, 1),
                "effective_to": date(2026, 12, 31),
                "currency": "OMR",
                "rating_method": Policy.RatingMethod.FLAT_ANNUAL,
                "rating_parameters": {"prorata_mode": "fixed_basis"},
                "required_fields_addition": ["full_name", "date_of_birth", "gender", "relationship", "plan"],
                "required_fields_deletion": ["member_no", "effective_date"],
                "day_count_basis": 365,
                "auto_stp": True,
                "insurer_sla": sla_insurer,
                "tpa_sla": sla_tpa,
                "client_query_sla": sla_query,
            },
        )
        silver, _ = PolicyPlan.objects.update_or_create(
            policy=medical, code="SILVER",
            defaults={"name": "Silver", "annual_rate": Decimal("85.000"), "relationship_rates": {"Spouse": "95.000", "Child": "70.000"}},
        )
        gold, _ = PolicyPlan.objects.update_or_create(
            policy=medical, code="GOLD",
            defaults={"name": "Gold", "annual_rate": Decimal("125.000"), "relationship_rates": {"Spouse": "140.000", "Child": "98.000"}},
        )
        PolicyPlan.objects.update_or_create(
            policy=medical, code="PLATINUM",
            defaults={"name": "Platinum", "annual_rate": Decimal("195.000"), "relationship_rates": {"Spouse": "215.000", "Child": "150.000"}},
        )

        life, _ = Policy.objects.update_or_create(
            policy_number="LIFE-2026-001",
            defaults={
                "policy_name": "Acme Group Life 2026",
                "product": Policy.Product.GROUP_LIFE,
                "insurer": insurer,
                "client": client,
                "tpa": None,
                "broker_code": "GB001",
                "effective_from": date(2026, 1, 1),
                "effective_to": date(2026, 12, 31),
                "currency": "OMR",
                "rating_method": Policy.RatingMethod.PER_MILLE_SUM_ASSURED,
                "rating_parameters": {"rate_per_mille": "1.25", "prorata_mode": "fixed_basis"},
                "required_fields_addition": ["full_name", "date_of_birth", "gender", "plan", "sum_assured"],
                "required_fields_deletion": ["member_no", "effective_date"],
                "day_count_basis": 365,
                "auto_stp": True,
                "insurer_sla": sla_insurer,
                "client_query_sla": sla_query,
            },
        )

        PolicyPlan.objects.update_or_create(
            policy=life, code="LIFE25",
            defaults={"name": "Life 25K", "annual_rate": Decimal("0.000"), "sum_assured": Decimal("25000.000")},
        )
        PolicyPlan.objects.update_or_create(
            policy=life, code="LIFE50",
            defaults={"name": "Life 50K", "annual_rate": Decimal("0.000"), "sum_assured": Decimal("50000.000")},
        )
        PolicyPlan.objects.update_or_create(
            policy=life, code="LIFE100",
            defaults={"name": "Life 100K", "annual_rate": Decimal("0.000"), "sum_assured": Decimal("100000.000")},
        )

        for policy in (medical, life):
            for org in (client, broker, agent):
                PolicyAccess.objects.update_or_create(
                    policy=policy,
                    organization=org,
                    defaults={"can_create": True, "can_view_premium": True, "can_view_members": True},
                )

        PolicyMember.objects.update_or_create(
            policy=medical, member_no="M0001",
            defaults={
                "employee_no": "E1001",
                "national_id": "DEMO1001",
                "full_name": "Aisha Al Harthi",
                "relationship": "Employee",
                "date_of_birth": date(1991, 3, 14),
                "gender": "Female",
                "plan": gold,
                "coverage_from": medical.effective_from,
                "coverage_to": medical.effective_to,
                "is_active": True,
            },
        )
        PolicyMember.objects.update_or_create(
            policy=medical, member_no="M0002",
            defaults={
                "employee_no": "E1002",
                "national_id": "DEMO1002",
                "full_name": "Omar Al Balushi",
                "relationship": "Employee",
                "date_of_birth": date(1987, 11, 2),
                "gender": "Male",
                "plan": silver,
                "coverage_from": medical.effective_from,
                "coverage_to": medical.effective_to,
                "is_active": True,
            },
        )
        PolicyMember.objects.update_or_create(
            policy=life, member_no="L0001",
            defaults={
                "employee_no": "E1001",
                "full_name": "Aisha Al Harthi",
                "relationship": "Employee",
                "date_of_birth": date(1991, 3, 14),
                "gender": "Female",
                "sum_assured": Decimal("25000.000"),
                "annual_salary": Decimal("12000.000"),
                "coverage_from": life.effective_from,
                "coverage_to": life.effective_to,
                "is_active": True,
            },
        )

        admin = self.user("smartadmin", "Smart", "Admin", "smartadmin@example.com", insurer, UserProfile.Role.SUPER_ADMIN, superuser=True)
        insurer_manager = self.user("insurer.manager", "Maha", "Manager", "maha@insurer.example", insurer, UserProfile.Role.INSURER_MANAGER)
        client_user = self.user("client.requester", "Amina", "HR", "amina@acme.example", client, UserProfile.Role.REQUESTER)
        broker_user = self.user("broker.user", "Salim", "Broker", "salim@gulfbroker.example", broker, UserProfile.Role.BROKER_USER)
        tpa_user = self.user("tpa.processor", "Riya", "Processor", "riya@nextcare.example", tpa, UserProfile.Role.TPA_PROCESSOR)
        self.user("agent.user", "Khalid", "Agent", "khalid@agency.example", agent, UserProfile.Role.AGENT)

        IntegrationEndpoint.objects.update_or_create(
            name="NextCare endorsement mailbox",
            defaults={
                "owner_type": IntegrationEndpoint.OwnerType.TPA,
                "organization": tpa,
                "product": Policy.Product.GROUP_MEDICAL,
                "transport": IntegrationEndpoint.Transport.EMAIL,
                "recipient_email": "endorsements@nextcare.example",
                "is_active": True,
            },
        )
        IntegrationEndpoint.objects.update_or_create(
            name="Insurer group life core queue",
            defaults={
                "owner_type": IntegrationEndpoint.OwnerType.INSURER_CORE,
                "organization": insurer,
                "product": Policy.Product.GROUP_LIFE,
                "transport": IntegrationEndpoint.Transport.EMAIL,
                "recipient_email": "grouplife.core@insurer.example",
                "is_active": True,
            },
        )

        AIProviderConfig.objects.update_or_create(
            name="Local Llama via Ollama",
            defaults={
                "provider": AIProviderConfig.Provider.OLLAMA,
                "model_name": "qwen2.5:7b",
                "base_url": "http://127.0.0.1:11434",
                "temperature": Decimal("0.00"),
                "is_active": True,
                "supports_vision": False,
            },
        )

        self.demo_request("END-DEMO-001", medical, client_user, client, EndorsementRequest.Type.ADDITION, EndorsementRequest.Status.SENT_TO_TPA, gold, "Fatma Al Riyami", Decimal("67.808"), tpa_user)
        self.demo_request("END-DEMO-002", medical, broker_user, broker, EndorsementRequest.Type.DELETION, EndorsementRequest.Status.COMPLETED, silver, "Omar Al Balushi", Decimal("-22.603"), tpa_user, completed=True, member_no="M0002")
        self.demo_request("END-DEMO-003", life, client_user, client, EndorsementRequest.Type.ADDITION, EndorsementRequest.Status.CORE_DISPATCHED, None, "Hassan Al Lawati", Decimal("18.836"), insurer_manager, sum_assured=Decimal("25000.000"))
        failed = self.demo_request("END-DEMO-004", medical, client_user, client, EndorsementRequest.Type.ADDITION, EndorsementRequest.Status.NEEDS_INFO, None, "Incomplete Member", Decimal("0.000"), client_user)
        failed.validation_errors = ["Item 4: Date Of Birth is required.", "Item 4: Plan is required."]
        failed.validation_score = Decimal("80.00")
        failed.current_sla_due_at = timezone.now() + timedelta(hours=6)
        failed.save(update_fields=["validation_errors", "validation_score", "current_sla_due_at", "updated_at"])

        queried = self.demo_request("END-DEMO-005", medical, client_user, client, EndorsementRequest.Type.ADDITION, EndorsementRequest.Status.TPA_QUERY, gold, "Noura Al Habsi", Decimal("73.630"), tpa_user)
        EndorsementQuery.objects.get_or_create(
            request=queried,
            subject="Please confirm dependent relationship",
            defaults={
                "raised_by": tpa_user,
                "assigned_organization": client,
                "message": "The uploaded document shows a different relationship than the submitted row. Please confirm.",
                "due_at": timezone.now() + timedelta(hours=4),
            },
        )

        self.stdout.write(self.style.SUCCESS("SmartEndorse demo data is ready."))
        self.stdout.write("Demo password for all users: Demo@12345")
        self.stdout.write("Users: smartadmin, insurer.manager, client.requester, broker.user, tpa.processor, agent.user")

    def org(self, code, name, org_type, email):
        obj, _ = Organization.objects.update_or_create(
            code=code,
            defaults={"name": name, "organization_type": org_type, "notification_email": email, "is_active": True},
        )
        return obj

    def user(self, username, first, last, email, organization, role, superuser=False):
        user, _ = User.objects.get_or_create(username=username)
        user.first_name = first
        user.last_name = last
        user.email = email
        user.is_active = True
        user.is_staff = superuser
        user.is_superuser = superuser
        user.set_password("Demo@12345")
        user.save()
        UserProfile.objects.update_or_create(
            user=user,
            defaults={"organization": organization, "role": role, "can_override_workflow": superuser},
        )
        return user

    def demo_request(self, reference, policy, requester, org, kind, status, plan, name, impact, actor, completed=False, member_no="", sum_assured=None):
        obj, created = EndorsementRequest.objects.get_or_create(
            reference=reference,
            defaults={
                "policy": policy,
                "endorsement_type": kind,
                "effective_date": date(2026, 9, 1),
                "requester": requester,
                "requester_organization": org,
                "status": status,
                "stp_eligible": status not in {EndorsementRequest.Status.NEEDS_INFO, EndorsementRequest.Status.FAILED},
                "validation_score": Decimal("100.00"),
                "premium_impact": impact,
                "currency": policy.currency,
                "submitted_at": timezone.now() - timedelta(days=2),
                "completed_at": timezone.now() - timedelta(days=1) if completed else None,
                "current_sla_due_at": None if completed else timezone.now() + timedelta(hours=12),
            },
        )
        if created:
            EndorsementItem.objects.create(
                request=obj,
                member_no=member_no,
                full_name=name,
                relationship="Employee",
                date_of_birth=date(1990, 6, 15),
                gender="Female",
                plan=plan,
                sum_assured=sum_assured,
                effective_date=obj.effective_date,
                annual_premium=abs(impact),
                prorata_factor=Decimal("0.650000"),
                premium_impact=impact,
            )
            WorkflowEvent.objects.create(
                request=obj,
                actor=actor,
                event_type="STATUS_CHANGE",
                from_status=EndorsementRequest.Status.SUBMITTED,
                to_status=status,
                description=f"Demo request moved to {obj.get_status_display()}.",
            )
        return obj
