"""GitHub client and error mapping - done by Sainath

Two rules drive everything here:

1. A GitHub response body is never forwarded verbatim.  It can carry
   internal URLs and wording that would leak implementation detail, so we
   extract only the message and field errors we understand.
2. A *client* mistake stays a 4xx; a *GitHub* problem becomes 502/503 so the
   caller can tell "I sent something wrong" from "the upstream is unwell".
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.errors import (
    AppError,
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    UnauthorizedError,
    UpstreamError,
    UpstreamUnavailableError,
)

#: GitHub signals both primary and secondary rate limiting with 403.
_RATE_LIMIT_REMAINING = "x-ratelimit-remaining"
_RATE_LIMIT_RESET = "x-ratelimit-reset"
_RETRY_AFTER = "retry-after"


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except Exception:  # pragma: no cover - defensive
        return {}
    return data if isinstance(data, dict) else {}


def _github_message(response: httpx.Response, fallback: str) -> str:
    return str(_safe_json(response).get("message") or fallback)


def _field_details(response: httpx.Response) -> list[dict[str, Any]]:
    """Flatten GitHub's 422 ``errors`` array into our ``details`` list."""
    details: list[dict[str, Any]] = []
    for item in _safe_json(response).get("errors", []) or []:
        if not isinstance(item, dict):
            continue
        field = item.get("field") or item.get("resource")
        code = item.get("code", "invalid")
        message = item.get("message") or f"{code} value for '{field}'"
        details.append({"field": field, "message": message})
    return details


def retry_after_seconds(response: httpx.Response, *, now: float | None = None) -> int | None:
    """Compute how long to wait before retrying, in seconds.

    Prefers an explicit ``Retry-After`` (secondary rate limits) and falls
    back to ``X-RateLimit-Reset`` (primary rate limits), which is an absolute
    UTC epoch second.
    """
    headers = response.headers
    explicit = headers.get(_RETRY_AFTER)
    if explicit:
        try:
            return max(0, int(float(explicit)))
        except ValueError:
            pass

    reset = headers.get(_RATE_LIMIT_RESET)
    if reset:
        try:
            delta = int(float(reset)) - int(now if now is not None else time.time())
            return max(0, delta)
        except ValueError:
            pass
    return None


def is_rate_limited(response: httpx.Response) -> bool:
    """True when this failure is a rate limit rather than a permission problem."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if response.headers.get(_RATE_LIMIT_REMAINING) == "0":
        return True
    if response.headers.get(_RETRY_AFTER):
        return True
    return "rate limit" in _github_message(response, "").lower()


def map_github_error(response: httpx.Response, *, resource: str = "resource") -> AppError:
    """Translate a non-2xx GitHub response into an :class:`AppError`."""
    status = response.status_code

    if is_rate_limited(response):
        wait = retry_after_seconds(response)
        headers = {"Retry-After": str(wait)} if wait is not None else {}
        suffix = f" Retry after {wait}s." if wait is not None else ""
        return RateLimitedError(
            f"GitHub API rate limit exceeded.{suffix}",
            headers=headers,
        )

    if status == 401:
        return UnauthorizedError(
            "GitHub rejected the configured credentials. Check that GITHUB_TOKEN is "
            "set, unexpired, and scoped to this repository."
        )

    if status == 403:
        return ForbiddenError(
            "GitHub denied this request. The token is valid but lacks "
            "'Issues: Read and write' permission on the configured repository."
        )

    if status == 404:
        return NotFoundError(
            f"The requested {resource} does not exist in the configured repository."
        )

    if status in (410, 422):
        return BadRequestError(
            _github_message(response, "GitHub rejected the request payload."),
            details=_field_details(response),
        )

    if status == 400:
        return BadRequestError(_github_message(response, "GitHub rejected the request."))

    if status in (502, 503, 504):
        return UpstreamUnavailableError(
            "GitHub is temporarily unavailable. Please retry shortly.",
            headers={"Retry-After": "30"},
        )

    return UpstreamError(f"Unexpected response from GitHub (HTTP {status}).")
