from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
import logging

import plotly.graph_objects as go
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .access import (
    accessible_endorsements, accessible_policies, can_create_endorsement,
    can_decide_approval, can_edit_request, can_insurer_operate, can_tpa_process,
)
from .forms import (
    ApprovalDecisionForm, BulkRecoveryForm, EndorsementCreateForm, EndorsementItemCorrectionForm,
    ItemOCRFillForm, QueryForm, QueryResponseForm, SupplementalUploadForm, TPAItemProcessingForm, UserProfileForm,
)
from .models import (
    Attachment, BOOTSWATCH_THEMES, EndorsementApproval, EndorsementItem,
    EndorsementRequest, Policy, PolicyPlan, PortalNotification, RecoveryUpload, UserProfile, WorkflowEvent,
)
from .services import BulkRecoveryService, FileIntakeService, PricingEngine, ValidationService, WorkflowService, json_safe


logger = logging.getLogger(__name__)


def _form_error_text(form):
    parts = []
    for field, errors in form.errors.items():
        label = form.fields.get(field).label if field in form.fields else field
        parts.extend(f"{label}: {error}" for error in errors)
    return "; ".join(parts) or "Invalid request."


def _chart_html(qs):
    statuses = Counter(qs.values_list("status", flat=True))
    status_map = dict(EndorsementRequest.Status.choices)
    labels = [status_map.get(key, key) for key in statuses]
    fig1 = go.Figure(data=[go.Bar(x=labels, y=list(statuses.values()), marker_color="#55e6b0")])
    fig1.update_layout(height=300, margin=dict(l=34, r=18, t=24, b=42), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", bargap=0.38)

    monthly = defaultdict(int)
    start = timezone.now() - timedelta(days=180)
    for created_at in qs.filter(created_at__gte=start).values_list("created_at", flat=True):
        monthly[created_at.strftime("%Y-%m")] += 1
    months = sorted(monthly)
    fig2 = go.Figure(data=[go.Scatter(x=months, y=[monthly[month] for month in months], mode="lines+markers", line=dict(color="#55e6b0", width=2), marker=dict(color="#f2b84b", size=7))])
    fig2.update_layout(height=300, margin=dict(l=34, r=18, t=24, b=42), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)")
    config = {"displayModeBar": False, "responsive": True}
    return fig1.to_html(full_html=False, include_plotlyjs=False, config=config), fig2.to_html(full_html=False, include_plotlyjs=False, config=config)


@login_required
def dashboard(request):
    qs = accessible_endorsements(request.user)
    total = qs.count()
    completed = qs.filter(status=EndorsementRequest.Status.COMPLETED).count()
    breached = sum(1 for item in qs.exclude(current_sla_due_at=None).exclude(status=EndorsementRequest.Status.COMPLETED) if item.sla_breached)
    stp = qs.filter(stp_eligible=True).count()
    premium = qs.aggregate(total=Sum("premium_impact"))["total"] or 0
    chart_status, chart_trend = _chart_html(qs)
    try:
        org_type = request.user.profile.organization.organization_type
    except Exception:
        org_type = ""
    tpa_queue = qs.filter(status__in=[
        EndorsementRequest.Status.SENT_TO_TPA,
        EndorsementRequest.Status.TPA_IN_PROGRESS,
        EndorsementRequest.Status.TPA_QUERY,
    ]).count()
    exceptions = qs.filter(status__in=[
        EndorsementRequest.Status.NEEDS_INFO,
        EndorsementRequest.Status.FAILED,
        EndorsementRequest.Status.PENDING_INSURER_APPROVAL,
        EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL,
    ]).count()
    return render(request, "dashboard.html", {
        "total": total, "completed": completed, "open_count": total - completed, "breached": breached,
        "stp_rate": round((stp / total * 100), 1) if total else 0, "premium": premium,
        "recent": qs.order_by("-created_at")[:10], "chart_status": chart_status, "chart_trend": chart_trend,
        "org_type": org_type, "policy_count": accessible_policies(request.user).count(),
        "can_create": can_create_endorsement(request.user), "tpa_queue": tpa_queue, "exceptions": exceptions,
        "completion_rate": round((completed / total * 100), 1) if total else 0,
        "exception_rate": round((exceptions / total * 100), 1) if total else 0,
        "tpa_queue_rate": round((tpa_queue / total * 100), 1) if total else 0,
    })


@login_required
def request_list(request):
    base_qs = accessible_endorsements(request.user)
    status = request.GET.get("status", "")
    product = request.GET.get("product", "")
    endorsement_type = request.GET.get("type", "")
    q = request.GET.get("q", "").strip()

    total = base_qs.count()
    completed = base_qs.filter(status=EndorsementRequest.Status.COMPLETED).count()
    needs_info = base_qs.filter(status__in=[EndorsementRequest.Status.NEEDS_INFO, EndorsementRequest.Status.TPA_QUERY]).count()
    pending_approval = base_qs.filter(status__in=[
        EndorsementRequest.Status.PENDING_INSURER_APPROVAL,
        EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL,
    ]).count()
    breached = sum(
        1 for item in base_qs.exclude(current_sla_due_at=None).exclude(status=EndorsementRequest.Status.COMPLETED)
        if item.sla_breached
    )
    premium = base_qs.aggregate(total=Sum("premium_impact"))["total"] or 0

    qs = base_qs.order_by("-created_at")
    if status:
        qs = qs.filter(status=status)
    if product:
        qs = qs.filter(policy__product=product)
    if endorsement_type:
        qs = qs.filter(endorsement_type=endorsement_type)
    if q:
        qs = qs.filter(
            Q(reference__icontains=q)
            | Q(policy__policy_number__icontains=q)
            | Q(policy__policy_name__icontains=q)
            | Q(policy__client__name__icontains=q)
            | Q(requester_organization__name__icontains=q)
            | Q(external_reference__icontains=q)
            | Q(items__full_name__icontains=q)
            | Q(items__member_no__icontains=q)
            | Q(items__employee_no__icontains=q)
            | Q(items__national_id__icontains=q)
        ).distinct()

    context = {
        "endorsements": qs[:200],
        "status_choices": EndorsementRequest.Status.choices,
        "product_choices": Policy.Product.choices,
        "type_choices": EndorsementRequest.Type.choices,
        "selected_status": status,
        "selected_product": product,
        "selected_type": endorsement_type,
        "q": q,
        "can_create": can_create_endorsement(request.user),
        "pipeline_kpis": {
            "total": total,
            "open": total - completed,
            "completed": completed,
            "needs_info": needs_info,
            "pending_approval": pending_approval,
            "breached": breached,
            "premium": premium,
        },
    }
    is_table_refresh = (
        request.headers.get("HX-Request", "").lower() == "true"
        and request.headers.get("HX-Target") == "request-table"
    )
    return render(
        request,
        "endorsements/_table.html" if is_table_refresh else "endorsements/list.html",
        context,
    )


@login_required
def policy_plans(request):
    policy_id = request.GET.get("policy")
    plans = PolicyPlan.objects.none()
    if policy_id and accessible_policies(request.user, require_create=True).filter(pk=policy_id).exists():
        plans = PolicyPlan.objects.filter(policy_id=policy_id, is_active=True).order_by("code")
    options = ['<option value="">Select plan</option>']
    for plan in plans:
        options.append(f'<option value="{plan.pk}">{plan.code} - {plan.name}</option>')
    return HttpResponse("".join(options))


@login_required
def policy_plan_sum_assured(request):
    plan_id = request.GET.get("plan")
    if not plan_id:
        return JsonResponse({"sum_assured": None})
    plan = PolicyPlan.objects.filter(pk=plan_id, policy__in=accessible_policies(request.user), is_active=True).first()
    if not plan:
        return JsonResponse({"sum_assured": None}, status=404)
    return JsonResponse({
        "sum_assured": str(plan.sum_assured) if plan.sum_assured is not None else None,
        "plan": f"{plan.code} - {plan.name}",
    })


@login_required
def bulk_recovery(request, pk):
    endorsement = _get_accessible_request(request.user, pk)
    if request.method != "POST":
        messages.error(request, "Bulk correction only accepts uploaded files.")
        return redirect("endorsement_detail", pk=pk)
    if not can_edit_request(request.user, endorsement):
        messages.error(request, "You do not have permission to correct this endorsement.")
        return redirect("endorsement_detail", pk=pk)
    if endorsement.status not in {EndorsementRequest.Status.NEEDS_INFO, EndorsementRequest.Status.TPA_QUERY}:
        messages.warning(request, "Bulk correction is only available while this endorsement is waiting for information.")
        return redirect("endorsement_detail", pk=pk)

    form = BulkRecoveryForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, f"Bulk correction could not be processed. {_form_error_text(form)}")
        return redirect("endorsement_detail", pk=pk)

    uploads = []
    try:
        for file_obj in form.cleaned_data["attachments"]:
            upload = RecoveryUpload.objects.create(
                uploaded_by=request.user,
                organization=request.user.profile.organization,
                file=file_obj,
                original_name=file_obj.name,
                kind=FileIntakeService.kind_for_name(file_obj.name),
            )
            BulkRecoveryService.process(upload, endorsement)
            uploads.append(upload)

        failed = [u for u in uploads if u.status == RecoveryUpload.Status.FAILED]
        if any(u.resolved_count for u in uploads):
            WorkflowService.revalidate_after_correction(endorsement, request.user)

        resolved = sum(u.resolved_count for u in uploads)
        ambiguous = sum(u.ambiguous_count for u in uploads)
        unmatched = sum(u.unmatched_count for u in uploads)

        if failed:
            details = "; ".join(f"{u.original_name}: {u.processing_error or 'processing failed'}" for u in failed)
            level = messages.warning if resolved else messages.error
            level(request, f"Bulk correction for {endorsement.reference} completed with errors. {details}")
        elif resolved:
            messages.success(
                request,
                f"Bulk correction applied only to {endorsement.reference}: {resolved} row(s) resolved; "
                f"{ambiguous} ambiguous and {unmatched} unmatched row(s) were left unchanged.",
            )
        else:
            messages.warning(
                request,
                f"No missing fields were safely resolved for {endorsement.reference}. "
                f"{ambiguous} ambiguous and {unmatched} unmatched row(s) were left unchanged.",
            )
    except Exception as exc:
        logger.exception("Bulk correction failed for endorsement %s", endorsement.pk)
        messages.error(request, f"Bulk correction failed for {endorsement.reference}: {exc}")
    return redirect("endorsement_detail", pk=pk)


