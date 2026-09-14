"""The last-resort handlers, and pagination on the comments route."""

from __future__ import annotations

import httpx
import respx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import REPO_URL, load_fixture

COMMENT = load_fixture("github_comment.json")


def _app_with_failing_routes(settings, store):
    app = create_app(settings)
    app.state.store = store

    @app.get("/boom")
    async def boom():  # pragma: no cover - invoked through the client
        raise RuntimeError("something unexpected happened internally")

    @app.get("/teapot")
    async def teapot():  # pragma: no cover - invoked through the client
        raise HTTPException(status_code=418, detail="I refuse to brew coffee")

    return app


def test_unexpected_exception_becomes_500_without_leaking_internals(settings, store):
    app = _app_with_failing_routes(settings, store)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    # The stack trace and the original message stay in the logs, not the body.
    assert "something unexpected happened internally" not in response.text
    assert "RuntimeError" not in response.text
    assert response.headers["X-Request-ID"]


def test_non_404_http_exception_uses_the_error_envelope(settings, store):
    app = _app_with_failing_routes(settings, store)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/teapot")

    assert response.status_code == 418
    assert response.json()["error"]["code"] == "http_error"
    assert "brew coffee" in response.json()["error"]["message"]


def test_method_not_allowed_uses_the_error_envelope(client):
    response = client.put("/healthz")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "http_error"


@respx.mock
def test_comments_link_header_is_rewritten(client):
    link = '<https://api.github.com/repositories/1/issues/42/comments?page=2>; rel="next"'
    respx.get(f"{REPO_URL}/issues/42/comments").mock(
        return_value=httpx.Response(200, json=[COMMENT], headers={"Link": link})
    )

    response = client.get("/issues/42/comments?per_page=1")
    forwarded = response.headers["Link"]
    assert "api.github.com" not in forwarded
    assert "/issues/42/comments?" in forwarded
    assert "page=2" in forwarded
