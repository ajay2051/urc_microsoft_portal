from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from django_bolt import Depends
from msgspec import Meta

from accounts.models import StaffUser
from audit.models import AuditEvent, AuditOutcome
from audit.schemas import AuditEventOut
from core.auth import admin_only
from core.errors import UnprocessableEntity
from core.pagination import DEFAULT_PAGE_SIZE, Page, PageNumber, PageSize, build_page, choice, paginate_bounds
from core.router import protected_router

router = protected_router("/audit", "audit")

Dotted = Annotated[str, Meta(pattern=r"^[a-z0-9_.]{1,64}$")]
EntityId = Annotated[str, Meta(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
OutcomeQuery = choice(*[o.value for o in AuditOutcome])


@router.get("", summary="Audit log (admin only)")
async def list_audit(
    admin: StaffUser = Depends(admin_only),
    actor_id: UUID | None = None,
    action: Dotted | None = None,
    entity_type: Dotted | None = None,
    entity_id: EntityId | None = None,
    outcome: OutcomeQuery | None = None,
    occurred_after: datetime | None = None,
    occurred_before: datetime | None = None,
    page: PageNumber = 1,
    page_size: PageSize = DEFAULT_PAGE_SIZE,
) -> Page[AuditEventOut]:
    for name, value in (("occurred_after", occurred_after), ("occurred_before", occurred_before)):
        if value is not None and value.tzinfo is None:
            raise UnprocessableEntity(f"{name} must include a timezone offset, e.g. 2026-11-01T00:00:00Z")
    if occurred_after and occurred_before and occurred_after > occurred_before:
        raise UnprocessableEntity("occurred_after must not be later than occurred_before")

    qs = AuditEvent.objects.all()
    filters = {
        "actor_id": actor_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "outcome": outcome,
        "occurred_at__gte": occurred_after,
        "occurred_at__lte": occurred_before,
    }
    qs = qs.filter(**{k: v for k, v in filters.items() if v is not None})
    total = await qs.acount()
    offset, limit = paginate_bounds(page, page_size)
    items = [
        AuditEventOut(
            id=e.id,
            occurred_at=e.occurred_at,
            actor_id=e.actor_id,
            actor_label=e.actor_label,
            action=e.action,
            entity_type=e.entity_type,
            entity_id=e.entity_id,
            outcome=e.outcome,
            metadata=e.metadata,
            request_id=e.request_id,
        )
        async for e in qs[offset : offset + limit]
    ]
    return build_page(items, page=page, page_size=page_size, total=total)
