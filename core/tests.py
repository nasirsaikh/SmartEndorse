from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from unittest.mock import MagicMock, patch

from .access import accessible_policies, can_decide_approval
from .ai import AIService
from .models import (
    AIExtractionProfile, AIProviderConfig, AITrainingExample, Attachment, EndorsementApproval, EndorsementItem, EndorsementRequest, Organization,
    PlatformConfiguration, Policy, PolicyAccess, PolicyMember, PolicyPlan, UserProfile, WorkflowEvent,
)
from .forms import EndorsementItemCorrectionForm
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


class PortalFrontendStyleTests(BaseInsuranceTest):
    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_authenticated_portal_uses_daisyui_command_center_assets(self):
        http = Client()
        http.force_login(self.requester)
        response = http.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("daisyui@5", html)
        self.assertIn("@tailwindcss/browser@4", html)
        self.assertIn("ENDORSEMENT CONTROL", html)
        self.assertIn("Global search: request, policy, client, member, Civil ID", html)
        self.assertNotIn("System status", html)
        self.assertNotIn("All services nominal", html)
        self.assertNotIn("tom-select", html.lower())
        self.assertIn('id="theme-toggle"', html)
        self.assertIn('id="portal-drawer"', html)
        self.assertNotIn("lg:drawer-open", html)
        self.assertNotIn("/static/admin/css/", html)
        self.assertNotIn("admin-lte", html.lower())
        self.assertNotIn("bootswatch", html.lower())

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_create_page_keeps_multi_file_drag_drop_controls(self):
        PolicyAccess.objects.get_or_create(policy=self.policy, organization=self.client, defaults={"can_create": True})
        http = Client()
        http.force_login(self.requester)
        response = http.get("/endorsements/new/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("se-dropzone", html)
        self.assertIn('data-drop-input="true"', html)
        self.assertIn("BROWSE FILES", html)
        self.assertIn('multiple', html)


class PortalValidationUXTests(BaseInsuranceTest):
    def grant_client_access(self):
        PolicyAccess.objects.get_or_create(
            policy=self.policy,
            organization=self.client,
            defaults={"can_create": True, "can_view_premium": True, "can_view_members": True},
        )

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_pipeline_page_has_dashboard_kpis_and_native_filters(self):
        self.grant_client_access()
        EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
            status=EndorsementRequest.Status.NEEDS_INFO,
        )
        http = Client()
        http.force_login(self.requester)
        response = http.get("/endorsements/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SEARCH & SEGMENT REQUESTS")
        self.assertContains(response, "Needs info")
        self.assertContains(response, "Pending approval")
        self.assertNotContains(response, "searchable-select")
        self.assertContains(response, 'name="status"')
        self.assertContains(response, 'name="product"')
        self.assertContains(response, 'name="type"')

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_boosted_navigation_to_pipeline_returns_full_dashboard_page(self):
        self.grant_client_access()
        http = Client()
        http.force_login(self.requester)
        response = http.get(
            "/endorsements/",
            HTTP_HX_REQUEST="true",
            HTTP_HX_BOOSTED="true",
            HTTP_HX_TARGET="body",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ENDORSEMENT PIPELINE")
        self.assertContains(response, "SEARCH & SEGMENT REQUESTS")
        self.assertContains(response, "Global search: request, policy, client, member, Civil ID")
        self.assertContains(response, 'id="request-table"')

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_pipeline_filter_refresh_returns_table_partial_only(self):
        self.grant_client_access()
        http = Client()
        http.force_login(self.requester)
        response = http.get(
            "/endorsements/",
            {"status": EndorsementRequest.Status.NEEDS_INFO},
            HTTP_HX_REQUEST="true",
            HTTP_HX_TARGET="request-table",
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Reference")
        self.assertNotContains(response, "SEARCH & SEGMENT REQUESTS")
        self.assertNotContains(response, "Global search: request, policy, client, member, Civil ID")


    def test_item_correction_uses_policy_dropdowns_and_plan_sum_assured(self):
        self.plan.sum_assured = Decimal("50000.000")
        self.plan.save(update_fields=["sum_assured"])
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
            status=EndorsementRequest.Status.NEEDS_INFO,
        )
        item = EndorsementItem.objects.create(
            request=req,
            full_name="Member",
            relationship="Employee",
            gender="Female",
            plan=self.plan,
            effective_date=self.today,
        )
        form = EndorsementItemCorrectionForm(data={
            "member_no": "",
            "employee_no": "E1",
            "national_id": "N1",
            "full_name": "Member",
            "relationship": "Spouse",
            "date_of_birth": "1990-01-01",
            "gender": "Female",
            "plan": str(self.plan.pk),
            "effective_date": self.today.isoformat(),
        }, instance=item)
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.relationship, "Spouse")
        self.assertEqual(saved.sum_assured, Decimal("50000.000"))
        self.assertNotIn("annual_salary", form.fields)
        self.assertTrue(form.fields["sum_assured"].disabled)
        self.assertEqual(
            [value for value, _ in form.fields["relationship"].choices],
            ["", "Employee", "Spouse", "Child"],
        )
        self.assertEqual(
            [value for value, _ in form.fields["gender"].choices],
            ["", "Male", "Female"],
        )
        self.assertEqual(list(form.fields["plan"].queryset), [self.plan])

    def test_plan_sum_assured_endpoint_is_access_scoped(self):
        self.grant_client_access()
        self.plan.sum_assured = Decimal("75000.000")
        self.plan.save(update_fields=["sum_assured"])
        http = Client()
        http.force_login(self.requester)
        response = http.get("/policy-plans/sum-assured/", {"plan": self.plan.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sum_assured"], "75000.000")

    def test_validation_upload_updates_existing_and_adds_new_member(self):
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
            status=EndorsementRequest.Status.NEEDS_INFO,
        )
        existing = EndorsementItem.objects.create(
            request=req,
            employee_no="E1",
            full_name="Old Name",
            relationship="Employee",
            gender="Male",
            effective_date=self.today,
        )
        attachment = Attachment.objects.create(
            request=req,
            file=SimpleUploadedFile("correction.csv", b"employee_no,full_name\nE1,New Name\n"),
            original_name="correction.csv",
            kind=Attachment.Kind.EXCEL,
            is_supplemental=True,
        )
        result = FileIntakeService._recover_items(req, [
            {"employee_no": "E1", "full_name": "New Name", "relationship": "Spouse", "gender": "Female"},
            {"employee_no": "E2", "full_name": "Added Member", "relationship": "Employee", "gender": "Male"},
        ], attachment)
        existing.refresh_from_db()
        self.assertEqual(existing.full_name, "New Name")
        self.assertEqual(existing.relationship, "Spouse")
        self.assertEqual(result["updated_items"], 1)
        self.assertEqual(result["created_items"], 1)
        self.assertTrue(req.items.filter(employee_no="E2", full_name="Added Member").exists())

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_validation_item_can_be_deleted_only_while_needs_info(self):
        self.grant_client_access()
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
            status=EndorsementRequest.Status.NEEDS_INFO,
        )
        item = EndorsementItem.objects.create(request=req, full_name="Remove Me", effective_date=self.today)
        http = Client()
        http.force_login(self.requester)
        response = http.post(f"/endorsements/{req.pk}/items/{item.pk}/delete/")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(EndorsementItem.objects.filter(pk=item.pk).exists())

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    @patch("core.views.FileIntakeService._extract")
    def test_item_ocr_fills_preview_without_saving_until_review(self, extract_mock):
        self.grant_client_access()
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
            status=EndorsementRequest.Status.NEEDS_INFO,
        )
        item = EndorsementItem.objects.create(
            request=req,
            employee_no="E1",
            full_name="Old Name",
            relationship="Employee",
            gender="Male",
            effective_date=self.today,
        )
        extract_mock.return_value = (
            [{"employee_no": "E1", "full_name": "OCR Name"}],
            [{"employee_no": "E1", "full_name": "OCR Name", "relationship": "Spouse", "gender": "Female", "plan_code": "G"}],
            {"method": "vision"},
        )
        http = Client()
        http.force_login(self.requester)
        upload = SimpleUploadedFile("member.jpg", b"fake-image", content_type="image/jpeg")
        response = http.post(
            f"/endorsements/{req.pk}/items/{item.pk}/edit/",
            {"action": "ocr_fill", "ocr_file": upload},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OCR Name")
        self.assertContains(response, "Nothing is saved until you click")
        item.refresh_from_db()
        self.assertEqual(item.full_name, "Old Name")
        ocr_attachment = req.attachments.order_by("-pk").first()
        self.assertEqual(ocr_attachment.extracted_payload.get("usage"), "item_ocr_preview")

    def test_member_validation_error_identifies_member(self):
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
        )
        item = EndorsementItem.objects.create(
            request=req,
            employee_no="E1101-1",
            full_name="Sara Al Hinai",
            date_of_birth=date(2018, 9, 22),
            gender="Female",
            effective_date=self.today,
        )
        errors, _ = ValidationService.validate(req)
        self.assertTrue(any('Member "Sara Al Hinai"' in error for error in errors))
        self.assertTrue(any("Relationship is required." in error for error in errors))
        item.refresh_from_db()
        self.assertEqual(item.validation_status, EndorsementItem.ValidationStatus.ERROR)

    def test_optional_item_ocr_timeout_does_not_block_valid_member(self):
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
        )
        EndorsementItem.objects.create(
            request=req,
            employee_no="E1101-1",
            full_name="Sara Al Hinai",
            relationship="Child",
            date_of_birth=date(2018, 9, 22),
            gender="Female",
            plan=self.plan,
            effective_date=self.today,
        )
        Attachment.objects.create(
            request=req,
            file=SimpleUploadedFile("id.pdf", b"fake-pdf", content_type="application/pdf"),
            original_name="id.pdf",
            kind=Attachment.Kind.PDF,
            is_supplemental=True,
            processed=False,
            processing_error="timed out",
            extracted_payload={"usage": "item_ocr_preview", "target_item_id": 1},
        )
        errors, score = ValidationService.validate(req)
        self.assertFalse(any("id.pdf" in error or "timed out" in error for error in errors))
        self.assertEqual(score, Decimal("100.00"))

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_detail_separates_document_error_from_member_error(self):
        self.grant_client_access()
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
            status=EndorsementRequest.Status.NEEDS_INFO,
            validation_errors=["id.pdf: timed out"],
        )
        EndorsementItem.objects.create(
            request=req,
            employee_no="E1101-1",
            full_name="Sara Al Hinai",
            relationship="Child",
            date_of_birth=date(2018, 9, 22),
            gender="Female",
            plan=self.plan,
            effective_date=self.today,
            validation_status=EndorsementItem.ValidationStatus.VALID,
        )
        Attachment.objects.create(
            request=req,
            file=SimpleUploadedFile("id.pdf", b"fake-pdf", content_type="application/pdf"),
            original_name="id.pdf",
            kind=Attachment.Kind.PDF,
            processed=False,
            processing_error="timed out",
        )
        http = Client()
        http.force_login(self.requester)
        response = http.get(f"/endorsements/{req.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Document processing issues")
        self.assertContains(response, "id.pdf")
        self.assertContains(response, "timed out")
        self.assertContains(response, "This is a document-processing error, not a member validation error.")
        self.assertNotContains(response, "Member validation issues")

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_failed_attachment_can_be_removed_during_validation(self):
        self.grant_client_access()
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
            status=EndorsementRequest.Status.NEEDS_INFO,
            validation_errors=["id.pdf: timed out"],
        )
        EndorsementItem.objects.create(
            request=req,
            employee_no="E1101-1",
            full_name="Sara Al Hinai",
            relationship="Child",
            date_of_birth=date(2018, 9, 22),
            gender="Female",
            plan=self.plan,
            effective_date=self.today,
        )
        attachment = Attachment.objects.create(
            request=req,
            file=SimpleUploadedFile("id.pdf", b"fake-pdf", content_type="application/pdf"),
            original_name="id.pdf",
            kind=Attachment.Kind.PDF,
            processed=False,
            processing_error="timed out",
        )
        http = Client()
        http.force_login(self.requester)
        response = http.post(f"/endorsements/{req.pk}/attachments/{attachment.pk}/remove-failed/")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Attachment.objects.filter(pk=attachment.pk).exists())

    @override_settings(STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    })
    def test_detail_has_one_validation_recovery_dropzone(self):
        self.grant_client_access()
        req = EndorsementRequest.objects.create(
            policy=self.policy,
            endorsement_type=EndorsementRequest.Type.ADDITION,
            effective_date=self.today,
            requester=self.requester,
            requester_organization=self.client,
            status=EndorsementRequest.Status.NEEDS_INFO,
        )
        EndorsementItem.objects.create(request=req, employee_no="E1", full_name="Member", effective_date=self.today)
        http = Client()
        http.force_login(self.requester)
        response = http.get(f"/endorsements/{req.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ADD OR UPDATE MEMBER DATA")
        self.assertNotContains(response, "BULK CORRECTION")
        self.assertContains(response, "APPLY & REVALIDATE")


