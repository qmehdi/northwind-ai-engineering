"""Behaviour drift for the agent service: is the agent still working the way it was measured?

The triage service watches its inputs and predictions. An agent has no single prediction
to watch; what moves when the world, the model or a tool changes is how the runs end and
what they cost:

- **Termination mix**: the share of runs stopped by the step cap, the budget or an error
  instead of an answer. A cap rate over 0.3 means the loop is spinning on something.
- **Tool error rate**: errors over tool calls. A tool that started failing shows up here
  before the pass rate does.
- **Steps and cost per run** against the committed baseline in `data/golden/agent_baseline.json`
  with the population stability index (PSI) over fixed bins: below 0.1 stable, 0.1 to 0.2
  watch, above 0.2 alert.

All of it lives in a sliding window of recent runs inside the service, appears on `/drift`
and `/metrics`, and logs `drift_alert` when a bar is crossed, the same line the deployment
already alarms on for every service. PSI is implemented here without numpy so the agent
image does not pull the training stack in for a subtraction and a logarithm.
"""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from nw.agent.trace import Termination, Trajectory

STEP_BINS: list[float] = [0, 1, 2, 3, 4, 5, 6, 8, 10, math.inf]
COST_BINS: list[float] = [0, 0.02, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, math.inf]
WATCH = 0.1
ALERT = 0.2
CAP_RATE_ALERT = 0.3
TOOL_ERROR_ALERT = 0.2
CAPPED = {Termination.MAX_STEPS.value, Termination.BUDGET.value, Termination.ERROR.value}


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
        }


class AgentMonitor:
    """Sliding window of the last `window` runs, compared with the committed baseline."""

    def __init__(
        self, baseline: dict[str, Any] | None, window: int = 200, min_window: int = 20
    ) -> None:
        self.baseline = baseline
        self.window = window
        self.min_window = min_window
        self.runs: deque[RunSummary] = deque(maxlen=window)

    @property
    def enabled(self) -> bool:
        """PSI needs a baseline with per-case steps and costs; the rates never do."""
        return bool(self.baseline and self.baseline.get("steps") and self.baseline.get("costs"))

    def observe(self, t: Trajectory) -> RunSummary:
        s = RunSummary.from_trajectory(t)
        self.runs.append(s)
        return s

    def snapshot(self) -> DriftSnapshot:
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
        reasons: list[str] = []
        if n >= self.min_window:
            if cap_rate > CAP_RATE_ALERT:
                reasons.append(f"cap rate {cap_rate:.2f} is above {CAP_RATE_ALERT}")
            if error_rate > TOOL_ERROR_ALERT:
                reasons.append(f"tool error rate {error_rate:.2f} is above {TOOL_ERROR_ALERT}")
            if cost_psi is not None and cost_psi > ALERT:
                reasons.append(f"cost per run PSI {cost_psi:.3f} is above {ALERT}")
        if n < self.min_window:
            level = "warming_up"
        elif reasons:
            level = "alert"
        elif max(steps_psi or 0.0, cost_psi or 0.0) >= WATCH:
            level = "watch"
        else:
            level = "ok"
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
        )
