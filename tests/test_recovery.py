"""Crash recovery: restart from durable state, no half-updated results."""

from __future__ import annotations

import os
import sqlite3

from app.config import Settings
from app.engine import Engine
from conftest import make_events


def _settings(db_path: str) -> Settings:
    return Settings(db_path=db_path, cell_size_deg=0.01, bandwidth_deg=0.02)


def test_restart_recovers_committed_state(tmp_path):
    db = str(tmp_path / "r.db")
    eng = Engine(_settings(db))
    eng.ingest(make_events("boot", 30, 31.0, 121.0, 3000.0))
    version_before = eng.snapshot().version
    grid_before = eng.snapshot().global_grid.counts
    eng.close()

    recovered = Engine(_settings(db))
    snap = recovered.snapshot()
    assert snap.total_events == 30
    assert snap.global_grid.counts == grid_before
    assert snap.version == version_before
    recovered.close()


def test_uncommitted_batch_leaves_no_partial_state(tmp_path):
    db = str(tmp_path / "partial.db")
    eng = Engine(_settings(db))
    eng.ingest(make_events("committed", 10, 31.0, 121.0, 1000.0))
    # simulate a crash mid-batch: open a transaction, insert, never commit
    raw = sqlite3.connect(db)
    raw.execute("BEGIN IMMEDIATE")
    raw.execute(
        "INSERT INTO events(event_id, lat, lon, ts, attrs) VALUES ('ghost', 1, 1, 1, '{}')"
    )
    raw.close()  # closes with the transaction uncommitted -> rolled back
    eng.close()

    recovered = Engine(_settings(db))
    assert recovered.snapshot().total_events == 10
    assert recovered.store.count() == 10
    recovered.close()


def test_stale_rebuild_marker_is_cleared_on_boot(tmp_path):
    db = str(tmp_path / "marker.db")
    eng = Engine(_settings(db))
    eng.ingest(make_events("m", 5, 31.0, 121.0, 100.0))
    # simulate a crash during rebuild: marker left set
    eng.store.set_meta("rebuild_in_progress", "1")
    eng.close()

    recovered = Engine(_settings(db))
    assert recovered.store.get_meta("rebuild_in_progress") == "0"
    assert recovered.snapshot().total_events == 5
    recovered.close()


def test_results_queryable_after_restart(tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    db = str(tmp_path / "api.db")
    app1 = create_app(_settings(db))
    with TestClient(app1) as client1:
        client1.post(
            "/events",
            json={
                "events": [
                    {"event_id": f"e{i}", "lat": 31.0, "lon": 121.0, "ts": 100.0 + i}
                    for i in range(20)
                ]
            },
        )
        before = client1.get("/stats/summary").json()

    app2 = create_app(_settings(db))
    with TestClient(app2) as client2:
        after = client2.get("/stats/summary").json()
        assert after["total_events"] == before["total_events"] == 20
        hotspots = client2.get("/hotspots").json()
        assert hotspots["cells"], "hotspot results must survive restart"


def test_wal_files_do_not_lose_commits(tmp_path):
    db = str(tmp_path / "wal.db")
    eng = Engine(_settings(db))
    for batch in range(10):
        eng.ingest(make_events(f"wal-{batch}", 10, 31.0, 121.0, 10.0 * batch))
    eng.close()  # checkpoint on close; commits must be durable either way
    assert os.path.exists(db)
    recovered = Engine(_settings(db))
    assert recovered.snapshot().total_events == 100
    recovered.close()

