"""Spatial geometry: projection, neighbourhood weights and grid indexing.

All distance computations happen on a local equirectangular projection in
metres anchored at a configurable origin (``(origin_lon, origin_lat)``).
Around the origin the projection is accurate to second order, i.e. suitable
for city-scale hotspot analysis where the bandwidth is small relative to the
Earth.  The default origin ``(0, 0)`` is exact only near the equator/prime
meridian; deployments should anchor the origin to their study area.

The grid is anchored at the projection origin and therefore does **not**
shift when late or out-of-order events extend the data extent — grid cell
indices are stable across incremental updates and full rebuilds.
"""

import math

from .models import KernelMethod

EARTH_RADIUS_M = 6_371_008.8


def project(
    lon: float,
    lat: float,
    origin_lon: float = 0.0,
    origin_lat: float = 0.0,
) -> tuple[float, float]:
    """Project lon/lat to local planar metres ``(x, y)`` around the origin."""

    lat0 = math.radians(origin_lat)
    x = EARTH_RADIUS_M * math.radians(lon - origin_lon) * math.cos(lat0)
    y = EARTH_RADIUS_M * math.radians(lat - origin_lat)
    return x, y


def unproject(
    x: float,
    y: float,
    origin_lon: float = 0.0,
    origin_lat: float = 0.0,
) -> tuple[float, float]:
    """Inverse of :func:`project`."""

    lat0 = math.radians(origin_lat)
    lon = origin_lon + math.degrees(x / (EARTH_RADIUS_M * math.cos(lat0)))
    lat = origin_lat + math.degrees(y / EARTH_RADIUS_M)
    return lon, lat


def binary_weight(distance: float, bandwidth: float) -> float:
    """1 inside (and on) the bandwidth disc, 0 outside.  Includes self."""

    return 1.0 if distance <= bandwidth else 0.0


def gaussian_weight(distance: float, bandwidth: float) -> float:
    """Gaussian kernel ``exp(-(d/bw)^2 / 2)`` truncated at the bandwidth."""

    if distance > bandwidth:
        return 0.0
    return math.exp(-0.5 * (distance / bandwidth) ** 2)


def weight_for(kernel: KernelMethod, distance: float, bandwidth: float) -> float:
    if kernel == KernelMethod.BINARY:
        return binary_weight(distance, bandwidth)
    return gaussian_weight(distance, bandwidth)


def cell_index(x: float, y: float, grid_size_m: float) -> tuple[int, int]:
    """Stable integer grid cell containing projected point ``(x, y)``."""

    return math.floor(x / grid_size_m), math.floor(y / grid_size_m)


def cell_center(
    cx: int,
    cy: int,
    grid_size_m: float,
) -> tuple[float, float]:
    """Projected-metre centre of grid cell ``(cx, cy)``."""

    return (cx + 0.5) * grid_size_m, (cy + 0.5) * grid_size_m
