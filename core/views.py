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
    ItemOCRFillForm, ProfileForm, QueryForm, QueryResponseForm, SupplementalUploadForm, TPAItemProcessingForm,
)
from .models import (
    Attachment, BOOTSWATCH_THEMES, EndorsementApproval, EndorsementItem,
    EndorsementRequest, Policy, PolicyPlan, PortalNotification, RecoveryUpload, UserProfile, WorkflowEvent,
)
from .services import BulkRecoveryService, FileIntakeService, PricingEngine, SLAService, ValidationService, WorkflowService, json_safe


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
    completed_qs = qs.filter(status=EndorsementRequest.Status.COMPLETED)
    completed = completed_qs.count()
    now = timezone.now()
    breached = sum(
        1 for item in qs.exclude(current_sla_due_at=None).exclude(status=EndorsementRequest.Status.COMPLETED)
        if item.sla_breached
    )
    due_soon = qs.exclude(current_sla_due_at=None).exclude(status=EndorsementRequest.Status.COMPLETED).filter(
        current_sla_due_at__gt=now,
        current_sla_due_at__lte=now + timedelta(hours=4),
    ).count()
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
        EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL,
    ]).count()
    approval_queue = qs.filter(status=EndorsementRequest.Status.PENDING_INSURER_APPROVAL).count()
    correction_queue = qs.filter(status__in=[
        EndorsementRequest.Status.NEEDS_INFO,
        EndorsementRequest.Status.REJECTED,
        EndorsementRequest.Status.TPA_QUERY,
    ]).count()
    exceptions = qs.filter(status__in=[
        EndorsementRequest.Status.NEEDS_INFO,
        EndorsementRequest.Status.REJECTED,
        EndorsementRequest.Status.FAILED,
        EndorsementRequest.Status.PENDING_INSURER_APPROVAL,
        EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL,
        EndorsementRequest.Status.TPA_QUERY,
    ]).count()

    completed_hours = [
        max((item.completed_at - item.created_at).total_seconds() / 3600, 0)
        for item in completed_qs
        if item.completed_at
    ]
    average_tat_hours = round(sum(completed_hours) / len(completed_hours), 1) if completed_hours else 0
    within_sla = 0
    for item in completed_qs:
        profiles = [
            item.policy.intake_sla or item.policy.client_query_sla,
            item.policy.validation_sla or item.policy.insurer_sla,
            item.policy.insurer_sla,
            item.policy.tpa_sla if item.policy.product == Policy.Product.GROUP_MEDICAL else item.policy.insurer_sla,
        ]
        target = sum(profile.target_hours for profile in profiles if profile)
        if target and item.completed_at and (item.completed_at - item.created_at).total_seconds() <= target * 3600:
            within_sla += 1
    sla_compliance_rate = round((within_sla / completed * 100), 1) if completed else 0

    return render(request, "dashboard.html", {
        "total": total,
        "completed": completed,
        "open_count": total - completed,
        "breached": breached,
        "due_soon": due_soon,
        "stp_rate": round((stp / total * 100), 1) if total else 0,
        "premium": premium,
        "recent": qs.order_by("-created_at")[:10],
        "chart_status": chart_status,
        "chart_trend": chart_trend,
        "org_type": org_type,
        "policy_count": accessible_policies(request.user).count(),
        "can_create": can_create_endorsement(request.user),
        "tpa_queue": tpa_queue,
        "approval_queue": approval_queue,
        "correction_queue": correction_queue,
        "exceptions": exceptions,
        "completion_rate": round((completed / total * 100), 1) if total else 0,
        "exception_rate": round((exceptions / total * 100), 1) if total else 0,
        "tpa_queue_rate": round((tpa_queue / total * 100), 1) if total else 0,
        "average_tat_hours": average_tat_hours,
        "sla_compliance_rate": sla_compliance_rate,
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
            endorsement.status = EndorsementRequest.Status.DRAFT
            endorsement.save()
            endorsement.current_sla_due_at = SLAService.for_status(endorsement, EndorsementRequest.Status.DRAFT)
            endorsement.save(update_fields=["current_sla_due_at", "updated_at"])

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
                request=endorsement,
                actor=request.user,
                event_type="REQUEST_CREATED",
                description="Endorsement draft created. Intake is ready for member/file review before validation.",
            )
            messages.success(
                request,
                f"{endorsement.reference} created as Draft. Review the extracted members and files in Intake, make any corrections, then continue to Validation.",
            )
            return redirect(f"/endorsements/{endorsement.pk}/?step=intake")
    else:
        form = EndorsementCreateForm(policies=policies)
    return render(request, "endorsements/form.html", {"form": form})

def _get_accessible_request(user, pk):
    return get_object_or_404(accessible_endorsements(user).prefetch_related("items__plan", "attachments", "events", "queries", "approvals__assigned_organization"), pk=pk)


def _status_stage_key(endorsement):
    status = endorsement.status
    if status in {
        EndorsementRequest.Status.DRAFT,
        EndorsementRequest.Status.NEEDS_INFO,
        EndorsementRequest.Status.REJECTED,
    }:
        return "intake"
    if status in {
        EndorsementRequest.Status.VALIDATING,
        EndorsementRequest.Status.SUBMITTED,
    }:
        return "validation"
    if status in {
        EndorsementRequest.Status.PENDING_INSURER_APPROVAL,
        EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL,
        EndorsementRequest.Status.AUTO_APPROVED,
    }:
        return "approval"
    if status in {
        EndorsementRequest.Status.SENT_TO_TPA,
        EndorsementRequest.Status.TPA_IN_PROGRESS,
        EndorsementRequest.Status.TPA_QUERY,
        EndorsementRequest.Status.READY_FOR_CORE,
        EndorsementRequest.Status.CORE_DISPATCHED,
        EndorsementRequest.Status.FAILED,
    }:
        return "tpa"
    if status == EndorsementRequest.Status.COMPLETED:
        return "completed"
    return "intake"


def _wizard(endorsement):
    is_medical = endorsement.policy.product == Policy.Product.GROUP_MEDICAL
    definitions = [
        ("intake", "Intake", endorsement.policy.intake_sla or endorsement.policy.client_query_sla),
        ("validation", "Validation", endorsement.policy.validation_sla or endorsement.policy.insurer_sla),
        ("approval", "Insurer Approval", endorsement.policy.insurer_sla),
        ("tpa", "TPA" if is_medical else "Operations", endorsement.policy.tpa_sla if is_medical else endorsement.policy.insurer_sla),
        ("completed", "Completed", None),
    ]
    keys = [row[0] for row in definitions]
    active_key = _status_stage_key(endorsement)
    active_index = keys.index(active_key)

    stage_for_status = {
        EndorsementRequest.Status.DRAFT: "intake",
        EndorsementRequest.Status.NEEDS_INFO: "intake",
        EndorsementRequest.Status.REJECTED: "intake",
        EndorsementRequest.Status.VALIDATING: "validation",
        EndorsementRequest.Status.SUBMITTED: "validation",
        EndorsementRequest.Status.PENDING_INSURER_APPROVAL: "approval",
        EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL: "approval",
        EndorsementRequest.Status.AUTO_APPROVED: "approval",
        EndorsementRequest.Status.SENT_TO_TPA: "tpa",
        EndorsementRequest.Status.TPA_IN_PROGRESS: "tpa",
        EndorsementRequest.Status.TPA_QUERY: "tpa",
        EndorsementRequest.Status.READY_FOR_CORE: "tpa",
        EndorsementRequest.Status.CORE_DISPATCHED: "tpa",
        EndorsementRequest.Status.FAILED: "tpa",
        EndorsementRequest.Status.COMPLETED: "completed",
    }
    durations = {key: 0.0 for key in keys}
    current_status = EndorsementRequest.Status.DRAFT
    cursor = endorsement.created_at
    end_time = endorsement.completed_at or timezone.now()
    for event in endorsement.events.filter(event_type="STATUS_CHANGE").order_by("created_at"):
        if event.created_at < cursor:
            continue
        stage_key = stage_for_status.get(current_status, "intake")
        durations[stage_key] += max((event.created_at - cursor).total_seconds() / 3600, 0)
        current_status = event.to_status or current_status
        cursor = event.created_at
    if cursor < end_time:
        durations[stage_for_status.get(current_status, active_key)] += max((end_time - cursor).total_seconds() / 3600, 0)

    steps = []
    for index, (key, label, profile) in enumerate(definitions):
        required = profile.target_hours if profile else None
        actual = round(durations.get(key, 0.0), 1)
        if endorsement.status == EndorsementRequest.Status.COMPLETED:
            state = "done" if key != "completed" else "active"
        else:
            state = "done" if index < active_index else "active" if index == active_index else "pending"
        steps.append({
            "key": key,
            "label": label,
            "state": state,
            "required_hours": required,
            "actual_hours": actual,
            "breached": bool(required is not None and actual > required),
        })

    target_total = sum(step["required_hours"] or 0 for step in steps if step["key"] != "completed")
    actual_total = round(max(((endorsement.completed_at or timezone.now()) - endorsement.created_at).total_seconds() / 3600, 0), 1)
    return steps, target_total, actual_total

@login_required
def request_detail(request, pk):
    endorsement = _get_accessible_request(request.user, pk)
    items = list(endorsement.items.select_related("plan").all())
    item_kpis = {
        "total": len(items),
        "correct": sum(i.validation_status == EndorsementItem.ValidationStatus.VALID for i in items),
        "errors": sum(i.validation_status == EndorsementItem.ValidationStatus.ERROR for i in items),
        "existing": sum(i.is_existing_record for i in items),
        "approval": sum(i.validation_status == EndorsementItem.ValidationStatus.APPROVAL_REQUIRED for i in items),
        "tpa_approved": sum(i.tpa_status == EndorsementItem.TPAStatus.APPROVED for i in items),
        "tpa_pending": sum(i.tpa_status != EndorsementItem.TPAStatus.APPROVED for i in items),
    }
    approvals = list(endorsement.approvals.select_related("assigned_organization", "item", "decided_by").all())
    for approval in approvals:
        approval.can_decide_for_user = can_decide_approval(request.user, approval)

    member_issues = []
    known_validation_errors = set()
    for item in items:
        member_label = item.full_name or item.member_no or item.employee_no or item.national_id or f"Item {item.pk}"
        for error in item.validation_errors or []:
            member_issues.append({"item": item, "member_label": member_label, "error": error})
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
        known_validation_errors.add(f'Document "{attachment.original_name}" could not be processed: {attachment.processing_error}')

    request_issues = [error for error in (endorsement.validation_errors or []) if error not in known_validation_errors]

    resolution_rows = []
    for event in endorsement.events.filter(
        event_type__in=[
            "ITEM_RECOVERED", "ITEM_MANUALLY_CORRECTED", "SUPPLEMENTAL_ROW_CREATED",
            "BULK_ITEM_RECOVERED", "VALIDATION_ROW_CREATED", "VALIDATION_ITEM_DELETED",
            "INTAKE_ITEM_ADDED", "TPA_ITEM_UPDATED",
        ]
    ):
        payload = event.payload if isinstance(event.payload, dict) else {}
        resolution_rows.append({
            "created_at": event.created_at,
            "description": event.description or event.event_type,
            "before_or_source": payload.get("before") or payload.get("source") or "—",
            "after_or_filled": payload.get("after") or payload.get("filled") or "—",
        })

    wizard_steps, total_sla_target, total_sla_actual = _wizard(endorsement)
    valid_steps = {step["key"] for step in wizard_steps}
    active_step = request.GET.get("step") or _status_stage_key(endorsement)
    if active_step not in valid_steps:
        active_step = _status_stage_key(endorsement)

    can_edit = can_edit_request(request.user, endorsement)
    item_form = EndorsementItemCorrectionForm(policy=endorsement.policy, initial={"effective_date": endorsement.effective_date})

    rejection_reason = ""
    rejected_approval = endorsement.approvals.filter(status=EndorsementApproval.Status.REJECTED).order_by("-decided_at").first()
    if rejected_approval:
        rejection_reason = rejected_approval.decision_comment or rejected_approval.reason

    return render(request, "endorsements/detail.html", {
        "endorsement": endorsement,
        "query_form": QueryForm(),
        "response_form": QueryResponseForm(),
        "supplemental_form": SupplementalUploadForm(),
        "bulk_recovery_form": BulkRecoveryForm(),
        "approval_form": ApprovalDecisionForm(),
        "item_form": item_form,
        "item_kpis": item_kpis,
        "wizard_steps": wizard_steps,
        "active_step": active_step,
        "total_sla_target": total_sla_target,
        "total_sla_actual": total_sla_actual,
        "approvals": approvals,
        "resolution_rows": resolution_rows,
        "member_issues": member_issues,
        "document_issues": document_issues,
        "request_issues": request_issues,
        "rejection_reason": rejection_reason,
        "risk_reasons": (endorsement.metadata or {}).get("insurer_review_reasons", []),
        "is_medical": endorsement.policy.product == Policy.Product.GROUP_MEDICAL,
        "has_blocking_document_issues": any(issue["blocking"] for issue in document_issues),
        "can_edit": can_edit,
        "can_delete_items": can_edit and endorsement.status in {
            EndorsementRequest.Status.DRAFT,
            EndorsementRequest.Status.NEEDS_INFO,
            EndorsementRequest.Status.TPA_QUERY,
            EndorsementRequest.Status.REJECTED,
        },
        "can_tpa_process": can_tpa_process(request.user, endorsement),
        "can_insurer_operate": can_insurer_operate(request.user, endorsement),
    })

@login_required
def add_item(request, pk):
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
        messages.error(request, "Manual members can only be added during Intake/correction.")
        return redirect(f"/endorsements/{pk}/?step=intake")

    form = EndorsementItemCorrectionForm(request.POST, policy=endorsement.policy)
    if form.is_valid():
        item = form.save(commit=False)
        item.request = endorsement
        if not item.effective_date:
            item.effective_date = endorsement.effective_date
        item.extracted_data = {"source": "manual_intake"}
        item.validation_status = EndorsementItem.ValidationStatus.PENDING
        item.save()
        WorkflowEvent.objects.create(
            request=endorsement,
            actor=request.user,
            event_type="INTAKE_ITEM_ADDED",
            description=f"Member item {item.pk} added manually during Intake.",
            payload={"item_id": item.pk, "source": "manual_intake"},
        )
        messages.success(request, "Member added to Intake. Review the row and continue to Validation when ready.")
    else:
        messages.error(request, _form_error_text(form))
    return redirect(f"/endorsements/{pk}/?step=intake")


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
        EndorsementRequest.Status.REJECTED,
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

        messages.success(
            request,
            f"{result['processed']} failed document(s) were retried together as one evidence bundle; "
            f"{result['rows']} member row(s) were identified. Review the recovered Intake rows before validating.",
        )
    except Exception as exc:
        logger.exception("Failed evidence bundle retry failed for endorsement %s", endorsement.pk)
        messages.error(request, f"Failed document bundle could not be recovered: {exc}")

    return redirect(f"/endorsements/{pk}/?step=intake")


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
        messages.warning(request, "Validation can only be submitted while the endorsement is in Intake/correction.")
        return redirect(f"/endorsements/{pk}/?step=validation")

    WorkflowService.revalidate_after_correction(endorsement, request.user)
    endorsement.refresh_from_db()
    if endorsement.validation_errors:
        messages.warning(request, f"Validation completed with {len(endorsement.validation_errors)} blocking issue(s). Return to Intake to correct them.")
        return redirect(f"/endorsements/{pk}/?step=validation")
    if endorsement.status == EndorsementRequest.Status.PENDING_INSURER_APPROVAL:
        messages.warning(request, "Validation passed. The request is waiting for insurer review.")
        return redirect(f"/endorsements/{pk}/?step=approval")
    if endorsement.status in {
        EndorsementRequest.Status.SENT_TO_TPA,
        EndorsementRequest.Status.TPA_IN_PROGRESS,
        EndorsementRequest.Status.READY_FOR_CORE,
        EndorsementRequest.Status.CORE_DISPATCHED,
    }:
        messages.success(request, "Validation and approval routing completed. The request has moved to downstream processing.")
        return redirect(f"/endorsements/{pk}/?step=tpa")
    if endorsement.status == EndorsementRequest.Status.COMPLETED:
        return redirect(f"/endorsements/{pk}/?step=completed")
    messages.success(request, "Validation passed.")
    return redirect(f"/endorsements/{pk}/?step=validation")

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
        EndorsementRequest.Status.REJECTED,
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
    messages.success(request, f"{name} was removed from Intake. Validate again when the corrected intake is ready.")
    return redirect(f"/endorsements/{pk}/?step=intake")


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
            for f in ["member_no", "employee_no", "national_id", "full_name", "relationship", "date_of_birth", "gender", "plan", "annual_salary", "sum_assured", "effective_date"]
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
            messages.success(request, "Item updated in Intake. Review the member list, then continue to Validation when ready.")
            return redirect(f"/endorsements/{pk}/?step=intake")
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
    if endorsement.status not in {
        EndorsementRequest.Status.DRAFT,
        EndorsementRequest.Status.NEEDS_INFO,
        EndorsementRequest.Status.TPA_QUERY,
        EndorsementRequest.Status.REJECTED,
    }:
        messages.error(request, "Member rows can only be deleted while the endorsement is in Intake/correction.")
        return redirect(f"/endorsements/{pk}/?step=intake")

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
        description=f"Item {deleted_id} deleted during Intake.",
        payload=snapshot,
    )
    messages.success(request, f"Member row {deleted_id} removed. Validate when the Intake list is ready.")
    return redirect(f"/endorsements/{pk}/?step=intake")

