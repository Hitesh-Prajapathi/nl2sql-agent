"""Small statistics helpers (stdlib only): percentiles and percentile-bootstrap CIs."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, q in [0, 100]. None for an empty sample."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def bootstrap_ci(
    values: Sequence[float],
    *,
    n_resamples: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
) -> tuple[float, float] | None:
    """Percentile bootstrap CI of the mean. None for an empty sample."""
    n = len(values)
    if n == 0:
        return None
    rng = random.Random(seed)  # noqa: S311 - reproducible resampling, not crypto
    means = []
    for _ in range(n_resamples):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    alpha = (1 - confidence) / 2 * 100
    lo = percentile(means, alpha)
    hi = percentile(means, 100 - alpha)
    assert lo is not None and hi is not None
    return (lo, hi)


def paired_bootstrap_diff(
    a: Sequence[float], b: Sequence[float], *, n_resamples: int = 2000, seed: int = 0
) -> tuple[float, float, float] | None:
    """Mean(a-b) with a 95% paired-bootstrap CI over aligned per-question scores."""
    if len(a) != len(b) or not a:
        return None
    diffs = [x - y for x, y in zip(a, b, strict=True)]
    ci = bootstrap_ci(diffs, n_resamples=n_resamples, seed=seed)
    assert ci is not None
    return (sum(diffs) / len(diffs), ci[0], ci[1])
