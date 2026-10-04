"""Out-of-order, duplicate, and late-arrival ingestion semantics."""

from __future__ import annotations

import random

from app.config import Settings
from app.engine import Engine
from conftest import make_events


def test_duplicates_count_once(engine):
    events = make_events("dup", 10, 31.0, 121.0, 1000.0)
    accepted, duplicates, _ = engine.ingest(events)
    assert (accepted, duplicates) == (10, 0)
    accepted, duplicates, _ = engine.ingest(events)
    assert (accepted, duplicates) == (0, 10)
    assert engine.snapshot().total_events == 10


def test_duplicates_within_single_batch(engine):
    event = make_events("same", 1, 31.0, 121.0, 1000.0)[0]
    accepted, duplicates, _ = engine.ingest([event, event, event])
    assert (accepted, duplicates) == (1, 2)
    assert engine.snapshot().total_events == 1


def test_out_of_order_write_matches_ordered_write(settings):
    events = make_events("ord", 200, 31.0, 121.0, 10_000.0)
    shuffled = list(events)
    random.Random(42).shuffle(shuffled)

    eng_ordered = Engine(settings)
    for e in events:
        eng_ordered.ingest([e])
    ordered_grid = eng_ordered.snapshot().global_grid.counts
    eng_ordered.close()

    import tempfile

    other = Settings(**{**settings.__dict__, "db_path": tempfile.mktemp(suffix=".db")})
    eng_shuffled = Engine(other)
    for e in shuffled:
        eng_shuffled.ingest([e])
    shuffled_grid = eng_shuffled.snapshot().global_grid.counts
    eng_shuffled.close()

    assert ordered_grid == shuffled_grid


def test_late_arrival_updates_historical_window(engine):
    engine.ingest(make_events("on-time", 5, 31.0, 121.0, 3600.0))
    before = engine.snapshot().buckets.aggregate(0, 7200).total()
    assert before == 5
    # late event belonging to an already-queried historical window
    engine.ingest([("late-1", 31.0, 121.0, 60.0, "{}")])
    after = engine.snapshot().buckets.aggregate(0, 7200).total()
    assert after == 6


def test_window_boundaries_are_half_open(engine):
    engine.ingest(
        [
            ("w1", 31.0, 121.0, 3600.0, "{}"),
            ("w2", 31.0, 121.0, 7200.0, "{}"),
        ]
    )
    store = engine.store
    assert [e[0] for e in store.events_in_window(3600.0, 7200.0)] == ["w1"]
    assert sorted(e[0] for e in store.events_in_window(3600.0, 7201.0)) == ["w1", "w2"]
