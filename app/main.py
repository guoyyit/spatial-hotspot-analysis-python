"""FastAPI application exposing the analysis service."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .cluster import dbscan
from .config import Settings
from .engine import Engine
from .models import (
    CellResult,
    ClusterInfo,
    ClusterParams,
    ClusterResponse,
    HotspotResponse,
    IngestRequest,
    IngestResponse,
    SummaryResponse,
)
from .stats import adjust_pvalues, gi_star_zscores, normal_cdf_two_sided_p


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings(
        db_path=os.environ.get("SHP_DB_PATH", "data/events.db")
    )
    engine = Engine(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        engine.close()

    app = FastAPI(title="spatial-hotspot-analysis", lifespan=lifespan)
    app.state.engine = engine
    app.state.settings = settings

    @app.post("/events", response_model=IngestResponse)
    def ingest_events(req: IngestRequest) -> IngestResponse:
        rows = [
            (e.event_id, e.lat, e.lon, e.ts, json.dumps(e.attrs)) for e in req.events
        ]
        accepted, duplicates, version = engine.ingest(rows)
        return IngestResponse(accepted=accepted, duplicates=duplicates, version=version)

    @app.get("/stats/summary", response_model=SummaryResponse)
    def summary() -> SummaryResponse:
        snap = engine.snapshot()
        return SummaryResponse(
            version=snap.version,
            total_events=snap.total_events,
            min_ts=snap.min_ts,
            max_ts=snap.max_ts,
            active_cells=len(snap.global_grid.counts),
            rebuilding=engine.rebuilding,
        )

    @app.get("/hotspots", response_model=HotspotResponse)
    def hotspots(
        start: float | None = None,
        end: float | None = None,
        alpha: float | None = None,
        bandwidth_deg: float | None = None,
        correction: str | None = None,
    ) -> HotspotResponse:
        snap = engine.snapshot()
        alpha = settings.alpha if alpha is None else alpha
        bandwidth = settings.bandwidth_deg if bandwidth_deg is None else bandwidth_deg
        correction = settings.correction if correction is None else correction
        if start is None and end is None:
            grid = snap.global_grid
        else:
            lo = float("-inf") if start is None else start
            hi = float("inf") if end is None else end
            grid = snap.buckets.aggregate(lo, hi)
        zscores = gi_star_zscores(grid.counts, settings.cell_size_deg, bandwidth)
        cells = sorted(grid.counts)
        pvalues = [normal_cdf_two_sided_p(zscores[c]) for c in cells]
        adjusted = adjust_pvalues(pvalues, correction)
        results = []
        for cell, p_raw, p_adj in zip(cells, pvalues, adjusted):
            z = zscores[cell]
            significant = p_adj < alpha
            if significant and z > 0:
                label = "hotspot"
            elif significant and z < 0:
                label = "coldspot"
            else:
                label = "not_significant"
            results.append(
                CellResult(
                    cell_x=cell[0],
                    cell_y=cell[1],
                    count=grid.counts[cell],
                    z_score=z,
                    p_value=p_raw,
                    p_adjusted=p_adj,
                    significant=significant,
                    correction=correction,
                    label=label,
                )
            )
        return HotspotResponse(
            version=snap.version,
            window_start=start,
            window_end=end,
            alpha=alpha,
            bandwidth_deg=bandwidth,
            correction=correction,
            cells=results,
        )

    @app.post("/analysis/rebuild")
    def rebuild() -> dict:
        version = engine.rebuild()
        return {"version": version, "rebuilding": engine.rebuilding}

    @app.post("/clusters/compute", response_model=ClusterResponse)
    def compute_clusters(params: ClusterParams) -> ClusterResponse:
        eps = params.eps_deg or settings.cluster_eps_deg
        min_samples = params.min_samples or settings.cluster_min_samples
        rows = engine.store.events_in_window(params.start, params.end)
        rows = rows[: settings.cluster_max_points]  # bounded resource
        points = [(lat, lon) for _eid, lat, lon, _ts in rows]
        labels = dbscan(points, eps, min_samples)
        clusters: dict[int, list[int]] = {}
        noise = 0
        for idx, label in enumerate(labels):
            if label == -1:
                noise += 1
            else:
                clusters.setdefault(label, []).append(idx)
        # deterministic cluster ids: order by descending size, then min lat/lon
        ordered = sorted(
            clusters.values(),
            key=lambda idxs: (
                -len(idxs),
                min(points[i][0] for i in idxs),
                min(points[i][1] for i in idxs),
            ),
        )
        infos = []
        for cid, idxs in enumerate(ordered):
            lats = [points[i][0] for i in idxs]
            lons = [points[i][1] for i in idxs]
            infos.append(
                ClusterInfo(
                    cluster_id=cid,
                    size=len(idxs),
                    min_lat=min(lats),
                    max_lat=max(lats),
                    min_lon=min(lons),
                    max_lon=max(lons),
                    centroid_lat=sum(lats) / len(lats),
                    centroid_lon=sum(lons) / len(lons),
                )
            )
        result = ClusterResponse(
            params_version=engine.snapshot().version,
            stale=False,
            window_start=params.start,
            window_end=params.end,
            eps_deg=eps,
            min_samples=min_samples,
            clusters=infos,
            noise_points=noise,
        )
        engine.set_cluster_result((eps, min_samples), result.model_dump())
        return result

    @app.get("/clusters", response_model=ClusterResponse)
    def get_clusters(
        eps_deg: float | None = None, min_samples: int | None = None
    ) -> ClusterResponse:
        if eps_deg is not None or min_samples is not None:
            engine.invalidate_clusters(
                (
                    eps_deg or settings.cluster_eps_deg,
                    min_samples or settings.cluster_min_samples,
                )
            )
        result, stale = engine.get_cluster_result()
        if result is None:
            return ClusterResponse(
                params_version=engine.snapshot().version,
                stale=False,
                window_start=None,
                window_end=None,
                eps_deg=eps_deg or settings.cluster_eps_deg,
                min_samples=min_samples or settings.cluster_min_samples,
                clusters=[],
                noise_points=0,
            )
        result = dict(result)
        result["stale"] = stale
        return ClusterResponse(**result)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": engine.snapshot().version}

    return app
