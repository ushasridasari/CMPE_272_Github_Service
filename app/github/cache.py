"""ETag cache for conditional GETs (extra credit).

GitHub does not charge a 304 against your rate limit, so replaying an ETag
with ``If-None-Match`` turns a repeated list call into a free one.  The cache
is deliberately tiny and in-process: it is an optimisation, never a source of
truth, and losing it on restart costs one extra API call.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CachedResponse:
    etag: str
    payload: Any
    link_header: str | None = None


class ETagCache:
    """A bounded LRU of ``cache key -> (etag, payload)``."""

    def __init__(self, max_entries: int = 256) -> None:
        self._max = max_entries
        self._items: OrderedDict[str, CachedResponse] = OrderedDict()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def build_key(path: str, params: dict[str, Any] | None = None) -> str:
        """Deterministic key: path plus sorted query parameters."""
        if not params:
            return path
        rendered = "&".join(f"{k}={params[k]}" for k in sorted(params) if params[k] is not None)
        return f"{path}?{rendered}"

    def get(self, key: str) -> CachedResponse | None:
        entry = self._items.get(key)
        if entry is None:
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return entry

    def set(self, key: str, etag: str, payload: Any, link_header: str | None = None) -> None:
        if not etag:
            return
        self._items[key] = CachedResponse(etag=etag, payload=payload, link_header=link_header)
        self._items.move_to_end(key)
        while len(self._items) > self._max:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self._items)
