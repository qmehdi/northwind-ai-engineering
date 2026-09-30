"""Drift and quality monitoring for the triage service, and the loop that closes it.

Two drift signals, both cheap enough to compute inside the request path:

- **Input drift**: the length of the text the service receives, against the training
  distribution the artifact carries in `data_profile.json`.
- **Prediction drift**: the share of each predicted priority, against what the model
  predicted on the validation split (`predicted_share` in the profile). Not the label shares:
  a threshold rule predicts P0 more often than P0 occurs, by design, so comparing
  predictions with labels would alarm on a healthy model. A profile written before
  2026-09-30 has no `predicted_share`, and the monitor falls back to the labels and says so.

Both use the population stability index (PSI) over a sliding window: below 0.1 is stable,
0.1 to 0.2 is worth watching, above 0.2 is an alert. A small window crosses 0.2 by chance
alone (at 50 requests about one stationary snapshot in four did), so the monitor waits for
`min_window` (200) requests and raises its bar to the PSI a stationary window of that size
stays under with 99 percent confidence (`nw.evalstats.psi_critical`) when that is higher.

Quality signals for the canary, exported as Prometheus gauges the IaC alarms on:

- `nw_triage_predicted_p0_share` and `nw_triage_p0_share_ratio`: the window's P0 share and
  its ratio to the validation share. A bad model shows here long before a 5xx does.
- `nw_triage_shadow_agreement`: the share of requests where the shadow model agreed.
- `nw_triage_quality_level`: 0 ok, 1 watch, 2 alert, -1 warming up.

A window lives in one process and resets on a cold start, so each instance sees a slice of
traffic. The central path aggregates every instance's capture file (`NW_TRIAGE_CAPTURE`,
written to object storage on the platform) in one place:

    uv run python -m nw.triage.monitor drift --profile artifacts/triage/latest/data_profile.json \
        --capture captures/*.jsonl
    uv run python -m nw.triage.monitor quality --capture captures/*.jsonl --outcomes outcomes.jsonl
    uv run python -m nw.triage.monitor trigger --data data/tickets.jsonl \
        --summary data/golden/triage_production.json [--drift drift.json] [--quality quality.json]

`quality` joins predictions with the priority a person set later (the outcome, keyed by
`ticket_id` or `correlation_id`) once the label is at least `--label-lag-days` old, and prints
live P0 recall and precision with intervals. `trigger` decides whether retraining is worth
running: new data, drift, live quality below the bar, or enough new labels. Same data and no
signal means the same model, so the weekly job skips.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import math
import sys
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from prometheus_client import Gauge

from nw.evalstats import proportion, psi_critical, wilson
from nw.quality import QualityAlerter
from nw.triage.features import PRIORITIES

WATCH = 0.1
ALERT = 0.2
MIN_WINDOW = 200
P0_RATIO_WATCH = 1.5  # window P0 share against the validation share, either direction
P0_RATIO_ALERT = 2.0
SHADOW_AGREEMENT_WATCH = 0.95
SHADOW_AGREEMENT_ALERT = 0.90
LEVELS = {"warming_up": -1, "ok": 0, "watch": 1, "alert": 2}

P0_SHARE = Gauge("nw_triage_predicted_p0_share", "P0 share of predictions in the drift window")
P0_RATIO = Gauge(
    "nw_triage_p0_share_ratio", "Window P0 share over the validation P0 share (1 is normal)"
)
SHADOW_AGREEMENT = Gauge(
    "nw_triage_shadow_agreement", "Share of windowed requests where the shadow model agreed"
)
QUALITY_LEVEL = Gauge("nw_triage_quality_level", "0 ok, 1 watch, 2 alert, -1 warming up")


def psi(expected: list[float], actual: list[float], eps: float = 1e-4) -> float:
    """Population stability index between two share vectors over the same bins."""
    e = np.clip(np.asarray(expected, dtype=float), eps, None)
    a = np.clip(np.asarray(actual, dtype=float), eps, None)
    e, a = e / e.sum(), a / a.sum()
    return float(np.sum((a - e) * np.log(a / e)))


def bars(n: int, bins: int) -> tuple[float, float]:
    """(watch, alert) for a window of n over `bins` bins: the fixed scale, raised to what a
    stationary window of that size reaches by chance (95 and 99 percent)."""
    return max(WATCH, psi_critical(n, bins, 0.95)), max(ALERT, psi_critical(n, bins, 0.99))


def level_of(value: float, n: int, bins: int) -> str:
    watch, alert = bars(n, bins)
    return "alert" if value >= alert else ("watch" if value >= watch else "ok")


def worst(levels: list[str]) -> str:
    return max(levels, key=lambda x: LEVELS[x]) if levels else "ok"


def expected_prediction_share(profile: dict[str, Any]) -> tuple[dict[str, float], str]:
    """The prediction baseline and where it came from."""
    if profile.get("predicted_share"):
        return profile["predicted_share"], "validation predictions"
    return profile.get("priority_share", {}), "training labels (legacy profile)"


def p0_quality(
    p0_count: int, n: int, expected_p0: float | None
) -> tuple[str, float | None, dict[str, Any]]:
    """Is the window's P0 share consistent with the validation share? Alert only when the
    ratio crosses the bar and the 99 percent interval of the window share excludes the
    expected share, so a window of a few hundred cannot alarm on one extra P0."""
    share = proportion(p0_count, n)
    if not expected_p0 or n == 0:
        return "ok", None, share
    ratio = (p0_count / n) / expected_p0
    lo, hi = wilson(p0_count, n, z=2.5758)
    outside = not (lo <= expected_p0 <= hi)
    far = max(ratio, 1 / ratio if ratio else math.inf)
    if outside and far >= P0_RATIO_ALERT:
        return "alert", ratio, share
    if outside and far >= P0_RATIO_WATCH:
        return "watch", ratio, share
    return "ok", ratio, share


@dataclass
class DriftSnapshot:
    window: int
    text_length_psi: float | None
    priority_psi: float | None
    predicted_share: dict[str, float]
    level: str  # ok | watch | alert | warming_up
    alert_bar: float | None = None
    baseline: str = ""
    p0_share_ratio: float | None = None
    shadow_agreement: float | None = None
    shadow_n: int = 0
    quality_level: str = "warming_up"
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, str] = field(default_factory=dict)  # quality signal to its level

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class DriftMonitor:
    """Sliding window of the last `window` requests, compared with the training profile."""

    def __init__(
        self,
        profile: dict[str, Any] | None,
        window: int = 500,
        min_window: int = MIN_WINDOW,
        publisher: Callable[[DriftSnapshot], None] | None = None,
        service: str = "triage",
    ) -> None:
        self.publisher = publisher or publish
        self.alerter = QualityAlerter(service)
        self.profile = profile
        self.window = window
        self.min_window = min_window
        self.lengths: deque[int] = deque(maxlen=window)
        self.priorities: deque[str] = deque(maxlen=window)
        self.shadow: deque[bool] = deque(maxlen=window)

    @property
    def enabled(self) -> bool:
        return bool(self.profile and self.profile.get("text_length_bins"))

    def observe(self, text_length: int, priority: str, shadow_priority: str | None = None) -> None:
        self.lengths.append(text_length)
        self.priorities.append(priority)
        if shadow_priority is not None:
            self.shadow.append(shadow_priority == priority)

    def observe_shadow(self, agree: bool) -> None:
        """The service's shadow comparison for the last request, when it scores one."""
        self.shadow.append(bool(agree))

    def snapshot(self) -> DriftSnapshot:
        snap = compute(
            self.profile if self.enabled else None,
            list(self.lengths),
            list(self.priorities),
            list(self.shadow),
            self.min_window,
        )
        self.publisher(snap)
        self.alerter.check(
            "p0_share",
            snap.signals.get("p0_share") == "alert",
            value=snap.predicted_share.get("P0"),
            bar=P0_RATIO_ALERT,
            window=snap.window,
        )
        self.alerter.check(
            "shadow_agreement",
            snap.signals.get("shadow_agreement") == "alert",
            value=snap.shadow_agreement,
            bar=SHADOW_AGREEMENT_ALERT,
            window=snap.shadow_n,
        )
        return snap


