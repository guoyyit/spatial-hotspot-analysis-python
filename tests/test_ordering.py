"""Ingest semantics: out-of-order, duplicate and late arrival events."""

from __future__ import annotations

import random

from spatial_hotspot.models import (
    CorrectionMethod,
    Event,
    HotspotParams,
    KernelMethod,
)

from .conftest import block_events, grid_events


HOTSPOT_PARAMS = HotspotParams(
    alpha=0.05,
    bandwidth_m=750.0,
    correction=CorrectionMethod.FDR_BH,
    kernel=KernelMethod.BINARY,
    grid_size_m=250.0,
)


def _dataset() -> list[Event]:
    return grid_events() + block_events()


def _hotspot_signatures(engine) -> dict[tuple[int, int], str]:
    result = engine.compute_hotspots(HOTSPOT_PARAMS, use_cache=False).result
    return {(c.cell_x, c.cell_y): c.kind for c in result.cells}


def test_duplicate_ids_count_once(engine):
    events = _dataset()
    first = engine.ingest(events)
    assert first.accepted == len(events)
    assert first.duplicates == 0

    again = engine.ingest(events)
    assert again.accepted == 0
    assert again.duplicates == len(events)
    assert again.version == first.version
    assert engine.storage.total_events() == len(events)


def test_within_batch_duplicates_collapse(engine):
    events = _dataset()
    duplicated = events + events[:10]
    report = engine.ingest(duplicated)
    assert report.accepted == len(events)
    assert report.duplicates == 10


def test_first_arrival_wins_for_duplicate_id(engine):
    engine.ingest([Event("e1", 0.0, 0.0, 100.0)])
    engine.ingest([Event("e1", 9.0, 9.0, 5000.0)])
    assert engine.storage.total_events() == 1
    assert engine.storage.window_event_count(0, 200) == 1
    assert engine.storage.window_event_count(4000, 6000) == 0


def test_out_of_order_ingest_is_equivalent(engine, db_path):
    ordered = _dataset()

    shuffled = ordered[:]
    random.Random(1234).shuffle(shuffled)
    engine.ingest(shuffled)
    shuffled_sigs = _hotspot_signatures(engine)

    from spatial_hotspot.engine import Engine

    other = Engine(db_path + ".order", engine.storage.config)  # noqa: SLF001
    try:
        other.ingest(ordered)
        ordered_sigs = {
            (c.cell_x, c.cell_y): c.kind
            for c in other.compute_hotspots(
                HOTSPOT_PARAMS, use_cache=False
            ).result.cells
        }
    finally:
        other.close()

    assert shuffled_sigs == ordered_sigs


def test_late_event_retroactively_enters_old_window(engine):
    engine.ingest(grid_events(prefix="g"))
    assert engine.storage.window_event_count(0, 1800) == 0

    late = Event("late1", 0.0001, 0.0001, 10.0)
    report = engine.ingest([late])
    assert report.accepted == 1

    assert engine.storage.window_event_count(0, 1800) == 1
    points = engine.storage.events_in_window(0, 1800)
    assert len(points) == 1


def test_version_increments_only_on_new_data(engine):
    events = _dataset()
    v0 = engine.version()
    engine.ingest(events[:5])
    v1 = engine.version()
    assert v1 == v0 + 1
    engine.ingest(events[:5])
    assert engine.version() == v1
    engine.ingest(events[5:10])
    assert engine.version() == v1 + 1


def test_half_open_window_boundaries(engine):
    engine.ingest(
        [
            Event("a", 0.0, 0.0, 0.0),
            Event("b", 0.0, 0.0, 3599.999),
            Event("c", 0.0, 0.0, 3600.0),
        ]
    )
    assert engine.storage.window_event_count(0, 3600) == 2
    assert engine.storage.window_event_count(0, 0) == 0
    assert engine.storage.window_event_count(3600, 7200) == 1
