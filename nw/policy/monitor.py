"""Drift monitoring for the policy service: are the questions still the ones the index answers?

A retrieval service has no label to compare against at request time, but it has three
signals that move when the world does, all free to compute inside the request path:

- **Top-hit confidence**: the best retrieved chunk's confidence on the one 0 to 1 scale.
  Questions the corpus does not cover score low. Its distribution over the golden
  questions is measured at index build and stored in the manifest; the service compares
  the last `window` requests with it using the population stability index (PSI), the same
  statistic and the same bar as Project 1: below 0.1 stable, 0.1 to 0.2 watch, above 0.2
  alert.
- **Refusal rate**: the share of refusals in the window against the golden set's share of
  must-refuse cases. Doubling it means the corpus stopped covering what people ask, or a
  document was mis-tagged, or the retriever broke. All three need a person.
- **Answer length**: the mean and PSI of answered lengths, against the baseline run. A
  prompt or model change shows up here before anyone reads an answer.

The service exposes the snapshot on `/drift`, the numbers on `/metrics`, and logs
`drift_alert` when the bar is crossed: the same log line the deployment alarms on for
every service.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

WATCH = 0.1
ALERT = 0.2
REFUSAL_WATCH = 1.5  # times the baseline rate
REFUSAL_ALERT = 2.0
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "confidence_psi": self.confidence_psi,
            "answer_length_psi": self.answer_length_psi,
            "refusal_rate": self.refusal_rate,
            "baseline_refusal_rate": self.baseline_refusal_rate,
            "answer_length_mean": self.answer_length_mean,
            "level": self.level,
        }


class PolicyDriftMonitor:
    """Sliding window of the last `window` uncached requests, compared with the manifest's
    baseline. Cached answers are not observed: they carry no fresh retrieval."""

    def __init__(
        self, baseline: dict[str, Any] | None, window: int = 500, min_window: int = 50
    ) -> None:
        self.baseline = baseline or {}
        self.window = window
        self.min_window = min_window
        self.confidences: deque[float] = deque(maxlen=window)
        self.refusals: deque[bool] = deque(maxlen=window)
        self.lengths: deque[int] = deque(maxlen=window)  # answered only

    @property
    def enabled(self) -> bool:
        return bool(self.baseline.get("confidence_hist"))

    def observe(self, confidence: float, refused: bool, answer_length: int) -> None:
        self.confidences.append(confidence)
        self.refusals.append(refused)
        if not refused:
            self.lengths.append(answer_length)

    def snapshot(self) -> PolicyDriftSnapshot:
        n = len(self.confidences)
        refusal_rate = sum(self.refusals) / n if n else None
        length_mean = statistics.mean(self.lengths) if self.lengths else None
        base_refusal = self.baseline.get("refusal_rate")
        if not self.enabled or n < self.min_window:
            return PolicyDriftSnapshot(
                n, None, None, refusal_rate, base_refusal, length_mean, "warming_up"
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
        ratio = (
            (refusal_rate or 0.0) / base_refusal if base_refusal else None
        )  # None: no baseline rate to compare with
        level = "ok"
        if conf_psi >= WATCH or (ratio is not None and ratio >= REFUSAL_WATCH):
            level = "watch"
        if conf_psi >= ALERT or (ratio is not None and ratio >= REFUSAL_ALERT):
            level = "alert"
        return PolicyDriftSnapshot(
            n, conf_psi, length_psi, refusal_rate, base_refusal, length_mean, level
        )
