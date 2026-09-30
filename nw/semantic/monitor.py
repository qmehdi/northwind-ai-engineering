"""Drift monitoring for the semantic service: the two Project 1 signals plus the tag rate.

Text length and predicted priority reuse `nw.triage.monitor` unchanged: the same PSI, the
same 200-request minimum window, the same bar raised to the chance level of the window, and
predictions compared with the validation predictions (`predicted_share` in the profile) when
the artifact carries them, else with the label shares and a note that says so.

The third signal is this model's own: the share of requests carrying each tag, against the
validation tag rate (`predicted_tag_share`) or, for an older artifact, the training rows'
tag prevalence (`tag_share`). With 52 tags a window of 50 requests reaches a PSI of 0.2 by
chance about four times in five; the bar here is the fixed 0.2 or the 99 percent chance level
of the window (about 0.39 at 200 requests over 52 tags), whichever is higher.

Quality gauges for the canary, the same shape as Project 1 under the semantic prefix:
`nw_semantic_predicted_p0_share`, `nw_semantic_p0_share_ratio`,
`nw_semantic_shadow_agreement` and `nw_semantic_quality_level` (0 ok, 1 watch, 2 alert,
-1 warming up).
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import Gauge

from nw.semantic.data import TAGS
from nw.triage.monitor import (
    ALERT,
    LEVELS,
    MIN_WINDOW,
    WATCH,
    DriftMonitor,
    DriftSnapshot,
    level_of,
    psi,
    worst,
)

__all__ = ["ALERT", "WATCH", "DriftMonitor", "SemanticDriftMonitor", "psi"]

P0_SHARE = Gauge("nw_semantic_predicted_p0_share", "P0 share of predictions in the drift window")
P0_RATIO = Gauge(
    "nw_semantic_p0_share_ratio", "Window P0 share over the validation P0 share (1 is normal)"
)
SHADOW_AGREEMENT = Gauge(
    "nw_semantic_shadow_agreement", "Share of windowed requests where the shadow model agreed"
)
QUALITY_LEVEL = Gauge("nw_semantic_quality_level", "0 ok, 1 watch, 2 alert, -1 warming up")


def publish(snap: DriftSnapshot) -> None:
    QUALITY_LEVEL.set(LEVELS[snap.quality_level])
    if snap.window:
        P0_SHARE.set(snap.predicted_share.get("P0", 0.0))
    if snap.p0_share_ratio is not None:
        P0_RATIO.set(snap.p0_share_ratio)
    if snap.shadow_agreement is not None:
        SHADOW_AGREEMENT.set(snap.shadow_agreement)


@dataclass
class SemanticDriftSnapshot:
    window: int
    text_length_psi: float | None
    priority_psi: float | None
    tag_rate_psi: float | None
    predicted_share: dict[str, float]
    tag_share: dict[str, float]  # the ten most frequent tags in the window
    level: str  # ok | watch | alert | warming_up
    quality_level: str = "warming_up"
    p0_share_ratio: float | None = None
    shadow_agreement: float | None = None
    baseline: str = ""
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "text_length_psi": self.text_length_psi,
            "priority_psi": self.priority_psi,
            "tag_rate_psi": self.tag_rate_psi,
            "predicted_share": self.predicted_share,
            "tag_share": self.tag_share,
            "level": self.level,
            "quality_level": self.quality_level,
            "p0_share_ratio": self.p0_share_ratio,
            "shadow_agreement": self.shadow_agreement,
            "baseline": self.baseline,
            "reasons": self.reasons,
        }


class SemanticDriftMonitor:
    """Sliding window of the last `window` requests against the training profile."""

    def __init__(
        self, profile: dict[str, Any] | None, window: int = 500, min_window: int = MIN_WINDOW
    ) -> None:
        self.profile = profile
        self.inner = DriftMonitor(
            profile, window=window, min_window=min_window, publisher=publish, service="semantic"
        )
        self.min_window = min_window
        self.tags: deque[tuple[str, ...]] = deque(maxlen=window)

    @property
    def enabled(self) -> bool:
        return self.inner.enabled

    def observe(
        self,
        text_length: int,
        priority: str,
        tags: Iterable[str],
        shadow_priority: str | None = None,
    ) -> None:
        self.inner.observe(text_length, priority, shadow_priority)
        self.tags.append(tuple(tags))

    def observe_shadow(self, agree: bool) -> None:
        self.inner.observe_shadow(agree)

    def expected_tags(self) -> dict[str, float] | None:
        p = self.profile or {}
        return p.get("predicted_tag_share") or p.get("tag_share")

    def tag_rate_psi(self) -> float | None:
        expected = self.expected_tags()
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
                base.window,
                None,
                None,
                None,
                base.predicted_share,
                top,
                "warming_up",
                shadow_agreement=base.shadow_agreement,
            )
        levels = [base.level]
        reasons = list(base.reasons)
        if tag_psi is not None:
            tag_level = level_of(tag_psi, n, len(TAGS))
            levels.append(tag_level)
            if tag_level == "alert":
                reasons.append(f"tag rate PSI {tag_psi:.3f} over {n} requests")
        return SemanticDriftSnapshot(
            base.window,
            base.text_length_psi,
            base.priority_psi,
            tag_psi,
            base.predicted_share,
            top,
            worst(levels),
            quality_level=base.quality_level,
            p0_share_ratio=base.p0_share_ratio,
            shadow_agreement=base.shadow_agreement,
            baseline=base.baseline,
            reasons=reasons,
        )
