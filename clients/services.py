"""Business logic for client accounts."""

from __future__ import annotations

from uuid import UUID

from django.db import transaction
from django.db.models import Count, Q

from accounts.models import StaffUser
from audit.models import AuditOutcome
from audit.services import record_event
from clients.models import ADMIN_ONLY_TARGETS, ALLOWED_TRANSITIONS, Client, ClientStatus
from core.errors import Conflict, Forbidden, NotFound

DETAIL_USER_LIMIT = 100


def clients_queryset(*, status: str | None, query: str | None):
    qs = Client.objects.annotate(
        m365_user_count=Count("m365_users", filter=Q(m365_users__removed_at__isnull=True))
    )
    if status:
        qs = qs.filter(status=status)
    if query:
        qs = qs.filter(Q(name__icontains=query) | Q(primary_domain__icontains=query))
    return qs.order_by("name", "id")


async def get_client_or_404(client_id: UUID) -> Client:
    client = await Client.objects.filter(pk=client_id).afirst()
    if client is None:
        raise NotFound("Client not found.", code="client_not_found")
    return client


def change_status(
    *, client_id: UUID, target: str, reason: str, actor: StaffUser, request_id: str, ip_address: str | None
) -> Client:
    """Apply a lifecycle transition atomically (row lock) and audit it."""
    # Authorisation does not depend on current state, so decide (and audit a denial)
    # *before* opening the transaction; otherwise the denial record would roll back.
    if ClientStatus(target) in ADMIN_ONLY_TARGETS and not actor.is_admin:
        record_event(
            action="client.status_change",
            entity_type="client",
            entity_id=str(client_id),
            actor=actor,
            outcome=AuditOutcome.DENIED,
            metadata={"to": target},
            request_id=request_id,
            ip_address=ip_address,
        )
        raise Forbidden(f"Only administrators may set a client to {target}.", code="admin_required")

    with transaction.atomic():
        client = Client.objects.select_for_update().filter(pk=client_id).first()
        if client is None:
            raise NotFound("Client not found.", code="client_not_found")
        current = client.status
        if target == current:
            raise Conflict(f"Client is already {current}.", code="status_unchanged")
        if ClientStatus(target) not in ALLOWED_TRANSITIONS[ClientStatus(current)]:
            raise Conflict(
                f"Cannot move a client from {current} to {target}.",
                code="invalid_transition",
                extra={"from": current, "to": target},
            )
        client.status = target
        client.save(update_fields=["status", "updated_at"])
        record_event(
            action="client.status_change",
            entity_type="client",
            entity_id=str(client.pk),
            actor=actor,
            metadata={"from": current, "to": target, "reason": reason},
            request_id=request_id,
            ip_address=ip_address,
        )
        return client
