from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from .access import accessible_policies
from .models import (
    EndorsementItem, EndorsementRequest, Organization, Policy, PolicyAccess,
    PolicyPlan, UserProfile,
)
from .services import PricingEngine


class PricingEngineTests(TestCase):
    def setUp(self):
        self.insurer = Organization.objects.create(name="Insurer", code="INS", organization_type=Organization.Type.INSURER)
        self.client = Organization.objects.create(name="Client", code="CLI", organization_type=Organization.Type.CLIENT)
        self.tpa = Organization.objects.create(name="TPA", code="TPA", organization_type=Organization.Type.TPA)
        self.policy = Policy.objects.create(
            policy_number="P1",
            policy_name="Medical",
            product=Policy.Product.GROUP_MEDICAL,
            insurer=self.insurer,
            client=self.client,
            tpa=self.tpa,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 12, 31),
            rating_method=Policy.RatingMethod.FLAT_ANNUAL,
            day_count_basis=365,
        )
        self.plan = PolicyPlan.objects.create(policy=self.policy, code="G", name="Gold", annual_rate=Decimal("365.000"))
        self.user = User.objects.create_user("requester", password="x")
        UserProfile.objects.create(user=self.user, organization=self.client, role=UserProfile.Role.REQUESTER)

    def test_daily_prorata_addition(self):
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=date(2026, 7, 1),
            requester=self.user,
            requester_organization=self.client,
        )
        item = EndorsementItem.objects.create(
            request=req,
            full_name="Member",
            plan=self.plan,
            effective_date=date(2026, 7, 1),
        )
        impact = PricingEngine.calculate_item(item)
        self.assertEqual(impact, Decimal("184.000"))

    def test_deletion_is_negative(self):
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.DELETION,
            effective_date=date(2026, 7, 1),
            requester=self.user,
            requester_organization=self.client,
        )
        item = EndorsementItem.objects.create(request=req, member_no="M1", plan=self.plan, effective_date=date(2026, 7, 1))
        self.assertLess(PricingEngine.calculate_item(item), 0)


class AccessTests(TestCase):
    def test_view_and_create_grants_are_separate(self):
        insurer = Organization.objects.create(name="Insurer", code="INS2", organization_type=Organization.Type.INSURER)
        client = Organization.objects.create(name="Client", code="CLI2", organization_type=Organization.Type.CLIENT)
        broker = Organization.objects.create(name="Broker", code="BR1", organization_type=Organization.Type.BROKER)
        policy = Policy.objects.create(
            policy_number="P2", policy_name="Life", product=Policy.Product.GROUP_LIFE,
            insurer=insurer, client=client, effective_from=date(2026, 1, 1), effective_to=date(2026, 12, 31),
        )
        PolicyAccess.objects.create(policy=policy, organization=broker, can_create=False)
        user = User.objects.create_user("viewer", password="x")
        UserProfile.objects.create(user=user, organization=broker, role=UserProfile.Role.BROKER_USER)
        self.assertTrue(accessible_policies(user).filter(pk=policy.pk).exists())
        self.assertFalse(accessible_policies(user, require_create=True).filter(pk=policy.pk).exists())
