"""HTTP API for event ingest, hotspots, clustering and administration.

Result lifecycle
----------------
Every query response carries a ``status`` field:

* ``current``      – computed at the latest committed data version with the
                     request's (active) parameters;
* ``stale_data``   – parameters still active but newer events have been
                     committed since this cached result was generated;
* ``stale_params`` – another parameter set has since been used, so this
                     result is marked invalid for current analysis.

Pass ``refresh=true`` to force a recomputation at the latest snapshot.
Each response also embeds the storage ``version`` it reflects, so callers
can tell exactly which committed data they observed.
"""

from __future__ import annotations

import os
from typing import Literal

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .engine import Engine
from .models import (
    ClusteringParams,
    CorrectionMethod,
    Event,
    HotspotParams,
    KernelMethod,
)
from .storage import StorageConfig

DEFAULT_DB_PATH = os.environ.get(
    "SPATIAL_DB_PATH", os.path.join(os.getcwd(), "spatial_hotspot.db")
)


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1)
    lon: float = Field(..., ge=-180, le=180)
    lat: float = Field(..., ge=-90, le=90)
    ts: float


class IngestBatchIn(BaseModel):
    events: list[EventIn]


class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    version: int


class HotspotCellOut(BaseModel):
    cell_x: int
    cell_y: int
    center_lon: float
    center_lat: float
    event_count: int
    z_score: float
    p_value_raw: float
    p_value_adj: float
    significant: bool
    kind: Literal["hotspot", "coldspot", "nonsignificant"]


class HotspotResponse(BaseModel):
    status: Literal["current", "stale_data", "stale_params"]
    version: int
    generated_at: float
    n_events: int
    n_tests: int
    alpha: float
    correction: str
    kernel: str
    bandwidth_m: float
    grid_size_m: float
    start: float | None
    end: float | None
    cells: list[HotspotCellOut]


class ClusterOut(BaseModel):
    cluster_id: int
    size: int
    center_lon: float
    center_lat: float
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float


class ClusteringResponse(BaseModel):
    status: Literal["current", "stale_data", "stale_params"]
    version: int
    generated_at: float
    n_events: int
    eps_m: float
    min_samples: int
    start: float | None
    end: float | None
    clusters: list[ClusterOut]
    noise_count: int


class StatusResponse(BaseModel):
    version: int
    total_events: int
    min_ts: float | None
    max_ts: float | None
    db_path: str


class RebuildResponse(BaseModel):
    version: int
    events: int
    verified_equal: bool


def _build_engine() -> Engine:
    db_path = os.environ.get("SPATIAL_DB_PATH", DEFAULT_DB_PATH)
    origin_lon = float(os.environ.get("SPATIAL_ORIGIN_LON", "0"))
    origin_lat = float(os.environ.get("SPATIAL_ORIGIN_LAT", "0"))
    bucket_width = float(os.environ.get("SPATIAL_BUCKET_WIDTH_S", "3600"))
    return Engine(
        db_path,
        StorageConfig(
            origin_lon=origin_lon,
            origin_lat=origin_lat,
            bucket_width_s=bucket_width,
        ),
    )


