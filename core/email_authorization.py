"""Policy-scoped sender grants and workflow attribution shared by intake and Admin."""
from dataclasses import dataclass

from django.db.models import Q
from django.db.models.functions import Lower, Trim
from django.utils import timezone

from .access import accessible_policies, can_create_endorsement
from .models import EmailAuthority, User


@dataclass(frozen=True)
class AuthorityDecision:
    authority: EmailAuthority | None = None
    actor: User | None = None
    reason: str = ""


def sender_users(sender):
    """Address grants are case insensitive; tolerate whitespace in legacy user rows."""
    return list(User.objects.annotate(normalized_email=Lower(Trim("email"))).filter(
        normalized_email=sender.strip().lower(), is_active=True,
    ).select_related("profile__organization").order_by("pk")[:2])


def authority_actor(authority, sender_user):
    if authority.user_id:
        return authority.user
    if authority.group_id:
        # Group grants act as the actual member, never as a delegated processing user.
        return sender_user
    # An explicit delegate belongs to the exact-address grant, even if the sender
    # also has a portal account with a different role or organization.
    return authority.processing_user or sender_user


def actor_error(authority, actor):
    if not actor:
        return "Select a Processing user for this exact email address; no unique active portal user matches the sender."
    if not actor.is_active:
        return "The workflow user is inactive. Select an active Processing user or reactivate the authorized portal user."
    if not can_create_endorsement(actor):
        return "The workflow user's role cannot create endorsements. Assign an allowed role in User profiles or select an eligible Processing user."
    profile = getattr(actor, "profile", None)
    if profile and profile.organization_id != authority.organization_id:
        return "The workflow user belongs to a different organization from this Email authority. Select a user in the grant organization."
    if not accessible_policies(actor, require_create=True).filter(pk=authority.policy_id).exists():
        return "The workflow user has no creation access to this policy. For client/broker organizations, add Admin > Policy access with Can create enabled."
    return ""


def inactive_sender(sender):
    return User.objects.annotate(normalized_email=Lower(Trim("email"))).filter(
        normalized_email=sender.strip().lower(), is_active=False,
    ).exists()


def authority_configuration_error(authority):
    """Validate fixed actors when a grant is saved; group members are checked at runtime."""
    if not authority.is_active:
        return ""
    if not authority.organization.is_active:
        return "The grant organization is inactive."
    if not authority.policy.is_active:
        return "The grant policy is inactive."
    if authority.group_id:
        return ""
    sender = authority.email_address or (authority.user.email if authority.user_id else "")
    if not sender.strip():
        return "Set the selected portal user's Email address in Admin > Users; it must match the email's From address."
    if inactive_sender(sender):
        return "A portal user with this sender address is inactive. Reactivate that identity before enabling the grant."
    users = sender_users(sender)
    if authority.user_id and len(users) != 1:
        return "Portal user grants require a unique active user email address. Correct duplicate email addresses in Admin > Users."
    sender_user = users[0] if len(users) == 1 else None
    return actor_error(authority, authority_actor(authority, sender_user))


def authority_decision(sender, policy, endorsement_type=""):
    sender = sender.strip().lower()
    if not sender:
        return AuthorityDecision(reason="The email must contain exactly one From address.")
    if not policy:
        return AuthorityDecision(reason="No single authorized policy could be identified. Include its policy number in the email or set Default policy in Admin > Mailbox configurations.")
    if not policy.is_active:
        return AuthorityDecision(reason=f"Policy {policy.policy_number} is inactive.")
    if inactive_sender(sender):
        return AuthorityDecision(reason="A portal user with this From address is inactive. An exact-address grant cannot bypass a deactivated identity.")
    users = sender_users(sender)
    sender_user = users[0] if len(users) == 1 else None
    identities = Q(normalized_email=sender)
    if sender_user:
        identities |= Q(user=sender_user) | Q(group__in=sender_user.groups.all())
    candidates = EmailAuthority.objects.annotate(normalized_email=Lower(Trim("email_address"))).filter(
        identities, policy=policy,
    ).select_related("organization", "user__profile__organization", "processing_user__profile__organization").order_by("pk")
    today = timezone.localdate()
    reasons = []
    for authority in candidates:
        if not authority.is_active:
            reason = "This Email authority is inactive."
        elif not authority.organization.is_active:
            reason = "The grant organization is inactive."
        elif authority.valid_from and authority.valid_from > today:
            reason = f"This Email authority starts on {authority.valid_from}; it is not valid today."
        elif authority.valid_until and authority.valid_until < today:
            reason = f"This Email authority expired on {authority.valid_until}."
        elif endorsement_type and authority.permitted_types and endorsement_type not in authority.permitted_types:
            reason = f"This Email authority does not permit {endorsement_type}. Update Permitted types in Admin."
        else:
            actor = authority_actor(authority, sender_user)
            reason = actor_error(authority, actor)
            if not reason:
                return AuthorityDecision(authority=authority, actor=actor)
        reasons.append(f"{authority.name}: {reason}")
    if reasons:
        return AuthorityDecision(reason="\n".join(reasons[:3]))
    if len(users) > 1:
        return AuthorityDecision(reason="More than one active portal user has this From address. User/group grants require a unique sender; fix duplicate user emails or use an exact-address grant with an explicit Processing user.")
    return AuthorityDecision(reason=f"No Email authority matches From address {sender} for policy {policy.policy_number}. Check Admin > Email authorities: exact address (or the user's email/group), Policy and Active.")


def resolve_authority(sender, policy, endorsement_type=""):
    decision = authority_decision(sender, policy, endorsement_type)
    return decision.authority, decision.actor
