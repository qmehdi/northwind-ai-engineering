"""Evaluation statistics every gate and report in the course uses.

A gate on 31 P0 tickets, 77 policy questions or 15 agent cases is a measurement on a small
sample, and a point estimate alone hides how small. This module holds the few tools that
turn a count into evidence, in pure Python so the agent image (no numpy) can import it:

- `wilson`: a 95 percent interval for a rate from k successes in n trials. 28 of 31 is
  0.903, and the interval (0.75, 0.97) says what that number can and cannot tell apart.
- `bootstrap_ci` and `paired_bootstrap`: intervals for a mean, and for the difference of two
  systems scored on the same cases (the champion against the challenger).
- `mcnemar`: the exact paired test for two classifiers on the same items: only the items
  where they disagree carry information.
- `pass_hat_k` and `pass_at_k`: for agents run k times per case. pass^k is the share of
  cases that pass on every one of k runs, the reliability a customer sees; pass@k is the
  share that pass at least once.
- `chi2_ppf` and `psi_critical`: the PSI a stationary stream reaches by chance at a given
  window, so a drift alarm can be sample-size aware instead of firing on noise.
- `as_count`: recover k and n from a rate when a summary kept only the rate.

Every function is deterministic for a fixed seed.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from fractions import Fraction
from typing import Any

Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials. (0, 1) when n is 0."""
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def proportion(k: int, n: int) -> dict[str, Any]:
    """A rate with its counts and its Wilson interval: the shape every report prints."""
    lo, hi = wilson(k, n)
    return {"k": int(k), "n": int(n), "rate": (k / n) if n else None, "lo": lo, "hi": hi}


def fmt_rate(p: dict[str, Any]) -> str:
    """`0.903 (28/31, 95% CI 0.751 to 0.967)`."""
    if not p or not p.get("n"):
        return "n/a (no cases)"
    return f"{p['rate']:.3f} ({p['k']}/{p['n']}, 95% CI {p['lo']:.3f} to {p['hi']:.3f})"


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return math.nan
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[lo])
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo))


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else math.nan


def bootstrap_ci(
    values: Sequence[float],
    stat: Callable[[Sequence[float]], float] = mean,
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile bootstrap interval of `stat` over `values`."""
    vals = list(values)
    if not vals:
        return math.nan, math.nan
    rng = random.Random(seed)
    n = len(vals)
    stats = sorted(stat([vals[rng.randrange(n)] for _ in range(n)]) for _ in range(n_boot))
    return _quantile(stats, alpha / 2), _quantile(stats, 1 - alpha / 2)


def paired_bootstrap(
    a: Sequence[float],
    b: Sequence[float],
    stat: Callable[[Sequence[float]], float] = mean,
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> dict[str, float]:
    """Difference `stat(b) - stat(a)` over paired items (the same cases scored by two
    systems), with a percentile interval from resampling the items together. `a` is the
    champion, `b` the challenger: a negative difference is a drop."""
    if len(a) != len(b):
        raise ValueError("paired_bootstrap: both systems must score the same items")
    if not a:
        return {"diff": math.nan, "lo": math.nan, "hi": math.nan, "n": 0}
    rng = random.Random(seed)
    n = len(a)
    diffs = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        diffs.append(stat([b[i] for i in idx]) - stat([a[i] for i in idx]))
    diffs.sort()
    return {
        "diff": stat(b) - stat(a),
        "lo": _quantile(diffs, alpha / 2),
        "hi": _quantile(diffs, 1 - alpha / 2),
        "n": n,
    }


def mcnemar(b: int, c: int) -> float:
    """Exact two-sided McNemar p-value. `b`: items the champion got right and the challenger
    wrong; `c`: the reverse. Items both got right or both got wrong carry no information."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def paired_counts(champion: Sequence[bool], challenger: Sequence[bool]) -> dict[str, Any]:
    """Per-item correctness of two systems on the same items, as McNemar's table."""
    if len(champion) != len(challenger):
        raise ValueError("paired_counts: both systems must score the same items")
    b = sum(1 for x, y in zip(champion, challenger, strict=True) if x and not y)
    c = sum(1 for x, y in zip(champion, challenger, strict=True) if y and not x)
    return {
        "n": len(champion),
        "champion_only": b,
        "challenger_only": c,
        "net_change": c - b,
        "mcnemar_p": mcnemar(b, c),
    }


def pass_hat_k(runs: Sequence[Sequence[bool]]) -> float:
    """pass^k: the share of cases whose every run passed."""
    return sum(1 for r in runs if r and all(r)) / len(runs) if runs else math.nan


def pass_at_k(runs: Sequence[Sequence[bool]]) -> float:
    """pass@k: the share of cases with at least one passing run."""
    return sum(1 for r in runs if any(r)) / len(runs) if runs else math.nan


def _norm_ppf(p: float) -> float:
    """Inverse standard normal (Acklam's rational approximation, error below 1e-9)."""
    if not 0 < p < 1:
        raise ValueError("p must be in (0, 1)")
    a = (-39.69683028665376, 220.9460984245205, -275.9285104469687, 138.3577518672690,
         -30.66479806614716, 2.506628277459239)  # fmt: skip
    b = (-54.47609879822406, 161.5858368580409, -155.6989798598866, 66.80131188771972,
         -13.28068155288572)  # fmt: skip
    c = (-7.784894002430293e-03, -0.3223964580411365, -2.400758277161838, -2.549732539343734,
         4.374664141464968, 2.938163982698783)  # fmt: skip
    d = (7.784695709041462e-03, 0.3224671290700398, 2.445134137142996, 3.754408661907416)
    lo, hi = 0.02425, 1 - 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > hi:
        return -_norm_ppf(1 - p)
    q = p - 0.5
    r = q * q
    return ((((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q) / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
    )


def chi2_ppf(p: float, df: int) -> float:
    """Chi-square quantile by the Wilson-Hilferty approximation (within 1 percent for df 2+)."""
    if df <= 0:
        raise ValueError("df must be positive")
    z = _norm_ppf(p)
    h = 2 / (9 * df)
    return df * (1 - h + z * math.sqrt(h)) ** 3


def psi_critical(n: int, bins: int, confidence: float = 0.99) -> float:
    """The PSI a window of `n` draws from the baseline distribution itself stays under with
    the given confidence. Under no drift, n times PSI is close to chi-square with bins - 1
    degrees of freedom, so small windows cross 0.2 by chance alone: with 10 bins at n=50
    this returns about 0.43, at n=200 about 0.11. An alarm that ignores this fires on noise."""
    if n <= 0:
        return math.inf
    return chi2_ppf(confidence, max(bins - 1, 1)) / n


def as_count(rate: float | None, n_hint: int | None = None, max_den: int = 2000) -> tuple[int, int]:
    """(k, n) behind a rate. With `n_hint`, k is the rounded count out of that n; without it,
    the smallest fraction that reproduces the rate (0.7419354838709677 is 23/31)."""
    if rate is None:
        return 0, 0
    if n_hint:
        return round(rate * n_hint), int(n_hint)
    f = Fraction(rate).limit_denominator(max_den)
    return f.numerator, f.denominator
