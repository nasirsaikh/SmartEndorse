from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
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
from .services import BulkRecoveryService, FileIntakeService, NotificationService, PricingEngine, ValidationService, WorkflowService, json_safe


logger = logging.getLogger(__name__)


def _form_error_text(form):
    parts = []
    for field, errors in form.errors.items():
        label = form.fields.get(field).label if field in form.fields else field
        parts.extend(f"{label}: {error}" for error in errors)
    return "; ".join(parts) or "Invalid request."


def _chart_data(qs):
    statuses = Counter(qs.values_list("status", flat=True))
    status_map = dict(EndorsementRequest.Status.choices)
    monthly = defaultdict(int)
    start = timezone.now() - timedelta(days=180)
    for created_at in qs.filter(created_at__gte=start).values_list("created_at", flat=True):
        monthly[created_at.strftime("%Y-%m")] += 1
    months = sorted(monthly)
    return (
        {"type": "bar", "categories": [status_map.get(key, key) for key in statuses], "series": [{"name": "Requests", "data": list(statuses.values())}]},
        {"type": "line", "categories": months, "series": [{"name": "Requests", "data": [monthly[month] for month in months]}]},
    )


@login_required
def dashboard(request):
    qs = accessible_endorsements(request.user)
    total = qs.count()
    completed = qs.filter(status=EndorsementRequest.Status.COMPLETED).count()
    breached = sum(1 for item in qs.exclude(current_sla_due_at=None).exclude(status=EndorsementRequest.Status.COMPLETED) if item.sla_breached)
    stp = qs.filter(stp_eligible=True).count()
    premium = qs.aggregate(total=Sum("premium_impact"))["total"] or 0
    chart_status, chart_trend = _chart_data(qs)
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
        EndorsementRequest.Status.REJECTED,
    ]).count()
    due_soon = qs.filter(
        current_sla_due_at__isnull=False,
        current_sla_due_at__gt=timezone.now(),
        current_sla_due_at__lte=timezone.now() + timedelta(hours=24),
    ).exclude(status=EndorsementRequest.Status.COMPLETED).count()
    completed_rows = qs.filter(
        status=EndorsementRequest.Status.COMPLETED,
        completed_at__isnull=False,
    ).values_list("created_at", "completed_at")
    completed_hours = [
        max((completed_at - created_at).total_seconds() / 3600, 0)
        for created_at, completed_at in completed_rows
    ]
    avg_total_sla_hours = round(sum(completed_hours) / len(completed_hours), 1) if completed_hours else 0
    validation_scores = list(qs.values_list("validation_score", flat=True))
    avg_validation_score = round(sum(float(v or 0) for v in validation_scores) / len(validation_scores), 1) if validation_scores else 0
    pending_insurer = qs.filter(status=EndorsementRequest.Status.PENDING_INSURER_APPROVAL).count()
    rejected = qs.filter(status=EndorsementRequest.Status.REJECTED).count()
    return render(request, "dashboard.html", {
        "total": total, "completed": completed, "open_count": total - completed, "breached": breached,
        "stp_rate": round((stp / total * 100), 1) if total else 0, "premium": premium,
        "recent": qs.order_by("-created_at")[:10], "chart_status": chart_status, "chart_trend": chart_trend,
        "org_type": org_type, "policy_count": accessible_policies(request.user).count(),
        "can_create": can_create_endorsement(request.user), "tpa_queue": tpa_queue, "exceptions": exceptions,
        "completion_rate": round((completed / total * 100), 1) if total else 0,
        "exception_rate": round((exceptions / total * 100), 1) if total else 0,
        "tpa_queue_rate": round((tpa_queue / total * 100), 1) if total else 0,
        "due_soon": due_soon, "avg_total_sla_hours": avg_total_sla_hours,
        "avg_validation_score": avg_validation_score, "pending_insurer": pending_insurer,
        "rejected": rejected,
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
    if endorsement.status not in {
        EndorsementRequest.Status.DRAFT,
        EndorsementRequest.Status.NEEDS_INFO,
        EndorsementRequest.Status.REJECTED,
        EndorsementRequest.Status.TPA_QUERY,
    }:
        messages.warning(request, "Bulk correction is only available while this endorsement is in intake/correction.")
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
                f"{ambiguous} ambiguous and {unmatched} unmatched row(s) were left unchanged. "
                "Review the changes, then use Validate & Submit.",
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


def _workflow_stage_key(status):
    mapping = {
        EndorsementRequest.Status.DRAFT: "intake",
        EndorsementRequest.Status.NEEDS_INFO: "intake",
        EndorsementRequest.Status.REJECTED: "intake",
        EndorsementRequest.Status.VALIDATING: "validation",
        EndorsementRequest.Status.SUBMITTED: "validation",
        EndorsementRequest.Status.PENDING_INSURER_APPROVAL: "approval",
        EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL: "approval",
        EndorsementRequest.Status.AUTO_APPROVED: "approval",
        EndorsementRequest.Status.SENT_TO_TPA: "processing",
        EndorsementRequest.Status.TPA_IN_PROGRESS: "processing",
        EndorsementRequest.Status.TPA_QUERY: "processing",
        EndorsementRequest.Status.CORE_DISPATCHED: "processing",
        EndorsementRequest.Status.FAILED: "processing",
        EndorsementRequest.Status.COMPLETED: "completion",
    }
    return mapping.get(status)


def _workflow_steps(endorsement):
    now = timezone.now()
    stage_defs = [
        ("intake", "Intake", endorsement.policy.client_query_sla, 24),
        ("validation", "Validation", endorsement.policy.insurer_sla, 8),
        ("approval", "Approval", endorsement.policy.insurer_sla, 24),
        (
            "processing",
            "TPA / Core",
            endorsement.policy.tpa_sla if endorsement.policy.product == Policy.Product.GROUP_MEDICAL else endorsement.policy.insurer_sla,
            24,
        ),
    ]
    status_to_stage = {
        EndorsementRequest.Status.DRAFT: "intake",
        EndorsementRequest.Status.NEEDS_INFO: "intake",
        EndorsementRequest.Status.REJECTED: "intake",
        EndorsementRequest.Status.VALIDATING: "validation",
        EndorsementRequest.Status.SUBMITTED: "validation",
        EndorsementRequest.Status.PENDING_INSURER_APPROVAL: "approval",
        EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL: "approval",
        EndorsementRequest.Status.AUTO_APPROVED: "approval",
        EndorsementRequest.Status.SENT_TO_TPA: "processing",
        EndorsementRequest.Status.TPA_IN_PROGRESS: "processing",
        EndorsementRequest.Status.TPA_QUERY: "processing",
        EndorsementRequest.Status.CORE_DISPATCHED: "processing",
        EndorsementRequest.Status.FAILED: "processing",
        EndorsementRequest.Status.COMPLETED: "completion",
    }

    durations = {key: 0.0 for key, _, _, _ in stage_defs}
    visited = {"intake"}
    events = list(endorsement.events.filter(event_type="STATUS_CHANGE").order_by("created_at"))
    current_status = EndorsementRequest.Status.DRAFT
    period_start = endorsement.created_at

    for event in events:
        from_status = event.from_status or current_status
        from_stage = status_to_stage.get(from_status)
        if from_stage in durations and event.created_at >= period_start:
            durations[from_stage] += (event.created_at - period_start).total_seconds() / 3600
            visited.add(from_stage)
        current_status = event.to_status or current_status
        to_stage = status_to_stage.get(current_status)
        if to_stage:
            visited.add(to_stage)
        period_start = event.created_at

    current_stage = _workflow_stage_key(endorsement.status)
    terminal = endorsement.status in {
        EndorsementRequest.Status.COMPLETED,
        EndorsementRequest.Status.CANCELLED,
    }
    if not terminal and current_stage in durations:
        durations[current_stage] += max((now - period_start).total_seconds() / 3600, 0)
        visited.add(current_stage)

    steps = []
    total_target = 0
    stage_order = [key for key, _, _, _ in stage_defs] + ["completion"]
    current_index = stage_order.index(current_stage) if current_stage in stage_order else 0
    for key, label, profile, fallback in stage_defs:
        target_hours = profile.target_hours if profile else fallback
        total_target += target_hours
        actual_hours = round(durations[key], 1) if key in visited else None
        if key == current_stage:
            state = "active"
        elif key in visited:
            state = "returned" if stage_order.index(key) > current_index else "done"
        else:
            state = "pending"
        steps.append({
            "key": key,
            "label": label,
            "target_hours": target_hours,
            "actual_hours": actual_hours,
            "breached": actual_hours is not None and actual_hours > target_hours,
            "state": state,
            "tat_label": "Step TAT",
        })

    total_actual = round((((endorsement.completed_at or now) - endorsement.created_at).total_seconds() / 3600), 1)
    steps.append({
        "key": "completion",
        "label": "Completion",
        "target_hours": total_target,
        "actual_hours": total_actual,
        "breached": total_actual > total_target,
        "state": "active" if current_stage == "completion" else "done" if endorsement.status == EndorsementRequest.Status.COMPLETED else "pending",
        "tat_label": "Total TAT",
    })
    return steps, current_stage or "intake"


def _change_value(field, value, plan_names=None):
    if value in (None, ""):
        return "—", None
    if field == "plan":
        if isinstance(value, dict):
            identity = value.get("id")
            display = value.get("display") or (plan_names or {}).get(identity) or identity
            return str(display or "—"), str(identity) if identity is not None else str(display)
        display = (plan_names or {}).get(value) or value
        return str(display), str(value)
    if isinstance(value, dict):
        if "display" in value:
            return str(value.get("display") or "—"), str(value.get("id") or value.get("display") or "")
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return rendered, rendered
    if isinstance(value, (list, tuple)):
        rendered = ", ".join(str(item) for item in value if item not in (None, "")) or "—"
        return rendered, rendered
    return str(value), str(value)


def _resolution_changes(endorsement):
    plan_names = {
        plan.pk: f"{plan.code} · {plan.name}"
        for plan in endorsement.policy.plans.all()
    }
    labels = {
        "member_no": "Member no.",
        "employee_no": "Employee no.",
        "national_id": "Civil / National ID",
        "full_name": "Full name",
        "relationship": "Relationship",
        "date_of_birth": "Date of birth",
        "gender": "Gender",
        "plan": "Plan",
        "annual_salary": "Annual salary",
        "sum_assured": "Sum assured",
        "effective_date": "Effective date",
        "card_number": "Card number",
        "amount": "Amount",
    }
    rows = []
    events = endorsement.events.filter(
        event_type__in=[
            "ITEM_RECOVERED", "ITEM_MANUALLY_CORRECTED", "BULK_ITEM_RECOVERED",
            "SOURCE_DATA_RECOVERED", "TPA_ITEM_UPDATED", "EMAIL_MEMBER_CORRECTED", "EMAIL_CORRECT_MEMBER_CHANGED",
        ]
    ).order_by("-created_at")
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        before = payload.get("before") if isinstance(payload.get("before"), dict) else {}
        after = payload.get("after") if isinstance(payload.get("after"), dict) else {}
        filled = payload.get("filled") if isinstance(payload.get("filled"), dict) else {}
        changed_values = after or filled
        changes = []
        for field, new_value in changed_values.items():
            if field in {"item_id", "source", "matched_by"}:
                continue
            old_value = before.get(field)
            old_display, old_key = _change_value(field, old_value, plan_names)
            new_display, new_key = _change_value(field, new_value, plan_names)
            if old_key == new_key:
                continue
            changes.append({
                "field": labels.get(field, str(field).replace("_", " ").title()),
                "before": old_display,
                "after": new_display,
            })
        if not changes:
            continue
        rows.append({
            "created_at": event.created_at,
            "description": event.description or event.event_type,
            "actor": event.actor,
            "changes": changes,
        })
    return rows


def _source_json_rows(items):
    canonical_fields = (
        "member_no", "employee_no", "national_id", "full_name", "relationship",
        "date_of_birth", "gender", "plan_code", "annual_salary", "sum_assured",
        "effective_date",
    )
    rows = []
    for item in items:
        data = item.extracted_data if isinstance(item.extracted_data, dict) else {}
        normalized = data.get("normalized") if isinstance(data.get("normalized"), dict) else {}
        unified = {}
        for field in canonical_fields:
            value = normalized.get(field)
            if value not in (None, "", [], {}):
                unified[field] = value
        if not unified:
            fallback = {
                "member_no": item.member_no,
                "employee_no": item.employee_no,
                "national_id": item.national_id,
                "full_name": item.full_name,
                "relationship": item.relationship,
                "date_of_birth": item.date_of_birth,
                "gender": item.gender,
                "plan_code": item.plan.code if item.plan_id else None,
                "annual_salary": item.annual_salary,
                "sum_assured": item.sum_assured,
                "effective_date": item.effective_date,
            }
            unified = {key: json_safe(value) for key, value in fallback.items() if value not in (None, "")}
        rows.append({
            "item": item,
            "source_attachment_id": data.get("source_attachment_id"),
            "json": json.dumps(json_safe(unified), indent=2, ensure_ascii=False, default=str),
        })
    return rows


def _helpdesk_messages(endorsement, user):
    messages_list = []
    processing_statuses = {
        EndorsementRequest.Status.SENT_TO_TPA,
        EndorsementRequest.Status.TPA_IN_PROGRESS,
        EndorsementRequest.Status.TPA_QUERY,
        EndorsementRequest.Status.PENDING_AMOUNT_APPROVAL,
        EndorsementRequest.Status.CORE_DISPATCHED,
        EndorsementRequest.Status.REJECTED,
        EndorsementRequest.Status.COMPLETED,
    }
    for event in endorsement.events.filter(event_type__in=["STATUS_CHANGE", "PROCESSING_MESSAGE"]).select_related("actor"):
        if event.event_type == "PROCESSING_MESSAGE":
            actor = event.actor
            try:
                actor_org = actor.profile.organization.name if actor else ""
            except Exception:
                actor_org = ""
            messages_list.append({
                "kind": "chat",
                "created_at": event.created_at,
                "sender": (actor.get_full_name() or actor.username) if actor else "System",
                "organization": actor_org,
                "subject": "",
                "text": event.description,
                "side": "end" if actor and actor.id == user.id else "start",
                "query": None,
                "can_reply": False,
            })
            continue
        if event.to_status not in processing_statuses and event.from_status not in processing_statuses:
            continue
        messages_list.append({
            "kind": "system",
            "created_at": event.created_at,
            "text": event.description or f"{event.from_status or '—'} → {event.to_status or '—'}",
            "status": event.to_status,
        })

    for query in endorsement.queries.all():
        raised_by = query.raised_by
        try:
            raised_org = raised_by.profile.organization.name
        except Exception:
            raised_org = ""
        messages_list.append({
            "kind": "chat",
            "created_at": query.created_at,
            "sender": raised_by.get_full_name() or raised_by.username,
            "organization": raised_org,
            "subject": query.subject,
            "text": query.message,
            "side": "end" if query.raised_by_id == user.id else "start",
            "query": query,
            "can_reply": (
                not query.is_closed
                and hasattr(user, "profile")
                and user.profile.organization_id == query.assigned_organization_id
            ),
        })
        if query.response:
            responded_by = query.responded_by
            try:
                response_org = responded_by.profile.organization.name if responded_by else query.assigned_organization.name
            except Exception:
                response_org = query.assigned_organization.name
            messages_list.append({
                "kind": "chat",
                "created_at": query.responded_at or query.updated_at,
                "sender": (responded_by.get_full_name() or responded_by.username) if responded_by else query.assigned_organization.name,
                "organization": response_org,
                "subject": f"RE: {query.subject}",
                "text": query.response,
                "side": "end" if responded_by and responded_by.id == user.id else "start",
                "query": None,
                "can_reply": False,
            })
    return sorted(messages_list, key=lambda row: row["created_at"])


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
    latest_rejection = next(
        (approval for approval in sorted(approvals, key=lambda x: x.updated_at, reverse=True)
         if approval.status == EndorsementApproval.Status.REJECTED and approval.decision_comment),
        None,
    )
    latest_rejection_reason = latest_rejection.decision_comment if latest_rejection else ""
    if not latest_rejection_reason:
        rejection_event = endorsement.events.filter(
            event_type="STATUS_CHANGE", to_status=EndorsementRequest.Status.REJECTED
        ).order_by("-created_at").first()
        if rejection_event:
            payload = rejection_event.payload if isinstance(rejection_event.payload, dict) else {}
            latest_rejection_reason = payload.get("rejection_reason") or payload.get("reason") or rejection_event.description
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
        optional_ocr = payload.get("usage") == "item_ocr_preview" or bool(payload.get("superseded_by_email_id"))
        document_issues.append({
            "attachment": attachment,
            "error": attachment.processing_error,
            "optional_ocr": optional_ocr,
            "blocking": not optional_ocr,
        })
        known_validation_errors.add(f"{attachment.original_name}: {attachment.processing_error}")
        known_validation_errors.add(f'Document "{attachment.original_name}" could not be processed: {attachment.processing_error}')

    request_issues = [
        error for error in (endorsement.validation_errors or [])
        if error not in known_validation_errors
    ]

    wizard_steps, current_step = _workflow_steps(endorsement)
    allowed_steps = {step["key"] for step in wizard_steps}
    selected_step = request.GET.get("step", "").strip().lower()
    if selected_step not in allowed_steps:
        selected_step = current_step
    selected_index = next(index for index, step in enumerate(wizard_steps) if step["key"] == selected_step)
    previous_step = wizard_steps[selected_index - 1] if selected_index > 0 else None
    next_step = wizard_steps[selected_index + 1] if selected_index < len(wizard_steps) - 1 else None
    completion_tat = next(step for step in wizard_steps if step["key"] == "completion")
    editable_intake = can_edit_request(request.user, endorsement) and endorsement.status in {
        EndorsementRequest.Status.DRAFT,
        EndorsementRequest.Status.NEEDS_INFO,
        EndorsementRequest.Status.REJECTED,
        EndorsementRequest.Status.TPA_QUERY,
    }
    tpa_ready = bool(items) and all(
        item.card_number and item.tpa_effective_date and item.tpa_premium_amount is not None
        for item in items
    )
    pending_amount_approval = any(
        approval.status == EndorsementApproval.Status.PENDING
        and approval.approval_type == EndorsementApproval.ApprovalType.TPA_AMOUNT_CHANGE
        for approval in approvals
    )
    has_open_query = endorsement.queries.filter(is_closed=False).exists()

    source_json_rows = _source_json_rows(items)
    source_json_by_item = {row["item"].pk: row["json"] for row in source_json_rows}
    for item in items:
        item.source_json = source_json_by_item.get(item.pk, "{}")

    return render(request, "endorsements/detail.html", {
        "endorsement": endorsement, "items": items, "query_form": QueryForm(), "response_form": QueryResponseForm(),
        "supplemental_form": SupplementalUploadForm(), "bulk_recovery_form": BulkRecoveryForm(),
        "approval_form": ApprovalDecisionForm(),
        "item_kpis": item_kpis, "wizard_steps": wizard_steps, "approvals": approvals,
        "current_step": current_step, "selected_step": selected_step, "completion_tat": completion_tat,
        "previous_step": previous_step, "next_step": next_step, "selected_step_number": selected_index + 1,
        "latest_rejection": latest_rejection, "latest_rejection_reason": latest_rejection_reason,
        "add_item_form": EndorsementItemCorrectionForm(instance=EndorsementItem(request=endorsement, effective_date=endorsement.effective_date)),
        "resolution_rows": _resolution_changes(endorsement),
        "helpdesk_messages": _helpdesk_messages(endorsement, request.user),
        "tpa_ready": tpa_ready, "pending_amount_approval": pending_amount_approval,
        "has_open_query": has_open_query,
        "member_issues": member_issues, "document_issues": document_issues,
        "request_issues": request_issues,
        "has_blocking_document_issues": any(issue["blocking"] for issue in document_issues),
        "can_edit": can_edit_request(request.user, endorsement),
        "editable_intake": editable_intake,
        "can_delete_items": editable_intake,
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
        EndorsementRequest.Status.REJECTED,
    }:
        messages.error(request, "Failed documents can only be retried while the endorsement is in intake/correction.")
        return redirect("endorsement_detail", pk=pk)

    failed = []
    for attachment in endorsement.attachments.exclude(processing_error=""):
        if Path(attachment.original_name).suffix.lower() not in {".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
            continue
        payload = attachment.extracted_payload if isinstance(attachment.extracted_payload, dict) else {}
        if payload.get("usage") == "item_ocr_preview" or payload.get("superseded_by_email_id"):
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
            f"{result['rows']} member row(s) were identified. Review the result, then use Validate & Submit.",
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
        messages.success(request, f"Validation passed. The request is now {endorsement.get_status_display()} and has been routed to the next workflow step.")
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
        EndorsementRequest.Status.REJECTED,
    }:
        messages.error(request, "Failed documents can only be removed while the endorsement is in intake/correction.")
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
    messages.success(request, f"{name} was removed. Review the request, then use Validate & Submit.")
    return redirect("endorsement_detail", pk=pk)


@login_required
def edit_item(request, pk, item_id):
    endorsement = _get_accessible_request(request.user, pk)
    if not can_edit_request(request.user, endorsement):
        raise PermissionDenied
    if endorsement.status not in {
        EndorsementRequest.Status.DRAFT,
        EndorsementRequest.Status.NEEDS_INFO,
        EndorsementRequest.Status.REJECTED,
        EndorsementRequest.Status.TPA_QUERY,
    }:
        messages.error(request, "Member data can only be edited during intake/correction.")
        return redirect("endorsement_detail", pk=pk)
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
            messages.success(request, "Item updated. Review the intake/correction and use Validate & Submit when ready.")
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
    if endorsement.status not in {EndorsementRequest.Status.DRAFT, EndorsementRequest.Status.NEEDS_INFO, EndorsementRequest.Status.REJECTED, EndorsementRequest.Status.TPA_QUERY}:
        messages.error(request, "Member rows can only be deleted while the endorsement is in intake/correction.")
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
    messages.success(request, f"Member row {deleted_id} deleted. Review the request, then use Validate & Submit.")
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
    if endorsement.status not in {EndorsementRequest.Status.DRAFT, EndorsementRequest.Status.NEEDS_INFO, EndorsementRequest.Status.REJECTED, EndorsementRequest.Status.TPA_QUERY}:
        messages.warning(request, "File intake/correction is only available while this endorsement is editable.")
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

        if failures:
            text = "; ".join(failures)
            if processed:
                messages.warning(request, f"{processed} file(s) were processed, but some failed: {text}. Review the result before Validate & Submit.")
            else:
                messages.error(request, f"Validation upload failed for {endorsement.reference}: {text}")
        elif warnings:
            messages.warning(
                request,
                f"{processed} file(s) were applied as one evidence bundle. "
                f"Some files could not be read, but the remaining evidence was sufficient: {'; '.join(warnings)}",
            )
        else:
            messages.success(
                request,
                f"{processed} file(s) processed. PDF/image files uploaded together were treated as one evidence bundle, so front/back pages can complement each other. Review the result, then use Validate & Submit.",
            )
    except Exception as exc:
        logger.exception("Supplemental upload failed for endorsement %s", endorsement.pk)
        messages.error(request, f"Supplemental upload failed for {endorsement.reference}: {exc}")
    return redirect("endorsement_detail", pk=pk)


@login_required
def processing_message(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    message_text = request.POST.get("message", "").strip()
    if not message_text:
        messages.error(request, "Type a message before sending.")
    else:
        WorkflowEvent.objects.create(
            request=endorsement,
            actor=request.user,
            event_type="PROCESSING_MESSAGE",
            description=message_text,
            payload={"source": "processing_helpdesk"},
        )
        recipients = [
            user for user in NotificationService.recipients_for_request(endorsement)
            if user.id != request.user.id
        ]
        NotificationService.create_portal(
            recipients,
            f"New message · {endorsement.reference}",
            message_text[:300],
            endorsement,
            PortalNotification.Level.INFO,
            {"event_type": "PROCESSING_MESSAGE"},
        )
        messages.success(request, "Message sent to the processing conversation.")
    url = reverse("endorsement_detail", kwargs={"pk": pk})
    return redirect(f"{url}?step=processing#processing-helpdesk")


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
        messages.error(request, "Card number, effective date and amount are required.")
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
def tpa_reject_request(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    if not can_tpa_process(request.user, endorsement):
        raise PermissionDenied
    if endorsement.status not in {EndorsementRequest.Status.SENT_TO_TPA, EndorsementRequest.Status.TPA_IN_PROGRESS}:
        messages.error(request, "TPA rejection is only available while the request is in TPA processing.")
        return redirect("endorsement_detail", pk=pk)
    reason = request.POST.get("reason", "").strip()
    if not reason:
        messages.error(request, "TPA rejection reason is mandatory.")
        return redirect("endorsement_detail", pk=pk)
    WorkflowService.reject_by_tpa(endorsement, request.user, reason)
    messages.warning(request, "TPA rejected the endorsement. The requester/insurer can correct it and resubmit.")
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


def _accessible_emails(user):
    from .models import InboundEmail
    if user.is_superuser:
        return InboundEmail.objects.all()
    profile = getattr(user, "profile", None)
    if not profile or not profile.organization.is_active:
        return InboundEmail.objects.none()
    policies = accessible_policies(user)
    qs = InboundEmail.objects.filter(policy__in=policies)
    if profile.organization.organization_type == "INSURER":
        return qs
    return qs.filter(Q(authority__organization=profile.organization) | Q(endorsement__requester_organization=profile.organization)).distinct()


@login_required
def inbound_email_list(request):
    from django.core.paginator import Paginator
    emails = _accessible_emails(request.user).select_related("mailbox", "endorsement", "policy")
    query = request.GET.get("q", "").strip()
    if query:
        emails = emails.filter(Q(reference__icontains=query) | Q(sender__icontains=query) | Q(subject__icontains=query))
    state = request.GET.get("state", "")
    if state:
        emails = emails.filter(processing_state=state)
    from .models import InboundEmail
    return render(request, "emails/list.html", {"page": Paginator(emails, 30).get_page(request.GET.get("page")), "query": query, "state": state, "states": InboundEmail.State.choices})


@login_required
def inbound_email_detail(request, pk):
    email = get_object_or_404(_accessible_emails(request.user).select_related("mailbox", "policy", "endorsement", "thread", "authority"), pk=pk)
    if request.method == "POST":
        if not request.user.is_staff or (email.endorsement_id and not can_insurer_operate(request.user, email.endorsement)):
            raise PermissionDenied("Only authorized staff can retry email processing.")
        from .email_intake import deliver_reply, process_email
        if request.POST.get("action") == "retry_delivery":
            reply = getattr(email, "reply", None)
            if reply:
                if deliver_reply(reply.pk):
                    messages.success(request, "Email reply delivered.")
                else:
                    reply.refresh_from_db()
                    messages.warning(request, reply.last_error or "Reply remains pending. Check email notification and delivery settings in Admin.")
        else:
            from .models import InboundEmail
            result = process_email(email.pk, force=True)
            if result.processing_state == InboundEmail.State.UNAUTHORIZED:
                messages.warning(request, result.processing_error)
            else:
                messages.success(request, f"Email rechecked: {result.status_label}.")
        return redirect("inbound_email_detail", pk=email.pk)
    return render(request, "emails/detail.html", {"email": email, "payload_json": json.dumps(email.extracted_payload, indent=2, ensure_ascii=False), "can_retry": request.user.is_staff and (not email.endorsement_id or can_insurer_operate(request.user, email.endorsement))})
