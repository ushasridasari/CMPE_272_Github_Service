"""Webhook receiver: authentication, event handling, idempotency."""

from __future__ import annotations

import json

import pytest

from tests.conftest import TEST_SECRET, load_fixture

ISSUE_OPENED = load_fixture("issue_opened.json")
COMMENT_CREATED = load_fixture("issue_comment_created.json")
PING = load_fixture("ping.json")


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
def test_valid_signature_is_accepted(client, signed, store):
    body, headers = signed(ISSUE_OPENED)
    response = client.post("/webhook", content=body, headers=headers)

    assert response.status_code == 204
    assert response.content == b""
    assert store.count() == 1


def test_invalid_signature_is_401_and_stores_nothing(client, signed, store):
    body, headers = signed(ISSUE_OPENED)
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64

    response = client.post("/webhook", content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"
    assert store.count() == 0


def test_missing_signature_header_is_401(client, store):
    response = client.post(
        "/webhook",
        content=json.dumps(ISSUE_OPENED).encode(),
        headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "d1"},
    )
    assert response.status_code == 401
    assert store.count() == 0


def test_tampered_body_is_401(client, signed, store):
    """Signature computed over the original body, body altered in flight."""
    body, headers = signed(ISSUE_OPENED, tamper=True)
    assert client.post("/webhook", content=body, headers=headers).status_code == 401
    assert store.count() == 0


def test_signature_from_wrong_secret_is_401(client, signed):
    body, headers = signed(ISSUE_OPENED, secret="the-attackers-guess")
    assert client.post("/webhook", content=body, headers=headers).status_code == 401


def test_signature_checked_before_event_type(client, signed):
    """An unsigned request with a bad event must not leak which check failed."""
    body = json.dumps({"action": "opened"}).encode()
    response = client.post(
        "/webhook",
        content=body,
        headers={"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert response.status_code == 401


def test_signature_never_appears_in_the_error_response(client, signed):
    body, headers = signed(ISSUE_OPENED)
    headers["X-Hub-Signature-256"] = "sha256=" + "a" * 64
    response = client.post("/webhook", content=body, headers=headers)

    text = response.text
    assert "a" * 64 not in text
    assert TEST_SECRET not in text


# --------------------------------------------------------------------------
# Event handling
# --------------------------------------------------------------------------
def test_ping_is_acked_but_not_stored(client, signed, store):
    body, headers = signed(PING, event="ping")
    assert client.post("/webhook", content=body, headers=headers).status_code == 204
    assert store.count() == 0


def test_issue_comment_event_is_stored(client, signed, store):
    body, headers = signed(COMMENT_CREATED, event="issue_comment", delivery="c-1")
    assert client.post("/webhook", content=body, headers=headers).status_code == 204

    stored = store.list_recent()[0]
    assert stored["event"] == "issue_comment"
    assert stored["action"] == "created"
    assert stored["issue_number"] == 42


@pytest.mark.parametrize("action", ["opened", "closed", "reopened", "edited", "labeled"])
def test_common_issue_actions_accepted(client, signed, store, action):
    payload = dict(ISSUE_OPENED, action=action)
    body, headers = signed(payload, delivery=f"d-{action}")
    assert client.post("/webhook", content=body, headers=headers).status_code == 204


def test_unknown_event_is_400(client, signed, store):
    body, headers = signed({"ref": "refs/heads/main"}, event="push")
    response = client.post("/webhook", content=body, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_event"
    assert store.count() == 0


def test_unknown_action_is_400(client, signed, store):
    body, headers = signed(dict(ISSUE_OPENED, action="exploded"))
    response = client.post("/webhook", content=body, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_action"
    assert store.count() == 0


def test_missing_event_header_is_400(client, signed):
    body, headers = signed(ISSUE_OPENED)
    del headers["X-GitHub-Event"]
    response = client.post("/webhook", content=body, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_event_header"


def test_signed_but_malformed_json_is_400(client, signed):
    body, headers = signed(b"{this is not json")
    response = client.post("/webhook", content=body, headers=headers)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "malformed_payload"


def test_signed_json_array_is_400(client, signed):
    body, headers = signed(b'["not", "an", "object"]')
    assert client.post("/webhook", content=body, headers=headers).status_code == 400


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------
def test_redelivery_of_same_id_is_stored_once(client, signed, store):
    """GitHub retries failed deliveries; a retry must not duplicate."""
    body, headers = signed(ISSUE_OPENED, delivery="same-delivery-id")

    first = client.post("/webhook", content=body, headers=headers)
    second = client.post("/webhook", content=body, headers=headers)
    third = client.post("/webhook", content=body, headers=headers)

    assert [first.status_code, second.status_code, third.status_code] == [204, 204, 204]
    assert store.count() == 1
    assert len(client.get("/events").json()) == 1


def test_different_deliveries_are_stored_separately(client, signed, store):
    for delivery in ("delivery-a", "delivery-b", "delivery-c"):
        body, headers = signed(ISSUE_OPENED, delivery=delivery)
        client.post("/webhook", content=body, headers=headers)
    assert store.count() == 3


def test_same_id_different_action_both_stored(client, signed, store):
    """The composite key keeps a hand-replayed variant distinguishable."""
    for action in ("opened", "closed"):
        payload = dict(ISSUE_OPENED, action=action)
        body, headers = signed(payload, delivery="shared-id")
        client.post("/webhook", content=body, headers=headers)
    assert store.count() == 2


def test_delivery_without_id_still_processed(client, signed, store):
    """Curl-driven local testing has no X-GitHub-Delivery; degrade, don't fail."""
    body, headers = signed(ISSUE_OPENED)
    del headers["X-GitHub-Delivery"]
    assert client.post("/webhook", content=body, headers=headers).status_code == 204
    assert store.count() == 1


# --------------------------------------------------------------------------
# /events
# --------------------------------------------------------------------------
def test_events_empty_initially(client):
    assert client.get("/events").json() == []


def test_events_returns_newest_first(client, signed):
    for i, action in enumerate(("opened", "closed", "reopened")):
        payload = dict(ISSUE_OPENED, action=action)
        body, headers = signed(payload, delivery=f"d-{i}")
        client.post("/webhook", content=body, headers=headers)

    events = client.get("/events").json()
    assert len(events) == 3
    assert events[0]["action"] == "reopened"


def test_events_respects_limit(client, signed):
    for i in range(5):
        body, headers = signed(ISSUE_OPENED, delivery=f"n-{i}")
        client.post("/webhook", content=body, headers=headers)
    assert len(client.get("/events?limit=2").json()) == 2


def test_events_rejects_bad_limit(client):
    assert client.get("/events?limit=0").status_code == 400
    assert client.get("/events?limit=9999").status_code == 400


def test_events_shape_matches_contract(client, signed):
    body, headers = signed(ISSUE_OPENED, delivery="shape-check")
    client.post("/webhook", content=body, headers=headers)

    event = client.get("/events").json()[0]
    assert set(event) == {"id", "event", "action", "issue_number", "sender", "timestamp"}
    assert event["id"] == "shape-check"
    assert event["sender"] == "test-owner"


def test_events_does_not_expose_raw_payload(client, signed):
    """The debug endpoint returns a summary, not the whole delivery."""
    body, headers = signed(ISSUE_OPENED, delivery="no-payload")
    client.post("/webhook", content=body, headers=headers)
    assert "payload" not in client.get("/events").json()[0]
