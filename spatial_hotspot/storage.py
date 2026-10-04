"""Durable local storage backed by a single on-machine SQLite database.

Concurrency model
-----------------
* The database runs in WAL mode with ``synchronous=FULL``; every commit is
  fsynced, so a crash never loses a committed batch and never leaves a
  half-applied one (SQLite guarantees atomicity per transaction).
* Read calls open a short-lived connection.  In WAL mode each statement
  observes one immutable snapshot taken when it starts; concurrent writers
  neither block it nor expose torn state.
* Writers are serialised by an in-process lock on one connection.  Raw
  events and their derived bucket summary are updated inside the same
  transaction, so committed statistics are always queryable and consistent
  with the raw data.

Storage stays independent of the analysis grid: ``grid_size_m`` is a
per-query parameter, while only fixed-width, epoch-anchored *time* buckets
and the projected coordinates are persisted.  The per-bucket summary lets
scans prune by bucket index so a window query never scans total history.

Rebuilds
--------
A full rebuild recomputes the bucket summary into a staging table and
atomically renames it into place under the write lock, guarded by a target
version.  Staging leftovers from a crashed rebuild are dropped at open.
"""

import json
import math
import sqlite3
import threading
from dataclasses import dataclass

from .models import Event
from .weights import project
from .windows import DEFAULT_BUCKET_WIDTH_S, time_bucket


@dataclass(frozen=True, slots=True)
class StorageConfig:
    origin_lon: float = 0.0
    origin_lat: float = 0.0
    bucket_width_s: float = DEFAULT_BUCKET_WIDTH_S


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    lon REAL NOT NULL,
    lat REAL NOT NULL,
    ts REAL NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    bucket INTEGER NOT NULL,
    ingest_seq INTEGER
);
CREATE INDEX IF NOT EXISTS events_ts_idx ON events(ts);
CREATE INDEX IF NOT EXISTS events_bucket_idx ON events(bucket);

