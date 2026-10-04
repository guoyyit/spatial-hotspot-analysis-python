"""Deterministic density clustering (DBSCAN) and region extraction.

Determinism contract: given identical parameters and input points the
result is identical on every run.  Inputs are normalised (deduped and
sorted by coordinate) before clustering, and cluster numbering follows the
first point that seeds each cluster in that order.
"""

from .models import Cluster


def dbscan(
    points: list[tuple[float, float]],
    eps: float,
    min_samples: int,
) -> list[int]:
    """Cluster planar points with DBSCAN.

    Returns a label per **input** point: non-negative cluster ids or ``-1``
    for noise.  ``eps`` is in metres, ``min_samples`` counts the point
    itself (so ``min_samples=5`` means a core point has 5 points within
    ``eps`` inclusive).
    """

    if eps <= 0:
        raise ValueError("eps must be positive")
    if min_samples < 1:
        raise ValueError("min_samples must be >= 1")

    # Coincident events retain multiplicity (it carries density signal);
    # determinism comes from stable sort order, not from deduplication.
    ordered = sorted(
        ((point[0], point[1], idx) for idx, point in enumerate(points)),
        key=lambda t: (t[0], t[1], t[2]),
    )
    coords = [(t[0], t[1]) for t in ordered]
    original_index = [t[2] for t in ordered]
    n = len(coords)

    neighbors: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        xi, yi = coords[i]
        for j in range(i, n):
            xj, yj = coords[j]
            dx = xi - xj
            dy = yi - yj
            if dx * dx + dy * dy <= eps * eps:
                neighbors[i].append(j)
                if i != j:
                    neighbors[j].append(i)
    for hood in neighbors:
        hood.sort()

    labels = [-1] * n
    visited = [False] * n
    cluster_id = -1

    for seed in range(n):
        if visited[seed]:
            continue
        visited[seed] = True
        if len(neighbors[seed]) < min_samples:
            continue
        cluster_id += 1
        labels[seed] = cluster_id
        queue = list(neighbors[seed])
        head = 0
        while head < len(queue):
            point_idx = queue[head]
            head += 1
            if not visited[point_idx]:
                visited[point_idx] = True
                if len(neighbors[point_idx]) >= min_samples:
                    for candidate in neighbors[point_idx]:
                        if candidate not in queue:
                            queue.append(candidate)
            if labels[point_idx] == -1:
                labels[point_idx] = cluster_id

    result = [-1] * n
    for ordered_idx, label in enumerate(labels):
        result[original_index[ordered_idx]] = label
    return result


def extract_clusters(
    points: list[tuple[float, float]],
    eps: float,
    min_samples: int,
) -> tuple[tuple[Cluster, ...], int]:
    """Run DBSCAN and describe each cluster as a :class:`Cluster`.

    Coordinates here are projected metres; the caller converts the centre
    and bounding box back to lon/lat.  Returns ``(clusters, noise_count)``
    ordered by descending size then cluster id.
    """

    point_labels = dbscan(points, eps, min_samples)
    members: dict[int, list[tuple[float, float]]] = {}
    noise = 0
    for point, label in zip(points, point_labels, strict=True):
        if label == -1:
            noise += 1
        else:
            members.setdefault(label, []).append(point)

    clusters: list[Cluster] = []
    for label in sorted(members, key=lambda k: (-len(members[k]), k)):
        member_points = sorted(members[label])
        size = len(member_points)
        mean_x = sum(p[0] for p in member_points) / size
        mean_y = sum(p[1] for p in member_points) / size
        clusters.append(
            Cluster(
                cluster_id=label,
                size=size,
                center_lon=mean_x,
                center_lat=mean_y,
                min_lon=min(p[0] for p in member_points),
                min_lat=min(p[1] for p in member_points),
                max_lon=max(p[0] for p in member_points),
                max_lat=max(p[1] for p in member_points),
            )
        )
    return tuple(clusters), noise
