"""Concurrent writers/readers never observe a torn or mixed snapshot."""

from __future__ import annotations

import threading

from spatial_hotspot.models import (
    CorrectionMethod,
    Event,
    HotspotParams,
    KernelMethod,
)

from .conftest import block_events, grid_events


PARAMS = HotspotParams(
    alpha=0.05,
    bandwidth_m=750.0,
    correction=CorrectionMethod.FDR_BH,
    kernel=KernelMethod.BINARY,
    grid_size_m=250.0,
)


def test_concurrent_ingest_does_not_lose_or_duplicate(engine):
    n_threads = 8
    per_thread = 60
    barrier = threading.Barrier(n_threads)

    def worker(tid: int):
        events = [
            Event(
                event_id=f"t{tid}_e{i}",
                lon=(tid % 10) * 0.001 + i * 0.000_001,
                lat=(i % 10) * 0.001,
                ts=3600.0 + i,
            )
            for i in range(per_thread)
        ]
        barrier.wait()
        engine.ingest(events)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert engine.storage.total_events() == n_threads * per_thread

    counts = engine.storage.cell_counts_for_window(None, None, 250.0)
    assert sum(counts.values()) == n_threads * per_thread


def test_reader_sees_consistent_version_under_writes(engine):
    engine.ingest(grid_events(n=5, prefix="seed"))
    stop = threading.Event()
    observed: list[tuple[int, int]] = []

    def writer():
        i = 0
        while not stop.is_set():
            engine.ingest(
                [
                    Event(
                        event_id=f"w{i}",
                        lon=(i % 13) * 0.001,
                        lat=(i % 7) * 0.001,
                        ts=7200.0 + i,
                    )
                ]
            )
            i += 1

    def reader():
        while not stop.is_set():
            version, count, counts = engine.storage.snapshot_cell_counts(
                None, None, 250.0
            )
            observed.append((version, count))
            # Total and per-cell counts come from one WAL snapshot, so
            # they must always agree exactly, never show torn state.
            assert sum(counts.values()) == count

    writers = [threading.Thread(target=writer) for _ in range(3)]
    readers = [threading.Thread(target=reader) for _ in range(3)]
    for thread in writers + readers:
        thread.start()
    stop.wait(1.0)
    stop.set()
    for thread in writers + readers:
        thread.join()

    # Final state is stable and aggregates match the raw table exactly.
    final_count = engine.storage.total_events()
    final_cell_total = sum(
        engine.storage.cell_counts_for_window(None, None, 250.0).values()
    )
    assert final_cell_total == final_count
    assert observed


def test_hotspot_query_snapshot_is_self_consistent(engine):
    engine.ingest(grid_events() + block_events())
    result = engine.compute_hotspots(PARAMS, use_cache=False).result
    total_in_cells = sum(cell.event_count for cell in result.cells)
    assert total_in_cells == result.n_events
    assert result.version == engine.version()
    assert result.n_tests == len(result.cells)
