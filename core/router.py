"""Router factory. Authentication itself is enforced globally by ``EntraAuthMiddleware``."""

from __future__ import annotations

from django_bolt import Router


def protected_router(prefix: str, tag: str) -> Router:
    """Router for authenticated endpoints (auth is applied by middleware, deny-by-default)."""
    return Router(prefix=prefix, tags=[tag])