def create_app(engine: Engine | None = None) -> FastAPI:
    """Application factory.

    Tests inject a temporary-database engine; production builds one from
    environment variables lazily on first use.
    """

    app = FastAPI(title="spatial-hotspot-analysis", version="0.1.0")
    state: dict[str, Engine] = {}
    if engine is not None:
        state["engine"] = engine

    def get_engine() -> Engine:
        if "engine" not in state:
            state["engine"] = _build_engine()
        return state["engine"]

    @app.exception_handler(ValueError)
    async def value_error_handler(request, exc: ValueError):  # noqa: ANN001
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.post("/events", response_model=IngestResponse)
    def ingest_events(
        batch: IngestBatchIn,
        eng: Engine = Depends(get_engine),
    ) -> IngestResponse:
        events = [
            Event(event_id=e.event_id, lon=e.lon, lat=e.lat, ts=e.ts)
            for e in batch.events
        ]
        report = eng.ingest(events)
        return IngestResponse(
            accepted=report.accepted,
            duplicates=report.duplicates,
            version=report.version,
        )

    @app.get("/hotspots", response_model=HotspotResponse)
    def query_hotspots(
        start: float | None = None,
        end: float | None = None,
        alpha: float = 0.05,
        bandwidth_m: float = 1000.0,
        grid_size_m: float = 250.0,
        correction: CorrectionMethod = CorrectionMethod.FDR_BH,
        kernel: KernelMethod = KernelMethod.BINARY,
        refresh: bool = False,
        eng: Engine = Depends(get_engine),
    ) -> HotspotResponse:
        params = HotspotParams(
            alpha=alpha,
            bandwidth_m=bandwidth_m,
            correction=correction,
            kernel=kernel,
            grid_size_m=grid_size_m,
            start=start,
            end=end,
        )
        cached = eng.compute_hotspots(params, use_cache=not refresh)
        result = cached.result
        return HotspotResponse(
            status=cached.status,
            version=result.version,
            generated_at=result.generated_at,
            n_events=result.n_events,
            n_tests=result.n_tests,
            alpha=result.alpha,
            correction=result.correction_applied.value,
            kernel=params.kernel.value,
            bandwidth_m=params.bandwidth_m,
            grid_size_m=params.grid_size_m,
            start=params.start,
            end=params.end,
            cells=[
                HotspotCellOut(
                    cell_x=cell.cell_x,
                    cell_y=cell.cell_y,
                    center_lon=cell.center_lon,
                    center_lat=cell.center_lat,
                    event_count=cell.event_count,
                    z_score=cell.z_score,
                    p_value_raw=cell.p_value_raw,
                    p_value_adj=cell.p_value_adj,
                    significant=cell.significant,
                    kind=cell.kind,
                )
                for cell in result.cells
            ],
        )

    @app.get("/clusters", response_model=ClusteringResponse)
    def query_clusters(
        start: float | None = None,
        end: float | None = None,
        eps_m: float = 500.0,
        min_samples: int = 5,
        refresh: bool = False,
        eng: Engine = Depends(get_engine),
    ) -> ClusteringResponse:
        params = ClusteringParams(
            eps_m=eps_m,
            min_samples=min_samples,
            start=start,
            end=end,
        )
        cached = eng.compute_clusters(params, use_cache=not refresh)
        result = cached.result
        return ClusteringResponse(
            status=cached.status,
            version=result.version,
            generated_at=result.generated_at,
            n_events=result.n_events,
            eps_m=params.eps_m,
            min_samples=params.min_samples,
            start=params.start,
            end=params.end,
            clusters=[
                ClusterOut(
                    cluster_id=cluster.cluster_id,
                    size=cluster.size,
                    center_lon=cluster.center_lon,
                    center_lat=cluster.center_lat,
                    min_lon=cluster.min_lon,
                    min_lat=cluster.min_lat,
                    max_lon=cluster.max_lon,
                    max_lat=cluster.max_lat,
                )
                for cluster in result.clusters
            ],
            noise_count=result.noise_count,
        )

    @app.post("/admin/rebuild", response_model=RebuildResponse)
    def rebuild(
        grid_size_m: float = 250.0,
        eng: Engine = Depends(get_engine),
    ) -> RebuildResponse:
        outcome = eng.full_rebuild(grid_size_m=grid_size_m)
        return RebuildResponse(**outcome)

    @app.post("/admin/invalidate")
    def invalidate(eng: Engine = Depends(get_engine)) -> dict[str, str]:
        eng.invalidate_hotspots()
        eng.invalidate_clusters()
        return {"status": "invalidated"}

    @app.get("/status", response_model=StatusResponse)
    def status(eng: Engine = Depends(get_engine)) -> StatusResponse:
        lo, hi = eng.storage.event_time_range()
        return StatusResponse(
            version=eng.version(),
            total_events=eng.storage.total_events(),
            min_ts=lo,
            max_ts=hi,
            db_path=eng.storage.path,
        )

    return app


app = create_app()
