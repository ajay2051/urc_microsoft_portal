"""Microsoft Graph access (app-only, per client tenant).

Security & reliability properties
---------------------------------
* The tenant id is validated as a GUID before it is placed in any URL.
* ``@odata.nextLink`` is followed **only** if it points at graph.microsoft.com
  over HTTPS; otherwise a hostile/buggy link could leak our bearer token (SSRF).
* Every call has a timeout, bounded retries with exponential backoff + jitter,
  honours ``Retry-After`` on 429/503/504 (capped), and pagination is bounded.
* Tokens are cached per tenant until shortly before expiry; a 401 forces one refresh.
* Errors are normalised into :class:`GraphError` with stable ``code`` values so the
  API layer never leaks upstream payloads (which can contain tenant details).
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from django.conf import settings

from core.validators import normalize_guid

logger = logging.getLogger(__name__)

USER_SELECT = "id,userPrincipalName,displayName,mail,jobTitle,department,accountEnabled"
MAX_RETRY_AFTER_SECONDS = 30.0
TOKEN_EXPIRY_SKEW_SECONDS = 120


@dataclass(frozen=True, slots=True)
class GraphUser:
    id: str
    user_principal_name: str
    display_name: str
    mail: str
    job_title: str
    department: str
    account_enabled: bool


class GraphError(Exception):
    """Normalised Graph/Entra failure."""

    def __init__(self, code: str, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


class GraphClient(Protocol):
    async def list_users(self, tenant_id: str) -> list[GraphUser]: ...

    async def probe(self, tenant_id: str) -> None:
        """Raise :class:`GraphError` unless the app can read the tenant's directory."""


def _clean(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def parse_user(raw: dict[str, Any]) -> GraphUser | None:
    """Convert one Graph user object, or ``None`` when it is unusable (never raises)."""
    try:
        user_id = normalize_guid(raw.get("id", ""))
    except ValueError:
        return None
    upn = _clean(raw.get("userPrincipalName"), 320)
    if not upn:
        return None
    return GraphUser(
        id=user_id,
        user_principal_name=upn.lower(),
        display_name=_clean(raw.get("displayName"), 256),
        mail=_clean(raw.get("mail"), 320).lower(),
        job_title=_clean(raw.get("jobTitle"), 256),
        department=_clean(raw.get("department"), 256),
        account_enabled=bool(raw.get("accountEnabled", True)),
    )


def _is_graph_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == "graph.microsoft.com" and parts.port in (None, 443)


class LiveGraphClient:
    """Client-credentials flow against Entra, then ``GET /users`` with paging."""

    def __init__(self) -> None:
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    # -- tokens ------------------------------------------------------------
    async def _token(self, tenant_id: str, *, force: bool = False) -> str:
        with self._lock:
            cached = self._tokens.get(tenant_id)
            if cached and not force and cached[1] - TOKEN_EXPIRY_SKEW_SECONDS > time.monotonic():
                return cached[0]
        url = f"{settings.GRAPH_AUTHORITY_HOST}/{tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": settings.GRAPH_CLIENT_ID,
            "client_secret": settings.GRAPH_CLIENT_SECRET,
            "scope": "https://graph.microsoft.com/.default",
        }
        try:
            async with httpx.AsyncClient(timeout=settings.GRAPH_TIMEOUT_SECONDS) as http:
                response = await http.post(url, data=data)
        except httpx.HTTPError as exc:
            raise GraphError("token_unreachable", "Could not reach the Microsoft identity platform.") from exc
        if response.status_code != 200:
            raise self._token_error(response)
        body = response.json()
        token, ttl = body.get("access_token"), int(body.get("expires_in", 3600))
        if not token:
            raise GraphError("token_invalid", "Microsoft identity platform returned no access token.")
        with self._lock:
            self._tokens[tenant_id] = (token, time.monotonic() + ttl)
        return token

    @staticmethod
    def _token_error(response: httpx.Response) -> GraphError:
        try:
            description = str(response.json().get("error_description", ""))
        except ValueError:
            description = ""
        # AADSTS65001: no consent; AADSTS700016: app not found in tenant (not installed).
        if "AADSTS65001" in description or "AADSTS700016" in description or "AADSTS7000229" in description:
            return GraphError("consent_missing", "Admin consent has not been granted in the client tenant.")
        if "AADSTS90002" in description or "AADSTS900023" in description:
            return GraphError("tenant_not_found", "The Microsoft 365 tenant id does not exist.")
        if response.status_code in (429, 500, 502, 503, 504):
            return GraphError("token_unavailable", "Microsoft identity platform is temporarily unavailable.")
        logger.warning("Token request failed with HTTP %s", response.status_code)
        return GraphError("token_rejected", "Microsoft identity platform rejected the credentials.")

    # -- HTTP with retries --------------------------------------------------
    async def _get(self, tenant_id: str, url: str, params: dict[str, str] | None) -> dict[str, Any]:
        refreshed = False
        for attempt in range(settings.GRAPH_MAX_RETRIES + 1):
            token = await self._token(tenant_id, force=refreshed)
            try:
                async with httpx.AsyncClient(timeout=settings.GRAPH_TIMEOUT_SECONDS, follow_redirects=False) as http:
                    response = await http.get(
                        url,
                        params=params,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    )
            except httpx.HTTPError as exc:
                if attempt >= settings.GRAPH_MAX_RETRIES:
                    raise GraphError("graph_unreachable", "Could not reach Microsoft Graph.") from exc
                await asyncio.sleep(self._backoff(attempt))
                continue

            status = response.status_code
            if status == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise GraphError("graph_bad_response", "Microsoft Graph returned invalid JSON.") from exc
            if status == 401 and not refreshed:
                refreshed = True  # token may have been revoked/expired early: refresh once
                continue
            if status == 403:
                raise GraphError("consent_missing", "The app lacks permission to read users in this tenant.")
            if status in (429, 503, 504):
                retry_after = self._retry_after(response)
                if attempt >= settings.GRAPH_MAX_RETRIES:
                    raise GraphError("throttled", "Microsoft Graph is throttling requests.", retry_after=retry_after)
                await asyncio.sleep(retry_after if retry_after is not None else self._backoff(attempt))
                continue
            logger.warning("Graph request failed with HTTP %s", status)
            raise GraphError("graph_error", f"Microsoft Graph returned HTTP {status}.")
        raise GraphError("graph_error", "Microsoft Graph request failed.")  # pragma: no cover

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(MAX_RETRY_AFTER_SECONDS, (2**attempt) * 0.5) * (0.5 + random.random() / 2)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        try:
            return min(MAX_RETRY_AFTER_SECONDS, max(0.0, float(raw))) if raw is not None else None
        except ValueError:
            return None

    # -- public API ---------------------------------------------------------
    async def list_users(self, tenant_id: str) -> list[GraphUser]:
        tenant_id = normalize_guid(tenant_id)
        url: str | None = f"{settings.GRAPH_BASE_URL}/users"
        params: dict[str, str] | None = {"$select": USER_SELECT, "$top": str(settings.GRAPH_PAGE_SIZE)}
        users: dict[str, GraphUser] = {}
        skipped = 0
        for _page in range(settings.GRAPH_MAX_PAGES):
            payload = await self._get(tenant_id, url, params)
            for raw in payload.get("value", []):
                parsed = parse_user(raw) if isinstance(raw, dict) else None
                if parsed is None:
                    skipped += 1
                else:
                    users[parsed.id] = parsed
            next_link = payload.get("@odata.nextLink")
            if not next_link:
                if skipped:
                    logger.warning("Skipped %s unusable Graph user records", skipped)
                return list(users.values())
            if not isinstance(next_link, str) or not _is_graph_url(next_link):
                raise GraphError("graph_bad_response", "Refusing to follow a non-Graph pagination link.")
            url, params = next_link, None  # nextLink already carries the query string
        # Incomplete data must never be treated as the full directory.
        raise GraphError("graph_too_large", "Directory exceeds the configured page limit; refusing partial sync.")

    async def probe(self, tenant_id: str) -> None:
        tenant_id = normalize_guid(tenant_id)
        await self._get(tenant_id, f"{settings.GRAPH_BASE_URL}/users", {"$select": "id", "$top": "1"})


