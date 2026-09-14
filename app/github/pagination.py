"""Pagination utilities.

GitHub paginates with an RFC 8288 ``Link`` header.  We parse it so we can
(a) forward it to our own clients and (b) rewrite the URLs so they point at
this gateway instead of at api.github.com.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_LINK_SPLIT = re.compile(r',\s*(?=<)')
_REL = re.compile(r'rel\s*=\s*"?([^";,]+)"?')

#: Relations GitHub emits, in the order we like to re-emit them.
REL_ORDER = ("prev", "next", "first", "last")


def parse_link_header(value: str | None) -> dict[str, str]:
    """Parse a ``Link`` header into ``{rel: url}``.

    Tolerant by design: a malformed segment is skipped rather than raising,
    because a broken pagination hint should never fail an otherwise good
    response.

    >>> parse_link_header('<https://api.github.com/x?page=2>; rel="next"')
    {'next': 'https://api.github.com/x?page=2'}
    """
    if not value:
        return {}

    links: dict[str, str] = {}
    for segment in _LINK_SPLIT.split(value):
        segment = segment.strip()
        if not segment.startswith("<"):
            continue
        end = segment.find(">")
        if end == -1:
            continue
        url = segment[1:end].strip()
        rel_match = _REL.search(segment[end + 1 :])
        if not url or not rel_match:
            continue
        for rel in rel_match.group(1).split():
            links.setdefault(rel.strip(), url)
    return links


def page_params(url: str) -> dict[str, str]:
    """Extract just the pagination query parameters from a URL."""
    query = dict(parse_qsl(urlsplit(url).query))
    return {k: v for k, v in query.items() if k in ("page", "per_page")}


def rewrite_link_header(
    value: str | None,
    *,
    public_base: str,
    public_path: str,
    keep_params: dict[str, str] | None = None,
) -> str | None:
    """Rewrite GitHub's ``Link`` URLs to point at this service.

    A client of the gateway should never be handed an api.github.com URL it
    cannot call without a token, so each link is rebuilt as
    ``{public_base}{public_path}?...`` carrying the caller's own filters plus
    GitHub's page cursor.
    """
    links = parse_link_header(value)
    if not links:
        return None

    parts: list[str] = []
    for rel in REL_ORDER:
        target = links.get(rel)
        if not target:
            continue
        params = dict(keep_params or {})
        params.update(page_params(target))
        base = urlsplit(public_base)
        url = urlunsplit(
            (
                base.scheme or "http",
                base.netloc,
                public_path,
                urlencode(params, doseq=True),
                "",
            )
        )
        parts.append(f'<{url}>; rel="{rel}"')

    return ", ".join(parts) if parts else None


def clamp_per_page(per_page: int, maximum: int = 100) -> int:
    """Clamp ``per_page`` into GitHub's supported 1..100 window."""
    return max(1, min(per_page, maximum))
