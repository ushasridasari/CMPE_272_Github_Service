"""Pagination utilities: Link header parsing, rewriting and clamping."""

from __future__ import annotations

import pytest

from app.github.pagination import (
    clamp_per_page,
    page_params,
    parse_link_header,
    rewrite_link_header,
)

FULL_LINK = (
    '<https://api.github.com/repositories/1/issues?page=2>; rel="next", '
    '<https://api.github.com/repositories/1/issues?page=9>; rel="last", '
    '<https://api.github.com/repositories/1/issues?page=1>; rel="first", '
    '<https://api.github.com/repositories/1/issues?page=1>; rel="prev"'
)


def test_parse_none_and_empty():
    assert parse_link_header(None) == {}
    assert parse_link_header("") == {}


def test_parse_single_relation():
    header = '<https://api.github.com/x?page=2>; rel="next"'
    assert parse_link_header(header) == {"next": "https://api.github.com/x?page=2"}


def test_parse_all_four_relations():
    links = parse_link_header(FULL_LINK)
    assert set(links) == {"next", "last", "first", "prev"}
    assert links["last"].endswith("page=9")


def test_parse_handles_unquoted_rel():
    assert parse_link_header("<https://api.github.com/x?page=3>; rel=next") == {
        "next": "https://api.github.com/x?page=3"
    }


def test_parse_ignores_extra_link_params():
    header = '<https://api.github.com/x?page=2>; rel="next"; type="application/json"'
    assert parse_link_header(header)["next"].endswith("page=2")


def test_parse_skips_malformed_segments_without_raising():
    """A broken hint must not fail an otherwise good response."""
    header = 'garbage, <https://api.github.com/x?page=2>; rel="next", <unclosed; rel="last"'
    assert parse_link_header(header) == {"next": "https://api.github.com/x?page=2"}


def test_parse_url_containing_comma_is_not_split():
    header = '<https://api.github.com/x?labels=bug,ui&page=2>; rel="next"'
    assert parse_link_header(header)["next"].endswith("labels=bug,ui&page=2")


def test_page_params_extracts_only_pagination_keys():
    url = "https://api.github.com/x?state=open&page=4&per_page=50&labels=bug"
    assert page_params(url) == {"page": "4", "per_page": "50"}


def test_rewrite_points_links_at_this_service():
    rewritten = rewrite_link_header(
        FULL_LINK,
        public_base="http://localhost:8080",
        public_path="/issues",
        keep_params={"state": "closed", "per_page": "30"},
    )
    assert rewritten is not None
    assert "api.github.com" not in rewritten
    assert "http://localhost:8080/issues?" in rewritten
    assert "state=closed" in rewritten
    assert 'rel="next"' in rewritten


def test_rewrite_preserves_relation_order():
    rewritten = rewrite_link_header(
        FULL_LINK, public_base="http://localhost:8080", public_path="/issues"
    )
    order = [chunk.split('rel="')[1].rstrip('"') for chunk in rewritten.split(", ")]
    assert order == ["prev", "next", "first", "last"]


def test_rewrite_returns_none_when_nothing_to_rewrite():
    assert rewrite_link_header(None, public_base="http://x", public_path="/issues") is None
    assert rewrite_link_header("junk", public_base="http://x", public_path="/issues") is None


@pytest.mark.parametrize(
    ("given", "expected"),
    [(1, 1), (30, 30), (100, 100), (101, 100), (5000, 100), (0, 1), (-7, 1)],
)
def test_clamp_per_page(given, expected):
    assert clamp_per_page(given) == expected
