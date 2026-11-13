"""Per-request metadata helpers."""

from __future__ import annotations

import ipaddress
import re
import uuid

from django.conf import settings
from django_bolt import Request

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def request_id(request: Request) -> str:
    """Correlation id: accept a well-formed inbound X-Request-ID, otherwise mint one."""
    inbound = request.headers.get("x-request-id", "")
    return inbound if _REQUEST_ID_RE.match(inbound) else uuid.uuid4().hex


def client_ip(request: Request) -> str | None:
    """Best-effort client IP for audit records.

    ``X-Forwarded-For`` is attacker-controlled unless a trusted proxy overwrites
    it, so it is only honoured when ``TRUST_X_FORWARDED_FOR`` is enabled.
    """
    if not settings.TRUST_X_FORWARDED_FOR:
        return None
    forwarded = request.headers.get("x-forwarded-for", "")
    if not forwarded:
        return None
    candidate = forwarded.split(",")[-1].strip()
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None
