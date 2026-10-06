"""Read Admin-managed mailbox credentials without disclosing them in errors."""
import re

from django.core.exceptions import ValidationError


ENVIRONMENT_REFERENCE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class MailboxCredentialError(RuntimeError):
    """An expected configuration error that can be reported without a traceback."""


def validate_credential_reference(value):
    # Retained for historical migration 0009; current mailbox fields store secrets.
    if not isinstance(value, str) or not ENVIRONMENT_REFERENCE_PATTERN.fullmatch(value):
        raise ValidationError(
            "Enter an environment variable name using letters, digits and underscores, "
            "starting with a letter or underscore. Store the actual password/client secret "
            "in .env or the worker environment, not in this reference field.",
            code="invalid_credential_reference",
        )


def credential(value, *, label="Mailbox password"):
    if not isinstance(value, str) or not value.strip():
        raise MailboxCredentialError(
            f"{label} is missing. Enter its actual value in Admin > Mailbox configurations and save the mailbox."
        )
    return value
