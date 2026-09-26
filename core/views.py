from collections import Counter, defaultdict
from datetime import timedelta

import plotly.graph_objects as go
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .access import accessible_endorsements, accessible_policies
from .forms import EndorsementCreateForm, QueryForm, QueryResponseForm
from .models import Attachment, EndorsementItem, EndorsementRequest, Organization, Policy, PolicyPlan
from .services import FileIntakeService, WorkflowService


def _chart_html(qs):
    statuses = Counter(qs.values_list("status", flat=True))
    status_map = dict(EndorsementRequest.Status.choices)
    labels = [status_map.get(key, key) for key in statuses]
    fig1 = go.Figure(data=[go.Bar(x=labels, y=list(statuses.values()))])
    fig1.update_layout(
        title="Pipeline by status",
        height=330,
        margin=dict(l=30, r=20, t=55, b=40),
    )

    monthly = defaultdict(int)
    start = timezone.now() - timedelta(days=180)
    for created_at in qs.filter(created_at__gte=start).values_list("created_at", flat=True):
        monthly[created_at.strftime("%Y-%m")] += 1
    months = sorted(monthly)
    fig2 = go.Figure(
        data=[go.Scatter(
            x=months,
            y=[monthly[month] for month in months],
            mode="lines+markers",
        )]
    )
    fig2.update_layout(
        title="Endorsement volume - last 6 months",
        height=330,
        margin=dict(l=30, r=20, t=55, b=40),
    )

    config = {"displayModeBar": False, "responsive": True}
    return (
        fig1.to_html(full_html=False, include_plotlyjs=False, config=config),
        fig2.to_html(full_html=False, include_plotlyjs=False, config=config),
    )


@login_required
def dashboard(request):
    qs = accessible_endorsements(request.user)
    total = qs.count()
    completed = qs.filter(status=EndorsementRequest.Status.COMPLETED).count()
    breached = sum(
        1
        for item in qs.exclude(current_sla_due_at=None).exclude(status=EndorsementRequest.Status.COMPLETED)
        if item.sla_breached
    )
    stp = qs.filter(stp_eligible=True).count()
    premium = qs.aggregate(total=Sum("premium_impact"))["total"] or 0
    chart_status, chart_trend = _chart_html(qs)
    try:
        org_type = request.user.profile.organization.organization_type
    except Exception:
        org_type = ""

    return render(request, "dashboard.html", {
        "total": total,
        "completed": completed,
        "open_count": total - completed,
        "breached": breached,
        "stp_rate": round((stp / total * 100), 1) if total else 0,
        "premium": premium,
        "recent": qs.order_by("-created_at")[:10],
        "chart_status": chart_status,
        "chart_trend": chart_trend,
        "org_type": org_type,
        "policy_count": accessible_policies(request.user).count(),
        "tpa_queue": qs.filter(status__in=[
            EndorsementRequest.Status.SENT_TO_TPA,
            EndorsementRequest.Status.TPA_IN_PROGRESS,
            EndorsementRequest.Status.TPA_QUERY,
        ]).count(),
        "exceptions": qs.filter(status__in=[
            EndorsementRequest.Status.NEEDS_INFO,
            EndorsementRequest.Status.FAILED,
        ]).count(),
    })


@login_required
def request_list(request):
    qs = accessible_endorsements(request.user).order_by("-created_at")
    status = request.GET.get("status", "")
    product = request.GET.get("product", "")
    q = request.GET.get("q", "").strip()

    if status:
        qs = qs.filter(status=status)
    if product:
        qs = qs.filter(policy__product=product)
    if q:
        qs = qs.filter(
            Q(reference__icontains=q) |
            Q(policy__policy_number__icontains=q) |
            Q(policy__client__name__icontains=q)
        )

    context = {
        "endorsements": qs[:200],
        "status_choices": EndorsementRequest.Status.choices,
        "product_choices": Policy.Product.choices,
        "selected_status": status,
        "selected_product": product,
        "q": q,
    }
    template = "endorsements/_table.html" if getattr(request, "htmx", False) else "endorsements/list.html"
    return render(request, template, context)


