"""Significance tests, multiple-comparison correction and the Gi* scan.

The scan runs on an origin-anchored planar lattice: each lattice cell is a
feature whose attribute is its event count.  The study region is the tight
cell-aligned bounding box of the observed cells, so every statistical
quantity is a pure function of the data and never of ingest order.
"""

import math
from dataclasses import dataclass

from .models import CorrectionMethod, KernelMethod
from .weights import cell_center, weight_for


@dataclass(frozen=True, slots=True)
class ScanCell:
    cell_x: int
    cell_y: int
    center_x: float
    center_y: float
    event_count: int
    z_score: float
    p_value_raw: float
    p_value_adj: float
    significant: bool
    kind: str


def normal_two_sided_p(z: float) -> float:
    """Two-sided p-value of a standard normal at observed score ``z``."""

    return math.erfc(abs(z) / math.sqrt(2.0))


def adjust_pvalues(
    raw_pvalues: list[float],
    method: CorrectionMethod,
) -> list[float]:
    """Adjust p-values for multiple comparisons.

    Supported methods:

    * ``none`` — identity;
    * ``bonferroni`` — ``min(1, p * m)``;
    * ``fdr_bh`` — Benjamini-Hochberg step-up false-discovery-rate control,
      which is monotone and invariant to the input order.

    The output is aligned positionally with ``raw_pvalues``.
    """

    m = len(raw_pvalues)
    if m == 0:
        return []
    if method == CorrectionMethod.NONE:
        return list(raw_pvalues)
    if method == CorrectionMethod.BONFERRONI:
        return [min(1.0, p * m) for p in raw_pvalues]

    # Benjamini-Hochberg step-up.
    order = sorted(range(m), key=lambda i: (raw_pvalues[i], i))
    adjusted_sorted: list[float] = [0.0] * m
    running_min = 1.0
    for rank in range(m - 1, -1, -1):
        idx = order[rank]
        value = min(1.0, raw_pvalues[idx] * m / (rank + 1))
        running_min = min(running_min, value)
        adjusted_sorted[rank] = running_min
    adjusted = [0.0] * m
    for rank, idx in enumerate(order):
        adjusted[idx] = adjusted_sorted[rank]
    return adjusted


def getis_ord_scan(
    cell_counts: dict[tuple[int, int], int],
    *,
    grid_size_m: float,
    bandwidth_m: float,
    kernel: KernelMethod,
    correction: CorrectionMethod,
    alpha: float,
) -> tuple[int, int, list[ScanCell]]:
    """Run a Getis-Ord Gi* scan over populated lattice cells.

    Returns ``(n_events, n_tests, cells)`` covering every cell in the tight
    bounding region (including zero-count cells, which are tested too).
    Weights are truncated at ``bandwidth_m`` for every kernel, so each cell's
    neighbourhood is a finite ring of lattice offsets.
    """

    if not cell_counts:
        return 0, 0, []

    min_cx = min(cx for cx, _ in cell_counts)
    max_cx = max(cx for cx, _ in cell_counts)
    min_cy = min(cy for _, cy in cell_counts)
    max_cy = max(cy for _, cy in cell_counts)

    n_events = sum(cell_counts.values())
    n_region = (max_cx - min_cx + 1) * (max_cy - min_cy + 1)
    mean = n_events / n_region
    sum_sq = sum(count * count for count in cell_counts.values())
    variance = sum_sq / n_region - mean * mean

    ring = max(0, math.ceil(bandwidth_m / grid_size_m))

    raw: list[ScanCell] = []
    raw_p: list[float] = []

    for cx in range(min_cx, max_cx + 1):
        for cy in range(min_cy, max_cy + 1):
            center_x, center_y = cell_center(cx, cy, grid_size_m)

            weight_sum = 0.0
            weight_sq_sum = 0.0
            weighted_count = 0.0

            for dx in range(-ring, ring + 1):
                nx = cx + dx
                if nx < min_cx or nx > max_cx:
                    continue
                for dy in range(-ring, ring + 1):
                    ny = cy + dy
                    if ny < min_cy or ny > max_cy:
                        continue
                    nx_center, ny_center = cell_center(nx, ny, grid_size_m)
                    distance = math.hypot(
                        center_x - nx_center, center_y - ny_center
                    )
                    w = weight_for(kernel, distance, bandwidth_m)
                    if w == 0.0:
                        continue
                    weight_sum += w
                    weight_sq_sum += w * w
                    weighted_count += w * cell_counts.get((nx, ny), 0)

            count = cell_counts.get((cx, cy), 0)
            if n_region > 1 and variance > 0.0:
                denom_var = (
                    (n_region * weight_sq_sum - weight_sum * weight_sum)
                    / (n_region - 1)
                    * variance
                )
                z = 0.0 if denom_var <= 0.0 else (
                    weighted_count - mean * weight_sum
                ) / math.sqrt(denom_var)
            else:
                z = 0.0
            p = normal_two_sided_p(z)
            raw_p.append(p)
            raw.append(
                ScanCell(
                    cell_x=cx,
                    cell_y=cy,
                    center_x=center_x,
                    center_y=center_y,
                    event_count=count,
                    z_score=z,
                    p_value_raw=p,
                    p_value_adj=0.0,
                    significant=False,
                    kind="nonsignificant",
                )
            )

    adjusted = adjust_pvalues(raw_p, correction)
    cells: list[ScanCell] = []
    for cell, p_adj in zip(raw, adjusted, strict=True):
        significant = p_adj < alpha
        if significant and cell.z_score > 0:
            kind = "hotspot"
        elif significant and cell.z_score < 0:
            kind = "coldspot"
        else:
            kind = "nonsignificant"
        cells.append(
            ScanCell(
                cell_x=cell.cell_x,
                cell_y=cell.cell_y,
                center_x=cell.center_x,
                center_y=cell.center_y,
                event_count=cell.event_count,
                z_score=cell.z_score,
                p_value_raw=cell.p_value_raw,
                p_value_adj=p_adj,
                significant=significant,
                kind=kind,
            )
        )

    cells.sort(key=lambda c: (c.cell_x, c.cell_y))
    return n_events, n_region, cells
