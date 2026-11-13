"""Domain errors mapped onto HTTP responses.

Every error body has the same shape so clients can handle failures uniformly::

    {"detail": "<human readable message>", "extra": {"code": "<machine_code>", ...}}
"""

from __future__ import annotations

from typing import Any

from django_bolt.exceptions import HTTPException


class ApiError(HTTPException):
    status_code = 400
    code = "bad_request"

    def __init__(self, detail: str, *, code: str | None = None, extra: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"code": code or self.code}
        if extra:
            payload.update(extra)
        super().__init__(status_code=self.status_code, detail=detail, extra=payload)


class Unauthorized(ApiError):
    status_code = 401
    code = "unauthorized"

    def __init__(self, detail: str = "Authentication required.", **kwargs: Any) -> None:
        super().__init__(detail, **kwargs)
        self.headers = {"WWW-Authenticate": "Bearer"}


class Forbidden(ApiError):
    status_code = 403
    code = "forbidden"


class NotFound(ApiError):
    status_code = 404
    code = "not_found"


class Conflict(ApiError):
    status_code = 409
    code = "conflict"


class UnprocessableEntity(ApiError):
    status_code = 422
    code = "validation_error"


class UpstreamError(ApiError):
    """A dependency we call (Microsoft Graph / Entra) failed."""

    status_code = 502
    code = "upstream_error"


class UpstreamUnavailable(ApiError):
    status_code = 503
    code = "upstream_unavailable"