CREATE TABLE IF NOT EXISTS bucket_stats (
    bucket INTEGER PRIMARY KEY,
    count INTEGER NOT NULL,
    min_ts REAL NOT NULL,
    max_ts REAL NOT NULL
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: str, config: StorageConfig | None = None) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._write_conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self._write_conn.row_factory = sqlite3.Row
        self._write_conn.execute("PRAGMA busy_timeout = 10000")
        self._write_conn.execute("PRAGMA journal_mode = WAL")
        self._write_conn.execute("PRAGMA synchronous = FULL")
        self._config = self._init_schema(config)

    # -- lifecycle -------------------------------------------------------

    def _init_schema(self, config: StorageConfig | None) -> StorageConfig:
        self._cleanup_staging()
        self._write_conn.executescript(SCHEMA)
        self._write_conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._write_conn.execute(
                "SELECT value FROM meta WHERE key = 'config'"
            ).fetchone()
            if row is None:
                resolved = config or StorageConfig()
                self._write_conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('config', ?)",
                    (
                        json.dumps(
                            {
                                "origin_lon": resolved.origin_lon,
                                "origin_lat": resolved.origin_lat,
                                "bucket_width_s": resolved.bucket_width_s,
                            }
                        ),
                    ),
                )
                self._write_conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('ingest_seq', '0')"
                )
            else:
                saved = json.loads(row["value"])
                resolved = StorageConfig(
                    origin_lon=saved["origin_lon"],
                    origin_lat=saved["origin_lat"],
                    bucket_width_s=saved["bucket_width_s"],
                )
                if config is not None and config != resolved:
                    raise ValueError(
                        f"config {config} conflicts with persisted {resolved} "
                        f"in {self.path}"
                    )
            self._write_conn.execute("COMMIT")
        except BaseException:
            self._write_conn.execute("ROLLBACK")
            raise
        return resolved

    def _cleanup_staging(self) -> None:
        rows = self._write_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'bucket_stats_stage_%'"
        ).fetchall()
        for row in rows:
            self._write_conn.execute(f"DROP TABLE IF EXISTS {row['name']}")

    def close(self) -> None:
        with self._lock:
            self._write_conn.close()

    @property
    def config(self) -> StorageConfig:
        return self._config

    def _read_connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA query_only = ON")
        tracer = getattr(self, "_query_tracer", None)
        if tracer is not None:
            conn.set_trace_callback(lambda sql: tracer(sql))
        return conn

    def set_query_tracer(self, tracer) -> None:
        """Attach a debug callback receiving every read-connection SQL."""

        self._query_tracer = tracer

    # -- versioning ------------------------------------------------------

    def current_version(self) -> int:
        with self._lock:
            row = self._write_conn.execute(
                "SELECT value FROM meta WHERE key = 'ingest_seq'"
            ).fetchone()
            return int(row["value"]) if row else 0

    def total_events(self) -> int:
        conn = self._read_connect()
        try:
            return int(
                conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
            )
        finally:
            conn.close()

    def event_time_range(self) -> tuple[float | None, float | None]:
        conn = self._read_connect()
        try:
            row = conn.execute(
                "SELECT MIN(ts) AS lo, MAX(ts) AS hi FROM events"
            ).fetchone()
        finally:
            conn.close()
        if row is None or row["lo"] is None:
            return None, None
        return float(row["lo"]), float(row["hi"])

    # -- ingest ----------------------------------------------------------

    def insert_events(self, events: list[Event]) -> tuple[int, int, int]:
        """Atomically apply one batch.

        Returns ``(accepted, duplicates, version_after)``.  The outcome is
        independent of batch order: in-batch duplicates collapse on
        ``event_id`` and only rows unknown to the database are accepted.
        """

        unique: dict[str, Event] = {}
        for event in events:
            unique.setdefault(event.event_id, event)

        with self._lock:
            self._write_conn.execute("BEGIN IMMEDIATE")
            try:
                accepted = 0
                for event_id in sorted(unique):
                    event = unique[event_id]
                    x, y = project(
                        event.lon,
                        event.lat,
                        self._config.origin_lon,
                        self._config.origin_lat,
                    )
                    bucket = time_bucket(event.ts, self._config.bucket_width_s)
                    cursor = self._write_conn.execute(
                        """
                        INSERT OR IGNORE INTO events(
                            event_id, lon, lat, ts, x, y, bucket, ingest_seq
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                        """,
                        (
                            event.event_id,
                            event.lon,
                            event.lat,
                            event.ts,
                            x,
                            y,
                            bucket,
                        ),
                    )
                    if cursor.rowcount > 0:
                        accepted += 1

                duplicates = len(events) - accepted
                if accepted:
                    self._write_conn.execute(
                        """
                        INSERT INTO bucket_stats(bucket, count, min_ts, max_ts)
                        SELECT bucket, COUNT(*), MIN(ts), MAX(ts)
                        FROM events WHERE ingest_seq IS NULL
                        GROUP BY bucket
                        ON CONFLICT(bucket) DO UPDATE SET
                            count = bucket_stats.count + excluded.count,
                            min_ts = MIN(bucket_stats.min_ts, excluded.min_ts),
                            max_ts = MAX(bucket_stats.max_ts, excluded.max_ts)
                        """
                    )
                    self._write_conn.execute(
                        "UPDATE meta SET value = "
                        "CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
                        "WHERE key = 'ingest_seq'"
                    )
                    new_version = int(
                        self._write_conn.execute(
                            "SELECT value FROM meta WHERE key = 'ingest_seq'"
                        ).fetchone()["value"]
                    )
                    self._write_conn.execute(
                        "UPDATE events SET ingest_seq = ? WHERE ingest_seq IS NULL",
                        (new_version,),
                    )

                version = int(
                    self._write_conn.execute(
                        "SELECT value FROM meta WHERE key = 'ingest_seq'"
                    ).fetchone()["value"]
                )
                self._write_conn.execute("COMMIT")
            except BaseException:
                self._write_conn.execute("ROLLBACK")
                raise
        return accepted, duplicates, version

    # -- reads -----------------------------------------------------------

    def _bucket_span(
        self,
        start: float | None,
        end: float | None,
    ) -> tuple[int, int] | None:
        conn = self._read_connect()
        try:
            row = conn.execute(
                "SELECT MIN(bucket) AS lo, MAX(bucket) AS hi FROM bucket_stats"
            ).fetchone()
        finally:
            conn.close()
        if row is None or row["lo"] is None:
            return None
        lo, hi = int(row["lo"]), int(row["hi"])
        if start is not None:
            lo = max(lo, time_bucket(start, self._config.bucket_width_s))
        if end is not None:
            hi = min(
                hi,
                math.ceil(end / self._config.bucket_width_s) - 1,
            )
        return (lo, hi) if lo <= hi else None

    def cell_counts_for_window(
        self,
        start: float | None,
        end: float | None,
        grid_size_m: float,
    ) -> dict[tuple[int, int], int]:
        """``(cell_x, cell_y) -> count`` aggregated from raw events.

        The scan is restricted to buckets overlapping ``[start, end)`` via
        the bucket index, so cost tracks the queried window rather than
        total history.  Cells are computed in SQL from projected metres and
        are therefore independent of any stored grid.
        """

        span = self._bucket_span(start, end)
        if span is None:
            return {}
        lo, hi = span
        clauses = ["bucket BETWEEN ? AND ?"]
        params: list[float] = [lo, hi]
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts < ?")
            params.append(end)
        query = (
            "SELECT CAST(FLOOR(x / ?) AS INTEGER) AS cx, "
            "       CAST(FLOOR(y / ?) AS INTEGER) AS cy, COUNT(*) AS c "
            "FROM events WHERE "
            + " AND ".join(clauses)
            + " GROUP BY cx, cy"
        )
        conn = self._read_connect()
        try:
            rows = conn.execute(
                query, [grid_size_m, grid_size_m, *params]
            ).fetchall()
        finally:
            conn.close()
        return {(int(r["cx"]), int(r["cy"])): int(r["c"]) for r in rows}

    def events_in_window(
        self,
        start: float | None,
        end: float | None,
    ) -> list[tuple[float, float]]:
        """Projected ``(x, y)`` points in ``[start, end)``, ordered by
        ``(ts, event_id)`` for deterministic downstream processing."""

        span = self._bucket_span(start, end)
        if span is None:
            return []
        lo, hi = span
        clauses = ["bucket BETWEEN ? AND ?"]
        params: list[float] = [lo, hi]
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts < ?")
            params.append(end)
        conn = self._read_connect()
        try:
            rows = conn.execute(
                "SELECT x, y FROM events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY ts, event_id",
                params,
            ).fetchall()
        finally:
            conn.close()
        return [(float(r["x"]), float(r["y"])) for r in rows]

    def window_event_count(
        self, start: float | None, end: float | None
    ) -> int:
        span = self._bucket_span(start, end)
        if span is None:
            return 0
        lo, hi = span
        clauses = ["bucket BETWEEN ? AND ?"]
        params: list[float] = [lo, hi]
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts < ?")
            params.append(end)
        conn = self._read_connect()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM events WHERE "
                + " AND ".join(clauses),
                params,
            ).fetchone()
        finally:
            conn.close()
        return int(row["c"])

    def snapshot_events(
        self,
        start: float | None,
        end: float | None,
    ) -> tuple[int, list[tuple[float, float]]]:
        """Single-snapshot ``(version, ordered points)`` for a window."""

        span = self._bucket_span(start, end)
        if span is None:
            return self.current_version(), []
        lo, hi = span
        clauses = ["bucket BETWEEN ? AND ?"]
        params: list[float] = [lo, hi]
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts < ?")
            params.append(end)
        conn = self._read_connect()
        try:
            conn.execute("BEGIN")
            rows = conn.execute(
                "SELECT x, y FROM events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY ts, event_id",
                params,
            ).fetchall()
            version = int(
                conn.execute(
                    "SELECT value AS v FROM meta WHERE key = 'ingest_seq'"
                ).fetchone()["v"]
            )
        finally:
            try:
                conn.execute("COMMIT")
            except sqlite3.Error:
                pass
            conn.close()
        return version, [(float(r["x"]), float(r["y"])) for r in rows]

    def snapshot_cell_counts(
        self,
        start: float | None,
        end: float | None,
        grid_size_m: float,
    ) -> tuple[int, int, dict[tuple[int, int], int]]:
        """Single-snapshot read.

        Returns ``(version, total_events_in_window, cell_counts)`` where
        the total and the per-cell counts are read on one connection, so
        in WAL mode they come from one immutable snapshot and always agree.
        """

        span = self._bucket_span(start, end)
        if span is None:
            return self.current_version(), 0, {}
        lo, hi = span
        clauses = ["bucket BETWEEN ? AND ?"]
        params: list[float] = [lo, hi]
        if start is not None:
            clauses.append("ts >= ?")
            params.append(start)
        if end is not None:
            clauses.append("ts < ?")
            params.append(end)
        where = " AND ".join(clauses)
        conn = self._read_connect()
        try:
            conn.execute("BEGIN")
            total = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM events WHERE " + where,
                    params,
                ).fetchone()["c"]
            )
            rows = conn.execute(
                "SELECT CAST(FLOOR(x / ?) AS INTEGER) AS cx, "
                "       CAST(FLOOR(y / ?) AS INTEGER) AS cy, COUNT(*) AS c "
                "FROM events WHERE "
                + where
                + " GROUP BY cx, cy",
                [grid_size_m, grid_size_m, *params],
            ).fetchall()
            version = int(
                conn.execute(
                    "SELECT value AS v FROM meta WHERE key = 'ingest_seq'"
                ).fetchone()["v"]
            )
        finally:
            try:
                conn.execute("COMMIT")
            except sqlite3.Error:
                pass
            conn.close()
        counts = {(int(r["cx"]), int(r["cy"])): int(r["c"]) for r in rows}
        return version, total, counts

    # -- rebuild ---------------------------------------------------------

    def rebuild_bucket_stats(self, target_version: int) -> bool:
        """Recompute the derived bucket summary from raw events.

        Serialised with ingest by the write lock.  The atomic rename only
        happens when the version still equals ``target_version``; a batch
        that lands first forces a retry (returns ``False``).  Reads during
        a rebuild see either the old table or the new one, never a mixture.
        """

        with self._lock:
            if self.current_version() != target_version:
                return False
            stage = f"bucket_stats_stage_{target_version}"
            self._write_conn.execute("BEGIN IMMEDIATE")
            try:
                if self.current_version() != target_version:
                    self._write_conn.execute("ROLLBACK")
                    return False
                self._write_conn.execute(f"DROP TABLE IF EXISTS {stage}")
                self._write_conn.execute(
                    f"""
                    CREATE TABLE {stage} (
                        bucket INTEGER PRIMARY KEY,
                        count INTEGER NOT NULL,
                        min_ts REAL NOT NULL,
                        max_ts REAL NOT NULL
                    ) WITHOUT ROWID
                    """
                )
                self._write_conn.execute(
                    f"""
                    INSERT INTO {stage}(bucket, count, min_ts, max_ts)
                    SELECT bucket, COUNT(*), MIN(ts), MAX(ts)
                    FROM events GROUP BY bucket
                    """
                )
                self._write_conn.execute(
                    "ALTER TABLE bucket_stats RENAME TO bucket_stats_old"
                )
                self._write_conn.execute(
                    f"ALTER TABLE {stage} RENAME TO bucket_stats"
                )
                self._write_conn.execute("DROP TABLE bucket_stats_old")
                if self.current_version() != target_version:
                    raise RuntimeError("version advanced during rebuild swap")
                self._write_conn.execute("COMMIT")
            except BaseException:
                self._write_conn.execute("ROLLBACK")
                self._cleanup_staging()
                raise
        return True
