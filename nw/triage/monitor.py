"""Drift monitoring for the triage service: what arrives versus what the model was trained on.

Two signals, both cheap enough to compute inside the request path:

- **Input drift**: the length of the text the service receives, against the training
  distribution the artifact carries in `data_profile.json`.
- **Prediction drift**: the share of each predicted priority, against the training label
  distribution. A model that starts predicting P0 twice as often has either met a new
  world or broken; either way a person should look.

Both use the population stability index (PSI) over a sliding window of recent requests:
below 0.1 is stable, 0.1 to 0.2 is worth watching, above 0.2 is an alert. The service
exposes the numbers on `/metrics` and `/drift`, and logs `drift_alert` when the bar is
crossed, which the deployment turns into an alarm on both clouds.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from nw.triage.features import PRIORITIES

WATCH = 0.1
ALERT = 0.2


def psi(expected: list[float], actual: list[float], eps: float = 1e-4) -> float:
    """Population stability index between two share vectors over the same bins."""
    e = np.clip(np.asarray(expected, dtype=float), eps, None)
    a = np.clip(np.asarray(actual, dtype=float), eps, None)
    e, a = e / e.sum(), a / a.sum()
    return float(np.sum((a - e) * np.log(a / e)))


@dataclass
class DriftSnapshot:
    window: int
    text_length_psi: float | None
    priority_psi: float | None
    predicted_share: dict[str, float]
    level: str  # ok | watch | alert | warming_up

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "text_length_psi": self.text_length_psi,
            "priority_psi": self.priority_psi,
            "predicted_share": self.predicted_share,
            "level": self.level,
        }


class DriftMonitor:
    """Sliding window of the last `window` requests, compared with the training profile."""

    def __init__(
        self, profile: dict[str, Any] | None, window: int = 500, min_window: int = 50
    ) -> None:
        self.profile = profile
        self.window = window
        self.min_window = min_window
        self.lengths: deque[int] = deque(maxlen=window)
        self.priorities: deque[str] = deque(maxlen=window)

    @property
    def enabled(self) -> bool:
        return bool(self.profile and self.profile.get("text_length_bins"))

    def observe(self, text_length: int, priority: str) -> None:
        self.lengths.append(text_length)
        self.priorities.append(priority)

    def snapshot(self) -> DriftSnapshot:
        n = len(self.lengths)
        counts = Counter(self.priorities)
        share = {p: counts.get(p, 0) / n if n else 0.0 for p in PRIORITIES}
        if not self.enabled or n < self.min_window:
            return DriftSnapshot(n, None, None, share, "warming_up")
        edges = self.profile["text_length_bins"]
        edges = [e if e != "inf" else math.inf for e in edges]
        actual_hist = np.histogram(np.asarray(self.lengths, dtype=float), bins=edges)[0] / n
        length_psi = psi(self.profile["text_length_hist"], list(actual_hist))
        expected_prio = [self.profile["priority_share"].get(p, 0.0) for p in PRIORITIES]
        prio_psi = psi(expected_prio, [share[p] for p in PRIORITIES])
        worst = max(length_psi, prio_psi)
        level = "alert" if worst >= ALERT else ("watch" if worst >= WATCH else "ok")
        return DriftSnapshot(n, length_psi, prio_psi, share, level)
