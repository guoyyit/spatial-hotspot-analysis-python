"""SQLite-backed durable event store with dedup and meta state."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    ts REAL NOT NULL,
    attrs TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class EventStore:
    """Durable store. Committed events are the single source of truth.

    WAL journaling plus per-batch transactions guarantee that a crash
    leaves either the fully committed batch or nothing - never a
    half-updated state. Duplicate event_ids are ignored (INSERT OR
    IGNORE), so re-deliveries and late duplicates count exactly once.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        if db_path != ":memory:":
            directory = os.path.dirname(db_path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = __import__("threading").Lock()

    def open_snapshot(self) -> sqlite3.Connection:
        """Open a read transaction pinned to the current committed state.

        With WAL journaling this read snapshot does not block writers;
        events committed afterwards are invisible to it, which lets the
        caller reconcile them separately.
        """
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.execute("BEGIN")
        conn.execute("SELECT COUNT(*) FROM events")  # establish read snapshot
        return conn

    @staticmethod
    def iter_events_from(conn: sqlite3.Connection) -> Iterator[tuple[str, float, float, float]]:
        cursor = conn.execute(
            "SELECT event_id, lat, lon, ts FROM events ORDER BY event_id"
        )
        return iter(cursor.fetchall())

    def insert_events(self, events: list[tuple[str, float, float, float, str]]) -> int:
        """Insert (event_id, lat, lon, ts, attrs_json); returns # newly inserted."""
        if not events:
            return 0
        with self._lock:
            before = self._conn.total_changes
            with self._conn:  # single atomic transaction per batch
                self._conn.executemany(
                    "INSERT OR IGNORE INTO events(event_id, lat, lon, ts, attrs)"
                    " VALUES (?, ?, ?, ?, ?)",
                    events,
                )
            return self._conn.total_changes - before

    def iter_events(self) -> Iterator[tuple[str, float, float, float]]:
        cursor = self._conn.execute(
            "SELECT event_id, lat, lon, ts FROM events ORDER BY event_id"
        )
        return iter(cursor.fetchall())

    def events_in_window(
        self, start: float | None, end: float | None
    ) -> list[tuple[str, float, float, float]]:
        """Events with ts in [start, end); None bounds are open."""
        clauses: list[str] = []
        params: list[float] = []
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts < ?")
            params.append(end)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cursor = self._conn.execute(
            "SELECT event_id, lat, lon, ts FROM events" + where + " ORDER BY event_id",
            params,
        )
        return cursor.fetchall()

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def time_range(self) -> tuple[float | None, float | None]:
        row = self._conn.execute("SELECT MIN(ts), MAX(ts) FROM events").fetchone()
        return row[0], row[1]

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def close(self) -> None:
        self._conn.close()
