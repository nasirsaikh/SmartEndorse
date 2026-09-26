from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from unittest.mock import patch

from .access import accessible_policies, can_decide_approval
from .models import (
    EndorsementApproval, EndorsementItem, EndorsementRequest, Organization,
    PlatformConfiguration, Policy, PolicyAccess, PolicyMember, PolicyPlan, UserProfile, WorkflowEvent,
)
from .services import FileIntakeService, PricingEngine, ValidationService, WorkflowService


class BaseInsuranceTest(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.insurer = Organization.objects.create(name="Insurer", code="INS", organization_type=Organization.Type.INSURER)
        self.client = Organization.objects.create(name="Client", code="CLI", organization_type=Organization.Type.CLIENT)
        self.tpa = Organization.objects.create(name="TPA", code="TPA", organization_type=Organization.Type.TPA)
        self.policy = Policy.objects.create(
            policy_number="P1", policy_name="Medical", product=Policy.Product.GROUP_MEDICAL,
            insurer=self.insurer, client=self.client, tpa=self.tpa,
            effective_from=self.today - timedelta(days=60), effective_to=self.today + timedelta(days=180),
            rating_method=Policy.RatingMethod.FLAT_ANNUAL, day_count_basis=365,
            allow_backdated_days=30, endorsement_expiry_cutoff_days=30,
        )
        self.plan = PolicyPlan.objects.create(policy=self.policy, code="G", name="Gold", annual_rate=Decimal("365.000"))
        self.requester = User.objects.create_user("requester", password="x")
        UserProfile.objects.create(user=self.requester, organization=self.client, role=UserProfile.Role.REQUESTER)
        self.tpa_user = User.objects.create_user("tpa.user", password="x")
        UserProfile.objects.create(user=self.tpa_user, organization=self.tpa, role=UserProfile.Role.TPA_PROCESSOR)
        self.manager = User.objects.create_user("manager", password="x")
        UserProfile.objects.create(user=self.manager, organization=self.insurer, role=UserProfile.Role.INSURER_MANAGER)
        PlatformConfiguration.objects.create(name="Default", endorsement_expiry_cutoff_days=30)


class PricingEngineTests(BaseInsuranceTest):
    def test_daily_prorata_addition(self):
        fixed_policy = Policy.objects.create(
            policy_number="PRICE1", policy_name="Fixed 2026", product=Policy.Product.GROUP_MEDICAL,
            insurer=self.insurer, client=self.client, tpa=self.tpa,
            effective_from=date(2026, 1, 1), effective_to=date(2026, 12, 31),
            rating_method=Policy.RatingMethod.FLAT_ANNUAL, day_count_basis=365,
        )
        fixed_plan = PolicyPlan.objects.create(policy=fixed_policy, code="PX", name="Pricing", annual_rate=Decimal("365.000"))
        req = EndorsementRequest.objects.create(policy=fixed_policy, endorsement_type=EndorsementRequest.Type.ADDITION, effective_date=date(2026, 7, 1), requester=self.requester, requester_organization=self.client)
        item = EndorsementItem.objects.create(request=req, full_name="Member", plan=fixed_plan, effective_date=date(2026, 7, 1))
        self.assertEqual(PricingEngine.calculate_item(item), Decimal("184.000"))

    def test_deletion_is_negative(self):
        req = EndorsementRequest.objects.create(policy=self.policy, endorsement_type=EndorsementRequest.Type.DELETION, effective_date=self.today, requester=self.requester, requester_organization=self.client)
        item = EndorsementItem.objects.create(request=req, member_no="M1", plan=self.plan, effective_date=self.today)
        self.assertLess(PricingEngine.calculate_item(item), 0)


class AccessTests(TestCase):
    def test_view_and_create_grants_are_separate(self):
        today = timezone.localdate()
        insurer = Organization.objects.create(name="Insurer", code="INS2", organization_type=Organization.Type.INSURER)
        client = Organization.objects.create(name="Client", code="CLI2", organization_type=Organization.Type.CLIENT)
        broker = Organization.objects.create(name="Broker", code="BR1", organization_type=Organization.Type.BROKER)
        policy = Policy.objects.create(policy_number="P2", policy_name="Life", product=Policy.Product.GROUP_LIFE, insurer=insurer, client=client, effective_from=today, effective_to=today + timedelta(days=365))
        PolicyAccess.objects.create(policy=policy, organization=broker, can_create=False)
        user = User.objects.create_user("viewer", password="x")
        UserProfile.objects.create(user=user, organization=broker, role=UserProfile.Role.BROKER_USER)
        self.assertTrue(accessible_policies(user).filter(pk=policy.pk).exists())
        self.assertFalse(accessible_policies(user, require_create=True).filter(pk=policy.pk).exists())


class IntakeNormalizationTests(BaseInsuranceTest):
    def test_ai_nulls_do_not_erase_values_already_present_in_source_raw(self):
        row = {
            "_source_raw": {
                "member_no": "IDOMN1365102783",
                "employee_no": "136510278",
                "national_id": "8",
                "full_name": "ANKIT PRAMOD SHINGARE",
                "relationship": None,
                "date_of_birth": "1992-12-26",
                "gender": "M",
                "plan_code": "ANKIT PRAMOD SHINGARE",
                "annual_salary": None,
                "sum_assured": None,
                "effective_date": "2026-09-13",
            },
            "member_no": "IDOMN1365102783",
            "employee_no": None,
            "national_id": None,
            "full_name": None,
            "relationship": None,
            "date_of_birth": None,
            "gender": None,
            "plan_code": None,
            "annual_salary": None,
            "sum_assured": None,
            "effective_date": None,
        }
        normalized = FileIntakeService._normalize_ai_row(row)
        self.assertEqual(normalized["employee_no"], "136510278")
        self.assertEqual(normalized["national_id"], "8")
        self.assertEqual(normalized["full_name"], "ANKIT PRAMOD SHINGARE")
        self.assertEqual(normalized["date_of_birth"], "1992-12-26")
        self.assertEqual(normalized["gender"], "Male")
        self.assertEqual(normalized["effective_date"], "2026-09-13")
        self.assertIsNone(normalized["relationship"])

    @patch("core.services.AIService")
    def test_structured_ai_mapping_cannot_null_deterministically_mapped_fields(self, ai_service):
        raw = {
            "Member No": "M-100",
            "Full Name": "ANKIT PRAMOD SHINGARE",
            "Date of Birth": "1992-12-26",
            "Gender": "M",
            "Effective Date": "2026-09-13",
            "Unusual Plan Header": "G",
        }
        ai = ai_service.return_value
        ai.available = True
        ai.normalize_structured_rows.return_value = [{
            "_source_raw": raw,
            "member_no": "M-100",
            "full_name": None,
            "date_of_birth": None,
            "gender": None,
            "effective_date": None,
            "plan_code": "G",
        }]
        rows, ai_used = FileIntakeService._normalize_structured([raw])
        self.assertTrue(ai_used)
        self.assertEqual(rows[0]["full_name"], "ANKIT PRAMOD SHINGARE")
        self.assertEqual(rows[0]["date_of_birth"], "1992-12-26")
        self.assertEqual(rows[0]["gender"], "Male")
        self.assertEqual(rows[0]["effective_date"], "2026-09-13")
        self.assertEqual(rows[0]["plan_code"], "G")

    def test_plan_can_match_configured_plan_name_as_well_as_code(self):
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
        )
        values = FileIntakeService._to_item_values(req, {
            "full_name": "Member",
            "date_of_birth": "1990-01-01",
            "gender": "F",
            "relationship": "self",
            "plan_code": "Gold",
        })
        self.assertEqual(values["plan"], self.plan)
        self.assertEqual(values["gender"], "Female")
        self.assertEqual(values["relationship"], "Employee")

    def test_validation_repairs_existing_item_from_stored_source_json(self):
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
        )
        raw = {
            "member_no": "IDOMN1365102783",
            "employee_no": "136510278",
            "national_id": "8",
            "full_name": "ANKIT PRAMOD SHINGARE",
            "relationship": None,
            "date_of_birth": "1992-12-26",
            "gender": "M",
            "plan_code": "ANKIT PRAMOD SHINGARE",
            "annual_salary": None,
            "sum_assured": None,
            "effective_date": self.today.isoformat(),
        }
        item = EndorsementItem.objects.create(
            request=req,
            member_no="IDOMN1365102783",
            extracted_data={
                "normalized": {
                    "_source_raw": raw,
                    "member_no": "IDOMN1365102783",
                    "employee_no": None,
                    "national_id": None,
                    "full_name": None,
                    "relationship": None,
                    "date_of_birth": None,
                    "gender": None,
                    "plan_code": None,
                    "annual_salary": None,
                    "sum_assured": None,
                    "effective_date": None,
                },
                "source_raw": raw,
            },
        )
        errors, _ = ValidationService.validate(req)
        item.refresh_from_db()
        self.assertEqual(item.employee_no, "136510278")
        self.assertEqual(item.national_id, "8")
        self.assertEqual(item.full_name, "ANKIT PRAMOD SHINGARE")
        self.assertEqual(item.date_of_birth, date(1992, 12, 26))
        self.assertEqual(item.gender, "Male")
        self.assertEqual(item.effective_date, self.today)
        self.assertEqual(item.validation_errors, ["Relationship is required.", "Plan is required."])
        self.assertTrue(any("Relationship is required." in error for error in errors))
        self.assertTrue(any("Plan is required." in error for error in errors))
        self.assertFalse(any("Full Name is required." in error for error in errors))
        self.assertFalse(any("Date Of Birth is required." in error for error in errors))
        self.assertFalse(any("Gender is required." in error for error in errors))


