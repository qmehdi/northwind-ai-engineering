"""Drift monitoring for the policy service: are the questions still the ones the index answers?

A retrieval service has no label to compare against at request time, but it has three
signals that move when the world does, all free to compute inside the request path:

- **Top-hit confidence**: the best retrieved chunk's confidence on the one 0 to 1 scale.
  Questions the corpus does not cover score low. The service compares the last `window`
  requests with the baseline distribution using the population stability index (PSI), the
  same statistic and the same scale as Project 1: below 0.1 stable, 0.1 to 0.2 watch, above
  0.2 alert, and never below the PSI a window of that size reaches by chance (with ten bins
  and 200 requests about 0.11 at 99 percent), from 200 requests.
- **Refusal rate**: the share of refusals in the window against the baseline's. Doubling it
  means the corpus stopped covering what people ask, or a document was mis-tagged, or the
  retriever broke. All three need a person. The alert needs the ratio and a 99 percent
  interval that excludes the baseline rate, so a window of 200 cannot alarm on three refusals.
- **Answer length**: the mean and PSI of answered lengths, against the baseline run. A
  prompt or model change shows up here before anyone reads an answer.

Where the baseline comes from matters. `build_index` writes it from the golden questions by
default, and the golden set is not traffic: over a quarter of its cases are must-refuse
questions written to be refused, and its answer lengths come from whatever model wrote the
baseline run. So the golden baseline is a placeholder for the first week. After a week of
traffic, rebuild the index with `--capture <NW_POLICY_CAPTURE file>` and the service
compares itself with its own traffic (`source: capture:...`); `/drift` reports the source
and says `golden baseline: replace with a week of traffic` until then.

Quality gauges for the canary: `nw_policy_refusal_rate`, `nw_policy_refusal_ratio` (window
over baseline) and `nw_policy_quality_level` (0 ok, 1 watch, 2 alert, -1 warming up).

The service exposes the snapshot on `/drift`, the numbers on `/metrics`, and logs
`drift_alert` when the bar is crossed: the same log line the deployment alarms on for
every service.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from prometheus_client import Gauge

from nw.evalstats import psi_critical, wilson
from nw.quality import QualityAlerter

WATCH = 0.1
ALERT = 0.2
MIN_WINDOW = 200
REFUSAL_WATCH = 1.5  # times the baseline rate
REFUSAL_ALERT = 2.0
LEVELS = {"warming_up": -1, "ok": 0, "watch": 1, "alert": 2}
REFUSAL_RATE = Gauge("nw_policy_refusal_rate", "Refusal share in the drift window")
REFUSAL_RATIO = Gauge("nw_policy_refusal_ratio", "Window refusal share over the baseline share")
QUALITY_LEVEL = Gauge("nw_policy_quality_level", "0 ok, 1 watch, 2 alert, -1 warming up")
CONFIDENCE_EDGES = [round(i / 10, 1) for i in range(11)]  # ten bins over the 0 to 1 scale


def psi(expected: list[float], actual: list[float], eps: float = 1e-4) -> float:
    """Population stability index between two share vectors over the same bins."""
    e = np.clip(np.asarray(expected, dtype=float), eps, None)
    a = np.clip(np.asarray(actual, dtype=float), eps, None)
    e, a = e / e.sum(), a / a.sum()
    return float(np.sum((a - e) * np.log(a / e)))


def _edges(edges: list[Any]) -> list[float]:
    return [math.inf if e == "inf" else float(e) for e in edges]


def shares(values: list[float], edges: list[Any]) -> list[float]:
    """Histogram shares over `edges`; the top edge is inclusive so 1.0 lands in the last bin."""
    if not values:
        return [0.0] * (len(edges) - 1)
    counts = np.histogram(np.asarray(values, dtype=float), bins=_edges(edges))[0]
    return [float(c) / len(values) for c in counts]


def make_baseline(
    confidences: list[float],
    refusal_rate: float,
    answer_lengths: list[int] | None = None,
) -> dict[str, Any]:
    """The distribution block the index manifest carries. `confidences` are the top-hit
    confidences over the golden questions; `answer_lengths` come from the baseline run when
    there is one."""
    baseline: dict[str, Any] = {
        "n": len(confidences),
        "confidence_bins": CONFIDENCE_EDGES,
        "confidence_hist": shares(confidences, CONFIDENCE_EDGES),
        "confidence_p50": statistics.median(confidences) if confidences else None,
        "refusal_rate": refusal_rate,
    }
    if answer_lengths:
        qs = np.quantile(answer_lengths, [0.2, 0.4, 0.6, 0.8]).tolist()
        edges: list[Any] = [0.0, *[float(q) for q in qs], "inf"]
        baseline["answer_length_bins"] = edges
        baseline["answer_length_hist"] = shares([float(x) for x in answer_lengths], edges)
        baseline["answer_length_mean"] = statistics.mean(answer_lengths)
    return baseline


@dataclass
class PolicyDriftSnapshot:
    window: int
    confidence_psi: float | None
    answer_length_psi: float | None
    refusal_rate: float | None
    baseline_refusal_rate: float | None
    answer_length_mean: float | None
    level: str  # ok | watch | alert | warming_up
    quality_level: str = "warming_up"
    refusal_ratio: float | None = None
    baseline_source: str = ""
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "confidence_psi": self.confidence_psi,
            "answer_length_psi": self.answer_length_psi,
            "refusal_rate": self.refusal_rate,
            "baseline_refusal_rate": self.baseline_refusal_rate,
            "answer_length_mean": self.answer_length_mean,
            "level": self.level,
            "quality_level": self.quality_level,
            "refusal_ratio": self.refusal_ratio,
            "baseline_source": self.baseline_source,
            "notes": self.notes,
        }


def _psi_level(value: float, n: int, bins: int) -> str:
    alert = max(ALERT, psi_critical(n, bins, 0.99))
    watch = max(WATCH, psi_critical(n, bins, 0.95))
    return "alert" if value >= alert else ("watch" if value >= watch else "ok")


def refusal_level(refused: int, n: int, base: float | None) -> tuple[str, float | None]:
    """The ratio bar, applied only when the window's 99 percent interval excludes the
    baseline rate."""
    if not base or n == 0:
        return "ok", None
    ratio = (refused / n) / base
    lo, hi = wilson(refused, n, z=2.5758)
    if lo <= base <= hi:
        return "ok", ratio
    if ratio >= REFUSAL_ALERT:
        return "alert", ratio
    if ratio >= REFUSAL_WATCH:
        return "watch", ratio
    return "ok", ratio


class PolicyDriftMonitor:
    """Sliding window of the last `window` uncached requests, compared with the manifest's
    baseline. Cached answers are not observed: they carry no fresh retrieval."""

    def __init__(
        self, baseline: dict[str, Any] | None, window: int = 500, min_window: int = MIN_WINDOW
    ) -> None:
        self.baseline = baseline or {}
        self.window = window
        self.min_window = min_window
        self.confidences: deque[float] = deque(maxlen=window)
        self.refusals: deque[bool] = deque(maxlen=window)
        self.lengths: deque[int] = deque(maxlen=window)  # answered only
        self.alerter = QualityAlerter("policy")

    @property
    def enabled(self) -> bool:
        return bool(self.baseline.get("confidence_hist"))

    def observe(self, confidence: float, refused: bool, answer_length: int) -> None:
        self.confidences.append(confidence)
        self.refusals.append(refused)
        if not refused:
            self.lengths.append(answer_length)

    def snapshot(self) -> PolicyDriftSnapshot:
        snap = self._snapshot()
        QUALITY_LEVEL.set(LEVELS[snap.quality_level])
        if snap.refusal_rate is not None:
            REFUSAL_RATE.set(snap.refusal_rate)
        if snap.refusal_ratio is not None:
            REFUSAL_RATIO.set(snap.refusal_ratio)
        self.alerter.check(
            "refusal_rate",
            snap.quality_level == "alert",
            value=snap.refusal_rate,
            bar=REFUSAL_ALERT,
            window=snap.window,
        )
        return snap

    def _snapshot(self) -> PolicyDriftSnapshot:
        n = len(self.confidences)
        refused = sum(self.refusals)
        refusal_rate = refused / n if n else None
        length_mean = statistics.mean(self.lengths) if self.lengths else None
        base_refusal = self.baseline.get("refusal_rate")
        source = str(self.baseline.get("source", ""))
        notes = []
        if source.startswith("golden"):
            notes.append("golden baseline: replace with a week of traffic (build_index --capture)")
        if not self.enabled or n < self.min_window:
            return PolicyDriftSnapshot(
                n,
                None,
                None,
                refusal_rate,
                base_refusal,
                length_mean,
                "warming_up",
                baseline_source=source,
                notes=notes,
            )
        conf_psi = psi(
            self.baseline["confidence_hist"],
            shares(list(self.confidences), self.baseline["confidence_bins"]),
        )
        length_psi = None
        if self.baseline.get("answer_length_hist") and self.lengths:
            length_psi = psi(
                self.baseline["answer_length_hist"],
                shares([float(x) for x in self.lengths], self.baseline["answer_length_bins"]),
            )
        level = _psi_level(conf_psi, n, len(self.baseline["confidence_hist"]))
        quality, ratio = refusal_level(refused, n, base_refusal)
        if LEVELS[quality] > LEVELS[level]:
            level = quality
        return PolicyDriftSnapshot(
            n,
            conf_psi,
            length_psi,
            refusal_rate,
            base_refusal,
            length_mean,
            level,
            quality_level=quality,
            refusal_ratio=ratio,
            baseline_source=source,
            notes=notes,
        )
