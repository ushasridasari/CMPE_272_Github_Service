"""Done by Jahnavi"""
"""Issue and comment routes — the public CRUD surface of the gateway.

Note on "delete": GitHub's REST API has no delete-issue operation, so the D of
CRUD is expressed as ``PATCH {"state": "closed"}``.  A ``DELETE`` route is
provided as a convenience alias that closes the issue and says so in its
response, rather than pretending to destroy data.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status

from app.config import Settings
from app.dependencies import get_github_client, get_settings_dep
from app.github.client import GitHubClient
from app.github.pagination import rewrite_link_header
from app.models import (
    Comment,
    CommentCreate,
    Error,
    Issue,
    IssueCreate,
    IssueStateFilter,
    IssueUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["issues"])

IssueNumber = Annotated[int, Path(ge=1, description="GitHub issue number", examples=[42])]

COMMON_ERRORS: dict[int | str, dict] = {
    400: {"model": Error, "description": "Invalid request payload or query parameter"},
    401: {"model": Error, "description": "GitHub rejected the server's credentials"},
    403: {"model": Error, "description": "Token lacks the required repository permission"},
    404: {"model": Error, "description": "Issue not found"},
    429: {"model": Error, "description": "GitHub rate limit exceeded"},
    502: {"model": Error, "description": "Unexpected upstream response"},
    503: {"model": Error, "description": "GitHub unavailable"},
}


#: GitHub's lowercase header -> the canonical casing we re-emit.
_RATE_HEADER_NAMES = {
    "x-ratelimit-limit": "X-RateLimit-Limit",
    "x-ratelimit-remaining": "X-RateLimit-Remaining",
    "x-ratelimit-reset": "X-RateLimit-Reset",
}


def _apply_rate_headers(response: Response, rate: dict[str, str]) -> None:
    """Surface GitHub's remaining budget so callers can pace themselves."""
    for key, value in rate.items():
        name = _RATE_HEADER_NAMES.get(key.lower())
        if name:
            response.headers[name] = value


@router.post(
    "/issues",
    status_code=status.HTTP_201_CREATED,
    response_model=Issue,
    summary="Create an issue",
    responses={201: {"description": "Issue created", "headers": {
        "Location": {"description": "Path of the created issue", "schema": {"type": "string"}}}},
        **COMMON_ERRORS},
)
async def create_issue(
    payload: IssueCreate,
    response: Response,
    client: Annotated[GitHubClient, Depends(get_github_client)],
) -> Issue:
    raw = await client.create_issue(
        title=payload.title, body=payload.body, labels=payload.labels
    )
    issue = Issue.from_github(raw)
    response.headers["Location"] = f"/issues/{issue.number}"
    logger.info("issue_created", extra={"issue_number": issue.number})
    return issue


@router.get(
    "/issues",
    response_model=list[Issue],
    summary="List issues",
    responses={200: {"description": "A page of issues", "headers": {
        "Link": {"description": "RFC 8288 pagination links", "schema": {"type": "string"}},
        "X-Total-Count": {"description": "Items on this page", "schema": {"type": "integer"}}}},
        **COMMON_ERRORS},
)
async def list_issues(
    request: Request,
    response: Response,
    client: Annotated[GitHubClient, Depends(get_github_client)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
    state: IssueStateFilter = Query("open", description="Filter by issue state"),
    labels: str | None = Query(
        None, description="Comma-separated label names", examples=["bug,ui"]
    ),
    page: int = Query(1, ge=1, description="1-based page number"),
    per_page: int = Query(30, ge=1, le=100, description="Items per page (max 100)"),
) -> list[Issue]:
    result = await client.list_issues(
        state=state, labels=labels, page=page, per_page=per_page
    )

    keep = {"state": state, "per_page": str(per_page)}
    if labels:
        keep["labels"] = labels
    public_base = str(request.base_url).rstrip("/")
    link = rewrite_link_header(
        result.link_header, public_base=public_base, public_path="/issues", keep_params=keep
    )
    if link:
        response.headers["Link"] = link
    response.headers["X-Total-Count"] = str(len(result.items))
    response.headers["X-Page"] = str(page)
    response.headers["X-Per-Page"] = str(per_page)
    if result.etag:
        response.headers["ETag"] = result.etag
    response.headers["X-Cache"] = "HIT" if result.from_cache else "MISS"
    _apply_rate_headers(response, result.rate_limit)

    return [Issue.from_github(item) for item in result.items]


@router.get(
    "/issues/{number}",
    response_model=Issue,
    summary="Get a single issue",
    responses=COMMON_ERRORS,
)
async def get_issue(
    number: IssueNumber,
    client: Annotated[GitHubClient, Depends(get_github_client)],
) -> Issue:
    return Issue.from_github(await client.get_issue(number))


@router.patch(
    "/issues/{number}",
    response_model=Issue,
    summary="Update an issue (rename, edit body, close or reopen)",
    responses=COMMON_ERRORS,
)
async def update_issue(
    number: IssueNumber,
    payload: IssueUpdate,
    client: Annotated[GitHubClient, Depends(get_github_client)],
) -> Issue:
    changes = payload.model_dump(exclude_none=True)
    raw = await client.update_issue(number, changes)
    logger.info(
        "issue_updated",
        extra={"issue_number": number, "changed_fields": sorted(changes)},
    )
    return Issue.from_github(raw)


@router.delete(
    "/issues/{number}",
    response_model=Issue,
    summary="Close an issue (GitHub has no delete; D of CRUD is a close)",
    responses=COMMON_ERRORS,
)
async def close_issue(
    number: IssueNumber,
    response: Response,
    client: Annotated[GitHubClient, Depends(get_github_client)],
) -> Issue:
    raw = await client.update_issue(number, {"state": "closed"})
    response.headers["X-Delete-Semantics"] = "closed-not-deleted"
    logger.info("issue_closed", extra={"issue_number": number})
    return Issue.from_github(raw)


@router.post(
    "/issues/{number}/comments",
    status_code=status.HTTP_201_CREATED,
    response_model=Comment,
    summary="Add a comment to an issue",
    responses={201: {"description": "Comment created", "headers": {
        "Location": {"description": "URL of the created comment", "schema": {"type": "string"}}}},
        **COMMON_ERRORS},
)
async def create_comment(
    number: IssueNumber,
    payload: CommentCreate,
    response: Response,
    client: Annotated[GitHubClient, Depends(get_github_client)],
) -> Comment:
    comment = Comment.from_github(await client.create_comment(number, payload.body))
    response.headers["Location"] = f"/issues/{number}/comments"
    logger.info("comment_created", extra={"issue_number": number, "comment_id": comment.id})
    return comment


@router.get(
    "/issues/{number}/comments",
    response_model=list[Comment],
    summary="List comments on an issue",
    responses=COMMON_ERRORS,
)
async def list_comments(
    number: IssueNumber,
    request: Request,
    response: Response,
    client: Annotated[GitHubClient, Depends(get_github_client)],
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
) -> list[Comment]:
    result = await client.list_comments(number, page=page, per_page=per_page)
    link = rewrite_link_header(
        result.link_header,
        public_base=str(request.base_url).rstrip("/"),
        public_path=f"/issues/{number}/comments",
        keep_params={"per_page": str(per_page)},
    )
    if link:
        response.headers["Link"] = link
    response.headers["X-Total-Count"] = str(len(result.items))
    _apply_rate_headers(response, result.rate_limit)
    return [Comment.from_github(item) for item in result.items]
