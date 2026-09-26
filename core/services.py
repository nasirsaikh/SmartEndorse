import csv
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import httpx
from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from openpyxl import load_workbook
from pypdf import PdfReader

from .ai import AIService
from .models import (
    Attachment, EndorsementItem, EndorsementQuery, EndorsementRequest, IntegrationEndpoint,
    Organization, PlatformConfiguration, Policy, PolicyMember, PolicyPlan, WorkflowEvent,
)


def platform_config():
    obj, _ = PlatformConfiguration.objects.get_or_create(name="Default")
    return obj


def accessible_policies(user):
    if not user.is_authenticated:
        return Policy.objects.none()
    if user.is_superuser:
        return Policy.objects.filter(is_active=True)
    try:
        profile = user.profile
    except Exception:
        return Policy.objects.none()
    org = profile.organization
    if org.organization_type == Organization.Type.INSURER:
        return Policy.objects.filter(insurer=org, is_active=True)
    if org.organization_type == Organization.Type.TPA:
        return Policy.objects.filter(tpa=org, is_active=True)
    return Policy.objects.filter(
        access_grants__organization=org,
        access_grants__can_create=True,
        is_active=True,
    ).distinct()


def accessible_endorsements(user):
    policies = accessible_policies(user)
    qs = EndorsementRequest.objects.filter(policy__in=policies).select_related(
        "policy", "policy__client", "policy__tpa", "requester", "requester_organization"
    )
    try:
        org = user.profile.organization
        if org.organization_type not in {Organization.Type.INSURER, Organization.Type.TPA} and not user.is_superuser:
            qs = qs.filter(
                Q(requester_organization=org) |
                Q(policy__access_grants__organization=org)
            ).distinct()
    except Exception:
        return EndorsementRequest.objects.none()
    return qs


class SLAService:
    @staticmethod
    def deadline(profile):
        if not profile:
            return None
        return timezone.now() + timedelta(hours=profile.target_hours)

    @classmethod
    def for_status(cls, request, status):
        if status in {EndorsementRequest.Status.SENT_TO_TPA, EndorsementRequest.Status.TPA_IN_PROGRESS}:
            return cls.deadline(request.policy.tpa_sla)
        if status == EndorsementRequest.Status.TPA_QUERY:
            return cls.deadline(request.policy.client_query_sla)
        if status in {EndorsementRequest.Status.SUBMITTED, EndorsementRequest.Status.AUTO_APPROVED}:
            return cls.deadline(request.policy.insurer_sla)
        return None


class PricingEngine:
    MONEY = Decimal("0.001")

    @classmethod
    def annual_premium(cls, policy, item):
        params = policy.rating_parameters or {}
        if policy.rating_method == Policy.RatingMethod.FLAT_ANNUAL:
            if item.plan_id:
                rate = item.plan.annual_rate
                rel_rates = item.plan.relationship_rates or {}
                if item.relationship and str(item.relationship) in rel_rates:
                    rate = Decimal(str(rel_rates[str(item.relationship)]))
                return Decimal(rate)
            return Decimal(str(params.get("default_annual_rate", "0")))
        if policy.rating_method == Policy.RatingMethod.PER_MILLE_SUM_ASSURED:
            rate = Decimal(str(params.get("rate_per_mille", "0")))
            return Decimal(item.sum_assured or 0) * rate / Decimal("1000")
        if policy.rating_method == Policy.RatingMethod.PERCENT_OF_SALARY:
            rate = Decimal(str(params.get("salary_percent", "0")))
            return Decimal(item.annual_salary or 0) * rate / Decimal("100")
        return Decimal("0")

    @classmethod
    def prorata_factor(cls, policy, effective_date):
        if effective_date < policy.effective_from:
            effective_date = policy.effective_from
        if effective_date > policy.effective_to:
            return Decimal("0")
        remaining = Decimal((policy.effective_to - effective_date).days + 1)
        mode = (policy.rating_parameters or {}).get("prorata_mode", "fixed_basis")
        if mode == "policy_days":
            denominator = Decimal((policy.effective_to - policy.effective_from).days + 1)
        else:
            denominator = Decimal(policy.day_count_basis or 365)
        return min(Decimal("1"), max(Decimal("0"), remaining / denominator))

    @classmethod
    def calculate_item(cls, item):
        policy = item.request.policy
        effective = item.effective_date or item.request.effective_date
        annual = cls.annual_premium(policy, item)
        factor = cls.prorata_factor(policy, effective)
        impact = (annual * factor).quantize(cls.MONEY, rounding=ROUND_HALF_UP)
        if item.request.endorsement_type == EndorsementRequest.Type.DELETION:
            impact = -impact
        item.annual_premium = annual.quantize(cls.MONEY, rounding=ROUND_HALF_UP)
        item.prorata_factor = factor.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        item.premium_impact = impact
        item.save(update_fields=["annual_premium", "prorata_factor", "premium_impact", "updated_at"])
        return impact

    @classmethod
    def calculate_request(cls, request):
        total = Decimal("0")
        for item in request.items.select_related("plan"):
            total += cls.calculate_item(item)
        request.premium_impact = total.quantize(cls.MONEY, rounding=ROUND_HALF_UP)
        request.currency = request.policy.currency
        request.save(update_fields=["premium_impact", "currency", "updated_at"])
        return request.premium_impact


