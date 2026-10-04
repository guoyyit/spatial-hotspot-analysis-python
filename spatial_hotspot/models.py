"""Core domain type definitions.

These frozen dataclasses are the shared vocabulary between storage, the
incremental engine, statistics, clustering and the HTTP layer.  Everything is
plainly serialisable so results can be cached to disk and replayed.
"""

from dataclasses import dataclass, field
from enum import Enum


class CorrectionMethod(str, Enum):
    """Multiple-comparison correction applied to raw p-values."""

    NONE = "none"
    BONFERRONI = "bonferroni"
    FDR_BH = "fdr_bh"


class KernelMethod(str, Enum):
    """Spatial neighbourhood kernel."""

    BINARY = "binary"
    GAUSSIAN = "gaussian"


@dataclass(frozen=True, slots=True)
class Event:
    """A single observed event with geographic coordinates and timestamp.

    ``event_id`` is the caller-supplied identity used for deduplication;
    re-posting the same id is idempotent and never changes stored state.
    ``ts`` is event time (POSIX seconds), which is independent of ingest
    time, so events may arrive out of order or late.
    """

    event_id: str
    lon: float
    lat: float
    ts: float


@dataclass(frozen=True, slots=True)
class HotspotParams:
    """Parameters governing a Getis-Ord Gi* hotspot scan."""

    alpha: float = 0.05
    bandwidth_m: float = 1000.0
    correction: CorrectionMethod = CorrectionMethod.FDR_BH
    kernel: KernelMethod = KernelMethod.BINARY
    grid_size_m: float = 250.0
    start: float | None = None
    end: float | None = None


@dataclass(frozen=True, slots=True)
class HotspotCell:
    """One tested grid location and its test outcome."""

    cell_x: int
    cell_y: int
    center_lon: float
    center_lat: float
    event_count: int
    z_score: float
    p_value_raw: float
    p_value_adj: float
    significant: bool
    kind: str  # "hotspot" | "coldspot" | "nonsignificant"


@dataclass(frozen=True, slots=True)
class HotspotResult:
    """Full result of a hotspot scan under one parameter set."""

    params: HotspotParams
    version: int
    generated_at: float
    n_events: int
    n_tests: int
    correction_applied: CorrectionMethod
    alpha: float
    cells: tuple[HotspotCell, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ClusteringParams:
    """Parameters for DBSCAN-based density clustering."""

    eps_m: float = 500.0
    min_samples: int = 5
    start: float | None = None
    end: float | None = None


@dataclass(frozen=True, slots=True)
class Cluster:
    """A extracted dense region: members, centre and bounding box."""

    cluster_id: int
    size: int
    center_lon: float
    center_lat: float
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float


@dataclass(frozen=True, slots=True)
class ClusteringResult:
    """Full result of a density-clustering run."""

    params: ClusteringParams
    version: int
    generated_at: float
    n_events: int
    clusters: tuple[Cluster, ...] = field(default_factory=tuple)
    noise_count: int = 0


@dataclass(frozen=True, slots=True)
class IngestReport:
    """Outcome of an ingest batch."""

    accepted: int
    duplicates: int
    version: int
