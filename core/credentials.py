"""Resolve mailbox credentials without including submitted values in error messages."""
import os
import re

from django.core.exceptions import ValidationError


ENVIRONMENT_REFERENCE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class MailboxCredentialError(RuntimeError):
    """An expected configuration error that can be reported without a traceback."""


def validate_credential_reference(value):
    if not isinstance(value, str) or not ENVIRONMENT_REFERENCE_PATTERN.fullmatch(value):
        raise ValidationError(
            "Enter an environment variable name using letters, digits and underscores, "
            "starting with a letter or underscore. Store the actual password/client secret "
            "in .env or the worker environment, not in this reference field.",
            code="invalid_credential_reference",
        )


def credential(name, *, label="Mailbox credential reference", example="ENDORSEMENT_IMAP_PASSWORD"):
    guidance = (
        f"Use an environment variable name such as {example} in Admin. "
        "Set its secret value in .env next to manage.py or the worker environment, "
        "then restart the web app/email worker."
    )
    if not name:
        raise MailboxCredentialError(f"{label} is not configured. {guidance}")
    try:
        validate_credential_reference(name)
    except ValidationError:
        raise MailboxCredentialError(
            f"{label} must be an environment variable name, not a password or client secret. {guidance}"
        ) from None
    value = os.getenv(name, "")
    if not value or not value.strip():
        # Even a syntactically valid name might be a pasted alphanumeric secret.
        raise MailboxCredentialError(f"The environment variable named by {label} is missing or empty. {guidance}")
    return value
