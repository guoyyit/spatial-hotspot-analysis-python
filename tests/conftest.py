from __future__ import annotations

import pytest

from app.config import Settings
from app.engine import Engine


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        db_path=str(tmp_path / "events.db"),
        cell_size_deg=0.01,
        bandwidth_deg=0.02,
        bucket_seconds=3600,
        max_buckets_in_memory=8,
    )


@pytest.fixture()
def engine(settings):
    eng = Engine(settings)
    yield eng
    eng.close()


def make_events(prefix: str, n: int, lat: float, lon: float, ts: float):
    return [
        (f"{prefix}-{i}", lat + 1e-6 * (i % 3), lon + 1e-6 * (i % 2), ts + i, "{}")
        for i in range(n)
    ]

