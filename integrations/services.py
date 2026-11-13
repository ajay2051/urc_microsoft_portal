"""Microsoft tenant settings and directory synchronisation."""

from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import StaffUser
from audit.models import AuditOutcome
from audit.services import record_event
from clients.models import Client, ClientStatus, M365User
from core.db import run_atomic_unit
from core.errors import ApiError, Conflict, NotFound, UpstreamError, UpstreamUnavailable
from integrations.graph import GraphError, GraphUser, get_graph_client
from integrations.models import ConsentStatus, SyncRun, SyncStatus, TenantIntegration

logger = logging.getLogger(__name__)

BATCH = 500
#: Graph failure codes that mean "the client must fix something in their tenant".
CONSENT_CODES = {"consent_missing", "tenant_not_found"}


# --------------------------------------------------------------------------- #
# Tenant settings
# --------------------------------------------------------------------------- #
def upsert_integration(
    *, client_id: UUID, tenant_id: str, sync_enabled: bool, actor: StaffUser, request_id: str, ip_address: str | None
) -> TenantIntegration:
    if tenant_id == settings.ENTRA_TENANT_ID:
        raise ApiError(
            "The operator's own tenant cannot be registered as a client tenant.", code="own_tenant_not_allowed"
        )
    with transaction.atomic():
        client = Client.objects.select_for_update().filter(pk=client_id).first()
        if client is None:
            raise NotFound("Client not found.", code="client_not_found")
        if client.status == ClientStatus.OFFBOARDED:
            raise Conflict("Offboarded clients cannot be modified.", code="client_offboarded")
        if SyncRun.objects.filter(client=client, status=SyncStatus.RUNNING).exists():
            raise Conflict("A sync is running for this client; try again when it finishes.", code="sync_in_progress")
        other = TenantIntegration.objects.filter(tenant_id=tenant_id).exclude(pk=client.pk).first()
        if other is not None:
            raise Conflict("This Microsoft tenant is already linked to another client.", code="tenant_already_linked")

        integration = TenantIntegration.objects.filter(pk=client.pk).first()
        previous: str | None = None
        if integration is None:
            integration = TenantIntegration(client=client, tenant_id=tenant_id, sync_enabled=sync_enabled)
            action = "integration.created"
        else:
            previous = integration.tenant_id
            action = "integration.updated"
            integration.sync_enabled = sync_enabled
            if tenant_id != integration.tenant_id:
                # A different tenant invalidates everything we knew: consent, last sync, mirrored users.
                integration.tenant_id = tenant_id
                integration.consent_status = ConsentStatus.NOT_REQUESTED
                integration.consent_granted_at = None
                integration.last_sync_at = None
                M365User.objects.filter(client=client, removed_at__isnull=True).update(removed_at=timezone.now())
        integration.full_clean()
        integration.save()
        record_event(
            action=action,
            entity_type="tenant_integration",
            entity_id=str(client.pk),
            actor=actor,
            metadata={"tenant_id": tenant_id, "previous_tenant_id": previous, "sync_enabled": sync_enabled},
            request_id=request_id,
            ip_address=ip_address,
        )
        return integration


def _set_consent(client_id: UUID, status: str) -> TenantIntegration:
    integration = TenantIntegration.objects.get(pk=client_id)
    integration.consent_status = status
    integration.consent_granted_at = timezone.now() if status == ConsentStatus.GRANTED else None
    integration.save(update_fields=["consent_status", "consent_granted_at", "updated_at"])
    return integration


async def verify_consent(
    *, client_id: UUID, actor: StaffUser, request_id: str, ip_address: str | None
) -> TenantIntegration:
    """Prove consent by actually calling Graph; never trust a human's assertion."""
    integration = await TenantIntegration.objects.filter(pk=client_id).afirst()
    if integration is None:
        raise NotFound("No Microsoft tenant is configured for this client.", code="integration_not_found")
    outcome, status, error = AuditOutcome.SUCCESS, ConsentStatus.GRANTED, None
    try:
        await get_graph_client().probe(integration.tenant_id)
    except GraphError as exc:
        error = exc
        if exc.code in CONSENT_CODES:
            outcome, status = AuditOutcome.FAILURE, ConsentStatus.REVOKED
        else:
            outcome, status = AuditOutcome.FAILURE, None  # transient: do not change recorded consent

    def _persist() -> TenantIntegration:
        with transaction.atomic():
            current = _set_consent(client_id, status) if status else TenantIntegration.objects.get(pk=client_id)
            record_event(
                action="integration.consent_verified",
                entity_type="tenant_integration",
                entity_id=str(client_id),
                actor=actor,
                outcome=outcome,
                metadata={"result": status or "unknown", "error_code": getattr(error, "code", None)},
                request_id=request_id,
                ip_address=ip_address,
            )
            return current

    integration = await run_atomic_unit(_persist)
    if error is not None and status is None:
        raise _map_graph_error(error)
    return integration