@login_required
def create_request(request):
    if not can_create_endorsement(request.user):
        raise PermissionDenied
    policies = accessible_policies(request.user, require_create=True)
    if request.method == "POST":
        form = EndorsementCreateForm(request.POST, request.FILES, policies=policies)
        if form.is_valid():
            endorsement = form.save(commit=False)
            endorsement.requester = request.user
            endorsement.requester_organization = request.user.profile.organization
            endorsement.currency = endorsement.policy.currency
            endorsement.save()
            if form.has_manual_item():
                EndorsementItem.objects.create(request=endorsement, **form.manual_item_payload())

            created_attachments = []
            for upload in form.cleaned_data.get("attachments", []):
                created_attachments.append(Attachment.objects.create(
                    request=endorsement,
                    file=upload,
                    original_name=upload.name,
                    kind=FileIntakeService.kind_for_name(upload.name),
                ))

            evidence_attachments = [
                attachment for attachment in created_attachments
                if Path(attachment.original_name).suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
            ]
            structured_attachments = [
                attachment for attachment in created_attachments
                if attachment not in evidence_attachments
            ]

            for attachment in structured_attachments:
                FileIntakeService.process(attachment)

            if evidence_attachments:
                try:
                    FileIntakeService.process_initial_evidence_bundle(evidence_attachments)
                except Exception:
                    logger.exception("Initial evidence bundle failed for endorsement %s", endorsement.pk)

            WorkflowEvent.objects.create(
                request=endorsement, actor=request.user, event_type="REQUEST_CREATED",
                description="Endorsement intake created. Extracted records are ready for requester review before validation.",
            )
            messages.success(
                request,
                f"{endorsement.reference} intake created. Review extracted/member data, add or remove rows if required, then use Validate & Submit.",
            )
            return redirect("endorsement_detail", pk=endorsement.pk)
    else:
        form = EndorsementCreateForm(policies=policies)
    return render(request, "endorsements/form.html", {"form": form})


