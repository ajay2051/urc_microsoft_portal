"""HTTP routes: tenant settings, Microsoft sync and the mirrored Graph users."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from django_bolt import Depends, Request
from msgspec import Meta

from accounts.models import StaffUser
from clients.api import user_out
from clients.models import M365User
from clients.schemas import M365UserOut
from clients.services import get_client_or_404
from core.auth import admin_only, current_staff
from core.db import run_atomic_unit
from core.errors import NotFound
from core.http import client_ip, request_id
from core.pagination import DEFAULT_PAGE_SIZE, Page, PageNumber, PageSize, build_page, paginate_bounds
from core.router import protected_router
from integrations.models import TenantIntegration
from integrations.schemas import (
    IntegrationOut,
    IntegrationUpsertRequest,
    SyncRequest,
    SyncResultOut,
)
from integrations.services import run_sync, upsert_integration, verify_consent

integrations_router = protected_router("/integrations", "integrations")
sync_router = protected_router("/sync", "sync")
graph_router = protected_router("/graph", "graph")

SearchText = Annotated[str, Meta(min_length=1, max_length=100)]


def _integration_out(i: TenantIntegration) -> IntegrationOut:
    return IntegrationOut(
        client_id=i.pk,
        tenant_id=i.tenant_id,
        consent_status=i.consent_status,
        consent_granted_at=i.consent_granted_at,
        sync_enabled=i.sync_enabled,
        last_sync_at=i.last_sync_at,
        updated_at=i.updated_at,
    )


@integrations_router.get("/{client_id}", summary="Get a client's Microsoft tenant settings")
async def get_integration(client_id: UUID, staff: StaffUser = Depends(current_staff)) -> IntegrationOut:
    integration = await TenantIntegration.objects.filter(pk=client_id).afirst()
    if integration is None:
        await get_client_or_404(client_id)  # 404 client_not_found vs integration_not_found
        raise NotFound("No Microsoft tenant is configured for this client.", code="integration_not_found")
    return _integration_out(integration)


@integrations_router.put("/{client_id}", summary="Create or update tenant settings (admin)")
async def put_integration(
    request: Request,
    client_id: UUID,
    body: IntegrationUpsertRequest,
    admin: StaffUser = Depends(admin_only),
) -> IntegrationOut:
    rid, ip = request_id(request), client_ip(request)
    integration = await run_atomic_unit(
        lambda: upsert_integration(
            client_id=client_id,
            tenant_id=body.tenant_id,
            sync_enabled=body.sync_enabled,
            actor=admin,
            request_id=rid,
            ip_address=ip,
        )
    )
    return _integration_out(integration)


@integrations_router.post("/{client_id}/verify-consent", summary="Verify admin consent by calling Graph (admin)")
async def post_verify_consent(
    request: Request, client_id: UUID, admin: StaffUser = Depends(admin_only)
) -> IntegrationOut:
    integration = await verify_consent(
        client_id=client_id, actor=admin, request_id=request_id(request), ip_address=client_ip(request)
    )
    return _integration_out(integration)


@sync_router.post("/microsoft", summary="Sync Microsoft 365 users for one client")
async def sync_microsoft(
    request: Request, body: SyncRequest, staff: StaffUser = Depends(current_staff)
) -> SyncResultOut:
    run, total_active = await run_sync(
        client_id=body.client_id, actor=staff, request_id=request_id(request), ip_address=client_ip(request)
    )
    return SyncResultOut(
        run_id=run.pk,
        status=run.status,
        created=run.created_count,
        updated=run.updated_count,
        removed=run.removed_count,
        total_active=total_active,
        finished_at=run.finished_at,
    )


@graph_router.get("/users", summary="Mirrored Microsoft 365 users of one client")
async def list_graph_users(
    client_id: UUID,
    staff: StaffUser = Depends(current_staff),
    q: SearchText | None = None,
    include_removed: bool = False,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> Page[M365UserOut]:
    await get_client_or_404(client_id)
    qs = M365User.objects.filter(client_id=client_id)
    if not include_removed:
        qs = qs.filter(removed_at__isnull=True)
    if q:
        from django.db.models import Q

        qs = qs.filter(Q(display_name__icontains=q) | Q(user_principal_name__icontains=q) | Q(mail__icontains=q))
    qs = qs.order_by("display_name", "user_principal_name", "id")
    total = await qs.acount()
    offset, limit = paginate_bounds(page, page_size)
    return build_page([user_out(u) async for u in qs[offset : offset + limit]], page=page, page_size=page_size, total=total)
