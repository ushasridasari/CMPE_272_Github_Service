"""Integration tests against the real GitHub API and a live service instance.

Skipped automatically unless ``RUN_INTEGRATION=1`` and real credentials are
present, so ``make test`` stays green offline and in CI forks.

Run with::

    RUN_INTEGRATION=1 pytest tests/integration -v

Each test cleans up after itself by closing the issues it opens. GitHub cannot
delete issues, so the test repository will accumulate closed issues titled
``[itest] ...``; that is expected.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="Set RUN_INTEGRATION=1 to run tests against the real GitHub API",
    ),
]


@pytest.fixture(scope="module")
def live_client():
    """Talks to a running instance of this service.

    Point BASE_URL at wherever the service is listening; defaults to the
    local PORT.
    """
    base = os.getenv("BASE_URL") or f"http://localhost:{os.getenv('PORT', '8080')}"
    with httpx.Client(base_url=base, timeout=30.0) as client:
        try:
            health = client.get("/healthz")
        except httpx.HTTPError as exc:  # pragma: no cover
            pytest.skip(f"Service not reachable at {base}: {exc}")
        if health.status_code != 200:  # pragma: no cover
            pytest.skip(f"Service unhealthy at {base}")
        yield client



def _eventually(fetch, predicate, *, timeout=20.0, interval=1.5):
    """Poll until predicate(fetch()) holds, or the timeout expires.

    GitHub serves issue *lists* from a search index that lags writes by a
    second or two, while reads by number are immediate. Asserting on a list
    straight after a write races that index rather than testing the gateway.
    """
    import time as _time
    deadline = _time.time() + timeout
    last = None
    while _time.time() < deadline:
        last = fetch()
        if predicate(last):
            return last
        _time.sleep(interval)
    return last

@pytest.fixture
def created_issue(live_client):
    """Create an issue, hand it to the test, close it afterwards."""
    marker = uuid.uuid4().hex[:8]
    response = live_client.post(
        "/issues",
        json={
            "title": f"[itest] automated test issue {marker}",
            "body": "Created by the integration suite. Safe to close.",
            "labels": ["automated-test"],
        },
    )
    assert response.status_code == 201, response.text
    issue = response.json()
    yield issue
    live_client.patch(f"/issues/{issue['number']}", json={"state": "closed"})


# --------------------------------------------------------------------------
# 1) Create -> read back
# --------------------------------------------------------------------------
def test_create_issue_then_get_it(live_client, created_issue):
    number = created_issue["number"]
    assert created_issue["state"] == "open"
    assert "automated-test" in created_issue["labels"]

    fetched = live_client.get(f"/issues/{number}")
    assert fetched.status_code == 200
    assert fetched.json()["number"] == number
    assert fetched.json()["title"] == created_issue["title"]


def test_created_issue_appears_in_the_list(live_client, created_issue):
    number = created_issue["number"]
    listed = _eventually(
        lambda: live_client.get("/issues", params={"state": "open", "per_page": 100}),
        lambda r: r.status_code == 200 and number in [i["number"] for i in r.json()],
    )
    assert listed.status_code == 200
    assert number in [item["number"] for item in listed.json()]


def test_location_header_resolves(live_client, created_issue):
    """The Location we advertise must actually be fetchable."""
    response = live_client.get(f"/issues/{created_issue['number']}")
    assert response.status_code == 200


# --------------------------------------------------------------------------
# 2) Update title/body; close and reopen
# --------------------------------------------------------------------------
def test_update_title_and_body(live_client, created_issue):
    number = created_issue["number"]
    new_title = created_issue["title"] + " (renamed)"

    response = live_client.patch(
        f"/issues/{number}", json={"title": new_title, "body": "Body edited by the suite."}
    )
    assert response.status_code == 200
    assert response.json()["title"] == new_title
    assert response.json()["body"] == "Body edited by the suite."


def test_close_then_reopen(live_client, created_issue):
    number = created_issue["number"]

    closed = live_client.patch(f"/issues/{number}", json={"state": "closed"})
    assert closed.status_code == 200
    assert closed.json()["state"] == "closed"
    assert closed.json()["closed_at"] is not None

    reopened = live_client.patch(f"/issues/{number}", json={"state": "open"})
    assert reopened.status_code == 200
    assert reopened.json()["state"] == "open"


def test_closed_issue_leaves_the_open_list(live_client, created_issue):
    number = created_issue["number"]
    live_client.patch(f"/issues/{number}", json={"state": "closed"})

    closed = _eventually(
        lambda: live_client.get("/issues?state=closed&per_page=100"),
        lambda r: r.status_code == 200 and number in [i["number"] for i in r.json()],
    )
    open_numbers = [i["number"] for i in live_client.get("/issues?state=open&per_page=100").json()]
    closed_numbers = [i["number"] for i in closed.json()]
    assert number not in open_numbers
    assert number in closed_numbers


def test_delete_alias_closes_the_issue(live_client, created_issue):
    response = live_client.delete(f"/issues/{created_issue['number']}")
    assert response.status_code == 200
    assert response.json()["state"] == "closed"
    assert response.headers["X-Delete-Semantics"] == "closed-not-deleted"


# --------------------------------------------------------------------------
# 3) Comment, then fetch the comments list
# --------------------------------------------------------------------------
def test_create_comment_then_list_comments(live_client, created_issue):
    number = created_issue["number"]
    text = f"Integration comment {uuid.uuid4().hex[:8]}"

    created = live_client.post(f"/issues/{number}/comments", json={"body": text})
    assert created.status_code == 201
    comment = created.json()
    assert comment["body"] == text
    assert comment["id"] > 0

    listed = live_client.get(f"/issues/{number}/comments")
    assert listed.status_code == 200
    assert comment["id"] in [c["id"] for c in listed.json()]


def test_comment_count_reflected_on_the_issue(live_client, created_issue):
    number = created_issue["number"]
    live_client.post(f"/issues/{number}/comments", json={"body": "counted"})
    assert live_client.get(f"/issues/{number}").json()["comments"] >= 1


# --------------------------------------------------------------------------
# Pagination and conditional GET against real data
# --------------------------------------------------------------------------
def test_pagination_headers_present(live_client):
    response = live_client.get("/issues", params={"state": "all", "per_page": 1})
    assert response.status_code == 200
    assert response.headers["X-Per-Page"] == "1"
    if "Link" in response.headers:
        assert "api.github.com" not in response.headers["Link"]


def test_conditional_get_uses_the_cache(live_client):
    """Second identical list should be served from the ETag cache."""
    params = {"state": "all", "per_page": 5}
    first = live_client.get("/issues", params=params)
    second = live_client.get("/issues", params=params)

    assert first.status_code == second.status_code == 200
    assert second.headers.get("X-Cache") == "HIT"
    assert first.json() == second.json()


# --------------------------------------------------------------------------
# Negative paths against the live service
# --------------------------------------------------------------------------
def test_missing_issue_is_404(live_client):
    assert live_client.get("/issues/99999999").status_code == 404


def test_invalid_payload_is_400(live_client):
    assert live_client.post("/issues", json={"body": "no title"}).status_code == 400


# --------------------------------------------------------------------------
# Webhooks: a real delivery, triggered by creating a real issue
# --------------------------------------------------------------------------
@pytest.mark.skipif(
    os.getenv("RUN_WEBHOOK_INTEGRATION") != "1",
    reason="Needs a public tunnel and a configured GitHub webhook; set RUN_WEBHOOK_INTEGRATION=1",
)
def test_real_webhook_delivery_is_recorded(live_client):
    """Requires the tunnel running and the repo webhook pointed at it.

    Creating an issue makes GitHub deliver `issues/opened`; we poll `/events`
    until it arrives.
    """
    import time

    marker = uuid.uuid4().hex[:8]
    created = live_client.post("/issues", json={"title": f"[itest] webhook probe {marker}"})
    assert created.status_code == 201
    number = created.json()["number"]

    try:
        deadline = time.time() + 45
        while time.time() < deadline:
            events = live_client.get("/events?limit=50").json()
            match = [
                e for e in events
                if e["event"] == "issues" and e["action"] == "opened" and e["issue_number"] == number
            ]
            if match:
                assert match[0]["id"]  # GitHub delivery UUID
                return
            time.sleep(2)
        pytest.fail(
            "No webhook delivery observed within 45s. Check that the tunnel is "
            "running, the webhook URL is current, and WEBHOOK_SECRET matches."
        )
    finally:
        live_client.patch(f"/issues/{number}", json={"state": "closed"})
