"""API schemas (request/response models)."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class EventIn(BaseModel):
    event_id: str
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    ts: float  # unix epoch seconds
    attrs: dict[str, Any] = {}


class IngestRequest(BaseModel):
    events: list[EventIn]


class IngestResponse(BaseModel):
    accepted: int  # newly stored (deduplicated)
    duplicates: int  # already known event_ids
    version: int  # state version after this ingest


CorrectionName = Literal["none", "bonferroni", "fdr_bh"]


class HotspotQuery(BaseModel):
    start: Optional[float] = None  # window start, inclusive
    end: Optional[float] = None  # window end, exclusive
    alpha: Optional[float] = None
    bandwidth_deg: Optional[float] = None
    correction: Optional[CorrectionName] = None


class CellResult(BaseModel):
    cell_x: int
    cell_y: int
    count: int
    z_score: float
    p_value: float
    p_adjusted: float
    significant: bool
    correction: str
    label: str  # "hotspot" | "coldspot" | "not_significant"


class HotspotResponse(BaseModel):
    version: int
    window_start: Optional[float]
    window_end: Optional[float]
    alpha: float
    bandwidth_deg: float
    correction: str
    cells: list[CellResult]


class ClusterParams(BaseModel):
    eps_deg: Optional[float] = None
    min_samples: Optional[int] = None
    start: Optional[float] = None
    end: Optional[float] = None


class ClusterInfo(BaseModel):
    cluster_id: int
    size: int
    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float
    centroid_lat: float
    centroid_lon: float


class ClusterResponse(BaseModel):
    params_version: int
    stale: bool  # True if params changed since this result was computed
    window_start: Optional[float]
    window_end: Optional[float]
    eps_deg: float
    min_samples: int
    clusters: list[ClusterInfo]
    noise_points: int


class SummaryResponse(BaseModel):
    version: int
    total_events: int
    min_ts: Optional[float]
    max_ts: Optional[float]
    active_cells: int
    rebuilding: bool

