"""Shared test fixtures.

Environment variables are set here, before anything imports ``app.*``, so the
settings object is constructible without a real ``.env`` file.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

TEST_SECRET = "test-webhook-secret-do-not-use-in-production"

os.environ.setdefault("GITHUB_TOKEN", "ghp_testtoken000000000000000000000000")
os.environ.setdefault("GITHUB_OWNER", "test-owner")
os.environ.setdefault("GITHUB_REPO", "test-repo")
os.environ.setdefault("WEBHOOK_SECRET", TEST_SECRET)
os.environ.setdefault("PORT", "8080")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.github.cache import ETagCache  # noqa: E402
from app.github.client import GitHubClient  # noqa: E402
from app.main import create_app  # noqa: E402
from app.webhooks.store import EventStore  # noqa: E402
from app.webhooks.verify import compute_signature  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
API = "https://api.github.com"
REPO_URL = f"{API}/repos/test-owner/test-repo"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        github_token="ghp_testtoken000000000000000000000000",
        github_owner="test-owner",
        github_repo="test-repo",
        webhook_secret=TEST_SECRET,
        port=8080,
        events_db_path=str(tmp_path / "events.db"),
        log_level="CRITICAL",
    )


@pytest.fixture
def store(settings: Settings) -> Iterator[EventStore]:
    s = EventStore(settings.events_db_path)
    yield s
    s.close()


@pytest.fixture
def client(settings: Settings, store: EventStore) -> Iterator[TestClient]:
    """A TestClient whose GitHub calls are interceptable by respx.

    The app's own lifespan is bypassed: we install the client and store
    directly so each test gets an isolated database and a fresh cache.
    """
    app = create_app(settings)
    http_client = httpx.AsyncClient(base_url=settings.github_api_base, timeout=5.0)
    app.state.github = GitHubClient(settings, client=http_client, cache=ETagCache())
    app.state.store = store

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def signed():
    """Helper producing (body_bytes, headers) for a webhook request."""

    def _sign(
        payload: dict[str, Any] | bytes,
        *,
        event: str = "issues",
        delivery: str = "11111111-2222-3333-4444-555555555555",
        secret: str = TEST_SECRET,
        tamper: bool = False,
    ) -> tuple[bytes, dict[str, str]]:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        signature = compute_signature(secret, body)
        if tamper:
            body = body + b" "
        return body, {
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        }

    return _sign


@pytest.fixture(autouse=True)
def _no_lifespan_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep tests from ever reaching the real network by accident."""
    monkeypatch.setenv("HTTP_PROXY", "")
    monkeypatch.setenv("HTTPS_PROXY", "")