class FileIntakeService:
    HEADER_MAP = {
        "member no": "member_no", "member_no": "member_no", "member id": "member_no",
        "employee no": "employee_no", "employee_no": "employee_no", "employee id": "employee_no",
        "national id": "national_id", "civil id": "national_id", "civil_id": "national_id",
        "name": "full_name", "full name": "full_name", "member name": "full_name",
        "relationship": "relationship", "relation": "relationship",
        "date of birth": "date_of_birth", "dob": "date_of_birth",
        "gender": "gender", "sex": "gender",
        "plan": "plan_code", "plan code": "plan_code", "category": "plan_code",
        "annual salary": "annual_salary", "salary": "annual_salary",
        "sum assured": "sum_assured", "sum_assured": "sum_assured",
        "effective date": "effective_date", "addition date": "effective_date", "deletion date": "effective_date",
    }

    @classmethod
    def kind_for_name(cls, name):
        ext = Path(name).suffix.lower()
        if ext in {".xlsx", ".xls", ".csv"}:
            return Attachment.Kind.EXCEL
        if ext == ".pdf":
            return Attachment.Kind.PDF
        if ext in {".png", ".jpg", ".jpeg", ".webp"}:
            return Attachment.Kind.IMAGE
        return Attachment.Kind.OTHER

    @classmethod
    def process(cls, attachment):
        try:
            ext = Path(attachment.original_name).suffix.lower()
            if ext == ".xlsx":
                rows = cls._xlsx_rows(attachment.file.path)
            elif ext == ".csv":
                rows = cls._csv_rows(attachment.file.path)
            elif ext == ".pdf":
                rows = cls._pdf_rows(attachment.file.path)
            elif ext in {".png", ".jpg", ".jpeg", ".webp"}:
                rows = AIService().extract_image_rows(attachment.file.path)
            else:
                raise ValueError("Unsupported file type. Use XLSX, CSV, PDF, PNG, JPG or JPEG.")
            created = cls._create_items(attachment.request, rows)
            attachment.extracted_payload = {"row_count": len(rows), "created_items": created}
            attachment.processed = True
            attachment.processing_error = ""
        except Exception as exc:
            attachment.processing_error = str(exc)
            attachment.processed = False
        attachment.save(update_fields=["extracted_payload", "processed", "processing_error", "updated_at"])
        return attachment.processed

    @classmethod
    def _xlsx_rows(cls, path):
        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        values = list(ws.iter_rows(values_only=True))
        if not values:
            return []
        headers = [cls._map_header(x) for x in values[0]]
        return [
            cls._normalize(dict(zip(headers, row)))
            for row in values[1:]
            if any(v not in (None, "") for v in row)
        ]

    @classmethod
    def _csv_rows(cls, path):
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            return [
                cls._normalize({cls._map_header(k): v for k, v in row.items()})
                for row in reader
            ]

    @classmethod
    def _pdf_rows(cls, path):
        reader = PdfReader(path)
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        if not text.strip():
            raise ValueError("The PDF has no extractable text. Convert scanned pages to images or configure an OCR/vision ingestion service.")
        return AIService().extract_text_rows(text)

    @classmethod
    def _map_header(cls, value):
        key = str(value or "").strip().lower().replace("-", " ")
        return cls.HEADER_MAP.get(key, key.replace(" ", "_"))

    @staticmethod
    def _normalize(row):
        clean = {}
        for key, value in row.items():
            if value in ("", None):
                clean[key] = None
            elif isinstance(value, datetime):
                clean[key] = value.date().isoformat()
            else:
                clean[key] = value
        return clean

    @classmethod
    def _date(cls, value):
        if not value:
            return None
        if hasattr(value, "year") and not isinstance(value, str):
            return value.date() if hasattr(value, "date") else value
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(str(value).strip(), fmt).date()
            except ValueError:
                continue
        return None

    @classmethod
    def _decimal(cls, value):
        if value in (None, ""):
            return None
        try:
            return Decimal(str(value).replace(",", "").strip())
        except Exception:
            return None

    @classmethod
    def _create_items(cls, request, rows):
        count = 0
        for row in rows:
            plan = None
            plan_code = row.get("plan_code")
            if plan_code:
                plan = PolicyPlan.objects.filter(
                    policy=request.policy,
                    code__iexact=str(plan_code).strip(),
                    is_active=True,
                ).first()
            member_no = str(row.get("member_no") or "").strip()
            existing = None
            if request.endorsement_type == EndorsementRequest.Type.DELETION and member_no:
                existing = PolicyMember.objects.filter(
                    policy=request.policy,
                    member_no=member_no,
                    is_active=True,
                ).select_related("plan").first()
            EndorsementItem.objects.create(
                request=request,
                member_no=member_no,
                employee_no=str(row.get("employee_no") or (existing.employee_no if existing else "") or "").strip(),
                national_id=str(row.get("national_id") or (existing.national_id if existing else "") or "").strip(),
                full_name=str(row.get("full_name") or (existing.full_name if existing else "") or "").strip(),
                relationship=str(row.get("relationship") or (existing.relationship if existing else "") or "").strip(),
                date_of_birth=cls._date(row.get("date_of_birth")) or (existing.date_of_birth if existing else None),
                gender=str(row.get("gender") or (existing.gender if existing else "") or "").strip(),
                plan=plan or (existing.plan if existing else None),
                annual_salary=cls._decimal(row.get("annual_salary")) or (existing.annual_salary if existing else None),
                sum_assured=cls._decimal(row.get("sum_assured")) or (existing.sum_assured if existing else None),
                effective_date=cls._date(row.get("effective_date")) or request.effective_date,
                extracted_data=row,
            )
            count += 1
        return count