# --------------------------------------------------------------------------- #
# Directory sync
# --------------------------------------------------------------------------- #
def _map_graph_error(exc: GraphError) -> ApiError:
    if exc.code in CONSENT_CODES:
        return Conflict(
            "Microsoft consent is missing or the tenant is not reachable; ask the client's admin to grant consent.",
            code="tenant_consent_missing",
        )
    if exc.code in {"throttled", "token_unavailable", "graph_unreachable", "token_unreachable"}:
        err = UpstreamUnavailable("Microsoft is temporarily unavailable. Please retry shortly.", code=exc.code)
        if exc.retry_after:
            err.headers = {"Retry-After": str(int(exc.retry_after) + 1)}
        return err
    return UpstreamError("Microsoft Graph returned an unexpected response.", code=exc.code)


def _begin_run(*, client_id: UUID, actor: StaffUser) -> tuple[SyncRun, str]:
    """Take the per-client sync lock (DB-enforced) and return the run + tenant id."""
    with transaction.atomic():
        client = Client.objects.select_for_update().filter(pk=client_id).first()
        if client is None:
            raise NotFound("Client not found.", code="client_not_found")
        if client.status == ClientStatus.OFFBOARDED:
            raise Conflict("Offboarded clients cannot be synced.", code="client_offboarded")
        integration = TenantIntegration.objects.filter(pk=client.pk).first()
        if integration is None:
            raise Conflict("No Microsoft tenant is configured for this client.", code="integration_not_configured")
        if not integration.sync_enabled:
            raise Conflict("Sync is disabled for this client.", code="sync_disabled")
        if integration.consent_status != ConsentStatus.GRANTED:
            raise Conflict("Tenant consent has not been verified.", code="tenant_consent_required")

        cutoff = timezone.now() - timedelta(seconds=settings.SYNC_STALE_AFTER_SECONDS)
        SyncRun.objects.filter(client=client, status=SyncStatus.RUNNING, started_at__lt=cutoff).update(
            status=SyncStatus.FAILED,
            finished_at=timezone.now(),
            error_code="stale",
            error_message="Superseded: the previous run never finished.",
        )
        try:
            with transaction.atomic():  # savepoint: an IntegrityError must not poison the outer transaction
                run = SyncRun.objects.create(client=client, started_by_id=actor.id)
        except IntegrityError as exc:
            raise Conflict("A sync is already running for this client.", code="sync_in_progress") from exc
        return run, integration.tenant_id


def _fail_run(run_id: UUID, *, code: str, message: str, actor: StaffUser, request_id: str, ip: str | None) -> None:
    with transaction.atomic():
        run = SyncRun.objects.select_for_update().get(pk=run_id)
        run.status, run.finished_at = SyncStatus.FAILED, timezone.now()
        run.error_code, run.error_message = code[:64], message[:500]
        run.save(update_fields=["status", "finished_at", "error_code", "error_message"])
        record_event(
            action="sync.microsoft",
            entity_type="client",
            entity_id=str(run.client_id),
            actor=actor,
            outcome=AuditOutcome.FAILURE,
            metadata={"run_id": str(run.pk), "error_code": code},
            request_id=request_id,
            ip_address=ip,
        )


class _SuspiciousSync(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)


