"""Incremental updates must match a from-scratch full computation."""

from __future__ import annotations

import os
import random

from spatial_hotspot.engine import Engine
from spatial_hotspot.models import (
    ClusteringParams,
    CorrectionMethod,
    Event,
    HotspotParams,
    KernelMethod,
)
from spatial_hotspot.storage import StorageConfig

from .conftest import block_events, grid_events


HOTSPOT_PARAMS = HotspotParams(
    alpha=0.05,
    bandwidth_m=750.0,
    correction=CorrectionMethod.FDR_BH,
    kernel=KernelMethod.BINARY,
    grid_size_m=250.0,
)
CLUSTER_PARAMS = ClusteringParams(eps_m=200.0, min_samples=10)


def _all_events() -> list[Event]:
    background = grid_events()
    cluster = block_events()
    late = [
        Event("late_a", 0.0004, 0.0004, 10.0),
        Event("late_b", 0.0004, 0.0005, 11.0),
    ]
    return background + cluster + late


def _snapshot(engine: Engine):
    hotspot = engine.compute_hotspots(HOTSPOT_PARAMS, use_cache=False).result
    clustering = engine.compute_clusters(
        CLUSTER_PARAMS, use_cache=False
    ).result
    return hotspot, clustering


def test_rebuild_preserves_cell_counts_and_results(engine):
    events = _all_events()
    random.Random(99).shuffle(events)
    engine.ingest(events)

    hotspot_before, clustering_before = _snapshot(engine)

    report = engine.full_rebuild(grid_size_m=250.0)
    assert report["verified_equal"] is True
    assert report["events"] == len(events)

    hotspot_after, clustering_after = _snapshot(engine)

    assert [
        (c.cell_x, c.cell_y, c.event_count, c.z_score, c.p_value_adj, c.kind)
        for c in hotspot_after.cells
    ] == [
        (c.cell_x, c.cell_y, c.event_count, c.z_score, c.p_value_adj, c.kind)
        for c in hotspot_before.cells
    ]
    assert clustering_after.clusters == clustering_before.clusters
    assert clustering_after.noise_count == clustering_before.noise_count


def test_incremental_batches_equal_single_full_ingest(db_path):
    events = _all_events()

    full_engine = Engine(
        db_path + ".full",
        StorageConfig(origin_lon=0.0, origin_lat=0.0, bucket_width_s=3600.0),
    )
    full_engine.ingest(events)
    full_hotspot, full_clustering = _snapshot(full_engine)

    incremental = Engine(db_path)
    shuffled = events[:]
    random.Random(7).shuffle(shuffled)
    batch_size = 37
    for start in range(0, len(shuffled), batch_size):
        incremental.ingest(shuffled[start : start + batch_size])
    inc_hotspot, inc_clustering = _snapshot(incremental)

    assert inc_hotspot.n_events == full_hotspot.n_events
    assert inc_hotspot.n_tests == full_hotspot.n_tests
    assert [
        (c.cell_x, c.cell_y, c.event_count, round(c.z_score, 12))
        for c in inc_hotspot.cells
    ] == [
        (c.cell_x, c.cell_y, c.event_count, round(c.z_score, 12))
        for c in full_hotspot.cells
    ]
    assert inc_clustering.clusters == full_clustering.clusters

    full_engine.close()
    incremental.close()


def test_rebuild_after_late_events_matches(db_path, engine):
    engine.ingest(grid_events())
    engine.full_rebuild()

    late = block_events(prefix="late") + [
        Event("late_point", 0.0002, 0.0002, 5.0)
    ]
    engine.ingest(late)
    hotspot_before, _ = _snapshot(engine)

    engine.full_rebuild()
    hotspot_after, _ = _snapshot(engine)

    assert [(c.cell_x, c.cell_y, c.kind) for c in hotspot_after.cells] == [
        (c.cell_x, c.cell_y, c.kind) for c in hotspot_before.cells
    ]


def test_rebuild_rejects_stale_target_and_retries(engine, monkeypatch):
    engine.ingest(grid_events(n=3, prefix="x"))
    target = engine.version()

    assert engine.storage.rebuild_bucket_stats(target + 100) is False
    outcome = engine.full_rebuild()
    assert outcome["version"] == target