class MockGraphClient:
    """Deterministic fake directory for local development and tests (DEBUG only)."""

    #: tenant ids whose consent has "not been granted" in the mock
    no_consent: frozenset[str] = frozenset({"00000000-0000-0000-0000-000000000bad"})

    def __init__(self, users_by_tenant: dict[str, list[GraphUser]] | None = None) -> None:
        self.users_by_tenant = users_by_tenant or {}
        self.calls = 0

    def _users(self, tenant_id: str) -> list[GraphUser]:
        if tenant_id in self.users_by_tenant:
            return self.users_by_tenant[tenant_id]
        namespace = uuid.UUID(tenant_id)
        return [
            GraphUser(
                id=str(uuid.uuid5(namespace, f"user-{i}")),
                user_principal_name=f"user{i}@mock-{tenant_id[:8]}.onmicrosoft.com",
                display_name=f"Mock User {i}",
                mail=f"user{i}@mock-{tenant_id[:8]}.onmicrosoft.com",
                job_title="Engineer" if i % 2 else "Analyst",
                department="IT" if i % 3 else "Finance",
                account_enabled=i != 5,
            )
            for i in range(1, 9)
        ]

    async def list_users(self, tenant_id: str) -> list[GraphUser]:
        self.calls += 1
        tenant_id = normalize_guid(tenant_id)
        if tenant_id in self.no_consent:
            raise GraphError("consent_missing", "Admin consent has not been granted in the client tenant.")
        return list(self._users(tenant_id))

    async def probe(self, tenant_id: str) -> None:
        await self.list_users(tenant_id)


_override: GraphClient | None = None


@lru_cache(maxsize=1)
def _default_client() -> GraphClient:
    return MockGraphClient() if settings.GRAPH_BACKEND == "mock" else LiveGraphClient()


def get_graph_client() -> GraphClient:
    return _override or _default_client()


def set_graph_client(client: GraphClient | None) -> None:
    """Test hook: replace (or with ``None`` restore) the process-wide Graph client."""
    global _override
    _override = client


