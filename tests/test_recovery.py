"""Crash recovery across process restarts."""

from __future__ import annotations

import os
import sqlite3

from spatial_hotspot.engine import Engine
from spatial_hotspot.models import Event
from spatial_hotspot.storage import Storage, StorageConfig

from .conftest import block_events, grid_events


def _remove_wal_artifacts(path: str) -> None:
    for suffix in ("-wal", "-shm"):
        wal = path + suffix
        if os.path.exists(wal):
            os.remove(wal)


def test_restart_resumes_from_committed_state(db_path):
    engine = Engine(db_path, StorageConfig(0.0, 0.0, 3600.0))
    events = grid_events()
    report = engine.ingest(events)
    hotspot = engine.compute_hotspots.__self__  # noqa: B018
    engine.close()

    restarted = Engine(db_path, StorageConfig(0.0, 0.0, 3600.0))
    assert restarted.version() == report.version
    assert restarted.storage.total_events() == len(events)
    assert restarted.storage.window_event_count(None, None) == len(events)
    assert (
        sum(
            restarted.storage.cell_counts_for_window(
                None, None, 250.0
            ).values()
        )
        == len(events)
    )
    restarted.close()


def test_committed_results_queryable_after_crash(db_path):
    engine = Engine(db_path, StorageConfig(0.0, 0.0, 3600.0))
    engine.ingest(grid_events() + block_events())
    before = engine.full_rebuild()
    engine.close()
    _remove_wal_artifacts(db_path)

    restarted = Engine(db_path, StorageConfig(0.0, 0.0, 3600.0))
    after = restarted.full_rebuild()
    assert after["events"] == before["events"]
    assert restarted.version() == before["version"]
    restarted.close()


def test_uncommitted_batch_leaves_no_partial_state(db_path):
    engine = Engine(db_path, StorageConfig(0.0, 0.0, 3600.0))
    engine.ingest([Event("committed1", 0.0, 0.0, 3600.0)])
    committed_version = engine.version()
    engine.close()

    # Simulate a writer that starts a batch but dies before COMMIT.
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO events(event_id, lon, lat, ts, x, y, bucket) "
        "VALUES ('ghost1', 1, 1, 1, 1, 1, 0)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO bucket_stats(bucket, count, min_ts, max_ts) "
        "VALUES (0, 999, 0, 100)"
    )
    # Abruptly drop the connection without committing.
    conn.close()
    _remove_wal_artifacts(db_path)

    restarted = Storage(db_path, StorageConfig(0.0, 0.0, 3600.0))
    assert restarted.total_events() == 1
    assert restarted.current_version() == committed_version
    assert restarted.window_event_count(None, None) == 1
    assert sum(restarted.cell_counts_for_window(None, None, 250.0).values()) == 1
    restarted.close()


def test_staging_table_from_crashed_rebuild_is_cleaned(db_path):
    engine = Engine(db_path, StorageConfig(0.0, 0.0, 3600.0))
    engine.ingest(grid_events(n=4, prefix="r"))
    engine.close()

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute(
        "CREATE TABLE bucket_stats_stage_7 ("
        "bucket INTEGER PRIMARY KEY, count INTEGER NOT NULL, "
        "min_ts REAL NOT NULL, max_ts REAL NOT NULL) WITHOUT ROWID"
    )
    conn.execute(
        "INSERT INTO bucket_stats_stage_7 VALUES (0, 1, 0, 1)"
    )
    conn.close()

    restarted = Engine(db_path, StorageConfig(0.0, 0.0, 3600.0))
    names = [
        row[0]
        for row in restarted.storage._write_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    assert not any(name.startswith("bucket_stats_stage_") for name in names)

    # Existing committed aggregates are intact and still queryable.
    assert restarted.storage.total_events() == 16 * 4
    restarted.close()


def test_repeated_reopen_is_stable(db_path):
    events = grid_events(n=3)
    for reopen in range(3):
        engine = Engine(db_path, StorageConfig(0.0, 0.0, 3600.0))
        if reopen == 0:
            engine.ingest(events)
        assert engine.storage.total_events() == len(events)
        engine.close()
        _remove_wal_artifacts(db_path)
