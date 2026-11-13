"""Starting the client onboarding workflow."""

from __future__ import annotations

from uuid import UUID

from django.db import IntegrityError, transaction

from accounts.models import StaffUser
from audit.services import record_event
from clients.models import ALLOWED_TRANSITIONS, Client, ClientStatus
from core.errors import Conflict, NotFound
from integrations.models import ConsentStatus, TenantIntegration
from onboarding.models import ACTIVE_ONBOARDING_STATUSES, ONBOARDING_STEPS, OnboardingRun


def start_onboarding(
    *, client_id: UUID, idempotency_key: str, actor: StaffUser, request_id: str, ip_address: str | None
) -> tuple[OnboardingRun, bool]:
    """Create an onboarding run. Returns ``(run, replayed)``.

    Preconditions (all enforced under a row lock so concurrent calls serialise):
    the client is a prospect, its tenant consent is *verified*, and no run is active.
    A repeated call with the same ``Idempotency-Key`` returns the original run.
    """
    with transaction.atomic():
        client = Client.objects.select_for_update().filter(pk=client_id).first()
        if client is None:
            raise NotFound("Client not found.", code="client_not_found")

        if idempotency_key:
            replay = OnboardingRun.objects.filter(client=client, idempotency_key=idempotency_key).first()
            if replay is not None:
                return replay, True

        if OnboardingRun.objects.filter(client=client, status__in=ACTIVE_ONBOARDING_STATUSES).exists():
            raise Conflict("An onboarding run is already active for this client.", code="onboarding_in_progress")
        if ClientStatus.ONBOARDING not in ALLOWED_TRANSITIONS[ClientStatus(client.status)]:
            raise Conflict(
                f"Onboarding cannot start for a client in status {client.status}.", code="invalid_client_status"
            )
        integration = TenantIntegration.objects.filter(pk=client.pk).first()
        if integration is None:
            raise Conflict("Configure the client's Microsoft tenant first.", code="integration_not_configured")
        if integration.consent_status != ConsentStatus.GRANTED:
            raise Conflict("Verify the client's Microsoft consent first.", code="tenant_consent_required")

        try:
            with transaction.atomic():
                run = OnboardingRun.objects.create(
                    client=client,
                    started_by_id=actor.id,
                    idempotency_key=idempotency_key,
                    steps=[{"name": name, "status": "pending"} for name in ONBOARDING_STEPS],
                )
        except IntegrityError as exc:  # lost a race on the partial unique constraints
            raise Conflict("An onboarding run is already active for this client.", code="onboarding_in_progress") from exc

        previous = client.status
        client.status = ClientStatus.ONBOARDING
        client.save(update_fields=["status", "updated_at"])
        record_event(
            action="onboarding.start",
            entity_type="client",
            entity_id=str(client.pk),
            actor=actor,
            metadata={"run_id": str(run.pk), "previous_status": previous},
            request_id=request_id,
            ip_address=ip_address,
        )
        return run, False
