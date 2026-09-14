"""Done by Jahnavi"""
"""Request and response models.

These mirror the ``components/schemas`` section of ``openapi.yaml`` one for
one.  The YAML is the contract; these classes are the implementation of it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

IssueState = Literal["open", "closed"]
IssueStateFilter = Literal["open", "closed", "all"]


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------
class IssueCreate(BaseModel):
    """Body of ``POST /issues``."""

    title: str = Field(..., min_length=1, max_length=256, description="Issue title")
    body: str | None = Field(None, description="Markdown body")
    labels: list[str] | None = Field(None, description="Label names to apply")

    model_config = {
        "json_schema_extra": {
            "example": {
                "title": "Login button is misaligned on mobile",
                "body": "On iOS Safari the button overlaps the footer.",
                "labels": ["bug", "ui"],
            }
        }
    }


class IssueUpdate(BaseModel):
    """Body of ``PATCH /issues/{number}``.

    At least one field must be present; an empty patch is a client error
    rather than a silent no-op.
    """

    title: str | None = Field(None, min_length=1, max_length=256)
    body: str | None = None
    state: IssueState | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> IssueUpdate:
        if self.title is None and self.body is None and self.state is None:
            raise ValueError("at least one of 'title', 'body' or 'state' must be provided")
        return self

    model_config = {
        "json_schema_extra": {
            "example": {"title": "Login button misaligned on iOS Safari", "state": "closed"}
        }
    }


class CommentCreate(BaseModel):
    """Body of ``POST /issues/{number}/comments``."""

    body: str = Field(..., min_length=1, description="Markdown comment body")

    model_config = {
        "json_schema_extra": {"example": {"body": "Reproduced on iPhone 14, iOS 17.4."}}
    }


# --------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------
class Issue(BaseModel):
    """Normalised issue.

    GitHub returns labels as objects and includes many fields a consumer of
    this gateway does not need.  We flatten labels to names and expose a
    stable subset.
    """

    number: int
    title: str
    body: str | None = None
    state: str
    labels: list[str] = Field(default_factory=list)
    html_url: str
    user: str | None = Field(None, description="Login of the issue author")
    comments: int = 0
    created_at: str
    updated_at: str
    closed_at: str | None = None

    @classmethod
    def from_github(cls, raw: dict[str, Any]) -> Issue:
        """Map a raw GitHub issue object onto this model."""
        return cls(
            number=raw["number"],
            title=raw.get("title") or "",
            body=raw.get("body"),
            state=raw.get("state", "open"),
            labels=_label_names(raw.get("labels")),
            html_url=raw.get("html_url", ""),
            user=(raw.get("user") or {}).get("login"),
            comments=raw.get("comments", 0) or 0,
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at", ""),
            closed_at=raw.get("closed_at"),
        )


class Comment(BaseModel):
    """Normalised issue comment."""

    id: int
    body: str | None = None
    user: str | None = None
    html_url: str
    created_at: str
    updated_at: str | None = None

    @classmethod
    def from_github(cls, raw: dict[str, Any]) -> Comment:
        return cls(
            id=raw["id"],
            body=raw.get("body"),
            user=(raw.get("user") or {}).get("login"),
            html_url=raw.get("html_url", ""),
            created_at=raw.get("created_at", ""),
            updated_at=raw.get("updated_at"),
        )


class ErrorDetail(BaseModel):
    field: str | None = None
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)


class Error(BaseModel):
    """The single error envelope used by every non-2xx response."""

    error: ErrorBody

    model_config = {
        "json_schema_extra": {
            "example": {
                "error": {
                    "code": "invalid_request",
                    "message": "Request body failed validation",
                    "details": [{"field": "title", "message": "Field required"}],
                }
            }
        }
    }


class WebhookEvent(BaseModel):
    """A stored webhook delivery, as returned by ``GET /events``."""

    id: str = Field(..., description="GitHub X-GitHub-Delivery UUID")
    event: str
    action: str | None = None
    issue_number: int | None = None
    sender: str | None = None
    timestamp: str


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    repo: str
    version: str


def _label_names(labels: Any) -> list[str]:
    """GitHub labels may be objects or bare strings depending on the call."""
    if not labels:
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name")
            if name:
                names.append(name)
        elif isinstance(label, str):
            names.append(label)
    return names
