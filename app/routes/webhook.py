"""Webhook receiver.

Order of operations matters here:

1. Read the raw body (needed for HMAC — a re-serialised body will not match).
2. Verify the signature before parsing or trusting anything.
3. Reject unknown events early.
4. Persist synchronously — the insert is sub-millisecond and doing it before
   the ack is what makes redelivery safe.
5. Ack with 204, then do any further work in a background task so GitHub's
   10-second delivery timeout is never at risk.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request, Response, status

from app.config import Settings
from app.dependencies import get_event_store, get_settings_dep
from app.errors import BadRequestError, UnauthorizedError
from app.models import Error, WebhookEvent
from app.webhooks.store import EventStore
from app.webhooks.verify import verify_signature

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

#: Events this receiver understands.  Anything else is a 400 so that a
#: misconfigured webhook subscription is visible rather than silently ignored.
SUPPORTED_EVENTS = {"issues", "issue_comment", "ping"}

#: Actions we expect per event type.  An unknown action is also a 400.
SUPPORTED_ACTIONS: dict[str, set[str]] = {
    "issues": {
        "opened", "edited", "closed", "reopened", "deleted", "transferred",
        "pinned", "unpinned", "assigned", "unassigned", "labeled", "unlabeled",
        "locked", "unlocked", "milestoned", "demilestoned", "typed", "untyped",
    },
    "issue_comment": {"created", "edited", "deleted"},
}


def _summarise(payload: dict[str, Any]) -> tuple[int | None, str | None]:
    issue = payload.get("issue") or {}
    number = issue.get("number") if isinstance(issue, dict) else None
    sender = (payload.get("sender") or {}).get("login")
    return number, sender


def _post_process(delivery_id: str, event: str, action: str | None, number: int | None) -> None:
    """Placeholder for slow work that must not block the ack.

    Anything expensive — notifying Slack, reconciling a cache, kicking off a
    build — belongs here, after the 204 has already been written.
    """
    logger.info(
        "webhook_processed",
        extra={
            "delivery_id": delivery_id,
            "github_event": event,
            "action": action,
            "issue_number": number,
        },
    )


@router.post(
    "/webhook",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Receive a GitHub webhook delivery",
    responses={
        204: {"description": "Delivery accepted (or recognised as a duplicate)"},
        400: {"model": Error, "description": "Unknown event, unknown action, or malformed JSON"},
        401: {"model": Error, "description": "Missing or invalid HMAC signature"},
    },
)
async def receive_webhook(
    request: Request,
    background: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings_dep)],
    store: Annotated[EventStore, Depends(get_event_store)],
    x_github_event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
    x_github_delivery: Annotated[str | None, Header(alias="X-GitHub-Delivery")] = None,
    x_hub_signature_256: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
) -> Response:
    raw_body = await request.body()

    # 1. Authenticate before anything else.  Note we log *that* verification
    #    failed, never the signature value itself.
    if not verify_signature(settings.webhook_secret, raw_body, x_hub_signature_256):
        logger.warning(
            "webhook_signature_rejected",
            extra={"delivery_id": x_github_delivery, "github_event": x_github_event},
        )
        raise UnauthorizedError(
            "Webhook signature verification failed.", code="invalid_signature"
        )

    # 2. Only then look at the event type.
    if not x_github_event:
        raise BadRequestError("Missing X-GitHub-Event header.", code="missing_event_header")
    if x_github_event not in SUPPORTED_EVENTS:
        raise BadRequestError(
            f"Unsupported event type '{x_github_event}'.", code="unsupported_event"
        )

    try:
        payload = json.loads(raw_body or b"{}")
    except json.JSONDecodeError as exc:
        raise BadRequestError("Request body is not valid JSON.", code="malformed_payload") from exc
    if not isinstance(payload, dict):
        raise BadRequestError("Webhook payload must be a JSON object.", code="malformed_payload")

    # 3. `ping` is GitHub confirming the endpoint on save. Ack and stop.
    if x_github_event == "ping":
        logger.info("webhook_ping", extra={"delivery_id": x_github_delivery})
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    action = payload.get("action")
    allowed = SUPPORTED_ACTIONS.get(x_github_event, set())
    if action not in allowed:
        raise BadRequestError(
            f"Unsupported action '{action}' for event '{x_github_event}'.",
            code="unsupported_action",
        )

    number, sender = _summarise(payload)
    delivery_id = x_github_delivery or f"local-{x_github_event}-{action}-{number}"

    # 4. Persist. `is_new` is False when GitHub redelivers.
    is_new = store.record(
        delivery_id=delivery_id,
        event=x_github_event,
        action=action,
        issue_number=number,
        sender=sender,
        payload=payload,
    )

    if is_new:
        # 5. Slow work happens after the response is sent.
        background.add_task(_post_process, delivery_id, x_github_event, action, number)
    else:
        logger.info(
            "webhook_duplicate_ignored",
            extra={"delivery_id": delivery_id, "github_event": x_github_event, "action": action},
        )

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/events",
    response_model=list[WebhookEvent],
    tags=["webhooks"],
    summary="List recently processed webhook deliveries (debugging aid)",
)
async def list_events(
    store: Annotated[EventStore, Depends(get_event_store)],
    limit: int = Query(50, ge=1, le=500, description="Maximum deliveries to return"),
) -> list[WebhookEvent]:
    return [WebhookEvent(**row) for row in store.list_recent(limit)]
