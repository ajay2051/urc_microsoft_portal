"""Write path for the audit log. Always call inside the mutating transaction so
the business change and its audit record commit (or roll back) together."""

from __future__ import annotations

from typing import Any

from audit.models import AuditEvent, AuditOutcome

# Keys that must never be persisted, whatever a caller passes in.
_REDACT = {"secret", "password", "token", "client_secret", "authorization"}
_MAX_VALUE_LEN = 500


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): ("[redacted]" if str(k).lower() in _REDACT else _sanitize(v)) for k, v in list(value.items())[:50]
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value[:50]]
    if isinstance(value, str):
        return value[:_MAX_VALUE_LEN]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:_MAX_VALUE_LEN]


def record_event(
    *,
    action: str,
    entity_type: str,
    entity_id: str = "",
    actor: Any | None = None,
    actor_label: str | None = None,
    outcome: str = AuditOutcome.SUCCESS,
    metadata: dict[str, Any] | None = None,
    request_id: str = "",
    ip_address: str | None = None,
) -> AuditEvent:
    label = actor_label or (getattr(actor, "email", None) or "system")
    return AuditEvent.objects.create(
        actor_id=getattr(actor, "id", None),
        actor_label=label[:254],
        action=action[:64],
        entity_type=entity_type[:64],
        entity_id=str(entity_id)[:64],
        outcome=outcome,
        metadata=_sanitize(metadata or {}),
        request_id=request_id[:64],
        ip_address=ip_address,
    )
