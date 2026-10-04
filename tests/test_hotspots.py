"""Hotspot detection: significance, corrections, windows, late events."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.stats import adjust_pvalues
from conftest import make_events


@pytest.fixture()
def client(tmp_path):
    settings = Settings(
        db_path=str(tmp_path / "api.db"),
        cell_size_deg=0.01,
        bandwidth_deg=0.02,
        bucket_seconds=3600,
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _background_events(n_per_cell: int = 2, size: int = 10):
    """A uniform background grid so the global mean is well defined."""
    events = []
    for i in range(size):
        for j in range(size):
            for k in range(n_per_cell):
                events.append(
                    {
                        "event_id": f"bg-{i}-{j}-{k}",
                        "lat": 10.0 + i * 0.01 + 1e-6 * k,
                        "lon": 20.0 + j * 0.01,
                        "ts": 1000.0 + k,
                    }
                )
    return events


def test_obvious_cluster_is_flagged_hotspot(client):
    events = _background_events(size=15)
    for k in range(300):
        events.append(
            {
                "event_id": f"hot-{k}",
                "lat": 10.031 + 1e-3 * (k % 29),
                "lon": 20.031 + 1e-3 * (k % 27),
                "ts": 2000.0 + k,
            }
        )
    r = client.post("/events", json={"events": events})
    assert r.status_code == 200
    body = client.get("/hotspots").json()
    assert body["correction"] == "fdr_bh"
    hot_cells = [c for c in body["cells"] if c["label"] == "hotspot"]
    assert hot_cells, "expected at least one hotspot cell"
    hottest = max(hot_cells, key=lambda c: c["count"])
    assert hottest["count"] >= 10
    assert hottest["p_adjusted"] < body["alpha"]
    # every cell carries the correction method actually applied
    assert {c["correction"] for c in body["cells"]} == {"fdr_bh"}


def test_correction_methods_are_configurable_and_stricter(client):
    events = _background_events()
    for k in range(30):
        events.append(
            {
                "event_id": f"hot-{k}",
                "lat": 10.045 + 1e-5 * k,
                "lon": 20.045,
                "ts": 2000.0 + k,
            }
        )
    client.post("/events", json={"events": events})
    none_body = client.get("/hotspots", params={"correction": "none"}).json()
    bonf_body = client.get("/hotspots", params={"correction": "bonferroni"}).json()
    fdr_body = client.get("/hotspots", params={"correction": "fdr_bh"}).json()
    for cell_none, cell_bonf, cell_fdr in zip(
        none_body["cells"], bonf_body["cells"], fdr_body["cells"]
    ):
        assert cell_none["p_adjusted"] == pytest.approx(cell_none["p_value"])
        assert cell_bonf["p_adjusted"] >= cell_fdr["p_adjusted"] >= cell_none["p_adjusted"]
    n_none = sum(c["significant"] for c in none_body["cells"])
    n_bonf = sum(c["significant"] for c in bonf_body["cells"])
    assert n_bonf <= n_none


def test_alpha_and_bandwidth_are_configurable(client):
    client.post("/events", json={"events": _background_events()})
    strict = client.get("/hotspots", params={"alpha": 1e-9}).json()
    loose = client.get("/hotspots", params={"alpha": 0.5}).json()
    assert sum(c["significant"] for c in strict["cells"]) <= sum(
        c["significant"] for c in loose["cells"]
    )
    wide = client.get("/hotspots", params={"bandwidth_deg": 0.05}).json()
    assert wide["bandwidth_deg"] == 0.05


def test_window_query_and_late_event_retroactive_update(client):
    # events in two distinct hour-buckets
    early = [
        {"event_id": f"early-{k}", "lat": 10.0, "lon": 20.0, "ts": 3600.0 + k}
        for k in range(5)
    ]
    late = [
        {"event_id": f"late-{k}", "lat": 10.0, "lon": 20.0, "ts": 3700.0 + k}
        for k in range(5)
    ]
    client.post("/events", json={"events": early})
    window = {"start": 0.0, "end": 7200.0}
    before = client.get("/hotspots", params=window).json()
    count_before = sum(c["count"] for c in before["cells"])
    assert count_before == 5
    # late events land in the already-queried historical window
    client.post("/events", json={"events": late})
    after = client.get("/hotspots", params=window).json()
    count_after = sum(c["count"] for c in after["cells"])
    assert count_after == 10
    assert after["version"] > before["version"]


def test_adjust_pvalues_known_values():
    # Benjamini-Hochberg reference values
    adjusted = adjust_pvalues([0.001, 0.01, 0.04, 0.5], "fdr_bh")
    assert adjusted[0] == pytest.approx(0.004)
    assert adjusted[1] == pytest.approx(0.02)
    assert adjusted[2] == pytest.approx(0.04 * 4 / 3)
    assert adjusted[3] == pytest.approx(0.5)
    with pytest.raises(ValueError):
        adjust_pvalues([0.1], "unknown-method")