class AIExtractionTrainingTests(BaseInsuranceTest):
    def test_product_specific_training_profile_overrides_global_profile(self):
        global_profile = AIExtractionProfile.objects.create(
            name="Test Global OCR",
            task=AIExtractionProfile.Task.DOCUMENT_EXTRACTION,
            product="",
            system_prompt="GLOBAL SYSTEM",
            instructions="GLOBAL INSTRUCTIONS",
            priority=500,
            is_active=True,
        )
        product_profile = AIExtractionProfile.objects.create(
            name="Test Medical OCR",
            task=AIExtractionProfile.Task.DOCUMENT_EXTRACTION,
            product=Policy.Product.GROUP_MEDICAL,
            system_prompt="MEDICAL SYSTEM",
            instructions="MEDICAL INSTRUCTIONS",
            field_aliases={"national_id": ["Civil ID"]},
            priority=600,
            is_active=True,
        )
        AITrainingExample.objects.create(
            profile=product_profile,
            name="Civil ID example",
            input_text="Civil ID: 12345678",
            expected_output=[{"national_id": "12345678"}],
            sort_order=1,
            is_active=True,
        )

        ai = AIService(
            product=Policy.Product.GROUP_MEDICAL,
            context={"policy_number": self.policy.policy_number, "valid_plans": [{"code": "G", "name": "Gold"}]},
        )
        system, prompt = ai._prompt_bundle(AIExtractionProfile.Task.DOCUMENT_EXTRACTION)

        self.assertEqual(system, "MEDICAL SYSTEM")
        self.assertIn("MEDICAL INSTRUCTIONS", prompt)
        self.assertIn("Civil ID example", prompt)
        self.assertIn("12345678", prompt)
        self.assertIn('"code": "G"', prompt)
        self.assertEqual(ai.last_profile, product_profile)
        self.assertNotEqual(ai.last_profile, global_profile)

    def test_json_parser_accepts_prefaced_array(self):
        content = 'Here is the result: [{"full_name":"Aisha"}]'
        parsed = AIService._parse_json_array(content)
        self.assertEqual(parsed[0]["full_name"], "Aisha")

    def test_text_and_vision_providers_can_be_active_together(self):
        text_provider = AIProviderConfig.objects.create(
            name="Text Ollama",
            provider=AIProviderConfig.Provider.OLLAMA,
            model_name="qwen2.5:7b",
            base_url="http://127.0.0.1:11434",
            is_active=True,
            supports_vision=False,
        )
        vision_provider = AIProviderConfig.objects.create(
            name="Vision Ollama",
            provider=AIProviderConfig.Provider.OLLAMA,
            model_name="qwen2.5vl:7b",
            base_url="http://127.0.0.1:11434",
            is_active=True,
            supports_vision=True,
        )

        ai = AIService()
        self.assertEqual(ai.config, text_provider)
        ai._require_provider(vision=True)
        self.assertEqual(ai.config, vision_provider)

    def test_vision_provider_is_selected_when_default_text_provider_cannot_see_images(self):
        AIProviderConfig.objects.create(
            name="Text Only",
            provider=AIProviderConfig.Provider.OLLAMA,
            model_name="qwen2.5:7b",
            is_active=True,
            supports_vision=False,
        )
        vision_provider = AIProviderConfig.objects.create(
            name="Image OCR",
            provider=AIProviderConfig.Provider.OLLAMA,
            model_name="qwen2.5vl:7b",
            is_active=True,
            supports_vision=True,
        )

        ai = AIService()
        self.assertFalse(ai.config.supports_vision)
        ai._require_provider(vision=True)
        self.assertEqual(ai.config, vision_provider)

    @patch("core.ai.httpx.Client")
    def test_bakllava_vision_uses_generate_endpoint_and_json_mode(self, client_cls):
        provider = AIProviderConfig.objects.create(
            name="BakLLaVA OCR",
            provider=AIProviderConfig.Provider.OLLAMA,
            model_name="bakllava:latest",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=300,
            is_active=True,
            supports_vision=True,
        )
        response = MagicMock()
        response.is_success = True
        response.status_code = 200
        response.json.return_value = {
            "response": '{"items":[{"full_name":"Aisha","relationship":"Employee"}]}'
        }
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = response

        ai = AIService(config=provider, product=Policy.Product.GROUP_MEDICAL)
        rows = ai.extract_image_bytes(b"fake-image", "image/jpeg")

        self.assertEqual(rows[0]["full_name"], "Aisha")
        url = client.post.call_args.args[0]
        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(url, "http://127.0.0.1:11434/api/generate")
        self.assertEqual(payload["model"], "bakllava:latest")
        self.assertEqual(payload["format"], "json")
        self.assertFalse(payload["stream"])
        self.assertTrue(payload["images"])
        self.assertIn('"items"', payload["prompt"])

    @patch("core.ai.httpx.Client")
    def test_ollama_vision_defaults_to_small_context_and_max_gpu_offload(self, client_cls):
        provider = AIProviderConfig.objects.create(
            name="GLM OCR",
            provider=AIProviderConfig.Provider.OLLAMA,
            model_name="glm-ocr:q8_0",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=300,
            is_active=True,
            supports_vision=True,
            options={"vision_pipeline": "direct_json"},
        )
        response = MagicMock()
        response.is_success = True
        response.status_code = 200
        response.json.return_value = {"response": '{"items":[]}'}
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = response

        AIService(config=provider).extract_image_bytes(b"fake-image", "image/jpeg")

        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(payload["options"]["num_ctx"], 4096)
        self.assertEqual(payload["options"]["num_gpu"], -1)
        self.assertEqual(payload["options"]["num_predict"], 1024)
        self.assertEqual(payload["keep_alive"], "15m")

    @patch("core.ai.httpx.Client")
    def test_ollama_vision_runtime_options_can_be_overridden_from_admin(self, client_cls):
        provider = AIProviderConfig.objects.create(
            name="GLM OCR Tuned",
            provider=AIProviderConfig.Provider.OLLAMA,
            model_name="glm-ocr:q8_0",
            base_url="http://127.0.0.1:11434",
            timeout_seconds=300,
            is_active=True,
            supports_vision=True,
            options={"num_ctx": 2048, "num_predict": 512, "keep_alive": "30m", "vision_pipeline": "direct_json"},
        )
        response = MagicMock()
        response.is_success = True
        response.status_code = 200
        response.json.return_value = {"response": '{"items":[]}'}
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = response

        AIService(config=provider).extract_image_bytes(b"fake-image", "image/jpeg")

        payload = client.post.call_args.kwargs["json"]
        self.assertEqual(payload["options"]["num_ctx"], 2048)
        self.assertEqual(payload["options"]["num_predict"], 512)
        self.assertEqual(payload["options"]["num_gpu"], -1)
        self.assertEqual(payload["keep_alive"], "30m")

    @patch("core.ai.httpx.Client")
    def test_ollama_404_explains_model_and_installed_models(self, client_cls):
        provider = AIProviderConfig.objects.create(
            name="BakLLaVA OCR",
            provider=AIProviderConfig.Provider.OLLAMA,
            model_name="bakllava:latest",
            base_url="http://127.0.0.1:11434",
            is_active=True,
            supports_vision=True,
        )
        failed = MagicMock()
        failed.is_success = False
        failed.status_code = 404
        failed.text = '{"error":"model \'bakllava:latest\' not found"}'
        failed.json.return_value = {"error": "model 'bakllava:latest' not found"}

        tags = MagicMock()
        tags.is_success = True
        tags.json.return_value = {"models": [{"name": "llava:latest"}, {"name": "qwen2.5:7b"}]}

        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = failed
        client.get.return_value = tags

        ai = AIService(config=provider)
        with self.assertRaisesRegex(RuntimeError, "bakllava:latest") as error:
            ai.extract_image_bytes(b"fake-image", "image/jpeg")
        self.assertIn("Installed models: llava:latest, qwen2.5:7b", str(error.exception))

    @patch("core.ai.httpx.Client")
    def test_glm_ocr_uses_ocr_text_then_text_model_json_mapping(self, client_cls):
        text_provider = AIProviderConfig.objects.create(
            name="Fast JSON Mapper",
            provider=AIProviderConfig.Provider.OLLAMA,
            model_name="qwen2.5:7b",
            base_url="http://127.0.0.1:11434",
            is_active=True,
            supports_vision=False,
            timeout_seconds=120,
        )
        vision_provider = AIProviderConfig.objects.create(
            name="GLM OCR",
            provider=AIProviderConfig.Provider.OLLAMA,
            model_name="glm-ocr:q8_0",
            base_url="http://127.0.0.1:11434",
            is_active=True,
            supports_vision=True,
            timeout_seconds=300,
        )

        ocr_response = MagicMock()
        ocr_response.is_success = True
        ocr_response.status_code = 200
        ocr_response.json.return_value = {
            "response": "Employee No: E100\nMember Name: Aisha\nRelationship: Employee"
        }

        mapper_response = MagicMock()
        mapper_response.raise_for_status.return_value = None
        mapper_response.json.return_value = {
            "message": {
                "content": '{"items":[{"member_no":null,"employee_no":"E100","national_id":null,"full_name":"Aisha","relationship":"Employee","date_of_birth":null,"gender":null,"plan_code":null,"annual_salary":null,"sum_assured":null,"effective_date":null}]}'
            }
        }

        client = client_cls.return_value.__enter__.return_value
        client.post.side_effect = [ocr_response, mapper_response]

        ai = AIService(config=vision_provider, product=Policy.Product.GROUP_MEDICAL)
        rows = ai.extract_image_bytes(b"fake-image", "image/jpeg")

        self.assertEqual(rows[0]["employee_no"], "E100")
        self.assertEqual(rows[0]["full_name"], "Aisha")
        self.assertEqual(client.post.call_count, 2)

        ocr_payload = client.post.call_args_list[0].kwargs["json"]
        self.assertNotIn("format", ocr_payload)
        self.assertIn("Text Recognition:", ocr_payload["prompt"])

        mapper_payload = client.post.call_args_list[1].kwargs["json"]
        self.assertEqual(mapper_payload["model"], text_provider.model_name)
        self.assertIsInstance(mapper_payload["format"], dict)
        self.assertIn("items", mapper_payload["format"]["properties"])

    def test_json_parser_recovers_first_valid_value_from_concatenated_json(self):
        content = (
            '{"items":[{"full_name":"Aisha"}]}'
            '{"items":[{"full_name":"Duplicate"}]}'
        )
        rows = AIService._parse_json_array(content)
        self.assertEqual(rows[0]["full_name"], "Aisha")

    def test_seeded_admin_training_profiles_exist(self):
        self.assertTrue(
            AIExtractionProfile.objects.filter(
                task=AIExtractionProfile.Task.DOCUMENT_EXTRACTION,
                is_active=True,
            ).exists()
        )
        self.assertTrue(
            AIExtractionProfile.objects.filter(
                task=AIExtractionProfile.Task.STRUCTURED_MAPPING,
                is_active=True,
            ).exists()
        )


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
