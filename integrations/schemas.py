"""Request/response contracts for Microsoft integration endpoints."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import msgspec
from django_bolt.serializers import field_validator

from core.schemas import StrictRequest

from core.validators import normalize_guid


class IntegrationUpsertRequest(StrictRequest):
    tenant_id: str
    sync_enabled: bool = True

    @field_validator("tenant_id")
    def _guid(cls, value: str) -> str:  # noqa: N805
        return normalize_guid(value)


class IntegrationOut(msgspec.Struct):
    client_id: UUID
    tenant_id: str
    consent_status: str
    consent_granted_at: datetime | None
    sync_enabled: bool
    last_sync_at: datetime | None
    updated_at: datetime


class SyncRequest(StrictRequest):
    client_id: UUID


class SyncResultOut(msgspec.Struct):
    run_id: UUID
    status: str
    created: int
    updated: int
    removed: int
    total_active: int
    finished_at: datetime | None
