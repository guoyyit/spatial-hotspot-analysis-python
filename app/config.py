"""Service configuration: statistical and resource parameters."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Immutable service settings.

    All spatial statistics are computed on a regular grid whose cell size is
    ``cell_size_deg`` degrees. Hotspot neighborhood weights are binary within
    ``bandwidth_deg``. Time is partitioned into buckets of
    ``bucket_seconds``; at most ``max_buckets_in_memory`` buckets are kept
    resident (bounded memory), older buckets are reloaded from SQLite on
    demand.
    """

    db_path: str = "data/events.db"
    cell_size_deg: float = 0.01
    bandwidth_deg: float = 0.05
    alpha: float = 0.05
    correction: str = "fdr_bh"  # none | bonferroni | fdr_bh
    bucket_seconds: int = 3600
    max_buckets_in_memory: int = 256
    cluster_eps_deg: float = 0.05
    cluster_min_samples: int = 5
    cluster_max_points: int = 100_000

