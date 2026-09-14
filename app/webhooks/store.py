"""Persistence for webhook deliveries.

SQLite keeps the service single-binary while still surviving a restart, which
matters because GitHub redelivers on failure and we must not double-process.

Idempotency comes from the primary key ``(delivery_id, action)`` combined with
``INSERT OR IGNORE``.  The delivery id alone would be enough for GitHub's own
retries, but including the action keeps the key meaningful if a delivery is
ever replayed by hand with a different action.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS webhook_events (
    delivery_id   TEXT    NOT NULL,
    action        TEXT    NOT NULL DEFAULT '',
    event         TEXT    NOT NULL,
    issue_number  INTEGER,
    sender        TEXT,
    payload       TEXT    NOT NULL,
    received_at   TEXT    NOT NULL,
    PRIMARY KEY (delivery_id, action)
);
CREATE INDEX IF NOT EXISTS idx_events_received_at
    ON webhook_events (received_at DESC);
"""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class EventStore:
    """A tiny, thread-safe event log."""

    def __init__(self, db_path: str = "data/events.db") -> None:
        self._path = db_path
        self._lock = threading.Lock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record(
        self,
        *,
        delivery_id: str,
        event: str,
        action: str | None,
        issue_number: int | None,
        sender: str | None,
        payload: dict[str, Any],
    ) -> bool:
        """Store a delivery.

        Returns ``True`` if this was the first time we saw it and ``False``
        if it was a duplicate, so the caller can log the difference and skip
        any side effects.
        """
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO webhook_events
                    (delivery_id, action, event, issue_number, sender, payload, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    action or "",
                    event,
                    issue_number,
                    sender,
                    json.dumps(payload)[:200_000],
                    _utcnow(),
                ),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent deliveries, newest first."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT delivery_id, event, action, issue_number, sender, received_at
                FROM webhook_events
                ORDER BY received_at DESC, rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["delivery_id"],
                "event": row["event"],
                "action": row["action"] or None,
                "issue_number": row["issue_number"],
                "sender": row["sender"],
                "timestamp": row["received_at"],
            }
            for row in rows
        ]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM webhook_events").fetchone()[0])

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM webhook_events")
            self._conn.commit()