def _get_accessible_request(user, pk):
    return get_object_or_404(accessible_endorsements(user).prefetch_related("items__plan", "attachments", "events", "queries", "approvals__assigned_organization"), pk=pk)


def _wizard(endorsement):
    stages = [
        ("Intake", {EndorsementRequest.Status.DRAFT, EndorsementRequest.Status.NEEDS_INFO, EndorsementRequest.Status.REJECTED}),
        ("Validated", {EndorsementRequest.Status.SUBMITTED, EndorsementRequest.Status.PENDING_INSURER_APPROVAL}),
        ("Approved", {EndorsementRequest.Status.AUTO_APPROVED, EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL}),
        ("TPA / Core", {EndorsementRequest.Status.SENT_TO_TPA, EndorsementRequest.Status.TPA_IN_PROGRESS, EndorsementRequest.Status.TPA_QUERY, EndorsementRequest.Status.CORE_DISPATCHED}),
        ("Completed", {EndorsementRequest.Status.COMPLETED}),
    ]
    index = next((i for i, (_, states) in enumerate(stages) if endorsement.status in states), 0)
    return [{"label": label, "state": "done" if i < index else "active" if i == index else "pending"} for i, (label, _) in enumerate(stages)]


def _workflow_sla_rows(endorsement):
    now = timezone.now()
    events = list(endorsement.events.filter(event_type="STATUS_CHANGE").order_by("created_at"))
    stage_defs = [
        ("Intake", {EndorsementRequest.Status.DRAFT, EndorsementRequest.Status.NEEDS_INFO, EndorsementRequest.Status.REJECTED}, endorsement.policy.client_query_sla),
        ("Validation", {EndorsementRequest.Status.VALIDATING, EndorsementRequest.Status.SUBMITTED}, endorsement.policy.insurer_sla),
        ("Insurance approval", {EndorsementRequest.Status.PENDING_INSURER_APPROVAL, EndorsementRequest.Status.AUTO_APPROVED, EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL}, endorsement.policy.insurer_sla),
        ("TPA / Core processing", {EndorsementRequest.Status.SENT_TO_TPA, EndorsementRequest.Status.TPA_IN_PROGRESS, EndorsementRequest.Status.TPA_QUERY, EndorsementRequest.Status.CORE_DISPATCHED}, endorsement.policy.tpa_sla if endorsement.policy.product == Policy.Product.GROUP_MEDICAL else endorsement.policy.insurer_sla),
        ("Completed", {EndorsementRequest.Status.COMPLETED}, None),
    ]
    transition_times = {}
    for event in events:
        transition_times.setdefault(event.to_status, event.created_at)
    rows = []
    cursor = endorsement.created_at
    total_target = 0
    for label, statuses, profile in stage_defs[:-1]:
        entered = min([transition_times[x] for x in statuses if x in transition_times], default=(endorsement.created_at if label == "Intake" else None))
        later = [event.created_at for event in events if entered and event.created_at > entered and event.to_status not in statuses]
        ended = min(later) if later else (endorsement.completed_at if endorsement.completed_at else now if endorsement.status in statuses else None)
        actual_hours = round(((ended - entered).total_seconds() / 3600), 1) if entered and ended else None
        target_hours = profile.target_hours if profile else (24 if label == "Intake" else 8 if label == "Validation" else 24)
        total_target += target_hours
        rows.append({
            "label": label,
            "target_hours": target_hours,
            "actual_hours": actual_hours,
            "breached": actual_hours is not None and actual_hours > target_hours,
            "active": endorsement.status in statuses,
        })
        if entered:
            cursor = entered
    total_actual = round((((endorsement.completed_at or now) - endorsement.created_at).total_seconds() / 3600), 1)
    rows.append({
        "label": "Total",
        "target_hours": total_target,
        "actual_hours": total_actual,
        "breached": total_actual > total_target,
        "active": endorsement.status != EndorsementRequest.Status.COMPLETED,
    })
    return rows