class ValidationService:
    MEDICAL_ADD_DEFAULTS = ["full_name", "date_of_birth", "gender", "relationship", "plan"]
    LIFE_ADD_DEFAULTS = ["full_name", "date_of_birth", "gender", "sum_assured"]
    DELETE_DEFAULTS = ["member_no", "effective_date"]

    @classmethod
    def validate(cls, request):
        errors = []
        policy = request.policy
        if request.effective_date < policy.effective_from or request.effective_date > policy.effective_to:
            errors.append(f"Effective date must be within policy period {policy.effective_from} to {policy.effective_to}.")
        if policy.product == Policy.Product.GROUP_MEDICAL and not policy.tpa_id:
            errors.append("Group Medical policy has no TPA configured.")
        if not request.items.exists():
            errors.append("No endorsement member rows were provided or extracted.")
        if request.endorsement_type == EndorsementRequest.Type.ADDITION:
            required = policy.required_fields_addition or (
                cls.MEDICAL_ADD_DEFAULTS if policy.product == Policy.Product.GROUP_MEDICAL else cls.LIFE_ADD_DEFAULTS
            )
        else:
            required = policy.required_fields_deletion or cls.DELETE_DEFAULTS
        for item in request.items.select_related("plan"):
            item_errors = []
            for field in required:
                value = item.plan_id if field == "plan" else getattr(item, field, None)
                if value in (None, ""):
                    item_errors.append(f"{field.replace('_', ' ').title()} is required.")
            if request.endorsement_type == EndorsementRequest.Type.DELETION and item.member_no:
                if not PolicyMember.objects.filter(
                    policy=policy,
                    member_no=item.member_no,
                    is_active=True,
                ).exists():
                    item_errors.append(f"Active member {item.member_no} was not found on the policy.")
            item.validation_errors = item_errors
            item.save(update_fields=["validation_errors", "updated_at"])
            errors.extend([f"Item {item.pk}: {e}" for e in item_errors])
        for attachment in request.attachments.exclude(processing_error=""):
            errors.append(f"{attachment.original_name}: {attachment.processing_error}")
        score = Decimal("100.00") if not errors else max(
            Decimal("0"),
            Decimal("100") - Decimal(len(errors) * 10),
        )
        return errors, score


