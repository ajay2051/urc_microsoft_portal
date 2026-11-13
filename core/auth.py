"""Authentication & authorisation for the staff API.

How identity is established (end to end)
----------------------------------------
1. The SPA signs the user in with MSAL and requests an *access token for this
   API* (scope ``api://<ENTRA_API_CLIENT_ID>/access_as_user``).
2. :class:`EntraAuthMiddleware` runs in front of **every** route except an
   explicit public allow-list (deny by default). It verifies the bearer token
   (see :mod:`apps.core.entra`) and stores the verified claims on the request.
3. :func:`current_staff` maps the verified ``tid``/``oid`` claims to a
   pre-provisioned :class:`~apps.accounts.models.StaffUser`. Unknown or
   deactivated people get **403** even though their token is valid.
4. Roles are read from the database. Nothing the client sends (headers, body,
   token claims other than the immutable identifiers) can grant privileges.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django_bolt import Depends, Request, Response
from django_bolt.middleware import BaseMiddleware

from accounts.models import StaffUser
from core.entra import EntraTokenVerifier, JWKSCache, KeysUnavailable, TokenInvalid
from core.errors import Forbidden, Unauthorized, UpstreamUnavailable
from core.validators import normalize_guid

logger = logging.getLogger(__name__)

CLAIMS_STATE_KEY = "auth_claims"
# The ONLY unauthenticated endpoints. A test asserts every other route demands a token.
PUBLIC_PATHS = ("/api/health", "/api/ready", "/docs")


@lru_cache(maxsize=1)
def get_verifier() -> EntraTokenVerifier:
    if not settings.ENTRA_TENANT_ID or not settings.ENTRA_API_CLIENT_ID:
        raise ImproperlyConfigured("ENTRA_TENANT_ID and ENTRA_API_CLIENT_ID must be set; there is no auth bypass.")
    jwks = (
        JWKSCache(static_jwks=settings.ENTRA_JWKS_JSON)
        if settings.ENTRA_JWKS_JSON
        else JWKSCache(url=settings.ENTRA_JWKS_URL)
    )
    return EntraTokenVerifier(
        issuer=settings.ENTRA_ISSUER,
        audience=settings.ENTRA_API_CLIENT_ID,
        tenant_id=settings.ENTRA_TENANT_ID,
        jwks=jwks,
    )


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthorized("Missing or malformed bearer token.", code="missing_token")
    return token.strip()


class EntraAuthMiddleware(BaseMiddleware):
    """Rejects every request that does not carry a valid Entra access token."""

    exclude_paths = list(PUBLIC_PATHS)
    exclude_methods = ["OPTIONS"]  # CORS preflights carry no credentials and no data

    async def process_request(self, request: Request) -> Response:
        token = _bearer_token(request)
        try:
            claims = await get_verifier().verify(token)
        except TokenInvalid as exc:
            logger.info("Rejected bearer token: %s", exc)
            raise Unauthorized("Invalid or expired token.", code="invalid_token") from exc
        except KeysUnavailable as exc:
            logger.error("Entra signing keys unavailable")
            raise UpstreamUnavailable("Identity provider keys unavailable.", code="idp_unavailable") from exc
        request.state[CLAIMS_STATE_KEY] = claims
        return await self.get_response(request)


async def resolve_staff(claims: dict[str, Any]) -> StaffUser | None:
    """Map verified Entra token claims to an *active* staff record, or ``None``."""
    try:
        tenant_id = normalize_guid(claims.get("tid", ""))
        object_id = normalize_guid(claims.get("oid", ""))
    except ValueError:
        return None
    if tenant_id != settings.ENTRA_TENANT_ID:
        return None

    # Only delegated (user) tokens are acceptable: they carry the required scope.
    # App-only tokens carry `roles`/`idtyp=app` and no `scp`.
    scopes = str(claims.get("scp", "")).split()
    if claims.get("idtyp") == "app" or settings.ENTRA_REQUIRED_SCOPE not in scopes:
        return None

    staff = await StaffUser.objects.filter(entra_tenant_id=tenant_id, entra_object_id=object_id).afirst()
    if staff is not None:
        return staff if staff.is_active else None

    # First sign-in: bind the immutable object id to the pre-provisioned record.
    # Binding by UPN is safe ONLY because the tenant is pinned to our own directory,
    # where UPNs are controlled by our admins. After binding, only `oid` is trusted.
    username = str(claims.get("preferred_username") or claims.get("upn") or "").strip().lower()
    if not username or "@" not in username:
        return None
    candidate = await StaffUser.objects.filter(email__iexact=username, is_active=True, entra_object_id="").afirst()
    if candidate is None:
        logger.warning("Rejected sign-in: no provisioned staff record for object id %s", object_id)
        return None
    # Conditional UPDATE keeps the bind atomic if two first requests race.
    bound = await StaffUser.objects.filter(pk=candidate.pk, entra_object_id="").aupdate(
        entra_tenant_id=tenant_id, entra_object_id=object_id
    )
    if not bound:
        return await StaffUser.objects.filter(entra_tenant_id=tenant_id, entra_object_id=object_id).afirst()
    return await StaffUser.objects.aget(pk=candidate.pk)


async def current_staff(request: Request) -> StaffUser:
    """Dependency: the authenticated, authorised, active staff member."""
    claims = request.state.get(CLAIMS_STATE_KEY)
    if not claims:  # defence in depth: middleware must have run
        raise Unauthorized()
    staff = await resolve_staff(claims)
    if staff is None:
        raise Forbidden("Your account is not authorised to use this application.", code="not_provisioned")
    return staff


async def admin_only(staff: StaffUser = Depends(current_staff)) -> StaffUser:
    """Dependency: like :func:`current_staff` but requires the admin role."""
    if not staff.is_admin:
        raise Forbidden("Administrator role required.", code="admin_required")
    return staff
