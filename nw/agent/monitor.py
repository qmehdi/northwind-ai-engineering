"""Behaviour drift for the agent service: is the agent still working the way it was measured?

The triage service watches its inputs and predictions. An agent has no single prediction
to watch; what moves when the world, the model or a tool changes is how the runs end and
what they cost:

- **Termination mix**: the share of runs stopped by the step cap, the budget or an error
  instead of an answer. A cap rate over 0.3 means the loop is spinning on something.
- **Tool error rate**: errors over tool calls. A tool that started failing shows up here
  before the pass rate does.
- **Steps and cost per run** against a live baseline for this track
  (`data/golden/baselines/agent-<track>.json`) with the population stability index (PSI)
  over fixed bins: below 0.1 stable, 0.1 to 0.2 watch, above 0.2 alert, and never below the
  PSI a window of that size reaches by chance. PSI is off, and `/drift` says why, when the
  baseline is `legacy` (the Claude-era costs in `data/golden/agent_baseline.json`), was
  measured offline with a scripted model (its costs mean nothing), or was measured with
  another Workhorse than the one this service runs.
- **Sampled judge score**: the service scores a sample of live turns with the Judge
  (`JudgeSampler`, `NW_AGENT_JUDGE_SAMPLE`, default one run in twenty) and feeds the score
  here. The window's mean is `nw_agent_judge_score`; below 3.5 over at least 20 judged runs is
  a quality alert, the same bar as the release gate's turn tier.

Every rule waits for `min_window` runs (default 200, the rates from 50) so a handful of runs
cannot alarm. The quality level is `nw_agent_quality_level` (0 ok, 1 watch, 2 alert, -1
warming up). All of it lives in a sliding window inside the service, appears on `/drift`
and `/metrics`, and logs `drift_alert` when a bar is crossed, the same line the deployment
already alarms on for every service. PSI is implemented here without numpy so the agent
image does not pull the training stack in for a subtraction and a logarithm.
"""

from __future__ import annotations

import math
import random
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Gauge
from pydantic import BaseModel, Field

from nw.agent.trace import Termination, Trajectory
from nw.evalstats import psi_critical
from nw.quality import QualityAlerter

STEP_BINS: list[float] = [0, 1, 2, 3, 4, 5, 6, 8, 10, math.inf]
COST_BINS: list[float] = [0, 0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, math.inf]
WATCH = 0.1
ALERT = 0.2
CAP_RATE_ALERT = 0.3
TOOL_ERROR_ALERT = 0.2
CAPPED = {Termination.MAX_STEPS.value, Termination.BUDGET.value, Termination.ERROR.value}
MIN_WINDOW = 200
MIN_RATE_WINDOW = 50
MIN_JUDGED = 20
JUDGE_SCORE_ALERT = 3.5
LEVELS = {"warming_up": -1, "ok": 0, "watch": 1, "alert": 2}
JUDGE_SCORE = Gauge("nw_agent_judge_score", "Mean Judge score of sampled live turns in the window")
QUALITY_LEVEL = Gauge("nw_agent_quality_level", "0 ok, 1 watch, 2 alert, -1 warming up")


def baseline_problem(baseline: dict[str, Any] | None, workhorse: str | None = None) -> str | None:
    """Why a baseline cannot be a cost and steps reference for this service, or None."""
    if not baseline:
        return "no baseline"
    if baseline.get("legacy"):
        return "the baseline is legacy (Claude-era costs)"
    prov = baseline.get("provenance") or {}
    if prov.get("mode") in ("offline", "replay"):
        return "the baseline was measured offline with a scripted model"
    if workhorse and (prov.get("models") or {}).get("workhorse") not in (None, workhorse):
        return f"the baseline was measured with {prov['models']['workhorse']}, not {workhorse}"
    if not (baseline.get("steps") and baseline.get("costs")):
        return "the baseline has no per-case steps and costs"
    return None


class JudgeSampler:
    """Which live runs the Judge scores: a fixed rate, seeded for tests."""

    def __init__(self, rate: float = 0.05, seed: int | None = None) -> None:
        self.rate = max(0.0, min(1.0, rate))
        self.rng = random.Random(seed)

    def should_sample(self) -> bool:
        return self.rate > 0 and self.rng.random() < self.rate


def psi(expected: list[float], actual: list[float], eps: float = 1e-4) -> float:
    """Population stability index between two share vectors over the same bins."""
    if len(expected) != len(actual):
        raise ValueError("psi: the two share vectors must use the same bins")
    e = [max(x, eps) for x in expected]
    a = [max(x, eps) for x in actual]
    se, sa = sum(e), sum(a)
    e = [x / se for x in e]
    a = [x / sa for x in a]
    return float(sum((y - x) * math.log(y / x) for x, y in zip(e, a, strict=True)))


def histogram(values: list[float], edges: list[float]) -> list[float]:
    """Shares per bin; bin i is [edges[i], edges[i+1]). Empty input gives all zeros."""
    counts = [0] * (len(edges) - 1)
    for v in values:
        for i in range(len(edges) - 1):
            if edges[i] <= v < edges[i + 1]:
                counts[i] += 1
                break
    n = len(values)
    return [c / n if n else 0.0 for c in counts]


class RunSummary(BaseModel):
    """One line of `NW_AGENT_CAPTURE`: enough to backtest a drift rule without the trace."""

    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    run_id: str
    agent: str
    agent_version: str | None
    terminated: str
    steps: int
    cost_usd: float
    tool_calls: int
    tool_errors: int
    proposals: int
    resumed_from: str | None = None

    @classmethod
    def from_trajectory(cls, t: Trajectory) -> RunSummary:
        tool_steps = [s for s in t.steps if s.tool and not s.tool.startswith("screen:")]
        return cls(
            run_id=t.run_id,
            agent=t.agent,
            agent_version=t.agent_version,
            terminated=t.terminated.value,
            steps=t.n_steps,
            cost_usd=t.cost_usd,
            tool_calls=len(tool_steps),
            tool_errors=sum(1 for s in tool_steps if not s.ok),
            proposals=len(t.proposed_actions),
            resumed_from=t.resumed_from,
        )


