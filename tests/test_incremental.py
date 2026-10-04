"""Incremental updates must match a from-scratch full rebuild exactly."""

from __future__ import annotations

import random
import threading

from app.config import Settings
from app.engine import Engine
from conftest import make_events


def _state_signature(engine):
    snap = engine.snapshot()
    return (
        snap.global_grid.counts,
        snap.total_events,
        snap.min_ts,
        snap.max_ts,
        {k: dict(g.counts) for k, g in snap.buckets._buckets.items()},
    )


def test_incremental_equals_full_rebuild(settings):
    settings = Settings(**{**settings.__dict__, "max_buckets_in_memory": 10_000})
    engine = Engine(settings)
    rng = random.Random(7)
    for batch in range(20):
        events = []
        for i in range(50):
            events.append(
                (
                    f"b{batch}-{i}",
                    31.0 + rng.random() * 0.5,
                    121.0 + rng.random() * 0.5,
                    rng.uniform(0, 100_000),
                    "{}",
                )
            )
        engine.ingest(events)
    before = _state_signature(engine)
    engine.rebuild()
    after = _state_signature(engine)
    assert before == after
    engine.close()


def test_bucket_cache_is_bounded_but_global_stats_exact(settings):
    settings = Settings(**{**settings.__dict__, "max_buckets_in_memory": 4})
    engine = Engine(settings)
    for i in range(40):
        engine.ingest(make_events(f"evict-{i}", 3, 31.0, 121.0, i * 3600.0))
    snap = engine.snapshot()
    assert snap.buckets.resident_buckets() <= 4  # bounded memory
    assert snap.total_events == 120  # global stats still exact
    assert snap.global_grid.total() == 120
    engine.rebuild()
    assert engine.snapshot().global_grid.total() == 120
    engine.close()


def test_rebuild_concurrent_with_writes_stays_consistent(engine):
    """Writes during a rebuild must not be lost from the swapped state."""
    engine.ingest(make_events("base", 100, 31.0, 121.0, 5000.0))
    stop = threading.Event()
    errors: list[Exception] = []

    def writer():
        n = 0
        try:
            while not stop.is_set():
                engine.ingest(make_events(f"w{n}", 5, 32.0, 122.0, 60_000.0 + n))
                n += 1
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    for _ in range(10):
        engine.rebuild()
    stop.set()
    thread.join()
    assert not errors

    # After the dust settles, state must equal a clean full rebuild.
    live = _state_signature(engine)
    engine.rebuild()
    assert live == _state_signature(engine)
    assert engine.snapshot().total_events == engine.store.count()


def test_snapshot_is_consistent_version(engine):
    engine.ingest(make_events("s", 50, 31.0, 121.0, 1000.0))
    snap = engine.snapshot()
    version = snap.version
    engine.ingest(make_events("s2", 50, 33.0, 123.0, 2000.0))
    # the previously taken snapshot is unaffected by later writes
    assert snap.total_events == 50
    assert snap.version == version
    assert engine.snapshot().total_events == 100
