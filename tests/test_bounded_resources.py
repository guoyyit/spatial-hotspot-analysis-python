"""Bounded resource usage: state stays on disk; scans are window-pruned."""

from __future__ import annotations

from spatial_hotspot.models import Event


def test_engine_holds_no_unbounded_event_collection(engine):
    # Ingest a non-trivial number of events spread across many time buckets.
    events = [
        Event(
            event_id=f"e{i}",
            lon=(i % 9) * 0.001,
            lat=(i // 9 % 9) * 0.001,
            ts=float(i * 7200),
        )
        for i in range(2000)
    ]
    engine.ingest(events)

    # The engine caches only a handful of result objects, never raw events.
    assert len(engine._hotspot_cache) <= 4  # noqa: SLF001
    assert len(engine._clustering_cache) <= 4  # noqa: SLF001

    # The durable store is the single source of truth on disk.
    assert engine.storage.total_events() == 2000


def test_window_scan_touches_only_overlapping_buckets(engine):
    events = [
        Event(
            event_id=f"e{i}",
            lon=(i % 5) * 0.001,
            lat=(i % 5) * 0.001,
            ts=float(i * 3600),
        )
        for i in range(100)
    ]
    engine.ingest(events)

    traced_sql: list[str] = []
    engine.storage.set_query_tracer(traced_sql.append)

    counts = engine.storage.cell_counts_for_window(
        start=10 * 3600.0, end=12 * 3600.0, grid_size_m=250.0
    )

    window_queries = [sql for sql in traced_sql if "GROUP BY cx" in sql]
    assert window_queries, "expected a cell-aggregation query"
    assert "bucket BETWEEN 10 AND 11" in " ".join(
        window_queries[0].split()
    )
    # Only events with ts in [10h, 12h) are aggregated.
    assert sum(counts.values()) == 2

    # The pruned bucket span is directly observable via the summary table.
    assert engine.storage._bucket_span(  # noqa: SLF001
        10 * 3600.0, 12 * 3600.0
    ) == (10, 11)


def test_points_query_is_bounded_to_window(engine):
    events = [
        Event(
            event_id=f"e{i}",
            lon=0.001 * (i % 4),
            lat=0.001 * (i % 4),
            ts=float(i * 1800),
        )
        for i in range(100)
    ]
    engine.ingest(events)
    points = engine.storage.events_in_window(0.0, 3600.0)
    assert len(points) == 2
    far_points = engine.storage.events_in_window(
        50 * 1800.0, 52 * 1800.0
    )
    assert len(far_points) == 2
