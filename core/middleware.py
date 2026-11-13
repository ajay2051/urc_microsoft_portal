"""Response hardening applied to every API response."""

from __future__ import annotations

from django_bolt import Request, Response

from core.http import request_id

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
    "Cross-Origin-Resource-Policy": "same-site",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}


class SecurityHeadersMiddleware:
    """Adds security headers and echoes/mints a correlation id (X-Request-ID)."""

    def __init__(self, get_response):
        self.get_response = get_response

    async def __call__(self, request: Request) -> Response:
        response = await self.get_response(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        response.headers["X-Request-ID"] = request_id(request)
        return response
