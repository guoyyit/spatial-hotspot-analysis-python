"""Density clustering: determinism, parameter invalidation, windows."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _two_cluster_events():
    events = []
    for k in range(20):
        events.append(
            {
                "event_id": f"a-{k}",
                "lat": 30.0 + 1e-4 * (k % 5),
                "lon": 120.0 + 1e-4 * (k % 4),
                "ts": 1000.0 + k,
            }
        )
    for k in range(20):
        events.append(
            {
                "event_id": f"b-{k}",
                "lat": 40.0 + 1e-4 * (k % 5),
                "lon": 130.0 + 1e-4 * (k % 4),
                "ts": 2000.0 + k,
            }
        )
    events.append({"event_id": "noise", "lat": 0.0, "lon": 0.0, "ts": 1500.0})
    return events


@pytest.fixture()
def client(tmp_path):
    app = create_app(Settings(db_path=str(tmp_path / "c.db")))
    with TestClient(app) as c:
        c.post("/events", json={"events": _two_cluster_events()})
        yield c


def test_clusters_found_and_deterministic(client):
    params = {"eps_deg": 0.01, "min_samples": 5}
    first = client.post("/clusters/compute", json=params).json()
    assert len(first["clusters"]) == 2
    assert first["noise_points"] == 1
    assert sorted(cl["size"] for cl in first["clusters"]) == [20, 20]
    second = client.post("/clusters/compute", json=params).json()
    assert first["clusters"] == second["clusters"]  # repeatable


def test_param_change_marks_previous_result_stale(client):
    client.post("/clusters/compute", json={"eps_deg": 0.01, "min_samples": 5})
    fresh = client.get("/clusters").json()
    assert fresh["stale"] is False
    stale = client.get("/clusters", params={"eps_deg": 0.5}).json()
    assert stale["stale"] is True


def test_cluster_window_restricts_points(client):
    # window covering only the first cluster's timestamps
    result = client.post(
        "/clusters/compute",
        json={"eps_deg": 0.01, "min_samples": 5, "start": 0.0, "end": 1500.0},
    ).json()
    assert len(result["clusters"]) == 1
    assert result["clusters"][0]["size"] == 20


def test_cluster_ids_stable_across_identical_runs(client):
    params = {"eps_deg": 0.01, "min_samples": 5}
    run1 = client.post("/clusters/compute", json=params).json()
    run2 = client.post("/clusters/compute", json=params).json()
    sig1 = [(c["cluster_id"], c["centroid_lat"], c["centroid_lon"]) for c in run1["clusters"]]
    sig2 = [(c["cluster_id"], c["centroid_lat"], c["centroid_lon"]) for c in run2["clusters"]]
    assert sig1 == sig2

