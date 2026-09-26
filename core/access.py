from django.db.models import Q

from .models import EndorsementApproval, EndorsementRequest, Organization, Policy, UserProfile


CREATE_ROLES = {
    UserProfile.Role.SUPER_ADMIN,
    UserProfile.Role.INSURER_ADMIN,
    UserProfile.Role.INSURER_MANAGER,
    UserProfile.Role.INSURER_SUPERVISOR,
    UserProfile.Role.INSURER_STAFF,
    UserProfile.Role.UNDERWRITER,
    UserProfile.Role.CLIENT_ADMIN,
    UserProfile.Role.REQUESTER,
    UserProfile.Role.BROKER_ADMIN,
    UserProfile.Role.BROKER_USER,
    UserProfile.Role.AGENT,
    UserProfile.Role.CHANNEL_PARTNER,
}
TPA_PROCESS_ROLES = {UserProfile.Role.TPA_ADMIN, UserProfile.Role.TPA_MANAGER, UserProfile.Role.TPA_PROCESSOR}
INSURER_APPROVER_ROLES = {UserProfile.Role.SUPER_ADMIN, UserProfile.Role.INSURER_ADMIN, UserProfile.Role.INSURER_MANAGER, UserProfile.Role.INSURER_SUPERVISOR, UserProfile.Role.UNDERWRITER}
CLIENT_APPROVER_ROLES = {UserProfile.Role.CLIENT_ADMIN, UserProfile.Role.REQUESTER}
INSURER_OPERATION_ROLES = INSURER_APPROVER_ROLES | {UserProfile.Role.INSURER_STAFF}


def _profile(user):
    try:
        return user.profile
    except Exception:
        return None


def can_create_endorsement(user):
    if user.is_superuser:
        return True
    p = _profile(user)
    return bool(p and p.role in CREATE_ROLES and p.organization.organization_type != Organization.Type.TPA)


def accessible_policies(user, require_create=False):
    if not user.is_authenticated:
        return Policy.objects.none()
    if require_create and not can_create_endorsement(user):
        return Policy.objects.none()
    if user.is_superuser:
        return Policy.objects.filter(is_active=True)
    p = _profile(user)
    if not p:
        return Policy.objects.none()
    org = p.organization
    if org.organization_type == Organization.Type.INSURER:
        return Policy.objects.filter(insurer=org, is_active=True)
    if org.organization_type == Organization.Type.TPA:
        return Policy.objects.filter(tpa=org, is_active=True)
    filters = {"access_grants__organization": org, "is_active": True}
    if require_create:
        filters["access_grants__can_create"] = True
    return Policy.objects.filter(**filters).distinct()


def accessible_endorsements(user):
    policies = accessible_policies(user)
    qs = EndorsementRequest.objects.filter(policy__in=policies).select_related(
        "policy", "policy__client", "policy__tpa", "policy__insurer",
        "policy__intake_sla", "policy__validation_sla", "policy__insurer_sla",
        "policy__tpa_sla", "policy__client_query_sla",
        "requester", "requester_organization"
    )
    if user.is_superuser:
        return qs
    p = _profile(user)
    if not p:
        return EndorsementRequest.objects.none()
    if p.organization.organization_type in {Organization.Type.INSURER, Organization.Type.TPA}:
        return qs
    return qs.filter(Q(requester_organization=p.organization) | Q(policy__access_grants__organization=p.organization)).distinct()


def can_edit_request(user, request_obj):
    if user.is_superuser:
        return True
    p = _profile(user)
    if not p:
        return False
    if p.organization_id == request_obj.requester_organization_id and p.role not in {UserProfile.Role.CLIENT_VIEWER, UserProfile.Role.AUDITOR}:
        return request_obj.status in {EndorsementRequest.Status.NEEDS_INFO, EndorsementRequest.Status.DRAFT, EndorsementRequest.Status.TPA_QUERY, EndorsementRequest.Status.REJECTED}
    return bool(
        p.organization_id == request_obj.policy.insurer_id
        and p.role in INSURER_OPERATION_ROLES
        and request_obj.status in {
            EndorsementRequest.Status.DRAFT,
            EndorsementRequest.Status.NEEDS_INFO,
            EndorsementRequest.Status.PENDING_INSURER_APPROVAL,
            EndorsementRequest.Status.TPA_QUERY,
            EndorsementRequest.Status.REJECTED,
        }
    )


def can_tpa_process(user, request_obj):
    if user.is_superuser:
        return True
    p = _profile(user)
    return bool(p and p.organization_id == request_obj.policy.tpa_id and p.role in TPA_PROCESS_ROLES)


def can_insurer_operate(user, request_obj):
    if user.is_superuser:
        return True
    p = _profile(user)
    return bool(p and p.organization_id == request_obj.policy.insurer_id and p.role in INSURER_OPERATION_ROLES)


def can_decide_approval(user, approval):
    if user.is_superuser:
        return True
    p = _profile(user)
    if not p or p.organization_id != approval.assigned_organization_id:
        return False
    if p.organization.organization_type == Organization.Type.INSURER:
        return p.role in INSURER_APPROVER_ROLES
    if p.organization.organization_type == Organization.Type.CLIENT:
        return p.role in CLIENT_APPROVER_ROLES
    return False
