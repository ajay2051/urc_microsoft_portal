"""Public health probes and the current-user endpoint."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import msgspec
from django.db import connection
from django_bolt import Depends, Router
from django_bolt.concurrency import run_in_orm_executor

from accounts.models import StaffUser
from core.auth import current_staff
from core.errors import UpstreamUnavailable
from core.router import protected_router

public_router = Router(prefix="", tags=["health"])
me_router = protected_router("/me", "me")


class Health(msgspec.Struct):
    status: str


class MeOut(msgspec.Struct):
    id: UUID
    email: str
    display_name: str
    role: str
    created_at: datetime


@public_router.get("/health", summary="Liveness probe")
async def health() -> Health:
    return Health(status="ok")


def _db_ping() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()


@public_router.get("/ready", summary="Readiness probe (checks the database)")
async def ready() -> Health:
    try:
        await run_in_orm_executor(_db_ping)
    except Exception as exc:  # noqa: BLE001 - never leak driver details to probes
        raise UpstreamUnavailable("Database unavailable.", code="db_unavailable") from exc
    return Health(status="ready")


@me_router.get("", summary="Current staff profile")
async def me(staff: StaffUser = Depends(current_staff)) -> MeOut:
    return MeOut(
        id=staff.id, email=staff.email, display_name=staff.display_name, role=staff.role, created_at=staff.created_at
    )
