"""Validation of Microsoft Entra ID (Azure AD) v2.0 access tokens.

Why this is implemented here instead of using django-bolt's built-in JWT backend:
Entra v2 tokens always carry ``"ver": "2.0"`` (a *string*), which django-bolt's
native validator interprets as its own integer token-version claim and rejects.
Using it would lock out every real Entra user while passing tests that use
hand-made tokens. So we verify tokens with PyJWT and pin everything that matters.

What is verified (all mandatory):
* signature: RS256 only (``alg`` is pinned; ``none``/HS256 downgrade is impossible)
* key: looked up by ``kid`` in the tenant's JWKS (cached, rotated, rate-limited)
* ``iss``: exactly our tenant's v2.0 issuer (blocks tokens from other tenants)
* ``aud``: exactly this API's client id (blocks tokens minted for other apps)
* ``exp`` / ``nbf`` with a small clock-skew leeway, ``iat`` present
* ``tid`` equals our tenant, ``ver`` is ``2.0``
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm

logger = logging.getLogger(__name__)

MAX_TOKEN_BYTES = 16 * 1024
MAX_JWKS_BYTES = 256 * 1024
ALLOWED_ALGORITHMS = ["RS256"]


class TokenInvalid(Exception):
    """The presented token must be rejected (maps to HTTP 401)."""


class KeysUnavailable(Exception):
    """Signing keys cannot be obtained right now (maps to HTTP 503)."""


@dataclass
class JWKSCache:
    """Thread-safe JWKS cache with TTL and a cooldown that bounds refresh traffic.

    A request carrying an unknown ``kid`` can trigger at most one refresh per
    ``min_refresh_interval`` seconds, so garbage tokens cannot be used to make us
    hammer login.microsoftonline.com (or to burn CPU).
    """

    url: str | None = None
    static_jwks: str | None = None
    ttl_seconds: int = 3600
    min_refresh_interval: int = 30
    timeout_seconds: float = 5.0
    _keys: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _fetched_at: float = field(default=0.0, init=False, repr=False)
    _next_refresh_allowed: float = field(default=0.0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if bool(self.url) == bool(self.static_jwks):
            raise ValueError("Provide exactly one of url or static_jwks.")
        if self.url and not self.url.startswith("https://"):
            raise ValueError("JWKS url must use https.")
        if self.static_jwks:
            self._load(json.loads(self.static_jwks))
            self._fetched_at = float("inf")  # static keys never expire

    def _load(self, document: dict[str, Any]) -> None:
        keys: dict[str, Any] = {}
        for jwk in document.get("keys", []):
            if jwk.get("kty") != "RSA" or not jwk.get("kid"):
                continue
            if jwk.get("use", "sig") != "sig":
                continue
            keys[jwk["kid"]] = RSAAlgorithm.from_jwk(json.dumps(jwk))
        if not keys:
            raise KeysUnavailable("JWKS contained no usable RSA signing keys.")
        self._keys = keys

    def _fresh(self, now: float) -> bool:
        return now - self._fetched_at < self.ttl_seconds

    async def get_key(self, kid: str) -> Any:
        now = time.monotonic()
        with self._lock:
            key = self._keys.get(kid)
            if key is not None and (self.static_jwks or self._fresh(now)):
                return key
            may_refresh = not self.static_jwks and now >= self._next_refresh_allowed
            if may_refresh:
                self._next_refresh_allowed = now + self.min_refresh_interval
        if may_refresh:
            await self._refresh()
        with self._lock:
            key = self._keys.get(kid)
        if key is None:
            if not self._keys:
                raise KeysUnavailable("Signing keys are not available.")
            raise TokenInvalid("unknown signing key")
        return key

    async def _refresh(self) -> None:
        assert self.url
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False) as client:
                response = await client.get(self.url, headers={"Accept": "application/json"})
            response.raise_for_status()
            if len(response.content) > MAX_JWKS_BYTES:
                raise ValueError("JWKS response too large")
            document = response.json()
            with self._lock:
                self._load(document)
                self._fetched_at = time.monotonic()
        except Exception:
            logger.exception("Failed to refresh Entra signing keys")


class EntraTokenVerifier:
    def __init__(self, *, issuer: str, audience: str, tenant_id: str, jwks: JWKSCache, leeway: int = 30) -> None:
        self.issuer = issuer
        self.audience = audience
        self.tenant_id = tenant_id
        self.jwks = jwks
        self.leeway = leeway

    async def verify(self, token: str) -> dict[str, Any]:
        if not token or len(token) > MAX_TOKEN_BYTES:
            raise TokenInvalid("malformed token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise TokenInvalid("malformed token") from exc
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise TokenInvalid("unsupported algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise TokenInvalid("missing key id")

        key = await self.jwks.get_key(kid)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=ALLOWED_ALGORITHMS,
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "nbf", "iat", "iss", "aud"], "verify_signature": True},
            )
        except jwt.PyJWTError as exc:
            raise TokenInvalid(type(exc).__name__) from exc

        if claims.get("tid") != self.tenant_id:
            raise TokenInvalid("wrong tenant")
        if claims.get("ver") != "2.0":
            raise TokenInvalid("unsupported token version")
        return claims