def compute(
    profile: dict[str, Any] | None,
    lengths: list[int],
    priorities: list[str],
    shadow: list[bool],
    min_window: int = MIN_WINDOW,
) -> DriftSnapshot:
    """One snapshot over a window of observations: in process, or central over captures."""
    n = len(lengths)
    counts = Counter(priorities)
    share = {p: counts.get(p, 0) / n if n else 0.0 for p in PRIORITIES}
    agreement = sum(shadow) / len(shadow) if shadow else None
    if not profile or n < min_window:
        return DriftSnapshot(
            n, None, None, share, "warming_up", shadow_agreement=agreement, shadow_n=len(shadow)
        )
    edges = [e if e != "inf" else math.inf for e in profile["text_length_bins"]]
    actual_hist = np.histogram(np.asarray(lengths, dtype=float), bins=edges)[0] / n
    length_psi = psi(profile["text_length_hist"], list(actual_hist))
    expected, source = expected_prediction_share(profile)
    prio_psi = psi([expected.get(p, 0.0) for p in PRIORITIES], [share[p] for p in PRIORITIES])
    drift = worst([level_of(length_psi, n, len(edges) - 1), level_of(prio_psi, n, len(PRIORITIES))])
    reasons: list[str] = []
    if drift == "alert":
        reasons.append(f"PSI text length {length_psi:.3f}, priority {prio_psi:.3f}")
    q_level, ratio, _ = p0_quality(counts.get("P0", 0), n, expected.get("P0"))
    if q_level != "ok":
        reasons.append(f"P0 share {share['P0']:.3f} is {ratio:.2f} times the {source} share")
    s_level = "ok"
    if agreement is not None and len(shadow) >= min_window:
        if agreement < SHADOW_AGREEMENT_ALERT:
            s_level = "alert"
        elif agreement < SHADOW_AGREEMENT_WATCH:
            s_level = "watch"
        if s_level != "ok":
            reasons.append(f"shadow agreement {agreement:.3f} over {len(shadow)} requests")
    return DriftSnapshot(
        n,
        length_psi,
        prio_psi,
        share,
        drift,
        alert_bar=bars(n, len(edges) - 1)[1],
        baseline=source,
        p0_share_ratio=ratio,
        shadow_agreement=agreement,
        shadow_n=len(shadow),
        quality_level=worst([q_level, s_level]),
        reasons=reasons,
        signals={"p0_share": q_level, "shadow_agreement": s_level},
    )


