"""Incremental analysis engine.

The engine is the only component that turns stored events into analytical
results.  It guarantees:

* **Incremental == full rebuild.**  Ingest updates a per-bucket summary
  transactionally; :meth:`full_rebuild` recomputes that summary from raw
  events and asserts both views produce identical cell counts.  Every result
  records the storage ``version`` it was computed at.
* **Snapshot reads.**  Each result is computed from one storage snapshot
  (one read connection).  SQLite WAL readers are never blocked by writers.
* **Staleness marking.**  Cached results are returned with a lifecycle
  label: ``current`` (parameters and data unchanged), ``stale_data``
  (newer events committed) or ``stale_params`` (previously seen parameters
  were superseded).  Changing parameters never overwrites an old result;
  the old one is explicitly marked invalid.
"""

import threading
import time
from dataclasses import dataclass

from .clustering import dbscan
from .models import (
    Cluster,
    ClusteringParams,
    ClusteringResult,
    CorrectionMethod,
    Event,
    HotspotCell,
    HotspotParams,
    HotspotResult,
    IngestReport,
)
from .stats import getis_ord_scan
from .storage import Storage, StorageConfig
from .weights import unproject
from .windows import parse_window


@dataclass(frozen=True, slots=True)
class CachedHotspot:
    result: HotspotResult
    status: str  # current | stale_data | stale_params


@dataclass(frozen=True, slots=True)
class CachedClustering:
    result: ClusteringResult
    status: str


