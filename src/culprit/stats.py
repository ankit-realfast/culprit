"""The test that decides whether a variant passes clearly more often than the baseline."""

from __future__ import annotations

from math import comb


def fisher_one_sided(variant_passes: int, variant_runs: int, baseline_passes: int, baseline_runs: int) -> float:
    """P(the variant passes at least this often | both pass at the same rate): the hypergeometric upper tail."""
    total_passes = variant_passes + baseline_passes
    total_runs = variant_runs + baseline_runs
    tail = sum(
        comb(total_passes, k) * comb(total_runs - total_passes, variant_runs - k)
        for k in range(variant_passes, min(total_passes, variant_runs) + 1)
    )
    return tail / comb(total_runs, variant_runs)