@dataclass
class DriftSnapshot:
    window: int
    cap_rate: float
    error_rate: float
    termination_share: dict[str, float]
    mean_steps: float
    mean_cost_usd: float
    steps_psi: float | None
    cost_psi: float | None
    level: str  # ok | watch | alert | warming_up
    reasons: list[str] = field(default_factory=list)
    judge_score: float | None = None
    judged: int = 0
    quality_level: str = "warming_up"
    psi_off: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "cap_rate": self.cap_rate,
            "error_rate": self.error_rate,
            "termination_share": self.termination_share,
            "mean_steps": self.mean_steps,
            "mean_cost_usd": self.mean_cost_usd,
            "steps_psi": self.steps_psi,
            "cost_psi": self.cost_psi,
            "level": self.level,
            "reasons": self.reasons,
            "judge_score": self.judge_score,
            "judged": self.judged,
            "quality_level": self.quality_level,
            "psi_off": self.psi_off,
        }


class AgentMonitor:
    """Sliding window of the last `window` runs, compared with the track's live baseline."""

    def __init__(
        self,
        baseline: dict[str, Any] | None,
        window: int = 500,
        min_window: int = MIN_WINDOW,
        workhorse: str | None = None,
    ) -> None:
        self.baseline = baseline
        self.window = window
        self.min_window = min_window
        self.rate_window = min(MIN_RATE_WINDOW, min_window)
        self.runs: deque[RunSummary] = deque(maxlen=window)
        self.judge_scores: deque[float] = deque(maxlen=window)
        self.psi_off = baseline_problem(baseline, workhorse)
        self.alerter = QualityAlerter("agent")

    @property
    def enabled(self) -> bool:
        """PSI needs a usable live baseline with per-case steps and costs; the rates never do."""
        return self.psi_off is None

    def observe(self, t: Trajectory) -> RunSummary:
        s = RunSummary.from_trajectory(t)
        self.runs.append(s)
        return s

    def observe_judge(self, score: float) -> None:
        """A sampled live turn's Judge score, 1 to 5."""
        self.judge_scores.append(float(score))

    def snapshot(self) -> DriftSnapshot:
        snap = self._snapshot()
        QUALITY_LEVEL.set(LEVELS[snap.quality_level])
        if snap.judge_score is not None:
            JUDGE_SCORE.set(snap.judge_score)
        self.alerter.check(
            "judge_score",
            snap.quality_level == "alert",
            value=snap.judge_score,
            bar=JUDGE_SCORE_ALERT,
            window=snap.judged,
        )
        return snap

    def _snapshot(self) -> DriftSnapshot:
        n = len(self.runs)
        ends = Counter(r.terminated for r in self.runs)
        share = {k.value: ends.get(k.value, 0) / n if n else 0.0 for k in Termination}
        cap_rate = sum(share[k] for k in CAPPED)
        calls = sum(r.tool_calls for r in self.runs)
        error_rate = sum(r.tool_errors for r in self.runs) / calls if calls else 0.0
        mean_steps = sum(r.steps for r in self.runs) / n if n else 0.0
        mean_cost = sum(r.cost_usd for r in self.runs) / n if n else 0.0
        steps_psi = cost_psi = None
        if self.enabled and n >= self.min_window:
            assert self.baseline is not None
            steps_psi = psi(
                histogram(self.baseline["steps"], STEP_BINS),
                histogram([float(r.steps) for r in self.runs], STEP_BINS),
            )
            cost_psi = psi(
                histogram(self.baseline["costs"], COST_BINS),
                histogram([r.cost_usd for r in self.runs], COST_BINS),
            )
        cost_alert = max(ALERT, psi_critical(n, len(COST_BINS) - 1, 0.99))
        watch = max(WATCH, psi_critical(n, len(STEP_BINS) - 1, 0.95))
        reasons: list[str] = []
        if n >= self.rate_window:
            if cap_rate > CAP_RATE_ALERT:
                reasons.append(f"cap rate {cap_rate:.2f} is above {CAP_RATE_ALERT}")
            if error_rate > TOOL_ERROR_ALERT:
                reasons.append(f"tool error rate {error_rate:.2f} is above {TOOL_ERROR_ALERT}")
        if cost_psi is not None and cost_psi > cost_alert:
            reasons.append(f"cost per run PSI {cost_psi:.3f} is above {cost_alert:.3f}")
        if n < self.rate_window:
            level = "warming_up"
        elif reasons:
            level = "alert"
        elif max(steps_psi or 0.0, cost_psi or 0.0) >= watch:
            level = "watch"
        else:
            level = "ok"
        judged = len(self.judge_scores)
        judge_mean = sum(self.judge_scores) / judged if judged else None
        if judged < MIN_JUDGED:
            quality = "warming_up"
        elif judge_mean is not None and judge_mean < JUDGE_SCORE_ALERT:
            quality = "alert"
            reasons.append(f"sampled judge score {judge_mean:.2f} over {judged} runs")
        else:
            quality = "ok"
        return DriftSnapshot(
            n,
            cap_rate,
            error_rate,
            share,
            mean_steps,
            mean_cost,
            steps_psi,
            cost_psi,
            level,
            reasons,
            judge_score=judge_mean,
            judged=judged,
            quality_level=quality,
            psi_off=self.psi_off,
        )
