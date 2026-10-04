"""Deterministic DBSCAN density clustering over event points."""

from __future__ import annotations

import math
from collections import deque


def dbscan(
    points: list[tuple[float, float]],
    eps: float,
    min_samples: int,
) -> list[int]:
    """Return cluster labels (-1 = noise). Deterministic for sorted input.

    Points are visited in input order and neighbors are expanded in
    increasing index order, so the same (points, eps, min_samples) always
    yields identical labels. Neighbor lookup uses a uniform grid, keeping
    the run near O(n) for typical densities.
    """
    n = len(points)
    labels = [-1] * n
    if n == 0:
        return labels
    cell = eps if eps > 0 else 1e-9
    grid: dict[tuple[int, int], list[int]] = {}
    for idx, (lat, lon) in enumerate(points):
        key = (math.floor(lat / cell), math.floor(lon / cell))
        grid.setdefault(key, []).append(idx)

    def region_query(idx: int) -> list[int]:
        lat, lon = points[idx]
        center = (math.floor(lat / cell), math.floor(lon / cell))
        found: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for other in grid.get((center[0] + dx, center[1] + dy), ()):
                    olat, olon = points[other]
                    if abs(olat - lat) <= eps and abs(olon - lon) <= eps:
                        if math.hypot(olat - lat, olon - lon) <= eps:
                            found.append(other)
        found.sort()
        return found

    cluster_id = 0
    for start in range(n):
        if labels[start] != -1:
            continue
        neighbors = region_query(start)
        if len(neighbors) < min_samples:
            continue  # stays noise unless reached from a core point later
        labels[start] = cluster_id
        queue = deque(neighbors)
        while queue:
            current = queue.popleft()
            if labels[current] == -1:
                labels[current] = cluster_id
                current_neighbors = region_query(current)
                if len(current_neighbors) >= min_samples:
                    for nxt in current_neighbors:
                        if labels[nxt] == -1:
                            queue.append(nxt)
        cluster_id += 1
    return labels
