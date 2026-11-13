from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import msgspec

from core.schemas import StrictRequest


class OnboardingStartRequest(StrictRequest):
    client_id: UUID


class OnboardingRunOut(msgspec.Struct):
    id: UUID
    client_id: UUID
    status: str
    steps: list[dict[str, Any]]
    created_at: datetime
    completed_at: datetime | None
    replayed: bool = False
