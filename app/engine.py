"""Analysis engine: ingest, incremental state, snapshots, rebuild."""

from __future__ import annotations

import threading

from .config import Settings
from .grid import GridIndex, TimeBucketIndex  # noqa: F401  (re-exported types)
from .store import EventStore


class _State:
    """Mutable analytical state; replaced atomically on rebuild."""

    def __init__(self, settings: Settings) -> None:
        self.global_grid = GridIndex(settings.cell_size_deg)
        self.buckets = TimeBucketIndex(
            settings.cell_size_deg,
            settings.bucket_seconds,
            settings.max_buckets_in_memory,
        )
        self.total = 0
        self.min_ts: float | None = None
        self.max_ts: float | None = None

    def add(self, lat: float, lon: float, ts: float) -> None:
        self.global_grid.add(lat, lon)
        self.buckets.add(lat, lon, ts)
        self.total += 1
        self.min_ts = ts if self.min_ts is None else min(self.min_ts, ts)
        self.max_ts = ts if self.max_ts is None else max(self.max_ts, ts)

    def fork(self) -> "_State":
        clone = _State.__new__(_State)
        clone.global_grid = self.global_grid.copy()
        clone.buckets = self.buckets.copy()
        clone.total = self.total
        clone.min_ts = self.min_ts
        clone.max_ts = self.max_ts
        return clone


class Snapshot:
    """Immutable, consistent view of the analytical state at a version."""

    def __init__(self, version: int, state: _State) -> None:
        self.version = version
        self.global_grid = state.global_grid
        self.buckets = state.buckets
        self.total_events = state.total
        self.min_ts = state.min_ts
        self.max_ts = state.max_ts


class Engine:
    """Coordinates store, in-memory indexes, and snapshot isolation.

    Concurrency model: a single write lock serializes ingest and state
    swaps; readers take a cheap copy-on-write snapshot under the same
    lock and compute outside it, so every query runs against one
    consistent version.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = EventStore(settings.db_path)
        self._lock = threading.RLock()
        self._state = _State(settings)
        self._version = 0
        self._rebuilding = False
        self._recent: list[tuple[float, float, float]] = []
        self._cluster_params: tuple[float, int] = (
            settings.cluster_eps_deg,
            settings.cluster_min_samples,
        )
        self._cluster_result: dict | None = None
        self._cluster_result_params: tuple[float, int] | None = None
        # Crash recovery: committed events are the source of truth, so
        # in-memory state is rebuilt from the store on startup. A stale
        # rebuild marker from a crashed run is simply cleared.
        self.store.set_meta("rebuild_in_progress", "0")
        self._rebuild_from_store()
        self._version = int(self.store.get_meta("version") or 0)

    # ------------------------------------------------------------------
    # ingest
    # ------------------------------------------------------------------

    def ingest(self, events: list[tuple[str, float, float, float, str]]) -> tuple[int, int, int]:
        """Insert events; returns (accepted, duplicates, version)."""
        if not events:
            return 0, 0, self._version
        with self._lock:
            existing = self._existing_ids([e[0] for e in events])
            seen: set[str] = set()
            fresh = []
            for e in events:
                if e[0] not in existing and e[0] not in seen:
                    seen.add(e[0])
                    fresh.append(e)
            inserted = self.store.insert_events(fresh)
            # insert_events commits all `fresh` rows atomically; apply them
            # to the in-memory state in the same critical section.
            for _event_id, lat, lon, ts, _attrs in fresh:
                self._state.add(lat, lon, ts)
                if self._rebuilding:
                    self._recent.append((lat, lon, ts))
            self._version += 1
            self.store.set_meta("version", str(self._version))
            return inserted, len(events) - inserted, self._version

    def _existing_ids(self, ids: list[str]) -> set[str]:
        found: set[str] = set()
        for chunk_start in range(0, len(ids), 500):
            chunk = ids[chunk_start : chunk_start + 500]
            placeholders = ",".join("?" for _ in chunk)
            rows = self.store._conn.execute(
                f"SELECT event_id FROM events WHERE event_id IN ({placeholders})",
                chunk,
            ).fetchall()
            found.update(row[0] for row in rows)
        return found

    # ------------------------------------------------------------------
    # snapshots
    # ------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(self._version, self._state.fork())

    @property
    def rebuilding(self) -> bool:
        return self._rebuilding

    # ------------------------------------------------------------------
    # full rebuild
    # ------------------------------------------------------------------

    def rebuild(self) -> int:
        """Full rebuild from the store; atomically swaps state. Returns version."""
        with self._lock:
            base_version = self._version
            self._rebuilding = True
            self._recent = []
            if self.store._db_path == ":memory:":
                # Single-connection store: scan under the write lock.
                rebuilt = self._build_state_from_store()
                for lat, lon, ts in self._recent:
                    rebuilt.add(lat, lon, ts)
                self._state = rebuilt
                self._rebuilding = False
                self._recent = []
                return base_version
            # Pin a consistent read snapshot while holding the write lock:
            # events committed after this point are invisible to the scan
            # and are captured in `_recent` by concurrent ingests instead.
            read_conn = self.store.open_snapshot()
        self.store.set_meta("rebuild_in_progress", "1")
        try:
            rebuilt = _State(self.settings)
            for _event_id, lat, lon, ts in EventStore.iter_events_from(read_conn):
                rebuilt.add(lat, lon, ts)
        finally:
            with self._lock:
                # Replay events committed while the rebuild was scanning;
                # they are not in the store snapshot we read.
                for lat, lon, ts in self._recent:
                    rebuilt.add(lat, lon, ts)
                self._state = rebuilt
                self._rebuilding = False
                self._recent = []
            read_conn.close()
            self.store.set_meta("rebuild_in_progress", "0")
        return base_version

    def _rebuild_from_store(self) -> None:
        self._state = self._build_state_from_store()

    def _build_state_from_store(self) -> _State:
        state = _State(self.settings)
        for _event_id, lat, lon, ts in self.store.iter_events():
            state.add(lat, lon, ts)
        return state

    # ------------------------------------------------------------------
    # clustering results (parameter-versioned)
    # ------------------------------------------------------------------

    def current_cluster_params(self) -> tuple[float, int]:
        return self._cluster_params

    def set_cluster_result(
        self,
        params: tuple[float, int],
        result: dict,
    ) -> int:
        with self._lock:
            if params != self._cluster_params:
                self._cluster_params = params
            self._cluster_result = result
            self._cluster_result_params = params
            version = self._version
        return version

    def get_cluster_result(self) -> tuple[dict | None, bool]:
        """Latest cluster result and whether its params are outdated."""
        with self._lock:
            if self._cluster_result is None:
                return None, False
            stale = self._cluster_result_params != self._cluster_params
            return self._cluster_result, stale

    def invalidate_clusters(self, params: tuple[float, int]) -> None:
        """Mark existing results stale when query params change."""
        with self._lock:
            if params != self._cluster_params:
                self._cluster_params = params

    def close(self) -> None:
        self.store.close()