class DispatchService:
    @classmethod
    def dispatch(cls, request):
        if request.policy.product == Policy.Product.GROUP_MEDICAL:
            org = request.policy.tpa
            owner_type = IntegrationEndpoint.OwnerType.TPA
        else:
            org = request.policy.insurer
            owner_type = IntegrationEndpoint.OwnerType.INSURER_CORE

        endpoint = IntegrationEndpoint.objects.filter(
            organization=org,
            owner_type=owner_type,
            is_active=True,
        ).filter(
            Q(product="") | Q(product=request.policy.product)
        ).first()

        if not endpoint:
            raise RuntimeError(f"No active {owner_type} integration endpoint configured for {org}.")

        payload = {
            "reference": request.reference,
            "policy_number": request.policy.policy_number,
            "endorsement_type": request.endorsement_type,
            "effective_date": request.effective_date.isoformat(),
            "premium_impact": str(request.premium_impact),
            "currency": request.currency,
            "items": [{
                "member_no": i.member_no,
                "employee_no": i.employee_no,
                "full_name": i.full_name,
                "relationship": i.relationship,
                "date_of_birth": i.date_of_birth.isoformat() if i.date_of_birth else None,
                "gender": i.gender,
                "plan": i.plan.code if i.plan else None,
                "annual_salary": str(i.annual_salary) if i.annual_salary is not None else None,
                "sum_assured": str(i.sum_assured) if i.sum_assured is not None else None,
                "premium_impact": str(i.premium_impact),
            } for i in request.items.select_related("plan")],
        }

        if endpoint.transport == IntegrationEndpoint.Transport.API:
            response = httpx.post(
                endpoint.endpoint_url,
                json=payload,
                headers=endpoint.auth_headers or {},
                timeout=60,
            )
            response.raise_for_status()
            try:
                data = response.json()
                request.external_reference = str(data.get("reference") or data.get("id") or "")
                request.save(update_fields=["external_reference", "updated_at"])
            except Exception:
                pass
        else:
            recipient = endpoint.recipient_email or org.notification_email
            if not recipient:
                raise RuntimeError("Email integration endpoint has no recipient.")
            send_mail(
                subject=f"SmartEndorse {request.reference} - {request.get_endorsement_type_display()}",
                message=(
                    f"New endorsement request {request.reference}\n"
                    f"Policy: {request.policy.policy_number}\n"
                    f"Type: {request.get_endorsement_type_display()}\n"
                    f"Effective date: {request.effective_date}\n"
                    f"Members: {request.items.count()}\n"
                    f"Premium impact: {request.currency} {request.premium_impact}\n"
                    f"Please log in to SmartEndorse to process the request."
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[recipient],
                fail_silently=False,
            )
        return endpoint


class WorkflowService:
    @staticmethod
    def transition(request, status, actor=None, description="", payload=None):
        old = request.status
        request.status = status
        request.current_sla_due_at = SLAService.for_status(request, status)
        if status == EndorsementRequest.Status.COMPLETED:
            request.completed_at = timezone.now()
            request.current_sla_due_at = None
        request.save(update_fields=["status", "current_sla_due_at", "completed_at", "updated_at"])
        WorkflowEvent.objects.create(
            request=request,
            actor=actor,
            event_type="STATUS_CHANGE",
            from_status=old,
            to_status=status,
            description=description,
            payload=payload or {},
        )

    @classmethod
    @transaction.atomic
    def submit(cls, request, actor=None):
        cls.transition(
            request,
            EndorsementRequest.Status.VALIDATING,
            actor,
            "Deterministic validation started.",
        )
        errors, score = ValidationService.validate(request)
        request.validation_errors = errors
        request.validation_score = score
        request.submitted_at = timezone.now()
        request.save(update_fields=[
            "validation_errors",
            "validation_score",
            "submitted_at",
            "updated_at",
        ])
        if errors:
            cls.transition(
                request,
                EndorsementRequest.Status.NEEDS_INFO,
                actor,
                "Validation requires additional or corrected information.",
                {"errors": errors},
            )
            return request

        PricingEngine.calculate_request(request)
        config = platform_config()
        request.stp_eligible = bool(config.auto_stp_enabled and request.policy.auto_stp)
        request.save(update_fields=["stp_eligible", "updated_at"])
        cls.transition(
            request,
            EndorsementRequest.Status.SUBMITTED,
            actor,
            "Request passed validation.",
        )

        if not request.stp_eligible:
            return request

        cls.transition(
            request,
            EndorsementRequest.Status.AUTO_APPROVED,
            None,
            "Straight-through rules passed. No insurer manual approval required.",
        )
        try:
            endpoint = DispatchService.dispatch(request)
            if request.policy.product == Policy.Product.GROUP_MEDICAL:
                cls.transition(
                    request,
                    EndorsementRequest.Status.SENT_TO_TPA,
                    None,
                    f"Automatically dispatched through {endpoint.get_transport_display()}.",
                )
            else:
                cls.transition(
                    request,
                    EndorsementRequest.Status.CORE_DISPATCHED,
                    None,
                    f"Automatically dispatched to insurer core integration through {endpoint.get_transport_display()}.",
                )
        except Exception as exc:
            request.validation_errors = [
                *request.validation_errors,
                f"Automatic dispatch failed: {exc}",
            ]
            request.save(update_fields=["validation_errors", "updated_at"])
            cls.transition(
                request,
                EndorsementRequest.Status.FAILED,
                None,
                "Automatic dispatch failed.",
                {"error": str(exc)},
            )
        return request

    @classmethod
    def start_processing(cls, request, actor):
        cls.transition(
            request,
            EndorsementRequest.Status.TPA_IN_PROGRESS,
            actor,
            "TPA processing started.",
        )

    @classmethod
    def complete(cls, request, actor, external_reference=""):
        if external_reference:
            request.external_reference = external_reference
            request.save(update_fields=["external_reference", "updated_at"])
        cls.transition(
            request,
            EndorsementRequest.Status.COMPLETED,
            actor,
            "Endorsement completed.",
        )
        cfg = platform_config()
        if cfg.enable_email_notifications and request.requester.email:
            send_mail(
                subject=f"Endorsement completed - {request.reference}",
                message=f"Your endorsement request {request.reference} for policy {request.policy.policy_number} has been completed.",
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[request.requester.email],
                fail_silently=True,
            )

    @classmethod
    def raise_query(cls, request, actor, subject, message):
        request.metadata = {
            **(request.metadata or {}),
            "pre_query_status": request.status,
        }
        request.save(update_fields=["metadata", "updated_at"])
        query = EndorsementQuery.objects.create(
            request=request,
            raised_by=actor,
            assigned_organization=request.requester_organization,
            subject=subject,
            message=message,
            due_at=SLAService.deadline(request.policy.client_query_sla),
        )
        cls.transition(
            request,
            EndorsementRequest.Status.TPA_QUERY,
            actor,
            "Additional information requested from requester.",
        )
        return query

    @classmethod
    def answer_query(cls, query, actor, response):
        query.response = response
        query.responded_by = actor
        query.responded_at = timezone.now()
        query.is_closed = True
        query.save(update_fields=[
            "response",
            "responded_by",
            "responded_at",
            "is_closed",
            "updated_at",
        ])
        request = query.request
        restore = (
            (request.metadata or {}).get("pre_query_status")
            or EndorsementRequest.Status.TPA_IN_PROGRESS
        )
        cls.transition(
            request,
            restore,
            actor,
            "Requester answered query.",
        )
