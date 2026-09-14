"""Event store persistence and the conditional-GET cache."""

from __future__ import annotations

import httpx
import respx

from app.github.cache import ETagCache
from app.webhooks.store import EventStore
from tests.conftest import REPO_URL, load_fixture

ISSUE = load_fixture("github_issue.json")


# --------------------------------------------------------------------------
# EventStore
# --------------------------------------------------------------------------
def _record(store: EventStore, delivery_id: str, action: str = "opened") -> bool:
    return store.record(
        delivery_id=delivery_id,
        event="issues",
        action=action,
        issue_number=42,
        sender="test-owner",
        payload={"action": action},
    )


def test_record_returns_true_first_time_false_after():
    store = EventStore(":memory:")
    assert _record(store, "abc") is True
    assert _record(store, "abc") is False
    assert store.count() == 1
    store.close()


def test_composite_key_distinguishes_actions():
    store = EventStore(":memory:")
    assert _record(store, "abc", "opened") is True
    assert _record(store, "abc", "closed") is True
    assert store.count() == 2
    store.close()


def test_list_recent_limit_and_order():
    store = EventStore(":memory:")
    for i in range(10):
        _record(store, f"d{i}")
    assert len(store.list_recent(limit=3)) == 3
    assert len(store.list_recent()) == 10
    store.close()


def test_stored_row_shape():
    store = EventStore(":memory:")
    _record(store, "shape")
    row = store.list_recent()[0]
    assert set(row) == {"id", "event", "action", "issue_number", "sender", "timestamp"}
    store.close()


def test_persists_across_reconnect(tmp_path):
    """Survives a restart — the point of using SQLite over a dict."""
    path = str(tmp_path / "events.db")
    first = EventStore(path)
    _record(first, "durable")
    first.close()

    second = EventStore(path)
    assert second.count() == 1
    assert second.list_recent()[0]["id"] == "durable"
    second.close()


def test_creates_parent_directory(tmp_path):
    store = EventStore(str(tmp_path / "nested" / "deeper" / "events.db"))
    assert _record(store, "x") is True
    store.close()


def test_clear_empties_the_log():
    store = EventStore(":memory:")
    _record(store, "a")
    store.clear()
    assert store.count() == 0
    store.close()


def test_null_action_and_number_tolerated():
    store = EventStore(":memory:")
    assert store.record(
        delivery_id="sparse", event="issues", action=None,
        issue_number=None, sender=None, payload={},
    ) is True
    assert store.list_recent()[0]["action"] is None
    store.close()


def test_oversized_payload_is_truncated_not_rejected():
    store = EventStore(":memory:")
    huge = {"blob": "x" * 500_000}
    assert store.record(
        delivery_id="big", event="issues", action="opened",
        issue_number=1, sender="s", payload=huge,
    ) is True
    store.close()


# --------------------------------------------------------------------------
# ETagCache
# --------------------------------------------------------------------------
def test_cache_key_is_order_independent():
    a = ETagCache.build_key("/issues", {"state": "open", "page": 1})
    b = ETagCache.build_key("/issues", {"page": 1, "state": "open"})
    assert a == b


def test_cache_key_distinguishes_different_filters():
    open_key = ETagCache.build_key("/issues", {"state": "open"})
    closed_key = ETagCache.build_key("/issues", {"state": "closed"})
    assert open_key != closed_key


def test_cache_miss_then_hit():
    cache = ETagCache()
    assert cache.get("k") is None
    cache.set("k", 'W/"abc"', [{"number": 1}])
    entry = cache.get("k")
    assert entry.etag == 'W/"abc"'
    assert cache.hits == 1 and cache.misses == 1


def test_cache_ignores_empty_etag():
    cache = ETagCache()
    cache.set("k", "", [1])
    assert cache.get("k") is None


def test_cache_evicts_least_recently_used():
    cache = ETagCache(max_entries=2)
    cache.set("a", "1", "A")
    cache.set("b", "2", "B")
    cache.get("a")           # 'a' becomes most recent
    cache.set("c", "3", "C")  # evicts 'b'
    assert cache.get("a") is not None
    assert cache.get("b") is None


def test_cache_clear_resets_counters():
    cache = ETagCache()
    cache.set("k", "1", "v")
    cache.get("k")
    cache.clear()
    assert len(cache) == 0 and cache.hits == 0


# --------------------------------------------------------------------------
# Conditional GET end to end (extra credit)
# --------------------------------------------------------------------------
@respx.mock
def test_second_list_sends_if_none_match_and_serves_304_from_cache(client):
    route = respx.get(f"{REPO_URL}/issues")
    route.side_effect = [
        httpx.Response(200, json=[ISSUE], headers={"ETag": 'W/"etag-1"'}),
        httpx.Response(304, headers={"ETag": 'W/"etag-1"'}),
    ]

    first = client.get("/issues")
    assert first.status_code == 200
    assert first.headers["X-Cache"] == "MISS"

    second = client.get("/issues")
    assert second.status_code == 200          # 304 upstream, 200 to our client
    assert second.headers["X-Cache"] == "HIT"
    assert second.json() == first.json()      # served from cache

    assert route.calls[1].request.headers["if-none-match"] == 'W/"etag-1"'


@respx.mock
def test_different_query_does_not_reuse_etag(client):
    route = respx.get(f"{REPO_URL}/issues").mock(
        return_value=httpx.Response(200, json=[], headers={"ETag": 'W/"e"'})
    )
    client.get("/issues?state=open")
    client.get("/issues?state=closed")
    assert "if-none-match" not in route.calls[1].request.headers


@respx.mock
def test_write_invalidates_the_cache(client):
    """A create must not leave a stale list behind."""
    respx.get(f"{REPO_URL}/issues").mock(
        return_value=httpx.Response(200, json=[], headers={"ETag": 'W/"e1"'})
    )
    client.get("/issues")

    respx.post(f"{REPO_URL}/issues").mock(return_value=httpx.Response(201, json=ISSUE))
    client.post("/issues", json={"title": "new"})

    route = respx.get(f"{REPO_URL}/issues").mock(
        return_value=httpx.Response(200, json=[ISSUE], headers={"ETag": 'W/"e2"'})
    )
    response = client.get("/issues")
    assert "if-none-match" not in route.calls[-1].request.headers
    assert response.headers["X-Cache"] == "MISS"
