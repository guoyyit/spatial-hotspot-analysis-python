"""Getis-Ord Gi* hotspot statistic and multiple-comparison corrections."""

from __future__ import annotations

import math


def normal_cdf_two_sided_p(z: float) -> float:
    """Two-sided p-value of a standard-normal z score."""
    return math.erfc(abs(z) / math.sqrt(2.0))


def _neighbor_radius_cells(cell_size: float, bandwidth: float) -> int:
    return max(0, int(math.floor(bandwidth / cell_size + 1e-9)))


def gi_star_zscores(
    counts: dict[tuple[int, int], int],
    cell_size: float,
    bandwidth: float,
) -> dict[tuple[int, int], float]:
    """Getis-Ord Gi* z-scores with binary neighborhood weights (self included).

    Weights w_ij = 1 when the Chebyshev distance between cell centers is
    within ``bandwidth`` (i.e. |dx|<=r and |dy|<=r in cell units), else 0.
    The statistic follows the standard Gi* formulation (Getis & Ord 1992):

        z_i = (sum_j w_ij x_j - x_bar * sum_j w_ij)
              / (S * sqrt((n * sum_j w_ij^2 - (sum_j w_ij)^2) / (n - 1)))

    where x_bar and S are the mean and population std-dev of all cell
    counts. Cells with zero neighborhood variance contribution (n <= 1 or
    S == 0) receive z = 0.
    """
    cells = sorted(counts)
    n = len(cells)
    if n <= 1:
        return {cell: 0.0 for cell in cells}
    values = [counts[cell] for cell in cells]
    total = sum(values)
    mean = total / n
    var = sum(v * v for v in values) / n - mean * mean
    if var <= 0.0:
        return {cell: 0.0 for cell in cells}
    std = math.sqrt(var)
    radius = _neighbor_radius_cells(cell_size, bandwidth)
    result: dict[tuple[int, int], float] = {}
    for cx, cy in cells:
        weighted_sum = 0
        weight_sum = 0
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                neighbor = (cx + dx, cy + dy)
                value = counts.get(neighbor)
                if value is not None:
                    weighted_sum += value
                    weight_sum += 1
        # binary weights: sum w^2 == sum w == weight_sum
        denom = std * math.sqrt((n * weight_sum - weight_sum * weight_sum) / (n - 1))
        if denom <= 0.0:
            result[(cx, cy)] = 0.0
        else:
            result[(cx, cy)] = (weighted_sum - mean * weight_sum) / denom
    return result


def adjust_pvalues(pvalues: list[float], method: str) -> list[float]:
    """Multiple-comparison correction: none | bonferroni | fdr_bh.

    - none:        p_adjusted = p (no correction)
    - bonferroni:  p_adjusted = min(1, p * m), family-wise error rate
    - fdr_bh:      Benjamini-Hochberg step-up, false discovery rate
    """
    m = len(pvalues)
    if method == "none" or m == 0:
        return list(pvalues)
    if method == "bonferroni":
        return [min(1.0, p * m) for p in pvalues]
    if method == "fdr_bh":
        order = sorted(range(m), key=lambda i: pvalues[i])
        adjusted = [0.0] * m
        running = 1.0
        for rank in range(m, 0, -1):
            idx = order[rank - 1]
            running = min(running, pvalues[idx] * m / rank)
            adjusted[idx] = min(1.0, running)
        return adjusted
    raise ValueError(f"unknown correction method: {method}")
