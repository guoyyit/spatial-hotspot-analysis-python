"""Shared pytest fixtures."""

from __future__ import annotations

import math
import os

import pytest

from spatial_hotspot.engine import Engine
from spatial_hotspot.models import Event
from spatial_hotspot.storage import StorageConfig


@pytest.fixture
def db_path(tmp_path: pytest.TempPathFactory) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def engine(db_path: str) -> Engine:
    eng = Engine(
        db_path,
        StorageConfig(origin_lon=0.0, origin_lat=0.0, bucket_width_s=3600.0),
    )
    yield eng
    eng.close()


def grid_events(
    n: int = 15,
    per_cell: int = 4,
    spacing_m: float = 250.0,
    ts: float = 3600.0,
    prefix: str = "b",
) -> list[Event]:
    """Uniform background events on an exact-metre aligned grid."""

    degree = math.degrees(spacing_m / 6_371_008.8)
    events: list[Event] = []
    k = 0
    for gx in range(n):
        for gy in range(n):
            for _ in range(per_cell):
                events.append(
                    Event(
                        event_id=f"{prefix}{k}",
                        lon=gx * degree,
                        lat=gy * degree,
                        ts=ts,
                    )
                )
                k += 1
    return events


def block_events(
    cells: tuple[int, ...] = (6, 7, 8),
    extra_per_cell: int = 30,
    spacing_m: float = 250.0,
    ts: float = 3600.0,
    prefix: str = "h",
) -> list[Event]:
    """Extra coincident events forming a dense spatial block."""

    degree = math.degrees(spacing_m / 6_371_008.8)
    events: list[Event] = []
    for bx in cells:
        for by in cells:
            for i in range(extra_per_cell):
                events.append(
                    Event(
                        event_id=f"{prefix}{bx}_{by}_{i}",
                        lon=bx * degree,
                        lat=by * degree,
                        ts=ts,
                    )
                )
    return events
