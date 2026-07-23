"""stats.py, the three honest numbers §13 requires: pass-rate CI, ICC, McNemar.

Stdlib only. Wilson (not Wald) so small-n bounds stay inside [0, 1]; McNemar's
EXACT binomial form so tiny paired A/Bs stay valid; ICC(1) one-way ANOVA so
seed-to-seed reliability is a number, not a vibe.
"""
from __future__ import annotations

import math

_Z95 = 1.959963984540054  # two-sided 95%


def wilson_ci(successes: int, trials: int, z: float = _Z95) -> "tuple[float, float]":
    """95% Wilson score interval for a binomial proportion."""
    if trials <= 0:
        return (0.0, 1.0)
    n, p = float(trials), successes / trials
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    lo = 0.0 if successes == 0 else max(0.0, center - half)  # exact at the endpoints
    hi = 1.0 if successes == trials else min(1.0, center + half)
    return (lo, hi)


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant-pair counts.

    ``b`` = pairs the baseline passed and the treatment failed; ``c`` = the
    reverse. Conditional on n = b + c, the smaller count is Binomial(n, 1/2)
    under H0; no discordant pairs means the data cannot speak (p = 1).
    """
    n = b + c
    if n == 0:
        return 1.0
    m = min(b, c)
    tail = sum(math.comb(n, i) for i in range(m + 1)) * 0.5 ** n
    return min(1.0, 2.0 * tail)


def icc1(rows: "list[list[int | float]]") -> "float | None":
    """ICC(1), one-way random effects: rows = cases, columns = seeds (equal n).

    Returns None when fewer than 2 cases or 2 seeds (undefined). Zero variance
    everywhere is perfect consistency → 1.0.
    """
    k = len(rows)
    if k < 2 or any(len(r) != len(rows[0]) for r in rows):
        return None
    n = len(rows[0])
    if n < 2:
        return None
    means = [sum(r) / n for r in rows]
    grand = sum(means) / k
    msb = n * sum((m - grand) ** 2 for m in means) / (k - 1)
    msw = sum((x - means[i]) ** 2 for i, r in enumerate(rows) for x in r) / (k * (n - 1))
    denom = msb + (n - 1) * msw
    if denom == 0.0:
        return 1.0  # no variance between or within, trivially consistent
    return (msb - msw) / denom