class Engine:
    def __init__(
        self,
        db_path: str,
        storage_config: StorageConfig | None = None,
    ) -> None:
        self.storage = Storage(db_path, storage_config)
        self._lock = threading.RLock()
        self._hotspot_cache: dict[HotspotParams, HotspotResult] = {}
        self._active_hotspot_params: HotspotParams | None = None
        self._clustering_cache: dict[ClusteringParams, ClusteringResult] = {}
        self._active_clustering_params: ClusteringParams | None = None

    def close(self) -> None:
        with self._lock:
            self.storage.close()

    # -- ingest ----------------------------------------------------------

    def ingest(self, events: list[Event]) -> IngestReport:
        accepted, duplicates, version = self.storage.insert_events(events)
        return IngestReport(
            accepted=accepted, duplicates=duplicates, version=version
        )

    def version(self) -> int:
        return self.storage.current_version()

    # -- hotspots --------------------------------------------------------

    @staticmethod
    def _canonical_hotspot(params: HotspotParams) -> HotspotParams:
        parse_window(params.start, params.end)
        if params.alpha <= 0 or params.alpha >= 1:
            raise ValueError("alpha must lie in (0, 1)")
        if params.bandwidth_m <= 0 or params.grid_size_m <= 0:
            raise ValueError("bandwidth_m and grid_size_m must be positive")
        correction = (
            params.correction
            if isinstance(params.correction, CorrectionMethod)
            else CorrectionMethod(params.correction)
        )
        return HotspotParams(
            alpha=float(params.alpha),
            bandwidth_m=float(params.bandwidth_m),
            correction=correction,
            kernel=params.kernel,
            grid_size_m=float(params.grid_size_m),
            start=params.start,
            end=params.end,
        )

    def compute_hotspots(
        self,
        params: HotspotParams,
        *,
        use_cache: bool = True,
    ) -> CachedHotspot:
        params = self._canonical_hotspot(params)
        with self._lock:
            if use_cache and params in self._hotspot_cache:
                cached = self._hotspot_cache[params]
                status = self._hotspot_status(params, cached)
                return CachedHotspot(cached, status)

            version, window_total, counts = self.storage.snapshot_cell_counts(
                params.start, params.end, params.grid_size_m
            )
            n_events, n_tests, scan_cells = getis_ord_scan(
                counts,
                grid_size_m=params.grid_size_m,
                bandwidth_m=params.bandwidth_m,
                kernel=params.kernel,
                correction=params.correction,
                alpha=params.alpha,
            )
            assert n_events == window_total
            origin = self.storage.config
            cells = tuple(
                HotspotCell(
                    cell_x=cell.cell_x,
                    cell_y=cell.cell_y,
                    center_lon=unproject(
                        cell.center_x,
                        cell.center_y,
                        origin.origin_lon,
                        origin.origin_lat,
                    )[0],
                    center_lat=unproject(
                        cell.center_x,
                        cell.center_y,
                        origin.origin_lon,
                        origin.origin_lat,
                    )[1],
                    event_count=cell.event_count,
                    z_score=cell.z_score,
                    p_value_raw=cell.p_value_raw,
                    p_value_adj=cell.p_value_adj,
                    significant=cell.significant,
                    kind=cell.kind,
                )
                for cell in scan_cells
            )
            result = HotspotResult(
                params=params,
                version=version,
                generated_at=time.time(),
                n_events=n_events,
                n_tests=n_tests,
                correction_applied=params.correction,
                alpha=params.alpha,
                cells=cells,
            )
            self._hotspot_cache[params] = result
            self._active_hotspot_params = params
            return CachedHotspot(result, "current")

    def _hotspot_status(
        self, params: HotspotParams, result: HotspotResult
    ) -> str:
        if self._active_hotspot_params != params:
            return "stale_params"
        if result.version != self.storage.current_version():
            return "stale_data"
        return "current"

    def invalidate_hotspots(self) -> None:
        """Drop cached hotspot results (e.g. administrative cleanup)."""

        with self._lock:
            self._hotspot_cache.clear()
            self._active_hotspot_params = None

    # -- clustering ------------------------------------------------------

    @staticmethod
    def _canonical_clustering(params: ClusteringParams) -> ClusteringParams:
        parse_window(params.start, params.end)
        if params.eps_m <= 0:
            raise ValueError("eps_m must be positive")
        if params.min_samples < 1:
            raise ValueError("min_samples must be >= 1")
        return ClusteringParams(
            eps_m=float(params.eps_m),
            min_samples=int(params.min_samples),
            start=params.start,
            end=params.end,
        )

    def compute_clusters(
        self,
        params: ClusteringParams,
        *,
        use_cache: bool = True,
    ) -> CachedClustering:
        params = self._canonical_clustering(params)
        with self._lock:
            if use_cache and params in self._clustering_cache:
                cached = self._clustering_cache[params]
                status = self._clustering_status(params, cached)
                return CachedClustering(cached, status)

            version, points = self.storage.snapshot_events(
                params.start, params.end
            )
            labels = dbscan(points, params.eps_m, params.min_samples)
            origin = self.storage.config

            grouped: dict[int, list[tuple[float, float]]] = {}
            noise = 0
            for point, label in zip(points, labels, strict=True):
                if label == -1:
                    noise += 1
                else:
                    grouped.setdefault(label, []).append(point)

            ordered_labels = sorted(
                grouped, key=lambda k: (-len(grouped[k]), k)
            )
            clusters: list[Cluster] = []
            for rank, label in enumerate(ordered_labels):
                members = grouped[label]
                size = len(members)
                mean_x = sum(p[0] for p in members) / size
                mean_y = sum(p[1] for p in members) / size
                c_lon, c_lat = unproject(
                    mean_x,
                    mean_y,
                    origin.origin_lon,
                    origin.origin_lat,
                )
                min_x = min(p[0] for p in members)
                min_y = min(p[1] for p in members)
                max_x = max(p[0] for p in members)
                max_y = max(p[1] for p in members)
                b_lo_lon, b_lo_lat = unproject(
                    min_x, min_y, origin.origin_lon, origin.origin_lat
                )
                b_hi_lon, b_hi_lat = unproject(
                    max_x, max_y, origin.origin_lon, origin.origin_lat
                )
                clusters.append(
                    Cluster(
                        cluster_id=rank,
                        size=size,
                        center_lon=c_lon,
                        center_lat=c_lat,
                        min_lon=b_lo_lon,
                        min_lat=b_lo_lat,
                        max_lon=b_hi_lon,
                        max_lat=b_hi_lat,
                    )
                )

            result = ClusteringResult(
                params=params,
                version=version,
                generated_at=time.time(),
                n_events=len(points),
                clusters=tuple(clusters),
                noise_count=noise,
            )
            self._clustering_cache[params] = result
            self._active_clustering_params = params
            return CachedClustering(result, "current")

    def _clustering_status(
        self, params: ClusteringParams, result: ClusteringResult
    ) -> str:
        if self._active_clustering_params != params:
            return "stale_params"
        if result.version != self.storage.current_version():
            return "stale_data"
        return "current"

    def invalidate_clusters(self) -> None:
        with self._lock:
            self._clustering_cache.clear()
            self._active_clustering_params = None

    # -- rebuild verification -------------------------------------------

    def full_rebuild(self, grid_size_m: float | None = None) -> dict:
        """Rebuild derived aggregates from raw events and verify equality.

        Concurrent ingests are serialised against the rebuild at the
        storage lock; if a batch lands first the rebuild retries with the
        new target version.  After the swap, window cell counts are
        recomputed and compared with a pre-rebuild snapshot taken at the
        same version — they must match exactly.
        """

        with self._lock:
            while True:
                target = self.storage.current_version()
                grid = grid_size_m or 250.0
                before = self.storage.cell_counts_for_window(
                    None, None, grid
                )
                if self.storage.rebuild_bucket_stats(target):
                    after = self.storage.cell_counts_for_window(
                        None, None, grid
                    )
                    if before != after:
                        raise RuntimeError(
                            "full rebuild diverged from incremental state"
                        )
                    return {
                        "version": target,
                        "events": self.storage.total_events(),
                        "verified_equal": True,
                    }