@login_required
def request_detail(request, pk):
    endorsement = _get_accessible_request(request.user, pk)
    items = list(endorsement.items.all())
    item_kpis = {
        "total": len(items),
        "correct": sum(i.validation_status == EndorsementItem.ValidationStatus.VALID for i in items),
        "errors": sum(i.validation_status == EndorsementItem.ValidationStatus.ERROR for i in items),
        "existing": sum(i.is_existing_record for i in items),
        "approval": sum(i.validation_status == EndorsementItem.ValidationStatus.APPROVAL_REQUIRED for i in items),
    }
    approvals = list(endorsement.approvals.all())
    for approval in approvals:
        approval.can_decide_for_user = can_decide_approval(request.user, approval)

    member_issues = []
    known_validation_errors = set()
    for item in items:
        member_label = item.full_name or item.member_no or item.employee_no or item.national_id or f"Item {item.pk}"
        for error in item.validation_errors or []:
            member_issues.append({
                "item": item,
                "member_label": member_label,
                "error": error,
            })
            known_validation_errors.add(f'Item {item.pk}: {error}')
            known_validation_errors.add(f'Member "{member_label}" (Item {item.pk}): {error}')

    document_issues = []
    for attachment in endorsement.attachments.all():
        if not attachment.processing_error:
            continue
        payload = attachment.extracted_payload if isinstance(attachment.extracted_payload, dict) else {}
        optional_ocr = payload.get("usage") == "item_ocr_preview"
        document_issues.append({
            "attachment": attachment,
            "error": attachment.processing_error,
            "optional_ocr": optional_ocr,
            "blocking": not optional_ocr,
        })
        known_validation_errors.add(f"{attachment.original_name}: {attachment.processing_error}")
        known_validation_errors.add(
            f'Document "{attachment.original_name}" could not be processed: {attachment.processing_error}'
        )

    request_issues = [
        error for error in (endorsement.validation_errors or [])
        if error not in known_validation_errors
    ]

    resolution_rows = []
    for event in endorsement.events.filter(
        event_type__in=[
            "ITEM_RECOVERED", "ITEM_MANUALLY_CORRECTED", "SUPPLEMENTAL_ROW_CREATED",
            "BULK_ITEM_RECOVERED", "VALIDATION_ROW_CREATED", "VALIDATION_ITEM_DELETED",
        ]
    ):
        payload = event.payload if isinstance(event.payload, dict) else {}
        resolution_rows.append({
            "created_at": event.created_at,
            "description": event.description or event.event_type,
            "before_or_source": payload.get("before") or payload.get("source") or "—",
            "after_or_filled": payload.get("after") or payload.get("filled") or "—",
        })

    return render(request, "endorsements/detail.html", {
        "endorsement": endorsement, "query_form": QueryForm(), "response_form": QueryResponseForm(),
        "supplemental_form": SupplementalUploadForm(), "bulk_recovery_form": BulkRecoveryForm(),
        "approval_form": ApprovalDecisionForm(),
        "item_kpis": item_kpis, "wizard_steps": _wizard(endorsement), "approvals": approvals,
        "sla_rows": _workflow_sla_rows(endorsement),
        "add_item_form": EndorsementItemCorrectionForm(instance=EndorsementItem(request=endorsement, effective_date=endorsement.effective_date)),
        "resolution_rows": resolution_rows,
        "member_issues": member_issues,
        "document_issues": document_issues,
        "request_issues": request_issues,
        "has_blocking_document_issues": any(issue["blocking"] for issue in document_issues),
        "can_edit": can_edit_request(request.user, endorsement),
        "can_delete_items": can_edit_request(request.user, endorsement) and endorsement.status in {
            EndorsementRequest.Status.DRAFT, EndorsementRequest.Status.NEEDS_INFO, EndorsementRequest.Status.REJECTED,
        },
        "can_tpa_process": can_tpa_process(request.user, endorsement),
        "can_insurer_operate": can_insurer_operate(request.user, endorsement),
    })


