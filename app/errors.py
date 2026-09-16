"""Application error types.
Done By Sainath
Every failure the client can see is expressed as an :class:`AppError`.
Handlers registered in ``app.main`` turn these into the single error
envelope documented in ``openapi.yaml``::

    {"error": {"code": "...", "message": "...", "details": [...]}}
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every error this service returns to a client.

    Attributes
    ----------
    status_code:
        HTTP status to send.
    code:
        Stable machine-readable string, safe to branch on in client code.
    message:
        Human-readable summary.  Never contains a token or a signature.
    details:
        Optional list of field-level problems.
    headers:
        Extra response headers (used for ``Retry-After``).
    """

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        self.details = details or []
        self.headers = headers or {}

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return {"error": payload}


class BadRequestError(AppError):
    status_code = 400
    code = "invalid_request"


class UnauthorizedError(AppError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"


class UpstreamError(AppError):
    """GitHub returned something we could not interpret as a client error."""

    status_code = 502
    code = "upstream_error"


class UpstreamUnavailableError(AppError):
    status_code = 503
    code = "upstream_unavailable"
