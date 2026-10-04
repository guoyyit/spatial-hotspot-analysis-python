"""Bounded in-memory spatial grid and time-bucket indexes."""

from __future__ import annotations

import math
from collections import OrderedDict


def cell_of(lat: float, lon: float, cell_size: float) -> tuple[int, int]:
    """Map a coordinate to its grid cell indices."""
    return (math.floor(lat / cell_size), math.floor(lon / cell_size))


class GridIndex:
    """Sparse count grid: only non-empty cells consume memory."""

    def __init__(self, cell_size: float) -> None:
        self.cell_size = cell_size
        self.counts: dict[tuple[int, int], int] = {}
        self._total = 0

    def add(self, lat: float, lon: float, n: int = 1) -> None:
        cell = cell_of(lat, lon, self.cell_size)
        self.counts[cell] = self.counts.get(cell, 0) + n
        self._total += n

    def items(self) -> list[tuple[tuple[int, int], int]]:
        return sorted(self.counts.items())

    def total(self) -> int:
        return self._total

    def copy(self) -> "GridIndex":
        clone = GridIndex(self.cell_size)
        clone.counts = dict(self.counts)
        clone._total = self._total
        return clone


class TimeBucketIndex:
    """Bucketed grids keyed by bucket start; LRU-bounded in memory.

    Evicted buckets are dropped from memory only; they can be rebuilt from
    the durable store, so memory stays bounded regardless of event volume.
    """

    def __init__(self, cell_size: float, bucket_seconds: int, max_buckets: int) -> None:
        self.cell_size = cell_size
        self.bucket_seconds = bucket_seconds
        self.max_buckets = max_buckets
        self._buckets: OrderedDict[int, GridIndex] = OrderedDict()

    def _bucket_start(self, ts: float) -> int:
        return int(math.floor(ts / self.bucket_seconds)) * self.bucket_seconds

    def add(self, lat: float, lon: float, ts: float) -> None:
        key = self._bucket_start(ts)
        grid = self._buckets.get(key)
        if grid is None:
            grid = GridIndex(self.cell_size)
            self._buckets[key] = grid
        else:
            self._buckets.move_to_end(key)
        grid.add(lat, lon)
        while len(self._buckets) > self.max_buckets:
            self._buckets.popitem(last=False)

    def bucket_range(self, start: float, end: float) -> list[int]:
        """Bucket starts overlapping the half-open window [start, end)."""
        if end <= start:
            return []
        first = self._bucket_start(start)
        last = self._bucket_start(math.nextafter(end, -math.inf))
        return list(range(first, last + self.bucket_seconds, self.bucket_seconds))

    def aggregate(self, start: float, end: float) -> GridIndex:
        """Aggregate resident buckets overlapping [start, end).

        Buckets are whole-granularity aggregates; window boundary semantics
        (bucket-level approximation) are documented in README.
        """
        merged = GridIndex(self.cell_size)
        for key in self.bucket_range(start, end):
            grid = self._buckets.get(key)
            if grid is None:
                continue
            self._buckets.move_to_end(key)
            for cell, n in grid.counts.items():
                merged.counts[cell] = merged.counts.get(cell, 0) + n
            merged._total += grid._total
        return merged

    def copy(self) -> "TimeBucketIndex":
        clone = TimeBucketIndex(self.cell_size, self.bucket_seconds, self.max_buckets)
        for key, grid in self._buckets.items():
            clone._buckets[key] = grid.copy()
        return clone

    def resident_buckets(self) -> int:
        return len(self._buckets)