def publish(snap: DriftSnapshot) -> None:
    """Set the quality gauges from a snapshot. Drift gauges stay with the service."""
    QUALITY_LEVEL.set(LEVELS[snap.quality_level])
    if snap.window:
        P0_SHARE.set(snap.predicted_share.get("P0", 0.0))
    if snap.p0_share_ratio is not None:
        P0_RATIO.set(snap.p0_share_ratio)
    if snap.shadow_agreement is not None:
        SHADOW_AGREEMENT.set(snap.shadow_agreement)


# ----- the central path: captures from every instance ----------------------------------


def read_jsonl(patterns: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pattern in patterns:
        for path in sorted(glob.glob(pattern)) or [pattern]:
            p = Path(path)
            if p.exists():
                rows += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]
    return rows


def central_snapshot(
    profile: dict[str, Any], captures: list[dict[str, Any]], min_window: int = MIN_WINDOW
) -> DriftSnapshot:
    """Drift over every captured prediction, whatever instance served it."""
    return compute(
        profile,
        [len(r.get("subject") or "") + len(r.get("body") or "") for r in captures],
        [r["priority"] for r in captures],
        [r["shadow_priority"] == r["priority"] for r in captures if r.get("shadow_priority")],
        min_window,
    )


def join_key(row: dict[str, Any]) -> str | None:
    for k in ("ticket_id", "correlation_id"):
        if row.get(k):
            return f"{k}:{row[k]}"
    return None


