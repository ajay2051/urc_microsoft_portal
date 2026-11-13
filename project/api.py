"""Root django-bolt application: composes the routers of every app."""

from __future__ import annotations

from django.conf import settings
from django_bolt import BoltAPI, OpenAPIConfig

from core.auth import EntraAuthMiddleware
from core.middleware import SecurityHeadersMiddleware

api = BoltAPI(
    prefix="/api",
    middleware=[SecurityHeadersMiddleware, EntraAuthMiddleware],
    # The interactive docs expose the full API surface: development only.
    openapi_config=OpenAPIConfig(title="Client Operations Portal API", version="1.0.0", path="/docs")
    if settings.DEBUG
    else None,
)

from audit.api import router as audit_router
from clients.api import router as clients_router
from core.api import me_router, public_router
from integrations.api import graph_router, integrations_router, sync_router
from onboarding.api import router as onboarding_router

api.include_router(public_router)
api.include_router(me_router)
api.include_router(clients_router)
api.include_router(integrations_router)
api.include_router(sync_router)
api.include_router(graph_router)
api.include_router(onboarding_router)
api.include_router(audit_router)
