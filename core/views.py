from collections import Counter, defaultdict
from datetime import timedelta
import logging

import plotly.graph_objects as go
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .access import (
    accessible_endorsements, accessible_policies, can_create_endorsement,
    can_decide_approval, can_edit_request, can_insurer_operate, can_tpa_process,
)
from .forms import (
    ApprovalDecisionForm, BulkRecoveryForm, EndorsementCreateForm, EndorsementItemCorrectionForm,
    QueryForm, QueryResponseForm, SupplementalUploadForm, TPAItemProcessingForm,
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
    qs = accessible_endorsements(request.user).order_by("-created_at")
    status, product, q = request.GET.get("status", ""), request.GET.get("product", ""), request.GET.get("q", "").strip()
    if status:
        qs = qs.filter(status=status)
    if product:
        qs = qs.filter(policy__product=product)
    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(policy__policy_number__icontains=q) | Q(policy__client__name__icontains=q) | Q(items__full_name__icontains=q)).distinct()
    context = {
        "endorsements": qs[:200], "status_choices": EndorsementRequest.Status.choices,
        "product_choices": Policy.Product.choices, "selected_status": status, "selected_product": product,
        "q": q, "can_create": can_create_endorsement(request.user),
    }
    return render(request, "endorsements/_table.html" if getattr(request, "htmx", False) else "endorsements/list.html", context)


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
            for upload in form.cleaned_data.get("attachments", []):
                attachment = Attachment.objects.create(request=endorsement, file=upload, original_name=upload.name, kind=FileIntakeService.kind_for_name(upload.name))
                FileIntakeService.process(attachment)
            WorkflowEvent.objects.create(request=endorsement, actor=request.user, event_type="REQUEST_CREATED", description="Endorsement request created.")
            WorkflowService.submit(endorsement, request.user)
            if endorsement.status == EndorsementRequest.Status.NEEDS_INFO:
                messages.warning(request, "Request created, but processing is blocked until all validation errors are corrected.")
            elif endorsement.status in {EndorsementRequest.Status.PENDING_INSURER_APPROVAL, EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL}:
                messages.warning(request, "Request requires approval before it can continue.")
            elif endorsement.status == EndorsementRequest.Status.FAILED:
                messages.error(request, "Validation passed, but automatic dispatch failed. Insurer operations can review the exception.")
            else:
                messages.success(request, f"{endorsement.reference} submitted successfully.")
            return redirect("endorsement_detail", pk=endorsement.pk)
    else:
        form = EndorsementCreateForm(policies=policies)
    return render(request, "endorsements/form.html", {"form": form})


def _get_accessible_request(user, pk):
    return get_object_or_404(accessible_endorsements(user).prefetch_related("items__plan", "attachments", "events", "queries", "approvals__assigned_organization"), pk=pk)


def _wizard(endorsement):
    stages = [
        ("Intake", {EndorsementRequest.Status.DRAFT, EndorsementRequest.Status.VALIDATING, EndorsementRequest.Status.NEEDS_INFO}),
        ("Validated", {EndorsementRequest.Status.SUBMITTED, EndorsementRequest.Status.PENDING_INSURER_APPROVAL}),
        ("Approved", {EndorsementRequest.Status.AUTO_APPROVED, EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL}),
        ("TPA / Core", {EndorsementRequest.Status.SENT_TO_TPA, EndorsementRequest.Status.TPA_IN_PROGRESS, EndorsementRequest.Status.TPA_QUERY, EndorsementRequest.Status.CORE_DISPATCHED}),
        ("Completed", {EndorsementRequest.Status.COMPLETED}),
    ]
    index = next((i for i, (_, states) in enumerate(stages) if endorsement.status in states), 0)
    return [{"label": label, "state": "done" if i < index else "active" if i == index else "pending"} for i, (label, _) in enumerate(stages)]


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

    resolution_rows = []
    for event in endorsement.events.filter(
        event_type__in=["ITEM_RECOVERED", "ITEM_MANUALLY_CORRECTED", "SUPPLEMENTAL_ROW_CREATED", "BULK_ITEM_RECOVERED"]
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
        "resolution_rows": resolution_rows,
        "can_edit": can_edit_request(request.user, endorsement),
        "can_tpa_process": can_tpa_process(request.user, endorsement),
        "can_insurer_operate": can_insurer_operate(request.user, endorsement),
    })