def _apply_users(
    *, run_id: UUID, users: list[GraphUser], actor: StaffUser, request_id: str, ip: str | None
) -> SyncRun:
    """Diff the fetched directory against the mirror and apply it atomically."""
    now = timezone.now()
    with transaction.atomic():
        run = SyncRun.objects.select_for_update().get(pk=run_id)
        existing = {u.graph_id: u for u in M365User.objects.select_for_update().filter(client_id=run.client_id)}
        active_before = sum(1 for u in existing.values() if u.removed_at is None)
        fetched_ids = {u.id for u in users}
        to_remove = [u for gid, u in existing.items() if u.removed_at is None and gid not in fetched_ids]

        # Never let an empty/partial answer wipe the mirror.
        if active_before and not users:
            raise _SuspiciousSync("Graph returned zero users for a tenant that previously had users.")
        if active_before >= 10 and len(to_remove) / active_before > settings.SYNC_MAX_REMOVAL_RATIO:
            raise _SuspiciousSync(f"Sync would remove {len(to_remove)} of {active_before} users; refusing.")

        created: list[M365User] = []
        changed: list[M365User] = []
        fields = ["user_principal_name", "display_name", "mail", "job_title", "department", "account_enabled"]
        for gu in users:
            current = existing.get(gu.id)
            if current is None:
                created.append(
                    M365User(
                        client_id=run.client_id,
                        graph_id=gu.id,
                        user_principal_name=gu.user_principal_name,
                        display_name=gu.display_name,
                        mail=gu.mail,
                        job_title=gu.job_title,
                        department=gu.department,
                        account_enabled=gu.account_enabled,
                        last_synced_at=now,
                    )
                )
                continue
            dirty = current.removed_at is not None  # user came back
            for f in fields:
                if getattr(current, f) != getattr(gu, f if f != "user_principal_name" else "user_principal_name"):
                    setattr(current, f, getattr(gu, f))
                    dirty = True
            current.removed_at = None
            current.last_synced_at = now
            if dirty:
                changed.append(current)
        # Users that are unchanged still get their freshness timestamp bumped in one statement.
        unchanged_ids = [
            existing[g].pk for g in fetched_ids if g in existing and existing[g] not in changed
        ]
        for i in range(0, len(unchanged_ids), BATCH):
            M365User.objects.filter(pk__in=unchanged_ids[i : i + BATCH]).update(last_synced_at=now)
        M365User.objects.bulk_create(created, batch_size=BATCH)
        M365User.objects.bulk_update(changed, [*fields, "removed_at", "last_synced_at"], batch_size=BATCH)
        for i in range(0, len(to_remove), BATCH):
            M365User.objects.filter(pk__in=[u.pk for u in to_remove[i : i + BATCH]]).update(removed_at=now)

        run.status, run.finished_at = SyncStatus.SUCCEEDED, now
        run.created_count, run.updated_count, run.removed_count = len(created), len(changed), len(to_remove)
        run.save(update_fields=["status", "finished_at", "created_count", "updated_count", "removed_count"])
        TenantIntegration.objects.filter(pk=run.client_id).update(last_sync_at=now)
        record_event(
            action="sync.microsoft",
            entity_type="client",
            entity_id=str(run.client_id),
            actor=actor,
            metadata={
                "run_id": str(run.pk),
                "created": len(created),
                "updated": len(changed),
                "removed": len(to_remove),
            },
            request_id=request_id,
            ip_address=ip,
        )
        return run


async def run_sync(*, client_id: UUID, actor: StaffUser, request_id: str, ip_address: str | None) -> tuple[SyncRun, int]:
    run, tenant_id = await run_atomic_unit(lambda: _begin_run(client_id=client_id, actor=actor))
    try:
        users = await get_graph_client().list_users(tenant_id)
    except GraphError as exc:
        code, message = exc.code, exc.message
        await run_atomic_unit(
            lambda: _fail_run(run.pk, code=code, message=message, actor=actor, request_id=request_id, ip=ip_address)
        )
        if exc.code in CONSENT_CODES:
            await run_atomic_unit(lambda: _mark_consent_revoked(client_id))
        raise _map_graph_error(exc) from exc
    except Exception as exc:
        logger.exception("Unexpected error while fetching Graph users")
        await run_atomic_unit(
            lambda: _fail_run(run.pk, code="unexpected", message="Unexpected error.", actor=actor, request_id=request_id, ip=ip_address)
        )
        raise UpstreamError("Directory sync failed.", code="sync_failed") from exc

    try:
        finished = await run_atomic_unit(
            lambda: _apply_users(run_id=run.pk, users=users, actor=actor, request_id=request_id, ip=ip_address)
        )
    except _SuspiciousSync as exc:
        reason = str(exc)
        await run_atomic_unit(
            lambda: _fail_run(run.pk, code="suspicious_result", message=reason, actor=actor, request_id=request_id, ip=ip_address)
        )
        raise Conflict(str(exc), code="suspicious_sync_result") from exc
    except Exception as exc:
        logger.exception("Failed to apply Graph users")
        await run_atomic_unit(
            lambda: _fail_run(run.pk, code="apply_failed", message="Failed to store users.", actor=actor, request_id=request_id, ip=ip_address)
        )
        raise UpstreamError("Directory sync failed.", code="sync_failed") from exc
    total_active = await M365User.objects.filter(client_id=client_id, removed_at__isnull=True).acount()
    return finished, total_active


def _mark_consent_revoked(client_id: UUID) -> None:
    TenantIntegration.objects.filter(pk=client_id).update(
        consent_status=ConsentStatus.REVOKED, consent_granted_at=None, updated_at=timezone.now()
    )