@login_required
def supplemental_upload(request, pk):
    endorsement = _get_accessible_request(request.user, pk)
    if request.method != "POST":
        messages.error(request, "Intake correction only accepts uploaded files.")
        return redirect(f"/endorsements/{pk}/?step=intake")
    if not can_edit_request(request.user, endorsement):
        messages.error(request, "You do not have permission to correct this endorsement.")
        return redirect(f"/endorsements/{pk}/?step=intake")
    if endorsement.status not in {
        EndorsementRequest.Status.DRAFT,
        EndorsementRequest.Status.NEEDS_INFO,
        EndorsementRequest.Status.TPA_QUERY,
        EndorsementRequest.Status.REJECTED,
    }:
        messages.warning(request, "Files can only be added while the endorsement is in Intake/correction.")
        return redirect(f"/endorsements/{pk}/?step=intake")

    form = SupplementalUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, f"Upload could not be processed. {_form_error_text(form)}")
        return redirect(f"/endorsements/{pk}/?step=intake")

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
        structured_attachments = [attachment for attachment in created_attachments if attachment not in evidence_attachments]

        for attachment in structured_attachments:
            if FileIntakeService.process(attachment):
                processed += 1
            else:
                failures.append(f"{attachment.original_name}: {attachment.processing_error or 'extraction failed'}")

        if evidence_attachments:
            try:
                bundle_result = FileIntakeService.process_supplemental_evidence_bundle(evidence_attachments)
                processed += bundle_result["processed"]
                warnings.extend(f"{item['file_name']}: {item['error']}" for item in bundle_result.get("warnings", []))
            except Exception as exc:
                failures.extend(
                    f"{attachment.original_name}: {attachment.processing_error or str(exc)}"
                    for attachment in evidence_attachments
                )

        WorkflowEvent.objects.create(
            request=endorsement,
            actor=request.user,
            event_type="INTAKE_FILES_ADDED",
            description=f"{len(created_attachments)} intake file(s) added for review.",
            payload={"processed": processed, "failures": failures, "warnings": warnings},
        )

        if failures:
            text = "; ".join(failures)
            messages.warning(request, f"{processed} file(s) were processed, but some need attention: {text}")
        elif warnings:
            messages.warning(request, f"{processed} file(s) were applied. Some evidence warnings remain: {'; '.join(warnings)}")
        else:
            messages.success(request, f"{processed} file(s) processed. Review the extracted member rows below before Validation.")
    except Exception as exc:
        logger.exception("Supplemental upload failed for endorsement %s", endorsement.pk)
        messages.error(request, f"Intake upload failed for {endorsement.reference}: {exc}")
    return redirect(f"/endorsements/{pk}/?step=intake")

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
        try:
            approval = WorkflowService.update_tpa_item(
                item,
                request.user,
                card_number=form.cleaned_data.get("card_number"),
                effective_date=form.cleaned_data.get("effective_date"),
                amount=form.cleaned_data.get("amount"),
                action=form.cleaned_data["action"],
                comment=form.cleaned_data.get("comment", ""),
            )
            action = form.cleaned_data["action"]
            if approval:
                messages.warning(request, "TPA amount changed from the system calculation. The configured approval party has been notified.")
            elif action == "approve":
                messages.success(request, "TPA member approved.")
            elif action == "reject":
                messages.warning(request, "TPA rejected the member and returned it for correction.")
            elif action == "query":
                messages.warning(request, "TPA query raised and the requester has been notified.")
            else:
                messages.success(request, "TPA processing data saved.")
        except ValueError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, _form_error_text(form))
    return redirect(f"/endorsements/{pk}/?step=tpa")

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
        try:
            WorkflowService.decide_approval(
                approval,
                request.user,
                form.cleaned_data["decision"] == "approve",
                form.cleaned_data["comment"],
            )
            if form.cleaned_data["decision"] == "approve":
                messages.success(request, "Approval recorded and the workflow was released to the next step.")
            else:
                messages.warning(request, "Rejection recorded with a mandatory reason. The requester can correct and resubmit.")
        except ValueError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, _form_error_text(form))
    endorsement.refresh_from_db()
    step = _status_stage_key(endorsement)
    return redirect(f"/endorsements/{pk}/?step={step}")

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
        return redirect(f"/endorsements/{pk}/?step=completed")
    except ValueError as exc:
        messages.error(request, str(exc))
    return redirect(f"/endorsements/{pk}/?step=tpa")

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
    if (
        request.user.profile.organization_id != query.assigned_organization_id
        and not can_insurer_operate(request.user, endorsement)
        and not request.user.is_superuser
    ):
        raise PermissionDenied
    form = QueryResponseForm(request.POST)
    if form.is_valid():
        WorkflowService.answer_query(query, request.user, form.cleaned_data["response"])
        messages.success(request, "Response submitted.")
    return redirect("endorsement_detail", pk=pk)


@login_required
def profile(request):
    form = ProfileForm(request.POST or None, request.FILES or None, instance=request.user.profile, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Profile updated.")
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
    response = render(request, "partials/_notifications.html", {"notifications": notifications, "unread_notifications": 0})
    response["HX-Trigger"] = "notificationsRead"
    return response