class ValidationAndApprovalTests(BaseInsuranceTest):
    def valid_item(self, request_obj, **overrides):
        values = {
            "full_name": "Aisha Test", "employee_no": "E900", "national_id": "N900",
            "relationship": "Employee", "date_of_birth": date(1990, 1, 1), "gender": "Female",
            "plan": self.plan, "effective_date": request_obj.effective_date,
        }
        values.update(overrides)
        return EndorsementItem.objects.create(request=request_obj, **values)

    def test_policy_expiry_cutoff_is_inclusive(self):
        self.policy.effective_to = self.today + timedelta(days=30)
        self.policy.endorsement_expiry_cutoff_days = 30
        self.policy.save(update_fields=["effective_to", "endorsement_expiry_cutoff_days"])
        req = EndorsementRequest.objects.create(policy=self.policy, endorsement_type=EndorsementRequest.Type.ADDITION, effective_date=self.today, requester=self.requester, requester_organization=self.client)
        self.valid_item(req)
        errors, _ = ValidationService.validate(req)
        self.assertTrue(any("blocked within 30 days" in e for e in errors))

    def test_no_records_is_blocked(self):
        req = EndorsementRequest.objects.create(policy=self.policy, endorsement_type=EndorsementRequest.Type.ADDITION, effective_date=self.today, requester=self.requester, requester_organization=self.client)
        WorkflowService.submit(req, self.requester)
        req.refresh_from_db()
        self.assertEqual(req.status, EndorsementRequest.Status.NEEDS_INFO)
        self.assertTrue(any("No endorsement member rows" in e for e in req.validation_errors))

    def test_existing_member_requires_insurer_approval(self):
        PolicyMember.objects.create(policy=self.policy, member_no="M900", employee_no="E900", national_id="N900", full_name="Existing Member", relationship="Employee", date_of_birth=date(1990, 1, 1), gender="Female", plan=self.plan, is_active=True)
        req = EndorsementRequest.objects.create(policy=self.policy, endorsement_type=EndorsementRequest.Type.ADDITION, effective_date=self.today, requester=self.requester, requester_organization=self.client)
        item = self.valid_item(req, member_no="M900")
        WorkflowService.submit(req, self.requester)
        req.refresh_from_db(); item.refresh_from_db()
        self.assertEqual(req.status, EndorsementRequest.Status.PENDING_INSURER_APPROVAL)
        self.assertTrue(item.is_existing_record)
        self.assertTrue(item.requires_insurer_approval)
        approval = req.approvals.get(approval_type=EndorsementApproval.ApprovalType.EXISTING_MEMBER)
        self.assertEqual(approval.assigned_organization, self.insurer)
        self.assertTrue(can_decide_approval(self.manager, approval))
        self.assertFalse(can_decide_approval(self.requester, approval))

    def test_tpa_amount_change_requires_approval_and_reuses_pending_task(self):
        req = EndorsementRequest.objects.create(policy=self.policy, endorsement_type=EndorsementRequest.Type.ADDITION, effective_date=self.today, requester=self.requester, requester_organization=self.client, status=EndorsementRequest.Status.TPA_IN_PROGRESS)
        item = self.valid_item(req)
        item.premium_impact = Decimal("100.000")
        item.validation_status = EndorsementItem.ValidationStatus.VALID
        item.save(update_fields=["premium_impact", "validation_status"])
        first = WorkflowService.update_tpa_item(item, self.tpa_user, "CARD-001", Decimal("110.000"))
        req.refresh_from_db()
        self.assertEqual(req.status, EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL)
        self.assertEqual(first.old_amount, Decimal("100.000"))
        self.assertEqual(first.new_amount, Decimal("110.000"))
        second = WorkflowService.update_tpa_item(item, self.tpa_user, "CARD-001", Decimal("115.000"))
        self.assertEqual(first.pk, second.pk)
        second.refresh_from_db()
        self.assertEqual(second.new_amount, Decimal("115.000"))
        self.assertEqual(req.approvals.filter(status=EndorsementApproval.Status.PENDING).count(), 1)

    def test_medical_completion_requires_card_number(self):
        req = EndorsementRequest.objects.create(policy=self.policy, endorsement_type=EndorsementRequest.Type.ADDITION, effective_date=self.today, requester=self.requester, requester_organization=self.client, status=EndorsementRequest.Status.TPA_IN_PROGRESS)
        item = self.valid_item(req)
        item.validation_status = EndorsementItem.ValidationStatus.VALID
        item.save(update_fields=["validation_status"])
        with self.assertRaisesMessage(ValueError, "Card number is mandatory"):
            WorkflowService.complete(req, self.tpa_user)


    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_resolution_event_without_filled_key_renders_detail(self):
        PolicyAccess.objects.get_or_create(policy=self.policy, organization=self.client, defaults={"can_create": True})
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
            status=EndorsementRequest.Status.NEEDS_INFO,
        )
        WorkflowEvent.objects.create(
            request=req,
            actor=self.requester,
            event_type="SUPPLEMENTAL_ROW_CREATED",
            description="Supplemental upload created item 18.",
            payload={"item_id": 18, "source": "id.pdf"},
        )
        http = Client()
        http.force_login(self.requester)
        response = http.get(f"/endorsements/{req.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "id.pdf")
        self.assertContains(response, "Supplemental upload created item 18.")

    @patch("core.views.BulkRecoveryService.process")
    def test_bulk_recovery_is_scoped_to_selected_endorsement(self, process_mock):
        PolicyAccess.objects.get_or_create(policy=self.policy, organization=self.client, defaults={"can_create": True})
        selected = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
            status=EndorsementRequest.Status.NEEDS_INFO,
        )
        other = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
            status=EndorsementRequest.Status.NEEDS_INFO,
        )
        self.valid_item(selected, employee_no="SEL-1", national_id="SEL-N1")
        self.valid_item(other, employee_no="OTH-1", national_id="OTH-N1")
        process_mock.return_value = 0
        http = Client()
        http.force_login(self.requester)
        upload = SimpleUploadedFile(
            "correction.csv",
            b"employee_no,date_of_birth\nSEL-1,1990-01-01\n",
            content_type="text/csv",
        )
        response = http.post(
            f"/endorsements/{selected.pk}/recovery/",
            {"attachments": upload},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(process_mock.call_count, 1)
        _, target = process_mock.call_args.args
        self.assertEqual(target.pk, selected.pk)
        self.assertNotEqual(target.pk, other.pk)
