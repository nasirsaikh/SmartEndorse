from django.db.models import Q
from .models import EndorsementRequest, Organization, Policy


def accessible_policies(user, require_create=False):
    if not user.is_authenticated:
        return Policy.objects.none()
    if user.is_superuser:
        return Policy.objects.filter(is_active=True)
    try:
        org = user.profile.organization
    except Exception:
        return Policy.objects.none()

    if org.organization_type == Organization.Type.INSURER:
        return Policy.objects.filter(insurer=org, is_active=True)
    if org.organization_type == Organization.Type.TPA:
        return Policy.objects.filter(tpa=org, is_active=True)

    filters = {
        "access_grants__organization": org,
        "is_active": True,
    }
    if require_create:
        filters["access_grants__can_create"] = True
    return Policy.objects.filter(**filters).distinct()


def accessible_endorsements(user):
    policies = accessible_policies(user)
    qs = EndorsementRequest.objects.filter(policy__in=policies).select_related(
        "policy",
        "policy__client",
        "policy__tpa",
        "requester",
        "requester_organization",
    )
    if user.is_superuser:
        return qs
    try:
        org = user.profile.organization
    except Exception:
        return EndorsementRequest.objects.none()
    if org.organization_type in {Organization.Type.INSURER, Organization.Type.TPA}:
        return qs
    return qs.filter(
        Q(requester_organization=org) |
        Q(policy__access_grants__organization=org)
    ).distinct()