@login_required
def policy_plans(request):
    policy_id = request.GET.get("policy")
    plans = PolicyPlan.objects.none()
    if policy_id and accessible_policies(request.user, require_create=True).filter(pk=policy_id).exists():
        plans = PolicyPlan.objects.filter(policy_id=policy_id, is_active=True).order_by("code")
    options = ['<option value="">---------</option>']
    for plan in plans:
        options.append(f'<option value="{plan.pk}">{plan.code} - {plan.name}</option>')
    return HttpResponse("".join(options))


@login_required
def create_request(request):
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
                EndorsementItem.objects.create(
                    request=endorsement,
                    **form.manual_item_payload(),
                )

            for upload in form.cleaned_data.get("attachments", []):
                attachment = Attachment.objects.create(
                    request=endorsement,
                    file=upload,
                    original_name=upload.name,
                    kind=FileIntakeService.kind_for_name(upload.name),
                )
                FileIntakeService.process(attachment)

            WorkflowService.submit(endorsement, request.user)

            if endorsement.status == EndorsementRequest.Status.NEEDS_INFO:
                messages.warning(
                    request,
                    "Request was created but needs correction. Review the validation messages.",
                )
            elif endorsement.status == EndorsementRequest.Status.FAILED:
                messages.error(
                    request,
                    "The request passed validation but automatic dispatch failed. The exception is visible to insurer operations.",
                )
            else:
                messages.success(
                    request,
                    f"{endorsement.reference} submitted successfully.",
                )
            return redirect("endorsement_detail", pk=endorsement.pk)
    else:
        form = EndorsementCreateForm(policies=policies)

    return render(request, "endorsements/form.html", {"form": form})


def _get_accessible_request(user, pk):
    return get_object_or_404(
        accessible_endorsements(user).prefetch_related(
            "items",
            "attachments",
            "events",
            "queries",
        ),
        pk=pk,
    )


@login_required
def request_detail(request, pk):
    endorsement = _get_accessible_request(request.user, pk)
    return render(request, "endorsements/detail.html", {
        "endorsement": endorsement,
        "query_form": QueryForm(),
        "response_form": QueryResponseForm(),
    })


@login_required
def start_processing(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    org = request.user.profile.organization
    if org.organization_type not in {Organization.Type.TPA, Organization.Type.INSURER}:
        raise PermissionDenied
    WorkflowService.start_processing(endorsement, request.user)
    messages.success(request, "Processing started.")
    return redirect("endorsement_detail", pk=pk)


@login_required
def complete_request(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    org = request.user.profile.organization
    if org.organization_type not in {Organization.Type.TPA, Organization.Type.INSURER}:
        raise PermissionDenied
    WorkflowService.complete(
        endorsement,
        request.user,
        request.POST.get("external_reference", "").strip(),
    )
    messages.success(
        request,
        "Endorsement marked completed and requester notified.",
    )
    return redirect("endorsement_detail", pk=pk)


@login_required
def raise_query(request, pk):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    org = request.user.profile.organization
    if org.organization_type not in {Organization.Type.TPA, Organization.Type.INSURER}:
        raise PermissionDenied
    form = QueryForm(request.POST)
    if form.is_valid():
        WorkflowService.raise_query(
            endorsement,
            request.user,
            form.cleaned_data["subject"],
            form.cleaned_data["message"],
        )
        messages.success(request, "Query raised to the requester.")
    else:
        messages.error(request, "Please provide a subject and message.")
    return redirect("endorsement_detail", pk=pk)


@login_required
def answer_query(request, pk, query_id):
    if request.method != "POST":
        return HttpResponse(status=405)
    endorsement = _get_accessible_request(request.user, pk)
    query = get_object_or_404(
        endorsement.queries,
        pk=query_id,
        is_closed=False,
    )
    if (
        request.user.profile.organization_id != query.assigned_organization_id
        and not request.user.is_superuser
    ):
        raise PermissionDenied
    form = QueryResponseForm(request.POST)
    if form.is_valid():
        WorkflowService.answer_query(
            query,
            request.user,
            form.cleaned_data["response"],
        )
        messages.success(request, "Response submitted.")
    return redirect("endorsement_detail", pk=pk)
