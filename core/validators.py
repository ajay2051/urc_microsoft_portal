"""Reusable input validators (used by request serializers and models)."""

from __future__ import annotations

import re
import uuid

from django.core.exceptions import ValidationError

# RFC 1035 style hostname: labels of 1-63 chars, at least one dot, alphabetic TLD.
_DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)(?!-)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


def normalize_guid(value: str) -> str:
    """Return the canonical lowercase form of a GUID or raise ``ValueError``."""
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("must be a valid GUID") from exc


def normalize_domain(value: str) -> str:
    """Lower-case and validate a DNS domain such as ``contoso.com``."""
    candidate = str(value).strip().lower().rstrip(".")
    if not _DOMAIN_RE.match(candidate):
        raise ValueError("must be a valid DNS domain name, e.g. contoso.com")
    return candidate


def validate_guid(value: str) -> None:
    """Django-model validator wrapper around :func:`normalize_guid`."""
    try:
        normalize_guid(value)
    except ValueError as exc:
        raise ValidationError(str(exc), code="invalid_guid") from exc


def validate_domain(value: str) -> None:
    try:
        normalize_domain(value)
    except ValueError as exc:
        raise ValidationError(str(exc), code="invalid_domain") from exc
