"""Thin async client for the GitHub Issues REST API.

Everything that knows about api.github.com lives here.  Route handlers deal
only in our own models and our own errors, which is what makes them easy to
test without a network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings
from app.errors import UpstreamUnavailableError
from app.github.cache import ETagCache
from app.github.errors import map_github_error

logger = logging.getLogger(__name__)


@dataclass
class ListResult:
    """A paginated list plus the metadata needed to forward pagination."""

    items: list[dict[str, Any]]
    link_header: str | None = None
    etag: str | None = None
    from_cache: bool = False
    rate_limit: dict[str, str] = field(default_factory=dict)


class GitHubClient:
    """Wraps the subset of the GitHub API this gateway exposes."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        cache: ETagCache | None = None,
    ) -> None:
        self._settings = settings
        self._cache = cache if cache is not None else ETagCache()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.github_api_base,
            timeout=settings.request_timeout_seconds,
        )

    # -- lifecycle ---------------------------------------------------------
    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @property
    def cache(self) -> ETagCache:
        return self._cache

    # -- plumbing ----------------------------------------------------------
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self._settings.github_api_version,
            "User-Agent": "cmpe272-issues-gw",
        }
        if extra:
            headers.update(extra)
        return headers

    def _repo_path(self, suffix: str = "") -> str:
        return f"/repos/{self._settings.repo_path}{suffix}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        resource: str = "resource",
        allow_304: bool = False,
    ) -> httpx.Response:
        """Issue one GitHub call, translating transport and HTTP failures."""
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json,
                headers=self._headers(headers),
            )
        except httpx.TimeoutException as exc:
            raise UpstreamUnavailableError(
                "Timed out while contacting GitHub.", headers={"Retry-After": "10"}
            ) from exc
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError("Could not reach GitHub.") from exc

        logger.debug(
            "github_call",
            extra={
                "github_method": method,
                "github_path": path,
                "github_status": response.status_code,
                "rate_remaining": response.headers.get("x-ratelimit-remaining"),
            },
        )

        if response.status_code == 304 and allow_304:
            return response
        if response.status_code >= 400:
            raise map_github_error(response, resource=resource)
        return response

    @staticmethod
    def _rate_headers(response: httpx.Response) -> dict[str, str]:
        keys = ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")
        return {k: response.headers[k] for k in keys if k in response.headers}

    # -- issues ------------------------------------------------------------
    async def create_issue(
        self, *, title: str, body: str | None = None, labels: list[str] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"title": title}
        if body is not None:
            payload["body"] = body
        if labels:
            payload["labels"] = labels
        response = await self._request(
            "POST", self._repo_path("/issues"), json=payload, resource="repository"
        )
        # A create invalidates any cached list.
        self._cache.clear()
        return response.json()

    async def list_issues(
        self,
        *,
        state: str = "open",
        labels: str | None = None,
        page: int = 1,
        per_page: int = 30,
        use_cache: bool = True,
    ) -> ListResult:
        """List issues, optionally using a stored ETag to avoid rate usage."""
        params: dict[str, Any] = {"state": state, "page": page, "per_page": per_page}
        if labels:
            params["labels"] = labels

        path = self._repo_path("/issues")
        key = ETagCache.build_key(path, params)
        cached = self._cache.get(key) if (use_cache and self._settings.enable_etag_cache) else None
        extra_headers = {"If-None-Match": cached.etag} if cached else None

        response = await self._request(
            "GET",
            path,
            params=params,
            headers=extra_headers,
            resource="repository",
            allow_304=True,
        )

        if response.status_code == 304 and cached is not None:
            logger.info("etag_hit", extra={"cache_key": key})
            return ListResult(
                items=cached.payload,
                link_header=cached.link_header,
                etag=cached.etag,
                from_cache=True,
                rate_limit=self._rate_headers(response),
            )

        # GitHub's issues endpoint also returns pull requests; drop them.
        items = [item for item in response.json() if "pull_request" not in item]
        etag = response.headers.get("etag")
        link = response.headers.get("link")
        if etag and self._settings.enable_etag_cache:
            self._cache.set(key, etag, items, link)

        return ListResult(
            items=items,
            link_header=link,
            etag=etag,
            from_cache=False,
            rate_limit=self._rate_headers(response),
        )

    async def get_issue(self, number: int) -> dict[str, Any]:
        response = await self._request(
            "GET", self._repo_path(f"/issues/{number}"), resource="issue"
        )
        data = response.json()
        if "pull_request" in data:
            # Pull requests share the issue number space but are not issues.
            from app.errors import NotFoundError

            raise NotFoundError(f"#{number} is a pull request, not an issue.")
        return data

    async def update_issue(self, number: int, changes: dict[str, Any]) -> dict[str, Any]:
        response = await self._request(
            "PATCH", self._repo_path(f"/issues/{number}"), json=changes, resource="issue"
        )
        self._cache.clear()
        return response.json()

    # -- comments ----------------------------------------------------------
    async def create_comment(self, number: int, body: str) -> dict[str, Any]:
        response = await self._request(
            "POST",
            self._repo_path(f"/issues/{number}/comments"),
            json={"body": body},
            resource="issue",
        )
        return response.json()

    async def list_comments(self, number: int, *, page: int = 1, per_page: int = 30) -> ListResult:
        response = await self._request(
            "GET",
            self._repo_path(f"/issues/{number}/comments"),
            params={"page": page, "per_page": per_page},
            resource="issue",
        )
        return ListResult(
            items=response.json(),
            link_header=response.headers.get("link"),
            rate_limit=self._rate_headers(response),
        )
