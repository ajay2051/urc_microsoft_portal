from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import msgspec


class AuditEventOut(msgspec.Struct):
    id: UUID
    occurred_at: datetime
    actor_id: UUID | None
    actor_label: str
    action: str
    entity_type: str
    entity_id: str
    outcome: str
    metadata: dict[str, Any]
    request_id: str
