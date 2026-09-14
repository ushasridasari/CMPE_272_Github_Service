"""Dependency providers.

Route handlers ask for these; tests override them.  Keeping the wiring in one
place is what lets the whole HTTP surface be exercised without a network.
"""

from __future__ import annotations

from fastapi import Request

from app.config import Settings
from app.github.client import GitHubClient
from app.webhooks.store import EventStore


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_github_client(request: Request) -> GitHubClient:
    return request.app.state.github


def get_event_store(request: Request) -> EventStore:
    return request.app.state.store
