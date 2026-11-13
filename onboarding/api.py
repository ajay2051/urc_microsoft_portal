from __future__ import annotations

import re

from django_bolt import Depends, Request

from accounts.models import StaffUser
from core.auth import current_staff
from core.db import run_atomic_unit
from core.errors import UnprocessableEntity
from core.http import client_ip, request_id
from core.router import protected_router
from onboarding.schemas import OnboardingRunOut, OnboardingStartRequest
from onboarding.services import start_onboarding

router = protected_router("/onboarding", "onboarding")

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _idempotency_key(request: Request) -> str:
    key = request.headers.get("idempotency-key", "")
    if key and not _IDEMPOTENCY_KEY.match(key):
        raise UnprocessableEntity("Idempotency-Key must be 8-64 characters of A-Z, a-z, 0-9, '_' or '-'.")
    return key


@router.post("/start", summary="Trigger the onboarding workflow for a client", status_code=202)
async def start(
    request: Request,
    body: OnboardingStartRequest,
    staff: StaffUser = Depends(current_staff),
) -> OnboardingRunOut:
    key = _idempotency_key(request)
    rid, ip = request_id(request), client_ip(request)
    run, replayed = await run_atomic_unit(
        lambda: start_onboarding(client_id=body.client_id, idempotency_key=key, actor=staff, request_id=rid, ip_address=ip)
    )
    return OnboardingRunOut(
        id=run.id,
        client_id=run.client_id,
        status=run.status,
        steps=run.steps,
        created_at=run.created_at,
        completed_at=run.completed_at,
        replayed=replayed,
    )