def live_quality(
    captures: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    *,
    label_lag_days: float = 3.0,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Predictions joined with the priority a person later set. An outcome younger than
    `label_lag_days` is left out: a ticket downgraded after two days is still in flux."""
    now = now or dt.datetime.now(dt.UTC)
    cutoff = now - dt.timedelta(days=label_lag_days)
    final: dict[str, str] = {}
    for o in outcomes:
        key = join_key(o)
        when = o.get("labelled_at")
        if key is None or not o.get("final_priority"):
            continue
        if when and dt.datetime.fromisoformat(when.replace("Z", "+00:00")) > cutoff:
            continue
        final[key] = o["final_priority"]
    pairs = [(r["priority"], final[k]) for r in captures if (k := join_key(r)) and k in final]
    tp = sum(1 for p, y in pairs if p == "P0" and y == "P0")
    n_p0 = sum(1 for _, y in pairs if y == "P0")
    n_pred = sum(1 for p, _ in pairs if p == "P0")
    return {
        "predictions": len(captures),
        "labelled": len(pairs),
        "label_coverage": len(pairs) / len(captures) if captures else 0.0,
        "label_lag_days": label_lag_days,
        "p0_recall": proportion(tp, n_p0),
        "p0_precision": proportion(tp, n_pred),
        "agreement": proportion(sum(1 for p, y in pairs if p == y), len(pairs)),
    }


@dataclass
class TriggerPolicy:
    min_new_labels: int = 500  # labelled tickets since the production data before a retrain
    min_live_p0: int = 20  # labelled P0 tickets before live recall can trigger
    min_live_p0_recall: float = 0.85  # the gate's own bar


def retrain_trigger(
    *,
    data_sha: str,
    production: dict[str, Any] | None,
    drift: dict[str, Any] | None = None,
    quality: dict[str, Any] | None = None,
    new_labels: int = 0,
    policy: TriggerPolicy | None = None,
) -> dict[str, Any]:
    """Whether a retrain is worth running, and why. No reason means the weekly job skips."""
    policy = policy or TriggerPolicy()
    reasons: list[str] = []
    if production is None:
        reasons.append("no production model yet")
    elif production.get("data_sha256_12") != data_sha:
        reasons.append(
            f"the data changed: {production.get('data_sha256_12')} in production, {data_sha} now"
        )
    if drift and drift.get("level") == "alert":
        reasons.append(f"central drift alert over {drift.get('window')} predictions")
    if drift and drift.get("quality_level") == "alert":
        reasons.append("a quality signal is in alert: " + "; ".join(drift.get("reasons") or []))
    if quality:
        r = quality.get("p0_recall") or {}
        if (r.get("n") or 0) >= policy.min_live_p0 and (r.get("rate") or 1.0) < (
            policy.min_live_p0_recall
        ):
            reasons.append(
                f"live P0 recall {r['rate']:.3f} ({r['k']}/{r['n']}) is below "
                f"{policy.min_live_p0_recall}"
            )
    if new_labels >= policy.min_new_labels:
        reasons.append(f"{new_labels} new labelled tickets since the production data")
    return {"retrain": bool(reasons), "reasons": reasons or ["same data and no signal: skip"]}


def _sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("drift", help="drift and quality over captures from every instance")
    d.add_argument("--profile", type=Path, required=True)
    d.add_argument("--capture", nargs="+", required=True)
    d.add_argument("--min-window", type=int, default=MIN_WINDOW)
    d.add_argument("--out", type=Path, default=None)
    q = sub.add_parser("quality", help="live P0 recall and precision from delayed labels")
    q.add_argument("--capture", nargs="+", required=True)
    q.add_argument("--outcomes", nargs="+", required=True)
    q.add_argument("--label-lag-days", type=float, default=3.0)
    q.add_argument("--out", type=Path, default=None)
    t = sub.add_parser("trigger", help="decide whether a retrain is worth running")
    t.add_argument("--data", type=Path, default=Path("data/tickets.jsonl"))
    t.add_argument("--summary", type=Path, default=Path("data/golden/triage_production.json"))
    t.add_argument("--drift", type=Path, default=None, help="a `drift --out` file")
    t.add_argument("--quality", type=Path, default=None, help="a `quality --out` file")
    t.add_argument("--new-labels", type=int, default=0)
    t.add_argument("--github-output", type=Path, default=None, help="append retrain=true|false")
    args = ap.parse_args(argv)
    if args.cmd == "drift":
        profile = json.loads(args.profile.read_text(encoding="utf-8"))
        snap = central_snapshot(profile, read_jsonl(args.capture), args.min_window)
        out = snap.as_dict()
    elif args.cmd == "quality":
        out = live_quality(
            read_jsonl(args.capture),
            read_jsonl(args.outcomes),
            label_lag_days=args.label_lag_days,
        )
    else:
        production = (
            json.loads(args.summary.read_text(encoding="utf-8")) if args.summary.exists() else None
        )
        load = lambda p: json.loads(p.read_text(encoding="utf-8")) if p else None  # noqa: E731
        out = retrain_trigger(
            data_sha=_sha12(args.data),
            production=production,
            drift=load(args.drift),
            quality=load(args.quality),
            new_labels=args.new_labels,
        )
        if args.github_output:
            with args.github_output.open("a", encoding="utf-8") as f:
                f.write(f"retrain={'true' if out['retrain'] else 'false'}\n")
    text = json.dumps(out, indent=1, default=str)
    print(text)
    if getattr(args, "out", None):
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
