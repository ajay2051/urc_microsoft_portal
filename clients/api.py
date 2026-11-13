"""HTTP routes for client accounts."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from django_bolt import Depends, Request
from msgspec import Meta

from accounts.models import StaffUser
from clients.models import M365User
from clients.schemas import (
    ClientDetailOut,
    ClientOut,
    IntegrationSummary,
    M365UserOut,
    StatusChangeRequest,
    StatusQuery,
)
from clients.services import (
    DETAIL_USER_LIMIT,
    change_status,
    clients_queryset,
    get_client_or_404,
)
from core.auth import current_staff
from core.db import run_atomic_unit
from core.http import client_ip, request_id
from core.pagination import DEFAULT_PAGE_SIZE, Page, PageNumber, PageSize, build_page, paginate_bounds
from core.router import protected_router
from integrations.models import TenantIntegration

router = protected_router("/clients", "clients")

SearchText = Annotated[str, Meta(min_length=1, max_length=100)]


def _client_out(client) -> ClientOut:
    return ClientOut(
        id=client.id,
        name=client.name,
        primary_domain=client.primary_domain,
        contact_email=client.contact_email,
        status=client.status,
        m365_user_count=getattr(client, "m365_user_count", 0),
        created_at=client.created_at,
        updated_at=client.updated_at,
    )


def user_out(u: M365User) -> M365UserOut:
    return M365UserOut(
        id=u.id,
        graph_id=u.graph_id,
        user_principal_name=u.user_principal_name,
        display_name=u.display_name,
        mail=u.mail,
        job_title=u.job_title,
        department=u.department,
        account_enabled=u.account_enabled,
        last_synced_at=u.last_synced_at,
    )


@router.get("", summary="List clients")
async def list_clients(
    staff: StaffUser = Depends(current_staff),
    status: StatusQuery | None = None,
    q: SearchText | None = None,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> Page[ClientOut]:
    qs = clients_queryset(status=status, query=q)
    total = await qs.acount()
    offset, limit = paginate_bounds(page, page_size)
    items = [_client_out(c) async for c in qs[offset : offset + limit]]
    return build_page(items, page=page, page_size=page_size, total=total)


@router.get("/{client_id}", summary="Get one client with its Microsoft 365 users")
async def get_client(client_id: UUID, staff: StaffUser = Depends(current_staff)) -> ClientDetailOut:
    client = await get_client_or_404(client_id)
    integration = await TenantIntegration.objects.filter(pk=client.pk).afirst()
    active_users = M365User.objects.filter(client_id=client.pk, removed_at__isnull=True)
    user_count = await active_users.acount()
    users = [user_out(u) async for u in active_users.order_by("display_name", "user_principal_name")[:DETAIL_USER_LIMIT]]
    return ClientDetailOut(
        id=client.id,
        name=client.name,
        primary_domain=client.primary_domain,
        contact_email=client.contact_email,
        status=client.status,
        created_at=client.created_at,
        updated_at=client.updated_at,
        integration=(
            IntegrationSummary(
                tenant_id=integration.tenant_id,
                consent_status=integration.consent_status,
                sync_enabled=integration.sync_enabled,
                last_sync_at=integration.last_sync_at,
            )
            if integration
            else None
        ),
        m365_user_count=user_count,
        users=users,
        users_truncated=user_count > len(users),
    )


@router.patch("/{client_id}/status", summary="Change a client's lifecycle status")
async def update_client_status(
    request: Request,
    client_id: UUID,
    body: StatusChangeRequest,
    staff: StaffUser = Depends(current_staff),
) -> ClientOut:
    rid, ip = request_id(request), client_ip(request)
    client = await run_atomic_unit(
        lambda: change_status(
            client_id=client_id, target=body.status, reason=body.reason, actor=staff, request_id=rid, ip_address=ip
        )
    )
    client.m365_user_count = await M365User.objects.filter(client_id=client.pk, removed_at__isnull=True).acount()
    return _client_out(client)
