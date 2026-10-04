"""End-to-end HTTP behaviour."""

from __future__ import annotations

from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from spatial_hotspot.api import create_app
from spatial_hotspot.engine import Engine
from spatial_hotspot.models import Event
from spatial_hotspot.storage import StorageConfig

from .conftest import block_events, grid_events


@pytest.fixture
def client(db_path):
    engine = Engine(db_path, StorageConfig(0.0, 0.0, 3600.0))
    app = create_app(engine)
    with TestClient(app) as test_client:
        yield test_client, engine
    engine.close()


def _ingest(client):
    test_client, _ = client
    events = [asdict(e) for e in grid_events() + block_events()]
    response = test_client.post("/events", json={"events": events})
    assert response.status_code == 200
    return response.json()


def test_status_starts_empty(client):
    test_client, _ = client
    body = test_client.get("/status").json()
    assert body["version"] == 0
    assert body["total_events"] == 0


def test_ingest_and_duplicate_reporting(client):
    test_client, _ = client
    events = [asdict(e) for e in grid_events(n=2)]
    first = test_client.post("/events", json={"events": events}).json()
    assert first["accepted"] == len(events)
    second = test_client.post("/events", json={"events": events}).json()
    assert second["accepted"] == 0
    assert second["duplicates"] == len(events)
    assert second["version"] == first["version"]


def test_hotspots_endpoint_annotates_correction(client):
    test_client, _ = client
    _ingest(client)
    response = test_client.get(
        "/hotspots",
        params={
            "bandwidth_m": 750,
            "grid_size_m": 250,
            "alpha": 0.05,
            "correction": "fdr_bh",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "current"
    assert body["correction"] == "fdr_bh"
    assert body["alpha"] == 0.05
    assert body["n_events"] > 0
    assert any(cell["kind"] == "hotspot" for cell in body["cells"])
    assert all(
        "p_value_adj" in cell and "p_value_raw" in cell
        for cell in body["cells"]
    )


def test_window_query_excludes_events_outside_range(client):
    test_client, _ = client
    events = [
        {"event_id": "old", "lon": 0.0, "lat": 0.0, "ts": 10.0},
        {"event_id": "mid", "lon": 0.0, "lat": 0.0, "ts": 5000.0},
        {"event_id": "new", "lon": 0.0, "lat": 0.0, "ts": 9000.0},
    ]
    test_client.post("/events", json={"events": events})
    body = test_client.get(
        "/hotspots", params={"start": 3600, "end": 7200}
    ).json()
    assert body["n_events"] == 1


def test_clusters_endpoint(client):
    test_client, _ = client
    _ingest(client)
    response = test_client.get(
        "/clusters", params={"eps_m": 200, "min_samples": 10}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "current"
    assert len(body["clusters"]) >= 1
    assert body["clusters"][0]["size"] >= 10
    assert body["noise_count"] >= 0


def test_stale_data_then_refresh(client):
    test_client, _ = client
    _ingest(client)
    first = test_client.get(
        "/hotspots", params={"bandwidth_m": 750, "grid_size_m": 250}
    ).json()
    test_client.post(
        "/events",
        json={
            "events": [
                {"event_id": "extra", "lon": 0.001, "lat": 0.001, "ts": 1.0}
            ]
        },
    )
    stale = test_client.get(
        "/hotspots", params={"bandwidth_m": 750, "grid_size_m": 250}
    ).json()
    assert stale["status"] == "stale_data"
    assert stale["version"] == first["version"]

    fresh = test_client.get(
        "/hotspots",
        params={"bandwidth_m": 750, "grid_size_m": 250, "refresh": "true"},
    ).json()
    assert fresh["status"] == "current"
    assert fresh["n_events"] == first["n_events"] + 1


def test_admin_rebuild_endpoint_verifies_equality(client):
    test_client, _ = client
    _ingest(client)
    response = test_client.post("/admin/rebuild", params={"grid_size_m": 250})
    assert response.status_code == 200
    assert response.json()["verified_equal"] is True


def test_invalid_alpha_rejected(client):
    test_client, _ = client
    response = test_client.get("/hotspots", params={"alpha": 1.5})
    assert response.status_code == 422


def test_invalid_coordinates_rejected(client):
    test_client, _ = client
    response = test_client.post(
        "/events",
        json={"events": [{"event_id": "x", "lon": 200, "lat": 0, "ts": 1}]},
    )
    assert response.status_code == 422