@login_required
def retry_failed_evidence_bundle(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)

    endorsement = _get_accessible_request(request.user, pk)
    if not can_edit_request(request.user, endorsement):
        raise PermissionDenied
    if endorsement.status not in {
        EndorsementRequest.Status.DRAFT,
        EndorsementRequest.Status.NEEDS_INFO,
        EndorsementRequest.Status.TPA_QUERY,
    }:
        messages.error(request, "Failed documents can only be retried while the endorsement is in validation/correction.")
        return redirect("endorsement_detail", pk=pk)

    failed = []
    for attachment in endorsement.attachments.exclude(processing_error=""):
        if Path(attachment.original_name).suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
            continue
        payload = attachment.extracted_payload if isinstance(attachment.extracted_payload, dict) else {}
        if payload.get("usage") == "item_ocr_preview":
            continue
        failed.append(attachment)
    if not failed:
        messages.info(request, "There are no failed PDF/image documents to retry.")
        return redirect("endorsement_detail", pk=pk)

    try:
        if endorsement.items.exists():
            result = FileIntakeService.process_supplemental_evidence_bundle(failed)
        else:
            result = FileIntakeService.process_initial_evidence_bundle(failed)

        WorkflowService.revalidate_after_correction(endorsement, request.user)
        messages.success(
            request,
            f"{result['processed']} failed document(s) were retried together as one evidence bundle; "
            f"{result['rows']} member row(s) were identified.",
        )
    except Exception as exc:
        logger.exception("Failed evidence bundle retry failed for endorsement %s", endorsement.pk)
        messages.error(request, f"Failed document bundle could not be recovered: {exc}")

    return redirect("endorsement_detail", pk=pk)


