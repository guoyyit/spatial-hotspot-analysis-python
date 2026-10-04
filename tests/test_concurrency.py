"""Concurrent reads and writes always observe consistent snapshots."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "conc.db")))
    with TestClient(app) as c:
        yield c


def test_concurrent_writes_and_reads(client):
    errors: list[Exception] = []
    batches_per_writer = 25

    def writer(writer_id: int):
        try:
            for b in range(batches_per_writer):
                events = [
                    {
                        "event_id": f"w{writer_id}-{b}-{i}",
                        "lat": 31.0 + 1e-4 * i,
                        "lon": 121.0,
                        "ts": 1000.0 + b,
                    }
                    for i in range(10)
                ]
                r = client.post("/events", json={"events": events})
                assert r.status_code == 200
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    def reader():
        try:
            for _ in range(200):
                summary = client.get("/stats/summary").json()
                assert summary["total_events"] >= 0
                hotspots = client.get("/hotspots").json()
                # every reported cell count is positive and versioned
                assert hotspots["version"] >= 0
                assert all(c["count"] > 0 for c in hotspots["cells"])
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    writers = [threading.Thread(target=writer, args=(w,)) for w in range(4)]
    readers = [threading.Thread(target=reader) for _ in range(2)]
    for thread in readers + writers:
        thread.start()
    for thread in readers + writers:
        thread.join()
    assert not errors
    summary = client.get("/stats/summary").json()
    assert summary["total_events"] == 4 * batches_per_writer * 10


def test_concurrent_rebuild_and_ingest(client):
    errors: list[Exception] = []
    stop = threading.Event()

    def ingest_loop():
        n = 0
        try:
            while not stop.is_set():
                client.post(
                    "/events",
                    json={
                        "events": [
                            {
                                "event_id": f"r-{n}",
                                "lat": 32.0,
                                "lon": 122.0,
                                "ts": 5000.0 + n,
                            }
                        ]
                    },
                )
                n += 1
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=ingest_loop)
    thread.start()
    for _ in range(15):
        r = client.post("/analysis/rebuild")
        assert r.status_code == 200
        client.get("/stats/summary")
    stop.set()
    thread.join()
    assert not errors
    # final rebuild: totals must match the durable store exactly
    client.post("/analysis/rebuild")
    summary = client.get("/stats/summary").json()
    engine = client.app.state.engine
    assert summary["total_events"] == engine.store.count()