@login_required
def edit_item(request, pk, item_id):
    endorsement = _get_accessible_request(request.user, pk)
    if not can_edit_request(request.user, endorsement):
        raise PermissionDenied
    item = get_object_or_404(endorsement.items, pk=item_id)
    if request.method == "POST":
        before = {f: json_safe(getattr(item, f)) for f in ["member_no", "employee_no", "national_id", "full_name", "relationship", "date_of_birth", "gender", "annual_salary", "sum_assured", "effective_date"]}
        form = EndorsementItemCorrectionForm(request.POST, instance=item)
        if form.is_valid():
            item = form.save()
            WorkflowEvent.objects.create(request=endorsement, actor=request.user, event_type="ITEM_MANUALLY_CORRECTED", description=f"Item {item.pk} corrected manually.", payload={"item_id": item.pk, "before": before, "after": {k: json_safe(v) for k, v in form.cleaned_data.items()}})
            WorkflowService.revalidate_after_correction(endorsement, request.user)
            messages.success(request, "Item updated and the case was revalidated.")
            return redirect("endorsement_detail", pk=pk)
    else:
        form = EndorsementItemCorrectionForm(instance=item)
    return render(request, "endorsements/item_edit.html", {"endorsement": endorsement, "item": item, "form": form})


@login_required
def supplemental_upload(request, pk):
    endorsement = _get_accessible_request(request.user, pk)
    if request.method != "POST":
        messages.error(request, "Supplemental correction only accepts uploaded files.")
        return redirect("endorsement_detail", pk=pk)
    if not can_edit_request(request.user, endorsement):
        messages.error(request, "You do not have permission to correct this endorsement.")
        return redirect("endorsement_detail", pk=pk)
    if endorsement.status != EndorsementRequest.Status.NEEDS_INFO:
        messages.warning(request, "Supplemental correction is only available while this endorsement needs information.")
        return redirect("endorsement_detail", pk=pk)

    form = SupplementalUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, f"Supplemental upload could not be processed. {_form_error_text(form)}")
        return redirect("endorsement_detail", pk=pk)

    processed = 0
    failures = []
    try:
        for upload in form.cleaned_data["attachments"]:
            attachment = Attachment.objects.create(
                request=endorsement,
                file=upload,
                original_name=upload.name,
                kind=FileIntakeService.kind_for_name(upload.name),
                is_supplemental=True,
            )
            if FileIntakeService.process(attachment):
                processed += 1
            else:
                failures.append(f"{attachment.original_name}: {attachment.processing_error or 'extraction failed'}")

        if processed:
            WorkflowService.revalidate_after_correction(endorsement, request.user)

        if failures:
            text = "; ".join(failures)
            if processed:
                messages.warning(
                    request,
                    f"{processed} supplemental file(s) were processed for {endorsement.reference}, but some failed: {text}",
                )
            else:
                messages.error(request, f"Supplemental upload failed for {endorsement.reference}: {text}")
        else:
            messages.success(
                request,
                f"{processed} supplemental file(s) processed for {endorsement.reference}. "
                "Unique rows were matched and missing fields were filled where possible.",
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
        approval = WorkflowService.update_tpa_item(item, request.user, form.cleaned_data["card_number"], form.cleaned_data["amount"])
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
        WorkflowService.decide_approval(approval, request.user, form.cleaned_data["decision"] == "approve", form.cleaned_data["comment"])
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
    return HttpResponse(status=204)