@login_required
def revalidate_request(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    if not can_edit_request(request.user, endorsement):
        raise PermissionDenied
    if endorsement.status not in {
        EndorsementRequest.Status.DRAFT,
        EndorsementRequest.Status.NEEDS_INFO,
        EndorsementRequest.Status.TPA_QUERY,
        EndorsementRequest.Status.REJECTED,
    }:
        messages.warning(request, "Validation is only available while the endorsement is in intake/correction.")
        return redirect("endorsement_detail", pk=pk)

    WorkflowService.revalidate_after_correction(endorsement, request.user)
    endorsement.refresh_from_db()
    if endorsement.validation_errors:
        messages.warning(request, f"Revalidation completed with {len(endorsement.validation_errors)} blocking issue(s).")
    else:
        messages.success(request, "Revalidation passed. No blocking validation issues remain.")
    return redirect("endorsement_detail", pk=pk)


@login_required
def remove_failed_attachment(request, pk, attachment_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    if not can_edit_request(request.user, endorsement):
        raise PermissionDenied
    if endorsement.status not in {
        EndorsementRequest.Status.DRAFT,
        EndorsementRequest.Status.NEEDS_INFO,
        EndorsementRequest.Status.TPA_QUERY,
    }:
        messages.error(request, "Failed documents can only be removed while the endorsement is in validation/correction.")
        return redirect("endorsement_detail", pk=pk)

    attachment = get_object_or_404(endorsement.attachments, pk=attachment_id)
    if not attachment.processing_error:
        messages.warning(request, "Only a failed document can be removed from this screen.")
        return redirect("endorsement_detail", pk=pk)

    name = attachment.original_name
    attachment.delete()
    WorkflowEvent.objects.create(
        request=endorsement,
        actor=request.user,
        event_type="FAILED_ATTACHMENT_REMOVED",
        description=f"Failed document {name} removed during validation.",
        payload={"file_name": name},
    )
    WorkflowService.revalidate_after_correction(endorsement, request.user)
    messages.success(request, f"{name} was removed and the endorsement was revalidated.")
    return redirect("endorsement_detail", pk=pk)


@login_required
def edit_item(request, pk, item_id):
    endorsement = _get_accessible_request(request.user, pk)
    if not can_edit_request(request.user, endorsement):
        raise PermissionDenied
    item = get_object_or_404(endorsement.items.select_related("plan", "request__policy"), pk=item_id)
    ocr_form = ItemOCRFillForm()
    ocr_source = None
    ocr_preview = None

    if request.method == "POST" and request.POST.get("action") == "ocr_fill":
        ocr_form = ItemOCRFillForm(request.POST, request.FILES)
        form = EndorsementItemCorrectionForm(instance=item)
        if ocr_form.is_valid():
            attachments = []
            for upload in ocr_form.cleaned_data["ocr_files"]:
                attachments.append(Attachment.objects.create(
                    request=endorsement,
                    file=upload,
                    original_name=upload.name,
                    kind=FileIntakeService.kind_for_name(upload.name),
                    is_supplemental=True,
                    extracted_payload={
                        "usage": "item_ocr_preview",
                        "target_item_id": item.pk,
                        "evidence_bundle": True,
                    },
                ))
            try:
                raw_rows, normalized_rows, metadata = FileIntakeService.extract_item_form_fields_group(attachments)
                target_row = None
                if len(normalized_rows) == 1:
                    target_row = normalized_rows[0]
                else:
                    for row in normalized_rows:
                        matched, _, _ = FileIntakeService._candidate_match(endorsement, row)
                        if matched and matched.pk == item.pk:
                            target_row = row
                            break
                if not target_row:
                    raise ValueError("The document contains multiple members or could not be matched uniquely to this item.")

                values = FileIntakeService._to_item_values(endorsement, target_row)
                initial = {
                    "member_no": item.member_no,
                    "employee_no": item.employee_no,
                    "national_id": item.national_id,
                    "full_name": item.full_name,
                    "relationship": item.relationship,
                    "date_of_birth": item.date_of_birth,
                    "gender": item.gender,
                    "plan": item.plan_id,
                    "sum_assured": item.sum_assured,
                    "effective_date": item.effective_date,
                }
                for field in initial:
                    value = values.get(field)
                    if value not in (None, ""):
                        initial[field] = value.pk if field == "plan" and value else value
                if values.get("plan"):
                    initial["sum_assured"] = values["plan"].sum_assured

                form = EndorsementItemCorrectionForm(instance=item, initial=initial)
                bundle_payload = json_safe({
                    "raw_rows": raw_rows,
                    "normalized_rows": normalized_rows,
                    **metadata,
                    "usage": "item_ocr_preview",
                    "ocr_fill_target_item": item.pk,
                    "evidence_bundle": True,
                })
                for attachment in attachments:
                    attachment.extracted_payload = {
                        **bundle_payload,
                        "source_file": attachment.original_name,
                    }
                    attachment.processed = True
                    attachment.processing_error = ""
                    attachment.save(update_fields=["extracted_payload", "processed", "processing_error", "updated_at"])
                source_names = [attachment.original_name for attachment in attachments]
                WorkflowEvent.objects.create(
                    request=endorsement,
                    actor=request.user,
                    event_type="ITEM_OCR_PREVIEW",
                    description=f"OCR extracted correction values for item {item.pk} from {len(attachments)} evidence file(s).",
                    payload={"item_id": item.pk, "sources": source_names, "extracted": json_safe(values)},
                )
                ocr_source = ", ".join(source_names)
                ocr_preview = values
                messages.info(
                    request,
                    "Document fields were read directly into the form. Review the values, then click Save & revalidate.",
                )
            except Exception as exc:
                for attachment in attachments:
                    attachment.processing_error = str(exc)
                    attachment.processed = False
                    attachment.save(update_fields=["processing_error", "processed", "updated_at"])
                messages.error(request, f"OCR extraction failed: {exc}")
        return render(request, "endorsements/item_edit.html", {
            "endorsement": endorsement, "item": item, "form": form, "ocr_form": ocr_form,
            "ocr_source": ocr_source, "ocr_preview": ocr_preview,
        })

    if request.method == "POST":
        before = {
            f: json_safe(getattr(item, f + "_id") if f == "plan" else getattr(item, f))
            for f in ["member_no", "employee_no", "national_id", "full_name", "relationship", "date_of_birth", "gender", "plan", "sum_assured", "effective_date"]
        }
        form = EndorsementItemCorrectionForm(request.POST, instance=item)
        if form.is_valid():
            item = form.save()
            WorkflowEvent.objects.create(
                request=endorsement,
                actor=request.user,
                event_type="ITEM_MANUALLY_CORRECTED",
                description=f"Item {item.pk} corrected manually.",
                payload={"item_id": item.pk, "before": before, "after": {k: json_safe(v) for k, v in form.cleaned_data.items()}},
            )
            WorkflowService.revalidate_after_correction(endorsement, request.user)
            messages.success(request, "Item updated and the endorsement was revalidated.")
            return redirect("endorsement_detail", pk=pk)
    else:
        form = EndorsementItemCorrectionForm(instance=item)

    return render(request, "endorsements/item_edit.html", {
        "endorsement": endorsement, "item": item, "form": form, "ocr_form": ocr_form,
        "ocr_source": ocr_source, "ocr_preview": ocr_preview,
    })


@login_required
def delete_item(request, pk, item_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    if not can_edit_request(request.user, endorsement):
        raise PermissionDenied
    if endorsement.status not in {EndorsementRequest.Status.DRAFT, EndorsementRequest.Status.NEEDS_INFO}:
        messages.error(request, "Member rows can only be deleted while the endorsement is in validation.")
        return redirect("endorsement_detail", pk=pk)

    item = get_object_or_404(endorsement.items, pk=item_id)
    snapshot = {
        "item_id": item.pk,
        "member_no": item.member_no,
        "employee_no": item.employee_no,
        "national_id": item.national_id,
        "full_name": item.full_name,
    }
    deleted_id = item.pk
    item.delete()
    WorkflowEvent.objects.create(
        request=endorsement,
        actor=request.user,
        event_type="VALIDATION_ITEM_DELETED",
        description=f"Item {deleted_id} deleted during validation.",
        payload=snapshot,
    )
    WorkflowService.revalidate_after_correction(endorsement, request.user)
    messages.success(request, f"Member row {deleted_id} deleted and the endorsement was revalidated.")
    return redirect("endorsement_detail", pk=pk)


@login_required
def supplemental_upload(request, pk):
    endorsement = _get_accessible_request(request.user, pk)
    if request.method != "POST":
        messages.error(request, "Supplemental correction only accepts uploaded files.")
        return redirect("endorsement_detail", pk=pk)
    if not can_edit_request(request.user, endorsement):
        messages.error(request, "You do not have permission to correct this endorsement.")
        return redirect("endorsement_detail", pk=pk)
    if endorsement.status not in {EndorsementRequest.Status.NEEDS_INFO, EndorsementRequest.Status.TPA_QUERY}:
        messages.warning(request, "Validation correction is only available while this endorsement is waiting for corrected information.")
        return redirect("endorsement_detail", pk=pk)

    form = SupplementalUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, f"Supplemental upload could not be processed. {_form_error_text(form)}")
        return redirect("endorsement_detail", pk=pk)

    processed = 0
    failures = []
    warnings = []
    try:
        created_attachments = []
        for upload in form.cleaned_data["attachments"]:
            created_attachments.append(Attachment.objects.create(
                request=endorsement,
                file=upload,
                original_name=upload.name,
                kind=FileIntakeService.kind_for_name(upload.name),
                is_supplemental=True,
            ))

        evidence_attachments = [
            attachment for attachment in created_attachments
            if Path(attachment.original_name).suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
        ]
        structured_attachments = [
            attachment for attachment in created_attachments
            if attachment not in evidence_attachments
        ]

        for attachment in structured_attachments:
            if FileIntakeService.process(attachment):
                processed += 1
            else:
                failures.append(f"{attachment.original_name}: {attachment.processing_error or 'extraction failed'}")

        if evidence_attachments:
            try:
                bundle_result = FileIntakeService.process_supplemental_evidence_bundle(evidence_attachments)
                processed += bundle_result["processed"]
                warnings.extend(
                    f"{item['file_name']}: {item['error']}"
                    for item in bundle_result.get("warnings", [])
                )
            except Exception as exc:
                failures.extend(
                    f"{attachment.original_name}: {attachment.processing_error or str(exc)}"
                    for attachment in evidence_attachments
                )

        if processed:
            WorkflowService.revalidate_after_correction(endorsement, request.user)

        if failures:
            text = "; ".join(failures)
            if processed:
                messages.warning(request, f"{processed} validation file(s) were processed, but some failed: {text}")
            else:
                messages.error(request, f"Validation upload failed for {endorsement.reference}: {text}")
        elif warnings:
            messages.warning(
                request,
                f"{processed} validation file(s) were applied as one evidence bundle. "
                f"Some files could not be read, but the remaining evidence was sufficient: {'; '.join(warnings)}",
            )
        else:
            messages.success(
                request,
                f"{processed} validation file(s) processed. PDF/image files uploaded together were treated as one evidence bundle, so front/back pages can complement each other.",
            )
    except Exception as exc:
        logger.exception("Supplemental upload failed for endorsement %s", endorsement.pk)
        messages.error(request, f"Supplemental upload failed for {endorsement.reference}: {exc}")
    return redirect("endorsement_detail", pk=pk)


@login_required
def start_processing(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    if not (can_tpa_process(request.user, endorsement) or can_insurer_operate(request.user, endorsement)):
        raise PermissionDenied
    try:
        WorkflowService.start_processing(endorsement, request.user)
        messages.success(request, "Processing started.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("endorsement_detail", pk=pk)


@login_required
def tpa_update_item(request, pk, item_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    if not can_tpa_process(request.user, endorsement):
        raise PermissionDenied
    item = get_object_or_404(endorsement.items, pk=item_id)
    form = TPAItemProcessingForm(request.POST, item=item)
    if form.is_valid():
        approval = WorkflowService.update_tpa_item(item, request.user, form.cleaned_data["card_number"], form.cleaned_data["effective_date"], form.cleaned_data["amount"])
        messages.warning(request, "Amount changed; approval was raised and relevant parties were notified.") if approval else messages.success(request, "TPA member processing data updated.")
    else:
        messages.error(request, "Card number and amount are required.")
    return redirect("endorsement_detail", pk=pk)


@login_required
def decide_approval(request, pk, approval_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    approval = get_object_or_404(endorsement.approvals, pk=approval_id, status=EndorsementApproval.Status.PENDING)
    if not can_decide_approval(request.user, approval):
        raise PermissionDenied
    form = ApprovalDecisionForm(request.POST)
    if form.is_valid():
        approving = form.cleaned_data["decision"] == "approve"
        comment = form.cleaned_data["comment"].strip()
        if not approving and not comment:
            messages.error(request, "Rejection reason is mandatory. Explain what must be corrected before resubmission.")
            return redirect("endorsement_detail", pk=pk)
        WorkflowService.decide_approval(approval, request.user, approving, comment)
        messages.success(request, "Approval decision recorded with full audit history.")
    return redirect("endorsement_detail", pk=pk)


@login_required
def complete_request(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    allowed = can_tpa_process(request.user, endorsement) if endorsement.policy.product == Policy.Product.GROUP_MEDICAL else can_insurer_operate(request.user, endorsement)
    if not allowed:
        raise PermissionDenied
    try:
        WorkflowService.complete(endorsement, request.user, request.POST.get("external_reference", "").strip())
        messages.success(request, "Endorsement completed and all relevant parties were notified.")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect("endorsement_detail", pk=pk)


@login_required
def raise_query(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    if not (can_tpa_process(request.user, endorsement) or can_insurer_operate(request.user, endorsement)):
        raise PermissionDenied
    form = QueryForm(request.POST)
    if form.is_valid():
        WorkflowService.raise_query(endorsement, request.user, form.cleaned_data["subject"], form.cleaned_data["message"])
        messages.success(request, "Query raised and requester notified.")
    else:
        messages.error(request, "Please provide a subject and message.")
    return redirect("endorsement_detail", pk=pk)


@login_required
def answer_query(request, pk, query_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    query = get_object_or_404(endorsement.queries, pk=query_id, is_closed=False)
    if request.user.profile.organization_id != query.assigned_organization_id and not request.user.is_superuser:
        raise PermissionDenied
    form = QueryResponseForm(request.POST)
    if form.is_valid():
        WorkflowService.answer_query(query, request.user, form.cleaned_data["response"])
        messages.success(request, "Response submitted.")
    return redirect("endorsement_detail", pk=pk)


@login_required
def add_item(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    if not can_edit_request(request.user, endorsement):
        raise PermissionDenied
    if endorsement.status not in {
        EndorsementRequest.Status.DRAFT, EndorsementRequest.Status.NEEDS_INFO,
        EndorsementRequest.Status.REJECTED, EndorsementRequest.Status.TPA_QUERY,
    }:
        messages.error(request, "Members can only be added during intake/correction.")
        return redirect("endorsement_detail", pk=pk)
    instance = EndorsementItem(request=endorsement, effective_date=endorsement.effective_date, extracted_data={"source": "manual_entry"})
    form = EndorsementItemCorrectionForm(request.POST, instance=instance)
    if form.is_valid():
        item = form.save()
        WorkflowEvent.objects.create(
            request=endorsement, actor=request.user, event_type="MANUAL_ITEM_ADDED",
            description=f"Member item {item.pk} added manually during intake.",
            payload={"item_id": item.pk, "full_name": item.full_name, "member_no": item.member_no},
        )
        messages.success(request, "Member added. Review the intake and run Validate & Submit when ready.")
    else:
        messages.error(request, f"Member could not be added. {_form_error_text(form)}")
    return redirect("endorsement_detail", pk=pk)


@login_required
def profile(request):
    profile_obj = request.user.profile
    form = UserProfileForm(request.POST or None, request.FILES or None, instance=profile_obj, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile updated successfully.")
        return redirect("profile")
    return render(request, "profile.html", {"form": form})


@login_required
def set_preferences(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    profile = request.user.profile
    theme = request.POST.get("theme", profile.portal_theme)
    mode = request.POST.get("color_mode", profile.color_mode)
    valid_themes = {value for value, _ in BOOTSWATCH_THEMES}
    valid_modes = {value for value, _ in UserProfile.ColorMode.choices}
    if theme in valid_themes:
        profile.portal_theme = theme
    if mode in valid_modes:
        profile.color_mode = mode
    profile.save(update_fields=["portal_theme", "color_mode", "updated_at"])
    return HttpResponse(status=204)


@login_required
def notifications_panel(request):
    notifications = request.user.portal_notifications.all()[:10]
    unread = request.user.portal_notifications.filter(is_read=False).count()
    return render(request, "partials/_notifications.html", {"notifications": notifications, "unread_notifications": unread})


@login_required
def notifications_read(request):
    if request.method != "POST":
        return HttpResponse(status=405)
    request.user.portal_notifications.filter(is_read=False).update(is_read=True)
    notifications = request.user.portal_notifications.all()[:10]
    return render(request, "partials/_notifications.html", {
        "notifications": notifications,
        "unread_notifications": 0,
    })
