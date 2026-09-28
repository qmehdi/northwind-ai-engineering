"""Drift monitoring for the semantic service: the two Project 1 signals plus the tag rate.

Text length and predicted priority reuse `nw.triage.monitor.DriftMonitor` unchanged, the
same PSI over the same profile shape. The third signal is this model's own: the share of
requests carrying each tag, against the tag prevalence of the training rows that the
artifact's `data_profile.json` records as `tag_share`. A service that starts tagging
every second ticket "Outage" has either met a new world or broken; either way a person
should look. Same scale: below 0.1 stable, 0.1 to 0.2 watch, above 0.2 alert.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from nw.semantic.data import TAGS
from nw.triage.monitor import ALERT, WATCH, DriftMonitor, psi

__all__ = ["ALERT", "WATCH", "DriftMonitor", "SemanticDriftMonitor", "psi"]


@dataclass
class SemanticDriftSnapshot:
    window: int
    text_length_psi: float | None
    priority_psi: float | None
    tag_rate_psi: float | None
    predicted_share: dict[str, float]
    tag_share: dict[str, float]  # the ten most frequent tags in the window
    level: str  # ok | watch | alert | warming_up

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "text_length_psi": self.text_length_psi,
            "priority_psi": self.priority_psi,
            "tag_rate_psi": self.tag_rate_psi,
            "predicted_share": self.predicted_share,
            "tag_share": self.tag_share,
            "level": self.level,
        }


class SemanticDriftMonitor:
    """Sliding window of the last `window` requests against the training profile."""

    def __init__(
        self, profile: dict[str, Any] | None, window: int = 500, min_window: int = 50
    ) -> None:
        self.profile = profile
        self.inner = DriftMonitor(profile, window=window, min_window=min_window)
        self.min_window = min_window
        self.tags: deque[tuple[str, ...]] = deque(maxlen=window)

    @property
    def enabled(self) -> bool:
        return self.inner.enabled

    def observe(self, text_length: int, priority: str, tags: Iterable[str]) -> None:
        self.inner.observe(text_length, priority)
        self.tags.append(tuple(tags))

    def tag_rate_psi(self) -> float | None:
        expected = (self.profile or {}).get("tag_share")
        n = len(self.tags)
        if not expected or n < self.min_window:
            return None
        counts = Counter(t for ts in self.tags for t in ts)
        return psi([expected.get(t, 0.0) for t in TAGS], [counts.get(t, 0) / n for t in TAGS])

    def snapshot(self) -> SemanticDriftSnapshot:
        base = self.inner.snapshot()
        n = len(self.tags)
        counts = Counter(t for ts in self.tags for t in ts)
        top = {t: c / n for t, c in counts.most_common(10)} if n else {}
        tag_psi = self.tag_rate_psi()
        if base.level == "warming_up":
            return SemanticDriftSnapshot(
                base.window, None, None, None, base.predicted_share, top, "warming_up"
            )
        worst = max(x for x in (base.text_length_psi, base.priority_psi, tag_psi) if x is not None)
        level = "alert" if worst >= ALERT else ("watch" if worst >= WATCH else "ok")
        return SemanticDriftSnapshot(
            base.window,
            base.text_length_psi,
            base.priority_psi,
            tag_psi,
            base.predicted_share,
            top,
            level,
        )
