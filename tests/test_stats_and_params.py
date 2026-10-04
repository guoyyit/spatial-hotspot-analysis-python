"""Statistics, clustering determinism and parameter staleness semantics."""

from __future__ import annotations

from spatial_hotspot.clustering import dbscan, extract_clusters
from spatial_hotspot.models import (
    ClusteringParams,
    CorrectionMethod,
    Event,
    HotspotParams,
    KernelMethod,
)
from spatial_hotspot.stats import (
    adjust_pvalues,
    getis_ord_scan,
    normal_two_sided_p,
)

from .conftest import block_events, grid_events


def test_normal_pvalue():
    assert normal_two_sided_p(0.0) == 1.0
    assert normal_two_sided_p(4.0) < 1e-4
    assert normal_two_sided_p(-4.0) == normal_two_sided_p(4.0)


def test_correction_methods():
    raw = [0.001, 0.02, 0.5]
    assert adjust_pvalues(raw, CorrectionMethod.NONE) == raw
    assert adjust_pvalues(raw, CorrectionMethod.BONFERRONI) == [
        0.003,
        0.06,
        1.0,
    ]
    bh = adjust_pvalues(raw, CorrectionMethod.FDR_BH)
    assert bh == [0.003, 0.03, 0.5]


def test_bh_is_monotone_and_order_independent():
    raw = [0.5, 0.001, 0.02, 0.0009, 0.4]
    adjusted = adjust_pvalues(raw, CorrectionMethod.FDR_BH)
    paired = sorted(zip(raw, adjusted, strict=True))
    assert [p for _, p in paired] == sorted(p for _, p in paired)
    reversed_input = list(reversed(raw))
    reversed_adjusted = adjust_pvalues(
        reversed_input, CorrectionMethod.FDR_BH
    )
    assert list(reversed(reversed_adjusted)) == adjusted


def test_gistar_detects_injected_spatial_block(engine):
    engine.ingest(grid_events() + block_events())
    params = HotspotParams(
        alpha=0.05,
        bandwidth_m=750.0,
        correction=CorrectionMethod.FDR_BH,
        kernel=KernelMethod.BINARY,
        grid_size_m=250.0,
    )
    result = engine.compute_hotspots(params, use_cache=False).result
    hotspots = {(c.cell_x, c.cell_y) for c in result.cells if c.kind == "hotspot"}
    assert (7, 7) in hotspots
    assert result.correction_applied == CorrectionMethod.FDR_BH
    assert all(0.0 <= c.p_value_raw <= 1.0 for c in result.cells)
    assert all(0.0 <= c.p_value_adj <= 1.0 for c in result.cells)


def test_scan_order_independent_and_deterministic():
    counts = {(x, y): 3 for x in range(7) for y in range(7)}
    counts[(3, 3)] = 60
    kwargs = dict(
        grid_size_m=250,
        bandwidth_m=500,
        kernel=KernelMethod.BINARY,
        correction=CorrectionMethod.FDR_BH,
        alpha=0.05,
    )
    first = getis_ord_scan(counts, **kwargs)[2]
    second = getis_ord_scan(dict(reversed(list(counts.items()))), **kwargs)[2]
    assert [
        (c.cell_x, c.cell_y, c.z_score, c.p_value_adj, c.kind) for c in first
    ] == [
        (c.cell_x, c.cell_y, c.z_score, c.p_value_adj, c.kind) for c in second
    ]


def test_dbscan_labels_and_noise():
    points = [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0), (5000.0, 5000.0)]
    labels = dbscan(points, eps=50.0, min_samples=3)
    assert labels[:3] == [0, 0, 0]
    assert labels[3] == -1
    clusters, noise = extract_clusters(points, eps=50.0, min_samples=3)
    assert len(clusters) == 1
    assert clusters[0].size == 3
    assert noise == 1


def test_dbscan_preserves_coincident_multiplicity():
    points = [(0.0, 0.0)] * 10 + [(9000.0, 9000.0)]
    labels = dbscan(points, eps=1.0, min_samples=5)
    assert labels[:10] == [0] * 10
    assert labels[-1] == -1


def test_dbscan_deterministic_under_shuffle():
    points = [(i * 7 % 100 * 5.0, i * 13 % 100 * 5.0) for i in range(60)]
    first = dict(zip(points, dbscan(points, 30.0, 4), strict=True))
    shuffled = list(reversed(points))
    second = dict(
        zip(shuffled, dbscan(shuffled, 30.0, 4), strict=True)
    )
    assert first == second


def test_changed_params_mark_old_result_stale(engine):
    engine.ingest(grid_events() + block_events())
    params_a = HotspotParams(
        alpha=0.05,
        bandwidth_m=750.0,
        correction=CorrectionMethod.FDR_BH,
        kernel=KernelMethod.BINARY,
        grid_size_m=250.0,
    )
    params_b = HotspotParams(
        alpha=0.01,
        bandwidth_m=750.0,
        correction=CorrectionMethod.BONFERRONI,
        kernel=KernelMethod.BINARY,
        grid_size_m=250.0,
    )

    result_a = engine.compute_hotspots(params_a)
    assert result_a.status == "current"
    assert engine.compute_hotspots(params_a).status == "current"

    result_b = engine.compute_hotspots(params_b)
    assert result_b.status == "current"
    assert engine.compute_hotspots(params_a).status == "stale_params"

    refreshed_a = engine.compute_hotspots(params_a, use_cache=False)
    assert refreshed_a.status == "current"


def test_new_data_marks_cached_result_stale(engine):
    engine.ingest(grid_events())
    params = HotspotParams(
        alpha=0.05,
        bandwidth_m=750.0,
        correction=CorrectionMethod.FDR_BH,
        kernel=KernelMethod.BINARY,
        grid_size_m=250.0,
    )
    assert engine.compute_hotspots(params).status == "current"
    engine.ingest([Event("new", 0.001, 0.001, 9000.0)])
    cached = engine.compute_hotspots(params)
    assert cached.status == "stale_data"
    assert cached.result.version < engine.version()
    assert engine.compute_hotspots(params, use_cache=False).status == "current"


def test_cluster_param_change_marks_stale(engine):
    engine.ingest(grid_events() + block_events())
    p1 = ClusteringParams(eps_m=200.0, min_samples=10)
    p2 = ClusteringParams(eps_m=400.0, min_samples=20)
    assert engine.compute_clusters(p1).status == "current"
    assert engine.compute_clusters(p2).status == "current"
    assert engine.compute_clusters(p1).status == "stale_params"
