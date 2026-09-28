"""The trajectory: every step of an agent run, replayable from a file.

A trace is the unit of debugging and of evaluation. The eval in Session 5
scores trajectories, not just final answers, and `replay` prints one so a
failed run can be read step by step without re-running it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Termination(StrEnum):
    ANSWER = "answer"
    MAX_STEPS = "max_steps"
    BUDGET = "budget"
    ERROR = "error"


class Step(BaseModel):
    index: int
    thought: str | None = None
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    observation: str | None = None
    ok: bool = True
    pending_approval: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    cost_usd: float = 0.0


class ProposedAction(BaseModel):
    tool: str
    arguments: dict[str, Any]
    step: int


class Trajectory(BaseModel):
    run_id: str
    agent: str
    task: str
    started_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    steps: list[Step] = Field(default_factory=list)
    final: str | None = None
    terminated: Termination = Termination.ANSWER
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    cost_usd: float = 0.0
    tools_called: list[str] = Field(default_factory=list)
    correlation_id: str | None = None
    agent_version: str | None = None  # hash of prompt, tool specs and model ids, see version.py
    model_id: str | None = None  # the model the role resolved to when the run started
    resumed_from: str | None = None  # the run whose proposal this run executed with approval
    tokens_total: int = 0  # input plus output over every model call in the run

    @property
    def n_steps(self) -> int:
        return len(self.steps)

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.run_id}.json"
        path.write_text(self.model_dump_json(indent=1))
        return path

    @classmethod
    def load(cls, path: Path) -> Trajectory:
        return cls.model_validate_json(path.read_text())


def replay(t: Trajectory, width: int = 110) -> str:
    """A readable transcript of a run."""
    lines = [
        f"run {t.run_id}  agent={t.agent}  terminated={t.terminated.value}  "
        f"steps={t.n_steps}  cost={t.cost_usd:.4f} USD"
    ]
    if t.agent_version or t.model_id or t.resumed_from:
        lines.append(
            f"agent_version={t.agent_version or '?'}  model_id={t.model_id or '?'}"
            + (f"  tokens={t.tokens_total}" if t.tokens_total else "")
            + (f"  resumed_from={t.resumed_from}" if t.resumed_from else "")
        )
    lines.append(f"task: {t.task[:width]}")
    for s in t.steps:
        head = f"[{s.index:02d}] "
        if s.thought:
            lines.append(head + "thought: " + s.thought[:width])
            head = "     "
        if s.tool:
            lines.append(head + f"call {s.tool}({json.dumps(s.arguments, default=str)[:width]})")
            mark = "PENDING APPROVAL" if s.pending_approval else ("ok" if s.ok else "ERROR")
            lines.append(f"     -> {mark}: {(s.observation or '')[:width]}")
        lines.append(
            f"     tokens {s.input_tokens}+{s.output_tokens}  {s.latency_ms:.0f} ms  "
            f"{s.cost_usd:.5f} USD"
        )
    if t.proposed_actions:
        lines.append("proposed actions awaiting approval:")
        lines += [
            f"  {p.tool}({json.dumps(p.arguments, default=str)[:width]})"
            for p in t.proposed_actions
        ]
    lines.append(f"final: {(t.final or '')[: width * 3]}")
    return "\n".join(lines)


def main() -> int:
    """`python -m nw.agent.trace artifacts/traces/<run_id>.json` prints a trajectory."""
    import sys

    if len(sys.argv) != 2:
        print("usage: python -m nw.agent.trace <trace.json>")
        return 2
    print(replay(Trajectory.load(Path(sys.argv[1]))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
