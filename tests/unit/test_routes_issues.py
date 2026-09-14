""""Done by Jahnavi""""
"""Issue routes: validation, HTTP semantics and error propagation.

GitHub is mocked with respx throughout, so these run offline and fast.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tests.conftest import REPO_URL, load_fixture

ISSUE = load_fixture("github_issue.json")
COMMENT = load_fixture("github_comment.json")


# --------------------------------------------------------------------------
# Validation — no GitHub call should ever be made for a bad request
# --------------------------------------------------------------------------
@respx.mock
def test_missing_title_is_400_and_never_calls_github(client):
    route = respx.post(f"{REPO_URL}/issues")
    response = client.post("/issues", json={"body": "no title here"})

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert any(d["field"] == "title" for d in body["error"]["details"])
    assert not route.called


@respx.mock
def test_empty_title_is_400(client):
    assert client.post("/issues", json={"title": ""}).status_code == 400


@respx.mock
def test_non_string_title_is_400(client):
    assert client.post("/issues", json={"title": 12345}).status_code == 400


@respx.mock
def test_labels_must_be_strings(client):
    response = client.post("/issues", json={"title": "ok", "labels": [{"name": "bug"}]})
    assert response.status_code == 400


@respx.mock
@pytest.mark.parametrize("state", ["deleted", "OPEN", "", "all"])
def test_invalid_patch_state_is_400(client, state):
    """`all` is a valid *filter* but not a valid state to set."""
    response = client.patch("/issues/42", json={"state": state})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


@respx.mock
def test_empty_patch_body_is_400(client):
    response = client.patch("/issues/42", json={})
    assert response.status_code == 400
    assert "at least one" in str(response.json()["error"]["details"]).lower()


@respx.mock
@pytest.mark.parametrize("state", ["archived", "OPEN", "any"])
def test_invalid_list_state_filter_is_400(client, state):
    assert client.get(f"/issues?state={state}").status_code == 400


@respx.mock
@pytest.mark.parametrize("per_page", [0, -1, 101, 500])
def test_per_page_outside_bounds_is_400(client, per_page):
    assert client.get(f"/issues?per_page={per_page}").status_code == 400


@respx.mock
def test_page_below_one_is_400(client):
    assert client.get("/issues?page=0").status_code == 400


@respx.mock
def test_non_numeric_issue_number_is_400(client):
    assert client.get("/issues/not-a-number").status_code == 400


@respx.mock
def test_empty_comment_body_is_400(client):
    assert client.post("/issues/42/comments", json={"body": ""}).status_code == 400


@respx.mock
def test_malformed_json_body_is_400(client):
    response = client.post(
        "/issues", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------
@respx.mock
def test_create_issue_returns_201_with_location_header(client):
    respx.post(f"{REPO_URL}/issues").mock(return_value=httpx.Response(201, json=ISSUE))

    response = client.post(
        "/issues", json={"title": ISSUE["title"], "body": ISSUE["body"], "labels": ["bug", "ui"]}
    )

    assert response.status_code == 201
    assert response.headers["Location"] == "/issues/42"
    body = response.json()
    assert body["number"] == 42
    assert body["labels"] == ["bug", "ui"]  # flattened from GitHub's objects
    assert body["state"] == "open"
    assert body["user"] == "test-owner"


@respx.mock
def test_create_sends_correct_github_headers(client):
    route = respx.post(f"{REPO_URL}/issues").mock(return_value=httpx.Response(201, json=ISSUE))
    client.post("/issues", json={"title": "x"})

    sent = route.calls[0].request
    assert sent.headers["accept"] == "application/vnd.github+json"
    assert sent.headers["authorization"].startswith("Bearer ")
    assert sent.headers["x-github-api-version"] == "2022-11-28"


@respx.mock
def test_create_omits_absent_optional_fields(client):
    import json as jsonlib

    route = respx.post(f"{REPO_URL}/issues").mock(return_value=httpx.Response(201, json=ISSUE))
    client.post("/issues", json={"title": "just a title"})

    payload = jsonlib.loads(route.calls[0].request.content)
    assert payload == {"title": "just a title"}


# --------------------------------------------------------------------------
# List
# --------------------------------------------------------------------------
@respx.mock
def test_list_issues_returns_200(client):
    respx.get(f"{REPO_URL}/issues").mock(return_value=httpx.Response(200, json=[ISSUE]))
    response = client.get("/issues")

    assert response.status_code == 200
    assert response.json()[0]["number"] == 42
    assert response.headers["X-Total-Count"] == "1"


@respx.mock
def test_list_filters_out_pull_requests(client):
    """GitHub returns PRs from the issues endpoint; they are not issues."""
    pull = dict(ISSUE, number=43, pull_request={"url": "https://api.github.com/pulls/43"})
    respx.get(f"{REPO_URL}/issues").mock(return_value=httpx.Response(200, json=[ISSUE, pull]))

    numbers = [item["number"] for item in client.get("/issues").json()]
    assert numbers == [42]


@respx.mock
def test_list_forwards_query_parameters_to_github(client):
    route = respx.get(f"{REPO_URL}/issues").mock(return_value=httpx.Response(200, json=[]))
    client.get("/issues?state=closed&labels=bug,ui&page=3&per_page=50")

    params = route.calls[0].request.url.params
    assert params["state"] == "closed"
    assert params["labels"] == "bug,ui"
    assert params["page"] == "3"
    assert params["per_page"] == "50"


@respx.mock
def test_link_header_is_rewritten_to_this_service(client):
    link = (
        '<https://api.github.com/repositories/1/issues?page=2>; rel="next", '
        '<https://api.github.com/repositories/1/issues?page=5>; rel="last"'
    )
    respx.get(f"{REPO_URL}/issues").mock(
        return_value=httpx.Response(200, json=[ISSUE], headers={"Link": link})
    )

    response = client.get("/issues?state=open&per_page=30")
    forwarded = response.headers["Link"]
    assert "api.github.com" not in forwarded
    assert "/issues?" in forwarded
    assert 'rel="next"' in forwarded and 'rel="last"' in forwarded


@respx.mock
def test_rate_limit_headers_are_surfaced(client):
    respx.get(f"{REPO_URL}/issues").mock(
        return_value=httpx.Response(
            200, json=[], headers={"x-ratelimit-remaining": "4321", "x-ratelimit-limit": "5000"}
        )
    )
    response = client.get("/issues")
    assert response.headers["X-RateLimit-Remaining"] == "4321"


@respx.mock
def test_empty_list_is_200_not_404(client):
    respx.get(f"{REPO_URL}/issues").mock(return_value=httpx.Response(200, json=[]))
    response = client.get("/issues")
    assert response.status_code == 200
    assert response.json() == []


# --------------------------------------------------------------------------
# Read, update, close
# --------------------------------------------------------------------------
@respx.mock
def test_get_issue_returns_200(client):
    respx.get(f"{REPO_URL}/issues/42").mock(return_value=httpx.Response(200, json=ISSUE))
    assert client.get("/issues/42").json()["number"] == 42


@respx.mock
def test_get_missing_issue_is_404(client):
    respx.get(f"{REPO_URL}/issues/999").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    response = client.get("/issues/999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@respx.mock
def test_pull_request_number_is_404_from_get_issue(client):
    pr = dict(ISSUE, pull_request={"url": "https://api.github.com/pulls/42"})
    respx.get(f"{REPO_URL}/issues/42").mock(return_value=httpx.Response(200, json=pr))
    assert client.get("/issues/42").status_code == 404


@respx.mock
def test_patch_sends_only_provided_fields(client):
    import json as jsonlib

    route = respx.patch(f"{REPO_URL}/issues/42").mock(
        return_value=httpx.Response(200, json=dict(ISSUE, title="Renamed"))
    )
    response = client.patch("/issues/42", json={"title": "Renamed"})

    assert response.status_code == 200
    assert jsonlib.loads(route.calls[0].request.content) == {"title": "Renamed"}


@respx.mock
def test_patch_can_close_and_reopen(client):
    closed = dict(ISSUE, state="closed", closed_at="2026-09-08T19:01:44Z")
    respx.patch(f"{REPO_URL}/issues/42").mock(return_value=httpx.Response(200, json=closed))
    assert client.patch("/issues/42", json={"state": "closed"}).json()["state"] == "closed"

    respx.patch(f"{REPO_URL}/issues/42").mock(return_value=httpx.Response(200, json=ISSUE))
    assert client.patch("/issues/42", json={"state": "open"}).json()["state"] == "open"


@respx.mock
def test_delete_route_closes_rather_than_deletes(client):
    closed = dict(ISSUE, state="closed")
    route = respx.patch(f"{REPO_URL}/issues/42").mock(
        return_value=httpx.Response(200, json=closed)
    )
    response = client.delete("/issues/42")

    assert response.status_code == 200
    assert response.json()["state"] == "closed"
    assert response.headers["X-Delete-Semantics"] == "closed-not-deleted"
    assert route.called  # a PATCH upstream, never a DELETE


# --------------------------------------------------------------------------
# Comments
# --------------------------------------------------------------------------
@respx.mock
def test_create_comment_returns_201(client):
    respx.post(f"{REPO_URL}/issues/42/comments").mock(
        return_value=httpx.Response(201, json=COMMENT)
    )
    response = client.post("/issues/42/comments", json={"body": COMMENT["body"]})

    assert response.status_code == 201
    assert response.json()["id"] == COMMENT["id"]
    assert response.json()["user"] == "test-owner"
    assert response.headers["Location"] == "/issues/42/comments"


@respx.mock
def test_list_comments_returns_200(client):
    respx.get(f"{REPO_URL}/issues/42/comments").mock(
        return_value=httpx.Response(200, json=[COMMENT])
    )
    response = client.get("/issues/42/comments")
    assert response.status_code == 200
    assert len(response.json()) == 1


@respx.mock
def test_comment_on_missing_issue_is_404(client):
    respx.post(f"{REPO_URL}/issues/999/comments").mock(
        return_value=httpx.Response(404, json={"message": "Not Found"})
    )
    assert client.post("/issues/999/comments", json={"body": "hi"}).status_code == 404


# --------------------------------------------------------------------------
# Upstream failure propagation
# --------------------------------------------------------------------------
@respx.mock
def test_github_401_propagates_as_401(client):
    respx.get(f"{REPO_URL}/issues").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )
    response = client.get("/issues")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert "GITHUB_TOKEN" in response.json()["error"]["message"]


@respx.mock
def test_rate_limit_returns_429_with_retry_after(client):
    respx.get(f"{REPO_URL}/issues").mock(
        return_value=httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"x-ratelimit-remaining": "0", "retry-after": "42"},
        )
    )
    response = client.get("/issues")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "42"
    assert response.json()["error"]["code"] == "rate_limited"


@respx.mock
def test_github_5xx_becomes_503_not_500(client):
    respx.get(f"{REPO_URL}/issues").mock(return_value=httpx.Response(503, json={}))
    response = client.get("/issues")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"


@respx.mock
def test_github_timeout_becomes_503(client):
    respx.get(f"{REPO_URL}/issues").mock(side_effect=httpx.ConnectTimeout("too slow"))
    response = client.get("/issues")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "upstream_unavailable"


@respx.mock
def test_network_failure_becomes_503(client):
    respx.get(f"{REPO_URL}/issues").mock(side_effect=httpx.ConnectError("no route to host"))
    assert client.get("/issues").status_code == 503


@respx.mock
def test_422_validation_from_github_becomes_400(client):
    respx.post(f"{REPO_URL}/issues").mock(
        return_value=httpx.Response(
            422,
            json={
                "message": "Validation Failed",
                "errors": [{"resource": "Issue", "field": "labels", "code": "invalid"}],
            },
        )
    )
    response = client.post("/issues", json={"title": "x", "labels": ["nope"]})
    assert response.status_code == 400
    assert response.json()["error"]["details"][0]["field"] == "labels"


# --------------------------------------------------------------------------
# Cross-cutting
# --------------------------------------------------------------------------
@respx.mock
def test_every_error_uses_the_same_envelope(client):
    respx.get(f"{REPO_URL}/issues/999").mock(return_value=httpx.Response(404, json={}))
    for response in (
        client.post("/issues", json={}),
        client.get("/issues/999"),
        client.get("/no-such-route"),
    ):
        body = response.json()
        assert "error" in body
        assert {"code", "message"} <= set(body["error"])


@respx.mock
def test_request_id_is_echoed_and_generated(client):
    respx.get(f"{REPO_URL}/issues").mock(return_value=httpx.Response(200, json=[]))

    supplied = client.get("/issues", headers={"X-Request-ID": "abc123"})
    assert supplied.headers["X-Request-ID"] == "abc123"

    generated = client.get("/issues")
    assert generated.headers["X-Request-ID"]


def test_unknown_route_is_404_with_envelope(client):
    response = client.get("/definitely-not-a-route")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_healthz_does_not_call_github(client):
    with respx.mock:
        route = respx.get(f"{REPO_URL}/issues")
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "repo": "test-owner/test-repo", "version": "1.0.0"}
    assert not route.called


def test_openapi_json_is_served(client):
    spec = client.get("/openapi.json").json()
    assert spec["openapi"].startswith("3.1")
    for path in ("/issues", "/issues/{number}", "/issues/{number}/comments",
                 "/webhook", "/events", "/healthz"):
        assert path in spec["paths"]
