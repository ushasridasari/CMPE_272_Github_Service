"""GitHub HTTP failure -> AppError translation."""

from __future__ import annotations

import httpx
import pytest

from app.errors import (
    BadRequestError,
    ForbiddenError,
    NotFoundError,
    RateLimitedError,
    UnauthorizedError,
    UpstreamError,
    UpstreamUnavailableError,
)
from app.github.errors import is_rate_limited, map_github_error, retry_after_seconds


def response(status: int, *, json=None, headers=None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=json if json is not None else {},
        headers=headers or {},
        request=httpx.Request("GET", "https://api.github.com/repos/o/r/issues"),
    )


@pytest.mark.parametrize(
    ("status", "expected", "expected_status"),
    [
        (401, UnauthorizedError, 401),
        (403, ForbiddenError, 403),
        (404, NotFoundError, 404),
        (422, BadRequestError, 400),
        (410, BadRequestError, 400),
        (400, BadRequestError, 400),
        (502, UpstreamUnavailableError, 503),
        (503, UpstreamUnavailableError, 503),
        (504, UpstreamUnavailableError, 503),
        (418, UpstreamError, 502),
        (500, UpstreamError, 502),
    ],
)
def test_status_mapping(status, expected, expected_status):
    error = map_github_error(response(status))
    assert isinstance(error, expected)
    assert error.status_code == expected_status


def test_client_faults_are_4xx_and_upstream_faults_are_5xx():
    """The distinction the caller actually needs."""
    assert map_github_error(response(422)).status_code < 500
    assert map_github_error(response(500)).status_code >= 500


def test_404_message_names_the_resource():
    error = map_github_error(response(404), resource="issue")
    assert "issue" in error.message
    assert error.code == "not_found"


def test_422_field_errors_flattened_into_details():
    payload = {
        "message": "Validation Failed",
        "errors": [
            {"resource": "Issue", "field": "title", "code": "missing_field"},
            {"resource": "Issue", "field": "labels", "code": "invalid",
             "message": "labels must be strings"},
        ],
    }
    error = map_github_error(response(422, json=payload))
    fields = [d["field"] for d in error.details]
    assert fields == ["title", "labels"]
    assert any(d["message"] == "labels must be strings" for d in error.details)


def test_403_with_exhausted_budget_becomes_429(monkeypatch):
    resp = response(
        403,
        json={"message": "API rate limit exceeded for user."},
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "2000000743"},
    )
    monkeypatch.setattr("app.github.errors.time.time", lambda: 2000000000.0)
    error = map_github_error(resp)
    assert isinstance(error, RateLimitedError)
    assert error.status_code == 429
    assert error.headers["Retry-After"] == "743"


def test_403_secondary_limit_uses_retry_after_header():
    resp = response(403, json={"message": "You have exceeded a secondary rate limit"},
                    headers={"retry-after": "60"})
    error = map_github_error(resp)
    assert isinstance(error, RateLimitedError)
    assert error.headers["Retry-After"] == "60"


def test_403_permission_problem_is_not_a_rate_limit():
    """The discrimination that matters: same status, different cause."""
    resp = response(403, json={"message": "Resource not accessible by personal access token"},
                    headers={"x-ratelimit-remaining": "4987"})
    error = map_github_error(resp)
    assert isinstance(error, ForbiddenError)
    assert "Issues: Read and write" in error.message


def test_explicit_429_is_rate_limited():
    assert is_rate_limited(response(429, headers={"retry-after": "5"})) is True


def test_retry_after_prefers_explicit_header_over_reset():
    resp = response(403, headers={"retry-after": "12", "x-ratelimit-reset": "2000009999"})
    assert retry_after_seconds(resp, now=2000000000) == 12


def test_retry_after_never_negative_for_stale_reset():
    resp = response(403, headers={"x-ratelimit-reset": "1000"})
    assert retry_after_seconds(resp, now=2000000000) == 0


def test_retry_after_none_when_no_headers():
    assert retry_after_seconds(response(403)) is None


def test_malformed_rate_headers_do_not_raise():
    resp = response(403, headers={"retry-after": "soon", "x-ratelimit-reset": "never"})
    assert retry_after_seconds(resp) is None


def test_github_body_is_not_forwarded_verbatim():
    """No upstream URL or internal wording should reach our client."""
    payload = {
        "message": "Not Found",
        "documentation_url": "https://docs.github.com/rest/issues/issues#get-an-issue",
    }
    error = map_github_error(response(404, json=payload), resource="issue")
    assert "documentation_url" not in str(error.to_payload())
    assert "docs.github.com" not in error.message


def test_non_json_error_body_is_handled():
    resp = httpx.Response(
        status_code=500,
        text="<html>502 Bad Gateway</html>",
        request=httpx.Request("GET", "https://api.github.com/x"),
    )
    assert map_github_error(resp).status_code == 502


def test_error_payload_shape():
    error = map_github_error(response(404), resource="issue")
    payload = error.to_payload()
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message"}
