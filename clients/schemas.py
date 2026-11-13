"""Request/response contracts for the clients API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import msgspec
from django_bolt.serializers import field_validator
from msgspec import Meta

from clients.models import ClientStatus
from core.pagination import choice
from core.schemas import StrictRequest

StatusLiteral = Literal["prospect", "onboarding", "active", "suspended", "offboarded"]

StatusQuery = choice(*[s.value for s in ClientStatus])

assert {s.value for s in ClientStatus} == set(StatusLiteral.__args__)  # keep contract & model in sync


class StatusChangeRequest(StrictRequest):
    status: StatusLiteral
    reason: Annotated[str, Meta(min_length=3, max_length=500, description="Why the status is changing")]

    @field_validator("reason")
    def _reason_not_blank(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) < 3:
            raise ValueError("reason must contain at least 3 non-whitespace characters")
        return cleaned


class IntegrationSummary(msgspec.Struct):
    tenant_id: str
    consent_status: str
    sync_enabled: bool
    last_sync_at: datetime | None


class M365UserOut(msgspec.Struct):
    id: UUID
    graph_id: str
    user_principal_name: str
    display_name: str
    mail: str
    job_title: str
    department: str
    account_enabled: bool
    last_synced_at: datetime


class ClientOut(msgspec.Struct):
    id: UUID
    name: str
    primary_domain: str
    contact_email: str
    status: str
    m365_user_count: int
    created_at: datetime
    updated_at: datetime


class ClientDetailOut(msgspec.Struct):
    id: UUID
    name: str
    primary_domain: str
    contact_email: str
    status: str
    created_at: datetime
    updated_at: datetime
    integration: IntegrationSummary | None
    m365_user_count: int
    users: list[M365UserOut]
    users_truncated: bool
